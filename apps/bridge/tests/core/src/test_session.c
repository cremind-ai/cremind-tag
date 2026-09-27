/*
 * The bridge's tag session against a fake tag built from the tag-side
 * libraries (ctag_session tag role, ctag_txn, ctag_frag): handshake (over the
 * CAPS the tag serves), credits, the render pre-pass digest, strip streaming,
 * results (with the tag's stored epoch and the result flags), and the failure
 * paths of docs/protocol.md 5.4-5.6 and 10.
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
	uint8_t who;  /* the fake tag / bridge session it belongs to */
	uint32_t at;  /* delivered at this time */
	uint32_t seq; /* push order among events due at the same time */
	uint8_t data[CTAG_TAG_CAPS_LEN + 4];
};

static struct ev q[2048];
static size_t q_n;
static uint32_t q_seq;
static uint32_t clock_ms;
static uint8_t cur; /* the current fake tag and session (sel()) */
static uint32_t tag_delay_now(void);

static void push_at(uint8_t type, const uint8_t *data, size_t len, uint32_t at)
{
	struct ev *e;

	zassert_true(q_n < ARRAY_SIZE(q), "event queue overflow");
	e = &q[q_n++];
	e->type = type;
	e->len = (uint8_t)len;
	e->at = at;
	e->who = cur;
	e->seq = q_seq++;
	if (len > 0) {
		memcpy(e->data, data, len);
	}
}

static void push(uint8_t type, const uint8_t *data, size_t len)
{
	bool from_tag = type == EV_CTRL_IND || type == EV_STATUS_NTF;

	push_at(type, data, len, clock_ms + (from_tag ? tag_delay_now() : 0u));
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
	uint32_t id; /* the tag's own id (caps.tag_id is what it serves) */
	/* Its CTRL indications and STATUS notifications arrive this much later. */
	uint32_t delay;
	uint32_t refresh_ms;  /* the RESULT of a refresh (FRAME_END, CLEAR) this much later */
	bool never_refresh;   /* the refresh never completes: no RESULT */
	/* A refresh is running: the record's credit follows its RESULT (the tag
	 * frees the buffer once the transaction is over), then the delay is back. */
	bool refreshing;
	uint32_t base_delay;
	struct ctag_tag_caps caps;
	/* An active relay: the bridge reads relay_caps, the tag hashes caps. */
	bool relay;
	struct ctag_tag_caps relay_caps;
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

static struct fake_tag tags[2];
static struct fake_tag *tp = &tags[0];

/* The current fake tag and bridge session (0: the one of the single-session tests). */
static void sel(uintptr_t who)
{
	cur = (uint8_t)who;
	tp = &tags[who];
}

static uint32_t tag_delay_now(void)
{
	return tp->delay;
}

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
		int n = ctag_frag_next(&tp->status_tx, msg, sizeof(msg), &off, CTAG_FRAG_PAYLOAD_MAX, v);

		zassert_true(n > 0);
		push_at(EV_STATUS_NTF, v, (size_t)n, clock_ms + i * 4000u);
	}
}

static void tag_record(uint8_t type, const uint8_t *pt, size_t len)
{
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
	int n = ctag_record_seal(&tp->s.tx, type, pt, len, rec, sizeof(rec));

	zassert_true(n > 0);
	tag_notify(&tp->status_tx, EV_STATUS_NTF, rec, (size_t)n);
}

static void tag_credit(uint8_t n)
{
	uint8_t msg[2] = {CTAG_PLAIN_CREDIT, n};

	tp->granted += n;
	tag_notify(&tp->status_tx, EV_STATUS_NTF, msg, sizeof(msg));
}

static void tag_result(uint64_t update_id, uint32_t revision, uint8_t status, const uint8_t *digest,
		       uint8_t flags)
{
	struct ctag_rec_result r = {.update_id = update_id, .epoch = tp->s.epoch,
				    .revision = revision, .status = status, .battery_mv = 2950,
				    .refresh_ms = 1234, .flags = flags};
	uint8_t pt[CTAG_REC_RESULT_LEN];

	if (digest != NULL) {
		memcpy(r.digest, digest, sizeof(r.digest));
	}
	(void)ctag_rec_result_pack(&r, pt, sizeof(pt));
	tag_record(CTAG_REC_RESULT, pt, sizeof(pt));
}

/* The RESULT after a refresh (the panel's refresh time: the link idles
 * meanwhile); the record's credit follows it (tag_data_record). */
static void refresh_result(uint64_t update_id, uint32_t revision, const uint8_t *digest)
{
	tp->refreshing = true;
	tp->base_delay = tp->delay;
	if (tp->never_refresh) {
		return; /* stuck in the refresh: no RESULT, no credit */
	}
	tp->delay += tp->refresh_ms;
	tag_result(update_id, revision, CTAG_STATUS_OK, digest, 0);
}

