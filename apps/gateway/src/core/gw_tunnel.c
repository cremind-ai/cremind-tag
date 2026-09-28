/*
 * DISCOVER and mesh tunnels (docs/connect-setup.md 5.2, 6; CONFIG_CTAG_GW_SECURE).
 *
 * DISCOVER {op_id, bridge (0 = every configured bridge), duration_s <= 120,
 * tag_id} sends the mesh DISCOVER (unsegmented) and opens a window per
 * bridge; each DISCOVERED from a bridge inside its window becomes
 * EVT_DISCOVERED {bridge, tag_id, rssi, flags}, at most one per (bridge, tag)
 * per DISCOVERED_MIN_INTERVAL_MS (best effort, never inventory).
 *
 * A tunnel carries the secure endpoint's messages between the worker and a
 * bridge (tag_id 0) or a tag behind it. TUNNEL_OPEN sends the mesh
 * TUNNEL_OPEN {tunnel, tag_id, timeout_s} and answers {status, tunnel}; the
 * endpoint's first message up the tunnel (its ident2) becomes EVT_TUNNEL
 * {OPEN, data}, every later one EVT_TUNNEL {DATA, data}. TUNNEL_SEND
 * fragments one message (<= TUNNEL_MSG_MAX) into TUNNEL_DATA of
 * TUNNEL_DATA_MAX bytes (seq from 0, START, END), each an acknowledged
 * segmented send through the lane (one outstanding segmented send
 * gateway-wide); a message whose send fails closes the tunnel. TUNNEL_UP
 * fragments are reassembled in order (a gap drops the message: the Noise
 * session above fails and is opened again); bit7 CLOSE (data = status) is the
 * bridge closing the tunnel. A tunnel without traffic for its timeout plus
 * a grace time is closed with TIMEOUT. One tunnel per bridge (a bridge holds
 * one at a time).
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

#define TUNNEL_GRACE_MS   5000 /* beyond the bridge's own idle timeout */
#define DISCOVER_GRACE_MS 2000 /* DISCOVERED already on its way when the window ends */

static const char T_DURATION[] = "duration_s out of range";
static const char T_TUNNEL_BUSY[] = "a tunnel to this bridge is open";
static const char T_NO_TUNNEL[] = "no such tunnel";

/* ---- DISCOVER ---- */

static void discover_to(struct gw_core *g, struct gw_node *n, const uint8_t *p, uint32_t duration_s)
{
	(void)gw_unseg_send(g, n->addr, CTAG_MESH_OP_DISCOVER, p, CTAG_MESH_DISCOVER_LEN);
	n->discover_until = duration_s > 0u ? g->now + (int64_t)duration_s * 1000 + DISCOVER_GRACE_MS
					    : 0;
}

struct gw_req_result gw_discover(struct gw_core *g, uint16_t bridge, uint32_t duration_s,
				 uint32_t tag_id)
{
	struct ctag_mesh_discover m = {.duration_s = (uint8_t)duration_s, .tag_id = tag_id};
	uint8_t p[CTAG_MESH_DISCOVER_LEN];

	if (duration_s > CTAG_DISCOVER_MAX_S) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_DURATION};
	}
	(void)ctag_mesh_discover_pack(&m, p, sizeof(p));
	if (bridge != 0u) {
		struct gw_req_result r = gw_node_usable(g, bridge);

		if (r.status != CTAG_STATUS_OK) {
			return r;
		}
		discover_to(g, gw_node_get(g, bridge), p, duration_s);
	} else {
		for (int i = 0; i < GW_NODES; i++) {
			if (g->nodes[i].used && g->nodes[i].configured) {
				discover_to(g, &g->nodes[i], p, duration_s);
			}
		}
	}
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

