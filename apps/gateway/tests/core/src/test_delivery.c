/* Delivery engine: queue, one outstanding segmented send, BEGIN/CHUNK/COMMIT,
 * INCOMPLETE rounds, commit time-outs, results and de-duplication (3.2-3.4, 10). */
#include <string.h>

#include <psa/crypto.h>

#include "common.h"

static uint8_t layout[4096];

static void before(void *f)
{
	(void)f;
	core_reset();
	core_session();
	for (size_t i = 0; i < sizeof(layout); i++) {
		layout[i] = (uint8_t)(i * 7u + 1u);
	}
}

ZTEST_SUITE(gw_delivery, NULL, NULL, before, NULL, NULL);

static uint8_t deliver_status(uint64_t op_id, uint16_t bridge, uint64_t update_id, size_t len)
{
	uint16_t rid = send_deliver(op_id, bridge, update_id, layout, len);

	host_read();
	zassert_not_null(response(rid));
	return (uint8_t)field_u(response(rid), CTAG_CBOR_KEY_STATUS);
}

static const struct frame *result_for(uint64_t update_id)
{
	for (size_t i = 0; i < events_of(CTAG_SERIAL_MSG_EVT_RESULT); i++) {
		const struct frame *e = event(CTAG_SERIAL_MSG_EVT_RESULT, i);

		if (field_u(e, CTAG_CBOR_KEY_UPDATE_ID) == update_id) {
			return e;
		}
	}
	return NULL;
}

static size_t tagged_in_flight(void)
{
	return core.lane.active >= 0 && core.lane.tag != 0u ? 1u : 0u;
}

ZTEST(gw_delivery, test_begin_chunks_commit_with_digest)
{
	struct ctag_mesh_layout_begin b;
	uint8_t digest[32];
	size_t olen;
	const struct sent *s;
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 501u, 400u), CTAG_STATUS_ACCEPTED);
	s = last_sent(CTAG_MESH_OP_LAYOUT_BEGIN);
	zassert_not_null(s);
	zassert_equal(s->dst, BRIDGE_A);
	zassert_not_equal(s->tag, 0u, "segmented: its end is reported");
	zassert_equal(ctag_mesh_layout_begin_unpack(&b, s->data, s->len), 0);
	zassert_equal(b.total_len, 400u);
	zassert_equal(b.chunk_count, 3u);
	zassert_equal(b.update_id, 501u);
	zassert_equal(b.tag_id, 0xCAFE0001u);
	zassert_equal(b.epoch, 3u);
	zassert_equal(b.revision, 18u);
	zassert_equal(psa_hash_compute(PSA_ALG_SHA_256, layout, 400u, digest, 32u, &olen),
		      PSA_SUCCESS);
	zassert_mem_equal(b.digest, digest, 16u, "digest = SHA-256(layout)[0:16]");
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CHUNK), 0u, "no chunk before BEGIN's end");
	xfer = drive_to_commit();
	zassert_equal(xfer, b.xfer_id);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CHUNK), 3u);
	for (uint8_t i = 0; i < 3u; i++) {
		struct ctag_mesh_layout_chunk c = {0};
		const struct sent *cs = sent_at(1u + i);

		zassert_equal(cs->op, CTAG_MESH_OP_LAYOUT_CHUNK);
		zassert_equal(ctag_mesh_layout_chunk_unpack(&c, cs->data, cs->len), 0);
		zassert_equal(c.index, i);
		zassert_equal(c.xfer_id, b.xfer_id);
		zassert_equal(c.data_len, i < 2u ? 150u : 100u);
		zassert_mem_equal(c.data, &layout[i * 150u], c.data_len);
	}
	s = last_sent(CTAG_MESH_OP_LAYOUT_COMMIT);
	zassert_equal(s->tag, 0u, "LAYOUT_COMMIT is unsegmented");
	zassert_equal(s->len, CTAG_MESH_LAYOUT_COMMIT_LEN);
}

ZTEST(gw_delivery, test_one_segmented_send_outstanding_gateway_wide)
{
	static const uint8_t key[16];
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 70u), GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 5u), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};

	zassert_equal(deliver_status(1u, BRIDGE_A, 501u, 300u), CTAG_STATUS_ACCEPTED);
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, f, 5u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_ASSIGN_SET), 0u, "waits for LAYOUT_BEGIN's end");
	zassert_equal(tagged_in_flight(), 1u);
	end_last(0); /* BEGIN done: the queued ASSIGN_SET goes before the next chunk */
	zassert_equal(sent_count(CTAG_MESH_OP_ASSIGN_SET), 1u);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CHUNK), 0u);
	end_last(0);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CHUNK), 1u);
}