static void tag_ctrl_msg(const uint8_t *msg, size_t len)
{
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	size_t out_len = 0;

	if (msg[0] == CTAG_CTRL_HELLO && tp->credit0_for_hello) {
		credit0_every_4s();
	} else if (msg[0] == CTAG_CTRL_HELLO && tp->fragment_stream) {
		/* The first fragments of a maximum-size CTRL message, 4 s apart. */
		static uint8_t big[CTAG_TAG_CTRL_MSG_MAX];
		uint8_t v[CTAG_ATT_VALUE_MAX];
		size_t off = 0;

		for (uint32_t i = 1; i <= 3u; i++) {
			int n = ctag_frag_next(&tp->ctrl_tx, big, sizeof(big), &off,
					       CTAG_FRAG_PAYLOAD_MAX, v);

			zassert_true(n > 0 && off < sizeof(big));
			push_at(EV_CTRL_IND, v, (size_t)n, clock_ms + i * 4000u);
		}
	} else if (msg[0] == CTAG_CTRL_HELLO) {
		struct ctag_ctrl_challenge ch = {
			.stored_epoch = tp->stored_epoch,
			.displayed_rev = tp->has_rec ? tp->rec.revision : 0,
			.last_status = tp->has_rec ? tp->rec.status : CTAG_STATUS_OK,
			.battery_mv = 2950,
			.flags = ctag_txn_unknown_pending(tp->has_rec ? &tp->rec : NULL) ? 1u : 0u,
		};

		uint8_t caps[CTAG_TAG_CAPS_LEN];

		zassert_ok(ctag_crypto_random(ch.nonce_t, sizeof(ch.nonce_t)));
		(void)ctag_tag_caps_pack(&tp->caps, caps, sizeof(caps)); /* the CAPS it serves */
		(void)ctag_session_tag_hello(&tp->s, tp->id, secret, caps, sizeof(caps), &ch, msg, len, out,
					     &out_len);
		tag_notify(&tp->ctrl_tx, EV_CTRL_IND, out, out_len);
	} else if (msg[0] == CTAG_CTRL_AUTH) {
		uint8_t st = ctag_session_tag_auth(&tp->s, tp->stored_epoch, msg, len, out, &out_len);

		tag_notify(&tp->ctrl_tx, EV_CTRL_IND, out, out_len);
		if (st == CTAG_STATUS_OK) {
			tp->established = true;
			tp->stored_epoch = MAX(tp->stored_epoch, tp->s.epoch);
			if (tp->credit0_after_auth) {
				credit0_every_4s();
			} else {
				tag_credit(tp->caps.credits);
			}
		}
	} else {
		tp->errors++;
	}
}

