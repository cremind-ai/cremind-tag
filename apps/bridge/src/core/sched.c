/* Tag connection scheduler: the mesh suspend window of docs/protocol.md 5.2. */
#include <errno.h>
#include <string.h>

#include <zephyr/sys/util.h>

#include "sched.h"

static uint32_t now(const struct sched *s)
{
	return s->ops->now(s->ctx);
}

static void set_state(struct sched *s, uint8_t state)
{
	s->state = state;
	s->t_state = now(s);
}

static void arm(struct sched *s, uint32_t ms)
{
	s->ops->timer(s->ctx, ms);
}

static bool after(uint32_t t, uint32_t at)
{
	return (int32_t)(t - at) >= 0;
}

/* ---- Per-tag back-off (BRIDGE_TAG_BACKOFF_MS after a failure) ---- */

static void backoff(struct sched *s, uint32_t tag_id, uint8_t status)
{
	uint32_t t = now(s);
	struct sched_backoff *b = NULL;
	size_t i;

	s->last_status = status;
	for (i = 0; i < ARRAY_SIZE(s->backoff); i++) {
		struct sched_backoff *e = &s->backoff[i];

		if (e->used && e->tag_id == tag_id) {
			b = e;
			break;
		}
		if (b == NULL || !e->used || (b->used && after(b->until, e->until))) {
			b = e; /* a free entry, else the one expiring first */
		}
	}
	b->used = true;
	b->tag_id = tag_id;
	b->until = t + CTAG_BRIDGE_TAG_BACKOFF_MS;
}

static bool in_backoff(const struct sched *s, uint32_t tag_id, uint32_t t)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(s->backoff); i++) {
		const struct sched_backoff *e = &s->backoff[i];

		if (e->used && e->tag_id == tag_id) {
			return !after(t, e->until);
		}
	}
	return false;
}

/* ---- Rolling-minute suspend limit (BRIDGE_MAX_SUSPENDS_PER_MIN) ---- */

bool sched_rate_ok(const struct sched *s, uint32_t t)
{
	/* suspends[] ascends; [0] is the oldest of the last N attempts. */
	return s->n_suspends < CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN ||
	       t - s->suspends[0] >= SCHED_WINDOW_MS;
}

static void record_suspend(struct sched *s, uint32_t t)
{
	if (s->n_suspends == CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN) {
		memmove(&s->suspends[0], &s->suspends[1],
			(CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN - 1u) * sizeof(s->suspends[0]));
		s->n_suspends--;
	}
	s->suspends[s->n_suspends++] = t;
}

/* ---- The window ---- */

void sched_init(struct sched *s, const struct sched_ops *ops, void *ctx)
{
	memset(s, 0, sizeof(*s));
	s->ops = ops;
	s->ctx = ctx;
	s->last_status = CTAG_STATUS_OK;
}

bool sched_busy(const struct sched *s)
{
	return s->state != SCHED_IDLE;
}

bool sched_deaf(const struct sched *s, uint32_t t, uint32_t heard_ms, uint32_t limit_ms)
{
	/* Idle means scanning (the mesh runs); silence counts from the later of the
	 * last report and the moment the scheduler went idle. */
	return limit_ms != 0u && s->state == SCHED_IDLE && t - heard_ms >= limit_ms &&
	       t - s->t_state >= limit_ms;
}

static void to_idle(struct sched *s)
{
	set_state(s, SCHED_IDLE);
	arm(s, SCHED_TIMER_OFF);
}

static void to_disconnecting(struct sched *s)
{
	(void)s->ops->disconnect(s->ctx);
	set_state(s, SCHED_DISCONNECTING);
	arm(s, SCHED_DISCONNECT_WAIT_MS);
}

/*
 * RECOVERY: retry until the mesh runs again, reboot after SCHED_RECOVERY_MS.
 * unknown: bt_mesh_suspend() failed part-way (it may have stopped the scanner
 * and not flagged the mesh suspended, so bt_mesh_resume() alone would answer
 * -EALREADY and change nothing): each retry suspends fully, then resumes.
 */
static void enter_recovery(struct sched *s, bool unknown)
{
	s->c.recoveries++;
	s->mesh_unknown = unknown;
	set_state(s, SCHED_RECOVERY);
	s->retry_ms = SCHED_RESUME_RETRY_MS;
	arm(s, s->retry_ms);
}