ZTEST(gw_delivery, test_failed_end_is_retried_three_times_then_timeout)
{
	const struct sent *first;

	zassert_equal(deliver_status(1u, BRIDGE_A, 502u, 200u), CTAG_STATUS_ACCEPTED);
	first = last_sent(CTAG_MESH_OP_LAYOUT_BEGIN);
	for (int i = 0; i < 3; i++) {
		end_last(-ETIMEDOUT);
		zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), (size_t)i + 2u);
		zassert_mem_equal(last_sent(CTAG_MESH_OP_LAYOUT_BEGIN)->data, first->data, first->len,
				  "the same message again");
	}
	end_last(-ETIMEDOUT);
	host_read();
	zassert_not_null(result_for(502u));
	zassert_equal(field_u(result_for(502u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_TIMEOUT);
	zassert_equal(core.c.mesh_send_retries, 3u);
	zassert_equal(core.c.mesh_send_failures, 1u);
}

ZTEST(gw_delivery, test_status_ok_then_bridge_result)
{
	struct ctag_cbor_field t[5] = {
		{.key = CTAG_CBOR_KEY_WAKE_MS},     {.key = CTAG_CBOR_KEY_MESH_MS},
		{.key = CTAG_CBOR_KEY_TRANSFER_MS}, {.key = CTAG_CBOR_KEY_REFRESH_MS},
		{.key = CTAG_CBOR_KEY_SUSPEND_MS},
	};
	struct ctag_cbor_field f[3] = {{.key = CTAG_CBOR_KEY_DIGEST},
				       {.key = CTAG_CBOR_KEY_TIMING},
				       {.key = CTAG_CBOR_KEY_BRIDGE}};
	const struct frame *e;
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 503u, 200u), CTAG_STATUS_ACCEPTED);
	xfer = drive_to_commit();
	now_ms += 1234;
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_OK, 0u);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_STAGE, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STAGE), CTAG_STAGE_BRIDGE_RECEIVED);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_UPDATE_ID), 503u);
	zassert_is_null(result_for(503u), "the bridge reports the outcome");
	mesh_result(BRIDGE_A, 7u, 503u, CTAG_STATUS_OK);
	zassert_equal(last_sent(CTAG_MESH_OP_RESULT_ACK)->dst, BRIDGE_A);
	zassert_equal(ctag_get_le16(last_sent(CTAG_MESH_OP_RESULT_ACK)->data), 7u);
	host_read();
	e = result_for(503u);
	zassert_not_null(e);
	zassert_true(field_has(e, CTAG_CBOR_KEY_SEQ), "EVT_RESULT is retained");
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_BATTERY_MV), 2900u);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_TAG_ID), 0xCAFE0001u);
	decode(e, f, 3u);
	zassert_equal(f[0].v.str.len, 8u);
	zassert_equal(f[0].v.str.ptr[0], 9u);
	zassert_equal(f[2].v.u, BRIDGE_A);
	zassert_equal(ctag_cbor_decode(f[1].v.str.ptr, f[1].v.str.len, t, 5u), 0);
	zassert_equal(t[0].v.u, 11u);
	zassert_equal(t[1].v.u, 1234u, "mesh_ms: transfer start to LAYOUT_STATUS");
	zassert_equal(t[2].v.u, 33u);
	zassert_equal(t[3].v.u, 44u);
	zassert_equal(t[4].v.u, 22u);
}

ZTEST(gw_delivery, test_results_are_deduplicated_by_bridge_and_seq)
{
	mesh_result(BRIDGE_A, 9u, 600u, CTAG_STATUS_OK);
	mesh_result(BRIDGE_A, 9u, 600u, CTAG_STATUS_OK); /* the bridge re-sent it */
	mesh_result(BRIDGE_B, 9u, 601u, CTAG_STATUS_OK); /* another bridge, same seq */
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_RESULT), 2u);
	zassert_equal(sent_count(CTAG_MESH_OP_RESULT_ACK), 3u, "every copy is acknowledged");
	zassert_equal(core.c.duplicate_results, 1u);
}

