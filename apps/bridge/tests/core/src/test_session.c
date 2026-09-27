/*
 * The bridge's tag session against a fake tag built from the tag-side
 * libraries (ctag_session tag role, ctag_txn, ctag_frag): handshake, credits,
 * the render pre-pass digest, strip streaming, results, and the failure paths
 * of docs/protocol.md 5.4-5.6 and 10.
 */
#include <string.h>

#include <ctag/ctag_frag.h>
#include <ctag/ctag_session.h>
#include <ctag/ctag_txn.h>

#include "common.h"
#include "tagsess.h"
#include "v_render.h"

#define TAG   0x11223344u
#define EPOCH 5u

static const uint8_t secret[32] = {
	0x5a, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
	0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f};

/* ---- The link: completions and values in the order the stack delivers them ---- */

enum ev_type {
	EV_CAPS,
	EV_CTRL_WRITTEN,
	EV_DATA_SENT,
	EV_CTRL_IND,
	EV_STATUS_NTF,
};

struct ev {
	uint8_t type;
	uint8_t len;
	uint8_t data[CTAG_TAG_CAPS_LEN + 4];
};

static struct ev q[2048];
static size_t q_head, q_tail;

static void push(uint8_t type, const uint8_t *data, size_t len)
{
	struct ev *e = &q[q_tail++ % ARRAY_SIZE(q)];

	zassert_true(q_tail - q_head <= ARRAY_SIZE(q), "event queue overflow");
	e->type = type;
	e->len = (uint8_t)len;
	if (len > 0) {
		memcpy(e->data, data, len);
	}
}

/* ---- Fake tag ---- */

struct fake_tag {
	struct ctag_tag_caps caps;
	struct ctag_session s;
	struct ctag_frag_rx ctrl_rx;
	struct ctag_frag_rx data_rx;
	struct ctag_frag_tx ctrl_tx;
	struct ctag_frag_tx status_tx;
	uint8_t ctrl_buf[CTAG_TAG_CTRL_MSG_MAX];
	uint8_t data_buf[CTAG_TAG_RECORD_WIRE_MAX];
	uint32_t stored_epoch;
	struct ctag_txn_record rec;
	bool has_rec;
	bool established;
	bool link_up;
	bool silent;          /* stops answering (the link stays up) */
	int32_t drop_after;   /* PLANE_DATA records before the link drops (-1: never) */
	bool lose_power;      /* at FRAME_END: REFRESH_INTENT persisted, then the reset */
	bool ind_first;       /* indications before the write response */
	int32_t granted;
	struct ctag_rec_frame_begin fb;
	bool framing;
	uint32_t got[2];
	uint8_t plane_now;
	ctag_sha256_ctx sha;
	uint32_t plane_records;
	uint32_t frames_begun;
	uint32_t refreshes;
	uint32_t errors;
};

static struct fake_tag tag;

static void tag_notify(struct ctag_frag_tx *tx, uint8_t type, const uint8_t *msg, size_t len)
{
	uint8_t v[CTAG_ATT_VALUE_MAX];
	size_t off = 0;
	int n;

	while ((n = ctag_frag_next(tx, msg, len, &off, CTAG_FRAG_PAYLOAD_MAX, v)) > 0) {
		push(type, v, (size_t)n);
	}
}

static void tag_record(uint8_t type, const uint8_t *pt, size_t len)
{
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
	int n = ctag_record_seal(&tag.s.tx, type, pt, len, rec, sizeof(rec));

	zassert_true(n > 0);
	tag_notify(&tag.status_tx, EV_STATUS_NTF, rec, (size_t)n);
}

static void tag_credit(uint8_t n)
{
	uint8_t msg[2] = {CTAG_PLAIN_CREDIT, n};

	tag.granted += n;
	tag_notify(&tag.status_tx, EV_STATUS_NTF, msg, sizeof(msg));
}

static void tag_result(uint64_t update_id, uint32_t revision, uint8_t status, const uint8_t *digest,
		       uint8_t flags)
{
	struct ctag_rec_result r = {.update_id = update_id, .epoch = tag.s.epoch,
				    .revision = revision, .status = status, .battery_mv = 2950,
				    .refresh_ms = 1234, .flags = flags};
	uint8_t pt[CTAG_REC_RESULT_LEN];

	if (digest != NULL) {
		memcpy(r.digest, digest, sizeof(r.digest));
	}
	(void)ctag_rec_result_pack(&r, pt, sizeof(pt));
	tag_record(CTAG_REC_RESULT, pt, sizeof(pt));
}

