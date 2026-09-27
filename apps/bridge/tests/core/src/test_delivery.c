/*
 * Delivery core: LAYOUT_COMMIT validation in the order of docs/protocol.md
 * 3.3, the DUPLICATE / SUPERSEDED / CLEAR rules of 10, pending layouts that
 * survive a reset, and DELIVERY_RESULT retries with a persisted result_seq.
 */
#include <string.h>

#include "common.h"

#define TAG   0x0A0B0C0Du
#define TAG2  0x01020304u
#define EPOCH 3u

static uint8_t big[CTAG_LAYOUT_HARD_MAX];

static void boot_with_pack(void)
{
	env_fresh(1000u);
	install_fixture_pack();
	assign(&benv.dlv, TAG, EPOCH, NULL);
	sent_clear();
}

static struct xfer x(uint16_t xfer_id, uint32_t revision, uint64_t update_id)
{
	return (struct xfer){.xfer_id = xfer_id, .tag_id = TAG, .epoch = EPOCH,
			     .revision = revision, .update_id = update_id};
}

static struct dlv_job *job_of(uint32_t tag_id)
{
	return dlv_next_job(&benv.dlv, tag_id, 0u, UINT32_MAX);
}

ZTEST(bridge_delivery, test_commit_order_transfer_checks)
{
	struct xfer t = x(1, 1, 100);
	struct ctag_mesh_layout_begin b = {.xfer_id = 9, .total_len = 4097, .chunk_count = 28};
	uint32_t missing = 0;
	size_t n = big_layout(big);

	boot_with_pack();
	/* 1. no transfer with this xfer_id */
	zassert_equal(dlv_layout_commit(&benv.dlv, 77, 0, &missing), CTAG_STATUS_NOT_FOUND);
	/* 2. INCOMPLETE with the missing bitmap; the resent chunks complete it */
	zassert_equal(deliver(&benv.dlv, &t, big, n, BIT(3) | BIT(27), 0, &missing),
		      CTAG_STATUS_INCOMPLETE);
	zassert_equal(missing, BIT(3) | BIT(27));
	for (int i = 0; i < 28; i++) {
		struct ctag_mesh_layout_chunk c = {
			.xfer_id = 1, .index = (uint8_t)i, .data = &big[i * 150],
			.data_len = MIN(150u, n - i * 150u),
		};

		if (missing & BIT(i)) {
			dlv_layout_chunk(&benv.dlv, &c);
		}
	}
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_OK,
		      "a LAYOUT_HARD_MAX layout is accepted");
	/* A chunk index outside the transfer (or >= 32) is ignored. */
	struct ctag_mesh_layout_chunk stray = {.xfer_id = 1, .index = 40, .data = big,
					       .data_len = 10};
	uint32_t strays = benv.dlv.c.stray_chunks;

	dlv_layout_chunk(&benv.dlv, &stray);
	zassert_equal(benv.dlv.c.stray_chunks, strays + 1);

	/* 3. total_len above LAYOUT_HARD_MAX */
	dlv_layout_begin(&benv.dlv, &b);
	for (int i = 0; i < 28; i++) {
		struct ctag_mesh_layout_chunk c = {.xfer_id = 9, .index = (uint8_t)i,
						   .data = big, .data_len = 150};

		dlv_layout_chunk(&benv.dlv, &c);
	}
	zassert_equal(dlv_layout_commit(&benv.dlv, 9, 0, &missing), CTAG_STATUS_TOO_LARGE);
	/* 4. a short chunk other than the last */
	t = x(2, 2, 101);
	b = (struct ctag_mesh_layout_begin){.xfer_id = 2, .tag_id = TAG, .epoch = EPOCH,
					    .revision = 2, .total_len = 200, .chunk_count = 2};
	dlv_layout_begin(&benv.dlv, &b);
	struct ctag_mesh_layout_chunk c0 = {.xfer_id = 2, .index = 0, .data = big, .data_len = 149};
	struct ctag_mesh_layout_chunk c1 = {.xfer_id = 2, .index = 1, .data = big, .data_len = 51};

	dlv_layout_chunk(&benv.dlv, &c0);
	dlv_layout_chunk(&benv.dlv, &c1);
	zassert_equal(dlv_layout_commit(&benv.dlv, 2, 0, &missing), CTAG_STATUS_INVALID);
	/* 5. digest */
	b.chunk_count = 1;
	b.total_len = (uint16_t)small_layout_len;
	memset(b.digest, 0, sizeof(b.digest));
	dlv_layout_begin(&benv.dlv, &b);
	c0.data = small_layout;
	c0.data_len = small_layout_len;
	dlv_layout_chunk(&benv.dlv, &c0);
	zassert_equal(dlv_layout_commit(&benv.dlv, 2, 0, &missing), CTAG_STATUS_DIGEST_MISMATCH);
}

