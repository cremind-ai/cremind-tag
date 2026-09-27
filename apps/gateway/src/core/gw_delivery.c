/*
 * Delivery engine (docs/protocol.md 1.5, 3.2-3.4, 10): a bounded queue of
 * layouts, one transfer at a time (LAYOUT_BEGIN -> LAYOUT_CHUNK x n ->
 * LAYOUT_COMMIT through the segmented lane), INCOMPLETE resend rounds, commit
 * re-sends, and the results: LAYOUT_STATUS rejections become the gateway's
 * own EVT_RESULT, DELIVERY_RESULTs are acknowledged and de-duplicated by
 * (bridge, result_seq). Mirrors sim/gateway.py _deliver/_transfer/_on_result.
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

#define N_SLOTS (CONFIG_CTAG_GW_DELIVERY_QUEUE + 1)

static const char T_EMPTY[] = "empty layout";
static const char T_TOO_LARGE[] = "layout larger than the gateway accepts";

uint8_t gw_queue_depth(const struct gw_core *g)
{
	uint8_t n = 0u;

	for (int i = 0; i < N_SLOTS; i++) {
		if (g->d[i].state == GW_D_QUEUED) {
			n++;
		}
	}
	return n;
}

static struct gw_delivery *active(struct gw_core *g)
{
	return g->x.slot >= 0 ? &g->d[g->x.slot] : NULL;
}

/* ---- Results ---- */

/* 10: exactly one EVT_RESULT per update_id. true = already reported; else
 * the update_id is remembered as reported now. */
static bool already_reported(struct gw_core *g, uint64_t update_id)
{
	for (uint16_t i = 0; i < g->reported_count; i++) {
		if (g->reported[i] == update_id) {
			g->c.repeated_results++;
			return true;
		}
	}
	g->reported[g->reported_next] = update_id;
	g->reported_next = (uint16_t)((g->reported_next + 1u) % CONFIG_CTAG_GW_REPORTED);
	if (g->reported_count < CONFIG_CTAG_GW_REPORTED) {
		g->reported_count++;
	}
	return false;
}

void gw_result_event(struct gw_core *g, uint64_t update_id, uint16_t bridge, uint32_t tag_id,
		     uint32_t epoch, uint32_t revision, uint8_t status, uint32_t mesh_ms)
{
	static const uint8_t zero_digest[8];
	struct ctag_cbor_field timing[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_WAKE_MS, 0u),
		GW_F_UINT(CTAG_CBOR_KEY_MESH_MS, mesh_ms),
		GW_F_UINT(CTAG_CBOR_KEY_TRANSFER_MS, 0u),
		GW_F_UINT(CTAG_CBOR_KEY_REFRESH_MS, 0u),
		GW_F_UINT(CTAG_CBOR_KEY_SUSPEND_MS, 0u),
	};
	struct ctag_cbor_field f[9] = {
		GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, update_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id),
		GW_F_UINT(CTAG_CBOR_KEY_EPOCH, epoch),
		GW_F_UINT(CTAG_CBOR_KEY_REVISION, revision),
		GW_F_UINT(CTAG_CBOR_KEY_STATUS, status),
		GW_F_BSTR(CTAG_CBOR_KEY_DIGEST, zero_digest, sizeof(zero_digest)),
		GW_F_UINT(CTAG_CBOR_KEY_BATTERY_MV, 0u),
		GW_F_MAP(CTAG_CBOR_KEY_TIMING, timing, 5u),
	};

	if (already_reported(g, update_id)) {
		return;
	}
	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_RESULT, f, 9u, true);
	g->c.results++;
}

static void at_bridge_add(struct gw_core *g, const struct gw_delivery *d, uint32_t mesh_ms)
{
	struct gw_at_bridge *e = NULL;

	for (int i = 0; i < CONFIG_CTAG_GW_AT_BRIDGE; i++) {
		struct gw_at_bridge *c = &g->ab[i];

		if (!c->used) {
			e = c;
			break;
		}
		if (e == NULL || c->order < e->order) {
			e = c; /* full: forget the oldest */
		}
	}
	e->used = true;
	e->bridge = d->bridge;
	e->order = d->order;
	e->tag_id = d->tag_id;
	e->epoch = d->epoch;
	e->revision = d->revision;
	e->update_id = d->update_id;
	e->mesh_ms = mesh_ms;
}