static void white_digest(uint8_t digest[32])
{
	ctag_sha256_ctx c;
	uint8_t row[64];
	uint32_t plane_len = (tp->caps.width + 7u) / 8u * tp->caps.height;

	zassert_ok(ctag_crypto_sha256_init(&c));
	for (uint8_t p = 0; p < tp->caps.planes; p++) {
		bool one = p == 0 ? (tp->caps.plane_flags & 1u) : !(tp->caps.plane_flags & 2u);

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
	uint32_t plane_len = tp->fb.plane_len;

	tp->framing = false;
	zassert_ok(ctag_crypto_sha256_finish(&tp->sha, digest));
	if (tp->got[0] != plane_len || (tp->fb.planes == 2 && tp->got[1] != plane_len)) {
		tag_result(tp->fb.update_id, tp->fb.revision, CTAG_STATUS_INCOMPLETE, NULL, 0);
		return;
	}
	if (memcmp(digest, tp->fb.digest, 32) != 0) {
		tag_result(tp->fb.update_id, tp->fb.revision, CTAG_STATUS_DIGEST_MISMATCH, NULL, 0);
		return;
	}
	/* 6: REFRESH_INTENT before the refresh, DISPLAYED before RESULT. */
	ctag_txn_intent(&tp->rec, tp->id, tp->s.epoch, tp->fb.revision, tp->fb.update_id, digest);
	tp->has_rec = true;
	tag_record(CTAG_REC_PROGRESS, &prog, 1);
	if (tp->lose_power) {
		tp->lose_power = false;
		tp->link_up = false; /* reset before DISPLAYED: RESULT never sent */
		(void)ctag_txn_boot(&tp->rec);
		return;
	}
	tp->refreshes++;
	ctag_txn_complete(&tp->rec, CTAG_STATUS_OK);
	refresh_result(tp->fb.update_id, tp->fb.revision, digest);
}

static void tag_data_record(const uint8_t *rec, size_t len)
{
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	struct ctag_rec_frame_begin fb = {0};
	struct ctag_rec_plane_data pd = {0};
	struct ctag_rec_cmd cmd = {0};
	uint8_t type = 0;
	int n = ctag_record_open(&tp->s.rx, rec, len, &type, pt, sizeof(pt));

	zassert_true(n >= 0, "record failed authentication");
	zassert_true(tp->granted > 0, "a record without a credit");
	tp->granted--;
	tp->records++;
	switch (type) {
	case CTAG_REC_FRAME_BEGIN: {
		uint32_t plane_len = (tp->caps.width + 7u) / 8u * tp->caps.height;
		uint8_t d;

		zassert_ok(ctag_rec_frame_begin_unpack(&fb, pt, (size_t)n));
		tp->frames_begun++;
		d = ctag_txn_frame_begin(tp->has_rec ? &tp->rec : NULL, tp->s.epoch, &fb,
					 tp->caps.planes, (uint16_t)plane_len);
		if (d == CTAG_TXN_ACCEPT) {
			tp->fb = fb;
			tp->framing = true;
			tp->fb_at = clock_ms;
			if (tp->slow_credit_ms != 0u) {
				tp->delay = tp->slow_credit_ms;
			}
			tp->got[0] = tp->got[1] = 0;
			zassert_ok(ctag_crypto_sha256_init(&tp->sha));
		} else if (d == CTAG_STATUS_OK) {
			struct ctag_rec_result r;
			uint8_t buf[CTAG_REC_RESULT_LEN];

			ctag_txn_result(&tp->rec, 1u, &r); /* the stored ACK, duplicate flag */
			(void)ctag_rec_result_pack(&r, buf, sizeof(buf));
			tag_record(CTAG_REC_RESULT, buf, sizeof(buf));
		} else {
			tag_result(fb.update_id, fb.revision, d, NULL, 0);
		}
		break;
	}
	case CTAG_REC_PLANE_DATA:
		zassert_ok(ctag_rec_plane_data_unpack(&pd, pt, (size_t)n));
		if (!tp->framing) {
			break; /* stray after an immediate RESULT */
		}
		zassert_true(pd.plane == 0 ? tp->got[0] < tp->fb.plane_len
					   : tp->got[0] == tp->fb.plane_len,
			     "planes in order");
		zassert_equal(pd.offset, tp->got[pd.plane], "offset = bytes received");
		zassert_true(pd.data_len > 0 && pd.data_len <= CTAG_TAG_PLANE_DATA_MAX);
		zassert_ok(ctag_crypto_sha256_update(&tp->sha, pd.data, pd.data_len));
		tp->got[pd.plane] += (uint32_t)pd.data_len;
		tp->plane_records++;
		if (tp->drop_after >= 0 && tp->plane_records >= (uint32_t)tp->drop_after) {
			tp->drop_after = -1;
			tp->link_up = false;
			return;
		}
		break;
	case CTAG_REC_FRAME_END:
		zassert_equal(n, 0);
		if (tp->framing) {
			tag_frame_end();
		}
		break;
	case CTAG_REC_CMD:
		zassert_ok(ctag_rec_cmd_unpack(&cmd, pt, (size_t)n));
		if (cmd.cmd == CTAG_TAG_CMD_CLEAR) {
			uint8_t digest[32];

			white_digest(digest);
			ctag_txn_intent(&tp->rec, tp->id, tp->s.epoch, 0, cmd.update_id, digest);
			ctag_txn_complete(&tp->rec, CTAG_STATUS_OK);
			tp->has_rec = true;
			refresh_result(cmd.update_id, 0, digest);
		} else {
			tag_result(cmd.update_id, 0, CTAG_STATUS_UNSUPPORTED, NULL, 0);
		}
		break;
	default:
		zassert_unreachable("record type %u", type);
	}
	if (tp->link_up && !(tp->refreshing && tp->never_refresh)) {
		tag_credit(1); /* the record's buffer is free again */
	}
	if (tp->refreshing) {
		tp->refreshing = false;
		tp->delay = tp->base_delay;
	}
}

static void tag_connect(void)
{
	memset(&tp->s, 0, sizeof(tp->s));
	ctag_frag_rx_init(&tp->ctrl_rx, tp->ctrl_buf, sizeof(tp->ctrl_buf));
	ctag_frag_rx_init(&tp->data_rx, tp->data_buf, sizeof(tp->data_buf));
	ctag_frag_tx_init(&tp->ctrl_tx);
	ctag_frag_tx_init(&tp->status_tx);
	tp->established = false;
	tp->link_up = true;
	tp->granted = 0;
	tp->framing = false;
	tp->delay = tp->reply_delay_ms;
}

/* ---- The bridge side: tsess_io ---- */

static struct tsess sessions[2];
#define sess (sessions[0])
static uint32_t timers[2][2]; /* per session: TSESS_T_STEP, TSESS_T_PACE */
static bool done[2];
static uint8_t done_status[2];
static uint32_t done_at[2];
static uint32_t session_t0; /* the connection */
static uint32_t in_event[2], max_in_event;
/* Mesh work between two link events once the tag has this many PLANE_DATA
 * records (the bridge work queue interleaves both). */
static void (*mid_fn)(void);
static uint32_t mid_at;

static int io_read_caps(void *ctx)
{
	uint8_t buf[CTAG_TAG_CAPS_LEN];

	sel((uintptr_t)ctx);
	(void)ctag_tag_caps_pack(tp->relay ? &tp->relay_caps : &tp->caps, buf, sizeof(buf));
	push(EV_CAPS, buf, sizeof(buf));
	return 0;
}

static int io_write_ctrl(void *ctx, const uint8_t *val, uint16_t len)
{
	int n;

	sel((uintptr_t)ctx);
	zassert_true(len >= 2 && len <= CTAG_ATT_VALUE_MAX);
	if (!tp->link_up) {
		return -ENOTCONN;
	}
	if (!tp->ind_first) {
		push(EV_CTRL_WRITTEN, NULL, 0);
	}
	n = tp->silent ? 0 : ctag_frag_rx_put(&tp->ctrl_rx, val, len);
	zassert_true(n >= 0, "CTRL fragment rejected");
	if (n > 0) {
		tag_ctrl_msg(tp->ctrl_buf, (size_t)n);
	}
	if (tp->ind_first) {
		push(EV_CTRL_WRITTEN, NULL, 0);
	}
	return 0;
}

static int io_write_data(void *ctx, const uint8_t *val, uint16_t len)
{
	uintptr_t who = (uintptr_t)ctx;
	int n;

	sel(who);
	zassert_true(len >= 2 && len <= CTAG_ATT_VALUE_MAX);
	zassert_true(sessions[who].inflight < CONFIG_CTAG_BRIDGE_ATT_INFLIGHT);
	if (!tp->link_up) {
		return -ENOTCONN;
	}
	if (val[0] & CTAG_FRAG_START) {
		in_event[who]++;
		max_in_event = MAX(max_in_event, in_event[who]);
	}
	push(EV_DATA_SENT, NULL, 0);
	n = ctag_frag_rx_put(&tp->data_rx, val, len);
	zassert_true(n >= 0, "DATA fragment rejected");
	if (n > 0 && !tp->silent) {
		tag_data_record(tp->data_buf, (size_t)n);
	}
	return 0;
}

static void io_timer(void *ctx, uint8_t which, uint32_t ms)
{
	timers[(uintptr_t)ctx][which] = ms == TSESS_TIMER_OFF ? UINT32_MAX : clock_ms + ms;
}

static void io_done(void *ctx, uint8_t status)
{
	done[(uintptr_t)ctx] = true;
	done_status[(uintptr_t)ctx] = status;
	done_at[(uintptr_t)ctx] = clock_ms;
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
	struct tsess *ss = &sessions[e->who];

	sel(e->who);
	switch (e->type) {
	case EV_CAPS:
		tsess_caps(ss, 0, e->data, e->len);
		break;
	case EV_CTRL_WRITTEN:
		tsess_ctrl_written(ss, 0);
		break;
	case EV_DATA_SENT:
		tsess_data_sent(ss);
		break;
	case EV_CTRL_IND:
		tsess_ctrl_value(ss, e->data, e->len);
		break;
	default:
		tsess_status_value(ss, e->data, e->len);
		break;
	}
}

/* Run the session until it reports done; returns its status. */
static uint8_t run(uint32_t suspend_ms)
{
	sel(0);
	done[0] = false;
	timers[0][0] = timers[0][1] = UINT32_MAX;
	q_n = 0;
	tag_connect();
	session_t0 = clock_ms;
	tsess_start(&sess, TAG, suspend_ms, 40, clock_ms);
	for (int guard = 0; !done[0]; guard++) {
		int i;

		zassert_true(guard < 2000000, "session never ended");
		sel(0);
		if (!tp->link_up) {
			q_n = 0;
			tsess_abort(&sess, CTAG_STATUS_DISCONNECTED); /* the disconnected event */
			break;
		}
		if (mid_fn != NULL && tp->plane_records >= mid_at) {
			void (*f)(void) = mid_fn;

			mid_fn = NULL;
			f();
		}
		i = next_event();
		uint8_t which = timers[0][0] <= timers[0][1] ? 0 : 1;

		if (i >= 0 && (q[i].at <= clock_ms || q[i].at <= timers[0][which])) {
			struct ev e = q[i];

			q[i] = q[--q_n];
			clock_ms = MAX(clock_ms, e.at);
			dispatch(&e);
			continue;
		}
		zassert_not_equal(timers[0][which], UINT32_MAX, "stalled: no event, no timer");
		clock_ms = MAX(clock_ms, timers[0][which]);
		timers[0][which] = UINT32_MAX;
		if (which == TSESS_T_PACE) {
			in_event[0] = 0;
		}
		tsess_timeout(&sess, which);
	}
	zassert_true(done[0]);
	return done_status[0];
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
	memset(tp, 0, sizeof(*tp));
	tp->id = TAG;
	tp->caps = (struct ctag_tag_caps){
		.proto = CTAG_PROTO_VERSION, .tag_id = TAG, .board = CTAG_BOARD_NRF52DK_TAG,
		.panel = 1, .width = v->width, .height = v->height, .planes = v->planes,
		.plane_flags = v->plane_flags, .max_record = CTAG_TAG_RECORD_PAYLOAD_MAX,
		.credits = 2,
	};
	tp->drop_after = -1;
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
	sel(0);
	setup_tag(v);
	tsess_init(&sess, &io, NULL, &benv.dlv, &benv.fonts, &test_sha);
	clock_ms = 5000;
	max_in_event = 0;
	in_event[0] = in_event[1] = 0;
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
	tp->ind_first = ind_first;
	send(v, 1, 42);
	zassert_equal(run(250), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r), "no DELIVERY_RESULT");
	zassert_equal(r.status, CTAG_STATUS_OK);
	/* The tag reproduced FRAME_BEGIN's digest: the fixture frame digest. */
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	zassert_mem_equal(tp->rec.digest, v->frame_digest, 32);
	zassert_equal(r.suspend_ms, 250);
	zassert_equal(r.refresh_ms, 1234);
	zassert_equal(r.battery_mv, 2950);
	zassert_equal(r.wake_ms, 5000 - 1000);
	zassert_equal(r.flags, 0u, "drawn: no result flag");
	zassert_equal(r.stored_epoch, EPOCH, "the tag stores the session's epoch after AUTH_OK");
	zassert_true(stage_sent(42, CTAG_STAGE_TRANSFERRING));
	zassert_true(stage_sent(42, CTAG_STAGE_REFRESHING));
	zassert_true(max_in_event <= TSESS_RECORDS_PER_EVENT, "%u records in one event",
		     max_in_event);
	zassert_equal(tp->plane_records, DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX) *
						 v->planes);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
	zassert_equal(tp->stored_epoch, EPOCH);
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
	tp->plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tp->plane_records, 0, "no image transfer for a displayed revision");
	zassert_equal(tp->refreshes, 1);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	/* The tag's duplicate flag reaches the gateway (bit0). */
	zassert_equal(r.flags, CTAG_RESULT_FLAG_DUPLICATE);
	zassert_equal(r.stored_epoch, EPOCH);
}