ZTEST(bridge_delivery, test_commit_order_bridge_checks)
{
	struct xfer t = x(1, 5, 200);
	uint8_t other_pack[8] = {1, 2, 3, 4, 5, 6, 7, 8};

	boot_with_pack();
	/* 6. assignment and epoch */
	t.tag_id = TAG2;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_NOT_ASSIGNED);
	t.tag_id = TAG;
	t.epoch = EPOCH + 1;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_NOT_ASSIGNED, "a newer epoch than assigned");
	t.epoch = EPOCH - 1;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_STALE_EPOCH);
	t.epoch = EPOCH;
	/* 8. font pack id before 9. layout checks (revision history empty) */
	t.fontpack_id = other_pack;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_FONTPACK_MISMATCH);
	t.fontpack_id = NULL;
	/* 9. layout bounds, then strikes against the active pack */
	static const uint8_t bad[] = {0x43, 0x4C, 2, 0, 0x90, 1, 0x2C, 1, 0, 0, 0, 0};

	zassert_equal(deliver(&benv.dlv, &t, bad, sizeof(bad), 0, 0, NULL),
		      CTAG_STATUS_UNSUPPORTED, "version 2");
	uint8_t missing_strike[32];

	(void)big_layout(big);
	memcpy(missing_strike, big, 12); /* header of a 400 x 300 layout */
	/* GLYPHS from strike (1, 32), which the fixture pack lacks. */
	missing_strike[10] = 1; /* cmd_count = 1 */
	missing_strike[11] = 0;
	static const uint8_t glyphs32[] = {CTAG_LAYOUT_CMD_GLYPHS, 1, 0, 32, 1, 10, 0, 20, 0, 0};

	memcpy(&missing_strike[12], glyphs32, sizeof(glyphs32));
	zassert_equal(deliver(&benv.dlv, &t, missing_strike, 22, 0, 0, NULL),
		      CTAG_STATUS_FONTPACK_MISMATCH);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	/* Accepted, then 7. revision rules */
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	t.revision = 4;
	t.update_id = 201;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_STALE_REVISION);
	t.revision = 5;
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL),
		      CTAG_STATUS_STALE_REVISION, "same revision, other digest");
	/* A revision check precedes the pack check. */
	t.fontpack_id = other_pack;
	t.revision = 3;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_STALE_REVISION);
	zassert_equal(sent_count(CTAG_MESH_OP_DELIVERY_RESULT), 0);
}

ZTEST(bridge_delivery, test_duplicate_rules)
{
	struct xfer t = x(1, 7, 300);
	struct ctag_mesh_delivery_result r;
	struct dlv_report tm = {.wake_ms = 1, .suspend_ms = 2, .transfer_ms = 3, .refresh_ms = 4,
				.stored_epoch = 0x01020304u, .flags = CTAG_RESULT_FLAG_DUPLICATE};
	uint8_t d8[8] = {9, 8, 7, 6, 5, 4, 3, 2};
	struct dlv_job *job;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	/* A repeated commit after acceptance (lost LAYOUT_STATUS): DUPLICATE. */
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &(uint32_t){0}), CTAG_STATUS_DUPLICATE);
	/* Still pending: a re-delivery adopts the new update_id. */
	t.xfer_id = 2;
	t.update_id = 301;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_DUPLICATE);
	job = job_of(TAG);
	zassert_not_null(job);
	zassert_equal(job->update_id, 301);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1);
	/* ... which survives a reset (the record was rewritten). */
	env_boot(5000u);
	job = job_of(TAG);
	zassert_not_null(job);
	zassert_equal(job->update_id, 301);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1);
	/* Displayed: the stored result is re-sent under the next update_id. */
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, d8, 2900, &tm, 6000u);
	zassert_true(last_result(301, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(r.stored_epoch, 0x01020304u);
	zassert_equal(r.flags, CTAG_RESULT_FLAG_DUPLICATE);
	/* The stored result, report included, survives a reset (history record). */
	env_boot(6500u);
	t.xfer_id = 3;
	t.update_id = 302;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 7000, NULL),
		      CTAG_STATUS_DUPLICATE);
	zassert_true(last_result(302, &r));
	zassert_equal(r.status, CTAG_STATUS_OK);
	zassert_equal(r.revision, 7);
	zassert_mem_equal(r.digest, d8, 8);
	zassert_equal(r.battery_mv, 2900);
	zassert_equal(r.refresh_ms, 4);
	zassert_equal(r.stored_epoch, 0x01020304u);
	zassert_equal(r.flags, CTAG_RESULT_FLAG_DUPLICATE);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0, "no new work for a displayed revision");
	/* Ended without being displayed: accepted again as a re-delivery. */
	t.revision = 8;
	t.xfer_id = 4;
	t.update_id = 303;
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL), CTAG_STATUS_OK);
	dlv_finish(&benv.dlv, job_of(TAG), CTAG_STATUS_DISPLAY_STATE_UNKNOWN, NULL, 0, NULL, 0);
	t.xfer_id = 5;
	t.update_id = 304;
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL), CTAG_STATUS_OK);
	zassert_equal(job_of(TAG)->update_id, 304);
}