static struct gw_at_bridge *at_bridge_find(struct gw_core *g, uint64_t update_id)
{
	for (int i = 0; i < CONFIG_CTAG_GW_AT_BRIDGE; i++) {
		if (g->ab[i].used && g->ab[i].update_id == update_id) {
			return &g->ab[i];
		}
	}
	return NULL;
}

enum end_mode {
	END_RESULT,    /* the gateway reports the delivery with this status */
	END_AT_BRIDGE, /* the bridge has it; its DELIVERY_RESULT will report it */
	END_REPORTED,  /* the bridge's DELIVERY_RESULT already reported it */
};

/* The active transfer ended; the slot is free and the next one may start. */
static void transfer_end_mode(struct gw_core *g, enum end_mode mode, uint8_t status)
{
	struct gw_delivery *d = active(g);
	uint32_t mesh_ms = (uint32_t)(g->now - g->x.started);

	if (d == NULL) {
		return;
	}
	if (mode == END_AT_BRIDGE) {
		at_bridge_add(g, d, mesh_ms);
	} else if (mode == END_RESULT) {
		gw_result_event(g, d->update_id, d->bridge, d->tag_id, d->epoch, d->revision, status,
				mesh_ms);
	}
	d->state = GW_D_FREE;
	gw_ring_free(&g->arena, d->layout);
	d->layout = NULL;
	memset(&g->x, 0, sizeof(g->x));
	g->x.slot = -1;
	gw_lane_withdraw(g, GW_REQ_DELIVERY);
	gw_delivery_pump(g);
}

static void transfer_end(struct gw_core *g, bool at_bridge, uint8_t status)
{
	transfer_end_mode(g, at_bridge ? END_AT_BRIDGE : END_RESULT, status);
}

/* ---- Accepting ---- */

struct gw_req_result gw_deliver(struct gw_core *g, uint64_t op_id, uint16_t bridge,
				uint32_t tag_id, uint32_t epoch, uint32_t revision,
				uint64_t update_id, const uint8_t *fontpack_id, const uint8_t *layout,
				size_t len)
{
	struct gw_req_result r = gw_node_usable(g, bridge);
	struct gw_delivery *d = NULL;

	if (r.status != CTAG_STATUS_OK) {
		return r; /* 10: unknown or unconfigured bridge -> NOT_FOUND at once */
	}
	if (len == 0u) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_EMPTY};
	}
	if (len > CONFIG_CTAG_GW_LAYOUT_MAX) {
		return (struct gw_req_result){CTAG_STATUS_TOO_LARGE, T_TOO_LARGE};
	}
	for (int i = 0; i < N_SLOTS; i++) {
		if (g->d[i].state == GW_D_FREE) {
			d = &g->d[i];
			break;
		}
	}
	if (d == NULL || gw_queue_depth(g) >= CONFIG_CTAG_GW_DELIVERY_QUEUE ||
	    (d->layout = gw_ring_alloc(&g->arena, (uint32_t)len)) == NULL) {
		/* No record, or no room in the layout arena: retry later. */
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL};
	}
	d->state = GW_D_QUEUED;
	d->order = ++g->d_order;
	d->op_id = op_id;
	d->bridge = bridge;
	d->tag_id = tag_id;
	d->epoch = epoch;
	d->revision = revision;
	d->update_id = update_id;
	memcpy(d->fontpack_id, fontpack_id, sizeof(d->fontpack_id));
	memcpy(d->layout, layout, len);
	d->len = (uint16_t)len;
	g->c.deliveries_accepted++;
	gw_delivery_pump(g);
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

struct gw_req_result gw_cancel(struct gw_core *g, uint64_t update_id)
{
	struct gw_delivery *d = active(g);
	struct gw_at_bridge *ab;
	uint8_t p[CTAG_MESH_LAYOUT_CANCEL_LEN];
	struct ctag_mesh_layout_cancel c = {.update_id = update_id};
	uint16_t bridge = 0u;

