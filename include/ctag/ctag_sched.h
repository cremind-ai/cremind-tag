/*
 * Tag connection scheduler (lib/sched, CONFIG_CTAG_SCHED): the mesh suspend
 * window of docs/protocol.md 5.2, for up to SCHED_LINKS tag connections at
 * once (CONFIG_CTAG_SCHED_LINKS). Shared by the bridge (its tag sessions,
 * CONFIG_CTAG_BRIDGE_SESSIONS: 2 on the nRF52840, 1 on the nRF52832) and the
 * gateway's own radio (docs/protocol.md 11, CONFIG_CTAG_GW_TAG_LINKS).
 *
 * The initiator (one connection attempt at a time, each in its own mesh
 * suspend window):
 *
 *   IDLE --advert(T), work pending, ready, a free link, every open link idle,
 *          no back-off (or T's one quick retry), rate limit ok-->
 *        [WAIT_SENDS: own mesh sends in flight, poll up to 500 ms, else defer]
 *        bt_mesh_suspend() --refused (-EINVAL, -EBUSY)--> IDLE (MESH_SUSPEND_FAILED, back-off)
 *                          --failed part-way--> RECOVERY (suspend + resume, reboot after 5 s)
 *        bt_conn_le_create(link) --sync fail--> resume
 *   CONNECTING --connected(link, err)--> resume (the link's session only when err == 0)
 *              --watchdog--> cancel --> CANCELLING --connected(err) or watchdog--> resume
 *   resume ok  --> IDLE; the link: SESSION | FREE (CONNECT_FAILED: back-off with one
 *                  quick retry inside the tag's advertising window)
 *   resume err --> the link disconnected, RECOVERY: retry resume with back-off, reboot after 5 s
 *
 * Each link (tag connection):
 *
 *   FREE --attempt--> ATTEMPT --connected, resumed--> SESSION
 *   SESSION --session done--> disconnect --> DISCONNECTING --disconnected (or 10 s)--> FREE
 *
 * A further link is initiated only while every open session's link is idle
 * (its tag refreshing after FRAME_END or CMD: sched_ops.link_idle), so a
 * connection attempt never competes with a frame being streamed.
 *
 * Pure logic: every Bluetooth call goes through sched_ops, so the state
 * machine runs unchanged under test with mocked outcomes. One thread (the
 * bridge work queue, the gateway loop) calls every sched_* function.
 */
#ifndef CTAG_SCHED_H_
#define CTAG_SCHED_H_

#include <stdbool.h>
#include <stdint.h>

#include <ctag/proto_ids.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Links (tag connections at once) and per-tag back-off entries: Kconfig under
 * Zephyr; outside it (tests/host) the definer's values or these defaults. */
#if defined(CONFIG_CTAG_SCHED_LINKS)
#define SCHED_LINKS CONFIG_CTAG_SCHED_LINKS
#elif !defined(SCHED_LINKS)
#define SCHED_LINKS 1
#endif
#if defined(CONFIG_CTAG_SCHED_TAGS)
#define SCHED_TAGS CONFIG_CTAG_SCHED_TAGS
#elif !defined(SCHED_TAGS)
#define SCHED_TAGS 20
#endif
#if SCHED_LINKS < 1 || SCHED_LINKS > 4
#error "SCHED_LINKS must be 1..4"
#endif
#if SCHED_TAGS < 1
#error "SCHED_TAGS must be at least 1"
#endif

#define SCHED_SEND_POLL_MS      20u
#define SCHED_SEND_WAIT_MS      500u
#define SCHED_ATTEMPT_GRACE_MS  500u  /* host timeout + this = our watchdog */
#define SCHED_CANCEL_WAIT_MS    2000u /* confirmation of a cancelled attempt */
#define SCHED_RESUME_RETRY_MS   100u
#define SCHED_RESUME_RETRY_MAX  1000u
#define SCHED_RECOVERY_MS       5000u /* 5.2 step 7: reboot after 5 s */
#define SCHED_DISCONNECT_WAIT_MS 10000u
#define SCHED_WINDOW_MS         60000u
/* 5.2: after CONNECT_FAILED, one retry on an advertisement seen this soon
 * (the rest of the tag's advertising window). */
#define SCHED_QUICK_RETRY_MS    CTAG_TAG_ADV_WINDOW_MS
#define SCHED_TIMER_OFF         UINT32_MAX
#define SCHED_NO_LINK           0xFFu

/* The initiator. */
enum sched_state {
	SCHED_IDLE = 0,
	SCHED_WAIT_SENDS,
	SCHED_CONNECTING,
	SCHED_CANCELLING,
	SCHED_RECOVERY,
};

enum sched_link_state {
	SCHED_LINK_FREE = 0,
	SCHED_LINK_ATTEMPT,       /* the initiator is connecting it */
	SCHED_LINK_SESSION,       /* connected, the mesh resumed: GATT, handshake, transfer */
	SCHED_LINK_DISCONNECTING, /* taken down, waiting for its disconnected event */
};