ZTEST(bridge_session, test_disconnect_mid_transfer)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;
	struct dlv_job *job;

	setup(v, NULL);
	send(v, 1, 42);
	tp->drop_after = 20;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	/* 10: link failures never produce a result; the job stays pending. */
	zassert_false(last_result(42, &r));
	job = dlv_next_job(&benv.dlv, TAG, 0, UINT32_MAX);
	zassert_not_null(job);
	zassert_false(job->flags & DLV_JOB_IN_SESSION);
	zassert_equal(tp->refreshes, 0);
	/* The next session restarts the frame from offset 0 and completes it. */
	tp->plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tp->plane_records, DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX));
}

ZTEST(bridge_session, test_result_lost_display_state_unknown)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tp->lose_power = true;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	zassert_false(last_result(42, &r));
	/* Next CHALLENGE reports the unknown state for this (epoch, revision). */
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_DISPLAY_STATE_UNKNOWN);
	zassert_equal(r.battery_mv, 2950);
	/* The companion re-delivers the same revision: accepted again, redrawn. */
	send(v, 1, 43);
	tp->plane_records = 0;
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(tp->refreshes, 1);
}

ZTEST(bridge_session, test_stale_epoch_ends_jobs)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tp->stored_epoch = EPOCH + 1;
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
	/* The tag's ERROR carried its stored epoch: the companion reassigns above
	 * it. The status was escalated from unauthenticated refusals (bit1). */
	zassert_equal(r.stored_epoch, EPOCH + 1u);
	zassert_equal(r.flags, CTAG_RESULT_FLAG_ESCALATED);
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
	zassert_equal(r.flags, CTAG_RESULT_FLAG_ESCALATED);
	zassert_equal(r.stored_epoch, 0u, "from the tag's CHALLENGE and ERROR");
	zassert_equal(tp->stored_epoch, 0, "a failed AUTH never raises the tag's epoch");
}