ZTEST(bridge_delivery, test_superseded_and_cancel)
{
	struct xfer t = x(1, 1, 400);
	struct ctag_mesh_delivery_result r;
	struct dlv_job *job;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	t = x(2, 2, 401);
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL), 0);
	zassert_true(last_result(400, &r));
	zassert_equal(r.status, CTAG_STATUS_SUPERSEDED);
	zassert_equal(r.revision, 1);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1);
	/* A job in a session is not superseded; the newer one waits beside it. */
	job = job_of(TAG);
	job->flags |= DLV_JOB_IN_SESSION;
	t = x(3, 3, 402);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	zassert_equal(results_for(401), 0);
	zassert_equal(dlv_queue_depth(&benv.dlv), 2);
	/* LAYOUT_CANCEL: only the job outside the session. */
	dlv_layout_cancel(&benv.dlv, 401, 0);
	zassert_equal(results_for(401), 0, "in session: not cancelled");
	dlv_layout_cancel(&benv.dlv, 402, 0);
	zassert_true(last_result(402, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1);
}

ZTEST(bridge_delivery, test_assignments)
{
	struct ctag_mesh_assign_set set = {.tag_id = TAG, .epoch = EPOCH, .flags = 1};
	struct ctag_mesh_assign_del del = {.tag_id = TAG2, .epoch = 1};
	struct xfer t = x(1, 1, 500);
	struct ctag_mesh_delivery_result r;

	boot_with_pack();
	zassert_equal(dlv_assign_del(&benv.dlv, &del, 0), CTAG_STATUS_OK, "absent: OK (10)");
	set.epoch = EPOCH - 1;
	zassert_equal(dlv_assign_set(&benv.dlv, &set, 0), CTAG_STATUS_STALE_EPOCH);
	set.epoch = EPOCH;
	zassert_equal(dlv_assign_set(&benv.dlv, &set, 0), CTAG_STATUS_OK, "same epoch again");
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	/* A newer epoch cancels the older epoch's work. */
	set.epoch = EPOCH + 1;
	zassert_equal(dlv_assign_set(&benv.dlv, &set, 0), CTAG_STATUS_OK);
	zassert_true(last_result(500, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED);
	del.tag_id = TAG;
	del.epoch = EPOCH;
	zassert_equal(dlv_assign_del(&benv.dlv, &del, 0), CTAG_STATUS_STALE_EPOCH);
	del.epoch = EPOCH + 1;
	zassert_equal(dlv_assign_del(&benv.dlv, &del, 0), CTAG_STATUS_OK);
	zassert_is_null(dlv_assignment(&benv.dlv, TAG));
	/* The table holds MAX_TAGS_PER_BRIDGE tags. */
	for (uint32_t i = 0; i < CONFIG_CTAG_BRIDGE_MAX_TAGS; i++) {
		set.tag_id = 100 + i;
		zassert_equal(dlv_assign_set(&benv.dlv, &set, 0), CTAG_STATUS_OK);
	}
	set.tag_id = 99;
	zassert_equal(dlv_assign_set(&benv.dlv, &set, 0), CTAG_STATUS_NO_RESOURCES);
	zassert_equal(dlv_assigned_count(&benv.dlv), CONFIG_CTAG_BRIDGE_MAX_TAGS);
	/* Assignments (with K_epoch) survive a reset. */
	env_boot(0);
	zassert_equal(dlv_assigned_count(&benv.dlv), CONFIG_CTAG_BRIDGE_MAX_TAGS);
	zassert_equal(dlv_assignment(&benv.dlv, 105)->epoch, EPOCH + 1);
}

ZTEST(bridge_delivery, test_tag_commands)
{
	struct ctag_mesh_tag_cmd m = {.update_id = 600, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_IDENTIFY};
	struct ctag_mesh_delivery_result r;
	struct xfer t = x(1, 9, 610);
	struct dlv_job *job;

	boot_with_pack();
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_true(last_result(600, &r));
	zassert_equal(r.status, CTAG_STATUS_UNSUPPORTED, "IDENTIFY is companion-level");
	m.update_id = 601;
	m.cmd = CTAG_TAG_CMD_REFRESH;
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_true(last_result(601, &r));
	zassert_equal(r.status, CTAG_STATUS_UNSUPPORTED);
	m.update_id = 602;
	m.epoch = EPOCH + 1;
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_true(last_result(602, &r));
	zassert_equal(r.status, CTAG_STATUS_NOT_ASSIGNED);
	m.update_id = 603;
	m.epoch = EPOCH - 1;
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_true(last_result(603, &r));
	zassert_equal(r.status, CTAG_STATUS_STALE_EPOCH);
	/* A displayed revision, then CLEAR: the history returns to revision 0, so
	 * the previously shown revision is drawn again rather than answered from
	 * history (10). */
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	dlv_finish(&benv.dlv, job_of(TAG), CTAG_STATUS_OK, NULL, 0, NULL, 0);
	m.update_id = 604;
	m.epoch = EPOCH;
	m.cmd = CTAG_TAG_CMD_CLEAR;
	dlv_tag_cmd(&benv.dlv, &m, 0);
	job = job_of(TAG);
	zassert_not_null(job);
	zassert_equal(job->kind, DLV_CMD);
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	t.xfer_id = 2;
	t.update_id = 611;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
}

ZTEST(bridge_delivery, test_pending_survives_reset)
{
	struct xfer t = x(1, 1, 700);
	struct dlv_job *job;
	size_t n = big_layout(big);

	boot_with_pack();
	assign(&benv.dlv, TAG2, 1, NULL);
	zassert_equal(deliver(&benv.dlv, &t, big, n, 0, 0, NULL), 0);
	t = (struct xfer){.xfer_id = 2, .tag_id = TAG2, .epoch = 1, .revision = 4, .update_id = 701};
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	/* A finished job is not restored. */
	t = x(3, 2, 702);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	job = job_of(TAG);
	zassert_equal(job->update_id, 702);
	zassert_equal(dlv_queue_depth(&benv.dlv), 2);

	env_boot(9000u);
	zassert_equal(dlv_queue_depth(&benv.dlv), 2);
	job = job_of(TAG);
	zassert_not_null(job);
	zassert_equal(job->update_id, 702);
	zassert_equal(job->revision, 2);
	zassert_equal(job->validated_ms, 9000u);
	const uint8_t *lay;

	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), (int)small_layout_len);
	zassert_equal_ptr(lay, benv.dlv.layout, "the shared buffer");
	zassert_mem_equal(lay, small_layout, small_layout_len);
	job = job_of(TAG2);
	zassert_equal(job->update_id, 701);
	/* Finish one, reset again: only the other comes back. */
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	env_boot(0u);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1);
	zassert_is_null(job_of(TAG2));
	/* A reset after the result reached the persisted history but before the
	 * record was consumed: the record is dropped at boot, not delivered twice. */
	job = job_of(TAG);
	job->slot = DLV_NO_SLOT; /* dlv_finish then leaves the flash record live */
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	env_boot(0u);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
}

