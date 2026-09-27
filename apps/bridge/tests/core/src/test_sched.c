/*
 * The mesh suspend window of docs/protocol.md 5.2 with mocked
 * bt_mesh_suspend / bt_mesh_resume / bt_conn_le_create outcomes, for one link
 * and (CONFIG_CTAG_BRIDGE_SESSIONS = 2) two tag sessions at once.
 */
#include <errno.h>
#include <string.h>

#include "common.h"
#include "sched.h"

#define HCI_UNKNOWN_CONN_ID 0x02
#define HCI_CONN_FAIL       0x3E

struct mock {
	struct sched *s;
	uint32_t now;
	bool ready;
	bool busy;
	bool work;
	bool suspended; /* the mesh's real state */
	bool initiating;
	int suspend_ret;
	int suspend_seq[8]; /* consumed in order before suspend_ret */
	size_t suspend_n, suspend_i;
	/* The pinned bt_mesh_suspend()/resume() semantics: a suspend that fails
	 * part-way stops the scanner without flagging the mesh suspended, and a
	 * resume of a mesh not flagged suspended is -EALREADY and does nothing. */
	bool strict;
	bool scanning;
	int create_ret;
	int resume_ret[16]; /* consumed in order; 0 afterwards */
	size_t resume_i;
	uint32_t resume_ms; /* time a resume takes */
	uint32_t timer_at;
	uint32_t suspends, resumes, creates, cancels, disconnects, starts, aborts, reboots;
	uint32_t start_suspend_ms;
	uint32_t create_timeout;
	uint8_t link;               /* the link of the last conn_create */
	uint32_t link_tag[2];       /* the tag each link's session started with */
	bool idle[2];               /* sched_ops.link_idle: the session waits for its tag */
	uint32_t disconnects_of[2]; /* disconnect() per link */
	uint32_t aborts_of[2];
};

static struct mock m;
static struct sched s;

static bool op_ready(void *ctx)
{
	return m.ready;
}

static bool op_work(void *ctx, uint32_t tag)
{
	return m.work;
}

static bool op_busy(void *ctx)
{
	return m.busy;
}

static bool op_link_idle(void *ctx, uint8_t link)
{
	zassert_true(link < SCHED_LINKS);
	return m.idle[link];
}

static int op_suspend(void *ctx)
{
	int r = m.suspend_i < m.suspend_n ? m.suspend_seq[m.suspend_i++] : m.suspend_ret;

	m.suspends++;
	if (r == 0 || r == -EALREADY) {
		m.suspended = true;
		m.scanning = false;
	} else if (m.strict && r != -EBUSY && r != -EINVAL) {
		m.scanning = false; /* failed after stopping the scanner */
	}
	return r;
}

static int op_resume(void *ctx)
{
	int r = m.resume_i < ARRAY_SIZE(m.resume_ret) ? m.resume_ret[m.resume_i++] : 0;

	m.resumes++;
	m.now += m.resume_ms;
	if (m.strict && !m.suspended) {
		return -EALREADY;
	}
	/* Scanning cannot restart while the controller still initiates. */
	if (r == 0 && m.initiating) {
		r = -EPERM;
	}
	if (r == 0) {
		m.suspended = false;
		m.scanning = true;
	}
	return r;
}

static int op_create(void *ctx, uint8_t link, const struct sched_peer *peer, uint32_t timeout_ms)
{
	zassert_true(link < SCHED_LINKS);
	zassert_false(m.initiating, "two initiations at once");
	m.creates++;
	m.create_timeout = timeout_ms;
	m.link = link;
	zassert_true(m.suspended, "initiating while the mesh scans");
	m.initiating = m.create_ret == 0;
	return m.create_ret;
}

static int op_cancel(void *ctx, uint8_t link)
{
	zassert_equal(link, m.link, "cancelling another link");
	m.cancels++;
	return 0;
}

static int op_disconnect(void *ctx, uint8_t link)
{
	zassert_true(link < SCHED_LINKS);
	m.disconnects++;
	m.disconnects_of[link]++;
	return 0;
}

