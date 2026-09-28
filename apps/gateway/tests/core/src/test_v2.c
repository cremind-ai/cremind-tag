/*
 * Protocol v2 on the gateway (docs/connect-setup.md 4-6, CONFIG_CTAG_GW_SECURE):
 * the plaintext layer (IDENTIFY, SECURE_OPEN, SECURE_DATA, AUTH_REQUIRED),
 * the access table of 4.2, CLAIM / RECOVER / RELEASE / STATUS with real
 * grants, sealed answers and events, session loss, PROVISION with static OOB,
 * DISCOVER and mesh tunnels. The other suites run the whole v1 catalogue
 * through a session in this configuration.
 */
#include <errno.h>
#include <string.h>

#include "common.h"

static void before(void *f)
{
	(void)f;
	core_reset();
}

ZTEST_SUITE(gw_v2, NULL, NULL, before, NULL, NULL);

static uint16_t hello(uint8_t credits)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, "t", 1u),
	};

	host_forget();
	return host_send(CTAG_SERIAL_MSG_HELLO, f, 2u, credits);
}

static uint8_t plain_status(uint8_t type, const struct ctag_cbor_field *f, size_t n)
{
	uint16_t rid = host_send_plain(type, f, n, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no answer to 0x%02x", type);
	zassert_true(r->h.type == type, "answered as 0x%02x", r->h.type);
	return (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
}

static uint32_t make_retained(uint64_t op_id, uint32_t tag_id)
{
	static const uint8_t key[16];
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),   GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};

	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u), CTAG_STATUS_ACCEPTED);
	end_last(0);
	mesh_assign_status(BRIDGE_A, tag_id, 1u, CTAG_STATUS_OK);
	return core.s.seq;
}

/* ---- The plaintext layer ---- */

ZTEST(gw_v2, test_plaintext_v1_requests_need_a_session)
{
	static const uint8_t key[16];
	struct ctag_cbor_field assign[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 7u),    GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 9u),   GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};
	struct ctag_cbor_field op = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 8u);
	static const uint8_t types[] = {CTAG_SERIAL_MSG_INFO, CTAG_SERIAL_MSG_LIST_NODES,
					CTAG_SERIAL_MSG_GET_COUNTERS, CTAG_SERIAL_MSG_STATUS,
					CTAG_SERIAL_MSG_EVENT_ACK, 0x7Fu};

	(void)hello(60u);
	host_read();
	zassert_equal(plain_status(CTAG_SERIAL_MSG_PING, NULL, 0u), CTAG_STATUS_OK);
	for (size_t i = 0; i < ARRAY_SIZE(types); i++) {
		zassert_equal(plain_status(types[i], NULL, 0u), CTAG_STATUS_AUTH_REQUIRED);
	}
	zassert_equal(plain_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u),
		      CTAG_STATUS_AUTH_REQUIRED);
	zassert_equal(plain_status(CTAG_SERIAL_MSG_REBOOT, &op, 1u), CTAG_STATUS_AUTH_REQUIRED);
	zassert_equal(mock.n_sent, 0u, "nothing reached the mesh");
	zassert_equal(mock.reboots, 0u);
	/* SECURE_DATA without a session */
	{
		static const uint8_t junk[20] = {1};

		(void)host_send_secure_raw(junk, sizeof(junk), 1u);
		host_read();
		zassert_equal(rx_count, 1u);
		zassert_equal(rx_frames[0].h.type, CTAG_SERIAL_MSG_SECURE_DATA);
		zassert_equal(rx_frames[0].h.flags, CTAG_SERIAL_FLAG_RESPONSE);
		zassert_equal(field_u(&rx_frames[0], CTAG_CBOR_KEY_STATUS), CTAG_STATUS_AUTH_REQUIRED);
	}
	/* Nothing was remembered: the same op_id works inside a session. */
	zassert_equal(host_open(), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_ACCEPTED);
	zassert_equal(counter("auth_required"), 9u);
}

ZTEST(gw_v2, test_identify_answers_identity_and_fresh_challenges)
{
	struct ctag_cbor_field f[12] = {
		{.key = CTAG_CBOR_KEY_STATUS},      {.key = CTAG_CBOR_KEY_PROTO},
		{.key = CTAG_CBOR_KEY_ROLE},        {.key = CTAG_CBOR_KEY_DEVICE_ID},
		{.key = CTAG_CBOR_KEY_IK},          {.key = CTAG_CBOR_KEY_FW},
		{.key = CTAG_CBOR_KEY_BUILD},       {.key = CTAG_CBOR_KEY_BOARD},
		{.key = CTAG_CBOR_KEY_OWNER_STATE}, {.key = CTAG_CBOR_KEY_GEN},
		{.key = CTAG_CBOR_KEY_AUTHORITY_ID}, {.key = CTAG_CBOR_KEY_CHALLENGE},
	};
	uint8_t first[16], auth_pub[32], auth_id[16];
	uint16_t rid;

	(void)hello(60u);
	host_read();
	rid = host_send_plain(CTAG_SERIAL_MSG_IDENTIFY, NULL, 0u, 1u);
	host_read();
	zassert_not_null(response(rid));
	decode(response(rid), f, 12u);
	zassert_equal(f[0].v.u, CTAG_STATUS_OK);
	zassert_equal(f[1].v.u, CTAG_SECURE_PROTO_VERSION);
	zassert_equal(f[2].v.u, CTAG_NODE_ROLE_GATEWAY);
	zassert_mem_equal(f[3].v.str.ptr, core.v2.keys.device_id, 16u);
	zassert_mem_equal(f[4].v.str.ptr, core.v2.keys.ik_pub, 32u);
	zassert_mem_equal(f[5].v.str.ptr, "0.1.0", 5u);
	zassert_equal(f[7].v.u, 1u);
	zassert_equal(f[8].v.u, CTAG_OWNER_OWNED);
	zassert_equal(f[9].v.u, 1u);
	ctag_secure_ed25519_public(test_auth_sk, auth_pub);
	ctag_secure_authority_id(auth_pub, auth_id);
	zassert_true(f[10].present);
	zassert_mem_equal(f[10].v.str.ptr, auth_id, 16u);
	zassert_mem_equal(f[11].v.str.ptr, core.v2.ep.challenge, 16u);
	memcpy(first, f[11].v.str.ptr, 16u);
	/* A new challenge per IDENTIFY; the device_id is the one of 2.1. */
	rid = host_send_plain(CTAG_SERIAL_MSG_IDENTIFY, NULL, 0u, 1u);
	host_read();
	decode(response(rid), f, 12u);
	zassert_true(memcmp(f[11].v.str.ptr, first, 16u) != 0);
	{
		uint8_t id[16];

		ctag_secure_device_id(CTAG_NODE_ROLE_GATEWAY, core.v2.keys.ik_pub, id);
		zassert_mem_equal(id, core.v2.keys.device_id, 16u);
	}
	/* Unowned: no authority_id */
	core_reset_unowned();
	(void)hello(60u);
	host_read();
	rid = host_send_plain(CTAG_SERIAL_MSG_IDENTIFY, NULL, 0u, 1u);
	host_read();
	decode(response(rid), f, 12u);
	zassert_equal(f[8].v.u, CTAG_OWNER_UNOWNED);
	zassert_false(f[10].present);
}

