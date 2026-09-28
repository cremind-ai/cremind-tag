/*
 * ctag_secure against v2_secure.json (identities, key schedule, every grant
 * rule in check order, the byte-exact Noise IK conversation and sealed PAIR)
 * and the device rules of companion secure/device.py (test_v2_device.py's
 * flows), the ownership record, tunnel fragments and heap exhaustion.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_secure.h>

#include "check.h"
#include "suites.h"
#include "v_v2.h"

/* ---- Deterministic randomness: SHA-256("ctag-test" | counter) ---- */

static uint32_t rng_counter;
static const uint8_t *next_challenge; /* one-shot: the next 16-byte draw */

static void rng_fill(uint8_t *buf, size_t len)
{
	while (len > 0u) {
		uint8_t seed[13] = {'c', 't', 'a', 'g', '-', 't', 'e', 's', 't'};
		uint8_t d[32];
		size_t n = len < sizeof(d) ? len : sizeof(d);

		seed[9] = (uint8_t)rng_counter;
		seed[10] = (uint8_t)(rng_counter >> 8);
		seed[11] = (uint8_t)(rng_counter >> 16);
		seed[12] = (uint8_t)(rng_counter >> 24);
		rng_counter++;
		ctag_secure_sha256(seed, sizeof(seed), d);
		memcpy(buf, d, n);
		buf += n;
		len -= n;
	}
}

static int test_rng(void *ctx, uint8_t *buf, size_t len)
{
	(void)ctx;
	if (next_challenge != NULL && len == CTAG_CHALLENGE_LEN) {
		memcpy(buf, next_challenge, len);
		next_challenge = NULL;
		return 0;
	}
	rng_fill(buf, len);
	return 0;
}

static int failing_rng(void *ctx, uint8_t *buf, size_t len)
{
	(void)ctx;
	(void)buf;
	(void)len;
	return -EIO;
}

/* ---- Fixtures of a device and its persistence ---- */

struct store {
	unsigned int writes;
	bool fail;
	struct ctag_owner_record last;
};

static int store_persist(void *ctx, const struct ctag_owner_record *r)
{
	struct store *s = ctx;

	if (s->fail) {
		return -EIO;
	}
	s->writes++;
	s->last = *r;
	return 0;
}

struct dut {
	struct ctag_secure_keys keys;
	struct ctag_secure_ep ep;
	struct store store;
};

static void dut_init(struct dut *d, uint8_t role)
{
	uint8_t ik[32], secret[CTAG_SETUP_SECRET_LEN];
	struct ctag_secure_ops ops = {.persist = store_persist, .random = test_rng};

	memset(d, 0, sizeof(*d));
	rng_fill(ik, sizeof(ik));
	rng_fill(secret, sizeof(secret));
	ctag_secure_keys_init(&d->keys, role, ik, role == CTAG_NODE_ROLE_GATEWAY ? NULL : secret, 19u,
			      0u, 2u, 0u);
	ops.ctx = &d->store;
	ctag_secure_ep_init(&d->ep, &d->keys, NULL, &ops);
}

struct worker {
	uint8_t priv[32];
	uint8_t pub[32];
	uint8_t h[32];
	struct ctag_noise nz;
};

static void worker_init(struct worker *w)
{
	memset(w, 0, sizeof(*w));
	rng_fill(w->priv, sizeof(w->priv));
	ctag_secure_x25519_public(w->priv, w->pub);
}

/* Noise IK from the worker to the device's endpoint; 0 or an error. */
static int connect_to(struct worker *w, struct dut *d, uint8_t link)
{
	uint8_t prologue[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN];
	size_t plen = ctag_noise_prologue(link, d->keys.device_id, prologue);
	uint8_t msg1[CTAG_SECURE_MSG1_LEN], msg2[CTAG_SECURE_MSG2_LEN];
	int err = ctag_noise_connect(&w->nz, w->priv, d->keys.ik_pub, prologue, plen, msg1);

	if (err == 0) {
		err = ctag_secure_open(&d->ep, link, msg1, sizeof(msg1), msg2);
	}
	if (err == 0) {
		err = ctag_noise_finish(&w->nz, msg2, sizeof(msg2), w->h);
	}
	return err;
}

struct authority {
	uint8_t sk[32];
	uint8_t pub[32];
	uint8_t owner[CTAG_OWNER_LEN];
};

static void authority_init(struct authority *a, uint8_t owner_byte)
{
	rng_fill(a->sk, sizeof(a->sk));
	ctag_secure_ed25519_public(a->sk, a->pub);
	memset(a->owner, owner_byte, sizeof(a->owner));
}

struct signed_grant {
	uint8_t grant[CTAG_GRANT_MAX];
	size_t len;
	uint8_t sig[CTAG_GRANT_SIG_LEN];
};

static void grant_for(const struct authority *a, const struct dut *d, uint8_t op,
		      const uint8_t controller[32], const uint8_t challenge[16], uint32_t gen_from,
		      struct signed_grant *out)
{
	struct ctag_grant g;
	int n;

	memset(&g, 0, sizeof(g));
	g.op = op;
	g.role = d->keys.role;
	g.gen_from = gen_from;
	g.gen_to = gen_from + 1u;
	memcpy(g.device_id, d->keys.device_id, sizeof(g.device_id));
	memcpy(g.authority_pub, a->pub, sizeof(g.authority_pub));
	memcpy(g.owner, a->owner, sizeof(g.owner));
	memcpy(g.controller, controller, sizeof(g.controller));
	memcpy(g.challenge, challenge, sizeof(g.challenge));
	n = ctag_grant_encode(&g, out->grant, sizeof(out->grant));
	out->len = n > 0 ? (size_t)n : 0u;
	ctag_secure_grant_sign(a->sk, out->grant, out->len, out->sig);
}

/* STATUS through the endpoint; returns the fresh challenge in out. */
static uint8_t status_of(struct dut *d, struct ctag_secure_answer *ans)
{
	struct ctag_secure_req req = {0};

	ctag_secure_handle(&d->ep, CTAG_SERIAL_MSG_STATUS, &req, ans);
	return ans->status;
}

static uint8_t call_grant(struct dut *d, uint8_t type, const struct signed_grant *g,
			  struct ctag_secure_answer *ans)
{
	struct ctag_secure_req req = {.grant = g->grant, .grant_len = g->len, .sig = g->sig,
				      .sig_len = sizeof(g->sig)};

	ctag_secure_handle(&d->ep, type, &req, ans);
	return ans->status;
}

static bool contains(const uint8_t *hay, size_t n, const uint8_t *needle, size_t len)
{
	for (size_t i = 0u; i + len <= n; i++) {
		if (memcmp(&hay[i], needle, len) == 0) {
			return true;
		}
	}
	return false;
}

static void setup(void)
{
	rng_counter = 0u;
	next_challenge = NULL;
	ctag_secure_rng_set(test_rng, NULL);
	ctag_secure_heap_fail_after(-1);
}

/* ---- Identity and key schedule ---- */

void test_v2_identities(void)
{
	setup();
	for (size_t i = 0u; i < V_COUNT(v_v2_identities); i++) {
		const struct v_v2_identity *v = &v_v2_identities[i];
		struct ctag_secure_keys k;

		ctag_secure_keys_init(&k, v->role, v->ik_priv, NULL, 0u, 0u, 2u, 0u);
		CHECK(memcmp(k.ik_pub, v->ik_pub, 32u) == 0);
		CHECK(memcmp(k.device_id, v->device_id, 16u) == 0);
		CHECK(k.short_id == v->short_id);
	}
	/* short_id never 0 or all-ones */
	{
		static const uint8_t zero[16], ones[16] = {0xFF, 0xFF, 0xFF, 0xFF};

		CHECK(ctag_secure_short_id(zero) == 0x5A5A5A5Au);
		CHECK(ctag_secure_short_id(ones) == 0xA5A5A5A5u);
	}
	for (size_t i = 0u; i < V_COUNT(v_v2_codes); i++) {
		uint8_t payload[CTAG_SETUP_PAYLOAD_LEN];

		ctag_secure_setup_payload(v_v2_codes[i].role, v_v2_codes[i].short_id,
					  v_v2_codes[i].secret, payload);
		CHECK(memcmp(payload, v_v2_codes[i].payload, sizeof(payload)) == 0);
	}
}

