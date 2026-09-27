/*
 * Tag protocol core (apps/tag/src/tag_core.c) on native_sim, with fake panel,
 * storage, clock and nonce hooks (fakes.c):
 *
 * - tag_core_conv: bridge conversations scripted with the companion's Python
 *   reference (gen_conversation.py -> conversation.h), replayed byte for byte;
 * - tag_core_fixtures: protocol/fixtures session.json (handshake, records,
 *   tampering), tag_txn.json (FRAME_BEGIN table, boot rule) and render.json
 *   (frame digests over the fixture's plane bytes), driven by a C bridge built
 *   on the bridge role of ctag_session;
 * - tag_core_rules: pacing, timeouts, protocol violations, storage and panel
 *   failures.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/ztest.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_crypto.h>
#include <ctag/ctag_frag.h>
#include <ctag/ctag_session.h>
#include <ctag/ctag_txn.h>

#include "conversation.h"
#include "fakes.h"
#include "tag_core.h"
#include "v_render.h"
#include "v_session.h"
#include "v_tag_txn.h"

#define ARRAY_LEN(a) (sizeof(a) / sizeof((a)[0]))

static struct tag_core core;
static struct tag_core_cfg cfg;

/* ---- persisted state ---- */

static void put_record(const struct ctag_txn_record *r)
{
	uint8_t buf[CTAG_TXN_RECORD_LEN];

	ctag_txn_record_encode(r, buf);
	zassert_equal(tag_hal_store_write(TAG_STORE_ID_RECORD, buf, sizeof(buf)), (int)sizeof(buf));
	fake_store.writes[TAG_STORE_ID_RECORD] = 0;
}

static void put_epoch(uint32_t tag_id, uint32_t epoch)
{
	uint8_t e[TAG_EPOCH_ENTRY_LEN];

	ctag_put_le32(e, tag_id);
	ctag_put_le32(&e[4], epoch);
	ctag_put_le32(&e[8], ctag_crc32(0u, e, 8u));
	zassert_equal(tag_hal_store_write(TAG_STORE_ID_EPOCH, e, sizeof(e)), (int)sizeof(e));
	fake_store.writes[TAG_STORE_ID_EPOCH] = 0;
}

static bool get_record(struct ctag_txn_record *r)
{
	const struct fake_entry *e = &fake_store.e[TAG_STORE_ID_RECORD];

	return e->present && ctag_txn_record_decode(r, e->data, e->len) == 0;
}

static uint32_t get_epoch(void)
{
	const struct fake_entry *e = &fake_store.e[TAG_STORE_ID_EPOCH];

	zassert_true(e->present && e->len == TAG_EPOCH_ENTRY_LEN, "no epoch entry");
	zassert_equal(ctag_get_le32(&e->data[8]), ctag_crc32(0u, e->data, 8u));
	return ctag_get_le32(&e->data[4]);
}

static void start(uint32_t tag_id, const uint8_t *secret, uint8_t planes, uint16_t plane_len,
		  uint8_t plane_flags, bool panel_ok)
{
	cfg = (struct tag_core_cfg){
		.tag_id = tag_id,
		.secret = secret,
		.plane_len = plane_len,
		.planes = planes,
		.plane_flags = plane_flags,
		.flags = panel_ok ? TAG_CFG_PANEL_OK : 0u,
	};
	tag_core_init(&core, &cfg);
}

/* ---- scripted conversations (conversation.h) ---- */

static struct ctag_txn_record reboot_record; /* what the boot rule left in NVS */

static void run_conv(const struct conv *cv)
{
	uint8_t out[CTAG_ATT_VALUE_MAX];
	uint8_t chr = 0;
	int n;

	fakes_reset();
	memset(&reboot_record, 0, sizeof(reboot_record));
	if (cv->has_rec) {
		struct ctag_txn_record r = {
			.tag_id = CONV_TAG_ID,
			.epoch = cv->rec_epoch,
			.revision = cv->rec_revision,
			.update_id = cv->rec_update_id,
			.status = cv->rec_status,
			.state = cv->rec_state,
		};

		memcpy(r.digest, cv->rec_digest, sizeof(r.digest));
		put_record(&r);
	}
	if (cv->has_epoch) {
		put_epoch(CONV_TAG_ID, cv->epoch_entry);
	}
	start(CONV_TAG_ID, conv_secret, cv->frame->planes, cv->frame->plane_len,
	      cv->frame->plane_flags, cv->panel_ok);

	for (size_t i = 0; i < cv->count; i++) {
		const struct conv_step *s = &cv->steps[i];

		switch (s->op) {
		case CONV_LINK_UP:
			tag_core_link_up(&core);
			break;
		case CONV_LINK_DOWN:
			tag_core_link_down(&core);
			break;
		case CONV_REBOOT:
			fake_panel.active = false;
			fake_panel.refreshing = false;
			tag_core_init(&core, &cfg);
			zassert_true(get_record(&reboot_record), "%s: no record after reboot", cv->name);
			break;
		case CONV_NONCE:
			fake_set_nonce(s->data, s->len);
			break;
		case CONV_WRITE:
			if (tag_core_rx(&core, s->chr, s->data, s->len)) {
				tag_core_poll(&core);
			}
			break;
		case CONV_WRITE_NOPOLL:
			(void)tag_core_rx(&core, s->chr, s->data, s->len);
			break;
		case CONV_POLL:
			tag_core_poll(&core);
			break;
		case CONV_EXPECT:
			n = tag_core_tx_next(&core, &chr, out);
			zassert_equal(n, s->len, "%s step %zu: sent %d bytes, expected %u", cv->name, i,
				      n, s->len);
			zassert_equal(chr, s->chr, "%s step %zu: characteristic %u, expected %u",
				      cv->name, i, chr, s->chr);
			zassert_mem_equal(out, s->data, s->len, "%s step %zu: value differs", cv->name,
					  i);
			break;
		case CONV_IDLE:
			zassert_equal(tag_core_tx_next(&core, &chr, out), 0, "%s step %zu: not idle",
				      cv->name, i);
			break;
		case CONV_CLOSING:
			zassert_true(tag_core_closing(&core), "%s step %zu: not closing", cv->name, i);
			break;
		case CONV_ADVANCE:
			fake_now += s->arg;
			break;
		case CONV_REFRESH_DONE:
			tag_core_refresh_done(&core, (uint8_t)s->arg);
			tag_core_poll(&core);
			break;
		default:
			zassert_unreachable("bad op %u", s->op);
		}
	}
	zassert_equal(fake_panel.misuse, 0, "%s: panel misuse", cv->name);
}