static void tag_ctrl_msg(const uint8_t *msg, size_t len)
{
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	size_t out_len = 0;

	if (msg[0] == CTAG_CTRL_HELLO) {
		struct ctag_ctrl_challenge ch = {
			.stored_epoch = tag.stored_epoch,
			.displayed_rev = tag.has_rec ? tag.rec.revision : 0,
			.last_status = tag.has_rec ? tag.rec.status : CTAG_STATUS_OK,
			.battery_mv = 2950,
			.flags = ctag_txn_unknown_pending(tag.has_rec ? &tag.rec : NULL) ? 1u : 0u,
		};

		zassert_ok(ctag_crypto_random(ch.nonce_t, sizeof(ch.nonce_t)));
		(void)ctag_session_tag_hello(&tag.s, TAG, secret, &ch, msg, len, out, &out_len);
		tag_notify(&tag.ctrl_tx, EV_CTRL_IND, out, out_len);
	} else if (msg[0] == CTAG_CTRL_AUTH) {
		uint8_t st = ctag_session_tag_auth(&tag.s, msg, len, out, &out_len);

		tag_notify(&tag.ctrl_tx, EV_CTRL_IND, out, out_len);
		if (st == CTAG_STATUS_OK) {
			tag.established = true;
			tag.stored_epoch = MAX(tag.stored_epoch, tag.s.epoch);
			tag_credit(tag.caps.credits);
		}
	} else {
		tag.errors++;
	}
}

static void white_digest(uint8_t digest[32])
{
	ctag_sha256_ctx c;
	uint8_t row[64];
	uint32_t plane_len = (tag.caps.width + 7u) / 8u * tag.caps.height;

	zassert_ok(ctag_crypto_sha256_init(&c));
	for (uint8_t p = 0; p < tag.caps.planes; p++) {
		bool one = p == 0 ? (tag.caps.plane_flags & 1u) : !(tag.caps.plane_flags & 2u);

		memset(row, one ? 0xFF : 0x00, sizeof(row));
		for (uint32_t off = 0; off < plane_len; off += sizeof(row)) {
			zassert_ok(ctag_crypto_sha256_update(&c, row, MIN(sizeof(row), plane_len - off)));
		}
	}
	zassert_ok(ctag_crypto_sha256_finish(&c, digest));
}

static void tag_frame_end(void)
{
	uint8_t digest[32];
	uint8_t prog = CTAG_STAGE_REFRESHING;
	uint32_t plane_len = tag.fb.plane_len;

	tag.framing = false;
	zassert_ok(ctag_crypto_sha256_finish(&tag.sha, digest));
	if (tag.got[0] != plane_len || (tag.fb.planes == 2 && tag.got[1] != plane_len)) {
		tag_result(tag.fb.update_id, tag.fb.revision, CTAG_STATUS_INCOMPLETE, NULL, 0);
		return;
	}
	if (memcmp(digest, tag.fb.digest, 32) != 0) {
		tag_result(tag.fb.update_id, tag.fb.revision, CTAG_STATUS_DIGEST_MISMATCH, NULL, 0);
		return;
	}
	/* 6: REFRESH_INTENT before the refresh, DISPLAYED before RESULT. */
	ctag_txn_intent(&tag.rec, TAG, tag.s.epoch, tag.fb.revision, tag.fb.update_id, digest);
	tag.has_rec = true;
	tag_record(CTAG_REC_PROGRESS, &prog, 1);
	if (tag.lose_power) {
		tag.lose_power = false;
		tag.link_up = false; /* reset before DISPLAYED: RESULT never sent */
		(void)ctag_txn_boot(&tag.rec);
		return;
	}
	tag.refreshes++;
	ctag_txn_complete(&tag.rec, CTAG_STATUS_OK);
	tag_result(tag.fb.update_id, tag.fb.revision, CTAG_STATUS_OK, digest, 0);
}