static void op_start(void *ctx, uint8_t link, uint32_t tag, uint32_t suspend_ms)
{
	zassert_false(m.suspended, "session before the mesh resumed");
	zassert_true(link < SCHED_LINKS);
	m.starts++;
	m.start_suspend_ms = suspend_ms;
	m.link_tag[link] = tag;
	m.idle[link] = false; /* GATT and the handshake first */
}

static void op_abort(void *ctx, uint8_t link, uint8_t status)
{
	m.aborts++;
	m.aborts_of[link]++;
	sched_session_done(m.s, link, status); /* as the session does when it ends */
}

static void op_timer(void *ctx, uint32_t ms)
{
	m.timer_at = ms == SCHED_TIMER_OFF ? UINT32_MAX : m.now + ms;
}

static uint32_t op_now(void *ctx)
{
	return m.now;
}

static void op_reboot(void *ctx)
{
	m.reboots++;
}

static const struct sched_ops ops = {
	.node_ready = op_ready,
	.has_work = op_work,
	.mesh_busy = op_busy,
	.link_idle = op_link_idle,
	.mesh_suspend = op_suspend,
	.mesh_resume = op_resume,
	.conn_create = op_create,
	.conn_cancel = op_cancel,
	.disconnect = op_disconnect,
	.session_start = op_start,
	.session_abort = op_abort,
	.timer = op_timer,
	.now = op_now,
	.reboot = op_reboot,
};

static const struct sched_peer peer = {.type = 1, .a = {1, 2, 3, 4, 5, 0xC0}};

static void reset(void *f)
{
	memset(&m, 0, sizeof(m));
	m.s = &s;
	m.now = 100000u;
	m.ready = true;
	m.work = true;
	m.timer_at = UINT32_MAX;
	sched_init(&s, &ops, NULL);
}

static void fire(void)
{
	zassert_not_equal(m.timer_at, UINT32_MAX, "no timer armed");
	m.now = m.timer_at;
	m.timer_at = UINT32_MAX;
	sched_timeout(&s);
}

/* The controller ends the attempt of the last conn_create (connected callback). */
static void connected(uint8_t err)
{
	m.initiating = false;
	sched_connected(&s, m.link, err);
}

static uint8_t link_state(uint8_t i)
{
	return s.links[i].state;
}

ZTEST(bridge_sched, test_connection_and_resume_before_auth)
{
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 1);
	zassert_equal(m.creates, 1);
	zassert_equal(m.create_timeout, CTAG_BRIDGE_CONN_ATTEMPT_MS);
	zassert_equal(s.state, SCHED_CONNECTING);
	zassert_equal(link_state(m.link), SCHED_LINK_ATTEMPT);
	zassert_true(s.mesh_suspended);
	m.now += 300;
	m.resume_ms = 4;
	connected(0);
	/* 5.2 step 6/8: resume first, and only then the session. */
	zassert_equal(m.resumes, 1);
	zassert_equal(m.starts, 1);
	zassert_false(m.suspended);
	zassert_equal(m.start_suspend_ms, 304, "suspend_ms runs to the resume's return");
	zassert_equal(s.c.suspend_max_ms, 304);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	zassert_equal(sched_link_of(&s, 7), 0);
	zassert_true(sched_busy(&s));
	/* While the session streams (its link is not idle) adverts start nothing,
	 * nor ever a second link to the same tag. */
	sched_advert(&s, 8, &peer);
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 1);
	sched_session_done(&s, 0, CTAG_STATUS_OK);
	zassert_equal(m.disconnects, 1);
	zassert_equal(link_state(0), SCHED_LINK_DISCONNECTING);
	sched_disconnected(&s, 0);
	zassert_equal(link_state(0), SCHED_LINK_FREE);
	zassert_false(sched_busy(&s));
	zassert_equal(s.c.sessions_ok, 1);
}