	for (int i = 0; i < N_SLOTS; i++) {
		struct gw_delivery *q = &g->d[i];

		if (q->state == GW_D_QUEUED && q->update_id == update_id) {
			q->state = GW_D_FREE;
			gw_ring_free(&g->arena, q->layout);
			q->layout = NULL;
			gw_result_event(g, q->update_id, q->bridge, q->tag_id, q->epoch, q->revision,
					CTAG_STATUS_CANCELLED, 0u);
			return (struct gw_req_result){CTAG_STATUS_OK, NULL};
		}
	}
	if (d != NULL && d->update_id == update_id) {
		bridge = d->bridge;
	} else if ((ab = at_bridge_find(g, update_id)) != NULL) {
		bridge = ab->bridge;
	}
	if (bridge == 0u) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, NULL};
	}
	/* At the bridge (or on its way): the bridge ends it with CANCELLED. */
	(void)ctag_mesh_layout_cancel_pack(&c, p, sizeof(p));
	(void)gw_unseg_send(g, bridge, CTAG_MESH_OP_LAYOUT_CANCEL, p, sizeof(p));
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

/* ---- Transfer ---- */

static void start(struct gw_core *g, int slot)
{
	struct gw_delivery *d = &g->d[slot];
	uint8_t digest[32];

	d->state = GW_D_ACTIVE;
	memset(&g->x, 0, sizeof(g->x));
	g->x.slot = (int8_t)slot;
	g->x.phase = GW_X_BEGIN;
	g->x.started = g->now;
	g->x.xfer_id = ++g->xfer_id; /* 3.2 rule 4: gateway-wide, wrapping */
	g->x.chunk_count =
		(uint8_t)((d->len + CTAG_LAYOUT_CHUNK_DATA_MAX - 1u) / CTAG_LAYOUT_CHUNK_DATA_MAX);
	if (g->be->sha256(g->be->ctx, d->layout, d->len, digest) != 0) {
		g->c.internal_errors++;
		transfer_end(g, false, CTAG_STATUS_INTERNAL);
		return;
	}
	memcpy(g->x.digest, digest, sizeof(g->x.digest));
	gw_lane_request(g, GW_REQ_DELIVERY);
}

void gw_delivery_pump(struct gw_core *g)
{
	int best = -1;

	if (g->x.slot >= 0) {
		return; /* one transfer at a time */
	}
	for (int i = 0; i < N_SLOTS; i++) {
		if (g->d[i].state == GW_D_QUEUED &&
		    (best < 0 || g->d[i].order < g->d[best].order)) {
			best = i;
		}
	}
	if (best >= 0) {
		start(g, best);
	}
}

static uint8_t lowest_bit(uint32_t v)
{
	uint8_t i = 0u;

	while ((v & 1u) == 0u) {
		v >>= 1;
		i++;
	}
	return i;
}

void gw_delivery_lane_go(struct gw_core *g)
{
	struct gw_delivery *d = active(g);
	uint8_t p[CTAG_MESH_LAYOUT_CHUNK_MAX_LEN > CTAG_MESH_LAYOUT_BEGIN_LEN
			  ? CTAG_MESH_LAYOUT_CHUNK_MAX_LEN
			  : CTAG_MESH_LAYOUT_BEGIN_LEN];
	int n = -EINVAL;
	uint8_t op = CTAG_MESH_OP_LAYOUT_BEGIN;

	if (d != NULL && g->x.phase == GW_X_BEGIN) {
		struct ctag_mesh_layout_begin b = {
			.xfer_id = g->x.xfer_id,
			.tag_id = d->tag_id,
			.epoch = d->epoch,
			.revision = d->revision,
			.update_id = d->update_id,
			.total_len = d->len,
			.chunk_count = g->x.chunk_count,
		};

		memcpy(b.fontpack_id, d->fontpack_id, sizeof(b.fontpack_id));
		memcpy(b.digest, g->x.digest, sizeof(b.digest));
		n = ctag_mesh_layout_begin_pack(&b, p, sizeof(p));
	} else if (d != NULL && g->x.phase == GW_X_CHUNKS && g->x.pending != 0u) {
		uint8_t i = lowest_bit(g->x.pending);
		size_t off = (size_t)i * CTAG_LAYOUT_CHUNK_DATA_MAX;
		size_t len = d->len - off;
		struct ctag_mesh_layout_chunk c = {
			.xfer_id = g->x.xfer_id,
			.index = i,
			.data = &d->layout[off],
			.data_len = len > CTAG_LAYOUT_CHUNK_DATA_MAX ? CTAG_LAYOUT_CHUNK_DATA_MAX : len,
		};

		g->x.cur = i;
		op = CTAG_MESH_OP_LAYOUT_CHUNK;
		n = ctag_mesh_layout_chunk_pack(&c, p, sizeof(p));
	}
	if (n < 0) {
		gw_lane_issued(g, -EINVAL); /* nothing to send: counts as a failed attempt */
		return;
	}
	gw_lane_issued(g, g->be->mesh_send(g->be->ctx, d->bridge, op, p, (size_t)n, g->lane.tag));
}

