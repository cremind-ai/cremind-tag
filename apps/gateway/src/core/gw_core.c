/* Gateway core: initialisation, mesh input dispatch, timers, counters. */
#include <string.h>

#include "gw_core.h"

void gw_core_init(struct gw_core *g, const struct gw_backend *be, const struct gw_info *info,
		  int64_t now)
{
	memset(g, 0, sizeof(*g));
	g->be = be;
	g->info = *info;
	g->now = now;
	g->boot_ms = now;
	g->x.slot = -1;
	gw_ring_init(&g->arena, g->arena_buf, sizeof(g->arena_buf));
	/* xfer_id is gateway-wide and wrapping (3.2 rule 4); start somewhere
	 * new each boot so a bridge never mistakes a new transfer for an old one. */
	g->xfer_id = (uint16_t)(info->boot_id ^ (info->boot_id >> 16));
	gw_serial_init(g);
	gw_lane_init(g);
#ifdef CONFIG_CTAG_GW_RADIO
	gw_radio_init(g);
#endif
}

void gw_core_start(struct gw_core *g, int64_t now)
{
	g->now = now;
	gw_refresh_inventory(g); /* fills GET_INVENTORY (sim/gateway.py _query_known_bridges) */
	gw_serial_pump(g);
}

void gw_core_mesh_rx(struct gw_core *g, uint16_t src, uint8_t op, const uint8_t *params,
		     size_t len, int64_t now)
{
	struct gw_node *n;

	g->now = now;
	n = gw_node_get(g, src);
	if (n != NULL) {
		n->last_seen = now;
	}
	switch (op) {
	case CTAG_MESH_OP_LAYOUT_STATUS: {
		struct ctag_mesh_layout_status st;

		if (ctag_mesh_layout_status_unpack(&st, params, len) == 0) {
			gw_delivery_status(g, src, &st);
		} else {
			g->c.unexpected_mesh++;
		}
		break;
	}
	case CTAG_MESH_OP_DELIVERY_STAGE: {
		struct ctag_mesh_delivery_stage st;

		if (ctag_mesh_delivery_stage_unpack(&st, params, len) == 0) {
			struct ctag_cbor_field f[4] = {
				GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, st.update_id),
				GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, st.tag_id),
				GW_F_UINT(CTAG_CBOR_KEY_REVISION, st.revision),
				GW_F_UINT(CTAG_CBOR_KEY_STAGE, st.stage),
			};

			(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_STAGE, f, 4u, false);
		} else {
			g->c.unexpected_mesh++;
		}
		break;
	}
	case CTAG_MESH_OP_DELIVERY_RESULT: {
		struct ctag_mesh_delivery_result r;

		if (ctag_mesh_delivery_result_unpack(&r, params, len) == 0) {
			gw_delivery_result(g, src, &r);
		} else {
			g->c.unexpected_mesh++;
		}
		break;
	}
	default:
		gw_nodes_mesh_rx(g, src, op, params, len);
		break;
	}
	gw_serial_pump(g);
}

void gw_core_send_end(struct gw_core *g, uint32_t tag, int err, int64_t now)
{
	g->now = now;
	gw_lane_end(g, tag, err);
	gw_serial_pump(g);
}

void gw_core_poll(struct gw_core *g, int64_t now)
{
	g->now = now;
#ifdef CONFIG_CTAG_GW_RADIO
	/* First: a resume releases the sends that waited for it (below). */
	gw_radio_timers(g);
#endif
	gw_lane_timers(g);
	gw_delivery_timers(g);
	gw_nodes_timers(g);
#ifdef CONFIG_CTAG_GW_SECURE
	gw_tunnel_timers(g);
#endif
	gw_serial_timers(g);
	gw_serial_pump(g);
}

int64_t gw_core_next_deadline(const struct gw_core *g)
{
	int64_t d = gw_lane_deadline(g);

	d = gw_min_deadline(d, gw_delivery_deadline(g));
	d = gw_min_deadline(d, gw_nodes_deadline(g));
#ifdef CONFIG_CTAG_GW_SECURE
	d = gw_min_deadline(d, gw_tunnel_deadline(g));
#endif
#ifdef CONFIG_CTAG_GW_RADIO
	d = gw_min_deadline(d, gw_radio_deadline(g));
#endif
	d = gw_min_deadline(d, gw_serial_deadline(g));
	return d;
}