static void tag_data_record(const uint8_t *rec, size_t len)
{
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	struct ctag_rec_frame_begin fb = {0};
	struct ctag_rec_plane_data pd = {0};
	struct ctag_rec_cmd cmd = {0};
	uint8_t type = 0;
	int n = ctag_record_open(&tag.s.rx, rec, len, &type, pt, sizeof(pt));

	zassert_true(n >= 0, "record failed authentication");
	zassert_true(tag.granted > 0, "a record without a credit");
	tag.granted--;
	switch (type) {
	case CTAG_REC_FRAME_BEGIN: {
		uint32_t plane_len = (tag.caps.width + 7u) / 8u * tag.caps.height;
		uint8_t d;

		zassert_ok(ctag_rec_frame_begin_unpack(&fb, pt, (size_t)n));
		tag.frames_begun++;
		d = ctag_txn_frame_begin(tag.has_rec ? &tag.rec : NULL, tag.s.epoch, &fb,
					 tag.caps.planes, (uint16_t)plane_len);
		if (d == CTAG_TXN_ACCEPT) {
			tag.fb = fb;
			tag.framing = true;
			tag.got[0] = tag.got[1] = 0;
			zassert_ok(ctag_crypto_sha256_init(&tag.sha));
		} else if (d == CTAG_STATUS_OK) {
			struct ctag_rec_result r;
			uint8_t buf[CTAG_REC_RESULT_LEN];

			ctag_txn_result(&tag.rec, 1u, &r); /* the stored ACK, duplicate flag */
			(void)ctag_rec_result_pack(&r, buf, sizeof(buf));
			tag_record(CTAG_REC_RESULT, buf, sizeof(buf));
		} else {
			tag_result(fb.update_id, fb.revision, d, NULL, 0);
		}
		break;
	}
	case CTAG_REC_PLANE_DATA:
		zassert_ok(ctag_rec_plane_data_unpack(&pd, pt, (size_t)n));
		if (!tag.framing) {
			break; /* stray after an immediate RESULT */
		}
		zassert_true(pd.plane == 0 ? tag.got[0] < tag.fb.plane_len
					   : tag.got[0] == tag.fb.plane_len,
			     "planes in order");
		zassert_equal(pd.offset, tag.got[pd.plane], "offset = bytes received");
		zassert_true(pd.data_len > 0 && pd.data_len <= CTAG_TAG_PLANE_DATA_MAX);
		zassert_ok(ctag_crypto_sha256_update(&tag.sha, pd.data, pd.data_len));
		tag.got[pd.plane] += (uint32_t)pd.data_len;
		tag.plane_records++;
		if (tag.drop_after >= 0 && tag.plane_records >= (uint32_t)tag.drop_after) {
			tag.drop_after = -1;
			tag.link_up = false;
			return;
		}
		break;
	case CTAG_REC_FRAME_END:
		zassert_equal(n, 0);
		if (tag.framing) {
			tag_frame_end();
		}
		break;
	case CTAG_REC_CMD:
		zassert_ok(ctag_rec_cmd_unpack(&cmd, pt, (size_t)n));
		if (cmd.cmd == CTAG_TAG_CMD_CLEAR) {
			uint8_t digest[32];

			white_digest(digest);
			ctag_txn_intent(&tag.rec, TAG, tag.s.epoch, 0, cmd.update_id, digest);
			ctag_txn_complete(&tag.rec, CTAG_STATUS_OK);
			tag.has_rec = true;
			tag_result(cmd.update_id, 0, CTAG_STATUS_OK, digest, 0);
		} else {
			tag_result(cmd.update_id, 0, CTAG_STATUS_UNSUPPORTED, NULL, 0);
		}
		break;
	default:
		zassert_unreachable("record type %u", type);
	}
	if (tag.link_up) {
		tag_credit(1); /* the record's buffer is free again */
	}
}

static void tag_connect(void)
{
	memset(&tag.s, 0, sizeof(tag.s));
	ctag_frag_rx_init(&tag.ctrl_rx, tag.ctrl_buf, sizeof(tag.ctrl_buf));
	ctag_frag_rx_init(&tag.data_rx, tag.data_buf, sizeof(tag.data_buf));
	ctag_frag_tx_init(&tag.ctrl_tx);
	ctag_frag_tx_init(&tag.status_tx);
	tag.established = false;
	tag.link_up = true;
	tag.granted = 0;
	tag.framing = false;
	q_head = q_tail = 0;
}

/* ---- The bridge side: tsess_io ---- */

