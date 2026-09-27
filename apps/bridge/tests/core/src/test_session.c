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
	uint32_t at;  /* delivered at this time */
	uint32_t seq; /* push order among events due at the same time */
	uint8_t data[CTAG_TAG_CAPS_LEN + 4];
};

static struct ev q[2048];
static size_t q_n;
static uint32_t q_seq;
static uint32_t clock_ms;
/* The tag's CTRL indications and STATUS notifications arrive this much later. */
static uint32_t tag_delay;

static void push_at(uint8_t type, const uint8_t *data, size_t len, uint32_t at)
{
	struct ev *e;

	zassert_true(q_n < ARRAY_SIZE(q), "event queue overflow");
	e = &q[q_n++];
	e->type = type;
	e->len = (uint8_t)len;
	e->at = at;
	e->seq = q_seq++;
	if (len > 0) {
		memcpy(e->data, data, len);
	}
}

static void push(uint8_t type, const uint8_t *data, size_t len)
{
	bool from_tag = type == EV_CTRL_IND || type == EV_STATUS_NTF;

	push_at(type, data, len, clock_ms + (from_tag ? tag_delay : 0u));
}

/* Index of the next event (earliest time, then push order); -1 when none. */
static int next_event(void)
{
	int best = -1;

	for (size_t i = 0; i < q_n; i++) {
		if (best < 0 || q[i].at < q[best].at ||
		    (q[i].at == q[best].at && q[i].seq < q[best].seq)) {
			best = (int)i;
		}
	}
	return best;
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
	/* Misbehaving peers (review probes). */
	bool credit0_for_hello;  /* HELLO answered by CREDIT{0} every 4 s, never CHALLENGE */
	bool credit0_after_auth; /* CREDIT{0} after AUTH_OK and every 4 s, never a credit */
	bool fragment_stream;    /* HELLO answered by CTRL fragments that never end */
	uint32_t slow_credit_ms; /* after FRAME_BEGIN its values arrive this much later */
	uint32_t reply_delay_ms; /* every CTRL/STATUS value arrives this much later */
	uint32_t records;        /* records received */
	uint32_t fb_at;          /* time of the last accepted FRAME_BEGIN */
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

/* CREDIT{0} 900 times, every 4 s from now on (the reviewer's 3600 s probe). */
static void credit0_every_4s(void)
{
	static const uint8_t msg[2] = {CTAG_PLAIN_CREDIT, 0};
	uint8_t v[CTAG_ATT_VALUE_MAX];

	for (uint32_t i = 0; i < 900u; i++) {
		size_t off = 0;
		int n = ctag_frag_next(&tag.status_tx, msg, sizeof(msg), &off, CTAG_FRAG_PAYLOAD_MAX, v);

		zassert_true(n > 0);
		push_at(EV_STATUS_NTF, v, (size_t)n, clock_ms + i * 4000u);
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

	if (msg[0] == CTAG_CTRL_HELLO && tag.credit0_for_hello) {
		credit0_every_4s();
	} else if (msg[0] == CTAG_CTRL_HELLO && tag.fragment_stream) {
		/* The first fragments of a maximum-size CTRL message, 4 s apart. */
		static uint8_t big[CTAG_TAG_CTRL_MSG_MAX];
		uint8_t v[CTAG_ATT_VALUE_MAX];
		size_t off = 0;

		for (uint32_t i = 1; i <= 3u; i++) {
			int n = ctag_frag_next(&tag.ctrl_tx, big, sizeof(big), &off,
					       CTAG_FRAG_PAYLOAD_MAX, v);

			zassert_true(n > 0 && off < sizeof(big));
			push_at(EV_CTRL_IND, v, (size_t)n, clock_ms + i * 4000u);
		}
	} else if (msg[0] == CTAG_CTRL_HELLO) {
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
			if (tag.credit0_after_auth) {
				credit0_every_4s();
			} else {
				tag_credit(tag.caps.credits);
			}
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
	tag.records++;
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
			tag.fb_at = clock_ms;
			if (tag.slow_credit_ms != 0u) {
				tag_delay = tag.slow_credit_ms;
			}
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
	q_n = 0;
	tag_delay = tag.reply_delay_ms;
}

/* ---- The bridge side: tsess_io ---- */

static struct tsess sess;
static uint32_t timers[2];
static bool done;
static uint8_t done_status;
static uint32_t session_t0; /* the connection */
static uint32_t in_event, max_in_event;
/* Mesh work between two link events once the tag has this many PLANE_DATA
 * records (the bridge work queue interleaves both). */
static void (*mid_fn)(void);
static uint32_t mid_at;

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
	session_t0 = clock_ms;
	tsess_start(&sess, TAG, suspend_ms, 40, clock_ms);
	for (int guard = 0; !done; guard++) {
		int i;

		zassert_true(guard < 2000000, "session never ended");
		if (!tag.link_up) {
			q_n = 0;
			tsess_abort(&sess, CTAG_STATUS_DISCONNECTED); /* the disconnected event */
			break;
		}
		if (mid_fn != NULL && tag.plane_records >= mid_at) {
			void (*f)(void) = mid_fn;

			mid_fn = NULL;
			f();
		}
		i = next_event();
		uint8_t which = timers[0] <= timers[1] ? 0 : 1;

		if (i >= 0 && (q[i].at <= clock_ms || q[i].at <= timers[which])) {
			struct ev e = q[i];

			q[i] = q[--q_n];
			clock_ms = MAX(clock_ms, e.at);
			dispatch(&e);
			continue;
		}
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

static uint8_t tag_key[16];

static void setup(const struct v_render *v, const uint8_t *key_override)
{
	uint8_t key[16];

	env_fresh(1000);
	install_fixture_pack();
	zassert_ok(ctag_session_k_epoch(secret, TAG, EPOCH, key));
	memcpy(tag_key, key, sizeof(tag_key));
	assign(&benv.dlv, TAG, EPOCH, key_override != NULL ? key_override : key);
	sent_clear();
	setup_tag(v);
	tsess_init(&sess, &io, NULL, &benv.dlv, &benv.fonts, &test_sha);
	clock_ms = 5000;
	max_in_event = 0;
	in_event = 0;
	mid_fn = NULL;
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
	assert_one_seq_per_update();
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
	/* 10: CHALLENGE is unauthenticated: a link failure twice ... */
	for (int i = 0; i < (int)DLV_UNAUTH_REPEATS - 1; i++) {
		zassert_equal(run(10), CTAG_STATUS_STALE_EPOCH);
		zassert_false(last_result(42, &r), "session %d ended the job", i + 1);
		zassert_true(dlv_has_work(&benv.dlv, TAG));
	}
	/* ... and final in the third consecutive session. */
	zassert_equal(run(10), CTAG_STATUS_STALE_EPOCH);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_STALE_EPOCH);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
	zassert_equal(sess.c.unauth_statuses, DLV_UNAUTH_REPEATS);
}

ZTEST(bridge_session, test_wrong_key_auth_failed)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	static const uint8_t wrong[16] = {1};
	struct ctag_mesh_delivery_result r;

	setup(v, wrong);
	send(v, 1, 42);
	for (int i = 0; i < (int)DLV_UNAUTH_REPEATS - 1; i++) {
		zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
		zassert_false(last_result(42, &r));
	}
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_AUTH_FAILED);
	zassert_equal(tag.stored_epoch, 0, "a failed AUTH never raises the tag's epoch");
}

/* 10: the count of an unauthenticated status restarts after an
 * authenticated session and with another status; per tag and epoch. */
ZTEST(bridge_session, test_unauthenticated_status_count)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	static const uint8_t wrong[16] = {1};
	struct ctag_mesh_delivery_result r;

	setup(v, wrong);
	send(v, 1, 42);
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	/* The right key (same epoch): authenticated, delivered, count restarts. */
	assign(&benv.dlv, TAG, EPOCH, tag_key);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	send(v, 2, 43);
	assign(&benv.dlv, TAG, EPOCH, wrong);
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_false(last_result(43, &r), "2 after the reset, not 4 in total");
	/* Another status starts its own count. */
	assign(&benv.dlv, TAG, EPOCH, tag_key);
	tag.stored_epoch = EPOCH + 1;
	zassert_equal(run(10), CTAG_STATUS_STALE_EPOCH);
	zassert_false(last_result(43, &r));
	/* A new epoch starts over as well. */
	zassert_equal(benv.dlv.asg[0].unauth_count, 1);
	assign(&benv.dlv, TAG, EPOCH + 1, NULL);
	zassert_equal(benv.dlv.asg[0].unauth_count, 0);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED, "the old epoch's job");
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
	assert_one_seq_per_update();
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

