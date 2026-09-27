/* Serial server: framing, HELLO, credits, idempotency, retained events (1, 10). */
#include <string.h>

#include "common.h"

static void before(void *f)
{
	(void)f;
	core_reset();
}

ZTEST_SUITE(gw_serial, NULL, NULL, before, NULL, NULL);

static uint16_t hello(uint8_t proto, uint8_t credits)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_PROTO, proto),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, "t", 1u),
	};

	return host_send(CTAG_SERIAL_MSG_HELLO, f, 2u, credits);
}

static uint16_t ping(uint8_t credits)
{
	return host_send(CTAG_SERIAL_MSG_PING, NULL, 0u, credits);
}

/* One retained event: an ASSIGN_TAG answered by the bridge. */
static uint32_t make_retained_c(uint64_t op_id, uint32_t tag_id, uint8_t credits)
{
	static const uint8_t key[16];
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),   GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};

	(void)host_send(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u, credits);
	end_last(0);
	mesh_assign_status(BRIDGE_A, tag_id, 1u, CTAG_STATUS_OK);
	return core.s.seq;
}

static uint32_t make_retained(uint64_t op_id, uint32_t tag_id)
{
	return make_retained_c(op_id, tag_id, 1u);
}

ZTEST(gw_serial, test_hello_answers_caps_and_boot_id)
{
	struct ctag_cbor_field f[6] = {
		{.key = CTAG_CBOR_KEY_STATUS}, {.key = CTAG_CBOR_KEY_PROTO},
		{.key = CTAG_CBOR_KEY_FW},     {.key = CTAG_CBOR_KEY_BUILD},
		{.key = CTAG_CBOR_KEY_BOOT_ID}, {.key = CTAG_CBOR_KEY_CAPS},
	};
	struct ctag_cbor_field caps[6] = {
		{.key = CTAG_CBOR_KEY_MAX_FRAME}, {.key = CTAG_CBOR_KEY_CREDITS},
		{.key = CTAG_CBOR_KEY_ROLE},      {.key = CTAG_CBOR_KEY_BOARD},
		{.key = CTAG_CBOR_KEY_MAX_BRIDGES}, {.key = CTAG_CBOR_KEY_MAX_TAGS},
	};
	uint16_t rid = hello(CTAG_PROTO_VERSION, 0u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r);
	zassert_equal(r->h.type, CTAG_SERIAL_MSG_HELLO);
	zassert_equal(r->h.credits, 0u, "HELLO's grant byte stays 0");
	decode(r, f, 6u);
	zassert_equal(f[0].v.u, CTAG_STATUS_OK);
	zassert_equal(f[1].v.u, CTAG_PROTO_VERSION);
	zassert_mem_equal(f[2].v.str.ptr, "0.1.0", 5u);
	zassert_mem_equal(f[3].v.str.ptr, "test", 4u);
	zassert_equal(f[4].v.u, BOOT_ID);
	zassert_equal(ctag_cbor_decode(f[5].v.str.ptr, f[5].v.str.len, caps, 6u), 0);
	zassert_equal(caps[0].v.u, CTAG_SERIAL_MAX_FRAME);
	zassert_equal(caps[1].v.u, CONFIG_CTAG_GW_SERIAL_CREDITS);
	zassert_equal(caps[2].v.u, CTAG_NODE_ROLE_GATEWAY);
	zassert_equal(caps[3].v.u, 1u);
	zassert_equal(caps[4].v.u, CTAG_MAX_BRIDGES);
	zassert_equal(caps[5].v.u, CTAG_MAX_TAGS);
}