ZTEST(gw_v2, test_secure_open_failures)
{
	struct ctag_cbor_field bad = GW_F_BSTR(CTAG_CBOR_KEY_DATA, (const uint8_t *)"short", 5u);
	uint8_t msg1[CTAG_SECURE_MSG1_LEN] = {1, 2, 3};
	struct ctag_cbor_field junk = GW_F_BSTR(CTAG_CBOR_KEY_DATA, msg1, sizeof(msg1));

	(void)hello(60u);
	host_read();
	zassert_equal(plain_status(CTAG_SERIAL_MSG_SECURE_OPEN, NULL, 0u), CTAG_STATUS_INVALID);
	zassert_equal(plain_status(CTAG_SERIAL_MSG_SECURE_OPEN, &bad, 1u), CTAG_STATUS_AUTH_FAILED);
	zassert_equal(plain_status(CTAG_SERIAL_MSG_SECURE_OPEN, &junk, 1u), CTAG_STATUS_AUTH_FAILED);
	zassert_false(core.v2.ep.session);
	/* The heap exhausted on the gateway's side: NO_RESOURCES, the gateway runs on. */
	{
		struct ctag_secure_heap_stats before, after;

		ctag_secure_heap_stats(&before);
		zassert_equal(host_open_fail_after(3), CTAG_STATUS_NO_RESOURCES);
		ctag_secure_heap_stats(&after);
		zassert_equal(after.failures, before.failures + 1u);
		zassert_equal(after.used, 0u, "the heap was reset");
	}
	zassert_false(core.v2.ep.session);
	zassert_equal(host_open(), CTAG_STATUS_OK);
	zassert_equal(counter("secure_failures"), 3u);
	zassert_equal(counter("secure_opens"), 1u);
}

static int no_rng(void *ctx, uint8_t *buf, size_t len)
{
	(void)ctx;
	(void)buf;
	(void)len;
	return -EIO;
}

/* No randomness: INTERNAL, never a weak challenge or ephemeral; the gateway runs on. */
ZTEST(gw_v2, test_rng_failures_answer_internal)
{
	(void)hello(60u);
	host_read();
	/* the challenge of IDENTIFY */
	mock.random_fail = true;
	zassert_equal(plain_status(CTAG_SERIAL_MSG_IDENTIFY, NULL, 0u), CTAG_STATUS_INTERNAL);
	zassert_false(core.v2.ep.has_challenge);
	mock.random_fail = false;
	/* the responder's ephemeral of SECURE_OPEN (message 1 made with randomness) */
	{
		struct ctag_noise nz = {0};
		uint8_t prologue[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN];
		uint8_t msg1[CTAG_SECURE_MSG1_LEN];
		size_t plen = ctag_noise_prologue(CTAG_LINK_SERIAL, core.v2.keys.device_id, prologue);
		struct ctag_cbor_field f = GW_F_BSTR(CTAG_CBOR_KEY_DATA, msg1, sizeof(msg1));

		zassert_ok(ctag_noise_connect(&nz, test_worker, core.v2.keys.ik_pub, prologue, plen,
					      msg1));
		ctag_secure_rng_set(no_rng, NULL);
		zassert_equal(plain_status(CTAG_SERIAL_MSG_SECURE_OPEN, &f, 1u), CTAG_STATUS_INTERNAL);
		zassert_false(core.v2.ep.session);
		ctag_noise_free(&nz);
	}
	core_reset(); /* the test RNG again */
	/* the challenge of STATUS, inside a session */
	core_session();
	mock.random_fail = true;
	zassert_equal(request_status(CTAG_SERIAL_MSG_STATUS, NULL, 0u), CTAG_STATUS_INTERNAL);
	mock.random_fail = false;
	zassert_equal(request_status(CTAG_SERIAL_MSG_STATUS, NULL, 0u), CTAG_STATUS_OK);
}