void test_v2_key_schedule(void)
{
	uint8_t out[32];
	uint8_t id[16];

	setup();
	ctag_secure_k_setup(V_V2_KS_SETUP_SECRET, V_V2_KS_DEVICE_ID, out);
	CHECK(memcmp(out, V_V2_KS_K_SETUP, 32u) == 0);
	ctag_secure_static_oob(V_V2_KS_SETUP_SECRET, V_V2_KS_DEVICE_ID, out);
	CHECK(memcmp(out, V_V2_KS_STATIC_OOB, 32u) == 0);
	ctag_secure_k_epoch(V_V2_KS_ROOT, V_V2_KS_TAG_ID, V_V2_KS_EPOCH, out);
	CHECK(memcmp(out, V_V2_KS_K_EPOCH, 16u) == 0);
	ctag_secure_root_proof(V_V2_KS_ROOT, V_V2_KS_H, out);
	CHECK(memcmp(out, V_V2_KS_ROOT_PROOF, 16u) == 0);
	ctag_secure_maint_proof(V_V2_KS_ROOT, V_V2_KS_H, out); /* the fixture uses root as mk */
	CHECK(memcmp(out, V_V2_KS_MAINT_PROOF, 16u) == 0);
	/* proof_s / proof_d of the conversation */
	ctag_secure_k_setup(V_V2_CONV_SETUP_SECRET, &V_V2_CONV_IDENT[2], out);
	{
		uint8_t ps[16];

		ctag_secure_proof_s(out, V_V2_CONV_HANDSHAKE_HASH, V_V2_CONV_GRANT, V_V2_CONV_GRANT_LEN, ps);
		CHECK(memcmp(ps, V_V2_CONV_PROOF_S, 16u) == 0);
	}
	/* authority_id and HKDF bounds */
	ctag_secure_authority_id(V_V2_AUTHORITY_PUB, id);
	ctag_secure_sha256(V_V2_AUTHORITY_PUB, 32u, out);
	CHECK(memcmp(id, out, 16u) == 0);
	CHECK(ctag_secure_hkdf(out, 32u, out, 1u, out, 64u, out, 16u) == -EINVAL);
	CHECK(ctag_secure_hkdf(out, 32u, out, 1u, out, 1u, out, 33u) == -EINVAL);
}

/* ---- Grants ---- */

void test_v2_grant_cases(void)
{
	setup();
	for (size_t i = 0u; i < V_COUNT(v_v2_grant_cases); i++) {
		const struct v_v2_grant_case *v = &v_v2_grant_cases[i];
		struct ctag_grant_ctx c = {
			.role = v->role,
			.state = v->state,
			.gen = v->gen,
			.device_id = v->device_id,
			.authority_pub = v->authority_len == 32u ? v->authority_pub : NULL,
			.owner = v->owner_len == 16u ? v->owner : NULL,
			.challenge = v->challenge,
			.session_controller = v->session_controller,
			.ops = v->ops,
			.setup_proof_ok = v->setup_proof_ok,
		};
		struct ctag_grant g;
		bool decoded = false;

		CHECK_CASE(ctag_grant_check(&c, v->grant, v->grant_len, v->sig, v->sig_len, &g,
					    &decoded) == v->expect,
			   v->name);
		if (ctag_grant_decode(v->grant, v->grant_len, &g) == 0) {
			uint8_t again[CTAG_GRANT_MAX];

			CHECK_CASE(ctag_grant_encode(&g, again, sizeof(again)) == (int)v->grant_len,
				   v->name);
			CHECK_CASE(memcmp(again, v->grant, v->grant_len) == 0, v->name);
		}
		/* no challenge drawn: GRANT_INVALID before any later rule */
		c.challenge = NULL;
		CHECK_CASE(ctag_grant_check(&c, v->grant, v->grant_len, v->sig, v->sig_len, NULL, NULL) ==
				   CTAG_STATUS_GRANT_INVALID ||
			   v->expect == CTAG_STATUS_GRANT_INVALID, v->name);
	}
}

void test_v2_grant_strict(void)
{
	const struct v_v2_grant_case *ok = &v_v2_grant_cases[0];
	uint8_t buf[CTAG_GRANT_MAX + 8u];
	struct ctag_grant g;
	size_t n = ok->grant_len;

	setup();
	CHECK(ctag_grant_decode(ok->grant, n, &g) == 0);
	CHECK(g.op == CTAG_GRANT_OP_CLAIM && g.role == CTAG_NODE_ROLE_GATEWAY && g.gen_to == 1u);
	/* every truncation, a trailing byte, an empty and an oversize grant */
	for (size_t len = 0u; len < n; len++) {
		CHECK(ctag_grant_decode(ok->grant, len, &g) == -EBADMSG);
	}
	memcpy(buf, ok->grant, n);
	buf[n] = 0x00u;
	CHECK(ctag_grant_decode(buf, n + 1u, &g) == -EBADMSG);
	CHECK(ctag_grant_decode(buf, CTAG_GRANT_MAX + 1u, &g) == -EBADMSG);
	/* map(11) with an extra key, map(9) */
	memcpy(buf, ok->grant, n);
	buf[0] = 0xABu;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	buf[0] = 0xA9u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	/* version 3, op 7, op 0, role 4, a bool for op */
	memcpy(buf, ok->grant, n);
	buf[2] = 0x03u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	memcpy(buf, ok->grant, n);
	buf[4] = 0x07u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	buf[4] = 0x00u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	buf[4] = 0xF5u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	memcpy(buf, ok->grant, n);
	CHECK(buf[23] == 0x03u && buf[24] == CTAG_NODE_ROLE_GATEWAY); /* key 3, role */
	buf[24] = 0x04u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	buf[24] = 0x00u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	/* keys swapped (1 before 0) */
	memcpy(buf, ok->grant, n);
	buf[1] = 0x01u;
	buf[3] = 0x00u;
	CHECK(ctag_grant_decode(buf, n, &g) == -EBADMSG);
	/* gen_from as a non-shortest 0x18 0x00 */
	{
		size_t gen_key = n - 1u - 17u - 1u - 1u - 1u - 1u; /* key 7 */
		size_t k = 0u;

		CHECK(ok->grant[gen_key] == 0x07u && ok->grant[gen_key + 1u] == 0x00u);
		memcpy(buf, ok->grant, gen_key + 1u);
		k = gen_key + 1u;
		buf[k++] = 0x18u;
		buf[k++] = 0x00u;
		memcpy(&buf[k], &ok->grant[gen_key + 2u], n - gen_key - 2u);
		k += n - gen_key - 2u;
		CHECK(ctag_grant_decode(buf, k, &g) == -EBADMSG);
	}
	/* the controller as a 31-byte string */
	{
		static const uint8_t short_bstr[] = {0x58, 0x1F};
		size_t at = 0u;

		for (size_t i = 0u; i + 1u < n; i++) {
			if (ok->grant[i] == 0x06u && ok->grant[i + 1u] == 0x58u) {
				at = i + 1u;
			}
		}
		CHECK(at != 0u);
		memcpy(buf, ok->grant, n);
		memcpy(&buf[at], short_bstr, sizeof(short_bstr));
		CHECK(ctag_grant_decode(buf, n - 1u, &g) == -EBADMSG);
	}
	/* encode bounds */
	CHECK(ctag_grant_encode(&g, buf, 10u) == -EMSGSIZE);
	/* big generations: 0xFFFFFFFF -> gen_to cannot follow */
	{
		struct ctag_grant big;
		uint8_t raw[CTAG_GRANT_MAX];
		int len;

		CHECK(ctag_grant_decode(ok->grant, n, &big) == 0);
		big.gen_from = 0xFFFFFFFFu;
		big.gen_to = 0u;
		len = ctag_grant_encode(&big, raw, sizeof(raw));
		CHECK(len > 0 && ctag_grant_decode(raw, (size_t)len, &g) == 0);
		CHECK(g.gen_from == 0xFFFFFFFFu && g.gen_to == 0u);
	}
}