ZTEST(gw_serial, test_hello_version_mismatch_opens_no_session)
{
	uint16_t rid = hello(2u, 0u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r);
	zassert_equal(field_u(r, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_VERSION_MISMATCH);
	zassert_equal(field_u(r, CTAG_CBOR_KEY_PROTO), CTAG_PROTO_VERSION);
	rid = ping(1u);
	host_read();
	zassert_is_null(response(rid), "no session: nothing is answered");
}

ZTEST(gw_serial, test_requests_before_hello_are_ignored)
{
	uint16_t rid = ping(1u);

	zassert_equal(host_read(), 0u);
	zassert_is_null(response(rid));
	core_session();
	zassert_equal(counter("overruns"), 1u);
	zassert_equal(counter("hellos"), 1u);
}

ZTEST(gw_serial, test_ping_and_info)
{
	uint16_t rid;
	const struct frame *r;

	core_session();
	now_ms += 5000;
	rid = ping(1u);
	host_read();
	r = response(rid);
	zassert_not_null(r);
	zassert_equal(field_u(r, CTAG_CBOR_KEY_UPTIME_S), 5u);
	rid = host_send(CTAG_SERIAL_MSG_INFO, NULL, 0u, 1u);
	host_read();
	r = response(rid);
	zassert_not_null(r);
	zassert_equal(field_u(r, CTAG_CBOR_KEY_BOOT_ID), BOOT_ID);
	zassert_true(field_has(r, CTAG_CBOR_KEY_CAPS));
	zassert_true(field_has(r, CTAG_CBOR_KEY_COUNTERS));
	zassert_equal(counter("uart_rx_overflow"), 0u, "backend counters are appended");
}

ZTEST(gw_serial, test_unknown_and_maintenance_types_are_unsupported)
{
	core_session();
	zassert_equal(request_status(0x7Fu, NULL, 0u), CTAG_STATUS_UNSUPPORTED);
	zassert_equal(request_status(CTAG_SERIAL_MSG_FONT_STATUS, NULL, 0u),
		      CTAG_STATUS_UNSUPPORTED);
	zassert_equal(request_status(CTAG_SERIAL_MSG_EVT_RESULT, NULL, 0u), CTAG_STATUS_UNSUPPORTED);
	zassert_equal(counter("unsupported"), 3u);
}

ZTEST(gw_serial, test_malformed_payload_and_missing_field_are_invalid)
{
	static const uint8_t bad[] = {0xFF};
	struct ctag_cbor_field only_op = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 77u);
	uint8_t frame[64], wire[80];
	struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION,
				       .type = CTAG_SERIAL_MSG_PING,
				       .request_id = 900u,
				       .credits = 1u};
	int n;

	core_session();
	n = ctag_serial_frame_build(frame, sizeof(frame), &h, bad, sizeof(bad));
	n = ctag_serial_wire_encode(frame, (size_t)n, wire, sizeof(wire));
	host_bytes(wire, (size_t)n);
	host_read();
	zassert_not_null(response(900u));
	zassert_equal(field_u(response(900u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_INVALID);
	/* A DELIVER_LAYOUT with only its op_id: INVALID, and not remembered. */
	zassert_equal(request_status(CTAG_SERIAL_MSG_DELIVER_LAYOUT, &only_op, 1u),
		      CTAG_STATUS_INVALID);
	{
		uint8_t layout[20] = {0};
		uint16_t rid = send_deliver(77u, BRIDGE_A, 1u, layout, sizeof(layout));

		host_read();
		zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_ACCEPTED);
		zassert_false(field_has(response(rid), CTAG_CBOR_KEY_DETAIL));
	}
	zassert_equal(counter("invalid"), 2u);
}

ZTEST(gw_serial, test_frames_flagged_as_answers_are_ignored_and_bad_crc_counted)
{
	uint8_t frame[32], wire[48];
	struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION,
				       .type = CTAG_SERIAL_MSG_PING,
				       .request_id = 5u,
				       .flags = CTAG_SERIAL_FLAG_RESPONSE};
	int n;

	core_session();
	n = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, 0u);
	n = ctag_serial_wire_encode(frame, (size_t)n, wire, sizeof(wire));
	host_bytes(wire, (size_t)n);
	h.flags = 0u;
	h.request_id = 6u;
	n = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, 0u);
	frame[3] ^= 0x40u; /* corrupt after the CRC was computed */
	n = ctag_serial_wire_encode(frame, (size_t)n, wire, sizeof(wire));
	host_bytes(wire, (size_t)n);
	zassert_equal(host_read(), 0u);
	zassert_equal(counter("unexpected_frames"), 1u);
	zassert_equal(counter("crc_errors"), 1u);
}