ZTEST(bridge_delivery, test_ring_wear_levelling)
{
	struct xfer t = x(1, 1, 800);
	uint16_t first, second;
	uint32_t rev;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	first = job_of(TAG)->slot;
	/* Every accepted layout goes to the next ring slot. */
	for (rev = 2; rev < 60; rev++) {
		t = x((uint16_t)rev, rev, 800 + rev);
		zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
			      0);
	}
	second = job_of(TAG)->slot;
	zassert_equal(second, (first + 58) % benv.flash.geom.pending_slots);
	/* The ring position survives a reset (no rewind to slot 0). */
	env_boot(0u);
	t = x(100, 100, 999);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	zassert_equal(job_of(TAG)->slot, (second + 1) % benv.flash.geom.pending_slots);
}

ZTEST(bridge_delivery, test_result_retry_and_ack)
{
	struct ctag_mesh_tag_cmd m = {.update_id = 900, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_IDENTIFY};
	struct ctag_mesh_delivery_result r;
	uint32_t next;

	boot_with_pack();
	dlv_tag_cmd(&benv.dlv, &m, 10000u);
	zassert_equal(results_for(900), 1);
	zassert_true(last_result(900, &r));
	/* Re-sent every MESH_RESULT_RETRY_MS up to MESH_RESULT_RETRIES times. */
	next = dlv_tick(&benv.dlv, 10000u);
	zassert_equal(next, CTAG_MESH_RESULT_RETRY_MS);
	zassert_equal(dlv_tick(&benv.dlv, 11999u), 1u);
	zassert_equal(results_for(900), 1);
	for (uint32_t i = 1; i <= CTAG_MESH_RESULT_RETRIES; i++) {
		(void)dlv_tick(&benv.dlv, 10000u + i * CTAG_MESH_RESULT_RETRY_MS);
		zassert_equal(results_for(900), 1 + i);
	}
	(void)dlv_tick(&benv.dlv, 10000u + 6 * CTAG_MESH_RESULT_RETRY_MS);
	zassert_equal(results_for(900), 1 + CTAG_MESH_RESULT_RETRIES, "gives up");
	zassert_equal(benv.dlv.c.results_unacked, 1);
	zassert_equal(dlv_tick(&benv.dlv, 30000u), UINT32_MAX);
	/* RESULT_ACK stops the re-sends; same result_seq on every copy. */
	m.update_id = 901;
	dlv_tag_cmd(&benv.dlv, &m, 40000u);
	zassert_true(last_result(901, &r));
	(void)dlv_tick(&benv.dlv, 42000u);
	zassert_equal(results_for(901), 2);
	struct ctag_mesh_delivery_result r2;

	zassert_true(last_result(901, &r2));
	zassert_equal(r2.result_seq, r.result_seq);
	dlv_result_ack(&benv.dlv, r.result_seq);
	(void)dlv_tick(&benv.dlv, 50000u);
	zassert_equal(results_for(901), 2);
}