/* ---- The conversation (Noise IK through a tunnel, then PAIR) ---- */

void test_v2_conversation(void)
{
	static const uint8_t challenge[16] = {0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
					      0x88, 0x89, 0x8a, 0x8b, 0x8c, 0x8d, 0x8e, 0x8f};
	struct ctag_noise init;
	struct dut tag;
	struct ctag_secure_ops ops = {.persist = store_persist, .random = test_rng};
	uint8_t msg1[CTAG_SECURE_MSG1_LEN], msg2[CTAG_SECURE_MSG2_LEN], h[32], prologue[31];
	uint8_t buf[512];
	const uint8_t *pt;
	size_t pt_len;
	struct ctag_ident2 ident;
	struct ctag_secure_req req = {0};
	struct ctag_secure_answer ans;
	struct ctag_secure_hdr hdr = {CTAG_SERIAL_MSG_PAIR, CTAG_SERIAL_FLAG_RESPONSE, 1u};
	int n;

	setup();
	memset(&init, 0, sizeof(init));
	memset(&tag, 0, sizeof(tag));
	/* The tag: its identity key and label secret, unowned, a Hema 52811 at 0.2.0. */
	ctag_secure_keys_init(&tag.keys, CTAG_NODE_ROLE_TAG, V_V2_CONV_TAG_IK_PRIV,
			      V_V2_CONV_SETUP_SECRET, CTAG_BOARD_HEMA_NRF52811, 0u, 2u, 0u);
	ops.ctx = &tag.store;
	ctag_secure_ep_init(&tag.ep, &tag.keys, NULL, &ops);
	next_challenge = challenge;
	CHECK(ctag_secure_ident2(&tag.ep, &ident) == 0);
	n = ctag_ident2_pack(&ident, buf, sizeof(buf));
	CHECK(n == (int)V_V2_CONV_IDENT_LEN && memcmp(buf, V_V2_CONV_IDENT, V_V2_CONV_IDENT_LEN) == 0);

	/* Initiator (the worker), with the fixture's ephemeral. */
	CHECK(ctag_noise_prologue(CTAG_LINK_TUNNEL, tag.keys.device_id, prologue) ==
	      V_V2_CONV_PROLOGUE_LEN);
	CHECK(memcmp(prologue, V_V2_CONV_PROLOGUE, V_V2_CONV_PROLOGUE_LEN) == 0);
	ctag_secure_test_ephemeral(V_V2_CONV_INIT_EPHEMERAL);
	CHECK(ctag_noise_connect(&init, V_V2_CONV_CONTROLLER_PRIV, tag.keys.ik_pub, prologue,
				 sizeof(prologue), msg1) == 0);
	CHECK(memcmp(msg1, V_V2_CONV_MSG1, sizeof(msg1)) == 0);

	/* Responder (the tag's endpoint) */
	ctag_secure_test_ephemeral(V_V2_CONV_RESP_EPHEMERAL);
	CHECK(ctag_secure_open(&tag.ep, CTAG_LINK_TUNNEL, V_V2_CONV_MSG1, V_V2_CONV_MSG1_LEN, msg2) ==
	      0);
	CHECK(memcmp(msg2, V_V2_CONV_MSG2, sizeof(msg2)) == 0);
	CHECK(memcmp(tag.ep.h, V_V2_CONV_HANDSHAKE_HASH, 32u) == 0);
	CHECK(memcmp(tag.ep.controller, V_V2_CONV_CONTROLLER_PUB, 32u) == 0);
	CHECK(!ctag_secure_controller_match(&tag.ep));
	CHECK(ctag_noise_finish(&init, msg2, sizeof(msg2), h) == 0);
	CHECK(memcmp(h, V_V2_CONV_HANDSHAKE_HASH, 32u) == 0);

	/* The sealed PAIR request, both ways */
	memcpy(buf, V_V2_CONV_REQUEST_PLAIN, V_V2_CONV_REQUEST_PLAIN_LEN);
	n = ctag_noise_seal(&init, buf, V_V2_CONV_REQUEST_PLAIN_LEN, buf, sizeof(buf));
	CHECK(n == (int)V_V2_CONV_REQUEST_SEALED_LEN &&
	      memcmp(buf, V_V2_CONV_REQUEST_SEALED, V_V2_CONV_REQUEST_SEALED_LEN) == 0);
	CHECK(ctag_secure_unseal(&tag.ep, V_V2_CONV_REQUEST_SEALED, V_V2_CONV_REQUEST_SEALED_LEN, &pt,
				 &pt_len) == 0);
	CHECK(pt_len == V_V2_CONV_REQUEST_PLAIN_LEN &&
	      memcmp(pt, V_V2_CONV_REQUEST_PLAIN, pt_len) == 0);
	CHECK(ctag_secure_hdr_unpack(&hdr, pt, pt_len) == 0 && hdr.type == CTAG_SERIAL_MSG_PAIR &&
	      hdr.flags == 0u && hdr.request_id == 1u);
	ctag_secure_unseal_done(&tag.ep);

	/* PAIR with the conversation's fields (the CBOR layer is the application's). */
	req.grant = V_V2_CONV_GRANT;
	req.grant_len = V_V2_CONV_GRANT_LEN;
	req.sig = V_V2_CONV_SIG;
	req.sig_len = V_V2_CONV_SIG_LEN;
	req.proof = V_V2_CONV_PROOF_S;
	req.proof_len = V_V2_CONV_PROOF_S_LEN;
	req.op_key = V_V2_KS_ROOT;
	req.op_key_len = V_V2_KS_ROOT_LEN;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_PAIR, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 1u);
	CHECK(tag.ep.rec.state == CTAG_OWNER_OWNED && tag.store.writes == 1u);
	CHECK(memcmp(tag.ep.rec.op_key, V_V2_KS_ROOT, 32u) == 0);
	CHECK(memcmp(tag.ep.rec.authority_pub, V_V2_AUTHORITY_PUB, 32u) == 0);
	CHECK(ctag_secure_controller_match(&tag.ep));
	hdr.type = CTAG_SERIAL_MSG_PAIR;
	hdr.flags = CTAG_SERIAL_FLAG_RESPONSE;
	hdr.request_id = 1u;
	ctag_secure_hdr_pack(&hdr, buf);
	n = ctag_secure_answer_encode(&ans, &buf[CTAG_SECURE_HEADER_LEN],
				      sizeof(buf) - CTAG_SECURE_HEADER_LEN);
	CHECK(n > 0 && (size_t)n + CTAG_SECURE_HEADER_LEN == V_V2_CONV_ANSWER_PLAIN_LEN);
	CHECK(memcmp(buf, V_V2_CONV_ANSWER_PLAIN, V_V2_CONV_ANSWER_PLAIN_LEN) == 0);
	n = ctag_secure_seal(&tag.ep, buf, V_V2_CONV_ANSWER_PLAIN_LEN, buf, sizeof(buf));
	CHECK(n == (int)V_V2_CONV_ANSWER_SEALED_LEN &&
	      memcmp(buf, V_V2_CONV_ANSWER_SEALED, V_V2_CONV_ANSWER_SEALED_LEN) == 0);
	CHECK(ctag_noise_unseal(&init, V_V2_CONV_ANSWER_SEALED, V_V2_CONV_ANSWER_SEALED_LEN, &pt,
				&pt_len) == 0);
	CHECK(pt_len == V_V2_CONV_ANSWER_PLAIN_LEN && memcmp(pt, V_V2_CONV_ANSWER_PLAIN, pt_len) == 0);
	ctag_noise_unseal_done(&init);

	/* A replayed or tampered message ends the session (3.2). */
	CHECK(ctag_secure_unseal(&tag.ep, V_V2_CONV_REQUEST_SEALED, V_V2_CONV_REQUEST_SEALED_LEN, &pt,
				 &pt_len) == -EBADMSG);
	CHECK(!tag.ep.session && !ctag_noise_open(&tag.ep.noise));
	CHECK(ctag_secure_seal(&tag.ep, buf, 4u, buf, sizeof(buf)) == -ENOTCONN);
	ctag_noise_free(&init);
	ctag_noise_free(&tag.ep.noise);
	{
		struct ctag_secure_heap_stats st;

		ctag_secure_heap_stats(&st);
		CHECK(st.used == 0u);
	}
}