static void check_staged(const struct conv_frame *f)
{
	for (int p = 0; p < f->planes; p++) {
		zassert_equal(fake_panel.len[p], f->plane_len);
		zassert_mem_equal(fake_panel.staged[p], f->data[p], f->plane_len, "plane %d", p);
	}
}

static void check_record(uint32_t epoch, uint32_t revision, uint64_t update_id,
			 const uint8_t *digest, uint8_t status, uint8_t state)
{
	struct ctag_txn_record r;

	zassert_true(get_record(&r), "no display record");
	zassert_equal(r.tag_id, core.cfg.tag_id);
	zassert_equal(r.epoch, epoch);
	zassert_equal(r.revision, revision);
	zassert_equal(r.update_id, update_id);
	zassert_equal(r.status, status, "status %u", r.status);
	zassert_equal(r.state, state, "state %u", r.state);
	if (digest != NULL) {
		zassert_mem_equal(r.digest, digest, sizeof(r.digest));
	}
}

ZTEST(tag_core_conv, test_happy_path)
{
	run_conv(&conv_happy);
	zassert_equal(fake_panel.begin, 1);
	zassert_equal(fake_panel.commit, 1);
	zassert_equal(fake_panel.sleep, 1);
	zassert_equal(fake_panel.abort, 0);
	check_staged(&conv_frame_thick_lines);
	check_record(1, 1, 100, conv_frame_thick_lines.digest, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
	/* REFRESH_INTENT, then DISPLAYED: two writes of the record. */
	zassert_equal(fake_store.writes[TAG_STORE_ID_RECORD], 2);
	zassert_equal(get_epoch(), 1);
	zassert_equal(core.result_pending, 0);
}

ZTEST(tag_core_conv, test_two_planes)
{
	run_conv(&conv_two_planes);
	check_staged(&conv_frame_glyph_bearings);
	check_record(1, 7, 70, conv_frame_glyph_bearings.digest, CTAG_STATUS_OK,
		     CTAG_TXN_DISPLAYED);
}

ZTEST(tag_core_conv, test_duplicate_returns_stored_ack)
{
	run_conv(&conv_duplicate);
	zassert_equal(fake_panel.begin, 0, "a duplicate never touches the panel");
	zassert_equal(fake_store.writes[TAG_STORE_ID_RECORD], 0);
}

ZTEST(tag_core_conv, test_stale_revision)
{
	run_conv(&conv_stale_revision);
	zassert_equal(fake_panel.begin, 0);
	check_record(1, 5, 100, NULL, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
}

ZTEST(tag_core_conv, test_revision_conflict)
{
	run_conv(&conv_revision_conflict);
	zassert_equal(fake_panel.begin, 0);
}

ZTEST(tag_core_conv, test_digest_mismatch_never_refreshes)
{
	run_conv(&conv_digest_mismatch);
	zassert_equal(fake_panel.begin, 1);
	zassert_equal(fake_panel.commit, 0, "a mismatching frame must not refresh");
	zassert_equal(fake_panel.abort, 1);
	zassert_false(fake_store.e[TAG_STORE_ID_RECORD].present, "no intent persisted");
}

ZTEST(tag_core_conv, test_auth_failure)
{
	run_conv(&conv_auth_failed);
	zassert_equal(core.auth_failures, 1);
	zassert_false(fake_store.e[TAG_STORE_ID_EPOCH].present, "epoch stored without AUTH");
	zassert_equal(core.s.state, CTAG_SESSION_FAILED);
}

ZTEST(tag_core_conv, test_disconnect_restarts_from_zero)
{
	run_conv(&conv_restart_from_zero);
	zassert_equal(fake_panel.abort, 1, "the disconnect aborts the frame");
	zassert_equal(fake_panel.begin, 2);
	zassert_equal(fake_panel.commit, 1);
	check_staged(&conv_frame_thick_lines);
	check_record(1, 3, 105, conv_frame_thick_lines.digest, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
}

ZTEST(tag_core_conv, test_power_loss_recovery)
{
	run_conv(&conv_power_loss_recovery);
	/* 6: after the reset the intent reads DISPLAY_STATE_UNKNOWN, persisted. */
	zassert_equal(reboot_record.state, CTAG_TXN_REFRESH_INTENT);
	zassert_equal(reboot_record.status, CTAG_STATUS_DISPLAY_STATE_UNKNOWN);
	zassert_equal(reboot_record.revision, 4);
	zassert_equal(fake_panel.commit, 2, "the re-delivery refreshes again");
	check_record(1, 4, 107, conv_frame_thick_lines.digest, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
}

ZTEST(tag_core_conv, test_epoch_persisted_only_after_auth)
{
	run_conv(&conv_epoch_after_auth);
	zassert_equal(get_epoch(), 4);
	zassert_equal(fake_store.writes[TAG_STORE_ID_EPOCH], 1, "one write, after the good AUTH");
}

ZTEST(tag_core_conv, test_identify_refresh_sleep_unsupported)
{
	run_conv(&conv_unsupported_commands);
	zassert_equal(fake_panel.begin, 0);
	zassert_equal(core.sleep, 0);
}

ZTEST(tag_core_conv, test_clear)
{
	static uint8_t white[2][480];
	uint8_t digest[32];
	ctag_sha256_ctx sha;

	run_conv(&conv_clear);
	/* plane_flags 3: plane 0 bit 1 = white, plane 1 bit 1 = red. */
	memset(white[0], 0xFF, sizeof(white[0]));
	memset(white[1], 0x00, sizeof(white[1]));
	zassert_mem_equal(fake_panel.staged[0], white[0], 480);
	zassert_mem_equal(fake_panel.staged[1], white[1], 480);
	zassert_ok(ctag_crypto_sha256_init(&sha));
	zassert_ok(ctag_crypto_sha256_update(&sha, white[0], 480));
	zassert_ok(ctag_crypto_sha256_update(&sha, white[1], 480));
	zassert_ok(ctag_crypto_sha256_finish(&sha, digest));
	/* The later FRAME_BEGIN rev 1 was accepted: the frame is being received. */
	zassert_equal(core.frame, TAG_FRAME_RECEIVING);
	check_record(2, 0, 11, digest, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
}

ZTEST(tag_core_conv, test_unverified_panel_refuses_frames)
{
	run_conv(&conv_unverified_panel);
	zassert_equal(fake_panel.begin + fake_panel.write + fake_panel.commit + fake_panel.abort +
			      fake_panel.sleep,
		      0, "panel id 255 must never be driven");
}

ZTEST(tag_core_conv, test_refresh_timeout_keeps_intent)
{
	run_conv(&conv_refresh_timeout);
	check_record(1, 6, 108, NULL, CTAG_STATUS_REFRESH_TIMEOUT, CTAG_TXN_REFRESH_INTENT);
	zassert_equal(fake_panel.sleep, 1);
}

/* ---- a bridge in C (ctag_session bridge role) for table-driven tests ---- */

static struct {
	struct ctag_session s;
	struct ctag_frag_tx ctrl_tx, data_tx;
	struct ctag_frag_rx ctrl_rx, status_rx;
	uint8_t ctrl_buf[CTAG_TAG_CTRL_MSG_MAX];
	uint8_t status_buf[CTAG_TAG_RECORD_WIRE_MAX];
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
} br;

static void link_up(void)
{
	memset(&br, 0, sizeof(br));
	ctag_frag_tx_init(&br.ctrl_tx);
	ctag_frag_tx_init(&br.data_tx);
	ctag_frag_rx_init(&br.ctrl_rx, br.ctrl_buf, sizeof(br.ctrl_buf));
	ctag_frag_rx_init(&br.status_rx, br.status_buf, sizeof(br.status_buf));
	tag_core_link_up(&core);
}

/* Fragment msg onto CTRL or DATA; poll the tag when a message completes. */
static void send_msg(uint8_t chr, const uint8_t *msg, size_t len, bool poll)
{
	uint8_t v[CTAG_ATT_VALUE_MAX];
	size_t off = 0;

	while (off < len) {
		int n = ctag_frag_next(chr == TAG_CHR_CTRL ? &br.ctrl_tx : &br.data_tx, msg, len, &off,
				       CTAG_FRAG_PAYLOAD_MAX, v);

		zassert_true(n > 0);
		if (tag_core_rx(&core, chr, v, (size_t)n) && poll) {
			tag_core_poll(&core);
		}
	}
}

static void send_record(uint8_t type, const uint8_t *pt, size_t len, bool poll)
{
	int n = ctag_record_seal(&br.s.tx, type, pt, len, br.rec, sizeof(br.rec));

	zassert_true(n > 0);
	send_msg(TAG_CHR_DATA, br.rec, (size_t)n, poll);
}

/* The tag's next whole message: its characteristic, or -1 when it sent none. */
static int recv_msg(const uint8_t **msg, size_t *len)
{
	uint8_t v[CTAG_ATT_VALUE_MAX];
	uint8_t chr;
	int n;

	while ((n = tag_core_tx_next(&core, &chr, v)) > 0) {
		struct ctag_frag_rx *rx = chr == TAG_CHR_CTRL ? &br.ctrl_rx : &br.status_rx;
		int m = ctag_frag_rx_put(rx, v, (size_t)n);

		zassert_true(m >= 0, "the tag broke the fragmentation rules");
		if (m > 0) {
			*msg = rx->buf;
			*len = (size_t)m;
			return chr;
		}
	}
	return -1;
}

static void expect_ctrl_error(uint8_t status)
{
	const uint8_t *m;
	size_t len;

	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	zassert_equal(len, 2u);
	zassert_equal(m[0], CTAG_CTRL_ERROR);
	zassert_equal(m[1], status, "ERROR{%u}, expected %u", m[1], status);
	zassert_true(tag_core_closing(&core));
}

static void expect_credit(uint8_t credits)
{
	const uint8_t *m;
	size_t len;

	zassert_equal(recv_msg(&m, &len), TAG_CHR_STATUS);
	zassert_equal(len, 2u);
	zassert_equal(m[0], CTAG_PLAIN_CREDIT, "0x%02x instead of CREDIT", m[0]);
	zassert_equal(m[1], credits);
}

/* The next STATUS message must be a record of type; its plaintext in pt. */
static size_t expect_record(uint8_t type, uint8_t *pt)
{
	const uint8_t *m;
	size_t len;
	uint8_t got;
	int n;

	zassert_equal(recv_msg(&m, &len), TAG_CHR_STATUS);
	zassert_not_equal(m[0], CTAG_PLAIN_CREDIT, "CREDIT instead of a record");
	n = ctag_record_open(&br.s.rx, m, len, &got, pt, CTAG_TAG_RECORD_PAYLOAD_MAX);
	zassert_true(n >= 0, "the tag's record does not authenticate");
	zassert_equal(got, type);
	return (size_t)n;
}

static void expect_result(struct ctag_rec_result *r)
{
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	size_t n = expect_record(CTAG_REC_RESULT, pt);

	zassert_ok(ctag_rec_result_unpack(r, pt, n));
}

static uint8_t handshake(uint32_t epoch, struct ctag_ctrl_challenge *seen)
{
	static const uint8_t nonce_b[CTAG_TAG_NONCE_LEN] = {0xB1, 0xB2, 0xB3};
	uint8_t k[CTAG_TAG_KEY_LEN];
	uint8_t hello[CTAG_SESSION_HELLO_LEN];
	uint8_t auth[CTAG_SESSION_AUTH_LEN];
	struct ctag_ctrl_challenge ch;
	const uint8_t *m;
	size_t len;
	uint8_t st;

	zassert_ok(ctag_session_k_epoch(core.cfg.secret, core.cfg.tag_id, epoch, k));
	zassert_ok(ctag_session_bridge_hello(&br.s, core.cfg.tag_id, epoch, k, nonce_b, hello));
	send_msg(TAG_CHR_CTRL, hello, sizeof(hello), true);
	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	st = ctag_session_bridge_challenge(&br.s, m, len, seen != NULL ? seen : &ch, auth);
	if (st != CTAG_STATUS_OK) {
		return st;
	}
	send_msg(TAG_CHR_CTRL, auth, sizeof(auth), true);
	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	st = ctag_session_bridge_auth_ok(&br.s, m, len);
	if (st == CTAG_STATUS_OK) {
		expect_credit(TAG_CREDITS);
	}
	return st;
}

static void frame_begin(uint32_t revision, uint64_t update_id, const uint8_t *digest,
			uint8_t planes, uint16_t plane_len)
{
	struct ctag_rec_frame_begin fb = {.revision = revision,
					  .update_id = update_id,
					  .planes = planes,
					  .plane_len = plane_len};
	uint8_t pt[CTAG_REC_FRAME_BEGIN_LEN];

	memcpy(fb.digest, digest, sizeof(fb.digest));
	(void)ctag_rec_frame_begin_pack(&fb, pt, sizeof(pt));
	send_record(CTAG_REC_FRAME_BEGIN, pt, sizeof(pt), true);
}

/* PLANE_DATA for every plane, each answered by CREDIT{1}. */
static void send_planes(const uint8_t *const *planes, uint8_t count, uint16_t plane_len)
{
	uint8_t pt[CTAG_REC_PLANE_DATA_MAX_LEN];

	for (uint8_t p = 0; p < count; p++) {
		for (uint16_t off = 0; off < plane_len; off = (uint16_t)(off + CTAG_TAG_PLANE_DATA_MAX)) {
			struct ctag_rec_plane_data pd = {.plane = p, .offset = off, .data = &planes[p][off]};
			int n;

			pd.data_len = MIN(CTAG_TAG_PLANE_DATA_MAX, (size_t)(plane_len - off));
			n = ctag_rec_plane_data_pack(&pd, pt, sizeof(pt));
			zassert_true(n > 0);
			send_record(CTAG_REC_PLANE_DATA, pt, (size_t)n, true);
			expect_credit(1);
		}
	}
}

static void send_empty(uint8_t type)
{
	send_record(type, NULL, 0, true);
}

/* ---- fixtures ---- */

static void session_fixture_setup(void)
{
	struct ctag_txn_record r = {
		.tag_id = V_SESSION_TAG_ID,
		.epoch = 2,
		.revision = 17,
		.update_id = 400,
		.status = CTAG_STATUS_OK,
		.state = CTAG_TXN_DISPLAYED,
	};

	/* The fixture CHALLENGE reports stored_epoch 2, displayed_rev 17, OK,
	 * battery 2950, flags 0, with the fixture nonce_t. */
	fakes_reset();
	put_record(&r);
	put_epoch(V_SESSION_TAG_ID, 2);
	start(V_SESSION_TAG_ID, V_SESSION_TAG_SECRET, 1, 15000, 0x01, true);
	link_up();
}

static void feed_ctrl(const uint8_t *msg, size_t len)
{
	send_msg(TAG_CHR_CTRL, msg, len, true);
}

static void fixture_handshake(void)
{
	const uint8_t *m;
	size_t len;

	fake_set_nonce(V_SESSION_NONCE_T, V_SESSION_NONCE_T_LEN);
	feed_ctrl(V_SESSION_HELLO, V_SESSION_HELLO_LEN);
	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	zassert_equal(len, V_SESSION_CHALLENGE_LEN);
	zassert_mem_equal(m, V_SESSION_CHALLENGE, len, "CHALLENGE differs from session.json");
	feed_ctrl(V_SESSION_AUTH, V_SESSION_AUTH_LEN);
	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	zassert_equal(len, V_SESSION_AUTH_OK_LEN);
	zassert_mem_equal(m, V_SESSION_AUTH_OK, len, "AUTH_OK differs from session.json");
	expect_credit(TAG_CREDITS);
	/* The bridge side of the fixture: records with k_b2t, answers with k_t2b. */
	memcpy(br.s.rx.key, V_SESSION_K_T2B, 16);
	br.s.rx.dir = CTAG_REC_DIR_T2B;
	br.s.rx.counter = 0;
}

ZTEST(tag_core_fixtures, test_session_handshake_and_records)
{
	struct ctag_rec_result res;
	int records = 0;

	session_fixture_setup();
	fixture_handshake();
	zassert_equal(get_epoch(), V_SESSION_EPOCH, "epoch 3 > 2 persisted after AUTH");

	/* The fixture's B2T records verbatim: FRAME_BEGIN, 2 x PLANE_DATA, FRAME_END. */
	for (size_t i = 0; i < ARRAY_LEN(v_session_records); i++) {
		const struct v_record *v = &v_session_records[i];

		if (v->dir != CTAG_REC_DIR_B2T) {
			continue;
		}
		send_msg(TAG_CHR_DATA, v->record, v->len, true);
		records++;
		if (v->type != CTAG_REC_FRAME_END) {
			expect_credit(1);
		}
	}
	zassert_equal(records, 4);
	zassert_equal(fake_panel.begin, 1);
	zassert_mem_equal(fake_panel.staged[0], &v_session_records[1].plaintext[3], 189);
	/* 200 of 15000 bytes: INCOMPLETE, sealed with the fixture's k_t2b. */
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_INCOMPLETE);
	zassert_equal(res.update_id, 501);
	zassert_equal(res.epoch, V_SESSION_EPOCH);
	zassert_equal(res.revision, 18);
	zassert_equal(res.battery_mv, 2950);
	expect_credit(1);
	zassert_equal(fake_panel.commit, 0);
	zassert_equal(fake_panel.abort, 1);
}

ZTEST(tag_core_fixtures, test_session_tampered_records)
{
	int tested = 0;

	for (size_t i = 0; i < ARRAY_LEN(v_session_tampered); i++) {
		const struct v_record *v = &v_session_tampered[i];

		/* The tag opens B2T records only; T2B cases are the bridge's (its
		 * "wrong direction key" record is a valid B2T record for the tag). */
		if (v->dir != CTAG_REC_DIR_B2T) {
			continue;
		}
		tested++;
		session_fixture_setup();
		fixture_handshake();
		/* Bring the receive counter to the case's expected value. */
		for (size_t j = 0; j < ARRAY_LEN(v_session_records) && core.s.rx.counter < v->counter;
		     j++) {
			if (v_session_records[j].dir == CTAG_REC_DIR_B2T) {
				send_msg(TAG_CHR_DATA, v_session_records[j].record,
					 v_session_records[j].len, true);
			}
		}
		while (tag_core_tx_next(&core, &(uint8_t){0}, (uint8_t[CTAG_ATT_VALUE_MAX]){0}) > 0) {
		}
		zassert_equal(core.s.rx.counter, v->counter, "%s", v->name);
		send_msg(TAG_CHR_DATA, v->record, v->len, true);
		expect_ctrl_error(CTAG_STATUS_AUTH_FAILED);
	}
	zassert_true(tested >= 5, "B2T tampered records: %d", tested);
}

ZTEST(tag_core_fixtures, test_session_bad_mac_b)
{
	uint8_t auth[CTAG_SESSION_AUTH_LEN] = {CTAG_CTRL_AUTH};
	const uint8_t *m;
	size_t len;

	session_fixture_setup();
	fake_set_nonce(V_SESSION_NONCE_T, V_SESSION_NONCE_T_LEN);
	feed_ctrl(V_SESSION_HELLO, V_SESSION_HELLO_LEN);
	zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
	memcpy(&auth[1], V_SESSION_BAD_MAC_B, CTAG_TAG_MAC_LEN);
	feed_ctrl(auth, sizeof(auth));
	expect_ctrl_error(CTAG_STATUS_AUTH_FAILED);
	zassert_equal(get_epoch(), 2, "a failed AUTH never stores its epoch");
}

static void txn_store(const struct v_txn_rec *v)
{
	struct ctag_txn_record r = {
		.tag_id = v->tag_id,
		.epoch = v->epoch,
		.revision = v->revision,
		.update_id = v->update_id,
		.status = (uint8_t)v->status,
		.state = (uint8_t)v->state,
	};

	memcpy(r.digest, v->digest, sizeof(r.digest));
	put_record(&r);
	put_epoch(v->tag_id, v->epoch);
}

ZTEST(tag_core_fixtures, test_txn_frame_begin_table)
{
	for (size_t i = 0; i < ARRAY_LEN(v_txn_frame_begin); i++) {
		const struct v_txn_frame_begin *v = &v_txn_frame_begin[i];
		const uint8_t *m;
		size_t len;
		uint8_t st;

		fakes_reset();
		if (v->stored.present) {
			txn_store(&v->stored);
		}
		start(V_SESSION_TAG_ID, V_SESSION_TAG_SECRET, V_TXN_PANEL_PLANES,
		      V_TXN_PANEL_PLANE_LEN, 0x01, true);
		link_up();
		st = handshake(v->epoch, NULL);
		if (v->stored.present && v->epoch < v->stored.epoch) {
			/* The handshake refuses an older epoch before any record. */
			zassert_equal(st, CTAG_STATUS_STALE_EPOCH, "%s", v->name);
			continue;
		}
		zassert_equal(st, CTAG_STATUS_OK, "%s", v->name);
		frame_begin(v->revision, 4242, v->digest, v->planes, v->plane_len);
		if (v->accept) {
			expect_credit(1);
			zassert_equal(recv_msg(&m, &len), -1, "%s: no RESULT on accept", v->name);
			zassert_equal(core.frame, TAG_FRAME_RECEIVING, "%s", v->name);
			zassert_equal(fake_panel.begin, 1, "%s", v->name);
		} else {
			struct ctag_rec_result res;

			expect_result(&res);
			zassert_equal(res.status, v->status, "%s: status %u", v->name, res.status);
			zassert_equal(res.flags, v->duplicate ? 1u : 0u, "%s", v->name);
			zassert_equal(res.update_id, v->duplicate ? v->stored.update_id : 4242u, "%s",
				      v->name);
			expect_credit(1);
			zassert_equal(fake_panel.begin, 0, "%s", v->name);
		}
	}
}

ZTEST(tag_core_fixtures, test_txn_boot_rule)
{
	for (size_t i = 0; i < ARRAY_LEN(v_txn_boot); i++) {
		const struct v_txn_boot *v = &v_txn_boot[i];
		struct ctag_ctrl_challenge seen;
		struct ctag_txn_record r;

		fakes_reset();
		if (v->stored.present) {
			txn_store(&v->stored);
		}
		start(V_SESSION_TAG_ID, V_SESSION_TAG_SECRET, 1, 15000, 0x01, true);
		zassert_equal(fake_store.writes[TAG_STORE_ID_RECORD], v->persist ? 1 : 0, "%s",
			      v->name);
		if (v->expect.present) {
			zassert_true(get_record(&r), "%s", v->name);
			zassert_equal(r.status, v->expect.status, "%s", v->name);
			zassert_equal(r.state, v->expect.state, "%s", v->name);
		}
		link_up();
		zassert_equal(handshake(v->stored.present ? v->stored.epoch : 1, &seen), CTAG_STATUS_OK,
			      "%s", v->name);
		zassert_equal(seen.flags & 0x01u, v->flag ? 1u : 0u, "%s", v->name);
		zassert_equal(seen.last_status, v->expect.present ? v->expect.status : 0, "%s",
			      v->name);
	}
}

ZTEST(tag_core_fixtures, test_render_frame_digests)
{
	int tested = 0;

	for (size_t i = 0; i < ARRAY_LEN(v_render); i++) {
		const struct v_render *v = &v_render[i];
		struct ctag_rec_result res;
		uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];

		if (v->plane_bytes[0] == NULL || v->plane_len > FAKE_PLANE_MAX) {
			continue;
		}
		fakes_reset();
		start(0x01020304u, conv_secret, v->planes, (uint16_t)v->plane_len, v->plane_flags,
		      true);
		link_up();
		zassert_equal(handshake(1, NULL), CTAG_STATUS_OK);
		frame_begin(1, 77, v->frame_digest, v->planes, (uint16_t)v->plane_len);
		expect_credit(1);
		send_planes(v->plane_bytes, v->planes, (uint16_t)v->plane_len);
		send_empty(CTAG_REC_FRAME_END);
		zassert_equal(expect_record(CTAG_REC_PROGRESS, pt), 1u);
		zassert_equal(pt[0], CTAG_STAGE_REFRESHING);
		fake_now += 1234u;
		tag_core_refresh_done(&core, CTAG_STATUS_OK);
		expect_result(&res);
		zassert_equal(res.status, CTAG_STATUS_OK, "%s", v->name);
		zassert_mem_equal(res.digest, v->frame_digest, 8);
		zassert_equal(res.refresh_ms, 1234);
		expect_credit(1);
		for (int p = 0; p < v->planes; p++) {
			zassert_mem_equal(fake_panel.staged[p], v->plane_bytes[p], v->plane_len, "%s",
					  v->name);
		}
		tested++;
	}
	zassert_true(tested >= 5, "render.json frames with plane bytes: %d", tested);
}

/* ---- rules ---- */

static const uint8_t frame_planes[1][384] = {{0x5A, 0xA5}};

static void rules_setup(void)
{
	fakes_reset();
	start(0x0A0B0C0Du, conv_secret, 1, 384, 0x01, true);
	link_up();
}

static void rules_established(void)
{
	rules_setup();
	zassert_equal(handshake(1, NULL), CTAG_STATUS_OK);
}

static void digest_of(const uint8_t *data, size_t len, uint8_t digest[32])
{
	ctag_sha256_ctx sha;

	zassert_ok(ctag_crypto_sha256_init(&sha));
	zassert_ok(ctag_crypto_sha256_update(&sha, data, len));
	zassert_ok(ctag_crypto_sha256_finish(&sha, digest));
}

ZTEST(tag_core_rules, test_three_auth_failures_skip_a_window)
{
	rules_setup();
	for (int i = 0; i < 3; i++) {
		uint8_t k[CTAG_TAG_KEY_LEN] = {0}; /* the wrong K_epoch */
		uint8_t hello[CTAG_SESSION_HELLO_LEN];
		uint8_t auth[CTAG_SESSION_AUTH_LEN];
		struct ctag_ctrl_challenge ch;
		const uint8_t *m;
		size_t len;

		zassert_false(tag_core_take_skip(&core));
		link_up();
		zassert_ok(ctag_session_bridge_hello(&br.s, core.cfg.tag_id, 1, k,
						     (const uint8_t[16]){1}, hello));
		send_msg(TAG_CHR_CTRL, hello, sizeof(hello), true);
		zassert_equal(recv_msg(&m, &len), TAG_CHR_CTRL);
		zassert_equal(ctag_session_bridge_challenge(&br.s, m, len, &ch, auth), 0);
		send_msg(TAG_CHR_CTRL, auth, sizeof(auth), true);
		expect_ctrl_error(CTAG_STATUS_AUTH_FAILED);
		tag_core_link_down(&core);
	}
	zassert_true(tag_core_take_skip(&core), "5.4: skip the next wake window");
	zassert_false(tag_core_take_skip(&core), "only one window");
	/* A good AUTH resets the count. */
	link_up();
	zassert_equal(handshake(1, NULL), CTAG_STATUS_OK);
	zassert_equal(core.auth_failures, 0);
}

ZTEST(tag_core_rules, test_session_timeout)
{
	uint8_t d[32];

	rules_established();
	tag_core_timeout(&core);
	zassert_true(tag_core_closing(&core));
	zassert_equal(recv_msg(&(const uint8_t *){NULL}, &(size_t){0}), -1, "silent close");

	/* Never while a refresh runs (5.1: connected through the refresh). */
	rules_established();
	digest_of(frame_planes[0], 384, d);
	frame_begin(1, 5, d, 1, 384);
	expect_credit(1);
	send_planes((const uint8_t *const[]){frame_planes[0]}, 1, 384);
	send_empty(CTAG_REC_FRAME_END);
	(void)expect_record(CTAG_REC_PROGRESS, (uint8_t[CTAG_TAG_RECORD_PAYLOAD_MAX]){0});
	zassert_true(tag_core_refreshing(&core));
	tag_core_timeout(&core);
	zassert_false(tag_core_closing(&core));
}

ZTEST(tag_core_rules, test_fragment_violation_closes_silently)
{
	uint8_t v[4] = {0x05, 1, 2, 3}; /* SEQ 5 without START: a gap */

	rules_established();
	zassert_true(tag_core_rx(&core, TAG_CHR_DATA, v, sizeof(v)));
	tag_core_poll(&core);
	zassert_true(tag_core_closing(&core));
	zassert_equal(recv_msg(&(const uint8_t *){NULL}, &(size_t){0}), -1);
	zassert_false(tag_core_rx(&core, TAG_CHR_DATA, v, sizeof(v)), "input refused when closing");
}

ZTEST(tag_core_rules, test_record_without_credit)
{
	uint8_t cmd[CTAG_REC_CMD_LEN] = {CTAG_TAG_CMD_IDENTIFY};

	rules_established();
	/* Three records without letting the tag answer: the third finds no buffer. */
	send_record(CTAG_REC_CMD, cmd, sizeof(cmd), false);
	send_record(CTAG_REC_CMD, cmd, sizeof(cmd), false);
	send_record(CTAG_REC_CMD, cmd, sizeof(cmd), false);
	tag_core_poll(&core);
	expect_ctrl_error(CTAG_STATUS_INVALID);
}

ZTEST(tag_core_rules, test_ctrl_after_established)
{
	uint8_t hello[CTAG_SESSION_HELLO_LEN] = {CTAG_CTRL_HELLO};

	rules_established();
	send_msg(TAG_CHR_CTRL, hello, sizeof(hello), true);
	expect_ctrl_error(CTAG_STATUS_INVALID);
}

ZTEST(tag_core_rules, test_data_before_auth)
{
	uint8_t rec[20] = {CTAG_REC_FRAME_END};

	rules_setup();
	send_msg(TAG_CHR_DATA, rec, sizeof(rec), true);
	expect_ctrl_error(CTAG_STATUS_INVALID);
}

ZTEST(tag_core_rules, test_malformed_record_plaintext)
{
	uint8_t bad[3] = {1, 2, 3}; /* FRAME_BEGIN is 47 bytes */

	rules_established();
	send_record(CTAG_REC_FRAME_BEGIN, bad, sizeof(bad), true);
	expect_ctrl_error(CTAG_STATUS_INVALID);
}

ZTEST(tag_core_rules, test_corrupt_record_reports_storage_error)
{
	struct ctag_ctrl_challenge seen;
	uint8_t d[32];

	fakes_reset();
	fake_store.e[TAG_STORE_ID_RECORD] = (struct fake_entry){.present = true, .len = 60};
	start(0x0A0B0C0Du, conv_secret, 1, 384, 0x01, true);
	zassert_equal(core.rec_state, TAG_REC_CORRUPT);
	link_up();
	zassert_equal(handshake(1, &seen), CTAG_STATUS_OK);
	zassert_equal(seen.last_status, CTAG_STATUS_STORAGE_ERROR);
	zassert_equal(seen.displayed_rev, 0);
	/* As if there were no record: any revision is accepted. */
	digest_of(frame_planes[0], 384, d);
	frame_begin(1, 5, d, 1, 384);
	expect_credit(1);
	zassert_equal(core.frame, TAG_FRAME_RECEIVING);
}

ZTEST(tag_core_rules, test_intent_write_failure)
{
	struct ctag_rec_result res;
	uint8_t d[32];

	rules_established();
	digest_of(frame_planes[0], 384, d);
	frame_begin(1, 5, d, 1, 384);
	expect_credit(1);
	send_planes((const uint8_t *const[]){frame_planes[0]}, 1, 384);
	fake_store.fail_write = true;
	send_empty(CTAG_REC_FRAME_END);
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_STORAGE_ERROR);
	expect_credit(1);
	zassert_equal(fake_panel.commit, 0, "no refresh without a durable intent");
}

ZTEST(tag_core_rules, test_epoch_write_failure)
{
	rules_setup();
	fake_store.fail_write = true;
	zassert_equal(handshake(1, NULL), CTAG_STATUS_STORAGE_ERROR);
	zassert_true(tag_core_closing(&core));
}

ZTEST(tag_core_rules, test_panel_failures)
{
	struct ctag_rec_result res;
	uint8_t d[32];

	rules_established();
	digest_of(frame_planes[0], 384, d);
	fake_panel.fail_begin = true;
	frame_begin(1, 5, d, 1, 384);
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_PANEL_ERROR);
	expect_credit(1);

	fake_panel.fail_begin = false;
	fake_panel.fail_commit = true;
	frame_begin(1, 6, d, 1, 384);
	expect_credit(1);
	send_planes((const uint8_t *const[]){frame_planes[0]}, 1, 384);
	send_empty(CTAG_REC_FRAME_END);
	(void)expect_record(CTAG_REC_PROGRESS, (uint8_t[CTAG_TAG_RECORD_PAYLOAD_MAX]){0});
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_PANEL_ERROR);
	expect_credit(1);
	/* REFRESH_INTENT stays: the screen state is unknown. */
	check_record(1, 1, 6, NULL, CTAG_STATUS_PANEL_ERROR, CTAG_TXN_REFRESH_INTENT);
}