static void discovered(struct gw_core *g, uint16_t src, const struct ctag_mesh_discovered *d)
{
	struct gw_node *n = gw_node_get(g, src);
	struct gw_discovered *e = NULL;

	if (n == NULL || n->discover_until == 0 || g->now > n->discover_until) {
		g->c.unexpected_mesh++; /* no window open: candidates are asked for, never kept */
		return;
	}
	for (int i = 0; i < CONFIG_CTAG_GW_DISCOVERED_SLOTS; i++) {
		struct gw_discovered *c = &g->v2.discovered[i];

		if (c->last != 0 && c->bridge == src && c->tag_id == d->tag_id) {
			e = c;
			break;
		}
	}
	if (e != NULL && g->now - e->last < CTAG_DISCOVERED_MIN_INTERVAL_MS) {
		g->v2.c.discovered_limited++;
		return;
	}
	if (e == NULL) {
		e = &g->v2.discovered[0];
		for (int i = 1; i < CONFIG_CTAG_GW_DISCOVERED_SLOTS; i++) {
			if (g->v2.discovered[i].last < e->last) {
				e = &g->v2.discovered[i];
			}
		}
		e->bridge = src;
		e->tag_id = d->tag_id;
	}
	e->last = g->now;
	g->v2.c.discovered++;
	{
		struct ctag_cbor_field f[4] = {
			GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, src),
			GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, d->tag_id),
			GW_F_INT(CTAG_CBOR_KEY_RSSI, d->rssi),
			GW_F_UINT(CTAG_CBOR_KEY_FLAGS, d->flags),
		};

		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_DISCOVERED, f, 4u, false);
	}
}

/* ---- Tunnels ---- */

static struct gw_tunnel *by_id(struct gw_core *g, uint32_t id)
{
	for (int i = 0; i < GW_TUNNELS; i++) {
		if (g->v2.tunnels[i].used && g->v2.tunnels[i].id == id) {
			return &g->v2.tunnels[i];
		}
	}
	return NULL;
}

static uint8_t slot_of(const struct gw_core *g, const struct gw_tunnel *t)
{
	return (uint8_t)(t - g->v2.tunnels);
}

static void tunnel_event(struct gw_core *g, const struct gw_tunnel *t, uint8_t state,
			 const uint8_t *data, size_t len, int status)
{
	struct ctag_cbor_field f[6] = {
		GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, t->id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, t->bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, t->tag_id),
		GW_F_UINT(CTAG_CBOR_KEY_STATE, state),
	};
	size_t n = 4u;

	if (data != NULL) {
		f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_DATA, data, len);
	}
	if (status >= 0) {
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, (uint32_t)status);
	}
	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_TUNNEL, f, n, false);
}

static void mesh_close(struct gw_core *g, const struct gw_tunnel *t, uint8_t status)
{
	struct ctag_mesh_tunnel_close m = {.tunnel = t->id, .status = status};
	uint8_t p[CTAG_MESH_TUNNEL_CLOSE_LEN];

	(void)ctag_mesh_tunnel_close_pack(&m, p, sizeof(p));
	(void)gw_unseg_send(g, t->bridge, CTAG_MESH_OP_TUNNEL_CLOSE, p, sizeof(p));
}

/* The slot is free; a fragment still in flight keeps it reserved until its end. */
static void tunnel_free(struct gw_core *g, struct gw_tunnel *t)
{
	if (t->tx_busy && gw_lane_cancel(g, (uint8_t)(GW_REQ_TUNNEL0 + slot_of(g, t)))) {
		t->tx_busy = false;
	}
	t->used = false;
	t->opened = false;
	t->tx_len = t->tx_off = 0u;
	ctag_tunnel_rx_init(&t->rx, t->rxbuf, sizeof(t->rxbuf));
}

static void touch(struct gw_core *g, struct gw_tunnel *t)
{
	t->idle_at = g->now + (int64_t)t->timeout_s * 1000 + TUNNEL_GRACE_MS;
}