ZTEST(gw_v2, test_decrypt_failure_ends_the_session)
{
	uint8_t ct[64];
	int n;

	core_session();
	zassert_equal(request_status(CTAG_SERIAL_MSG_PING, NULL, 0u), CTAG_STATUS_OK);
	n = host_seal(CTAG_SERIAL_MSG_PING, 0u, 77u, NULL, 0u, ct, sizeof(ct));
	zassert_true(n > 0);
	ct[n - 1] ^= 0x01u; /* the tag no longer verifies */
	(void)host_send_secure_raw(ct, (size_t)n, 1u);
	host_read();
	zassert_equal(rx_count, 1u);
	zassert_equal(rx_frames[0].h.type, CTAG_SERIAL_MSG_SECURE_DATA);
	zassert_equal(rx_frames[0].h.flags, CTAG_SERIAL_FLAG_RESPONSE);
	zassert_equal(field_u(&rx_frames[0], CTAG_CBOR_KEY_STATUS), CTAG_STATUS_AUTH_REQUIRED);
	zassert_false(core.v2.ep.session);
	/* A replayed message is refused the same way (the nonce moved on). */
	host_forget();
	zassert_equal(host_open(), CTAG_STATUS_OK);
	n = host_seal(CTAG_SERIAL_MSG_PING, 0u, 78u, NULL, 0u, ct, sizeof(ct));
	(void)host_send_secure_raw(ct, (size_t)n, 1u);
	host_read();
	zassert_not_null(response(78u));
	(void)host_send_secure_raw(ct, (size_t)n, 1u);
	host_read();
	zassert_equal(field_u(&rx_frames[0], CTAG_CBOR_KEY_STATUS), CTAG_STATUS_AUTH_REQUIRED);
	zassert_equal(rx_frames[0].h.flags, CTAG_SERIAL_FLAG_RESPONSE);
	host_forget();
	zassert_equal(host_open(), CTAG_STATUS_OK);
	zassert_equal(counter("decrypt_failures"), 2u);
}

ZTEST(gw_v2, test_hello_drops_the_session_and_events_wait_for_the_next)
{
	uint32_t seq;
	static const uint8_t junk[20];

	core_session();
	seq = make_retained(100u, 0x51u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 1u);
	/* HELLO: no session; SECURE_DATA answers AUTH_REQUIRED; no event is sent. */
	(void)hello(60u);
	host_read();
	zassert_false(core.v2.ep.session);
	(void)host_send_secure_raw(junk, sizeof(junk), 1u);
	host_read();
	zassert_equal(rx_count, 1u);
	zassert_equal(field_u(&rx_frames[0], CTAG_CBOR_KEY_STATUS), CTAG_STATUS_AUTH_REQUIRED);
	/* SECURE_OPEN by the pinned worker: the retained event comes again, sealed. */
	zassert_equal(host_open(), CTAG_STATUS_OK);
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_SEQ), seq);
}

ZTEST(gw_v2, test_answers_of_an_old_session_never_reach_a_new_one)
{
	uint16_t owed;

	/* A session whose host granted only what HELLO gave: the gateway's
	 * credits run out and the next answers wait. */
	(void)hello(0u);
	host_read();
	zassert_equal(host_open_credits(0u), CTAG_STATUS_OK);
	for (int i = 0; i < 8 && core.s.resp_count == 0u; i++) {
		(void)host_send(CTAG_SERIAL_MSG_PING, NULL, 0u, 0u);
		host_read();
	}
	zassert_equal(core.s.resp_count, 1u, "one sealed answer is waiting");
	owed = core.s.owed;
	/* Another computer opens a session: the waiting answer was for the old
	 * one and is dropped (host_read would fail on a frame sealed for it);
	 * its credit comes back with the SECURE_OPEN answer. */
	host_use_worker(test_other);
	zassert_equal(host_open(), CTAG_STATUS_OK);
	zassert_equal(core.s.resp_count, 0u);
	zassert_equal(rx_count, 1u, "only the SECURE_OPEN answer");
	zassert_equal(rx_frames[0].h.credits, (uint8_t)(owed + 2u), "both credits returned");
	zassert_equal(request_status(CTAG_SERIAL_MSG_PING, NULL, 0u), CTAG_STATUS_OK);
}

ZTEST(gw_v2, test_inner_answer_flags_are_ignored_and_the_credit_returns)
{
	uint8_t ct[64];
	int n;

	core_session();
	n = host_seal(CTAG_SERIAL_MSG_PING, CTAG_SERIAL_FLAG_RESPONSE, 5u, NULL, 0u, ct, sizeof(ct));
	(void)host_send_secure_raw(ct, (size_t)n, 1u);
	host_read();
	zassert_equal(rx_count, 0u);
	zassert_equal(core.s.owed, 1u, "the frame's credit is owed back");
	zassert_true(core.v2.ep.session);
	zassert_equal(counter("unexpected_frames"), 1u);
}

/* ---- The access table and the ownership messages ---- */

