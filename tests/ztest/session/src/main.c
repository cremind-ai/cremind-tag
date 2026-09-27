/*
 * ctag_session with the PSA backend against protocol/fixtures/session.json:
 * K_epoch, the handshake from both roles, record seal/open, and the tamper,
 * replay and handshake failure cases.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/ztest.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_session.h>

#include "v_session.h"

static struct ctag_session bridge;
static struct ctag_session tag;

/* CHALLENGE fields the tag app supplies, taken from the fixture message. */
static void challenge_fields(struct ctag_ctrl_challenge *ch)
{
	zassert_ok(ctag_ctrl_challenge_unpack(ch, &V_SESSION_CHALLENGE[1],
					      V_SESSION_CHALLENGE_LEN - 1u));
	zassert_mem_equal(ch->nonce_t, V_SESSION_NONCE_T, CTAG_TAG_NONCE_LEN);
}

/* The tag role with the fixture tag id, secret and CAPS. */
static uint8_t tag_hello(const struct ctag_ctrl_challenge *ch, const uint8_t *hello, size_t hello_len,
			 uint8_t *out, size_t *len)
{
	return ctag_session_tag_hello(&tag, V_SESSION_TAG_ID, V_SESSION_TAG_SECRET, V_SESSION_CAPS,
				      V_SESSION_CAPS_LEN, ch, hello, hello_len, out, len);
}

/* ERROR{status, stored_epoch} as the tag must send it. */
static void assert_error(const uint8_t *out, size_t len, uint8_t status, uint32_t stored_epoch)
{
	uint8_t st = 0;
	uint32_t epoch = 0;

	zassert_equal(len, CTAG_SESSION_ERROR_LEN);
	zassert_equal(out[0], CTAG_CTRL_ERROR);
	zassert_true(ctag_session_error_unpack(out, len, &st, &epoch));
	zassert_equal(st, status, "ERROR{%u}, expected %u", st, status);
	zassert_equal(epoch, stored_epoch, "stored epoch %u, expected %u", epoch, stored_epoch);
}

/* Run the fixture handshake; leaves bridge and tag established. */
static void handshake(void)
{
	struct ctag_ctrl_challenge ch, seen;
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	uint8_t k_epoch[CTAG_TAG_KEY_LEN];
	size_t len;

	zassert_ok(ctag_session_k_epoch(V_SESSION_TAG_SECRET, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					k_epoch));
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH, k_epoch,
					     V_SESSION_NONCE_B, out));
	zassert_mem_equal(out, V_SESSION_HELLO, V_SESSION_HELLO_LEN);

	challenge_fields(&ch);
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len), CTAG_STATUS_OK);
	zassert_equal(len, V_SESSION_CHALLENGE_LEN);
	zassert_mem_equal(out, V_SESSION_CHALLENGE, len);
	zassert_mem_equal(tag.t.th, V_SESSION_TH, V_SESSION_TH_LEN);
	zassert_equal(tag.state, CTAG_SESSION_CHALLENGED);

	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, V_SESSION_CAPS_LEN,
						    V_SESSION_CHALLENGE, V_SESSION_CHALLENGE_LEN,
						    &seen, out),
		      CTAG_STATUS_OK);
	zassert_equal(seen.stored_epoch, ch.stored_epoch);
	zassert_mem_equal(out, V_SESSION_AUTH, V_SESSION_AUTH_LEN);
	zassert_mem_equal(bridge.t.th, V_SESSION_TH, V_SESSION_TH_LEN);

	zassert_equal(ctag_session_tag_auth(&tag, ch.stored_epoch, V_SESSION_AUTH, V_SESSION_AUTH_LEN,
					    out, &len),
		      CTAG_STATUS_OK);
	zassert_equal(len, V_SESSION_AUTH_OK_LEN);
	zassert_mem_equal(out, V_SESSION_AUTH_OK, len);
	zassert_equal(tag.state, CTAG_SESSION_ESTABLISHED);
	zassert_equal(tag.epoch, V_SESSION_EPOCH);

	zassert_equal(
		ctag_session_bridge_auth_ok(&bridge, V_SESSION_AUTH_OK, V_SESSION_AUTH_OK_LEN),
		CTAG_STATUS_OK);
	zassert_equal(bridge.state, CTAG_SESSION_ESTABLISHED);
}