ZTEST(gw_delivery, test_incomplete_resends_exactly_the_missing_chunks)
{
	uint16_t xfer;
	size_t chunks;

	zassert_equal(deliver_status(1u, BRIDGE_A, 504u, 700u), CTAG_STATUS_ACCEPTED); /* 5 chunks */
	xfer = drive_to_commit();
	chunks = sent_count(CTAG_MESH_OP_LAYOUT_CHUNK);
	zassert_equal(chunks, 5u);
	for (uint8_t round = 1; round <= GW_INCOMPLETE_ROUNDS; round++) {
		struct ctag_mesh_layout_chunk c = {0};
		const struct sent *s;

		mesh_status(BRIDGE_A, xfer, CTAG_STATUS_INCOMPLETE, (1u << 1) | (1u << 3) | (1u << 20));
		zassert_equal(drive_to_commit(), xfer);
		zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CHUNK), chunks + 2u * round,
			      "bits beyond chunk_count are ignored");
		s = &mock.sent[mock.n_sent - 2u];
		zassert_equal(ctag_mesh_layout_chunk_unpack(&c, s->data, s->len), 0);
		zassert_equal(c.index, 3u);
	}
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_INCOMPLETE, 1u << 1);
	host_read();
	zassert_equal(field_u(result_for(504u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_INCOMPLETE);
	zassert_equal(core.c.chunks_resent, 6u);
}

ZTEST(gw_delivery, test_commit_resent_then_timeout)
{
	zassert_equal(deliver_status(1u, BRIDGE_A, 505u, 100u), CTAG_STATUS_ACCEPTED);
	(void)drive_to_commit();
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_COMMIT), 1u);
	for (int i = 1; i <= (int)GW_COMMIT_RESENDS; i++) {
		advance(CONFIG_CTAG_GW_STATUS_TIMEOUT_MS - 1);
		zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_COMMIT), (size_t)i);
		advance(1);
		zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_COMMIT), (size_t)i + 1u);
	}
	advance(CONFIG_CTAG_GW_STATUS_TIMEOUT_MS);
	host_read();
	zassert_equal(field_u(result_for(505u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_TIMEOUT);
	zassert_equal(core.c.commit_resends, 3u);
}

ZTEST(gw_delivery, test_rejection_becomes_the_result_with_zero_digest)
{
	static const uint8_t zero[8];
	struct ctag_cbor_field d = {.key = CTAG_CBOR_KEY_DIGEST};
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 506u, 100u), CTAG_STATUS_ACCEPTED);
	xfer = drive_to_commit();
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_DIGEST_MISMATCH, 0u);
	host_read();
	zassert_equal(field_u(result_for(506u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_DIGEST_MISMATCH);
	decode(result_for(506u), &d, 1u);
	zassert_equal(d.v.str.len, 8u);
	zassert_mem_equal(d.v.str.ptr, zero, 8u);
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_STAGE), 0u);
}

ZTEST(gw_delivery, test_duplicate_status_waits_for_the_bridge_result)
{
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 507u, 100u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(2u, BRIDGE_A, 508u, 100u), CTAG_STATUS_ACCEPTED);
	xfer = drive_to_commit();
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 1u, "one transfer at a time");
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_DUPLICATE, 0u);
	host_read();
	zassert_is_null(result_for(507u), "the bridge reports the outcome");
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_STAGE), 1u, "DUPLICATE is OK (10)");
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_STAGE, 0), CTAG_CBOR_KEY_STAGE),
		      CTAG_STAGE_BRIDGE_RECEIVED);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 2u, "the next transfer starts");
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_OK, 0u); /* stale xfer_id */
	zassert_equal(core.c.stale_status, 1u);
}

ZTEST(gw_delivery, test_lost_status_ok_recovered_by_the_recommit)
{
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 90u),
				       GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, 509u)};
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 509u, 300u), CTAG_STATUS_ACCEPTED);
	xfer = drive_to_commit();
	/* The bridge validated the layout and answered OK; the answer was lost. */
	advance(CONFIG_CTAG_GW_STATUS_TIMEOUT_MS);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_COMMIT), 2u, "the commit is re-sent");
	/* The bridge already holds that transfer: DUPLICATE, never NOT_FOUND. */
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_DUPLICATE, 0u);
	host_read();
	zassert_is_null(result_for(509u), "a lost status never ends the delivery");
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_STAGE), 1u);
	/* It is at the bridge: a cancel still reaches it, and its result is the one. */
	zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u), CTAG_STATUS_ACCEPTED);
	advance(3 * CONFIG_CTAG_GW_STATUS_TIMEOUT_MS);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_COMMIT), 2u, "no more commits");
	mesh_result(BRIDGE_A, 3u, 509u, CTAG_STATUS_OK);
	host_read();
	zassert_not_null(result_for(509u));
	zassert_equal(field_u(result_for(509u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_RESULT), 1u);
}