void test_v2_handshake_errors(void)
{
	struct dut d;
	struct worker w;
	uint8_t msg1[CTAG_SECURE_MSG1_LEN], msg2[CTAG_SECURE_MSG2_LEN], prologue[31];
	size_t plen;

	setup();
	dut_init(&d, CTAG_NODE_ROLE_GATEWAY);
	worker_init(&w);
	plen = ctag_noise_prologue(CTAG_LINK_SERIAL, d.keys.device_id, prologue);
	CHECK(ctag_noise_connect(&w.nz, w.priv, d.keys.ik_pub, prologue, plen, msg1) == 0);
	/* wrong length, a flipped bit in each part, another link's prologue */
	CHECK(ctag_secure_open(&d.ep, CTAG_LINK_SERIAL, msg1, sizeof(msg1) - 1u, msg2) == -EBADMSG);
	for (size_t i = 0u; i < sizeof(msg1); i += 11u) {
		uint8_t bad[CTAG_SECURE_MSG1_LEN];

		memcpy(bad, msg1, sizeof(bad));
		bad[i] ^= 0x01u;
		CHECK(ctag_secure_open(&d.ep, CTAG_LINK_SERIAL, bad, sizeof(bad), msg2) == -EBADMSG);
		CHECK(!d.ep.session);
	}
	CHECK(ctag_secure_open(&d.ep, CTAG_LINK_TUNNEL, msg1, sizeof(msg1), msg2) == -EBADMSG);
	CHECK(ctag_secure_open(&d.ep, CTAG_LINK_SERIAL, msg1, sizeof(msg1), msg2) == 0);
	CHECK(d.ep.session && memcmp(d.ep.controller, w.pub, 32u) == 0);
	/* the initiator refuses a tampered message 2 */
	msg2[40] ^= 0x80u;
	CHECK(ctag_noise_finish(&w.nz, msg2, sizeof(msg2), w.h) == -EBADMSG);
	CHECK(!ctag_noise_open(&w.nz));
	/* a second session from the same worker replaces the first */
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
	{
		uint32_t serial = d.ep.session_serial;

		CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
		CHECK(d.ep.session_serial != serial);
	}
	ctag_secure_close(&d.ep);
	CHECK(!d.ep.session);
	ctag_noise_free(&w.nz);
	ctag_noise_free(&d.ep.noise);
}

/* ---- The device rules (test_v2_device.py) ---- */

void test_v2_gateway_flows(void)
{
	struct dut gw;
	struct worker a, b;
	struct authority auth;
	struct signed_grant g;
	struct ctag_secure_answer ans;

	setup();
	dut_init(&gw, CTAG_NODE_ROLE_GATEWAY);
	worker_init(&a);
	worker_init(&b);
	authority_init(&auth, 0x11u);
	/* Without a session every message needs one. */
	CHECK(status_of(&gw, &ans) == CTAG_STATUS_AUTH_REQUIRED);

	CHECK(connect_to(&a, &gw, CTAG_LINK_SERIAL) == 0);
	CHECK(!ctag_secure_controller_match(&gw.ep));
	CHECK(status_of(&gw, &ans) == CTAG_STATUS_OK && ans.owner_state == CTAG_OWNER_UNOWNED &&
	      ans.gen == 0u && !ans.controller_match &&
	      (ans.fields & CTAG_SECURE_F_OWNED) == 0u);
	grant_for(&auth, &gw, CTAG_GRANT_OP_CLAIM, a.pub, ans.challenge, 0u, &g);
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_CLAIM, &g, &ans) == CTAG_STATUS_OK && ans.gen == 1u);
	CHECK(gw.ep.rec.state == CTAG_OWNER_OWNED && ctag_secure_controller_match(&gw.ep));
	CHECK(gw.store.writes == 1u && gw.store.last.gen == 1u);
	/* The same grant again: the challenge was single use. */
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_CLAIM, &g, &ans) == CTAG_STATUS_GRANT_INVALID);
	/* STATUS when owned: authority_id, and the owner to the pinned controller */
	CHECK(status_of(&gw, &ans) == CTAG_STATUS_OK && ans.controller_match &&
	      (ans.fields & CTAG_SECURE_F_OWNED) != 0u && (ans.fields & CTAG_SECURE_F_OWNER) != 0u &&
	      memcmp(ans.owner, auth.owner, 16u) == 0 && (ans.fields & CTAG_SECURE_F_ROOT_PROOF) == 0u);
	{
		uint8_t buf[128];
		int n = ctag_secure_answer_encode(&ans, buf, sizeof(buf));

		/* {status, owner_state, gen, authority_id, challenge, owner, controller_match} */
		CHECK(n > 0 && buf[0] == 0xA7u && contains(buf, (size_t)n, auth.owner, 16u));
	}

	/* Another computer: STATUS without the owner; CLAIM again is NOT_OWNER. */
	CHECK(connect_to(&b, &gw, CTAG_LINK_SERIAL) == 0);
	CHECK(!ctag_secure_controller_match(&gw.ep));
	CHECK(status_of(&gw, &ans) == CTAG_STATUS_OK && !ans.controller_match &&
	      ans.owner_state == CTAG_OWNER_OWNED && (ans.fields & CTAG_SECURE_F_OWNED) != 0u &&
	      (ans.fields & CTAG_SECURE_F_OWNER) == 0u);
	{
		uint8_t buf[128], aid[16];
		int n = ctag_secure_answer_encode(&ans, buf, sizeof(buf));

		ctag_secure_authority_id(auth.pub, aid);
		CHECK(memcmp(ans.authority_id, aid, 16u) == 0);
		CHECK(n > 0 && buf[0] == 0xA6u && !contains(buf, (size_t)n, auth.owner, 16u) &&
		      contains(buf, (size_t)n, aid, 16u));
	}
	grant_for(&auth, &gw, CTAG_GRANT_OP_CLAIM, b.pub, ans.challenge, 1u, &g);
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_CLAIM, &g, &ans) == CTAG_STATUS_NOT_OWNER);
	/* RECOVER moves the controller to b. */
	(void)status_of(&gw, &ans);
	grant_for(&auth, &gw, CTAG_GRANT_OP_RECOVER, b.pub, ans.challenge, 1u, &g);
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_RECOVER, &g, &ans) == CTAG_STATUS_OK && ans.gen == 2u);
	CHECK(ctag_secure_controller_match(&gw.ep));
	CHECK(connect_to(&a, &gw, CTAG_LINK_SERIAL) == 0);
	CHECK(!ctag_secure_controller_match(&gw.ep));

	/* RELEASE: UNOWNED, the generation kept (moved forward). */
	CHECK(connect_to(&b, &gw, CTAG_LINK_SERIAL) == 0);
	(void)status_of(&gw, &ans);
	grant_for(&auth, &gw, CTAG_GRANT_OP_RELEASE, b.pub, ans.challenge, 2u, &g);
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_RELEASE, &g, &ans) == CTAG_STATUS_OK && ans.released);
	CHECK(gw.ep.rec.state == CTAG_OWNER_UNOWNED && gw.ep.rec.gen == 3u && ans.gen == 3u);
	CHECK((ans.fields & CTAG_SECURE_F_DATA) != 0u && ans.data_len == 0u);
	/* RECOVER / RELEASE of an unowned gateway */
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_RECOVER, &g, &ans) == CTAG_STATUS_NOT_OWNER);
	CHECK(call_grant(&gw, CTAG_SERIAL_MSG_RELEASE, &g, &ans) == CTAG_STATUS_NOT_OWNER);
	/* Gateway-only rules */
	{
		struct ctag_secure_req req = {0};

		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_PAIR, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_REKEY, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_RECOMMISSION, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_INFO, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
		/* no grant: INVALID and the challenge stays */
		(void)status_of(&gw, &ans);
		ctag_secure_handle(&gw.ep, CTAG_SERIAL_MSG_CLAIM, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_INVALID && gw.ep.has_challenge);
	}
	ctag_noise_free(&a.nz);
	ctag_noise_free(&b.nz);
	ctag_noise_free(&gw.ep.noise);
}