ZTEST(ctag_session, test_k_epoch)
{
	uint8_t k[CTAG_TAG_KEY_LEN];

	zassert_ok(
		ctag_session_k_epoch(V_SESSION_TAG_SECRET, V_SESSION_TAG_ID, V_SESSION_EPOCH, k));
	zassert_mem_equal(k, V_SESSION_K_EPOCH, sizeof(k));
	zassert_ok(ctag_session_k_epoch(V_SESSION_TAG_SECRET, V_SESSION_TAG_ID,
					V_SESSION_NEXT_EPOCH, k));
	zassert_mem_equal(k, V_SESSION_K_EPOCH_NEXT, sizeof(k));
}

ZTEST(ctag_session, test_handshake)
{
	handshake();
	zassert_mem_equal(bridge.tx.key, V_SESSION_K_B2T, 16);
	zassert_mem_equal(bridge.rx.key, V_SESSION_K_T2B, 16);
	zassert_mem_equal(tag.rx.key, V_SESSION_K_B2T, 16);
	zassert_mem_equal(tag.tx.key, V_SESSION_K_T2B, 16);
	zassert_equal(bridge.tx.dir, CTAG_REC_DIR_B2T);
	zassert_equal(tag.tx.dir, CTAG_REC_DIR_T2B);
}

ZTEST(ctag_session, test_records)
{
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	uint8_t type;
	size_t i;

	handshake();
	for (i = 0; i < ARRAY_SIZE(v_session_records); i++) {
		const struct v_record *v = &v_session_records[i];
		bool b2t = v->dir == CTAG_REC_DIR_B2T;
		struct ctag_record_dir *tx = b2t ? &bridge.tx : &tag.tx;
		struct ctag_record_dir *rx = b2t ? &tag.rx : &bridge.rx;
		int n;

		zassert_equal(tx->counter, v->counter, "%s", v->name);
		n = ctag_record_seal(tx, v->type, v->plaintext, v->pt_len, rec, sizeof(rec));
		zassert_equal(n, (int)v->len, "%s", v->name);
		zassert_mem_equal(rec, v->record, v->len, "%s", v->name);
		n = ctag_record_open(rx, v->record, v->len, &type, pt, sizeof(pt));
		zassert_equal(n, (int)v->pt_len, "%s", v->name);
		zassert_equal(type, v->type, "%s", v->name);
		zassert_mem_equal(pt, v->plaintext, v->pt_len, "%s", v->name);
	}
	/* Replaying the first B2T record after the stream moved on. */
	zassert_equal(ctag_record_open(&tag.rx, v_session_records[0].record,
				       v_session_records[0].len, &type, pt, sizeof(pt)),
		      -EBADMSG);
}

ZTEST(ctag_session, test_tampered)
{
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	size_t i;

	for (i = 0; i < ARRAY_SIZE(v_session_tampered); i++) {
		const struct v_record *v = &v_session_tampered[i];
		struct ctag_record_dir d = {.counter = v->counter, .dir = (uint8_t)v->dir};
		uint8_t type;

		memcpy(d.key, v->dir == CTAG_REC_DIR_B2T ? V_SESSION_K_B2T : V_SESSION_K_T2B, 16);
		zassert_equal(ctag_record_open(&d, v->record, v->len, &type, pt, sizeof(pt)),
			      -EBADMSG, "%s", v->name);
		zassert_equal(d.counter, v->counter, "%s", v->name);
	}
}

