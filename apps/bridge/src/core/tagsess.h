/*
 * Bridge side of one tag connection (docs/protocol.md 5.3-5.6, 10; companion
 * sim/bridge.py _BridgeSession), after the mesh has resumed and the GATT
 * handles are known:
 *
 *   CAPS read -> HELLO -> CHALLENGE -> AUTH -> AUTH_OK -> first CREDIT
 *   per job, in arrival order:
 *     CMD:    wait credit -> CMD -> RESULT
 *     layout: load it into the shared buffer (dlv_job_layout) ->
 *             render pre-pass (frame digest) -> wait credit -> FRAME_BEGIN ->
 *             wait its credit (or an immediate RESULT) -> PLANE_DATA strip by
 *             strip under the tag's credits, <= 4 records per connection
 *             event -> FRAME_END -> RESULT
 *
 * Event driven and Bluetooth-free: GATT operations go through tsess_io and
 * their completions come back as tsess_* calls on the same thread.
 */
#ifndef BRIDGE_TAGSESS_H_
#define BRIDGE_TAGSESS_H_

#include <stdbool.h>
#include <stdint.h>

#include <ctag/ctag_frag.h>
#include <ctag/ctag_render.h>
#include <ctag/ctag_session.h>

#include "delivery.h"
#include "fontstore.h"

/*
 * Deadlines (docs/protocol.md 10 "Session deadlines"). The step timer moves
 * only on progress: a complete handshake message, an authenticated record,
 * or a CREDIT with n > 0 while the session waits for credit. Absolute bounds
 * on top: CAPS to AUTH_OK within TSESS_HANDSHAKE_MS of the connection, and
 * each frame (FRAME_BEGIN to RESULT) within 60 s + 1 s per KiB of plane data
 * + the refresh bound. Expiry ends the session with TIMEOUT.
 */
#define TSESS_STEP_TIMEOUT_MS   5000u
#define TSESS_RESULT_TIMEOUT_MS 60000u /* longest panel refresh bound */
#define TSESS_HANDSHAKE_MS      5000u
#define TSESS_FRAME_BASE_MS     60000u
#define TSESS_FRAME_PER_KIB_MS  1000u
#define TSESS_REFRESH_BOUND_MS  TSESS_RESULT_TIMEOUT_MS
#define TSESS_RECORDS_PER_EVENT 4u
#define TSESS_TIMER_OFF         UINT32_MAX
#define TSESS_STRIP_BUF         CONFIG_CTAG_BRIDGE_STRIP_BUF

enum tsess_timer {
	TSESS_T_STEP = 0,
	TSESS_T_PACE = 1,
};

enum tsess_state {
	TS_IDLE = 0,
	TS_CAPS,
	TS_HELLO,
	TS_AUTH,
	TS_GRANT,
	TS_JOB_CREDIT,
	TS_FB_GRANT,
	TS_STREAM,
	TS_END_CREDIT,
	TS_RESULT,
	TS_DONE,
};

struct tsess_io {
	int (*read_caps)(void *ctx);
	/* Write request on CTRL; completion: tsess_ctrl_written(). */
	int (*write_ctrl)(void *ctx, const uint8_t *val, uint16_t len);
	/* Write without response on DATA; completion: tsess_data_sent().
	 * -ENOMEM / -EAGAIN: no buffer now, retried after the next completion. */
	int (*write_data)(void *ctx, const uint8_t *val, uint16_t len);
	void (*timer)(void *ctx, uint8_t which, uint32_t delay_ms);
	/* The session is over: take the link down and tell the scheduler. */
	void (*done)(void *ctx, uint8_t status);
	uint32_t (*now)(void *ctx);
};

struct tsess_counters {
	uint32_t records_tx;
	uint32_t records_rx;
	uint32_t frames;
	uint32_t results;
	uint32_t protocol_errors;
	uint32_t timeouts;
	uint32_t render_ms_max;
	uint32_t layout_reloads; /* a commit borrowed the shared buffer mid-frame */
	uint32_t unauth_statuses; /* statuses outside authenticated records (10) */
};