ZTEST(bridge_delivery, test_result_seq_persisted)
{
	struct ctag_mesh_tag_cmd m = {.update_id = 1000, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_IDENTIFY};
	struct ctag_mesh_delivery_result r;
	uint16_t seen[40];
	size_t n = 0;

	boot_with_pack();
	/* Across resets, result_seq never repeats (10): the gateway de-duplicates
	 * by (bridge, result_seq). */
	for (int boot = 0; boot < 4; boot++) {
		for (int i = 0; i < 7 + boot * 5; i++) {
			m.update_id++;
			dlv_tag_cmd(&benv.dlv, &m, 0);
			zassert_true(last_result(m.update_id, &r));
			if (n < ARRAY_SIZE(seen)) {
				for (size_t k = 0; k < n; k++) {
					zassert_not_equal(seen[k], r.result_seq, "seq %u reused",
							  r.result_seq);
				}
				seen[n++] = r.result_seq;
			}
		}
		env_boot(0u);
	}
	zassert_true(n > 30);
	/* Persisted in blocks: not one write per result. */
	zassert_true(kv_count() < 10);
}

/* 10: a repeated LAYOUT_COMMIT of an accepted transfer answers DUPLICATE,
 * never NOT_FOUND, whatever became of its job, and after a reset as well. */
ZTEST(bridge_delivery, test_repeated_commit_of_accepted_transfer)
{
	struct ctag_mesh_tag_cmd clear = {.update_id = 1210, .tag_id = TAG, .epoch = EPOCH,
					  .cmd = CTAG_TAG_CMD_CLEAR};
	uint8_t d8[8] = {1, 2, 3, 4, 5, 6, 7, 8};
	struct xfer t = x(1, 1, 1200);
	uint32_t missing;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_DUPLICATE);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1, "pending: no second job");
	/* Ended without being displayed: still DUPLICATE, no new work, no result. */
	dlv_layout_cancel(&benv.dlv, 1200, 0);
	zassert_equal(results_for(1200), 1);
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_DUPLICATE);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	zassert_equal(results_for(1200), 1);
	/* CLEAR returned the history to revision 0: the transfer is still known. */
	dlv_tag_cmd(&benv.dlv, &clear, 0);
	dlv_finish(&benv.dlv, job_of(TAG), CTAG_STATUS_OK, NULL, 0, NULL, 0);
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_DUPLICATE);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	/* After a reset: the newest ring record names the accepted transfer. */
	env_boot(0u);
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_DUPLICATE);
	zassert_equal(dlv_layout_commit(&benv.dlv, 2, 0, &missing), CTAG_STATUS_NOT_FOUND);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	/* The next LAYOUT_BEGIN replaces it (one transfer at a time). */
	t = x(3, 2, 1201);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	zassert_equal(dlv_layout_commit(&benv.dlv, 1, 0, &missing), CTAG_STATUS_NOT_FOUND);
	zassert_equal(dlv_layout_commit(&benv.dlv, 3, 0, &missing), CTAG_STATUS_DUPLICATE);
	/* A transfer answered DUPLICATE (a displayed revision) is remembered the
	 * same way, across a reset, and its result is not sent twice. */
	dlv_finish(&benv.dlv, job_of(TAG), CTAG_STATUS_OK, d8, 3000, NULL, 0);
	t = x(4, 2, 1202);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_DUPLICATE);
	zassert_equal(results_for(1202), 1);
	env_boot(0u);
	zassert_equal(dlv_layout_commit(&benv.dlv, 4, 0, &missing), CTAG_STATUS_DUPLICATE);
	zassert_equal(results_for(1202), 1);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	assert_one_seq_per_update();
}

/* 10: never two DELIVERY_RESULTs with different result_seq for one update_id. */
ZTEST(bridge_delivery, test_one_result_seq_per_update_id)
{
	struct ctag_mesh_tag_cmd m = {.update_id = 1310, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_CLEAR};
	struct ctag_mesh_delivery_result r1, r2;
	struct xfer t = x(1, 3, 1300);
	struct dlv_job *layout_job, *cmd;

	boot_with_pack();
	/* The same update_id in a new transfer (the gateway restarted it) after
	 * its job ended without being displayed: no new work, the first result
	 * again under its result_seq. */
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	dlv_finish(&benv.dlv, job_of(TAG), CTAG_STATUS_DISPLAY_STATE_UNKNOWN, NULL, 0, NULL, 0);
	zassert_true(last_result(1300, &r1));
	dlv_result_ack(&benv.dlv, r1.result_seq);
	t.xfer_id = 2;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_DUPLICATE);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0, "no new work");
	zassert_equal(results_for(1300), 2);
	zassert_true(last_result(1300, &r2));
	zassert_equal(r2.result_seq, r1.result_seq);
	zassert_equal(r2.status, CTAG_STATUS_DISPLAY_STATE_UNKNOWN);
	/* After a reset the history still knows (update_id, result_seq). */
	env_boot(0u);
	t.xfer_id = 3;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_DUPLICATE);
	zassert_true(last_result(1300, &r2));
	zassert_equal(r2.result_seq, r1.result_seq);
	/* A new update_id for that revision is a re-delivery: accepted again. */
	t.xfer_id = 4;
	t.update_id = 1301;
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	layout_job = job_of(TAG);
	/* A repeated TAG_CMD (the segment ACK was lost): one job, one result_seq. */
	dlv_tag_cmd(&benv.dlv, &m, 0);
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_equal(dlv_queue_depth(&benv.dlv), 2);
	cmd = dlv_next_job(&benv.dlv, TAG, layout_job->order, UINT32_MAX);
	zassert_equal(cmd->kind, DLV_CMD);
	dlv_finish(&benv.dlv, cmd, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	zassert_true(last_result(1310, &r1));
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_equal(dlv_queue_depth(&benv.dlv), 1, "no second CLEAR");
	zassert_equal(results_for(1310), 1, "still being re-sent: nothing extra");
	dlv_result_ack(&benv.dlv, r1.result_seq);
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_equal(results_for(1310), 2, "acknowledged: the same result again");
	/* An immediate answer, repeated after its retries gave up. */
	m.update_id = 1311;
	m.cmd = CTAG_TAG_CMD_IDENTIFY;
	dlv_tag_cmd(&benv.dlv, &m, 10000u);
	for (uint32_t i = 1; i <= CTAG_MESH_RESULT_RETRIES + 1u; i++) {
		(void)dlv_tick(&benv.dlv, 10000u + i * CTAG_MESH_RESULT_RETRY_MS);
	}
	zassert_equal(results_for(1311), 1 + CTAG_MESH_RESULT_RETRIES);
	dlv_tag_cmd(&benv.dlv, &m, 30000u);
	zassert_equal(results_for(1311), 2 + CTAG_MESH_RESULT_RETRIES);
	assert_one_seq_per_update();
}