ZTEST(gw_delivery, test_result_while_waiting_for_the_status_ends_the_transfer)
{
	struct ctag_cbor_field t = {.key = CTAG_CBOR_KEY_MESH_MS};
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_TIMING};
	uint16_t xfer;

	zassert_equal(deliver_status(1u, BRIDGE_A, 510u, 200u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(2u, BRIDGE_A, 511u, 200u), CTAG_STATUS_ACCEPTED);
	xfer = drive_to_commit();
	now_ms += 700;
	/* LAYOUT_STATUS OK lost; the bridge already reports the outcome. */
	mesh_result(BRIDGE_A, 30u, 510u, CTAG_STATUS_OK);
	host_read();
	zassert_equal(field_u(result_for(510u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	decode(result_for(510u), &f, 1u);
	zassert_equal(ctag_cbor_decode(f.v.str.ptr, f.v.str.len, &t, 1u), 0);
	zassert_equal(t.v.u, 700u);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 2u, "the next transfer starts at once");
	advance(CONFIG_CTAG_GW_STATUS_TIMEOUT_MS);
	zassert_equal(core.c.commit_resends, 0u, "no re-commit of the finished transfer");
	mesh_status(BRIDGE_A, xfer, CTAG_STATUS_OK, 0u); /* the late status is stale */
	zassert_equal(core.c.stale_status, 1u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_RESULT), 0u);
}

ZTEST(gw_delivery, test_exactly_one_result_per_update_id)
{
	/* Two results from the bridge for one update_id (different result_seq). */
	mesh_result(BRIDGE_A, 20u, 1100u, CTAG_STATUS_SUPERSEDED);
	mesh_result(BRIDGE_A, 21u, 1100u, CTAG_STATUS_OK);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_RESULT), 1u);
	zassert_equal(field_u(result_for(1100u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_SUPERSEDED);
	zassert_equal(sent_count(CTAG_MESH_OP_RESULT_ACK), 2u, "both are acknowledged");
	zassert_equal(core.c.repeated_results, 1u);

	/* The gateway reported TIMEOUT; the bridge's late result is dropped. */
	zassert_equal(deliver_status(1u, BRIDGE_A, 1101u, 100u), CTAG_STATUS_ACCEPTED);
	(void)drive_to_commit();
	for (int i = 0; i < 1 + (int)GW_COMMIT_RESENDS; i++) {
		advance(CONFIG_CTAG_GW_STATUS_TIMEOUT_MS);
	}
	host_read();
	zassert_not_null(result_for(1101u));
	zassert_equal(field_u(result_for(1101u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_TIMEOUT);
	mesh_result(BRIDGE_A, 22u, 1101u, CTAG_STATUS_OK);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_RESULT), 0u);
	zassert_equal(last_sent(CTAG_MESH_OP_RESULT_ACK)->data[0], 22u);
	zassert_equal(counter("repeated_results"), 2u);
}

ZTEST(gw_delivery, test_queue_full_answers_busy_and_is_not_remembered)
{
	uint16_t rid;

	zassert_equal(deliver_status(1u, BRIDGE_A, 1u, 100u), CTAG_STATUS_ACCEPTED); /* active */
	for (uint32_t i = 0; i < CONFIG_CTAG_GW_DELIVERY_QUEUE; i++) {
		zassert_equal(deliver_status(10u + i, BRIDGE_A, 10u + i, 100u), CTAG_STATUS_ACCEPTED);
	}
	zassert_equal(deliver_status(99u, BRIDGE_A, 99u, 100u), CTAG_STATUS_BUSY);
	zassert_equal(counter("queue_depth"), CONFIG_CTAG_GW_DELIVERY_QUEUE);
	/* The active one ends; the retry with the same op_id is accepted. */
	mesh_status(BRIDGE_A, drive_to_commit(), CTAG_STATUS_OK, 0u);
	rid = send_deliver(99u, BRIDGE_A, 99u, layout, 100u);
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_ACCEPTED);
	zassert_false(field_has(response(rid), CTAG_CBOR_KEY_DETAIL));
}

