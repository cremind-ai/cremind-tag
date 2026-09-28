/*
 * Tag connection scheduler: the mesh suspend window of docs/protocol.md 5.2
 * (include/ctag/ctag_sched.h). Portable C: no Zephyr or Bluetooth calls, the
 * sizes from ctag_sched.h.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_sched.h>

#define ARRAY_LEN(a) (sizeof(a) / sizeof((a)[0]))

static uint32_t min_u32(uint32_t a, uint32_t b)
{
	return a < b ? a : b;
}

static uint32_t max_u32(uint32_t a, uint32_t b)
{
	return a > b ? a : b;
}

static uint32_t now(const struct sched *s)
{
	return s->ops->now(s->ctx);
}

static bool after(uint32_t t, uint32_t at)
{
	return (int32_t)(t - at) >= 0;
}

static void set_state(struct sched *s, uint8_t state)
{
	s->state = state;
	s->t_state = now(s);
}

static void set_link(struct sched *s, uint8_t i, uint8_t state)
{
	s->links[i].state = state;
	s->links[i].t_state = now(s);
}

/*
 * One timer serves the initiator's deadline and every link waiting for its
 * disconnected event: it is armed for the earliest of them.
 */
static void rearm(struct sched *s)
{
	uint32_t t = now(s);
	uint32_t next = SCHED_TIMER_OFF;
	size_t i;

	if (s->armed) {
		next = after(t, s->t_deadline) ? 0u : s->t_deadline - t;
	}
	for (i = 0; i < SCHED_LINKS; i++) {
		const struct sched_link *l = &s->links[i];
		uint32_t dl = l->t_state + SCHED_DISCONNECT_WAIT_MS;

		if (l->state == SCHED_LINK_DISCONNECTING) {
			next = min_u32(next, after(t, dl) ? 0u : dl - t);
		}
	}
	s->ops->timer(s->ctx, next);
}

static void arm(struct sched *s, uint32_t ms)
{
	s->armed = ms != SCHED_TIMER_OFF;
	s->t_deadline = now(s) + (s->armed ? ms : 0u);
	rearm(s);
}

/* ---- Per-tag back-off (BRIDGE_TAG_BACKOFF_MS after a failure) ---- */

static struct sched_backoff *backoff_entry(struct sched *s, uint32_t tag_id)
{
	size_t i;

	for (i = 0; i < ARRAY_LEN(s->backoff); i++) {
		if ((s->backoff[i].flags & SCHED_BO_USED) && s->backoff[i].tag_id == tag_id) {
			return &s->backoff[i];
		}
	}
	return NULL;
}

/*
 * quick: a CONNECT_FAILED that was not itself the retry: one more attempt is
 * allowed on an advertisement within SCHED_QUICK_RETRY_MS (the tag's current
 * window) instead of forfeiting it (5.2 step 2).
 */
static void backoff(struct sched *s, uint32_t tag_id, uint8_t status, bool quick)
{
	struct sched_backoff *b = backoff_entry(s, tag_id);
	size_t i;

	s->last_status = status;
	for (i = 0; b == NULL && i < ARRAY_LEN(s->backoff); i++) {
		if (!(s->backoff[i].flags & SCHED_BO_USED)) {
			b = &s->backoff[i]; /* a free entry */
		}
	}
	if (b == NULL) {
		b = &s->backoff[0]; /* else the one expiring first */
		for (i = 1; i < ARRAY_LEN(s->backoff); i++) {
			if (after(b->at, s->backoff[i].at)) {
				b = &s->backoff[i];
			}
		}
	}
	b->tag_id = tag_id;
	b->at = now(s);
	b->flags = (uint8_t)(SCHED_BO_USED | (quick ? SCHED_BO_QUICK : 0u));
}

enum { BO_NONE, BO_WAIT, BO_QUICK };

