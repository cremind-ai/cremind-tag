/*
 * Mesh transmit (docs/protocol.md 3.2 rule 1): the segmented lane hands the
 * next segmented message to the stack only after the previous one's end
 * callback reported success; a failed end retries the same message up to
 * GW_SEND_RETRIES times. Every segmented message the gateway originates goes
 * through it: LAYOUT_BEGIN/CHUNK, ASSIGN_SET, TAG_CMD and the configuration
 * client's AppKey Add. Unsegmented messages (<= 11 access bytes) queue in a
 * small ring that is re-offered when the stack is out of advertising buffers.
 * While the mesh is suspended for a tag connection on the own radio
 * (gw_mesh_paused(), docs/protocol.md 11.3) both wait the same way.
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

bool gw_is_retryable(int err)
{
	return err == -EBUSY || err == -ENOBUFS || err == -EAGAIN || err == -ENOMEM;
}

void gw_lane_init(struct gw_core *g)
{
	memset(&g->lane, 0, sizeof(g->lane));
	g->lane.active = -1;
	memset(&g->unseg, 0, sizeof(g->unseg));
}

static void lane_go(struct gw_core *g)
{
	struct gw_lane *l = &g->lane;
	uint8_t who = (uint8_t)l->active;

	/* A fresh tag per attempt, so a late end of an earlier attempt is ignored. */
	l->tag_seq = (l->tag_seq + 1u) & 0x00FFFFFFu;
	if (l->tag_seq == 0u) {
		l->tag_seq = 1u;
	}
	l->tag = (l->tag_seq << 8) | who;
	l->retry_at = 0;
	l->watchdog_at = g->now + GW_LANE_WATCHDOG_MS;
	if (gw_mesh_paused(g)) {
		/* 5.2: the mesh is suspended for a tag connection; offered again
		 * later, as after a buffer shortage (not a failed attempt). */
		l->tag = 0u;
		l->retry_at = g->now + GW_RETRY_MS;
#ifdef CONFIG_CTAG_GW_RADIO
		g->radio.c.mesh_paused++;
#endif
		return;
	}
	if (who == GW_REQ_DELIVERY) {
		gw_delivery_lane_go(g);
	} else if (who == GW_REQ_CFG) {
		gw_cfg_lane_go(g);
#ifdef CONFIG_CTAG_GW_SECURE
	} else if (who >= GW_REQ_TUNNEL0) {
		gw_tunnel_lane_go(g, (uint8_t)(who - GW_REQ_TUNNEL0));
#endif
	} else {
		gw_op_lane_go(g, (uint8_t)(who - GW_REQ_OP0));
	}
}

static void lane_pump(struct gw_core *g)
{
	struct gw_lane *l = &g->lane;

	if (l->active >= 0 || l->count == 0u) {
		return;
	}
	l->active = l->fifo[l->head];
	l->head = (uint8_t)((l->head + 1u) % GW_REQ_COUNT);
	l->count--;
	l->attempts = 0u;
	lane_go(g);
}

void gw_lane_request(struct gw_core *g, uint8_t requester)
{
	struct gw_lane *l = &g->lane;

	if (l->active == requester) {
		return;
	}
	for (uint8_t i = 0; i < l->count; i++) {
		if (l->fifo[(l->head + i) % GW_REQ_COUNT] == requester) {
			return;
		}
	}
	l->fifo[(l->head + l->count) % GW_REQ_COUNT] = requester;
	l->count++;
	lane_pump(g);
}

void gw_lane_withdraw(struct gw_core *g, uint8_t requester)
{
	struct gw_lane *l = &g->lane;
	uint8_t kept = 0u;

	for (uint8_t i = 0; i < l->count; i++) {
		uint8_t r = l->fifo[(l->head + i) % GW_REQ_COUNT];

		if (r != requester) {
			l->fifo[(l->head + kept) % GW_REQ_COUNT] = r;
			kept++;
		}
	}
	l->count = kept;
}

bool gw_lane_cancel(struct gw_core *g, uint8_t requester)
{
	if (g->lane.active == requester) {
		return false; /* in flight: its end still reaches the owner */
	}
	gw_lane_withdraw(g, requester);
	return true;
}

static void lane_finish(struct gw_core *g, bool ok)
{
	struct gw_lane *l = &g->lane;
	uint8_t who = (uint8_t)l->active;

	l->active = -1;
	l->tag = 0u;
	l->retry_at = 0;
	l->watchdog_at = 0;
	if (who == GW_REQ_DELIVERY) {
		gw_delivery_lane_done(g, ok);
	} else if (who == GW_REQ_CFG) {
		gw_cfg_lane_done(g, ok);
#ifdef CONFIG_CTAG_GW_SECURE
	} else if (who >= GW_REQ_TUNNEL0) {
		gw_tunnel_lane_done(g, (uint8_t)(who - GW_REQ_TUNNEL0), ok);
#endif
	} else {
		gw_op_lane_done(g, (uint8_t)(who - GW_REQ_OP0), ok);
	}
	lane_pump(g);
}