ZTEST(gw_delivery, test_layout_arena_full_answers_busy)
{
	/* 8 KiB arena: two full-size layouts, then only small ones fit. */
	zassert_equal(deliver_status(1u, BRIDGE_A, 1201u, 4000u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(2u, BRIDGE_A, 1202u, 4000u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(3u, BRIDGE_A, 1203u, 4000u), CTAG_STATUS_BUSY);
	zassert_equal(deliver_status(4u, BRIDGE_A, 1204u, 100u), CTAG_STATUS_ACCEPTED);
	/* The first transfer ends: its bytes are free again, in order. */
	mesh_status(BRIDGE_A, drive_to_commit(), CTAG_STATUS_OK, 0u);
	zassert_equal(deliver_status(3u, BRIDGE_A, 1203u, 4000u), CTAG_STATUS_ACCEPTED);
	zassert_true(counter("layout_arena_used") > 8000u);
}

ZTEST(gw_delivery, test_unknown_or_unconfigured_bridge_is_not_found)
{
	zassert_equal(deliver_status(1u, 0x0009u, 1u, 100u), CTAG_STATUS_NOT_FOUND);
	zassert_equal(deliver_status(2u, BRIDGE_B, 2u, 100u), CTAG_STATUS_NOT_FOUND);
	zassert_equal(mock.n_sent, 0u);
}

ZTEST(gw_delivery, test_empty_and_oversized_layouts_are_refused)
{
	zassert_equal(deliver_status(1u, BRIDGE_A, 1u, 0u), CTAG_STATUS_INVALID);
	zassert_equal(deliver_status(2u, BRIDGE_A, 2u, CONFIG_CTAG_GW_LAYOUT_MAX + 1u),
		      CTAG_STATUS_TOO_LARGE);
	zassert_equal(deliver_status(3u, BRIDGE_A, 3u, CONFIG_CTAG_GW_LAYOUT_MAX),
		      CTAG_STATUS_ACCEPTED);
}

ZTEST(gw_delivery, test_cancel_queued_in_flight_and_unknown)
{
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 0u),
				       GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, 0u)};

	zassert_equal(deliver_status(1u, BRIDGE_A, 701u, 100u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(2u, BRIDGE_A, 702u, 100u), CTAG_STATUS_ACCEPTED);
	f[0].v.u = 50u;
	f[1].v.u = 702u; /* queued: dropped here, CANCELLED result */
	zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u), CTAG_STATUS_OK);
	host_read();
	zassert_equal(counter("queue_depth"), 0u);
	f[0].v.u = 51u;
	f[1].v.u = 701u; /* in transfer: LAYOUT_CANCEL to the bridge */
	zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u), CTAG_STATUS_ACCEPTED);
	zassert_equal(last_sent(CTAG_MESH_OP_LAYOUT_CANCEL)->dst, BRIDGE_A);
	zassert_equal(ctag_get_le64(last_sent(CTAG_MESH_OP_LAYOUT_CANCEL)->data), 701u);
	f[0].v.u = 52u;
	f[1].v.u = 703u;
	zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u), CTAG_STATUS_NOT_FOUND);
	/* The CANCELLED result of 702 was emitted (and is retained). */
	core_session();
	zassert_equal(field_u(result_for(702u), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_CANCELLED);
}

ZTEST(gw_delivery, test_cancel_reaches_a_layout_at_the_bridge)
{
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 60u),
				       GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, 801u)};

	zassert_equal(deliver_status(1u, BRIDGE_A, 801u, 100u), CTAG_STATUS_ACCEPTED);
	mesh_status(BRIDGE_A, drive_to_commit(), CTAG_STATUS_OK, 0u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_CANCEL_DELIVERY, f, 2u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_CANCEL), 1u);
}

ZTEST(gw_delivery, test_no_buffer_is_not_a_failed_send)
{
	mock.send_rc[0] = -ENOBUFS;
	mock.send_rc_n = 1u;
	zassert_equal(deliver_status(1u, BRIDGE_A, 901u, 100u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 0u);
	advance(GW_RETRY_MS);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 1u);
	zassert_equal(core.c.mesh_send_retries, 0u);
	zassert_equal(core.c.mesh_busy, 1u);
}

ZTEST(gw_delivery, test_deliveries_run_in_acceptance_order)
{
	struct ctag_mesh_layout_begin b;

	zassert_equal(deliver_status(1u, BRIDGE_A, 1001u, 100u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(2u, BRIDGE_A, 1002u, 100u), CTAG_STATUS_ACCEPTED);
	zassert_equal(deliver_status(3u, BRIDGE_A, 1003u, 100u), CTAG_STATUS_ACCEPTED);
	for (uint64_t u = 1001u; u <= 1003u; u++) {
		const struct sent *s = last_sent(CTAG_MESH_OP_LAYOUT_BEGIN);
		uint16_t xfer;

		zassert_equal(ctag_mesh_layout_begin_unpack(&b, s->data, s->len), 0);
		zassert_equal(b.update_id, u);
		xfer = drive_to_commit();
		zassert_equal(xfer, b.xfer_id);
		mesh_status(BRIDGE_A, xfer, CTAG_STATUS_OK, 0u);
	}
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_STAGE), 3u);
}