/*
 * 5.4, review finding "CAPS is never authenticated": an active relay rewrites
 * plane_flags in the CAPS the bridge reads. The transcript covers CAPS, so the
 * tag refuses the bridge's AUTH: nothing is rendered with the altered polarity,
 * and the unauthenticated AUTH_FAILED ends the job only in the third session.
 */
ZTEST(bridge_session, test_relayed_caps_fail_the_handshake)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	setup(v, NULL);
	send(v, 1, 42);
	tp->relay = true;
	tp->relay_caps = tp->caps;
	tp->relay_caps.plane_flags ^= 0x01u;
	for (int i = 0; i < (int)DLV_UNAUTH_REPEATS - 1; i++) {
		zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
		zassert_false(last_result(42, &r));
	}
	zassert_equal(run(10), CTAG_STATUS_AUTH_FAILED);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_AUTH_FAILED);
	zassert_equal(r.flags, CTAG_RESULT_FLAG_ESCALATED);
	zassert_equal(tp->records, 0u, "no record, no frame, after a failed AUTH");
	zassert_equal(tp->frames_begun + tp->refreshes, 0u);
	/* Without the relay the same tag and key deliver. */
	tp->relay = false;
	send(v, 2, 43);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(43, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
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
	tp->stored_epoch = EPOCH + 1;
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
	tp->caps.width = 296; /* 4.4 Rotation: 400 x 300 does not fit */
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_INVALID);
	zassert_equal(tp->frames_begun, 0);
}