struct gw_req_result gw_tunnel_open(struct gw_core *g, uint16_t bridge, uint32_t tag_id,
				    uint32_t duration_s, uint16_t *tunnel)
{
	struct gw_req_result r = gw_node_usable(g, bridge);
	struct gw_tunnel *t = NULL;
	struct ctag_mesh_tunnel_open m;
	uint8_t p[CTAG_MESH_TUNNEL_OPEN_LEN];

	if (r.status != CTAG_STATUS_OK) {
		return r;
	}
	if (duration_s == 0u || duration_s > UINT8_MAX) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_DURATION};
	}
	for (int i = 0; i < GW_TUNNELS; i++) {
		struct gw_tunnel *c = &g->v2.tunnels[i];

		if (c->used && c->bridge == bridge) {
			g->c.busy++;
			return (struct gw_req_result){CTAG_STATUS_BUSY, T_TUNNEL_BUSY};
		}
		if (t == NULL && !c->used && !c->tx_busy) {
			t = c;
		}
	}
	if (t == NULL) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL};
	}
	/* A fresh non-zero id, never one in use. */
	do {
		g->v2.tunnel_next = (uint16_t)(g->v2.tunnel_next == UINT16_MAX ? 1u
									: g->v2.tunnel_next + 1u);
	} while (by_id(g, g->v2.tunnel_next) != NULL);
	memset(t, 0, sizeof(*t));
	t->used = true;
	t->id = g->v2.tunnel_next;
	t->bridge = bridge;
	t->tag_id = tag_id;
	t->timeout_s = (uint8_t)duration_s;
	ctag_tunnel_rx_init(&t->rx, t->rxbuf, sizeof(t->rxbuf));
	touch(g, t);
	m = (struct ctag_mesh_tunnel_open){.tunnel = t->id, .tag_id = tag_id, .timeout_s = t->timeout_s};
	(void)ctag_mesh_tunnel_open_pack(&m, p, sizeof(p));
	(void)gw_unseg_send(g, bridge, CTAG_MESH_OP_TUNNEL_OPEN, p, sizeof(p));
	g->v2.c.tunnels_opened++;
	*tunnel = t->id;
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

struct gw_req_result gw_tunnel_send(struct gw_core *g, uint32_t tunnel, const uint8_t *data,
				    size_t len)
{
	struct gw_tunnel *t = by_id(g, tunnel);

	if (t == NULL) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_NO_TUNNEL};
	}
	if (len == 0u) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, NULL};
	}
	if (len > CTAG_TUNNEL_MSG_MAX) {
		return (struct gw_req_result){CTAG_STATUS_TOO_LARGE, NULL};
	}
	if (t->tx_busy) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL}; /* one message at a time */
	}
	memcpy(t->tx, data, len);
	t->tx_len = (uint16_t)len;
	t->tx_off = 0u;
	t->tx_busy = true;
	touch(g, t);
	gw_lane_request(g, (uint8_t)(GW_REQ_TUNNEL0 + slot_of(g, t)));
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

struct gw_req_result gw_tunnel_close(struct gw_core *g, uint32_t tunnel)
{
	struct gw_tunnel *t = by_id(g, tunnel);

	if (t == NULL) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_NO_TUNNEL};
	}
	mesh_close(g, t, CTAG_STATUS_OK);
	tunnel_free(g, t);
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

void gw_tunnel_lane_go(struct gw_core *g, uint8_t slot)
{
	struct gw_tunnel *t = &g->v2.tunnels[slot];
	struct ctag_mesh_tunnel_data m = {.tunnel = t->id};
	uint8_t p[CTAG_MESH_TUNNEL_DATA_MAX_LEN];
	int n;

	if (!t->used || !t->tx_busy) {
		gw_lane_issued(g, -EINVAL); /* closed meanwhile */
		return;
	}
	n = ctag_tunnel_frag(t->tx_len, t->tx_off, &m.seq, &m.flags);
	if (n <= 0) {
		gw_lane_issued(g, -EINVAL);
		return;
	}
	m.data = &t->tx[t->tx_off];
	m.data_len = (size_t)n;
	n = ctag_mesh_tunnel_data_pack(&m, p, sizeof(p));
	gw_lane_issued(g, n < 0 ? n
				: g->be->mesh_send(g->be->ctx, t->bridge, CTAG_MESH_OP_TUNNEL_DATA, p,
						   (size_t)n, g->lane.tag));
}