struct tsess {
	const struct tsess_io *io;
	void *ctx;
	struct dlv *dlv;
	struct fontstore *fonts;
	const struct ctag_sha256_ops *sha;
	uint16_t pace_ms; /* one connection interval */
	uint8_t state;
	uint8_t status;
	uint32_t tag_id;
	uint32_t epoch;
	uint8_t key[CTAG_TAG_KEY_LEN];
	uint32_t connected_ms;
	uint32_t suspend_ms;
	struct ctag_tag_caps caps;
	struct ctag_ctrl_challenge ch;
	struct ctag_session sess;
	/* Fragmentation (5.3), one per characteristic and direction. */
	struct ctag_frag_tx ctrl_tx;
	struct ctag_frag_tx data_tx;
	struct ctag_frag_rx ctrl_rx;
	struct ctag_frag_rx status_rx;
	uint8_t ctrl_in[CTAG_TAG_CTRL_MSG_MAX];
	uint8_t status_in[CTAG_TAG_RECORD_WIRE_MAX];
	uint8_t ctrl_out[CTAG_TAG_CTRL_MSG_MAX];
	size_t ctrl_len;
	size_t ctrl_off;
	bool ctrl_inflight;
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
	size_t rec_len;
	size_t rec_off;
	uint8_t inflight;
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	/* Credits (5.5), counted per record (10): the first CREDIT after AUTH_OK
	 * is the tag's window, every later one returns processed records. */
	uint32_t credits;       /* records the bridge may send now */
	uint32_t credited;      /* records the tag has returned a credit for */
	uint32_t records_sent;
	uint32_t fb_index;      /* the current FRAME_BEGIN's record number */
	bool window_set;
	uint8_t records_in_event;
	bool pacing;
	/* Absolute bounds (10). */
	uint32_t hs_deadline;    /* AUTH_OK by then */
	uint32_t frame_deadline; /* the current frame's RESULT by then */
	bool in_frame;
	/* Jobs of this session (those present at its start). */
	uint32_t upto;
	uint32_t last_order;
	struct dlv_job *job;
	uint32_t job_started;
	bool refreshing_reported;
	/* Frame of the current layout job. */
	struct fontstore_view view;
	struct ctag_render render;
	struct ctag_render_work work;
	struct ctag_render_panel panel;
	uint32_t plane_len;
	uint16_t row_bytes;
	uint16_t strip_rows;
	uint8_t plane;
	uint32_t offset;
	int32_t strip_y0; /* -1: nothing rendered */
	uint8_t strip_plane;
	size_t strip_len;
	uint8_t digest[CTAG_FRAME_DIGEST_LEN];
	/* The layout itself is in the delivery core's shared buffer (dlv.layout). */
	uint8_t strip[TSESS_STRIP_BUF] __aligned(4);
	struct tsess_counters c;
};

void tsess_init(struct tsess *s, const struct tsess_io *io, void *ctx, struct dlv *dlv,
		struct fontstore *fonts, const struct ctag_sha256_ops *sha);

/* Connection up (at connected_ms), mesh resumed, CTRL/STATUS subscribed:
 * run the session. */
void tsess_start(struct tsess *s, uint32_t tag_id, uint32_t suspend_ms, uint16_t pace_ms,
		 uint32_t connected_ms);
/* End now (link lost, resume failure): jobs stay pending; done() is called. */
void tsess_abort(struct tsess *s, uint8_t status);
bool tsess_active(const struct tsess *s);

/* ---- Completions and received values ---- */
void tsess_caps(struct tsess *s, int err, const uint8_t *data, uint16_t len);
void tsess_ctrl_written(struct tsess *s, int err);
void tsess_data_sent(struct tsess *s);
void tsess_ctrl_value(struct tsess *s, const uint8_t *val, uint16_t len);   /* indication */
void tsess_status_value(struct tsess *s, const uint8_t *val, uint16_t len); /* notification */
void tsess_timeout(struct tsess *s, uint8_t which);

#endif /* BRIDGE_TAGSESS_H_ */