/* Called by the owners from their lane_go with the stack's return value. */
static void lane_failed_attempt(struct gw_core *g)
{
	struct gw_lane *l = &g->lane;

	l->attempts++;
	if (l->attempts <= GW_SEND_RETRIES) {
		g->c.mesh_send_retries++;
		lane_go(g);
		return;
	}
	g->c.mesh_send_failures++;
	lane_finish(g, false);
}

void gw_lane_issued(struct gw_core *g, int err)
{
	struct gw_lane *l = &g->lane;

	if (err == 0) {
		return; /* wait for the end callback */
	}
	l->tag = 0u;
	if (gw_is_retryable(err)) {
		/* No buffer or a lower-transport slot still busy: not a failed end. */
		g->c.mesh_busy++;
		l->retry_at = g->now + GW_RETRY_MS;
		return;
	}
	lane_failed_attempt(g);
}

void gw_lane_end(struct gw_core *g, uint32_t tag, int err)
{
	struct gw_lane *l = &g->lane;

	if (tag == 0u || l->active < 0 || tag != l->tag) {
		return; /* not the send in flight */
	}
	l->tag = 0u;
	if (err == 0) {
		lane_finish(g, true);
		return;
	}
	lane_failed_attempt(g);
}

void gw_lane_timers(struct gw_core *g)
{
	struct gw_lane *l = &g->lane;
	struct gw_unseg_q *u = &g->unseg;

	if (l->active >= 0 && l->retry_at != 0 && g->now >= l->retry_at) {
		lane_go(g);
	} else if (l->active >= 0 && l->tag != 0u && l->watchdog_at != 0 && g->now >= l->watchdog_at) {
		l->tag = 0u;
		lane_failed_attempt(g); /* the stack never reported the end */
	}
	if (u->retry_at != 0 && g->now >= u->retry_at) {
		u->retry_at = 0;
		(void)gw_unseg_send(g, 0u, 0u, NULL, 0u);
	}
}

int64_t gw_lane_deadline(const struct gw_core *g)
{
	const struct gw_lane *l = &g->lane;
	int64_t d = GW_NEVER;

	if (l->active >= 0 && l->retry_at != 0) {
		d = l->retry_at;
	} else if (l->active >= 0 && l->tag != 0u && l->watchdog_at != 0) {
		d = l->watchdog_at;
	}
	if (g->unseg.retry_at != 0) {
		d = gw_min_deadline(d, g->unseg.retry_at);
	}
	return d;
}

/* ---- Unsegmented messages ---- */

/* Queue one message (dst != 0) and offer the queue to the stack; dst == 0
 * only re-offers. false = the queue was full (dropped). */
bool gw_unseg_send(struct gw_core *g, uint16_t dst, uint8_t op, const uint8_t *params, size_t len)
{
	struct gw_unseg_q *u = &g->unseg;
	bool queued = true;

	if (dst != 0u) {
		if (u->count == CONFIG_CTAG_GW_UNSEG_QUEUE || len > sizeof(u->q[0].params)) {
			g->c.unseg_dropped++;
			queued = false;
		} else {
			struct gw_unseg *m = &u->q[(u->head + u->count) % CONFIG_CTAG_GW_UNSEG_QUEUE];

			m->dst = dst;
			m->op = op;
			m->len = (uint8_t)len;
			if (len > 0u) {
				memcpy(m->params, params, len);
			}
			u->count++;
		}
	}
	if (u->retry_at != 0) {
		return queued; /* waiting for buffers */
	}
	if (u->count > 0u && gw_mesh_paused(g)) {
		/* The mesh is suspended for a tag connection: after the resume. */
		u->retry_at = g->now + GW_RETRY_MS;
#ifdef CONFIG_CTAG_GW_RADIO
		g->radio.c.mesh_paused++;
#endif
		return queued;
	}
	while (u->count > 0u) {
		struct gw_unseg *m = &u->q[u->head];
		int err = g->be->mesh_send(g->be->ctx, m->dst, m->op, m->params, m->len, 0u);

		if (gw_is_retryable(err)) {
			g->c.mesh_busy++;
			u->retry_at = g->now + GW_RETRY_MS;
			break;
		}
		if (err != 0) {
			g->c.mesh_send_failures++;
		}
		u->head = (uint8_t)((u->head + 1u) % CONFIG_CTAG_GW_UNSEG_QUEUE);
		u->count--;
	}
	return queued;
}