void gw_tunnel_lane_done(struct gw_core *g, uint8_t slot, bool ok)
{
	struct gw_tunnel *t = &g->v2.tunnels[slot];
	uint8_t seq, flags;
	int n;

	if (!t->used) {
		t->tx_busy = false; /* closed while its fragment was in flight */
		return;
	}
	if (!ok) {
		/* The message is lost: the tunnel ends (the worker opens another). */
		mesh_close(g, t, CTAG_STATUS_TIMEOUT);
		tunnel_event(g, t, CTAG_TUNNEL_CLOSED, NULL, 0u, CTAG_STATUS_TIMEOUT);
		t->tx_busy = false;
		tunnel_free(g, t);
		return;
	}
	n = ctag_tunnel_frag(t->tx_len, t->tx_off, &seq, &flags);
	t->tx_off = (uint16_t)(t->tx_off + (n > 0 ? (unsigned int)n : 0u));
	touch(g, t);
	if (n > 0 && t->tx_off < t->tx_len) {
		gw_lane_request(g, (uint8_t)(GW_REQ_TUNNEL0 + slot));
	} else {
		t->tx_busy = false;
	}
}

static void tunnel_up(struct gw_core *g, uint16_t src, const struct ctag_mesh_tunnel_up *m)
{
	struct gw_tunnel *t = by_id(g, m->tunnel);
	size_t len = 0u;
	int rc;

	if (t == NULL || t->bridge != src) {
		g->c.unexpected_mesh++;
		return;
	}
	touch(g, t);
	if ((m->flags & CTAG_TUNNEL_FRAG_CLOSE) != 0u) {
		/* The bridge (or the tag behind it) closed the tunnel. */
		tunnel_event(g, t, CTAG_TUNNEL_CLOSED, NULL, 0u,
			     m->data_len > 0u ? m->data[0] : CTAG_STATUS_INTERNAL);
		tunnel_free(g, t);
		return;
	}
	rc = ctag_tunnel_rx_feed(&t->rx, m->seq, m->flags, m->data, m->data_len, &len);
	if (rc < 0) {
		g->v2.c.tunnel_gaps++; /* the message is lost; the session above notices */
		return;
	}
	if (rc == 1) {
		tunnel_event(g, t, t->opened ? CTAG_TUNNEL_DATA : CTAG_TUNNEL_OPEN, t->rxbuf, len, -1);
		t->opened = true;
	}
}

void gw_tunnel_mesh_rx(struct gw_core *g, uint16_t src, uint8_t op, const uint8_t *p, size_t len)
{
	if (op == CTAG_MESH_OP_DISCOVERED) {
		struct ctag_mesh_discovered d;

		if (ctag_mesh_discovered_unpack(&d, p, len) == 0) {
			discovered(g, src, &d);
			return;
		}
	} else if (op == CTAG_MESH_OP_TUNNEL_UP) {
		struct ctag_mesh_tunnel_up m;

		if (ctag_mesh_tunnel_up_unpack(&m, p, len) == 0) {
			tunnel_up(g, src, &m);
			return;
		}
	}
	g->c.unexpected_mesh++;
}

void gw_tunnel_timers(struct gw_core *g)
{
	for (int i = 0; i < GW_TUNNELS; i++) {
		struct gw_tunnel *t = &g->v2.tunnels[i];

		if (t->used && g->now >= t->idle_at) {
			mesh_close(g, t, CTAG_STATUS_TIMEOUT);
			tunnel_event(g, t, CTAG_TUNNEL_CLOSED, NULL, 0u, CTAG_STATUS_TIMEOUT);
			tunnel_free(g, t);
		}
	}
}

int64_t gw_tunnel_deadline(const struct gw_core *g)
{
	int64_t d = GW_NEVER;

	for (int i = 0; i < GW_TUNNELS; i++) {
		if (g->v2.tunnels[i].used) {
			d = gw_min_deadline(d, g->v2.tunnels[i].idle_at);
		}
	}
	return d;
}

void gw_tunnel_reset(struct gw_core *g)
{
	for (int i = 0; i < GW_TUNNELS; i++) {
		if (g->v2.tunnels[i].used) {
			tunnel_free(g, &g->v2.tunnels[i]);
		}
	}
	memset(g->v2.discovered, 0, sizeof(g->v2.discovered));
}