ZTEST(gw_serial, test_answers_wait_for_credits_and_return_them)
{
	uint16_t rid[6];

	(void)hello(CTAG_PROTO_VERSION, 0u); /* the gateway may send SERIAL_DEFAULT_CREDITS */
	host_read();
	for (int i = 0; i < 4; i++) {
		rid[i] = ping(0u);
	}
	host_read();
	zassert_equal(rx_count, 4u);
	for (size_t i = 0; i < rx_count; i++) {
		zassert_equal(rx_frames[i].h.credits, 1u, "each answer returns its request's credit");
	}
	rid[4] = ping(0u);
	host_read();
	zassert_equal(rx_count, 0u, "no credit: the answer waits");
	rid[5] = ping(2u); /* grants 2 */
	host_read();
	zassert_not_null(response(rid[4]));
	zassert_not_null(response(rid[5]));
	zassert_equal(core.c.credit_violations, 0u);
	zassert_equal(core.c.overruns, 0u);
}

ZTEST(gw_serial, test_request_without_credit_and_no_slot_is_dropped)
{
	uint16_t rid[9];

	(void)hello(CTAG_PROTO_VERSION, 0u);
	host_read();
	for (int i = 0; i < 4; i++) {
		(void)ping(0u); /* answered; the gateway's credits are gone */
	}
	host_read();
	for (int i = 0; i < 4; i++) {
		rid[i] = ping(0u); /* fill the four answer slots */
	}
	rid[8] = ping(0u); /* the host had no credit and no slot is free */
	zassert_equal(core.c.overruns, 1u);
	zassert_equal(core.c.credit_violations, 1u);
	(void)ping(10u); /* grant: the four waiting answers go out (this one overruns too) */
	host_read();
	for (int i = 0; i < 4; i++) {
		zassert_not_null(response(rid[i]));
	}
	zassert_is_null(response(rid[8]));
}

ZTEST(gw_serial, test_hello_drops_unsent_answers_then_resends_retained)
{
	uint16_t lost, rid;
	uint32_t seq;

	(void)hello(CTAG_PROTO_VERSION, 0u);
	host_read();
	for (int i = 0; i < 4; i++) {
		(void)ping(0u);
	}
	host_read();
	lost = ping(0u); /* waits for a credit */
	seq = make_retained_c(1000u, 0xA1u, 0u);
	host_read();
	zassert_equal(rx_count, 0u);
	rid = hello(CTAG_PROTO_VERSION, 60u);
	host_read();
	zassert_true(rx_count >= 2u);
	zassert_equal(rx_frames[0].h.request_id, rid, "HELLO is answered first");
	zassert_is_null(response(lost), "unsent answers are dropped at HELLO");
	zassert_not_null(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0));
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_SEQ), seq);
}

ZTEST(gw_serial, test_retained_events_until_cumulative_ack)
{
	uint16_t rid;

	core_session();
	(void)make_retained(1u, 0x11u);
	(void)make_retained(2u, 0x12u);
	(void)make_retained(3u, 0x13u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 3u);
	for (size_t i = 0; i < 3u; i++) {
		const struct frame *e = event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, i);

		zassert_equal(field_u(e, CTAG_CBOR_KEY_SEQ), i + 1u);
		zassert_equal(e->h.request_id, 0u);
	}
	core_session(); /* HELLO again: every retained event is re-sent */
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 3u);
	rid = send_ack(2u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	core_session();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_SEQ), 3u);
	zassert_equal(counter("retained"), 1u);
}

ZTEST(gw_serial, test_retention_ring_overflow_drops_the_oldest)
{
	core_session();
	for (uint32_t i = 0; i < CTAG_SERIAL_EVENT_RETAIN + 1u; i++) {
		(void)make_retained(100u + i, 0x100u + i);
	}
	host_read();
	core_session();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT), CTAG_SERIAL_EVENT_RETAIN);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_SEQ), 2u);
	zassert_equal(counter("events_dropped"), 1u);
}

ZTEST(gw_serial, test_best_effort_events_need_a_credit)
{
	struct ctag_mesh_delivery_stage st = {.update_id = 5u, .tag_id = 6u, .revision = 7u,
					      .stage = CTAG_STAGE_TRANSFERRING};
	uint8_t p[CTAG_MESH_DELIVERY_STAGE_LEN];

	(void)ctag_mesh_delivery_stage_pack(&st, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_DELIVERY_STAGE, p, sizeof(p), now_ms);
	zassert_equal(core.c.events_discarded, 1u, "no session: discarded");
	core_session();
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_DELIVERY_STAGE, p, sizeof(p), now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_STAGE), 1u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_STAGE, 0), CTAG_CBOR_KEY_STAGE),
		      CTAG_STAGE_TRANSFERRING);
	zassert_false(field_has(event(CTAG_SERIAL_MSG_EVT_STAGE, 0), CTAG_CBOR_KEY_SEQ),
		      "best-effort events carry no seq");
}