ZTEST(bridge_sched, test_suspend_rejected_no_attempt)
{
	m.suspend_ret = -EBUSY;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 1);
	zassert_equal(m.creates, 0, "no connection attempt after a failed suspend");
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_false(sched_busy(&s));
	zassert_equal(s.last_status, CTAG_STATUS_MESH_SUSPEND_FAILED);
	zassert_equal(s.c.suspend_fail, 1);
	/* Retried on a later advertisement, after the per-tag back-off (no quick
	 * retry: that is for a failed connection only). */
	m.suspend_ret = 0;
	m.now += 100;
	sched_advert(&s, 7, &peer);
	m.now += CTAG_BRIDGE_TAG_BACKOFF_MS - 101;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 1);
	zassert_equal(s.c.backoff_skips, 2);
	sched_advert(&s, 9, &peer); /* another tag is not held back */
	zassert_equal(m.suspends, 2);
}

/* -EINVAL (mesh not ready) and -EBUSY (provisioning) touch nothing. */
ZTEST(bridge_sched, test_suspend_refusals_leave_the_mesh_alone)
{
	m.strict = true;
	m.scanning = true;
	m.suspend_ret = -EINVAL;
	sched_advert(&s, 7, &peer);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_true(m.scanning);
	zassert_equal(s.c.recoveries, 0);
}

/* bt_mesh_suspend() failing part-way (review: the scanner stopped, the mesh
 * not flagged suspended): RECOVERY suspends fully, then resumes. */
ZTEST(bridge_sched, test_partial_suspend_failure_recovers)
{
	m.strict = true;
	m.scanning = true;
	m.suspend_seq[0] = -EIO;
	m.suspend_n = 1;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.creates, 0, "no connection attempt");
	zassert_false(m.scanning, "the failure stopped the scanner");
	zassert_equal(s.state, SCHED_RECOVERY);
	zassert_equal(s.last_status, CTAG_STATUS_MESH_SUSPEND_FAILED);
	/* A plain resume would be -EALREADY and change nothing. */
	fire();
	zassert_equal(m.suspends, 2);
	zassert_equal(m.resumes, 1);
	zassert_true(m.scanning, "scanning again");
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(m.reboots, 0);
}

ZTEST(bridge_sched, test_partial_suspend_failure_reboots)
{
	uint32_t t0 = m.now;

	m.strict = true;
	m.scanning = true;
	m.suspend_ret = -EIO; /* the controller keeps refusing */
	sched_advert(&s, 7, &peer);
	zassert_equal(s.state, SCHED_RECOVERY);
	while (m.reboots == 0u) {
		zassert_true(m.now - t0 < 2u * SCHED_RECOVERY_MS, "never rebooted");
		fire();
		zassert_equal(m.creates, 0);
	}
	zassert_true(m.now - t0 >= SCHED_RECOVERY_MS, "rebooted after %u ms", m.now - t0);
	zassert_false(m.scanning);
}

/* Liveness: idle (scanning) and no advertising report for the limit. */
ZTEST(bridge_sched, test_liveness)
{
	const uint32_t limit = 1800u * 1000u;
	uint32_t t0 = m.now;

	zassert_false(sched_deaf(&s, t0 + limit - 1u, t0, limit));
	zassert_true(sched_deaf(&s, t0 + limit, t0, limit));
	zassert_false(sched_deaf(&s, t0 + limit, t0 + 1000u, limit), "a report since");
	zassert_false(sched_deaf(&s, t0 + limit, t0, 0u), "disabled");
	/* Not idle (the mesh is suspended for an attempt): not a deaf scanner. */
	sched_advert(&s, 7, &peer);
	zassert_equal(s.state, SCHED_CONNECTING);
	zassert_false(sched_deaf(&s, t0 + 2u * limit, t0, limit));
	/* Back to idle: silence counts from then. */
	connected(HCI_UNKNOWN_CONN_ID);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_false(sched_deaf(&s, m.now + limit - 1u, t0, limit));
	zassert_true(sched_deaf(&s, m.now + limit, t0, limit));
	/* A session in progress is not idle either, and counts until it ends. */
	m.now += 20000u;
	sched_advert(&s, 8, &peer);
	connected(0);
	zassert_false(sched_deaf(&s, m.now + 2u * limit, t0, limit));
	m.now += 5000u;
	sched_session_done(&s, 0, CTAG_STATUS_OK);
	sched_disconnected(&s, 0);
	zassert_false(sched_deaf(&s, m.now + limit - 1u, t0, limit));
	zassert_true(sched_deaf(&s, m.now + limit, t0, limit));
}