static uint8_t other[CTAG_LAYOUT_HARD_MAX];

static void commit_other_tag(void)
{
	struct xfer t = {.xfer_id = 900, .tag_id = TAG + 1, .epoch = 1, .revision = 1,
			 .update_id = 900};

	zassert_equal(deliver(&benv.dlv, &t, other, big_layout(other), 0, clock_ms, NULL),
		      CTAG_STATUS_OK);
}

/* Another tag's LAYOUT_COMMIT validates in the shared layout buffer while a
 * frame streams: the session reloads its layout and the frame is unchanged. */
ZTEST(bridge_session, test_commit_during_frame)
{
	const struct v_render *v = scenario("rot1_every_command");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	assign(&benv.dlv, TAG + 1, 1, NULL);
	send(v, 1, 42);
	mid_fn = commit_other_tag;
	mid_at = 3;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_is_null(mid_fn, "the commit ran mid-frame");
	zassert_true(sess.c.layout_reloads >= 1);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	zassert_mem_equal(tag.rec.digest, v->frame_digest, 32);
	zassert_true(dlv_has_work(&benv.dlv, TAG + 1));
	assert_one_seq_per_update();
}

static void redeliver_same_revision(void)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct xfer t = {.xfer_id = 901, .tag_id = TAG, .epoch = EPOCH, .revision = 1,
			 .update_id = 44};

	zassert_equal(deliver(&benv.dlv, &t, v->layout, v->len, 0, clock_ms, NULL),
		      CTAG_STATUS_DUPLICATE);
}