ZTEST(gw_serial, test_repeated_op_id_returns_remembered_status)
{
	static const uint8_t key[16];
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 4242u), GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 9u),   GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};
	uint16_t rid;
	size_t sends;

	core_session();
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u), CTAG_STATUS_ACCEPTED);
	sends = mock.n_sent;
	rid = host_send(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u, 1u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_ACCEPTED);
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_DETAIL), CTAG_STATUS_DUPLICATE);
	zassert_equal(mock.n_sent, sends, "a duplicate does no new work");
	zassert_equal(counter("duplicate_ops"), 1u);
}

ZTEST(gw_serial, test_transient_refusals_are_not_remembered)
{
	static const uint8_t key[16];
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 0u),  GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 9u), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};
	uint16_t rid;

	core_session();
	for (uint32_t i = 0; i < CONFIG_CTAG_GW_MESH_OPS; i++) {
		f[0].v.u = 500u + i;
		f[2].v.u = 900u + i;
		zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u), CTAG_STATUS_ACCEPTED);
	}
	f[0].v.u = 777u;
	f[2].v.u = 777u;
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u), CTAG_STATUS_BUSY);
	/* One operation completes; the same op_id now succeeds. */
	end_last(0);
	mesh_assign_status(BRIDGE_A, 900u, 1u, CTAG_STATUS_OK);
	rid = host_send(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u, 1u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_ACCEPTED);
	zassert_false(field_has(response(rid), CTAG_CBOR_KEY_DETAIL));
}

ZTEST(gw_serial, test_idempotency_slots_forget_the_oldest)
{
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 0u),
				       GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, 99u)};
	uint16_t rid;

	core_session();
	for (uint32_t i = 1; i <= CTAG_SERIAL_IDEMPOTENCY_SLOTS + 1u; i++) {
		f[0].v.u = i;
		zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u),
			      CTAG_STATUS_NOT_FOUND);
	}
	f[0].v.u = 1u; /* evicted */
	rid = host_send(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u, 1u);
	host_read();
	zassert_false(field_has(response(rid), CTAG_CBOR_KEY_DETAIL));
	f[0].v.u = CTAG_SERIAL_IDEMPOTENCY_SLOTS + 1u;
	rid = host_send(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u, 1u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_DETAIL), CTAG_STATUS_DUPLICATE);
}

ZTEST(gw_serial, test_reboot_answers_then_resets)
{
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 31u);
	uint16_t rid;

	core_session();
	mock.tx_room = 3u; /* a slow UART: the answer leaves in pieces */
	rid = host_send(CTAG_SERIAL_MSG_REBOOT, &f, 1u, 1u);
	zassert_equal(mock.reboots, 0u, "not before the answer is out");
	for (int i = 0; i < 20 && mock.reboots == 0u; i++) {
		advance(1);
	}
	zassert_equal(mock.reboots, 1u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
}

ZTEST(gw_serial, test_reboot_resets_even_when_its_answer_is_stuck)
{
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 32u);

	(void)hello(CTAG_PROTO_VERSION, 0u);
	host_read();
	for (int i = 0; i < 4; i++) {
		(void)ping(0u);
	}
	host_read();
	(void)host_send(CTAG_SERIAL_MSG_REBOOT, &f, 1u, 0u);
	advance(GW_REBOOT_GRACE_MS - 1);
	zassert_equal(mock.reboots, 0u);
	advance(1);
	zassert_equal(mock.reboots, 1u);
}

ZTEST(gw_serial, test_frames_survive_partial_writes)
{
	uint16_t rid;

	core_session();
	mock.tx_room = 5u;
	rid = host_send(CTAG_SERIAL_MSG_INFO, NULL, 0u, 1u);
	for (int i = 0; i < 400; i++) {
		gw_core_poll(&core, now_ms);
	}
	host_read();
	zassert_not_null(response(rid));
	zassert_true(field_has(response(rid), CTAG_CBOR_KEY_COUNTERS));
}