void test_v2_claim_rules(void)
{
	enum { WRONG_CHALLENGE, REPLAYED, STALE_GEN, OTHER_CONTROLLER, BAD_SIG, OTHER_DEVICE, WRONG_OP,
	       STORE_FAILS, FAULTS };
	static const uint8_t expect[FAULTS] = {
		CTAG_STATUS_GRANT_INVALID, CTAG_STATUS_GRANT_INVALID, CTAG_STATUS_STALE_GENERATION,
		CTAG_STATUS_GRANT_INVALID, CTAG_STATUS_GRANT_INVALID, CTAG_STATUS_GRANT_INVALID,
		CTAG_STATUS_GRANT_INVALID, CTAG_STATUS_STORAGE_ERROR,
	};

	setup();
	for (int fault = 0; fault < FAULTS; fault++) {
		struct dut gw, other;
		struct worker w;
		struct authority auth;
		struct signed_grant g;
		struct ctag_secure_answer ans;
		uint8_t controller[32], challenge[16];
		uint32_t gen = 0u;
		uint8_t op = CTAG_GRANT_OP_CLAIM;
		const struct dut *target = &gw;

		dut_init(&gw, CTAG_NODE_ROLE_GATEWAY);
		dut_init(&other, CTAG_NODE_ROLE_GATEWAY);
		worker_init(&w);
		authority_init(&auth, 0x11u);
		CHECK(connect_to(&w, &gw, CTAG_LINK_SERIAL) == 0);
		(void)status_of(&gw, &ans);
		memcpy(challenge, ans.challenge, sizeof(challenge));
		memcpy(controller, w.pub, sizeof(controller));
		if (fault == WRONG_CHALLENGE) {
			memset(challenge, 0, sizeof(challenge));
		} else if (fault == STALE_GEN) {
			gen = 5u;
		} else if (fault == OTHER_CONTROLLER) {
			rng_fill(controller, sizeof(controller));
		} else if (fault == OTHER_DEVICE) {
			target = &other;
		} else if (fault == WRONG_OP) {
			op = CTAG_GRANT_OP_RECOVER;
		} else if (fault == STORE_FAILS) {
			gw.store.fail = true;
		}
		grant_for(&auth, target, op, controller, challenge, gen, &g);
		if (fault == BAD_SIG) {
			memset(g.sig, 0, sizeof(g.sig));
		}
		if (fault == REPLAYED) {
			struct signed_grant bad = g;

			memset(bad.sig, 0, sizeof(bad.sig));
			(void)call_grant(&gw, CTAG_SERIAL_MSG_CLAIM, &bad, &ans); /* uses the challenge */
		}
		CHECK_CASE(call_grant(&gw, CTAG_SERIAL_MSG_CLAIM, &g, &ans) == expect[fault], "fault");
		CHECK(gw.ep.rec.state == CTAG_OWNER_UNOWNED && gw.ep.rec.gen == 0u);
		CHECK(!gw.ep.has_challenge);
		ctag_noise_free(&w.nz);
		ctag_noise_free(&gw.ep.noise);
	}
}

static uint8_t pair(const struct authority *auth, struct dut *d, struct worker *w,
		    const uint8_t secret[10], const uint8_t op_key[32], uint8_t link,
		    struct ctag_secure_answer *ans)
{
	struct signed_grant g;
	uint8_t k_set[32], proof_s[16], proof_d[16];
	struct ctag_secure_req req;

	if (connect_to(w, d, link) != 0) {
		return 0xFFu;
	}
	(void)status_of(d, ans);
	grant_for(auth, d, CTAG_GRANT_OP_PAIR, w->pub, ans->challenge, d->ep.rec.gen, &g);
	ctag_secure_k_setup(secret, d->keys.device_id, k_set);
	ctag_secure_proof_s(k_set, w->h, g.grant, g.len, proof_s);
	memset(&req, 0, sizeof(req));
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.sig_len = sizeof(g.sig);
	req.proof = proof_s;
	req.proof_len = sizeof(proof_s);
	req.op_key = op_key;
	req.op_key_len = 32u;
	ctag_secure_handle(&d->ep, CTAG_SERIAL_MSG_PAIR, &req, ans);
	if (ans->status == CTAG_STATUS_OK) {
		ctag_secure_proof_d(k_set, w->h, proof_s, proof_d);
		if ((ans->fields & CTAG_SECURE_F_PROOF) == 0u || memcmp(proof_d, ans->proof, 16u) != 0) {
			return 0xFEu; /* the device's proof does not check out */
		}
	}
	return ans->status;
}