struct sched_peer {
	uint8_t type;
	uint8_t a[6];
};

struct sched_ops {
	bool (*node_ready)(void *ctx);   /* provisioned, configured, not being configured */
	bool (*has_work)(void *ctx, uint32_t tag_id);
	bool (*mesh_busy)(void *ctx);    /* own mesh sends not finished */
	/* The session on this link waits for its tag (the refresh after FRAME_END
	 * or CMD): the link carries nothing, another tag may be initiated. */
	bool (*link_idle)(void *ctx, uint8_t link);
	int (*mesh_suspend)(void *ctx);
	int (*mesh_resume)(void *ctx);
	int (*conn_create)(void *ctx, uint8_t link, const struct sched_peer *peer,
			   uint32_t timeout_ms);
	int (*conn_cancel)(void *ctx, uint8_t link);
	int (*disconnect)(void *ctx, uint8_t link);
	/* The link is up and the mesh resumed: GATT, handshake, transfer. */
	void (*session_start)(void *ctx, uint8_t link, uint32_t tag_id, uint32_t suspend_ms);
	/* The link dropped under a running session: end it (it reports done). */
	void (*session_abort)(void *ctx, uint8_t link, uint8_t status);
	void (*timer)(void *ctx, uint32_t delay_ms); /* one-shot; SCHED_TIMER_OFF cancels */
	uint32_t (*now)(void *ctx);
	void (*reboot)(void *ctx);
};

struct sched_counters {
	uint32_t attempts;
	uint32_t suspend_count;
	uint32_t suspend_fail;
	uint32_t suspend_max_ms;
	uint32_t resume_fail;
	uint32_t connect_failed;
	uint32_t cancels;
	uint32_t deferred;
	uint32_t rate_limited;
	uint32_t backoff_skips;
	uint32_t not_ready;
	uint32_t sessions_ok;
	uint32_t sessions_fail;
	uint32_t recoveries;
	uint32_t reboots;
	uint32_t quick_retries;       /* attempts that were the one retry after CONNECT_FAILED */
	uint32_t concurrent_sessions; /* sessions started while another one ran */
};

#define SCHED_BO_USED  0x01u
#define SCHED_BO_QUICK 0x02u /* the one quick retry is still available */

struct sched_backoff {
	uint32_t tag_id;
	uint32_t at; /* the failure: back-off until at + BRIDGE_TAG_BACKOFF_MS */
	uint8_t flags;
};

struct sched_link {
	uint32_t tag_id;
	uint32_t t_state; /* when it entered its state */
	uint8_t state;
	bool conn_up; /* a connection exists (until its disconnected event) */
};

struct sched {
	const struct sched_ops *ops;
	void *ctx;
	uint8_t state;       /* the initiator */
	uint8_t link;        /* the link of the attempt in progress */
	uint8_t last_status;
	bool mesh_suspended; /* between a successful suspend and resume */
	bool mesh_unknown;   /* a suspend failed part-way: suspend again, then resume */
	bool retry;          /* the attempt in progress is a tag's quick retry */
	bool armed;          /* the initiator's deadline is set */
	struct sched_peer peer;
	uint32_t t_state;
	uint32_t t_deadline; /* the initiator's timer */
	uint32_t t_suspend;
	uint32_t retry_ms;
	uint32_t last_suspend_ms;
	uint32_t suspends[CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN];
	uint8_t n_suspends;
	struct sched_backoff backoff[SCHED_TAGS];
	struct sched_link links[SCHED_LINKS];
	struct sched_counters c;
};

void sched_init(struct sched *s, const struct sched_ops *ops, void *ctx);

/* A tag's connectable advertisement (5.1) was seen. */
void sched_advert(struct sched *s, uint32_t tag_id, const struct sched_peer *peer);
/* bt_conn_cb.connected for the link's attempt (err = HCI status, 0 = connected). */
void sched_connected(struct sched *s, uint8_t link, uint8_t err);
void sched_disconnected(struct sched *s, uint8_t link);
/* The one-shot timer fired. */
void sched_timeout(struct sched *s);
/* The link's session ended with status (OK, or why it failed); the link is
 * taken down. */
void sched_session_done(struct sched *s, uint8_t link, uint8_t status);

bool sched_busy(const struct sched *s); /* an attempt or session is in progress */
/* The link holding tag_id (attempt, session or disconnecting), else SCHED_NO_LINK. */
uint8_t sched_link_of(const struct sched *s, uint32_t tag_id);
/*
 * Liveness: true when the bridge has been idle (the mesh scanning, no attempt
 * or session) and heard no advertising report at all (mesh traffic, beacons,
 * tags) for limit_ms, since heard_ms and since it went idle. A scanner that
 * silently stopped leaves the node deaf: the caller reboots (0 disables the
 * check).
 */
bool sched_deaf(const struct sched *s, uint32_t now, uint32_t heard_ms, uint32_t limit_ms);
/* Attempts allowed right now by the rolling-minute limit. */
bool sched_rate_ok(const struct sched *s, uint32_t now);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_SCHED_H_ */