ZTEST(bridge_sched, test_suspend_already_never_left_suspended)
{
	m.suspend_ret = -EALREADY;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.creates, 0);
	zassert_equal(m.resumes, 1);
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_false(sched_busy(&s));
}

ZTEST(bridge_sched, test_failed_attempts_resume)
{
	/* bt_conn_le_create() refused synchronously. */
	m.create_ret = -ENOMEM;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.resumes, 1);
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(s.c.connect_failed, 1);
	zassert_equal(s.last_status, CTAG_STATUS_CONNECT_FAILED);
	/* The attempt times out in the host (connected with an error). */
	m.create_ret = 0;
	sched_advert(&s, 8, &peer);
	m.now += CTAG_BRIDGE_CONN_ATTEMPT_MS;
	connected(HCI_UNKNOWN_CONN_ID);
	zassert_equal(m.resumes, 2);
	zassert_false(m.suspended);
	zassert_equal(m.starts, 0);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(s.c.connect_failed, 2);
	/* Connection failed to be established. */
	sched_advert(&s, 9, &peer);
	connected(HCI_CONN_FAIL);
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_false(sched_busy(&s));
}

ZTEST(bridge_sched, test_cancellation)
{
	sched_advert(&s, 7, &peer);
	/* No outcome from the host: our watchdog cancels the attempt. */
	zassert_equal(m.timer_at, m.now + CTAG_BRIDGE_CONN_ATTEMPT_MS + SCHED_ATTEMPT_GRACE_MS);
	fire();
	zassert_equal(m.cancels, 1);
	zassert_equal(s.state, SCHED_CANCELLING);
	zassert_true(m.suspended, "resume waits for the confirmed cancellation");
	connected(HCI_UNKNOWN_CONN_ID); /* the confirmation */
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);

	/* The confirmation never arrives: resume anyway; while the controller
	 * still initiates the resume is refused and recovery retries it. */
	m.now += 60000;
	sched_advert(&s, 8, &peer);
	fire();
	zassert_equal(s.state, SCHED_CANCELLING);
	fire();
	zassert_equal(s.state, SCHED_RECOVERY);
	zassert_true(m.suspended);
	m.initiating = false;
	fire();
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);

	/* The link completes just as it is cancelled: taken down, no session. */
	m.now += 60000;
	sched_advert(&s, 9, &peer);
	fire();
	connected(0);
	zassert_false(m.suspended);
	zassert_equal(m.starts, 0);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(link_state(m.link), SCHED_LINK_DISCONNECTING);
	sched_disconnected(&s, m.link);
	zassert_false(sched_busy(&s));
}

ZTEST(bridge_sched, test_resume_failure_recovery)
{
	/* Resume fails once after the link came up: disconnect, no session. */
	m.resume_ret[0] = -EIO;
	sched_advert(&s, 7, &peer);
	connected(0);
	zassert_equal(m.starts, 0, "no session without a resumed mesh");
	zassert_equal(m.disconnects, 1);
	zassert_equal(s.state, SCHED_RECOVERY);
	zassert_equal(s.c.resume_fail, 1);
	zassert_equal(s.last_status, CTAG_STATUS_MESH_RESUME_FAILED);
	zassert_true(m.suspended);
	sched_disconnected(&s, m.link); /* the link goes; recovery continues */
	zassert_equal(s.state, SCHED_RECOVERY);
	zassert_equal(link_state(m.link), SCHED_LINK_FREE);
	fire(); /* retry succeeds */
	zassert_false(m.suspended);
	zassert_equal(s.state, SCHED_IDLE);
	zassert_equal(m.reboots, 0);

	/* Resume keeps failing: retries with back-off, reboot after 5 s. */
	m.now += 60000;
	for (size_t i = 0; i < ARRAY_SIZE(m.resume_ret); i++) {
		m.resume_ret[i] = -EIO;
	}
	m.resume_i = 0;
	sched_advert(&s, 8, &peer);
	uint32_t start = m.now;

	connected(HCI_UNKNOWN_CONN_ID);
	zassert_equal(s.state, SCHED_RECOVERY);
	uint32_t prev = m.now;
	uint32_t gaps = 0;

	while (m.reboots == 0) {
		fire();
		zassert_true(m.now - prev >= SCHED_RESUME_RETRY_MS);
		zassert_true(m.now - prev <= SCHED_RESUME_RETRY_MAX);
		prev = m.now;
		gaps++;
		zassert_true(gaps < 50);
	}
	zassert_true(m.now - start >= SCHED_RECOVERY_MS);
	zassert_true(m.now - start < SCHED_RECOVERY_MS + SCHED_RESUME_RETRY_MAX + 1);
}