void test_v2_tag_flows(void)
{
	struct dut tag;
	struct worker w, w2, w3;
	struct authority auth, other;
	struct signed_grant g;
	struct ctag_secure_answer ans;
	uint8_t root[32], new_root[32], k[16], expect[16], factory[10], fresh[10];
	struct ctag_secure_req req;

	setup();
	dut_init(&tag, CTAG_NODE_ROLE_TAG);
	worker_init(&w);
	worker_init(&w2);
	worker_init(&w3);
	authority_init(&auth, 0x11u);
	authority_init(&other, 0x22u);
	memcpy(factory, tag.keys.factory_secret, sizeof(factory));
	memset(root, 7, sizeof(root));
	memset(new_root, 9, sizeof(new_root));

	/* A wrong setup code: PROOF_FAILED, counted; three in a row pause pairing. */
	{
		uint8_t wrong[10] = {0};

		CHECK(pair(&auth, &tag, &w, wrong, root, CTAG_LINK_TUNNEL, &ans) ==
		      CTAG_STATUS_PROOF_FAILED);
		CHECK(tag.ep.failures == 1u && !ctag_secure_pairing_paused(&tag.ep));
		CHECK(pair(&auth, &tag, &w, wrong, root, CTAG_LINK_TUNNEL, &ans) ==
		      CTAG_STATUS_PROOF_FAILED);
		CHECK(pair(&auth, &tag, &w, wrong, root, CTAG_LINK_TUNNEL, &ans) ==
		      CTAG_STATUS_PROOF_FAILED);
		CHECK(ctag_secure_pairing_paused(&tag.ep) && tag.ep.rec.state == CTAG_OWNER_UNOWNED);
	}
	CHECK(pair(&auth, &tag, &w, factory, root, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_OK);
	CHECK(tag.ep.failures == 0u && tag.ep.rec.gen == 1u);
	CHECK(ctag_secure_ep_k_epoch(&tag.ep, tag.keys.short_id, 1u, k) == 0);
	ctag_secure_k_epoch(root, tag.keys.short_id, 1u, expect);
	CHECK(memcmp(k, expect, 16u) == 0);

	/* root_proof tells which root the tag committed. */
	CHECK(connect_to(&w, &tag, CTAG_LINK_TUNNEL) == 0);
	CHECK(status_of(&tag, &ans) == CTAG_STATUS_OK && (ans.fields & CTAG_SECURE_F_ROOT_PROOF));
	ctag_secure_root_proof(root, w.h, expect);
	CHECK(memcmp(ans.root_proof, expect, 16u) == 0);

	/* An owned tag refuses PAIR from anyone; REKEY by another authority is refused. */
	CHECK(pair(&other, &tag, &w3, factory, root, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_NOT_OWNER);
	CHECK(connect_to(&w3, &tag, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&tag, &ans);
	grant_for(&other, &tag, CTAG_GRANT_OP_REKEY, w3.pub, ans.challenge, 1u, &g);
	memset(&req, 0, sizeof(req));
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.sig_len = sizeof(g.sig);
	req.op_key = new_root;
	req.op_key_len = 32u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_REKEY, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_NOT_OWNER);
	/* REKEY without op_key: INVALID (the challenge is used) */
	req.op_key = NULL;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_REKEY, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID && !tag.ep.has_challenge);

	/* REKEY (recovery onto another computer): new root, old K_epoch dead. */
	CHECK(connect_to(&w2, &tag, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_REKEY, w2.pub, ans.challenge, 1u, &g);
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.op_key = new_root;
	req.op_key_len = 32u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_REKEY, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 2u && ans.rekeyed);
	CHECK(ctag_secure_controller_match(&tag.ep));
	CHECK(ctag_secure_ep_k_epoch(&tag.ep, tag.keys.short_id, 5u, k) == 0);
	ctag_secure_k_epoch(root, tag.keys.short_id, 5u, expect);
	CHECK(memcmp(k, expect, 16u) != 0);

	/* RELEASE: stage 1 before stage 0 is INVALID; stage 2 is INVALID. */
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w2.pub, ans.challenge, 2u, &g);
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.op_key = NULL;
	req.has_release_stage = true;
	req.release_stage = 1u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID && tag.ep.rec.state == CTAG_OWNER_OWNED);
	req.release_stage = 2u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID);
	/* stage 0: a fresh setup payload, still OWNED at the same generation */
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w2.pub, ans.challenge, 2u, &g);
	req.release_stage = 0u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 2u && ans.data_len == 15u);
	CHECK(tag.ep.rec.state == CTAG_OWNER_OWNED && !ans.released);
	CHECK(ans.data[0] == (0x20u | CTAG_NODE_ROLE_TAG) &&
	      ctag_get_le32(&ans.data[1]) == tag.keys.short_id);
	memcpy(fresh, &ans.data[5], sizeof(fresh));
	CHECK(memcmp(fresh, factory, sizeof(fresh)) != 0);
	/* stage 1 by another controller: INVALID */
	CHECK(connect_to(&w3, &tag, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w3.pub, ans.challenge, 2u, &g);
	req.release_stage = 1u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID);
	CHECK(tag.ep.rec.has_pending_override && tag.ep.rec.has_pending_controller);
	/* A REKEY (onto w3) abandons the pending release: w2 cannot finish it. */
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_REKEY, w3.pub, ans.challenge, 2u, &g);
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.op_key = root;
	req.op_key_len = 32u;
	req.has_release_stage = false;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_REKEY, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 3u && ans.rekeyed);
	CHECK(!tag.ep.rec.has_pending_override && !tag.ep.rec.has_pending_controller);
	CHECK(!tag.store.last.has_pending_override && !tag.store.last.has_pending_controller);
	{
		static const uint8_t none[10u + 32u];
		uint8_t raw[CTAG_OWNER_RECORD_LEN];

		ctag_owner_record_encode(&tag.store.last, raw);
		CHECK(memcmp(&raw[130], none, 10u + 32u) == 0); /* no stale secret in the record */
	}
	CHECK(connect_to(&w2, &tag, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w2.pub, ans.challenge, 3u, &g);
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.op_key = NULL;
	req.op_key_len = 0u;
	req.has_release_stage = true;
	req.release_stage = 1u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID && tag.ep.rec.state == CTAG_OWNER_OWNED);
	/* stage 0 then 1 by the pinned controller: RELEASED, root gone */
	CHECK(connect_to(&w3, &tag, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&tag, &ans);
	grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w3.pub, ans.challenge, 3u, &g);
	req.grant = g.grant;
	req.grant_len = g.len;
	req.sig = g.sig;
	req.release_stage = 0u;
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 3u && ans.data_len == 15u);
	{
		uint8_t abandoned[10];

		memcpy(abandoned, fresh, sizeof(abandoned));
		memcpy(fresh, &ans.data[5], sizeof(fresh));
		CHECK(memcmp(fresh, abandoned, sizeof(fresh)) != 0);
		(void)status_of(&tag, &ans);
		grant_for(&auth, &tag, CTAG_GRANT_OP_RELEASE, w3.pub, ans.challenge, 3u, &g);
		req.grant = g.grant;
		req.grant_len = g.len;
		req.sig = g.sig;
		req.release_stage = 1u;
		ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_RELEASE, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_OK && ans.released && ans.gen == 4u &&
		      ans.data_len == 0u);
		CHECK(tag.ep.rec.state == CTAG_OWNER_RELEASED && !tag.ep.rec.has_op_key);
		CHECK(ctag_secure_ep_k_epoch(&tag.ep, tag.keys.short_id, 1u, k) == -ENOENT);
		/* neither the label's secret nor the abandoned one pairs; the fresh one does */
		CHECK(pair(&auth, &tag, &w, factory, root, CTAG_LINK_TUNNEL, &ans) ==
		      CTAG_STATUS_PROOF_FAILED);
		CHECK(pair(&auth, &tag, &w, abandoned, root, CTAG_LINK_TUNNEL, &ans) ==
		      CTAG_STATUS_PROOF_FAILED);
	}
	CHECK(pair(&auth, &tag, &w, fresh, root, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_OK);
	CHECK(tag.ep.rec.gen == 5u && tag.ep.failures == 0u);
	/* MAINT_AUTH / RECOMMISSION are bridge messages */
	memset(&req, 0, sizeof(req));
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
	ctag_secure_handle(&tag.ep, CTAG_SERIAL_MSG_CLAIM, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
	ctag_noise_free(&w.nz);
	ctag_noise_free(&w2.nz);
	ctag_noise_free(&w3.nz);
	ctag_noise_free(&tag.ep.noise);
}

void test_v2_bridge_flows(void)
{
	struct dut br;
	struct worker w;
	struct authority auth;
	struct signed_grant g;
	struct ctag_secure_answer ans;
	struct ctag_secure_req req;
	uint8_t mk[32], proof[16], fresh[10], factory[10], zero[32] = {0};

	setup();
	dut_init(&br, CTAG_NODE_ROLE_BRIDGE);
	worker_init(&w);
	authority_init(&auth, 0x11u);
	memcpy(factory, br.keys.factory_secret, sizeof(factory));
	memset(mk, 5, sizeof(mk));
	/* PAIR with op_key missing or short: INVALID */
	CHECK(connect_to(&w, &br, CTAG_LINK_TUNNEL) == 0);
	memset(&req, 0, sizeof(req));
	req.proof = proof;
	req.proof_len = 16u;
	req.op_key = mk;
	req.op_key_len = 31u;
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_PAIR, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_INVALID);
	CHECK(pair(&auth, &br, &w, factory, mk, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_OK);

	/* MAINT_AUTH never over the mesh, even with the right mk (nothing counted). */
	CHECK(connect_to(&w, &br, CTAG_LINK_TUNNEL) == 0);
	ctag_secure_maint_proof(mk, w.h, proof);
	memset(&req, 0, sizeof(req));
	req.proof = proof;
	req.proof_len = 16u;
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_NOT_OWNER && !br.ep.maint_ok && br.ep.failures == 0u);

	/* USB maintenance: a wrong mk is refused, the right one unlocks RECOMMISSION. */
	CHECK(connect_to(&w, &br, CTAG_LINK_SERIAL) == 0);
	ctag_secure_maint_proof(zero, w.h, proof);
	memset(&req, 0, sizeof(req));
	req.proof = proof;
	req.proof_len = 16u;
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_PROOF_FAILED && br.ep.failures == 1u);
	(void)status_of(&br, &ans);
	grant_for(&auth, &br, CTAG_GRANT_OP_MAINT, w.pub, ans.challenge, 1u, &g);
	CHECK(call_grant(&br, CTAG_SERIAL_MSG_RECOMMISSION, &g, &ans) == CTAG_STATUS_NOT_OWNER);
	ctag_secure_maint_proof(mk, w.h, proof);
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && br.ep.maint_ok && br.ep.failures == 0u);
	(void)status_of(&br, &ans);
	grant_for(&auth, &br, CTAG_GRANT_OP_MAINT, w.pub, ans.challenge, 1u, &g);
	CHECK(call_grant(&br, CTAG_SERIAL_MSG_RECOMMISSION, &g, &ans) == CTAG_STATUS_OK);
	CHECK(ans.recommissioned && ans.gen == 2u && ans.data_len == 15u);
	CHECK(br.ep.rec.state == CTAG_OWNER_RELEASED && !br.ep.rec.locked);
	memcpy(fresh, &ans.data[5], sizeof(fresh));
	CHECK(pair(&auth, &br, &w, fresh, mk, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_OK);
	CHECK(br.ep.rec.gen == 3u);

	/* RELEASE over the mesh locks the bridge until a local recommission. */
	CHECK(connect_to(&w, &br, CTAG_LINK_TUNNEL) == 0);
	(void)status_of(&br, &ans);
	grant_for(&auth, &br, CTAG_GRANT_OP_RELEASE, w.pub, ans.challenge, 3u, &g);
	CHECK(call_grant(&br, CTAG_SERIAL_MSG_RELEASE, &g, &ans) == CTAG_STATUS_OK && ans.released);
	CHECK(br.ep.rec.state == CTAG_OWNER_RELEASED && br.ep.rec.locked && br.ep.rec.gen == 4u);
	CHECK(pair(&auth, &br, &w, factory, mk, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_LOCKED);
	memset(&req, 0, sizeof(req));
	CHECK(connect_to(&w, &br, CTAG_LINK_TUNNEL) == 0);
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_RECOMMISSION, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_NOT_OWNER); /* never over the mesh */
	CHECK(connect_to(&w, &br, CTAG_LINK_SERIAL) == 0);
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_RECOMMISSION, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_OK && ans.gen == 4u && !br.ep.rec.locked);
	memcpy(fresh, &ans.data[5], sizeof(fresh));
	CHECK(pair(&auth, &br, &w, fresh, mk, CTAG_LINK_TUNNEL, &ans) == CTAG_STATUS_OK);
	/* MAINT_AUTH of an unowned bridge / without the field */
	{
		struct dut fresh_br;
		struct worker w2;

		dut_init(&fresh_br, CTAG_NODE_ROLE_BRIDGE);
		worker_init(&w2);
		CHECK(connect_to(&w2, &fresh_br, CTAG_LINK_SERIAL) == 0);
		ctag_secure_handle(&fresh_br.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_NOT_OWNER);
		/* RECOMMISSION of an unowned bridge: INVALID (the label's secret stays
		 * valid), the challenge used up, nothing stored. */
		(void)status_of(&fresh_br, &ans);
		CHECK(fresh_br.ep.has_challenge);
		ctag_secure_handle(&fresh_br.ep, CTAG_SERIAL_MSG_RECOMMISSION, &req, &ans);
		CHECK(ans.status == CTAG_STATUS_INVALID && !ans.recommissioned &&
		      !fresh_br.ep.has_challenge);
		CHECK(fresh_br.ep.rec.state == CTAG_OWNER_UNOWNED && fresh_br.store.writes == 0u);
		CHECK(pair(&auth, &fresh_br, &w2, fresh_br.keys.factory_secret, mk, CTAG_LINK_TUNNEL,
			   &ans) == CTAG_STATUS_OK);
		ctag_noise_free(&w2.nz);
		ctag_noise_free(&fresh_br.ep.noise);
	}
	CHECK(connect_to(&w, &br, CTAG_LINK_SERIAL) == 0);
	memset(&req, 0, sizeof(req));
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_MAINT_AUTH, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_PROOF_FAILED);
	ctag_secure_handle(&br.ep, CTAG_SERIAL_MSG_CLAIM, &req, &ans);
	CHECK(ans.status == CTAG_STATUS_UNSUPPORTED);
	ctag_noise_free(&w.nz);
	ctag_noise_free(&br.ep.noise);
}