ZTEST(tag_core_rules, test_frame_abort_and_out_of_order_data)
{
	struct ctag_rec_result res;
	uint8_t abort_pt[CTAG_REC_FRAME_ABORT_LEN] = {CTAG_STATUS_CANCELLED};
	uint8_t pt[CTAG_REC_PLANE_DATA_LEN + 4] = {0, 4, 0}; /* plane 0, offset 4 */
	uint8_t d[32];

	rules_established();
	digest_of(frame_planes[0], 384, d);
	frame_begin(1, 5, d, 1, 384);
	expect_credit(1);
	send_record(CTAG_REC_FRAME_ABORT, abort_pt, sizeof(abort_pt), true);
	expect_credit(1);
	zassert_equal(fake_panel.abort, 1);
	zassert_equal(core.frame, TAG_FRAME_IDLE);

	/* PLANE_DATA must continue at the bytes already received. */
	frame_begin(1, 6, d, 1, 384);
	expect_credit(1);
	send_record(CTAG_REC_PLANE_DATA, pt, sizeof(pt), true);
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_INVALID);
	expect_credit(1);

	/* FRAME_END before every byte arrived. */
	frame_begin(1, 7, d, 1, 384);
	expect_credit(1);
	send_empty(CTAG_REC_FRAME_END);
	expect_result(&res);
	zassert_equal(res.status, CTAG_STATUS_INCOMPLETE);
	expect_credit(1);
	zassert_equal(fake_panel.commit, 0);
}