ZTEST(bridge_session, test_caps_of_another_tag)
{
	const struct v_render *v = scenario("rot0_bw_status_card");

	setup(v, NULL);
	send(v, 1, 42);
	tp->caps.tag_id = TAG + 1;
	zassert_equal(run(10), CTAG_STATUS_NOT_FOUND);
	zassert_true(dlv_has_work(&benv.dlv, TAG), "not a tag ERROR: retried later");
}

ZTEST(bridge_session, test_silent_tag_times_out)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	uint32_t t0;

	setup(v, NULL);
	send(v, 1, 42);
	tp->silent = true;
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
	zassert_equal(tp->refreshes, 2);
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
	tp->drop_after = 1;
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
	zassert_mem_equal(tp->rec.digest, v->frame_digest, 32);
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
	zassert_equal(tp->refreshes, 1);
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
	tp->credit0_for_hello = true;
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
	tp->credit0_after_auth = true;
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
	tp->fragment_stream = true;
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
	tp->reply_delay_ms = 3000u;
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
	tp->slow_credit_ms = 4000u;
	zassert_equal(run(10), CTAG_STATUS_TIMEOUT);
	zassert_equal(clock_ms - tp->fb_at,
		      TSESS_FRAME_BASE_MS + kib * TSESS_FRAME_PER_KIB_MS + TSESS_REFRESH_BOUND_MS);
	zassert_true(tp->plane_records < DIV_ROUND_UP(v->plane_len, CTAG_TAG_PLANE_DATA_MAX));
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
	ctag_txn_intent(&tp->rec, TAG, EPOCH, 10, 999, digest);
	ctag_txn_complete(&tp->rec, CTAG_STATUS_OK);
	tp->has_rec = true;
	tp->stored_epoch = EPOCH;
	dlv_tag_cmd(&benv.dlv, &m, 2000);
	send(v, 1, 42);
	zassert_equal(run(10), CTAG_STATUS_OK);
	zassert_equal(tp->records, 2, "CMD and FRAME_BEGIN only, not %u", tp->records);
	zassert_equal(tp->plane_records, 0);
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
	tp->drop_after = 6;
	zassert_equal(run(10), CTAG_STATUS_DISCONNECTED);
	zassert_true(last_result(42, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED);
	zassert_false(dlv_has_work(&benv.dlv, TAG));
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
}

#if CONFIG_CTAG_BRIDGE_SESSIONS > 1

/*
 * Two sessions on one bridge (the nRF52840, docs/protocol.md 5.2): the
 * scheduler initiates a second tag while the first one's link idles (its tag
 * refreshing); from then on both sessions run on the one work queue with
 * their own records, credits, pacing and deadlines, sharing the delivery core
 * (jobs, the one layout buffer) and the font store.
 */
#define TAG2 0x55667788u

static bool b_started;
static uint32_t b_t0;
static uint32_t b_start_after_ms; /* start B this long after A's link went idle */
static uint32_t a_idle_at;

/* The scheduler's rule: B is initiated once A's link idles. */
static bool a_idle(void)
{
	if (!tsess_link_idle(&sessions[0])) {
		return false;
	}
	if (a_idle_at == UINT32_MAX) {
		a_idle_at = clock_ms;
	}
	return clock_ms - a_idle_at >= b_start_after_ms;
}

static void drop_events_of(uint8_t who)
{
	for (size_t i = 0; i < q_n;) {
		if (q[i].who == who) {
			q[i] = q[--q_n];
		} else {
			i++;
		}
	}
}

/*
 * A (tags[0], sessions[0]) from now, B (tags[1], sessions[1]) once a_idle();
 * events and both sessions' timers in time order until both are done.
 */