ZTEST(bridge_sched, test_disconnect_mid_transfer)
{
	sched_advert(&s, 7, &peer);
	connected(0);
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	sched_disconnected(&s, 0);
	zassert_equal(m.aborts, 1, "the running session is ended");
	zassert_equal(link_state(0), SCHED_LINK_FREE);
	zassert_equal(s.c.sessions_fail, 1);
	zassert_equal(s.last_status, CTAG_STATUS_DISCONNECTED);
	zassert_equal(m.disconnects, 0, "the link is already down");
	/* Back-off for this tag (no quick retry after a session). */
	m.now += 500;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 1);
	m.now += CTAG_BRIDGE_TAG_BACKOFF_MS;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 2);
}

ZTEST(bridge_sched, test_waits_for_own_mesh_sends)
{
	m.busy = true;
	sched_advert(&s, 7, &peer);
	zassert_equal(s.state, SCHED_WAIT_SENDS);
	zassert_equal(m.suspends, 0);
	fire();
	fire();
	m.busy = false;
	fire();
	zassert_equal(m.suspends, 1, "attempt once our sends finished");
	zassert_equal(s.state, SCHED_CONNECTING);
	connected(HCI_UNKNOWN_CONN_ID);
	/* Still busy after 500 ms: the attempt is deferred (no back-off). */
	m.now += 60000;
	m.busy = true;
	sched_advert(&s, 8, &peer);
	while (s.state == SCHED_WAIT_SENDS) {
		fire();
	}
	zassert_equal(s.state, SCHED_IDLE);
	zassert_false(sched_busy(&s), "the link is free again");
	zassert_equal(s.c.deferred, 1);
	zassert_equal(m.suspends, 1);
	m.busy = false;
	sched_advert(&s, 8, &peer);
	zassert_equal(m.suspends, 2);
}

ZTEST(bridge_sched, test_not_while_configuring)
{
	m.ready = false;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 0);
	zassert_equal(s.c.not_ready, 1);
	m.ready = true;
	m.work = false;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 0, "no attempt without pending work");
}

/*
 * 5.2: after CONNECT_FAILED the tag gets ONE retry on an advertisement within
 * its advertising window (SCHED_QUICK_RETRY_MS) instead of the whole back-off;
 * the retry is an attempt like any other (a suspension, the rate limit).
 */