ZTEST(ctag_session, test_counter_kept_on_failure)
{
	const struct v_record *v = &v_session_records[0];
	struct ctag_record_dir d = {.counter = 0, .dir = CTAG_REC_DIR_B2T};
	uint8_t rec[CTAG_TAG_RECORD_WIRE_MAX];
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	uint8_t type;

	memset(pt, 0x5A, sizeof(pt));
	memcpy(d.key, V_SESSION_K_B2T, 16);
	memcpy(rec, v->record, v->len);
	rec[v->len - 1] ^= 1;
	zassert_equal(ctag_record_open(&d, rec, v->len, &type, pt, sizeof(pt)), -EBADMSG);
	zassert_equal(ctag_record_open(&d, v->record, v->len, &type, pt, sizeof(pt)),
		      (int)v->pt_len);
	zassert_equal(d.counter, 1);
	/* Plaintext limit and output room. */
	zassert_equal(ctag_record_seal(&d, CTAG_REC_PLANE_DATA, pt,
				       CTAG_TAG_RECORD_PAYLOAD_MAX + 1u, rec, sizeof(rec)),
		      -EMSGSIZE);
	zassert_equal(
		ctag_record_seal(&d, CTAG_REC_FRAME_END, pt, 0, rec, CTAG_RECORD_OVERHEAD - 1u),
		-EMSGSIZE);
	zassert_equal(ctag_record_seal(&d, CTAG_REC_PLANE_DATA, pt, CTAG_TAG_RECORD_PAYLOAD_MAX,
				       rec, sizeof(rec)),
		      (int)CTAG_TAG_RECORD_WIRE_MAX);
	d.counter--; /* open what was just sealed with the same key and direction */
	zassert_equal(ctag_record_open(&d, rec, CTAG_TAG_RECORD_WIRE_MAX, &type, pt, 10),
		      -EMSGSIZE);
	zassert_equal(
		ctag_record_open(&d, rec, CTAG_TAG_RECORD_WIRE_MAX + 1u, &type, pt, sizeof(pt)),
		-EBADMSG);
	zassert_equal(ctag_record_open(&d, rec, CTAG_TAG_RECORD_WIRE_MAX, &type, pt, sizeof(pt)),
		      (int)CTAG_TAG_RECORD_PAYLOAD_MAX);
}

ZTEST(ctag_session, test_bad_mac_b)
{
	struct ctag_ctrl_challenge ch;
	uint8_t auth[CTAG_SESSION_AUTH_LEN] = {CTAG_CTRL_AUTH};
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	size_t len;

	challenge_fields(&ch);
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len), CTAG_STATUS_OK);
	memcpy(&auth[1], V_SESSION_BAD_MAC_B, CTAG_TAG_MAC_LEN);
	zassert_equal(ctag_session_tag_auth(&tag, ch.stored_epoch, auth, sizeof(auth), out, &len),
		      CTAG_STATUS_AUTH_FAILED);
	assert_error(out, len, CTAG_STATUS_AUTH_FAILED, ch.stored_epoch);
	zassert_equal(tag.state, CTAG_SESSION_FAILED);
	/* Nothing works on a failed session, not even the right MAC. */
	zassert_equal(ctag_session_tag_auth(&tag, ch.stored_epoch, V_SESSION_AUTH,
					    V_SESSION_AUTH_LEN, out, &len),
		      CTAG_STATUS_AUTH_FAILED);
}

/*
 * 5.4: th covers CAPS. A relay that rewrites the CAPS the bridge reads (here
 * plane_flags bit0) gives the bridge another transcript: its AUTH is the
 * fixture relayed one and the tag, hashing the CAPS it serves, refuses it.
 */
ZTEST(ctag_session, test_caps_bound_into_transcript)
{
	struct ctag_ctrl_challenge ch, seen;
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	size_t len;

	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_RELAYED_CAPS,
						    V_SESSION_RELAYED_CAPS_LEN, V_SESSION_CHALLENGE,
						    V_SESSION_CHALLENGE_LEN, &seen, out),
		      CTAG_STATUS_OK);
	zassert_mem_equal(bridge.t.th, V_SESSION_RELAYED_TH, V_SESSION_RELAYED_TH_LEN);
	zassert_mem_equal(out, V_SESSION_RELAYED_AUTH, V_SESSION_RELAYED_AUTH_LEN);

	challenge_fields(&ch);
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len), CTAG_STATUS_OK);
	zassert_equal(ctag_session_tag_auth(&tag, ch.stored_epoch, V_SESSION_RELAYED_AUTH,
					    V_SESSION_RELAYED_AUTH_LEN, out, &len),
		      CTAG_STATUS_AUTH_FAILED);
	assert_error(out, len, CTAG_STATUS_AUTH_FAILED, ch.stored_epoch);

	/* Without CAPS there is no transcript: refused, never a weaker one. */
	zassert_equal(ctag_session_tag_hello(&tag, V_SESSION_TAG_ID, V_SESSION_TAG_SECRET, NULL, 0u,
					     &ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len),
		      CTAG_STATUS_INTERNAL);
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, 0u, V_SESSION_CHALLENGE,
						    V_SESSION_CHALLENGE_LEN, &seen, out),
		      CTAG_STATUS_INTERNAL);
}