ZTEST(gw_v2, test_unowned_gateway_serves_only_info_ping_status_claim)
{
	static const uint8_t key[16];
	struct ctag_cbor_field assign[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 70u),   GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 9u),   GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};

	core_reset_unowned();
	core_session();
	zassert_true(host_sealed());
	zassert_equal(request_status(CTAG_SERIAL_MSG_PING, NULL, 0u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_INFO, NULL, 0u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_STATUS, NULL, 0u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_LIST_NODES, NULL, 0u), CTAG_STATUS_NOT_OWNER);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_NOT_OWNER);
	zassert_equal(request_status(CTAG_SERIAL_MSG_GET_COUNTERS, NULL, 0u), CTAG_STATUS_NOT_OWNER);
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_RECOVER, CTAG_GRANT_OP_RECOVER, 0u),
		      CTAG_STATUS_NOT_OWNER);
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_RELEASE, CTAG_GRANT_OP_RELEASE, 0u),
		      CTAG_STATUS_NOT_OWNER);
	/* Bridge / tag messages are not the gateway's. */
	zassert_equal(request_status(CTAG_SERIAL_MSG_PAIR, NULL, 0u), CTAG_STATUS_UNSUPPORTED);
	/* ... nor are the plaintext layer's own messages, sealed. */
	{
		static uint8_t ct[64];
		int len = host_seal(CTAG_SERIAL_MSG_SECURE_OPEN, 0u, 0x7777u, NULL, 0u, ct, sizeof(ct));

		zassert_true(len > 0);
		(void)host_send_secure_raw(ct, (size_t)len, 1u);
		host_read();
		zassert_not_null(response(0x7777u));
		zassert_equal(field_u(response(0x7777u), CTAG_CBOR_KEY_STATUS),
			      CTAG_STATUS_UNSUPPORTED);
	}
	zassert_equal(mock.n_sent, 0u);
	/* CLAIM: owned, pinned; the whole catalogue opens. */
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 0u),
		      CTAG_STATUS_OK);
	zassert_equal(core.v2.ep.rec.state, CTAG_OWNER_OWNED);
	zassert_equal(core.v2.ep.rec.gen, 1u);
	zassert_equal(mock.owner_writes, 1u);
	zassert_equal(mock.stored_floor, 1u);
	{
		struct ctag_owner_record r;

		zassert_ok(ctag_owner_record_decode(&r, mock.stored_owner, sizeof(mock.stored_owner)));
		zassert_equal(r.state, CTAG_OWNER_OWNED);
		zassert_mem_equal(r.owner, test_owner, 16u);
	}
	zassert_equal(request_status(CTAG_SERIAL_MSG_LIST_NODES, NULL, 0u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_ACCEPTED);
	zassert_equal(counter("claims"), 1u);
	zassert_equal(counter("not_owner"), 5u);
}

ZTEST(gw_v2, test_claim_answer_fields_and_grant_rules)
{
	uint8_t challenge[16], grant[CTAG_GRANT_MAX], sig[64], other_pub[32];
	size_t len;
	uint16_t rid;

	core_reset_unowned();
	core_session();
	/* A grant for another controller: GRANT_INVALID, the challenge used up. */
	host_challenge(challenge);
	ctag_secure_x25519_public(test_other, other_pub);
	host_grant(CTAG_GRANT_OP_CLAIM, other_pub, challenge, 0u, grant, &len, sig);
	{
		struct ctag_cbor_field f[2] = {GW_F_BSTR(CTAG_CBOR_KEY_GRANT, grant, len),
					       GW_F_BSTR(CTAG_CBOR_KEY_SIG, sig, 64u)};

		zassert_equal(request_status(CTAG_SERIAL_MSG_CLAIM, f, 2u), CTAG_STATUS_GRANT_INVALID);
		zassert_false(core.v2.ep.has_challenge);
		/* missing field: INVALID */
		zassert_equal(request_status(CTAG_SERIAL_MSG_CLAIM, f, 1u), CTAG_STATUS_INVALID);
	}
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 5u),
		      CTAG_STATUS_STALE_GENERATION);
	/* The answer: {status, gen} */
	host_challenge(challenge);
	{
		uint8_t pub[32];

		ctag_secure_x25519_public(test_worker, pub);
		host_grant(CTAG_GRANT_OP_CLAIM, pub, challenge, 0u, grant, &len, sig);
	}
	{
		struct ctag_cbor_field f[2] = {GW_F_BSTR(CTAG_CBOR_KEY_GRANT, grant, len),
					       GW_F_BSTR(CTAG_CBOR_KEY_SIG, sig, 64u)};

		rid = host_send(CTAG_SERIAL_MSG_CLAIM, f, 2u, 1u);
	}
	host_read();
	zassert_not_null(response(rid));
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_GEN), 1u);
	/* CLAIM again (owned): NOT_OWNER through the grant rules */
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 1u),
		      CTAG_STATUS_NOT_OWNER);
	/* The store failing: STORAGE_ERROR and nothing changes. */
	mock.owner_fail = true;
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_RECOVER, CTAG_GRANT_OP_RECOVER, 1u),
		      CTAG_STATUS_STORAGE_ERROR);
	zassert_equal(core.v2.ep.rec.gen, 1u);
}

ZTEST(gw_v2, test_status_fields)
{
	struct ctag_cbor_field f[8] = {
		{.key = CTAG_CBOR_KEY_STATUS},       {.key = CTAG_CBOR_KEY_OWNER_STATE},
		{.key = CTAG_CBOR_KEY_GEN},          {.key = CTAG_CBOR_KEY_AUTHORITY_ID},
		{.key = CTAG_CBOR_KEY_OWNER},        {.key = CTAG_CBOR_KEY_CONTROLLER_MATCH},
		{.key = CTAG_CBOR_KEY_CHALLENGE},    {.key = CTAG_CBOR_KEY_ROOT_PROOF},
	};
	uint16_t rid;

	core_session();
	rid = host_send(CTAG_SERIAL_MSG_STATUS, NULL, 0u, 1u);
	host_read();
	decode(response(rid), f, 8u);
	zassert_equal(f[0].v.u, CTAG_STATUS_OK);
	zassert_equal(f[1].v.u, CTAG_OWNER_OWNED);
	zassert_equal(f[2].v.u, 1u);
	zassert_true(f[3].present && f[4].present);
	zassert_mem_equal(f[4].v.str.ptr, test_owner, 16u);
	zassert_true(f[5].present && f[5].v.b);
	zassert_mem_equal(f[6].v.str.ptr, core.v2.ep.challenge, 16u);
	zassert_false(f[7].present, "root_proof is a tag's");
	/* another controller: controller_match false, the authority but not the owner */
	host_use_worker(test_other);
	core_session();
	rid = host_send(CTAG_SERIAL_MSG_STATUS, NULL, 0u, 1u);
	host_read();
	decode(response(rid), f, 8u);
	zassert_equal(f[0].v.u, CTAG_STATUS_OK);
	zassert_equal(f[1].v.u, CTAG_OWNER_OWNED);
	zassert_true(f[3].present, "authority_id to anyone");
	zassert_false(f[4].present, "the owner only to the pinned controller");
	zassert_true(f[5].present && !f[5].v.b);
}