static struct tsess sess;
static uint32_t clock_ms;
static uint32_t timers[2];
static bool done;
static uint8_t done_status;
static uint32_t in_event, max_in_event;

static int io_read_caps(void *ctx)
{
	uint8_t buf[CTAG_TAG_CAPS_LEN];

	(void)ctag_tag_caps_pack(&tag.caps, buf, sizeof(buf));
	push(EV_CAPS, buf, sizeof(buf));
	return 0;
}

static int io_write_ctrl(void *ctx, const uint8_t *val, uint16_t len)
{
	int n;

	zassert_true(len >= 2 && len <= CTAG_ATT_VALUE_MAX);
	if (!tag.link_up) {
		return -ENOTCONN;
	}
	if (!tag.ind_first) {
		push(EV_CTRL_WRITTEN, NULL, 0);
	}
	n = tag.silent ? 0 : ctag_frag_rx_put(&tag.ctrl_rx, val, len);
	zassert_true(n >= 0, "CTRL fragment rejected");
	if (n > 0) {
		tag_ctrl_msg(tag.ctrl_buf, (size_t)n);
	}
	if (tag.ind_first) {
		push(EV_CTRL_WRITTEN, NULL, 0);
	}
	return 0;
}

static int io_write_data(void *ctx, const uint8_t *val, uint16_t len)
{
	int n;

	zassert_true(len >= 2 && len <= CTAG_ATT_VALUE_MAX);
	zassert_true(sess.inflight < CONFIG_CTAG_BRIDGE_ATT_INFLIGHT);
	if (!tag.link_up) {
		return -ENOTCONN;
	}
	if (val[0] & CTAG_FRAG_START) {
		in_event++;
		max_in_event = MAX(max_in_event, in_event);
	}
	push(EV_DATA_SENT, NULL, 0);
	n = ctag_frag_rx_put(&tag.data_rx, val, len);
	zassert_true(n >= 0, "DATA fragment rejected");
	if (n > 0 && !tag.silent) {
		tag_data_record(tag.data_buf, (size_t)n);
	}
	return 0;
}

static void io_timer(void *ctx, uint8_t which, uint32_t ms)
{
	timers[which] = ms == TSESS_TIMER_OFF ? UINT32_MAX : clock_ms + ms;
}

static void io_done(void *ctx, uint8_t status)
{
	done = true;
	done_status = status;
}

static uint32_t io_now(void *ctx)
{
	return clock_ms;
}

static const struct tsess_io io = {
	.read_caps = io_read_caps,
	.write_ctrl = io_write_ctrl,
	.write_data = io_write_data,
	.timer = io_timer,
	.done = io_done,
	.now = io_now,
};

static void dispatch(struct ev *e)
{
	switch (e->type) {
	case EV_CAPS:
		tsess_caps(&sess, 0, e->data, e->len);
		break;
	case EV_CTRL_WRITTEN:
		tsess_ctrl_written(&sess, 0);
		break;
	case EV_DATA_SENT:
		tsess_data_sent(&sess);
		break;
	case EV_CTRL_IND:
		tsess_ctrl_value(&sess, e->data, e->len);
		break;
	default:
		tsess_status_value(&sess, e->data, e->len);
		break;
	}
}

/* Run the session until it reports done; returns its status. */
static uint8_t run(uint32_t suspend_ms)
{
	done = false;
	timers[0] = timers[1] = UINT32_MAX;
	tag_connect();
	tsess_start(&sess, TAG, suspend_ms, 40);
	for (int guard = 0; !done; guard++) {
		zassert_true(guard < 2000000, "session never ended");
		if (!tag.link_up) {
			q_head = q_tail;
			tsess_abort(&sess, CTAG_STATUS_DISCONNECTED); /* the disconnected event */
			break;
		}
		if (q_head != q_tail) {
			struct ev e = q[q_head++ % ARRAY_SIZE(q)];

			dispatch(&e);
			continue;
		}
		uint8_t which = timers[0] <= timers[1] ? 0 : 1;

		zassert_not_equal(timers[which], UINT32_MAX, "stalled: no event, no timer");
		clock_ms = MAX(clock_ms, timers[which]);
		timers[which] = UINT32_MAX;
		if (which == TSESS_T_PACE) {
			in_event = 0;
		}
		tsess_timeout(&sess, which);
	}
	zassert_true(done);
	return done_status;
}