static void send_commit(struct gw_core *g)
{
	struct gw_delivery *d = active(g);
	struct ctag_mesh_layout_commit c = {.xfer_id = g->x.xfer_id};
	uint8_t p[CTAG_MESH_LAYOUT_COMMIT_LEN];

	g->x.phase = GW_X_COMMIT;
	g->x.commits++;
	g->x.deadline = g->now + CONFIG_CTAG_GW_STATUS_TIMEOUT_MS;
	(void)ctag_mesh_layout_commit_pack(&c, p, sizeof(p));
	(void)gw_unseg_send(g, d->bridge, CTAG_MESH_OP_LAYOUT_COMMIT, p, sizeof(p));
}

static void commit_round(struct gw_core *g)
{
	g->x.commits = 0u;
	send_commit(g);
}

void gw_delivery_lane_done(struct gw_core *g, bool ok)
{
	if (active(g) == NULL) {
		return;
	}
	if (!ok) {
		/* 3.2 rule 1: a failed end retried 3 times fails the delivery. */
		transfer_end(g, false, CTAG_STATUS_TIMEOUT);
		return;
	}
	if (g->x.phase == GW_X_BEGIN) {
		g->x.phase = GW_X_CHUNKS;
		g->x.pending = g->x.chunk_count >= 32u ? UINT32_MAX : (1u << g->x.chunk_count) - 1u;
		gw_lane_request(g, GW_REQ_DELIVERY);
		return;
	}
	g->x.pending &= ~(1u << g->x.cur);
	if (g->x.pending != 0u) {
		gw_lane_request(g, GW_REQ_DELIVERY);
	} else {
		commit_round(g);
	}
}

static uint8_t popcount32(uint32_t v)
{
	uint8_t n = 0u;

	while (v != 0u) {
		v &= v - 1u;
		n++;
	}
	return n;
}

void gw_delivery_status(struct gw_core *g, uint16_t src, const struct ctag_mesh_layout_status *st)
{
	struct gw_delivery *d = active(g);
	uint32_t valid;

	if (d == NULL || g->x.phase != GW_X_COMMIT || src != d->bridge ||
	    st->xfer_id != g->x.xfer_id) {
		g->c.stale_status++;
		return;
	}
	switch (st->status) {
	case CTAG_STATUS_OK:
	case CTAG_STATUS_DUPLICATE: {
		/* 10: DUPLICATE is OK for this transfer: a re-sent commit whose first
		 * OK was lost, or a revision the bridge already holds (it re-sends
		 * the stored result or delivers the pending one). */
		struct ctag_cbor_field f[4] = {
			GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, d->update_id),
			GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, d->tag_id),
			GW_F_UINT(CTAG_CBOR_KEY_REVISION, d->revision),
			GW_F_UINT(CTAG_CBOR_KEY_STAGE, CTAG_STAGE_BRIDGE_RECEIVED),
		};

		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_STAGE, f, 4u, false);
		transfer_end(g, true, st->status);
		break;
	}
	case CTAG_STATUS_INCOMPLETE:
		if (g->x.round >= GW_INCOMPLETE_ROUNDS) {
			transfer_end(g, false, CTAG_STATUS_INCOMPLETE);
			break;
		}
		g->x.round++;
		valid = g->x.chunk_count >= 32u ? UINT32_MAX : (1u << g->x.chunk_count) - 1u;
		g->x.pending = st->missing & valid; /* exactly the missing chunks */
		g->c.chunks_resent += popcount32(g->x.pending);
		if (g->x.pending != 0u) {
			g->x.phase = GW_X_CHUNKS;
			gw_lane_request(g, GW_REQ_DELIVERY);
		} else {
			commit_round(g);
		}
		break;
	default:
		/* 10: any other status ends the delivery with that status, zero digest. */
		transfer_end(g, false, st->status);
		break;
	}
}