ZTEST(gw_v2, test_other_controller_recovers_and_gets_the_events)
{
	static const uint8_t key[16];
	struct ctag_cbor_field assign[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 71u),   GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 9u),   GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};
	uint32_t seq;

	core_session();
	seq = make_retained(200u, 0x61u);
	host_read();
	/* Another computer: INFO, PING, STATUS, RECOVER; no events. */
	host_use_worker(test_other);
	core_session();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 0u, "no events for it");
	zassert_equal(request_status(CTAG_SERIAL_MSG_INFO, NULL, 0u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_NOT_OWNER);
	zassert_equal(request_status(CTAG_SERIAL_MSG_EVENT_ACK, NULL, 0u), CTAG_STATUS_NOT_OWNER);
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 1u),
		      CTAG_STATUS_NOT_OWNER);
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_RELEASE, CTAG_GRANT_OP_RELEASE, 1u),
		      CTAG_STATUS_NOT_OWNER);
	/* RECOVER: the new controller is pinned, and the retained event follows. */
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_RECOVER, CTAG_GRANT_OP_RECOVER, 1u),
		      CTAG_STATUS_OK);
	zassert_equal(core.v2.ep.rec.gen, 2u);
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_SEQ), seq);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_ACCEPTED);
	/* The old worker is only "another controller" now. */
	host_use_worker(test_worker);
	core_session();
	zassert_equal(request_status(CTAG_SERIAL_MSG_LIST_NODES, NULL, 0u), CTAG_STATUS_NOT_OWNER);
}

ZTEST(gw_v2, test_release_wipes_and_reboots)
{
	uint16_t rid;
	uint8_t challenge[16], grant[CTAG_GRANT_MAX], sig[64], pub[32];
	size_t len;

	core_session();
	(void)make_retained(300u, 0x71u);
	host_read();
	host_challenge(challenge);
	ctag_secure_x25519_public(test_worker, pub);
	host_grant(CTAG_GRANT_OP_RELEASE, pub, challenge, 1u, grant, &len, sig);
	{
		struct ctag_cbor_field f[2] = {GW_F_BSTR(CTAG_CBOR_KEY_GRANT, grant, len),
					       GW_F_BSTR(CTAG_CBOR_KEY_SIG, sig, 64u)};

		mock.tx_room = 3u; /* the answer leaves slowly */
		rid = host_send(CTAG_SERIAL_MSG_RELEASE, f, 2u, 1u);
	}
	zassert_equal(mock.releases, 1u, "the network is wiped");
	zassert_equal(core.v2.ep.rec.state, CTAG_OWNER_UNOWNED);
	zassert_equal(core.v2.ep.rec.gen, 2u, "the generation moves on");
	zassert_equal(gw_node_count(&core), 0u);
	zassert_equal(core.assign_count, 0u);
	zassert_equal(core.s.ret_count, 0u, "the previous owner's events are gone");
	zassert_equal(mock.reboots, 0u, "not before the answer is out");
	for (int i = 0; i < 400 && mock.reboots == 0u; i++) {
		advance(1);
	}
	zassert_equal(mock.reboots, 1u);
	host_read();
	zassert_not_null(response(rid));
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_GEN), 2u);
	zassert_true(field_has(response(rid), CTAG_CBOR_KEY_DATA));
	/* The record the next boot reads */
	{
		struct ctag_owner_record r;

		zassert_ok(ctag_owner_record_decode(&r, mock.stored_owner, sizeof(mock.stored_owner)));
		zassert_equal(r.state, CTAG_OWNER_UNOWNED);
		zassert_equal(r.gen, 2u);
		zassert_equal(mock.stored_floor, 2u);
	}
}

ZTEST(gw_v2, test_boot_rule_keeps_the_generation)
{
	uint8_t raw[CTAG_OWNER_RECORD_LEN];
	struct ctag_owner_record r = {.state = CTAG_OWNER_OWNED, .gen = 4u};

	ctag_owner_record_encode(&r, raw);
	raw[10] ^= 0x01u; /* corrupt */
	core_reset_record(raw, sizeof(raw), 6u);
	zassert_equal(core.v2.ep.rec.state, CTAG_OWNER_UNOWNED);
	zassert_equal(core.v2.ep.rec.gen, 6u);
	core_session();
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 0u),
		      CTAG_STATUS_STALE_GENERATION);
	zassert_equal(host_grant_request(CTAG_SERIAL_MSG_CLAIM, CTAG_GRANT_OP_CLAIM, 6u),
		      CTAG_STATUS_OK);
	zassert_equal(core.v2.ep.rec.gen, 7u);
	/* A good record below the floor: the floor wins. */
	r.state = CTAG_OWNER_UNOWNED;
	r.gen = 2u;
	ctag_owner_record_encode(&r, raw);
	core_reset_record(raw, sizeof(raw), 9u);
	zassert_equal(core.v2.ep.rec.gen, 9u);
}

/* ---- PROVISION with static OOB ---- */