ZTEST(tag_core_rules, test_disconnect_during_refresh_completes)
{
	uint8_t d[32];

	rules_established();
	digest_of(frame_planes[0], 384, d);
	frame_begin(1, 5, d, 1, 384);
	expect_credit(1);
	send_planes((const uint8_t *const[]){frame_planes[0]}, 1, 384);
	send_empty(CTAG_REC_FRAME_END);
	(void)expect_record(CTAG_REC_PROGRESS, (uint8_t[CTAG_TAG_RECORD_PAYLOAD_MAX]){0});
	tag_core_link_down(&core);
	zassert_equal(fake_panel.abort, 0, "a running refresh is never aborted");
	tag_core_refresh_done(&core, CTAG_STATUS_OK);
	check_record(1, 1, 5, d, CTAG_STATUS_OK, CTAG_TXN_DISPLAYED);
	/* The ACK was not delivered: advertise "result pending". */
	zassert_equal(tag_core_adv_flags(&core, 3000), TAG_ADV_RESULT_PENDING);
}

ZTEST(tag_core_rules, test_advertising_flags)
{
	rules_setup();
	zassert_equal(tag_core_adv_flags(&core, 0), 0, "0 mV = unknown, never low");
	zassert_equal(tag_core_adv_flags(&core, 2300), TAG_ADV_LOW_BATTERY);
	core.rec_state = TAG_REC_VALID;
	core.rec.state = CTAG_TXN_REFRESH_INTENT;
	core.rec.revision = 0x12345;
	zassert_equal(tag_core_adv_flags(&core, 3000), TAG_ADV_STATE_UNKNOWN);
	zassert_equal(tag_core_disp_rev(&core), 0x2345);
}

static void *suite_setup(void)
{
	zassert_ok(ctag_crypto_init());
	return NULL;
}

ZTEST_SUITE(tag_core_conv, NULL, suite_setup, NULL, NULL, NULL);
ZTEST_SUITE(tag_core_fixtures, NULL, suite_setup, NULL, NULL, NULL);
ZTEST_SUITE(tag_core_rules, NULL, suite_setup, NULL, NULL, NULL);