static void run2(void)
{
	q_n = 0;
	b_started = false;
	a_idle_at = UINT32_MAX;
	for (int k = 0; k < 2; k++) {
		done[k] = false;
		timers[k][0] = timers[k][1] = UINT32_MAX;
		in_event[k] = 0;
	}
	sel(0);
	tag_connect();
	session_t0 = clock_ms;
	tsess_start(&sessions[0], TAG, 10, 40, clock_ms);
	for (int guard = 0; !(done[0] && done[1]); guard++) {
		uint32_t tmin = UINT32_MAX;
		int i, tk = -1, tw = 0;

		zassert_true(guard < 2000000, "sessions never ended");
		if (!b_started && a_idle()) {
			b_started = true;
			b_t0 = clock_ms;
			sel(1);
			tag_connect();
			tsess_start(&sessions[1], TAG2, 20, 40, clock_ms);
			continue;
		}
		zassert_true(b_started || !done[0], "A ended before its link idled");
		for (uint8_t k = 0; k < 2; k++) {
			if (!tags[k].link_up && tsess_active(&sessions[k])) {
				drop_events_of(k);
				tsess_abort(&sessions[k], CTAG_STATUS_DISCONNECTED);
			}
		}
		for (int k = 0; k < 2; k++) {
			for (int w = 0; w < 2; w++) {
				if (timers[k][w] < tmin) {
					tmin = timers[k][w];
					tk = k;
					tw = w;
				}
			}
		}
		i = next_event();
		if (i >= 0 && (q[i].at <= clock_ms || q[i].at <= tmin)) {
			struct ev e = q[i];

			q[i] = q[--q_n];
			clock_ms = MAX(clock_ms, e.at);
			dispatch(&e);
			continue;
		}
		zassert_true(tk >= 0, "stalled: no event, no timer");
		clock_ms = MAX(clock_ms, tmin);
		timers[tk][tw] = UINT32_MAX;
		if (tw == TSESS_T_PACE) {
			in_event[tk] = 0;
		}
		tsess_timeout(&sessions[tk], (uint8_t)tw);
	}
}

static void setup_second(const struct v_render *v)
{
	uint8_t key[16];

	zassert_ok(ctag_session_k_epoch(secret, TAG2, EPOCH, key));
	assign(&benv.dlv, TAG2, EPOCH, key);
	sel(1);
	setup_tag(v);
	tp->id = TAG2;
	tp->caps.tag_id = TAG2;
	tsess_init(&sessions[1], &io, (void *)1, &benv.dlv, &benv.fonts, &test_sha);
	sel(0);
	b_start_after_ms = 0;
}

static void send_to(uint32_t tag_id, const struct v_render *v, uint32_t revision, uint64_t update_id)
{
	struct xfer t = {.xfer_id = (uint16_t)update_id, .tag_id = tag_id, .epoch = EPOCH,
			 .revision = revision, .update_id = update_id};

	zassert_equal(deliver(&benv.dlv, &t, v->layout, v->len, 0, 1000, NULL), CTAG_STATUS_OK);
}

/* B is served during A's refresh; both frames are the reference frames. */
ZTEST(bridge_session, test_two_sessions_side_by_side)
{
	const struct v_render *va = scenario("rot0_bw_status_card");
	const struct v_render *vb = scenario("bwr_flags_3");
	struct ctag_mesh_delivery_result ra, rb;

	setup(va, NULL);
	setup_second(vb);
	send(va, 1, 42);
	send_to(TAG2, vb, 1, 52);
	tags[0].refresh_ms = 4000u;
	tags[1].refresh_ms = 4000u;
	run2();
	zassert_equal(done_status[0], CTAG_STATUS_OK, "A ended %u at %u (B from %u, ended %u at %u)", done_status[0], done_at[0], b_t0, done_status[1], done_at[1]);
	zassert_equal(done_status[1], CTAG_STATUS_OK);
	zassert_true(last_result(42, &ra) && last_result(52, &rb));
	zassert_equal(ra.status, CTAG_STATUS_OK);
	zassert_equal(rb.status, CTAG_STATUS_OK);
	zassert_mem_equal(ra.digest, va->frame_digest, 8);
	zassert_mem_equal(rb.digest, vb->frame_digest, 8);
	zassert_mem_equal(tags[0].rec.digest, va->frame_digest, 32);
	zassert_mem_equal(tags[1].rec.digest, vb->frame_digest, 32);
	/* B connected while A refreshed, and its whole frame went out meanwhile. */
	zassert_true(b_t0 < done_at[0] && done_at[1] - b_t0 < 4000u + 2500u,
		     "B %u..%u, A done %u", b_t0, done_at[1], done_at[0]);
	zassert_equal(rb.suspend_ms, 20u, "each session reports its own suspend window");
	zassert_equal(ra.suspend_ms, 10u);
	zassert_equal(sessions[0].c.layout_reloads, 0u, "A no longer needed its layout");
	zassert_true(max_in_event <= TSESS_RECORDS_PER_EVENT, "%u records in one event",
		     max_in_event);
	zassert_equal(tags[1].plane_records,
		      DIV_ROUND_UP(vb->plane_len, CTAG_TAG_PLANE_DATA_MAX) * vb->planes);
	zassert_false(dlv_has_work(&benv.dlv, TAG) || dlv_has_work(&benv.dlv, TAG2));
	assert_one_seq_per_update();
}

/*
 * Deadlines are per session: A's RESULT bound runs from its own FRAME_END and
 * B's step and handshake bounds from its own connection; neither moves the
 * other's, and a failing session ends alone.
 */