size_t gw_core_counters(const struct gw_core *g, struct ctag_cbor_counter *items, size_t max)
{
	const struct gw_counters *c = &g->c;
	const struct ctag_serial_rx *rx = &g->s.rx;
	const struct ctag_cbor_counter all[] = {
		CTAG_CBOR_COUNTER("frames_rx", c->frames_rx),
		CTAG_CBOR_COUNTER("frames_tx", c->frames_tx),
		CTAG_CBOR_COUNTER("crc_errors", rx->crc_errors),
		CTAG_CBOR_COUNTER("len_errors", rx->len_errors),
		CTAG_CBOR_COUNTER("version_errors", rx->version_errors),
		CTAG_CBOR_COUNTER("cobs_errors", rx->cobs.errors),
		CTAG_CBOR_COUNTER("oversize", rx->cobs.oversize),
		CTAG_CBOR_COUNTER("unexpected_frames", c->unexpected_frames),
		CTAG_CBOR_COUNTER("overruns", c->overruns),
		CTAG_CBOR_COUNTER("credit_violations", c->credit_violations),
		CTAG_CBOR_COUNTER("unsupported", c->unsupported),
		CTAG_CBOR_COUNTER("invalid", c->invalid),
		CTAG_CBOR_COUNTER("internal_errors", c->internal_errors),
		CTAG_CBOR_COUNTER("hellos", c->hellos),
		CTAG_CBOR_COUNTER("events_dropped", c->events_dropped),
		CTAG_CBOR_COUNTER("events_discarded", c->events_discarded),
		CTAG_CBOR_COUNTER("retained", gw_retained_count(g)),
		CTAG_CBOR_COUNTER("event_seq", g->s.seq),
		CTAG_CBOR_COUNTER("duplicate_ops", c->duplicate_ops),
		CTAG_CBOR_COUNTER("busy", c->busy),
		CTAG_CBOR_COUNTER("deliveries_accepted", c->deliveries_accepted),
		CTAG_CBOR_COUNTER("results", c->results),
		CTAG_CBOR_COUNTER("duplicate_results", c->duplicate_results),
		CTAG_CBOR_COUNTER("repeated_results", c->repeated_results),
		CTAG_CBOR_COUNTER("stale_status", c->stale_status),
		CTAG_CBOR_COUNTER("mesh_send_retries", c->mesh_send_retries),
		CTAG_CBOR_COUNTER("mesh_send_failures", c->mesh_send_failures),
		CTAG_CBOR_COUNTER("mesh_busy", c->mesh_busy),
		CTAG_CBOR_COUNTER("chunks_resent", c->chunks_resent),
		CTAG_CBOR_COUNTER("commit_resends", c->commit_resends),
		CTAG_CBOR_COUNTER("unexpected_mesh", c->unexpected_mesh),
		CTAG_CBOR_COUNTER("unseg_dropped", c->unseg_dropped),
		CTAG_CBOR_COUNTER("provisions", c->provisions),
		CTAG_CBOR_COUNTER("beacons", c->beacons),
		CTAG_CBOR_COUNTER("tag_seen_limited", c->tag_seen_limited),
		CTAG_CBOR_COUNTER("reboots", c->reboots),
		CTAG_CBOR_COUNTER("queue_depth", gw_queue_depth(g)),
		CTAG_CBOR_COUNTER("layout_arena_used", gw_ring_used(&g->arena)),
		CTAG_CBOR_COUNTER("nodes", gw_node_count(g)),
		CTAG_CBOR_COUNTER("assignments", g->assign_count),
		CTAG_CBOR_COUNTER("uptime_s", gw_uptime_s(g)),
	};
	size_t n = sizeof(all) / sizeof(all[0]);

	if (n > max) {
		n = max;
	}
	memcpy(items, all, n * sizeof(all[0]));
#ifdef CONFIG_CTAG_GW_SECURE
	n += gw_v2_counters(g, &items[n], max - n);
#endif
#ifdef CONFIG_CTAG_GW_RADIO
	n += gw_radio_counters(g, &items[n], max - n);
#endif
	if (g->be->counters != NULL && n < max) {
		n += g->be->counters(g->be->ctx, &items[n], max - n);
	}
	return n;
}