/* Every finished result stays findable until the table needs its entry:
 * the oldest finished one goes first, an unacknowledged one last. */
ZTEST(bridge_delivery, test_result_table_eviction)
{
	struct ctag_mesh_tag_cmd m = {.update_id = 1400, .tag_id = TAG, .epoch = EPOCH,
				      .cmd = CTAG_TAG_CMD_IDENTIFY};
	struct ctag_mesh_delivery_result r;
	uint32_t i;

	boot_with_pack();
	/* One result left unacknowledged, the others acknowledged. */
	dlv_tag_cmd(&benv.dlv, &m, 0);
	for (i = 1; i < DLV_RESULT_SLOTS + 3u; i++) {
		m.update_id = 1400 + i;
		dlv_tag_cmd(&benv.dlv, &m, 0);
		zassert_true(last_result(m.update_id, &r));
		dlv_result_ack(&benv.dlv, r.result_seq);
	}
	zassert_equal(benv.dlv.c.results_dropped, 0, "an unacknowledged result is never evicted "
						     "while finished ones remain");
	(void)dlv_tick(&benv.dlv, CTAG_MESH_RESULT_RETRY_MS);
	zassert_equal(results_for(1400), 2, "still re-sent");
	/* The newest acknowledged one is still known: repeated, not renumbered. */
	m.update_id = 1400 + DLV_RESULT_SLOTS + 2u;
	dlv_tag_cmd(&benv.dlv, &m, 0);
	zassert_equal(results_for(m.update_id), 2);
	assert_one_seq_per_update();
}

/* The transfer goes straight into its ring slot; only the commit makes it a record. */
ZTEST(bridge_delivery, test_transfer_assembled_in_flash)
{
	size_t n = big_layout(big);
	struct xfer t = x(5, 1, 1500);
	struct bflash_pending h;
	uint32_t missing;
	uint16_t slot;
	const uint8_t *mem;

	boot_with_pack();
	/* Chunks in reverse order, one repeated: each written once, in place. */
	transfer(&benv.dlv, &t, big, n, 0xFFFFFFFFu);
	slot = benv.dlv.x.slot;
	zassert_not_equal(slot, DLV_NO_SLOT);
	for (int i = 27; i >= 0; i--) {
		struct ctag_mesh_layout_chunk c = {
			.xfer_id = 5, .index = (uint8_t)i, .data = &big[i * 150],
			.data_len = MIN(150u, n - i * 150u),
		};

		dlv_layout_chunk(&benv.dlv, &c);
		if (i == 20) {
			dlv_layout_chunk(&benv.dlv, &c);
		}
	}
	mem = flash_mem() + bflash_pending_offset(&benv.flash, slot);
	zassert_mem_equal(mem + BFLASH_PENDING_HDR + 5 * BFLASH_PENDING_STRIDE, &big[5 * 150], 150);
	zassert_mem_equal(mem + BFLASH_PENDING_HDR + 27 * BFLASH_PENDING_STRIDE, &big[27 * 150],
			  n - 27 * 150);
	zassert_equal(bflash_pending_peek(&benv.flash, slot, &h), BFLASH_PENDING_EMPTY,
		      "no record before the commit");
	/* A reset before the commit: the transfer is gone, nothing is restored. */
	env_boot(0u);
	zassert_equal(dlv_layout_commit(&benv.dlv, 5, 0, &missing), CTAG_STATUS_NOT_FOUND);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	/* Again, committed: the same slot now holds the record of the job. */
	t.xfer_id = 6;
	zassert_equal(deliver(&benv.dlv, &t, big, n, 0, 0, NULL), CTAG_STATUS_OK);
	slot = job_of(TAG)->slot;
	zassert_equal(bflash_pending_read(&benv.flash, slot, &h, scratch, sizeof(scratch)), 1);
	zassert_equal(h.xfer_id, 6);
	zassert_equal(h.update_id, 1500);
	zassert_mem_equal(scratch, big, n);
	/* A flash error while a chunk is written: STORAGE_ERROR at the commit. */
	t = x(7, 2, 1501);
	flash_cut_after(0);
	transfer(&benv.dlv, &t, small_layout, small_layout_len, 0);
	flash_cut_after(-1);
	zassert_equal(dlv_layout_commit(&benv.dlv, 7, 0, &missing), CTAG_STATUS_STORAGE_ERROR);
	zassert_equal(job_of(TAG)->update_id, 1500, "the pending layout stays");
}

