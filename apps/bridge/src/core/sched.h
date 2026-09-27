/*
 * Tag connection scheduler: the mesh suspend window of docs/protocol.md 5.2.
 *
 *   IDLE --advert(T), work pending, ready, no back-off, rate limit ok-->
 *        [WAIT_SENDS: own mesh sends in flight, poll up to 500 ms, else defer]
 *        bt_mesh_suspend() --fail--> IDLE (MESH_SUSPEND_FAILED, back-off)
 *        bt_conn_le_create() --sync fail--> resume
 *   CONNECTING --connected(err)--> resume (session only when err == 0)
 *              --watchdog--> cancel --> CANCELLING --connected(err) or watchdog--> resume
 *   resume ok  --> SESSION (connected) | IDLE (CONNECT_FAILED, back-off)
 *   resume err --> disconnect, RECOVERY: retry resume with back-off, reboot after 5 s
 *   SESSION --session done--> disconnect --> DISCONNECTING --disconnected--> IDLE
 *
 * Pure logic: every Bluetooth call goes through sched_ops, so the state
 * machine runs unchanged under test with mocked outcomes. One thread (the
 * bridge work queue) calls every sched_* function.
 */
#ifndef BRIDGE_SCHED_H_
#define BRIDGE_SCHED_H_

#include <stdbool.h>
#include <stdint.h>

#include <ctag/proto_ids.h>

#define SCHED_SEND_POLL_MS      20u
#define SCHED_SEND_WAIT_MS      500u
#define SCHED_ATTEMPT_GRACE_MS  500u  /* host timeout + this = our watchdog */
#define SCHED_CANCEL_WAIT_MS    2000u /* confirmation of a cancelled attempt */
#define SCHED_RESUME_RETRY_MS   100u
#define SCHED_RESUME_RETRY_MAX  1000u
#define SCHED_RECOVERY_MS       5000u /* 5.2 step 7: reboot after 5 s */
#define SCHED_DISCONNECT_WAIT_MS 10000u
#define SCHED_WINDOW_MS         60000u
#define SCHED_TIMER_OFF         UINT32_MAX

enum sched_state {
	SCHED_IDLE = 0,
	SCHED_WAIT_SENDS,
	SCHED_CONNECTING,
	SCHED_CANCELLING,
	SCHED_SESSION,
	SCHED_DISCONNECTING,
	SCHED_RECOVERY,
};

struct sched_peer {
	uint8_t type;
	uint8_t a[6];
};

struct sched_ops {
	bool (*node_ready)(void *ctx);   /* provisioned, configured, not being configured */
	bool (*has_work)(void *ctx, uint32_t tag_id);
	bool (*mesh_busy)(void *ctx);    /* own mesh sends not finished */
	int (*mesh_suspend)(void *ctx);
	int (*mesh_resume)(void *ctx);
	int (*conn_create)(void *ctx, const struct sched_peer *peer, uint32_t timeout_ms);
	int (*conn_cancel)(void *ctx);
	int (*disconnect)(void *ctx);
	/* The connection is up and the mesh resumed: GATT, handshake, transfer. */
	void (*session_start)(void *ctx, uint32_t tag_id, uint32_t suspend_ms);
	/* The link dropped under a running session: end it (it reports done). */
	void (*session_abort)(void *ctx, uint8_t status);
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
};

struct sched_backoff {
	uint32_t tag_id;
	uint32_t until;
	bool used;
};

struct sched {
	const struct sched_ops *ops;
	void *ctx;
	uint8_t state;
	uint8_t last_status;
	bool mesh_suspended; /* between a successful suspend and resume */
	bool conn_up;        /* a connection exists (until its disconnected event) */
	uint32_t tag_id;
	struct sched_peer peer;
	uint32_t t_state;
	uint32_t t_suspend;
	uint32_t retry_ms;
	uint32_t last_suspend_ms;
	uint32_t suspends[CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN];
	uint8_t n_suspends;
	struct sched_backoff backoff[CTAG_MAX_TAGS_PER_BRIDGE];
	struct sched_counters c;
};

void sched_init(struct sched *s, const struct sched_ops *ops, void *ctx);

/* A tag's connectable advertisement (5.1) was seen. */
void sched_advert(struct sched *s, uint32_t tag_id, const struct sched_peer *peer);
/* bt_conn_cb.connected for our attempt (err = HCI status, 0 = connected). */
void sched_connected(struct sched *s, uint8_t err);
void sched_disconnected(struct sched *s);
/* The one-shot timer fired. */
void sched_timeout(struct sched *s);
/* The session ended with status (OK, or why it failed); the link is taken down. */
void sched_session_done(struct sched *s, uint8_t status);

bool sched_busy(const struct sched *s); /* an attempt or session is in progress */
/* Attempts allowed right now by the rolling-minute limit. */
bool sched_rate_ok(const struct sched *s, uint32_t now);

#endif /* BRIDGE_SCHED_H_ */