ZTEST(bridge_session, test_two_sessions_deadlines_are_their_own)
{
	const struct v_render *v = scenario("rot0_bw_status_card");
	struct ctag_mesh_delivery_result r;

	/* A's refresh never completes; B delivers meanwhile. */
	setup(v, NULL);
	setup_second(v);
	send(v, 1, 42);
	send_to(TAG2, v, 1, 52);
	tags[0].never_refresh = true;
	run2();
	zassert_equal(done_status[1], CTAG_STATUS_OK);
	zassert_true(last_result(52, &r) && r.status == CTAG_STATUS_OK);
	zassert_true(done_at[1] < done_at[0]);
	zassert_equal(done_status[0], CTAG_STATUS_TIMEOUT);
	zassert_true(done_at[0] - b_t0 <= TSESS_RESULT_TIMEOUT_MS &&
		     done_at[0] - b_t0 + 50u >= TSESS_RESULT_TIMEOUT_MS,
		     "A's RESULT bound from its FRAME_END: %u", done_at[0] - b_t0);
	zassert_false(last_result(42, &r), "a link failure: no result");
	zassert_true(dlv_has_work(&benv.dlv, TAG));

	/* B falls silent; A delivers, B times out on its own bound. */
	setup(v, NULL);
	setup_second(v);
	send(v, 1, 43);
	send_to(TAG2, v, 1, 53);
	tags[0].refresh_ms = 8000u;
	tags[1].silent = true;
	run2();
	zassert_equal(done_status[1], CTAG_STATUS_TIMEOUT);
	zassert_equal(done_at[1] - b_t0, TSESS_STEP_TIMEOUT_MS, "B's own 5 s from its connection");
	zassert_equal(done_status[0], CTAG_STATUS_OK, "A ended %u at %u (B from %u, ended %u at %u)", done_status[0], done_at[0], b_t0, done_status[1], done_at[1]);
	zassert_true(last_result(43, &r) && r.status == CTAG_STATUS_OK);
	zassert_false(last_result(53, &r));
	zassert_true(dlv_has_work(&benv.dlv, TAG2), "B's job stays pending");

	/* B drops its link mid-frame: A is untouched. */
	setup(v, NULL);
	setup_second(v);
	send(v, 1, 44);
	send_to(TAG2, v, 1, 54);
	tags[0].refresh_ms = 4000u;
	tags[1].drop_after = 10;
	run2();
	zassert_equal(done_status[1], CTAG_STATUS_DISCONNECTED);
	zassert_equal(done_status[0], CTAG_STATUS_OK, "A ended %u at %u (B from %u, ended %u at %u)", done_status[0], done_at[0], b_t0, done_status[1], done_at[1]);
	zassert_true(last_result(44, &r) && r.status == CTAG_STATUS_OK);
	zassert_mem_equal(r.digest, v->frame_digest, 8);
	zassert_true(dlv_has_work(&benv.dlv, TAG2));
}

/*
 * Both links stream at once: A's CLEAR refreshes while B starts, then A's
 * layout job streams beside B's two-plane frame. They share the one layout
 * buffer (each reloads its layout when the other took it) and both frames are
 * exact.
 */
ZTEST(bridge_session, test_two_sessions_stream_at_once)
{
	const struct v_render *va = scenario("rot0_bw_status_card");
	const struct v_render *vb = scenario("bwr_flags_3");
	struct ctag_mesh_tag_cmd clear = {.update_id = 77, .tag_id = TAG, .epoch = EPOCH,
					  .cmd = CTAG_TAG_CMD_CLEAR};
	struct ctag_mesh_delivery_result ra, rb, rc;

	setup(va, NULL);
	setup_second(vb);
	dlv_tag_cmd(&benv.dlv, &clear, 2000);
	send(va, 1, 42);
	send_to(TAG2, vb, 1, 52);
	tags[0].refresh_ms = 150u; /* the CLEAR, then A's frame */
	run2();
	zassert_equal(done_status[0], CTAG_STATUS_OK, "A ended %u at %u (B from %u, ended %u at %u)", done_status[0], done_at[0], b_t0, done_status[1], done_at[1]);
	zassert_equal(done_status[1], CTAG_STATUS_OK);
	zassert_true(last_result(77, &rc) && last_result(42, &ra) && last_result(52, &rb));
	zassert_equal(rc.status, CTAG_STATUS_OK);
	zassert_equal(ra.status, CTAG_STATUS_OK);
	zassert_equal(rb.status, CTAG_STATUS_OK);
	zassert_mem_equal(ra.digest, va->frame_digest, 8);
	zassert_mem_equal(rb.digest, vb->frame_digest, 8);
	zassert_mem_equal(tags[0].rec.digest, va->frame_digest, 32);
	zassert_mem_equal(tags[1].rec.digest, vb->frame_digest, 32);
	zassert_true(sessions[0].c.layout_reloads + sessions[1].c.layout_reloads >= 1,
		     "the frames interleaved over the shared layout buffer");
	zassert_true(max_in_event <= TSESS_RECORDS_PER_EVENT);
	assert_one_seq_per_update();
}

#endif /* CONFIG_CTAG_BRIDGE_SESSIONS > 1 */

ZTEST_SUITE(bridge_session, NULL, NULL, NULL, NULL, NULL);