/* Power lost while the commit seals the record: no job and no accepted
 * transfer after the reset (the commit was answered STORAGE_ERROR). */
ZTEST(bridge_delivery, test_seal_power_cut)
{
	uint32_t missing;

	for (int32_t k = 0; k <= (int32_t)BFLASH_PENDING_HDR; k += 4) {
		struct xfer t = x(8, 1, 1600);
		uint8_t st;

		boot_with_pack();
		transfer(&benv.dlv, &t, small_layout, small_layout_len, 0);
		flash_cut_after(k);
		st = dlv_layout_commit(&benv.dlv, 8, 0, &missing);
		flash_cut_after(-1);
		zassert_equal(st, k < (int32_t)BFLASH_PENDING_HDR ? CTAG_STATUS_STORAGE_ERROR
								  : CTAG_STATUS_OK,
			      "cut after %d header bytes", k);
		env_boot(0u);
		zassert_equal(dlv_queue_depth(&benv.dlv), k < (int32_t)BFLASH_PENDING_HDR ? 0 : 1);
		zassert_equal(dlv_layout_commit(&benv.dlv, 8, 0, &missing),
			      k < (int32_t)BFLASH_PENDING_HDR ? CTAG_STATUS_NOT_FOUND
							      : CTAG_STATUS_DUPLICATE,
			      "cut after %d header bytes", k);
	}
}

/* One layout buffer: a commit borrows it, the job's layout is reloaded. */
ZTEST(bridge_delivery, test_shared_layout_buffer)
{
	struct xfer t = x(1, 1, 1700);
	struct dlv_job *job;
	const uint8_t *lay;
	uint32_t loads;
	uint16_t slot;

	boot_with_pack();
	assign(&benv.dlv, TAG2, 1, NULL);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	job = job_of(TAG);
	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), (int)small_layout_len);
	zassert_true(dlv_job_layout_held(&benv.dlv, job));
	loads = benv.dlv.c.layout_loads;
	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), (int)small_layout_len);
	zassert_equal(benv.dlv.c.layout_loads, loads, "held: not read again");
	/* Another tag's commit validates in the buffer. */
	t = (struct xfer){.xfer_id = 2, .tag_id = TAG2, .epoch = 1, .revision = 1, .update_id = 1701};
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL), 0);
	zassert_false(dlv_job_layout_held(&benv.dlv, job));
	zassert_mem_equal(benv.dlv.layout, big, 64);
	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), (int)small_layout_len);
	zassert_mem_equal(lay, small_layout, small_layout_len);
	zassert_equal(benv.dlv.c.layout_loads, loads + 1);
	/* A pending re-delivery moves the job to the new record: same bytes. */
	slot = job->slot;
	t = x(3, 1, 1702);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_DUPLICATE);
	zassert_not_equal(job->slot, slot);
	zassert_equal(job->update_id, 1702);
	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), (int)small_layout_len);
	zassert_mem_equal(lay, small_layout, small_layout_len);
	/* A damaged record is never served. */
	(void)deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL);
	flash_mem()[bflash_pending_offset(&benv.flash, job->slot) + BFLASH_PENDING_HDR + 3] ^= 0x10;
	zassert_false(dlv_job_layout_held(&benv.dlv, job));
	zassert_equal(dlv_job_layout(&benv.dlv, job, &lay), -EBADMSG);
	/* A finished job gives the buffer up. */
	benv.dlv.lb_job = job;
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	zassert_false(dlv_job_layout_held(&benv.dlv, job));
}

/* Crash consistency of dlv_finish (review): a job whose result the history
 * records keeps its pending record live until the history is saved and the
 * result is out; any other job's record is consumed before its result. */
static uint16_t probe_slot;
static int probe_state_at_hist = -1;
static int probe_state_at_result = -1;

static void finish_save_probe(const char *name)
{
	struct bflash_pending h;

	if (name[0] == 'h') {
		probe_state_at_hist = bflash_pending_peek(&benv.flash, probe_slot, &h);
	}
}

static void finish_send_probe(uint8_t op, const uint8_t *params, size_t len)
{
	struct bflash_pending h;

	if (op == CTAG_MESH_OP_DELIVERY_RESULT) {
		probe_state_at_result = bflash_pending_peek(&benv.flash, probe_slot, &h);
	}
}