ZTEST(ctag_session, test_error_message)
{
	uint8_t msg[CTAG_SESSION_ERROR_LEN + 1u];
	uint8_t st;
	uint32_t epoch;

	zassert_equal(ctag_session_error_pack(msg, V_SESSION_STALE_STATUS, V_SESSION_STALE_STORED_EPOCH),
		      CTAG_SESSION_ERROR_LEN);
	zassert_mem_equal(msg, V_SESSION_STALE_ERROR, V_SESSION_STALE_ERROR_LEN);
	zassert_true(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN, &st, &epoch));
	zassert_equal(st, V_SESSION_STALE_STATUS);
	zassert_equal(epoch, V_SESSION_STALE_STORED_EPOCH);
	zassert_true(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN, NULL, NULL));
	zassert_false(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN - 1u, &st, &epoch));
	zassert_false(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN + 1u, &st, &epoch));
	msg[1] = CTAG_STATUS_OK; /* an ERROR never carries OK */
	zassert_false(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN, &st, &epoch));
	msg[1] = CTAG_STATUS_STALE_EPOCH;
	msg[0] = CTAG_CTRL_AUTH_OK;
	zassert_false(ctag_session_error_unpack(msg, CTAG_SESSION_ERROR_LEN, &st, &epoch));
}

ZTEST(ctag_session, test_tag_hello_checks)
{
	struct ctag_ctrl_challenge ch;
	uint8_t hello[CTAG_SESSION_HELLO_LEN];
	uint8_t out[CTAG_SESSION_CHALLENGE_LEN];
	size_t len;

	challenge_fields(&ch);
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN - 1u, out, &len),
		      CTAG_STATUS_INVALID);
	assert_error(out, len, CTAG_STATUS_INVALID, ch.stored_epoch);
	zassert_equal(ctag_session_tag_hello(&tag, V_SESSION_TAG_ID + 1u, V_SESSION_TAG_SECRET,
					     V_SESSION_CAPS, V_SESSION_CAPS_LEN, &ch, V_SESSION_HELLO,
					     V_SESSION_HELLO_LEN, out, &len),
		      CTAG_STATUS_NOT_FOUND);
	assert_error(out, len, CTAG_STATUS_NOT_FOUND, ch.stored_epoch);
	memcpy(hello, V_SESSION_HELLO, sizeof(hello));
	hello[1] = 2; /* proto */
	zassert_equal(tag_hello(&ch, hello, sizeof(hello), out, &len), CTAG_STATUS_VERSION_MISMATCH);
	assert_error(out, len, CTAG_STATUS_VERSION_MISMATCH, ch.stored_epoch);
	/* The fixture refusal: HELLO epoch 3 to a tag that stores 4. */
	ch.stored_epoch = V_SESSION_STALE_STORED_EPOCH;
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len),
		      V_SESSION_STALE_STATUS);
	zassert_equal(len, V_SESSION_STALE_ERROR_LEN);
	zassert_mem_equal(out, V_SESSION_STALE_ERROR, len);
	/* A newer epoch than stored is accepted (and persisted by the app after AUTH). */
	ch.stored_epoch = V_SESSION_EPOCH - 1u;
	zassert_equal(tag_hello(&ch, V_SESSION_HELLO, V_SESSION_HELLO_LEN, out, &len), CTAG_STATUS_OK);
}