/* ---- Ownership record ---- */

void test_v2_record(void)
{
	struct ctag_owner_record r, back;
	uint8_t raw[CTAG_OWNER_RECORD_LEN];

	setup();
	memset(&r, 0, sizeof(r));
	r.state = CTAG_OWNER_OWNED;
	r.gen = 7u;
	memset(r.authority_pub, 'a', sizeof(r.authority_pub));
	memset(r.owner, 'o', sizeof(r.owner));
	memset(r.controller, 'c', sizeof(r.controller));
	r.has_op_key = true;
	memset(r.op_key, 'k', sizeof(r.op_key));
	r.has_pending_override = true;
	memset(r.pending_override, 'p', sizeof(r.pending_override));
	r.has_pending_controller = true;
	memset(r.pending_controller, 'q', sizeof(r.pending_controller));
	ctag_owner_record_encode(&r, raw);
	CHECK(ctag_owner_record_decode(&back, raw, sizeof(raw)) == 0);
	CHECK(memcmp(&back, &r, sizeof(r)) == 0);
	CHECK(ctag_owner_record_load(&back, raw, sizeof(raw), 3u) && back.gen == 7u);
	CHECK(ctag_owner_record_load(&back, raw, sizeof(raw), 9u) && back.gen == 9u);
	/* released / locked */
	memset(&r, 0, sizeof(r));
	r.state = CTAG_OWNER_RELEASED;
	r.gen = 4u;
	r.locked = true;
	r.has_override = true;
	memset(r.override_secret, 's', sizeof(r.override_secret));
	ctag_owner_record_encode(&r, raw);
	CHECK(ctag_owner_record_decode(&back, raw, sizeof(raw)) == 0 && memcmp(&back, &r, sizeof(r)) == 0);
	/* any corrupted byte: UNOWNED with the floor */
	for (size_t i = 0u; i < sizeof(raw); i++) {
		uint8_t bad[CTAG_OWNER_RECORD_LEN];

		memcpy(bad, raw, sizeof(bad));
		bad[i] ^= 0x10u;
		CHECK(ctag_owner_record_decode(&back, bad, sizeof(bad)) == -EBADMSG);
		CHECK(!ctag_owner_record_load(&back, bad, sizeof(bad), 5u));
		CHECK(back.state == CTAG_OWNER_UNOWNED && back.gen == 5u && !back.has_override);
	}
	CHECK(!ctag_owner_record_load(&back, raw, sizeof(raw) - 1u, 6u) && back.gen == 6u);
	CHECK(!ctag_owner_record_load(&back, NULL, 0u, 0u) && back.gen == 0u &&
	      back.state == CTAG_OWNER_UNOWNED);
	/* a good CRC over a bad state, flag bit, reserved byte or version */
	{
		static const size_t at[] = {1u, 2u, 3u, 0u};
		static const uint8_t val[] = {3u, 0x20u, 1u, 2u};

		for (size_t i = 0u; i < V_COUNT(at); i++) {
			uint8_t bad[CTAG_OWNER_RECORD_LEN];

			memcpy(bad, raw, sizeof(bad));
			bad[at[i]] = (uint8_t)(i == 1u ? bad[2] | val[i] : val[i]);
			ctag_put_le32(&bad[172], ctag_crc32(0u, bad, 172u));
			CHECK(ctag_owner_record_decode(&back, bad, sizeof(bad)) == -EBADMSG);
		}
	}
}

/* ---- Tunnel fragments ---- */