ZTEST(bridge_sched, test_quick_retry_after_a_failed_connection)
{
	sched_advert(&s, 7, &peer);
	m.now += 200;
	connected(HCI_CONN_FAIL);
	zassert_equal(s.c.connect_failed, 1);
	/* The tag's next advertisement, 250 ms later, in the same window. */
	m.now += 250;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 2, "the quick retry");
	zassert_equal(s.c.quick_retries, 1);
	m.now += 200;
	connected(HCI_CONN_FAIL);
	/* The retry failed too: the back-off holds, no second retry. */
	m.now += 250;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 2);
	zassert_equal(s.c.backoff_skips, 1);
	m.now += CTAG_BRIDGE_TAG_BACKOFF_MS;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 3, "after the back-off");
	/* A retry that connects serves the tag. */
	m.now += 300;
	connected(HCI_CONN_FAIL);
	m.now += 250;
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 4);
	connected(0);
	zassert_equal(m.starts, 1);
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	sched_session_done(&s, 0, CTAG_STATUS_OK);
	sched_disconnected(&s, 0);

	/* Once the window is over (SCHED_QUICK_RETRY_MS) the back-off holds. */
	m.now += 60000;
	sched_advert(&s, 8, &peer);
	connected(HCI_UNKNOWN_CONN_ID);
	m.now += SCHED_QUICK_RETRY_MS;
	sched_advert(&s, 8, &peer);
	zassert_equal(m.suspends, 5, "no retry after the window");
	zassert_equal(s.c.quick_retries, 2);

	/* The retry counts toward the rolling-minute limit: with the minute's
	 * suspensions used up it is refused (and kept for a later advert). */
	m.now += 60000;
	for (uint32_t i = 0; i < CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN - 1u; i++) {
		sched_advert(&s, 100u + i, &peer);
		connected(HCI_UNKNOWN_CONN_ID);
		m.now += 10;
	}
	sched_advert(&s, 9, &peer);
	connected(HCI_CONN_FAIL);
	m.now += 250;
	zassert_false(sched_rate_ok(&s, m.now));
	sched_advert(&s, 9, &peer);
	zassert_equal(s.c.rate_limited, 1);
	zassert_equal(s.c.quick_retries, 2);
	zassert_equal(s.state, SCHED_IDLE);
}

/*
 * Repeated failures over many tags: the rolling minute never sees more than
 * BRIDGE_MAX_SUSPENDS_PER_MIN suspensions and the mesh stays up most of the
 * time (not starved) — with quick retries, and with a second link open.
 */
static void rate_limit_run(bool session_open)
{
	uint32_t times[400];
	size_t n = 0;
	uint32_t t0 = m.now, attempt_at = 0, suspended_ms = 0, last = m.now;
	uint32_t tag = 0;

	if (session_open) {
		/* A long session on one link, its tag refreshing (the link idles). */
		sched_advert(&s, 999, &peer);
		connected(0);
		m.idle[0] = true;
		m.now += 1000;
	}
	while (m.now - t0 < 5u * 60u * 1000u) {
		if (m.suspended) {
			suspended_ms += m.now - last;
		}
		last = m.now;
		if (s.state == SCHED_CONNECTING && m.now - attempt_at >= CTAG_BRIDGE_CONN_ATTEMPT_MS) {
			connected(HCI_UNKNOWN_CONN_ID); /* every tag fails to connect */
		}
		if (m.timer_at <= m.now) {
			fire();
		}
		/* 20 tags waking every 30 s, each advertising every 250 ms for 2 s. */
		for (tag = 0; m.now % 250u == 0u && tag < 20u; tag++) {
			uint32_t before = m.suspends;

			if ((m.now - t0 + 30000u - tag * 1500u) % 30000u >= CTAG_TAG_ADV_WINDOW_MS) {
				continue;
			}
			sched_advert(&s, 1000 + tag, &peer);
			if (m.suspends != before) {
				zassert_true(n < ARRAY_SIZE(times));
				times[n++] = m.now;
				attempt_at = m.now;
			}
		}
		m.now += 10;
	}
	zassert_true(n > 6);
	for (size_t i = CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN; i < n; i++) {
		zassert_true(times[i] - times[i - CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN] >= 60000u,
			     "%d suspends within a minute", CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN + 1);
	}
	zassert_true(s.c.rate_limited > 0);
	zassert_true(s.c.quick_retries > 0, "failed connections were retried in their window");
	/* At most 6 attempts of ~1 s per minute: the mesh runs >= 88 % of the time. */
	zassert_true(suspended_ms * 100u <= (m.now - t0) * 12u, "mesh suspended %u of %u ms",
		     suspended_ms, m.now - t0);
	if (s.state == SCHED_CONNECTING) {
		connected(HCI_UNKNOWN_CONN_ID);
	}
	zassert_false(m.suspended);
	if (session_open) {
		zassert_equal(link_state(0), SCHED_LINK_SESSION, "the open session is untouched");
		zassert_equal(m.disconnects_of[0], 0u);
	}
}