ZTEST(bridge_delivery, test_finish_order)
{
	struct xfer t = x(1, 1, 1800);
	struct dlv_job *job;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	job = job_of(TAG);
	probe_slot = job->slot;
	save_probe = finish_save_probe;
	send_probe = finish_send_probe;
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	zassert_equal(probe_state_at_hist, BFLASH_PENDING_LIVE, "history saved first");
	zassert_equal(probe_state_at_result, BFLASH_PENDING_LIVE, "result sent before the consume");
	zassert_equal(bflash_pending_peek(&benv.flash, probe_slot, &(struct bflash_pending){0}),
		      BFLASH_PENDING_CONSUMED);
	/* A superseded job: its record is consumed before SUPERSEDED goes out. */
	t = x(2, 2, 1801);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_OK);
	probe_slot = job_of(TAG)->slot;
	probe_state_at_result = -1;
	t = x(3, 3, 1802);
	zassert_equal(deliver(&benv.dlv, &t, big, big_layout(big), 0, 0, NULL), 0);
	zassert_equal(probe_state_at_result, BFLASH_PENDING_CONSUMED);
	/* A reset between the history save and the consume never redelivers. */
	save_probe = NULL;
	send_probe = NULL;
	job = job_of(TAG);
	job->slot = DLV_NO_SLOT; /* as if the reset came before the consume */
	dlv_finish(&benv.dlv, job, CTAG_STATUS_OK, NULL, 0, NULL, 0);
	env_boot(0u);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	assert_one_seq_per_update();
}

/* accept() changes nothing unless the new record is sealed (review). */
ZTEST(bridge_delivery, test_accept_failures_change_nothing)
{
	struct ctag_mesh_tag_cmd m = {.tag_id = TAG2, .epoch = 1, .cmd = CTAG_TAG_CMD_CLEAR};
	struct ctag_mesh_delivery_result r;
	struct xfer t = x(1, 1, 1900);
	uint32_t missing;

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	/* Revision 3 cannot be sealed (flash failure): revision 1 stays pending,
	 * nothing is SUPERSEDED, and the history still allows revision 2. */
	t = x(2, 3, 1901);
	transfer(&benv.dlv, &t, big, big_layout(big), 0);
	flash_cut_after(0);
	zassert_equal(dlv_layout_commit(&benv.dlv, 2, 0, &missing), CTAG_STATUS_STORAGE_ERROR);
	flash_cut_after(-1);
	zassert_equal(results_for(1900), 0, "not superseded");
	zassert_equal(job_of(TAG)->update_id, 1900);
	t = x(3, 2, 1902);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0,
		      "the history never took revision 3");
	zassert_true(last_result(1900, &r));
	zassert_equal(r.status, CTAG_STATUS_SUPERSEDED);
	/* A full job table: NO_RESOURCES for another tag, history untouched ... */
	assign(&benv.dlv, TAG2, 1, NULL);
	for (uint32_t i = 1; i < DLV_MAX_JOBS; i++) {
		m.update_id = 1950 + i;
		dlv_tag_cmd(&benv.dlv, &m, 0);
	}
	zassert_equal(dlv_queue_depth(&benv.dlv), DLV_MAX_JOBS);
	t = (struct xfer){.xfer_id = 4, .tag_id = TAG2, .epoch = 1, .revision = 5, .update_id = 1903};
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL),
		      CTAG_STATUS_NO_RESOURCES);
	zassert_false(benv.dlv.hist[1].valid && benv.dlv.hist[1].revision == 5);
	/* ... but a tag whose older layout it replaces is accepted. */
	t = x(5, 4, 1904);
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	zassert_true(last_result(1902, &r));
	zassert_equal(r.status, CTAG_STATUS_SUPERSEDED);
	assert_one_seq_per_update();
}

/* ASSIGN_DEL cancels inclusively: also with epoch 0xFFFFFFFF (review). */
ZTEST(bridge_delivery, test_assign_del_max_epoch)
{
	struct ctag_mesh_assign_del del = {.tag_id = TAG, .epoch = UINT32_MAX};
	struct ctag_mesh_delivery_result r;
	struct xfer t = x(1, 1, 2000);

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	zassert_equal(dlv_assign_del(&benv.dlv, &del, 0), CTAG_STATUS_OK);
	zassert_true(last_result(2000, &r));
	zassert_equal(r.status, CTAG_STATUS_CANCELLED);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
}

ZTEST(bridge_delivery, test_node_reset)
{
	struct xfer t = x(1, 1, 1100);

	boot_with_pack();
	zassert_equal(deliver(&benv.dlv, &t, small_layout, small_layout_len, 0, 0, NULL), 0);
	dlv_reset(&benv.dlv);
	zassert_equal(dlv_assigned_count(&benv.dlv), 0);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
	env_boot(0u);
	zassert_equal(dlv_assigned_count(&benv.dlv), 0);
	zassert_equal(dlv_queue_depth(&benv.dlv), 0);
}

ZTEST_SUITE(bridge_delivery, NULL, NULL, NULL, NULL, NULL);