ZTEST(ctag_session, test_bridge_failures)
{
	static const uint8_t short_error[] = {CTAG_CTRL_ERROR, CTAG_STATUS_STALE_EPOCH};
	struct ctag_ctrl_challenge ch;
	uint8_t out[CTAG_SESSION_HELLO_LEN];
	uint8_t msg[CTAG_SESSION_CHALLENGE_LEN];

	/* The tag ERROR: its status, and its stored epoch in ch (the rest zero). */
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, V_SESSION_CAPS_LEN,
						    V_SESSION_STALE_ERROR, V_SESSION_STALE_ERROR_LEN,
						    &ch, out),
		      CTAG_STATUS_STALE_EPOCH);
	zassert_equal(ch.stored_epoch, V_SESSION_STALE_STORED_EPOCH);
	zassert_equal(ch.displayed_rev, 0u);
	zassert_equal(bridge.state, CTAG_SESSION_FAILED);
	/* An ERROR of the draft length (no stored epoch) is malformed. */
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, V_SESSION_CAPS_LEN,
						    short_error, sizeof(short_error), &ch, out),
		      CTAG_STATUS_INVALID);
	zassert_equal(ch.stored_epoch, 0u);

	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	memcpy(msg, V_SESSION_CHALLENGE, sizeof(msg));
	msg[1] = 2; /* proto */
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, V_SESSION_CAPS_LEN, msg,
						    sizeof(msg), &ch, out),
		      CTAG_STATUS_VERSION_MISMATCH);

	/* AUTH_OK with a wrong mac_t. */
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(ctag_session_bridge_challenge(&bridge, V_SESSION_CAPS, V_SESSION_CAPS_LEN,
						    V_SESSION_CHALLENGE, V_SESSION_CHALLENGE_LEN, &ch,
						    out),
		      CTAG_STATUS_OK);
	memcpy(msg, V_SESSION_AUTH_OK, V_SESSION_AUTH_OK_LEN);
	msg[V_SESSION_AUTH_OK_LEN - 1u] ^= 0x80;
	zassert_equal(ctag_session_bridge_auth_ok(&bridge, msg, V_SESSION_AUTH_OK_LEN),
		      CTAG_STATUS_AUTH_FAILED);
	zassert_equal(bridge.state, CTAG_SESSION_FAILED);
	/* Out of order: AUTH_OK before CHALLENGE. */
	zassert_ok(ctag_session_bridge_hello(&bridge, V_SESSION_TAG_ID, V_SESSION_EPOCH,
					     V_SESSION_K_EPOCH, V_SESSION_NONCE_B, out));
	zassert_equal(
		ctag_session_bridge_auth_ok(&bridge, V_SESSION_AUTH_OK, V_SESSION_AUTH_OK_LEN),
		CTAG_STATUS_INVALID);
}

ZTEST(ctag_session, test_crypto_backend)
{
	/* FIPS 180-2 "abc", fed in two parts. */
	static const uint8_t abc_digest[32] = {
		0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea, 0x41, 0x41, 0x40,
		0xde, 0x5d, 0xae, 0x22, 0x23, 0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17,
		0x7a, 0x9c, 0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad,
	};
	ctag_sha256_ctx ctx;
	uint8_t digest[32];
	uint8_t a[16], b[16];

	zassert_ok(ctag_crypto_sha256_init(&ctx));
	zassert_ok(ctag_crypto_sha256_update(&ctx, (const uint8_t *)"a", 1));
	zassert_ok(ctag_crypto_sha256_update(&ctx, (const uint8_t *)"bc", 2));
	zassert_ok(ctag_crypto_sha256_finish(&ctx, digest));
	zassert_mem_equal(digest, abc_digest, sizeof(digest));
	zassert_ok(ctag_crypto_random(a, sizeof(a)));
	zassert_ok(ctag_crypto_random(b, sizeof(b)));
	zassert_true(memcmp(a, b, sizeof(a)) != 0);
	zassert_true(ctag_session_equal(a, a, sizeof(a)));
	zassert_false(ctag_session_equal(a, b, sizeof(a)));
}

static void *setup(void)
{
	zassert_ok(ctag_crypto_init());
	return NULL;
}

ZTEST_SUITE(ctag_session, NULL, setup, NULL, NULL, NULL);