ZTEST(gw_v2, test_provision_requires_static_oob)
{
	static const uint8_t uuid[16] = {0xC0, 0xFF, 0xEE, 1};
	static const uint8_t oob[32] = {0x5A, 0xA5};
	struct ctag_cbor_field f[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 90u),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, 16u),
		GW_F_BSTR(CTAG_CBOR_KEY_STATIC_OOB, oob, 32u),
	};
	const struct frame *e;

	core_session();
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, f, 2u), CTAG_STATUS_INVALID);
	zassert_equal(mock.n_provision, 0u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, f, 3u), CTAG_STATUS_ACCEPTED);
	zassert_equal(mock.n_provision, 1u);
	zassert_true(mock.provision_oob_set[0]);
	zassert_mem_equal(mock.provision_oob[0], oob, 32u);
	/* The device offered no static OOB: SECURITY_CONFIG */
	gw_core_prov_link_open(&core, now_ms);
	gw_core_prov_security(&core, now_ms);
	gw_core_prov_closed(&core, now_ms);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_SECURITY_CONFIG);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_ADDR), 0u);
	zassert_equal(counter("prov_security"), 1u);
}

/* A wrong static OOB fails the confirmation: the link closes without the node
 * after the capabilities, which is SECURITY_CONFIG (final), never TIMEOUT. */
ZTEST(gw_v2, test_provision_wrong_static_oob_is_security_config)
{
	static const uint8_t uuid[16] = {0xC0, 0xFF, 0xEE, 2};
	static const uint8_t oob[32] = {0x11};
	struct ctag_cbor_field f[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 91u),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, 16u),
		GW_F_BSTR(CTAG_CBOR_KEY_STATIC_OOB, oob, 32u),
	};
	const struct frame *e;

	core_session();
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, f, 3u), CTAG_STATUS_ACCEPTED);
	gw_core_prov_link_open(&core, now_ms);
	gw_core_prov_auth(&core, now_ms);
	gw_core_prov_closed(&core, now_ms);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_SECURITY_CONFIG);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_ADDR), 0u);
	zassert_equal(counter("prov_security"), 1u);
	zassert_equal(gw_node_count(&core), 2u, "nothing added");

	/* The exchange began and then the deadline passed: SECURITY_CONFIG too. */
	f[0] = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 92u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, f, 3u), CTAG_STATUS_ACCEPTED);
	gw_core_prov_link_open(&core, now_ms);
	gw_core_prov_auth(&core, now_ms);
	advance(CONFIG_CTAG_GW_PROVISION_TIMEOUT_MS + 1);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_SECURITY_CONFIG);
	gw_core_prov_closed(&core, now_ms); /* the stack's own close, later: nothing more */

	/* The link opened but the device never sent its capabilities: TIMEOUT. */
	f[0] = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 93u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, f, 3u), CTAG_STATUS_ACCEPTED);
	gw_core_prov_link_open(&core, now_ms);
	gw_core_prov_closed(&core, now_ms);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_TIMEOUT);
	zassert_equal(counter("prov_security"), 2u);
}

/* ---- DISCOVER ---- */

static void mesh_discovered(uint16_t src, uint32_t tag_id, int8_t rssi)
{
	struct ctag_mesh_discovered d = {.tag_id = tag_id, .rssi = rssi, .flags = CTAG_ADV_FLAG_SETUP};
	uint8_t p[CTAG_MESH_DISCOVERED_LEN];

	(void)ctag_mesh_discovered_pack(&d, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_DISCOVERED, p, sizeof(p), now_ms);
}

static uint8_t discover(uint64_t op_id, uint16_t bridge, uint32_t duration, uint32_t tag_id)
{
	struct ctag_cbor_field f[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, duration),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id),
	};

	return request_status(CTAG_SERIAL_MSG_DISCOVER, f, 4u);
}

ZTEST(gw_v2, test_discover_forwards_rate_limited_candidates)
{
	const struct sent *s;
	const struct frame *e;

	core_session();
	zassert_equal(discover(1u, 0u, 121u, 0u), CTAG_STATUS_INVALID);
	zassert_equal(discover(2u, BRIDGE_B, 30u, 0u), CTAG_STATUS_NOT_FOUND, "not configured");
	/* bridge 0: every configured bridge */
	zassert_equal(discover(3u, 0u, 30u, 0x1234u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_DISCOVER), 1u);
	s = last_sent(CTAG_MESH_OP_DISCOVER);
	zassert_equal(s->dst, BRIDGE_A);
	zassert_equal(s->len, CTAG_MESH_DISCOVER_LEN);
	zassert_equal(s->data[0], 30u);
	zassert_equal(ctag_get_le32(&s->data[1]), 0x1234u);
	mesh_discovered(BRIDGE_A, 0x1234u, -61);
	mesh_discovered(BRIDGE_A, 0x1234u, -60); /* within 5 s: limited */
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 1u);
	e = event(CTAG_SERIAL_MSG_EVT_DISCOVERED, 0);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_BRIDGE), BRIDGE_A);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_TAG_ID), 0x1234u);
	zassert_equal((int64_t)field_u(e, CTAG_CBOR_KEY_RSSI), -61);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_FLAGS), CTAG_ADV_FLAG_SETUP);
	zassert_false(field_has(e, CTAG_CBOR_KEY_SEQ), "not retained");
	advance(CTAG_DISCOVERED_MIN_INTERVAL_MS);
	mesh_discovered(BRIDGE_A, 0x1234u, -59);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 1u);
	/* after the window: dropped */
	advance(40000);
	mesh_discovered(BRIDGE_A, 0x1234u, -58);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 0u);
	zassert_equal(counter("discovered_limited"), 1u);
	/* a repeated op_id does no new work */
	zassert_equal(discover(3u, 0u, 30u, 0x1234u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_DISCOVER), 1u);
}