/* The revision being drawn is re-delivered under a new update_id: the job
 * adopts it (and moves to the new record) mid-frame; one result, for it. */
ZTEST(bridge_session, test_redelivery_during_frame)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	mid_fn = redeliver_same_revision;
	mid_at = 5;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_is_null(mid_fn);
	zassert_true(sess.c.layout_reloads >= 1);
	zassert_true(last_result(44, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	zassert_equal(results_for(42), 0, "the adopted update_id only");
	zassert_equal(tag.refreshes, 1);
	assert_one_seq_per_update();
}

/* Review probe: CREDIT{0} every 4 s instead of CHALLENGE, for an hour. A
 * CREDIT before AUTH_OK ends the session at once (10). */
ZTEST(bridge_session, test_credit_before_auth_ok_ends_session)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tag.credit0_for_hello = true;
	zassert_equal(run(10), CTAG_STATUS_INVALID);
	zassert_true(clock_ms - session_t0 <= TSESS_HANDSHAKE_MS, "held %u ms",
		     clock_ms - session_t0);
	zassert_false(last_result(42, &r), "a link failure");
	zassert_true(dlv_has_work(&benv.dlv, TAG));
}

/* CREDIT{0} forever after AUTH_OK: not progress, the step timer ends it. */
ZTEST(bridge_session, test_zero_credits_time_out)
{
	const struct v_render *v = scenario("rot0_bw_status_card");

	setup(v, NULL);
	send(v, 1, 42);
	tag.credit0_after_auth = true;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_true(clock_ms - session_t0 <= TSESS_STEP_TIMEOUT_MS + 100u, "held %u ms",
		     clock_ms - session_t0);
	zassert_true(dlv_has_work(&benv.dlv, TAG));
}

/* CTRL fragments of a message that never completes: fragments are not
 * progress; the handshake bound ends it 5 s after the connection. */
ZTEST(bridge_session, test_fragment_stream_times_out)
{
	const struct v_render *v = scenario("rot0_bw_status_card");

	setup(v, NULL);
	send(v, 1, 42);
	tag.fragment_stream = true;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_equal(clock_ms - session_t0, TSESS_HANDSHAKE_MS);
}

/* Every handshake message 3 s late: each one is progress, but the handshake
 * as a whole is bounded (5 s from the connection). */
ZTEST(bridge_session, test_handshake_deadline)
{
	const struct v_render *v = scenario("rot0_bw_status_card");

	setup(v, NULL);
	send(v, 1, 42);
	tag.reply_delay_ms = 3000u;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_equal(clock_ms - session_t0, TSESS_HANDSHAKE_MS);
	zassert_true(dlv_has_work(&benv.dlv, TAG));
}

/* A tag returning a credit every 4 s keeps the step timer alive, but the
 * frame has an absolute bound: 60 s + 1 s per KiB + the refresh bound. */
ZTEST(bridge_session, test_frame_deadline)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	uint32_t kib = DIV_ROUND_UP(v->plane_len * v->planes, 1024u);
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tag.slow_credit_ms = 4000u;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_equal(clock_ms - tag.fb_at,
		      TSESS_FRAME_BASE_MS + kib * TSESS_FRAME_PER_KIB_MS + TSESS_REFRESH_BOUND_MS);
	zassert_true(tag.plane_records < DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX));
	zassert_false(last_result(42, &r), "a link failure: retried later");
	zassert_true(dlv_has_work(&benv.dlv, TAG));
}

/* Review probe: a CMD, then a layout the tag refuses at FRAME_BEGIN (it shows
 * a newer revision). The CMD's credit must not start the plane data (10). */
ZTEST(bridge_session, test_plane_data_waits_for_its_frame_begin_credit)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_tag_cmd m = {.update_id = 77, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_SLEEP};
	struct ctag_mesh_delivery_result r;
	uint8_t digest[32] = {1};

	setup(v, NULL);
	ctag_txn_intent(&tag.rec, TAG, EPOCH, 10, 999, digest);
	ctag_txn_complete(&tag.rec, CTAG_STATUS_OK);
	tag.has_rec = true;
	tag.stored_epoch = EPOCH;
	dlv_tag_cmd(&benv.dlv, &m, 2000);
	send(v, 1, 42);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_equal(tag.records, 2, "CMD and FRAME_BEGIN only, not %u", tag.records);
	zassert_equal(tag.plane_records, 0);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_STALE_REVISION);
	assert_one_seq_per_update();
}

static void delete_assignment(void)
{
	struct ctag_mesh_assign_del del = {.tag_id = TAG, .epoch = EPOCH};

	zassert_equal(dlv_assign_del(&benv.dlv, &del, clock_ms), CTAG_STATUS_OK);
}

/* ASSIGN_DEL while the tag's job is in a session that then fails: the job
 * ends CANCELLED with the session instead of staying stranded. */
ZTEST(bridge_session, test_assignment_deleted_during_session)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	mid_fn = delete_assignment;
	mid_at = 3;
	tag.drop_after = 6;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
}

ZTEST_SUITE(bridge_session, NULL, NULL, NULL, NULL, NULL);