ZTEST(bridge_sched, test_rate_limit_no_starvation)
{
	rate_limit_run(false);
}

#if SCHED_LINKS > 1

/*
 * Two sessions (nRF52840): initiations are serialised, each in its own mesh
 * suspend window; a second tag is initiated only while the first one's link
 * idles (its tag refreshing); both sessions then run side by side.
 */
ZTEST(bridge_sched, test_two_sessions_initiation_serialised)
{
	uint32_t first;

	sched_advert(&s, 7, &peer);
	zassert_equal(m.link, 0);
	/* One initiation at a time: the mesh is suspended, nothing else starts. */
	sched_advert(&s, 8, &peer);
	zassert_equal(m.suspends, 1);
	m.now += 120;
	connected(0);
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	zassert_false(m.suspended, "resumed after the first initiation");
	first = m.start_suspend_ms;
	/* The first session streams (GATT, handshake, frame): no second attempt. */
	m.now += 300;
	sched_advert(&s, 8, &peer);
	zassert_equal(m.suspends, 1, "not while the first link carries a frame");
	/* Its tag refreshes: the link idles, the second tag is initiated on the
	 * other link, in a mesh suspend window of its own. */
	m.idle[0] = true;
	sched_advert(&s, 8, &peer);
	zassert_equal(m.suspends, 2);
	zassert_equal(m.creates, 2);
	zassert_equal(m.link, 1, "the free link");
	zassert_true(m.suspended);
	zassert_equal(link_state(0), SCHED_LINK_SESSION, "the first session is untouched");
	/* Nothing else while it initiates: not the first tag again, not a third. */
	sched_advert(&s, 9, &peer);
	sched_advert(&s, 7, &peer);
	zassert_equal(m.suspends, 2);
	m.now += 80;
	m.resume_ms = 3;
	connected(0);
	zassert_false(m.suspended);
	zassert_equal(m.starts, 2);
	zassert_equal(m.link_tag[0], 7);
	zassert_equal(m.link_tag[1], 8);
	zassert_equal(m.start_suspend_ms, 83, "the second window's own suspend_ms");
	zassert_equal(first, 120);
	zassert_equal(link_state(1), SCHED_LINK_SESSION);
	zassert_equal(s.c.concurrent_sessions, 1);
	zassert_equal(s.c.suspend_count, 2);
	/* Both links taken: a third tag waits, even with both idle. */
	m.idle[1] = true;
	sched_advert(&s, 9, &peer);
	zassert_equal(m.suspends, 2);
	/* The first ends; its link is free again once it is down. */
	sched_session_done(&s, 0, CTAG_STATUS_OK);
	zassert_equal(m.disconnects_of[0], 1u);
	zassert_equal(m.disconnects_of[1], 0u);
	sched_advert(&s, 9, &peer);
	zassert_equal(m.suspends, 2, "the link is still disconnecting");
	sched_disconnected(&s, 0);
	sched_advert(&s, 9, &peer);
	zassert_equal(m.suspends, 3);
	zassert_equal(m.link, 0);
	connected(0);
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	zassert_equal(link_state(1), SCHED_LINK_SESSION);
	zassert_equal(s.c.sessions_ok, 1);
}

/* A failed second initiation (connect failure, resume failure, the link
 * dropping) never touches the session on the other link. */