/* ---- Fixtures ---- */

static const struct v_render *scenario(const char *name)
{
	for (size_t i = 0; i < ARRAY_SIZE(v_render); i++) {
		if (strcmp(v_render[i].name, name) == 0) {
			return &v_render[i];
		}
	}
	zassert_unreachable("no scenario %s", name);
	return NULL;
}

static void setup_tag(const struct v_render *v)
{
	memset(&tag, 0, sizeof(tag));
	tag.caps = (struct ctag_tag_caps){
		.proto = CTAG_PROTO_VERSION, .tag_id = TAG, .board = CTAG_BOARD_NRF52DK_TAG,
		.panel = 1, .width = v->width, .height = v->height, .planes = v->planes,
		.plane_flags = v->plane_flags, .max_record = CTAG_TAG_RECORD_PAYLOAD_MAX,
		.credits = 2,
	};
	tag.drop_after = -1;
}

static void setup(const struct v_render *v, const uint8_t *key_override)
{
	uint8_t key[16];

	env_fresh(1000);
	install_fixture_pack();
	zassert_ok(ctag_session_k_epoch(secret, TAG, EPOCH, key));
	assign(&benv.dlv, TAG, EPOCH, key_override != NULL ? key_override : key);
	sent_clear();
	setup_tag(v);
	tsess_init(&sess, &io, NULL, &benv.dlv, &benv.fonts, &test_sha);
	clock_ms = 5000;
	max_in_event = 0;
	in_event = 0;
}

static void send(const struct v_render *v, uint32_t revision, uint64_t update_id)
{
	struct xfer t = {.xfer_id = (uint16_t)update_id, .tag_id = TAG, .epoch = EPOCH,
			 .revision = revision, .update_id = update_id};

	zassert_equal(deliver(&benv.dlv, &t, v->layout, v->len, 0, 1000, NULL), CTAG_STATUS_OK);
}

static bool stage_sent(uint64_t update_id, uint8_t stage)
{
	for (size_t i = 0; i < sent_n; i++) {
		struct ctag_mesh_delivery_stage m;

		if (sent_log[i].op == CTAG_MESH_OP_DELIVERY_STAGE &&
		    ctag_mesh_delivery_stage_unpack(&m, sent_log[i].data, sent_log[i].len) == 0 &&
		    m.update_id == update_id && m.stage == stage) {
			return true;
		}
	}
	return false;
}

/* ---- Tests ---- */

static void end_to_end(const char *name, bool ind_first)
{
	const struct v_render *v = scenario(name);
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	tag.ind_first = ind_first;
	send(v, 1, 42);
	zassert_equal(run(250), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r), "no DELIVERY_RESULT");
	zassert_equal(r.status, CTAG_STATUS_OK);
	/* The tag reproduced FRAME_BEGIN's digest: the fixture frame digest. */
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	zassert_mem_equal(tag.rec.digest, v->frame_digest, 32);
	zassert_equal(r.suspend_ms, 250);
	zassert_equal(r.refresh_ms, 1234);
	zassert_equal(r.battery_mv, 2950);
	zassert_equal(r.wake_ms, 5000 - 1000);
	zassert_true(stage_sent(42, CTAG_STAGE_TRANSFERRING));
	zassert_true(stage_sent(42, CTAG_STAGE_REFRESHING));
	zassert_true(max_in_event <= TSESS_RECORDS_PER_EVENT, "%u records in one event",
		     max_in_event);
	zassert_equal(tag.plane_records, DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX) *
						 v->planes);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
	zassert_equal(tag.stored_epoch, EPOCH);
}

ZTEST(bridge_session, test_end_to_end_one_plane)
{
	end_to_end("rot0_bw_status_card", false);
}

ZTEST(bridge_session, test_end_to_end_two_planes_rotated)
{
	end_to_end("rot1_every_command", true);
}

ZTEST(bridge_session, test_end_to_end_qr_bwr)
{
	end_to_end("qr", false);
}

ZTEST(bridge_session, test_duplicate_answered_by_tag)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	zassert_equal(run(10), CTAG_STATUS_OK);
	/* A replaced bridge without history: the tag answers from its record. */
	memset(benv.dlv.hist, 0, sizeof(benv.dlv.hist));
	send(v, 1, 43);
	tag.plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tag.plane_records, 0, "no image transfer for a displayed revision");
	zassert_equal(tag.refreshes, 1);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
}