static int backoff_state(struct sched *s, uint32_t tag_id, uint32_t t)
{
	const struct sched_backoff *b = backoff_entry(s, tag_id);

	if (b == NULL || t - b->at >= CTAG_BRIDGE_TAG_BACKOFF_MS) {
		return BO_NONE;
	}
	if ((b->flags & SCHED_BO_QUICK) && t - b->at < SCHED_QUICK_RETRY_MS) {
		return BO_QUICK;
	}
	return BO_WAIT;
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

/* ---- Links ---- */

void sched_init(struct sched *s, const struct sched_ops *ops, void *ctx)
{
	memset(s, 0, sizeof(*s));
	s->ops = ops;
	s->ctx = ctx;
	s->last_status = CTAG_STATUS_OK;
	s->link = SCHED_NO_LINK;
}

bool sched_busy(const struct sched *s)
{
	size_t i;

	if (s->state != SCHED_IDLE) {
		return true;
	}
	for (i = 0; i < SCHED_LINKS; i++) {
		if (s->links[i].state != SCHED_LINK_FREE) {
			return true;
		}
	}
	return false;
}

uint8_t sched_link_of(const struct sched *s, uint32_t tag_id)
{
	size_t i;

	for (i = 0; i < SCHED_LINKS; i++) {
		if (s->links[i].state != SCHED_LINK_FREE && s->links[i].tag_id == tag_id) {
			return (uint8_t)i;
		}
	}
	return SCHED_NO_LINK;
}

static uint8_t free_link(const struct sched *s)
{
	size_t i;

	for (i = 0; i < SCHED_LINKS; i++) {
		if (s->links[i].state == SCHED_LINK_FREE) {
			return (uint8_t)i;
		}
	}
	return SCHED_NO_LINK;
}

/* No other link carries anything now: every session waits for its tag. */
static bool others_idle(const struct sched *s, uint8_t except)
{
	size_t i;

	for (i = 0; i < SCHED_LINKS; i++) {
		const struct sched_link *l = &s->links[i];

		if (i == except || l->state == SCHED_LINK_FREE ||
		    l->state == SCHED_LINK_DISCONNECTING) {
			continue;
		}
		if (l->state != SCHED_LINK_SESSION || !s->ops->link_idle(s->ctx, (uint8_t)i)) {
			return false;
		}
	}
	return true;
}

bool sched_deaf(const struct sched *s, uint32_t t, uint32_t heard_ms, uint32_t limit_ms)
{
	uint32_t idle_for = t - s->t_state;
	size_t i;

	/* Idle means scanning (the mesh runs) and no connection; silence counts
	 * from the later of the last report and the moment the scheduler and
	 * every link went idle. */
	if (limit_ms == 0u || sched_busy(s)) {
		return false;
	}
	for (i = 0; i < SCHED_LINKS; i++) {
		idle_for = min_u32(idle_for, t - s->links[i].t_state);
	}
	return t - heard_ms >= limit_ms && idle_for >= limit_ms;
}

static void to_idle(struct sched *s)
{
	set_state(s, SCHED_IDLE);
	arm(s, SCHED_TIMER_OFF);
}

/* Take the link down: wait for its disconnected event when it is connected. */
static void link_down(struct sched *s, uint8_t i)
{
	struct sched_link *l = &s->links[i];

	if (l->conn_up) {
		(void)s->ops->disconnect(s->ctx, i);
		set_link(s, i, SCHED_LINK_DISCONNECTING);
	} else {
		set_link(s, i, SCHED_LINK_FREE);
	}
	rearm(s);
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
	uint8_t i = s->link;
	struct sched_link *l = &s->links[i];
	int err = s->ops->mesh_resume(s->ctx);
	uint32_t t = now(s);

	s->last_suspend_ms = t - s->t_suspend;
	s->c.suspend_max_ms = max_u32(s->c.suspend_max_ms, s->last_suspend_ms);
	if (err == 0 || err == -EALREADY) {
		s->mesh_suspended = false;
		if (session && l->conn_up) {
			set_link(s, i, SCHED_LINK_SESSION);
			for (size_t k = 0; k < SCHED_LINKS; k++) {
				if (k != i && s->links[k].state == SCHED_LINK_SESSION) {
					s->c.concurrent_sessions++; /* beside a session in progress */
					break;
				}
			}
			to_idle(s); /* the next initiation may start */
			s->ops->session_start(s->ctx, i, l->tag_id, s->last_suspend_ms);
			return;
		}
		if (fail_status == CTAG_STATUS_CONNECT_FAILED) {
			s->c.connect_failed++;
		}
		backoff(s, l->tag_id, fail_status,
			fail_status == CTAG_STATUS_CONNECT_FAILED && !s->retry);
		link_down(s, i);
		to_idle(s);
		return;
	}
	/* Resume failure: no session on this link; controlled recovery. */
	s->c.resume_fail++;
	backoff(s, l->tag_id, CTAG_STATUS_MESH_RESUME_FAILED, false);
	link_down(s, i);
	enter_recovery(s, false);
}

static void attempt(struct sched *s)
{
	struct sched_link *l = &s->links[s->link];
	uint32_t t = now(s);
	int err;

	record_suspend(s, t);
	s->c.attempts++;
	s->c.suspend_count++;
	if (s->retry) {
		struct sched_backoff *b = backoff_entry(s, l->tag_id);

		if (b != NULL) {
			b->flags &= (uint8_t)~SCHED_BO_QUICK; /* one retry per failure */
		}
		s->c.quick_retries++;
	}
	err = s->ops->mesh_suspend(s->ctx);
	if (err == -EALREADY) {
		/* Someone left the mesh suspended: never keep it that way. */
		s->c.suspend_fail++;
		s->mesh_suspended = true;
		s->t_suspend = t;
		l->conn_up = false;
		resume_after_attempt(s, false, CTAG_STATUS_MESH_SUSPEND_FAILED);
		return;
	}
	if (err != 0) {
		/* 5.2 step 4: no connection is attempted. */
		s->c.suspend_fail++;
		backoff(s, l->tag_id, CTAG_STATUS_MESH_SUSPEND_FAILED, false);
		set_link(s, s->link, SCHED_LINK_FREE);
		if (err == -EINVAL || err == -EBUSY) {
			to_idle(s); /* refused before touching anything (not ready, provisioning) */
		} else {
			enter_recovery(s, true); /* failed part-way: the mesh state is unknown */
		}
		return;
	}
	s->mesh_suspended = true;
	s->t_suspend = t;
	l->conn_up = false;
	err = s->ops->conn_create(s->ctx, s->link, &s->peer, CTAG_BRIDGE_CONN_ATTEMPT_MS);
	if (err != 0) {
		resume_after_attempt(s, false, CTAG_STATUS_CONNECT_FAILED);
		return;
	}
	set_state(s, SCHED_CONNECTING);
	arm(s, CTAG_BRIDGE_CONN_ATTEMPT_MS + SCHED_ATTEMPT_GRACE_MS);
}

/* Preconditions of 5.2 steps 1-3 other than the links and the local sends.
 * *retry: allowed as the tag's one quick retry after CONNECT_FAILED. */
static bool may_attempt(struct sched *s, uint32_t tag_id, bool count, bool *retry)
{
	uint32_t t = now(s);
	int bo;

	if (!s->ops->has_work(s->ctx, tag_id)) {
		return false;
	}
	if (!s->ops->node_ready(s->ctx)) {
		s->c.not_ready += count ? 1u : 0u;
		return false;
	}
	bo = backoff_state(s, tag_id, t);
	if (bo == BO_WAIT) {
		s->c.backoff_skips += count ? 1u : 0u;
		return false;
	}
	if (!sched_rate_ok(s, t)) {
		s->c.rate_limited += count ? 1u : 0u;
		return false;
	}
	*retry = bo == BO_QUICK;
	return true;
}

void sched_advert(struct sched *s, uint32_t tag_id, const struct sched_peer *peer)
{
	uint8_t i;
	bool retry = false;

	/* One attempt at a time, one link per tag, a free link, and a further
	 * link only while every open one idles (its tag refreshing). */
	if (s->state != SCHED_IDLE || sched_link_of(s, tag_id) != SCHED_NO_LINK) {
		return;
	}
	i = free_link(s);
	if (i == SCHED_NO_LINK || !others_idle(s, i) || !may_attempt(s, tag_id, true, &retry)) {
		return;
	}
	s->link = i;
	s->retry = retry;
	s->peer = *peer;
	s->links[i].tag_id = tag_id;
	s->links[i].conn_up = false;
	set_link(s, i, SCHED_LINK_ATTEMPT);
	if (s->ops->mesh_busy(s->ctx)) {
		/* 5.2 step 3: let our own mesh sends finish, or defer. */
		set_state(s, SCHED_WAIT_SENDS);
		arm(s, SCHED_SEND_POLL_MS);
		return;
	}
	attempt(s);
}

void sched_connected(struct sched *s, uint8_t i, uint8_t err)
{
	struct sched_link *l;

	if (i >= SCHED_LINKS) {
		return;
	}
	l = &s->links[i];
	if ((s->state == SCHED_CONNECTING || s->state == SCHED_CANCELLING) && i == s->link) {
		bool session = s->state == SCHED_CONNECTING && err == 0u;

		l->conn_up = err == 0u;
		s->armed = false;
		resume_after_attempt(s, session, CTAG_STATUS_CONNECT_FAILED);
		return;
	}
	if (err == 0u && l->state != SCHED_LINK_SESSION) {
		/* A connection we no longer want (late completion): take it down. */
		l->conn_up = true;
		(void)s->ops->disconnect(s->ctx, i);
		if (l->state == SCHED_LINK_FREE) {
			set_link(s, i, SCHED_LINK_DISCONNECTING);
			rearm(s);
		}
	}
}

static void session_end(struct sched *s, uint8_t i, uint8_t status)
{
	if (status == CTAG_STATUS_OK) {
		s->c.sessions_ok++;
		s->last_status = CTAG_STATUS_OK;
	} else {
		s->c.sessions_fail++;
		backoff(s, s->links[i].tag_id, status, false);
	}
	link_down(s, i);
}

void sched_session_done(struct sched *s, uint8_t i, uint8_t status)
{
	if (i < SCHED_LINKS && s->links[i].state == SCHED_LINK_SESSION) {
		session_end(s, i, status);
	}
}

void sched_disconnected(struct sched *s, uint8_t i)
{
	struct sched_link *l;

	if (i >= SCHED_LINKS) {
		return;
	}
	l = &s->links[i];
	l->conn_up = false;
	switch (l->state) {
	case SCHED_LINK_SESSION:
		/* The session reports done (normally from inside this call). */
		s->ops->session_abort(s->ctx, i, CTAG_STATUS_DISCONNECTED);
		if (l->state == SCHED_LINK_SESSION) {
			session_end(s, i, CTAG_STATUS_DISCONNECTED);
		}
		break;
	case SCHED_LINK_DISCONNECTING:
		set_link(s, i, SCHED_LINK_FREE);
		rearm(s);
		break;
	default:
		break;
	}
}

static void initiator_timeout(struct sched *s, uint32_t t)
{
	bool retry = false;
	int err;

	switch (s->state) {
	case SCHED_WAIT_SENDS:
		if (!s->ops->mesh_busy(s->ctx)) {
			if (others_idle(s, s->link) &&
			    may_attempt(s, s->links[s->link].tag_id, false, &retry)) {
				s->retry = retry;
				attempt(s);
			} else {
				set_link(s, s->link, SCHED_LINK_FREE);
				to_idle(s);
			}
		} else if (t - s->t_state >= SCHED_SEND_WAIT_MS) {
			s->c.deferred++; /* retried on a later advertisement */
			set_link(s, s->link, SCHED_LINK_FREE);
			to_idle(s);
		} else {
			arm(s, SCHED_SEND_POLL_MS);
		}
		break;
	case SCHED_CONNECTING:
		/* The host's own create timeout did not report: cancel explicitly. */
		s->c.cancels++;
		(void)s->ops->conn_cancel(s->ctx, s->link);
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
			to_idle(s);
			break;
		}
		s->c.resume_fail++;
		if (t - s->t_state >= SCHED_RECOVERY_MS) {
			/* The mesh stack reloads from settings after the reset. */
			s->c.reboots++;
			s->ops->reboot(s->ctx);
		}
		s->retry_ms = min_u32(2u * s->retry_ms, SCHED_RESUME_RETRY_MAX);
		arm(s, s->retry_ms);
		break;
	default:
		break;
	}
}

void sched_timeout(struct sched *s)
{
	uint32_t t = now(s);
	size_t i;

	for (i = 0; i < SCHED_LINKS; i++) {
		struct sched_link *l = &s->links[i];

		if (l->state == SCHED_LINK_DISCONNECTING &&
		    t - l->t_state >= SCHED_DISCONNECT_WAIT_MS) {
			l->conn_up = false; /* no disconnected event: do not block forever */
			set_link(s, (uint8_t)i, SCHED_LINK_FREE);
		}
	}
	if (s->armed && after(t, s->t_deadline)) {
		s->armed = false;
		initiator_timeout(s, t);
	}
	rearm(s);
}