ZTEST(bridge_sched, test_failing_second_link_leaves_the_first_alone)
{
	sched_advert(&s, 7, &peer);
	connected(0);
	m.idle[0] = true;
	/* A failed connection on link 1. */
	sched_advert(&s, 8, &peer);
	zassert_equal(m.link, 1);
	connected(HCI_CONN_FAIL);
	zassert_equal(link_state(1), SCHED_LINK_FREE);
	/* A resume failure after link 1 came up: recovery; link 1 is dropped. */
	m.now += CTAG_BRIDGE_TAG_BACKOFF_MS;
	m.resume_ret[m.resume_i] = -EIO;
	sched_advert(&s, 9, &peer);
	connected(0);
	zassert_equal(s.state, SCHED_RECOVERY);
	zassert_equal(m.disconnects_of[1], 1u);
	zassert_equal(m.disconnects_of[0], 0u);
	sched_disconnected(&s, 1);
	fire();
	zassert_equal(s.state, SCHED_IDLE);
	/* A session on link 1 that drops. */
	m.now += CTAG_BRIDGE_TAG_BACKOFF_MS;
	sched_advert(&s, 10, &peer);
	connected(0);
	zassert_equal(link_state(1), SCHED_LINK_SESSION);
	sched_disconnected(&s, 1);
	zassert_equal(m.aborts_of[1], 1u);
	zassert_equal(m.aborts_of[0], 0u);
	/* Link 0 went through all of it untouched. */
	zassert_equal(link_state(0), SCHED_LINK_SESSION);
	zassert_equal(m.disconnects_of[0], 0u);
	sched_session_done(&s, 0, CTAG_STATUS_OK);
	zassert_equal(s.c.sessions_ok, 1);
	zassert_equal(s.c.sessions_fail, 1);
}

/*
 * A tag whose sessions keep failing (it drops the link mid-transfer) never
 * starves another tag: its back-off frees the bridge, the other tag is
 * served on every window.
 */
ZTEST(bridge_sched, test_failing_session_never_starves_the_other)
{
	uint32_t served_b = 0, tries_a = 0;

	for (int round = 0; round < 10; round++) {
		uint8_t la;

		/* Tag A (7) connects and drops mid-transfer. */
		sched_advert(&s, 7, &peer);
		if (s.state == SCHED_CONNECTING) {
			tries_a++;
			la = m.link;
			connected(0);
			m.now += 400;
			sched_disconnected(&s, la);
		}
		/* Tag B (8) in the same wake: served. */
		m.now += 250;
		sched_advert(&s, 8, &peer);
		zassert_equal(s.state, SCHED_CONNECTING, "round %d: B not attempted", round);
		connected(0);
		m.idle[m.link] = true;
		m.now += 5000;
		sched_session_done(&s, m.link, CTAG_STATUS_OK);
		sched_disconnected(&s, m.link);
		served_b++;
		m.now += 25000; /* the next wake */
	}
	zassert_equal(served_b, 10);
	zassert_true(tries_a >= 5, "A retried after each back-off");
	zassert_equal(s.c.sessions_fail, tries_a);
}

/* The rolling-minute limit holds with one link open: 20 failing tags for
 * five minutes beside a session. */
ZTEST(bridge_sched, test_two_links_rate_limit)
{
	rate_limit_run(true);
}

/* A link whose disconnected event never comes is freed after 10 s: the other
 * link's session and the initiator's timers go on meanwhile. */
ZTEST(bridge_sched, test_lost_disconnect_per_link)
{
	uint32_t t0;

	sched_advert(&s, 7, &peer);
	connected(0);
	m.idle[0] = true;
	sched_advert(&s, 8, &peer);
	connected(0);
	t0 = m.now;
	sched_session_done(&s, 0, CTAG_STATUS_OK); /* link 0 disconnecting, never confirmed */
	zassert_equal(m.timer_at, t0 + SCHED_DISCONNECT_WAIT_MS);
	m.now += 3000;
	m.idle[1] = true;
	sched_session_done(&s, 1, CTAG_STATUS_OK);
	sched_disconnected(&s, 1);
	sched_advert(&s, 9, &peer); /* link 1 is free: an attempt on it */
	zassert_equal(m.link, 1);
	zassert_equal(m.timer_at, m.now + CTAG_BRIDGE_CONN_ATTEMPT_MS + SCHED_ATTEMPT_GRACE_MS,
		      "the attempt's watchdog comes first");
	connected(0);
	zassert_equal(m.timer_at, t0 + SCHED_DISCONNECT_WAIT_MS, "then link 0's wait");
	fire();
	zassert_equal(link_state(0), SCHED_LINK_FREE);
	zassert_equal(link_state(1), SCHED_LINK_SESSION);
	zassert_equal(m.timer_at, UINT32_MAX);
}

#endif /* SCHED_LINKS > 1 */

ZTEST_SUITE(bridge_sched, NULL, NULL, reset, NULL, NULL);