void test_v2_tunnel_fragments(void)
{
	static const size_t sizes[] = {1u, 149u, 150u, 151u, 300u, 399u, 400u};
	uint8_t msg[CTAG_TUNNEL_MSG_MAX], buf[CTAG_TUNNEL_MSG_MAX];
	struct ctag_tunnel_rx rx;
	uint8_t seq, flags;
	size_t got;

	setup();
	for (size_t i = 0u; i < sizeof(msg); i++) {
		msg[i] = (uint8_t)(i * 7u);
	}
	ctag_tunnel_rx_init(&rx, buf, sizeof(buf));
	for (size_t s = 0u; s < V_COUNT(sizes); s++) {
		size_t len = sizes[s], off = 0u;
		int n, done = 0, frags = 0;

		while ((n = ctag_tunnel_frag(len, off, &seq, &flags)) > 0) {
			CHECK(seq == frags && ((flags & CTAG_TUNNEL_FRAG_START) != 0u) == (off == 0u));
			CHECK(((flags & CTAG_TUNNEL_FRAG_END) != 0u) == (off + (size_t)n == len));
			done = ctag_tunnel_rx_feed(&rx, seq, flags, &msg[off], (size_t)n, &got);
			off += (size_t)n;
			frags++;
		}
		CHECK(n == 0 && off == len && frags == (int)((len + 149u) / 150u));
		CHECK(done == 1 && got == len && memcmp(buf, msg, len) == 0);
	}
	CHECK(ctag_tunnel_frag(0u, 0u, &seq, &flags) == -EINVAL);
	CHECK(ctag_tunnel_frag(401u, 0u, &seq, &flags) == -EINVAL);
	CHECK(ctag_tunnel_frag(300u, 10u, &seq, &flags) == -EINVAL);
	/* a gap, a continuation without START, an overlong message */
	CHECK(ctag_tunnel_rx_feed(&rx, 0u, CTAG_TUNNEL_FRAG_START, msg, 150u, &got) == 0);
	CHECK(ctag_tunnel_rx_feed(&rx, 2u, CTAG_TUNNEL_FRAG_END, msg, 10u, &got) == -EBADMSG);
	CHECK(ctag_tunnel_rx_feed(&rx, 1u, CTAG_TUNNEL_FRAG_END, msg, 10u, &got) == -EBADMSG);
	CHECK(ctag_tunnel_rx_feed(&rx, 0u, CTAG_TUNNEL_FRAG_START, msg, 150u, &got) == 0);
	CHECK(ctag_tunnel_rx_feed(&rx, 1u, 0u, msg, 150u, &got) == 0);
	CHECK(ctag_tunnel_rx_feed(&rx, 2u, CTAG_TUNNEL_FRAG_END, msg, 101u, &got) == -EBADMSG);
	/* START restarts a message in progress; an empty single fragment */
	CHECK(ctag_tunnel_rx_feed(&rx, 0u, CTAG_TUNNEL_FRAG_START, msg, 5u, &got) == 0);
	CHECK(ctag_tunnel_rx_feed(&rx, 0u, CTAG_TUNNEL_FRAG_START | CTAG_TUNNEL_FRAG_END, msg, 3u,
				  &got) == 1 && got == 3u);
	CHECK(ctag_tunnel_rx_feed(&rx, 0u, CTAG_TUNNEL_FRAG_START | CTAG_TUNNEL_FRAG_END, msg, 0u,
				  &got) == 1 && got == 0u);
	/* secure message header */
	{
		struct ctag_secure_hdr h = {0x0C, 0x01, 0xBEEF}, back;
		uint8_t raw[4];

		ctag_secure_hdr_pack(&h, raw);
		CHECK(raw[0] == 0x0C && raw[1] == 0x01 && raw[2] == 0xEF && raw[3] == 0xBE);
		CHECK(ctag_secure_hdr_unpack(&back, raw, 4u) == 0 && back.request_id == 0xBEEFu);
		CHECK(ctag_secure_hdr_unpack(&back, raw, 3u) == -EBADMSG);
	}
}

/* ---- Heap and randomness failures ---- */

void test_v2_heap_failures(void)
{
	struct dut d;
	struct worker w;
	struct ctag_secure_heap_stats st;
	uint8_t buf[256];
	const uint8_t *pt;
	size_t pt_len;
	int err;
	int32_t n;

	setup();
	dut_init(&d, CTAG_NODE_ROLE_GATEWAY);
	worker_init(&w);
	ctag_secure_heap_reset_peak();
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
	ctag_secure_heap_stats(&st);
	CHECK(st.peak > 0u && st.peak <= st.size);
	ctag_noise_free(&w.nz);
	ctag_noise_free(&d.ep.noise);
	ctag_secure_close(&d.ep);
	/* Every allocation of a handshake (both sides share the heap) failing in
	 * turn: -ENOMEM, the heap reset, no session left on either side. */
	for (n = 0; n < 400; n++) {
		uint32_t failures;
		struct ctag_secure_answer ans;

		ctag_secure_heap_stats(&st);
		failures = st.failures;
		worker_init(&w);
		ctag_secure_heap_fail_after(n);
		err = connect_to(&w, &d, CTAG_LINK_SERIAL);
		ctag_secure_heap_fail_after(-1);
		ctag_secure_heap_stats(&st);
		if (err == 0) {
			break; /* enough allocations for the whole handshake */
		}
		CHECK(err == -ENOMEM && st.failures == failures + 1u && st.used == 0u);
		CHECK(!ctag_noise_open(&d.ep.noise) && !ctag_noise_open(&w.nz));
		CHECK(status_of(&d, &ans) == CTAG_STATUS_AUTH_REQUIRED && !d.ep.session);
	}
	CHECK(n > 5 && n < 400);
	/* the session works; sealing with the heap exhausted fails only the session */
	CHECK(ctag_noise_seal(&w.nz, (const uint8_t *)"ping", 4u, buf, sizeof(buf)) == 20);
	CHECK(ctag_secure_unseal(&d.ep, buf, 20u, &pt, &pt_len) == 0 && pt_len == 4u);
	ctag_secure_unseal_done(&d.ep);
	ctag_secure_heap_fail_after(0);
	CHECK(ctag_secure_seal(&d.ep, buf, 4u, buf, sizeof(buf)) == -ENOMEM);
	ctag_secure_heap_fail_after(-1);
	CHECK(!d.ep.session);
	CHECK(ctag_secure_seal(&d.ep, buf, 4u, buf, sizeof(buf)) == -ENOTCONN);
	/* both sides' objects are gone; a new handshake works */
	CHECK(!ctag_noise_open(&w.nz));
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
	/* no randomness: no weak ephemeral, the call fails (and the shared heap
	 * reset took the device's previous session too) */
	ctag_secure_rng_set(failing_rng, NULL);
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == -EIO);
	ctag_secure_rng_set(test_rng, NULL);
	CHECK(!ctag_secure_controller_match(&d.ep) && !ctag_noise_open(&d.ep.noise));
	CHECK(ctag_secure_seal(&d.ep, buf, 4u, buf, sizeof(buf)) == -ENOTCONN && !d.ep.session);
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
	/* the responder's own ephemeral missing */
	ctag_secure_rng_set(failing_rng, NULL);
	{
		uint8_t prologue[31], msg1[CTAG_SECURE_MSG1_LEN], msg2[CTAG_SECURE_MSG2_LEN];
		size_t plen = ctag_noise_prologue(CTAG_LINK_SERIAL, d.keys.device_id, prologue);

		ctag_secure_test_ephemeral(buf); /* the initiator's only */
		CHECK(ctag_noise_connect(&w.nz, w.priv, d.keys.ik_pub, prologue, plen, msg1) == 0);
		CHECK(ctag_secure_open(&d.ep, CTAG_LINK_SERIAL, msg1, sizeof(msg1), msg2) == -EIO);
		CHECK(!d.ep.session);
	}
	ctag_secure_rng_set(test_rng, NULL);
	CHECK(connect_to(&w, &d, CTAG_LINK_SERIAL) == 0);
	/* the endpoint's own randomness failing */
	d.ep.ops.random = failing_rng;
	{
		struct ctag_secure_answer ans;

		CHECK(status_of(&d, &ans) == CTAG_STATUS_INTERNAL);
		CHECK(!d.ep.has_challenge);
	}
	ctag_noise_free(&w.nz);
	ctag_noise_free(&d.ep.noise);
	ctag_secure_heap_stats(&st);
	CHECK(st.used == 0u);
}