/* ---- Tunnels ---- */

static uint16_t tunnel_open(uint64_t op_id, uint16_t bridge, uint32_t tag_id, uint32_t duration,
			    uint8_t *status)
{
	struct ctag_cbor_field f[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, duration),
	};
	uint16_t rid = host_send(CTAG_SERIAL_MSG_TUNNEL_OPEN, f, 4u, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r);
	*status = (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
	return field_has(r, CTAG_CBOR_KEY_TUNNEL) ? (uint16_t)field_u(r, CTAG_CBOR_KEY_TUNNEL) : 0u;
}

static uint8_t tunnel_send(uint32_t tunnel, const uint8_t *data, size_t len)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, tunnel),
		GW_F_BSTR(CTAG_CBOR_KEY_DATA, data, len),
	};

	return request_status(CTAG_SERIAL_MSG_TUNNEL_SEND, f, 2u);
}

static void tunnel_up(uint16_t src, uint16_t tunnel, uint8_t seq, uint8_t flags,
		      const uint8_t *data, size_t len)
{
	struct ctag_mesh_tunnel_up m = {
		.tunnel = tunnel, .seq = seq, .flags = flags, .data = data, .data_len = len};
	uint8_t p[CTAG_MESH_TUNNEL_UP_MAX_LEN];
	int n = ctag_mesh_tunnel_up_pack(&m, p, sizeof(p));

	zassert_true(n > 0);
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_TUNNEL_UP, p, (size_t)n, now_ms);
}

ZTEST(gw_v2, test_tunnel_round_trip)
{
	static uint8_t msg[CTAG_TUNNEL_MSG_MAX];
	struct ctag_cbor_field f[6] = {
		{.key = CTAG_CBOR_KEY_TUNNEL}, {.key = CTAG_CBOR_KEY_BRIDGE},
		{.key = CTAG_CBOR_KEY_TAG_ID}, {.key = CTAG_CBOR_KEY_STATE},
		{.key = CTAG_CBOR_KEY_DATA},   {.key = CTAG_CBOR_KEY_STATUS},
	};
	uint8_t status, again;
	uint16_t t, dup;
	const struct sent *s;
	size_t first;

	for (size_t i = 0; i < sizeof(msg); i++) {
		msg[i] = (uint8_t)(i * 3u + 1u);
	}
	core_session();
	t = tunnel_open(10u, BRIDGE_A, 0xAABBCCDDu, 30u, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	zassert_true(t != 0u);
	s = last_sent(CTAG_MESH_OP_TUNNEL_OPEN);
	zassert_not_null(s);
	zassert_equal(s->dst, BRIDGE_A);
	zassert_equal(ctag_get_le16(s->data), t);
	zassert_equal(ctag_get_le32(&s->data[2]), 0xAABBCCDDu);
	zassert_equal(s->data[6], 30u);
	zassert_equal(s->tag, 0u, "unsegmented");
	/* a repeat of the op_id: the same tunnel, DUPLICATE */
	dup = tunnel_open(10u, BRIDGE_A, 0xAABBCCDDu, 30u, &again);
	zassert_equal(dup, t);
	zassert_equal(again, CTAG_STATUS_OK);
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_OPEN), 1u);
	/* one tunnel per bridge; bad arguments */
	(void)tunnel_open(11u, BRIDGE_A, 0u, 30u, &status);
	zassert_equal(status, CTAG_STATUS_BUSY);
	(void)tunnel_open(12u, BRIDGE_B, 0u, 30u, &status);
	zassert_equal(status, CTAG_STATUS_NOT_FOUND);
	(void)tunnel_open(13u, BRIDGE_A, 0u, 256u, &status);
	zassert_equal(status, CTAG_STATUS_INVALID);

	/* The endpoint's ident2 comes up in two fragments: EVT_TUNNEL OPEN. */
	tunnel_up(BRIDGE_A, t, 0u, CTAG_TUNNEL_FRAG_START, msg, 60u);
	tunnel_up(BRIDGE_A, t, 1u, CTAG_TUNNEL_FRAG_END, &msg[60], 31u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	decode(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), f, 6u);
	zassert_equal(f[0].v.u, t);
	zassert_equal(f[1].v.u, BRIDGE_A);
	zassert_equal(f[2].v.u, 0xAABBCCDDu);
	zassert_equal(f[3].v.u, CTAG_TUNNEL_OPEN);
	zassert_equal(f[4].v.str.len, 91u);
	zassert_mem_equal(f[4].v.str.ptr, msg, 91u);
	zassert_false(f[5].present);

	/* TUNNEL_SEND of 400 bytes: three TUNNEL_DATA, one segmented send at a time. */
	first = mock.n_sent;
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	zassert_equal(tunnel_send(t, msg, 10u), CTAG_STATUS_BUSY, "one message at a time");
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_DATA), 1u);
	for (int k = 0; k < 3; k++) {
		const struct sent *d = last_sent(CTAG_MESH_OP_TUNNEL_DATA);

		zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_DATA), (size_t)k + 1u);
		zassert_equal(ctag_get_le16(d->data), t);
		zassert_equal(d->data[2], k);
		zassert_equal(d->data[3], (k == 0 ? CTAG_TUNNEL_FRAG_START : 0u) |
						  (k == 2 ? CTAG_TUNNEL_FRAG_END : 0u));
		zassert_equal(d->len, 4u + (k < 2 ? 150u : 100u));
		zassert_mem_equal(&d->data[4], &msg[k * 150], d->len - 4u);
		zassert_true(d->tag != 0u, "segmented, through the lane");
		end_tag(d->tag, 0);
	}
	zassert_true(mock.n_sent >= first + 3u);
	zassert_equal(tunnel_send(t, msg, 10u), CTAG_STATUS_OK, "free again");
	end_last(0);
	zassert_equal(tunnel_send(t, msg, CTAG_TUNNEL_MSG_MAX + 1u), CTAG_STATUS_TOO_LARGE);
	zassert_equal(tunnel_send(0x7777u, msg, 10u), CTAG_STATUS_NOT_FOUND);

	/* Later messages up: EVT_TUNNEL DATA; a gap drops the message. */
	tunnel_up(BRIDGE_A, t, 0u, CTAG_TUNNEL_FRAG_START | CTAG_TUNNEL_FRAG_END, msg, 20u);
	tunnel_up(BRIDGE_A, t, 0u, CTAG_TUNNEL_FRAG_START, msg, 150u);
	tunnel_up(BRIDGE_A, t, 2u, CTAG_TUNNEL_FRAG_END, msg, 10u); /* seq 1 missing */
	tunnel_up(BRIDGE_B, t, 0u, CTAG_TUNNEL_FRAG_START | CTAG_TUNNEL_FRAG_END, msg, 5u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	decode(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), f, 6u);
	zassert_equal(f[3].v.u, CTAG_TUNNEL_DATA);
	zassert_equal(f[4].v.str.len, 20u);
	zassert_equal(counter("tunnel_gaps"), 1u);

	/* The bridge closes it: EVT_TUNNEL CLOSED with its status. */
	{
		const uint8_t st = CTAG_STATUS_BUSY;

		tunnel_up(BRIDGE_A, t, 0u, CTAG_TUNNEL_FRAG_CLOSE, &st, 1u);
	}
	host_read();
	decode(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), f, 6u);
	zassert_equal(f[3].v.u, CTAG_TUNNEL_CLOSED);
	zassert_equal(f[5].v.u, CTAG_STATUS_BUSY);
	zassert_false(f[4].present);
	zassert_equal(tunnel_send(t, msg, 10u), CTAG_STATUS_NOT_FOUND);
}