/*
 * 5.2 steps 6-7: resume after the attempt ended (connected, failed or
 * cancelled). session: the link is up and may carry the session. Otherwise
 * fail_status is recorded (with the per-tag back-off).
 */
static void resume_after_attempt(struct sched *s, bool session, uint8_t fail_status)
{
	int err = s->ops->mesh_resume(s->ctx);
	uint32_t t = now(s);

	s->last_suspend_ms = t - s->t_suspend;
	s->c.suspend_max_ms = MAX(s->c.suspend_max_ms, s->last_suspend_ms);
	if (err == 0 || err == -EALREADY) {
		s->mesh_suspended = false;
		if (session && s->conn_up) {
			set_state(s, SCHED_SESSION);
			arm(s, SCHED_TIMER_OFF);
			s->ops->session_start(s->ctx, s->tag_id, s->last_suspend_ms);
			return;
		}
		if (fail_status == CTAG_STATUS_CONNECT_FAILED) {
			s->c.connect_failed++;
		}
		backoff(s, s->tag_id, fail_status);
		if (s->conn_up) {
			to_disconnecting(s);
		} else {
			to_idle(s);
		}
		return;
	}
	/* Resume failure: no session on this link; controlled recovery. */
	s->c.resume_fail++;
	backoff(s, s->tag_id, CTAG_STATUS_MESH_RESUME_FAILED);
	if (s->conn_up) {
		(void)s->ops->disconnect(s->ctx);
	}
	enter_recovery(s, false);
}

static void attempt(struct sched *s)
{
	uint32_t t = now(s);
	int err;

	record_suspend(s, t);
	s->c.attempts++;
	s->c.suspend_count++;
	err = s->ops->mesh_suspend(s->ctx);
	if (err == -EALREADY) {
		/* Someone left the mesh suspended: never keep it that way. */
		s->c.suspend_fail++;
		s->mesh_suspended = true;
		s->t_suspend = t;
		s->conn_up = false;
		resume_after_attempt(s, false, CTAG_STATUS_MESH_SUSPEND_FAILED);
		return;
	}
	if (err != 0) {
		/* 5.2 step 4: no connection is attempted. */
		s->c.suspend_fail++;
		backoff(s, s->tag_id, CTAG_STATUS_MESH_SUSPEND_FAILED);
		if (err == -EINVAL || err == -EBUSY) {
			to_idle(s); /* refused before touching anything (not ready, provisioning) */
		} else {
			enter_recovery(s, true); /* failed part-way: the mesh state is unknown */
		}
		return;
	}
	s->mesh_suspended = true;
	s->t_suspend = t;
	s->conn_up = false;
	err = s->ops->conn_create(s->ctx, &s->peer, CTAG_BRIDGE_CONN_ATTEMPT_MS);
	if (err != 0) {
		resume_after_attempt(s, false, CTAG_STATUS_CONNECT_FAILED);
		return;
	}
	set_state(s, SCHED_CONNECTING);
	arm(s, CTAG_BRIDGE_CONN_ATTEMPT_MS + SCHED_ATTEMPT_GRACE_MS);
}

/* Preconditions of 5.2 steps 1-3 other than the local sends. */
static bool may_attempt(struct sched *s, uint32_t tag_id, bool count)
{
	uint32_t t = now(s);

	if (!s->ops->has_work(s->ctx, tag_id)) {
		return false;
	}
	if (!s->ops->node_ready(s->ctx)) {
		s->c.not_ready += count ? 1u : 0u;
		return false;
	}
	if (in_backoff(s, tag_id, t)) {
		s->c.backoff_skips += count ? 1u : 0u;
		return false;
	}
	if (!sched_rate_ok(s, t)) {
		s->c.rate_limited += count ? 1u : 0u;
		return false;
	}
	return true;
}

void sched_advert(struct sched *s, uint32_t tag_id, const struct sched_peer *peer)
{
	if (s->state != SCHED_IDLE || !may_attempt(s, tag_id, true)) {
		return;
	}
	s->tag_id = tag_id;
	s->peer = *peer;
	if (s->ops->mesh_busy(s->ctx)) {
		/* 5.2 step 3: let our own mesh sends finish, or defer. */
		set_state(s, SCHED_WAIT_SENDS);
		arm(s, SCHED_SEND_POLL_MS);
		return;
	}
	attempt(s);
}