void gw_delivery_timers(struct gw_core *g)
{
	if (g->x.slot < 0 || g->x.phase != GW_X_COMMIT || g->now < g->x.deadline) {
		return;
	}
	if (g->x.commits < 1u + GW_COMMIT_RESENDS) {
		g->c.commit_resends++;
		send_commit(g);
	} else {
		transfer_end(g, false, CTAG_STATUS_TIMEOUT);
	}
}

int64_t gw_delivery_deadline(const struct gw_core *g)
{
	return g->x.slot >= 0 && g->x.phase == GW_X_COMMIT ? g->x.deadline : GW_NEVER;
}

/* ---- From the bridge ---- */

static bool result_seen(struct gw_core *g, uint16_t src, uint16_t result_seq)
{
	uint32_t key = ((uint32_t)src << 16) | result_seq;

	for (int i = 0; i < CONFIG_CTAG_GW_RESULT_DEDUP; i++) {
		if (g->seen_results[i] == key + 1u) {
			return true;
		}
	}
	g->seen_results[g->seen_next] = key + 1u;
	g->seen_next = (uint16_t)((g->seen_next + 1u) % CONFIG_CTAG_GW_RESULT_DEDUP);
	return false;
}

void gw_delivery_result(struct gw_core *g, uint16_t src, const struct ctag_mesh_delivery_result *r)
{
	struct ctag_mesh_result_ack ack = {.result_seq = r->result_seq};
	uint8_t p[CTAG_MESH_RESULT_ACK_LEN];
	struct gw_at_bridge *ab;
	uint32_t mesh_ms = 0u;

	/* Always acknowledged, also a copy already reported (3.4). */
	(void)ctag_mesh_result_ack_pack(&ack, p, sizeof(p));
	(void)gw_unseg_send(g, src, CTAG_MESH_OP_RESULT_ACK, p, sizeof(p));
	if (result_seen(g, src, r->result_seq)) {
		g->c.duplicate_results++;
		return;
	}
	ab = at_bridge_find(g, r->update_id);
	if (ab != NULL) {
		mesh_ms = ab->mesh_ms;
		ab->used = false;
	} else if (active(g) != NULL && g->x.phase == GW_X_COMMIT && active(g)->bridge == src &&
		   active(g)->update_id == r->update_id) {
		/* The bridge accepted the layout and even finished it, but its
		 * LAYOUT_STATUS was lost: the result ends the wait for it. */
		mesh_ms = (uint32_t)(g->now - g->x.started);
		transfer_end_mode(g, END_REPORTED, CTAG_STATUS_OK);
	}
	if (already_reported(g, r->update_id)) {
		return; /* 10: one EVT_RESULT per update_id; acknowledged above */
	}
	{
		struct ctag_cbor_field timing[5] = {
			GW_F_UINT(CTAG_CBOR_KEY_WAKE_MS, r->wake_ms),
			GW_F_UINT(CTAG_CBOR_KEY_MESH_MS, mesh_ms),
			GW_F_UINT(CTAG_CBOR_KEY_TRANSFER_MS, r->transfer_ms),
			GW_F_UINT(CTAG_CBOR_KEY_REFRESH_MS, r->refresh_ms),
			GW_F_UINT(CTAG_CBOR_KEY_SUSPEND_MS, r->suspend_ms),
		};
		struct ctag_cbor_field f[9] = {
			GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, r->update_id),
			GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, src),
			GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, r->tag_id),
			GW_F_UINT(CTAG_CBOR_KEY_EPOCH, r->epoch),
			GW_F_UINT(CTAG_CBOR_KEY_REVISION, r->revision),
			GW_F_UINT(CTAG_CBOR_KEY_STATUS, r->status),
			GW_F_BSTR(CTAG_CBOR_KEY_DIGEST, r->digest, sizeof(r->digest)),
			GW_F_UINT(CTAG_CBOR_KEY_BATTERY_MV, r->battery_mv),
			GW_F_MAP(CTAG_CBOR_KEY_TIMING, timing, 5u),
		};

		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_RESULT, f, 9u, true);
	}
	g->c.results++;
}