ZTEST(gw_v2, test_tunnel_close_timeout_and_failed_send)
{
	uint8_t status;
	uint16_t t;
	const struct sent *s;
	static const uint8_t msg[40] = {1};
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, 0u);

	core_session();
	/* TUNNEL_CLOSE: the mesh close, no event */
	t = tunnel_open(20u, BRIDGE_A, 0u, 10u, &status);
	f.v.u = t;
	zassert_equal(request_status(CTAG_SERIAL_MSG_TUNNEL_CLOSE, &f, 1u), CTAG_STATUS_OK);
	s = last_sent(CTAG_MESH_OP_TUNNEL_CLOSE);
	zassert_not_null(s);
	zassert_equal(ctag_get_le16(s->data), t);
	zassert_equal(s->data[2], CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_TUNNEL_CLOSE, &f, 1u), CTAG_STATUS_NOT_FOUND);
	/* idle for timeout + grace: closed with TIMEOUT */
	t = tunnel_open(21u, BRIDGE_A, 0u, 10u, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	advance(10000 + 4999);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	advance(1);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
	zassert_equal(ctag_get_le16(last_sent(CTAG_MESH_OP_TUNNEL_CLOSE)->data), t);
	/* a message whose send fails for good closes the tunnel */
	t = tunnel_open(22u, BRIDGE_A, 0u, 30u, &status);
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	for (int i = 0; i < 4; i++) {
		end_last(-EIO);
	}
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), CTAG_CBOR_KEY_STATE),
		      CTAG_TUNNEL_CLOSED);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
	/* both slots usable again */
	t = tunnel_open(23u, BRIDGE_A, 0u, 30u, &status);
	zassert_equal(status, CTAG_STATUS_OK);
}

ZTEST(gw_v2, test_caps2_status_reaches_the_inventory)
{
	struct ctag_mesh_caps2_status c = {.device_id = {0xD0, 1, 2}, .gen = 3u, .owner_state = 1u};
	uint8_t p[CTAG_MESH_CAPS2_STATUS_LEN];
	struct ctag_cbor_field items = {.key = CTAG_CBOR_KEY_ITEMS};
	struct ctag_cbor_str item[4];
	struct ctag_cbor_field f[4] = {{.key = CTAG_CBOR_KEY_ADDR}, {.key = CTAG_CBOR_KEY_DEVICE_ID},
				       {.key = CTAG_CBOR_KEY_GEN}, {.key = CTAG_CBOR_KEY_OWNER_STATE}};
	uint16_t rid;

	core_session();
	(void)ctag_mesh_caps2_status_pack(&c, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_CAPS2_STATUS, p, sizeof(p), now_ms);
	rid = host_send(CTAG_SERIAL_MSG_GET_INVENTORY, NULL, 0u, 1u);
	host_read();
	decode(response(rid), &items, 1u);
	zassert_equal(ctag_cbor_maps(&items.v.str, item, 4u), 2);
	zassert_ok(ctag_cbor_decode(item[0].ptr, item[0].len, f, 4u));
	zassert_equal(f[0].v.u, BRIDGE_A);
	zassert_true(f[1].present);
	zassert_mem_equal(f[1].v.str.ptr, c.device_id, 16u);
	zassert_equal(f[2].v.u, 3u);
	zassert_equal(f[3].v.u, 1u);
	zassert_ok(ctag_cbor_decode(item[1].ptr, item[1].len, f, 4u));
	zassert_false(f[1].present, "the other bridge sent no CAPS2");
}