void sched_connected(struct sched *s, uint8_t err)
{
	if (s->state == SCHED_CONNECTING || s->state == SCHED_CANCELLING) {
		bool session = s->state == SCHED_CONNECTING && err == 0u;

		s->conn_up = err == 0u;
		arm(s, SCHED_TIMER_OFF);
		resume_after_attempt(s, session, CTAG_STATUS_CONNECT_FAILED);
		return;
	}
	if (err == 0u) {
		/* A connection we no longer want (late completion): take it down. */
		s->conn_up = true;
		if (s->state == SCHED_IDLE) {
			to_disconnecting(s);
		} else {
			(void)s->ops->disconnect(s->ctx);
		}
	}
}

static void session_end(struct sched *s, uint8_t status)
{
	if (status == CTAG_STATUS_OK) {
		s->c.sessions_ok++;
		s->last_status = CTAG_STATUS_OK;
	} else {
		s->c.sessions_fail++;
		backoff(s, s->tag_id, status);
	}
	if (s->conn_up) {
		to_disconnecting(s);
	} else {
		to_idle(s);
	}
}

void sched_session_done(struct sched *s, uint8_t status)
{
	if (s->state == SCHED_SESSION) {
		session_end(s, status);
	}
}

void sched_disconnected(struct sched *s)
{
	s->conn_up = false;
	switch (s->state) {
	case SCHED_SESSION:
		/* The session reports done (normally from inside this call). */
		s->ops->session_abort(s->ctx, CTAG_STATUS_DISCONNECTED);
		if (s->state == SCHED_SESSION) {
			session_end(s, CTAG_STATUS_DISCONNECTED);
		}
		break;
	case SCHED_DISCONNECTING:
		to_idle(s);
		break;
	default:
		break;
	}
}

void sched_timeout(struct sched *s)
{
	uint32_t t = now(s);
	int err;

	switch (s->state) {
	case SCHED_WAIT_SENDS:
		if (!s->ops->mesh_busy(s->ctx)) {
			if (may_attempt(s, s->tag_id, false)) {
				attempt(s);
			} else {
				to_idle(s);
			}
		} else if (t - s->t_state >= SCHED_SEND_WAIT_MS) {
			s->c.deferred++; /* retried on a later advertisement */
			to_idle(s);
		} else {
			arm(s, SCHED_SEND_POLL_MS);
		}
		break;
	case SCHED_CONNECTING:
		/* The host's own create timeout did not report: cancel explicitly. */
		s->c.cancels++;
		(void)s->ops->conn_cancel(s->ctx);
		set_state(s, SCHED_CANCELLING);
		arm(s, SCHED_CANCEL_WAIT_MS);
		break;
	case SCHED_CANCELLING:
		/* No confirmation: resume anyway (a refused resume enters recovery). */
		resume_after_attempt(s, false, CTAG_STATUS_CONNECT_FAILED);
		break;
	case SCHED_RECOVERY:
		if (s->mesh_unknown) {
			err = s->ops->mesh_suspend(s->ctx);
			if (err == 0 || err == -EALREADY) {
				s->mesh_unknown = false;
				s->mesh_suspended = true;
			}
		}
		err = s->mesh_unknown ? -EIO : s->ops->mesh_resume(s->ctx);
		if (err == 0 || err == -EALREADY) {
			s->mesh_suspended = false;
			if (s->conn_up) {
				set_state(s, SCHED_DISCONNECTING);
				arm(s, SCHED_DISCONNECT_WAIT_MS);
			} else {
				to_idle(s);
			}
			break;
		}
		s->c.resume_fail++;
		if (t - s->t_state >= SCHED_RECOVERY_MS) {
			/* The mesh stack reloads from settings after the reset. */
			s->c.reboots++;
			s->ops->reboot(s->ctx);
		}
		s->retry_ms = MIN(2u * s->retry_ms, SCHED_RESUME_RETRY_MAX);
		arm(s, s->retry_ms);
		break;
	case SCHED_DISCONNECTING:
		s->conn_up = false; /* no disconnected event: do not block forever */
		to_idle(s);
		break;
	default:
		break;
	}
}