ZTEST(bridge_session, test_disconnect_mid_transfer)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;
	struct dlv_job *job;

	setup(v, NULL);
	send(v, 1, 42);
	tag.drop_after = 20;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	/* 10: link failures never produce a result; the job stays pending. */
	zassert_false(last_result(42, &r));
	job = dlv_next_job(&benv.dlv, TAG, 0, UINT32_MAX);
	zassert_not_null(job);
	zassert_false(job->flags & DLV_JOB_IN_SESSION);
	zassert_equal(tag.refreshes, 0);
	/* The next session restarts the frame from offset 0 and completes it. */
	tag.plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tag.plane_records, DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX));
}

ZTEST(bridge_session, test_result_lost_display_state_unknown)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tag.lose_power = true;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	zassert_false(last_result(42, &r));
	/* Next CHALLENGE reports the unknown state for this (epoch, revision). */
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_DISPLAY_STATE_UNKNOWN);
	zassert_equal(r.battery_mv, 2950);
	/* The companion re-delivers the same revision: accepted again, redrawn. */
	send(v, 1, 43);
	tag.plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tag.refreshes, 1);
}

ZTEST(bridge_session, test_stale_epoch_ends_jobs)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tag.stored_epoch = EPOCH + 1;
	zassert_equal(run(10), CTAG_STATUS_STALE_EPOCH);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_STALE_EPOCH);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
}

ZTEST(bridge_session, test_wrong_key_auth_failed)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	static const uint8_t wrong[16] = {1};
	struct ctag_mesh_delivery_result r;

	setup(v, wrong);
	send(v, 1, 42);
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_AUTH_FAILED);
	zassert_equal(tag.stored_epoch, 0, "a failed AUTH never raises the tag's epoch");
}

ZTEST(bridge_session, test_panel_geometry_invalid)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tag.caps.width = 296; /* 4.4 Rotation: 400 x 300 does not fit */
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_INVALID);
	zassert_equal(tag.frames_begun, 0);
}

ZTEST(bridge_session, test_caps_of_another_tag)
{
	const struct v_render *v = scenario("rot0_bw_status_card");

	setup(v, NULL);
	send(v, 1, 42);
	tag.caps.tag_id = TAG + 1;
	zassert_equal(run(10), CTAG_STATUS_NOT_FOUND);
	zassert_true(dlv_has_work(&benv.dlv, TAG), "not a tag ERROR: retried later");
}

ZTEST(bridge_session, test_silent_tag_times_out)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	uint32_t t0;

	setup(v, NULL);
	send(v, 1, 42);
	tag.silent = true;
	t0 = clock_ms;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_equal(clock_ms - t0, TSESS_STEP_TIMEOUT_MS);
	zassert_true(dlv_has_work(&benv.dlv, TAG));
}

ZTEST(bridge_session, test_commands_and_clear)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_tag_cmd m = {.update_id = 77, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_CLEAR};
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	dlv_tag_cmd(&benv.dlv, &m, 2000);
	m.update_id = 78;
	m.cmd = CTAG_TAG_CMD_SLEEP;
	dlv_tag_cmd(&benv.dlv, &m, 2000);
	zassert_equal(run(10), CTAG_STATUS_OK);
	/* In arrival order: the layout, CLEAR, SLEEP. */
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_true(last_result(77, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(r.revision, 0);
	zassert_true(last_result(78, &r));
	zassert_equal(r.status, CTAG_STATUS_UNSUPPORTED, "no verified external wake");
	/* After CLEAR the same revision is drawn again (10). */
	send(v, 1, 43);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tag.refreshes, 2);
}

ZTEST(bridge_session, test_superseded_after_interrupted_session)
{
	const struct v_render *v = scenario("rot0_asym");
	const struct v_render *v2 = scenario("rot2_asym");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	/* Revision 1 is interrupted; revision 2 then replaces it (SUPERSEDED)
	 * and the next session draws revision 2. */
	tag.drop_after = 1;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	send(v2, 2, 43);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_SUPERSEDED);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_mem_equal(r.digest, v2->frame_digest, 8);
}

ZTEST_SUITE(bridge_session, NULL, NULL, NULL, NULL, NULL);
