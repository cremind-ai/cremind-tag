/*
 * Tags on the gateway's own radio (docs/protocol.md 11; CONFIG_CTAG_GW_RADIO).
 *
 * The gateway is a thin BLE relay here: the companion runs the tag session
 * (handshake, records, frames) and renders; the gateway connects to the tag,
 * sets GATT up and carries whole messages between the serial tunnel
 * (TUNNEL_SEND, EVT_TUNNEL) and the tag's characteristics, fragmented and
 * reassembled per 5.3. It never looks into them and holds no tag key.
 *
 *   TUNNEL_OPEN {bridge GATEWAY_ADDR, tag_id, duration_s, mode}
 *        -> WAITING: the tag's advertisement (ver 1 or 2) within duration_s
 *           goes to the scheduler (lib/sched: the mesh suspend window of 5.2,
 *           the rate limit, the per-tag back-off and its quick retry);
 *           not connected in time -> EVT_TUNNEL CLOSED TIMEOUT
 *        -> SETUP (connected, the mesh resumed): the platform discovers the
 *           tag service, subscribes (CTRL + STATUS, or PAIR) and reads CAPS
 *           (SESSION) or IDENT (PAIR) within SETUP_MS
 *        -> OPEN: EVT_TUNNEL {OPEN, data = the value read, rssi}; then
 *           messages both ways until TUNNEL_CLOSE (no event), the link drops
 *           (DISCONNECTED), a 5.3 violation or a failed write (INVALID /
 *           DISCONNECTED), or duration_s (+ grace) without a message
 *           (TIMEOUT)
 *
 * Sending: TUNNEL_SEND queues one message (two per tunnel); messages leave in
 * order, one at a time, fragmented with the gateway's own SEQ per
 * characteristic (from 0 at the connection). CTRL and PAIR fragments are
 * written with response, one outstanding; DATA fragments without response,
 * CONFIG_CTAG_GW_LINK_INFLIGHT outstanding. A message is done when its last
 * fragment completed. A host out of buffers is asked again after GW_RETRY_MS.
 *
 * The scheduler's Bluetooth side is struct gw_backend (radio_*, link_*); its
 * decisions about the mesh read the core's own state (the lane, the
 * unsegmented queue, provisioning and configuration). While the mesh is
 * suspended, gw_mesh_paused() holds every mesh send back (gw_lane.c,
 * gw_nodes.c).
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

#if SCHED_LINKS != CONFIG_CTAG_GW_TAG_LINKS
#error "CONFIG_CTAG_SCHED_LINKS must equal CONFIG_CTAG_GW_TAG_LINKS (Kconfig.gateway)"
#endif

#define SETUP_MS          5000 /* GATT discovery, subscriptions and the first read */
#define SEND_TYPE_CTRL_LO 0x01u /* 11.3: a SESSION message's first byte picks the characteristic */
#define SEND_TYPE_CTRL_HI 0x0Fu
#define SEND_TYPE_DATA_LO 0x10u
#define SEND_TYPE_DATA_HI 0x2Fu

static const char T_TAG[] = "tag_id 0 on the gateway's own radio";
static const char T_DURATION[] = "duration_s out of range";
static const char T_MODE[] = "unknown mode";
static const char T_TAG_BUSY[] = "a tunnel to this tag is open";
static const char T_TYPE[] = "not a CTRL message or a DATA record";
static const char T_NOT_OPEN[] = "the tunnel is not open yet";

/* ---- Tunnels and links ---- */

static struct gw_rtunnel *tunnel_by_id(struct gw_core *g, uint32_t id)
{
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		struct gw_rtunnel *t = &g->radio.tunnels[i];

		if (t->state != GW_RT_FREE && t->id == id) {
			return t;
		}
	}
	return NULL;
}

bool gw_radio_has_tunnel(const struct gw_core *g, uint32_t id)
{
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		if (g->radio.tunnels[i].state != GW_RT_FREE && g->radio.tunnels[i].id == id) {
			return true;
		}
	}
	return false;
}

/* The tunnel waiting for tag_id's advertisement, if any. */
static struct gw_rtunnel *waiting_for(struct gw_core *g, uint32_t tag_id)
{
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		struct gw_rtunnel *t = &g->radio.tunnels[i];

		if (t->state == GW_RT_WAITING && t->tag_id == tag_id) {
			return t;
		}
	}
	return NULL;
}

/* The tunnel bound to a link (SETUP or OPEN), if any. */
static struct gw_rtunnel *bound(struct gw_core *g, uint8_t link)
{
	if (link >= SCHED_LINKS || g->radio.links[link].tunnel < 0) {
		return NULL;
	}
	return &g->radio.tunnels[g->radio.links[link].tunnel];
}

/* The scheduler is connecting to tag_id right now (suspend window). */
static bool attempting(const struct gw_core *g, uint32_t tag_id)
{
	uint8_t li = sched_link_of(&g->radio.sched, tag_id);

	return li != SCHED_NO_LINK && g->radio.sched.links[li].state == SCHED_LINK_ATTEMPT;
}

/* Tell the platform whether advertisements are wanted (so its scan callback
 * does not fill the event queue with them otherwise). */
static void listen_update(struct gw_core *g)
{
	struct gw_radio *r = &g->radio;
	bool want = r->disc_until != 0;

	for (int i = 0; !want && i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		want = r->tunnels[i].state == GW_RT_WAITING;
	}
	if (want != r->listening) {
		r->listening = want;
		if (g->be->radio_listen != NULL) {
			g->be->radio_listen(g->be->ctx, want);
		}
	}
}

static void touch(struct gw_core *g, struct gw_rtunnel *t)
{
	/* 11.3: no message either way for duration_s once connected: TIMEOUT. */
	t->deadline = g->now + (int64_t)t->timeout_s * 1000 + GW_TUNNEL_GRACE_MS;
}

static void tunnel_event(struct gw_core *g, const struct gw_rtunnel *t, uint8_t state,
			 const uint8_t *data, size_t len, int status)
{
	/* OPEN also carries the RSSI of the advertisement it connected on. */
	gw_tunnel_event(g, t->id, GW_ADDR, t->tag_id, state, data, len, status,
			state == CTAG_TUNNEL_OPEN ? &t->rssi : NULL);
}

/* The connection is the tunnel's now: fresh 5.3 state both ways. */
static void link_bind(struct gw_core *g, uint8_t li, struct gw_rtunnel *t)
{
	struct gw_rlink *l = &g->radio.links[li];
	int8_t rssi = l->adv_rssi;

	memset(l, 0, sizeof(*l));
	l->tunnel = (int8_t)(t - g->radio.tunnels);
	l->adv_rssi = rssi;
	for (int i = 0; i < GW_CHR_COUNT; i++) {
		ctag_frag_tx_init(&l->tx[i]);
	}
	if (t->mode == CTAG_TUNNEL_MODE_SESSION) {
		ctag_frag_rx_init(&l->rx[0], l->rxbuf, CTAG_TAG_CTRL_MSG_MAX);
		ctag_frag_rx_init(&l->rx[1], &l->rxbuf[CTAG_TAG_CTRL_MSG_MAX],
				  CTAG_TAG_RECORD_WIRE_MAX);
	} else {
		ctag_frag_rx_init(&l->rx[0], l->rxbuf, CTAG_PAIR_MSG_MAX);
		ctag_frag_rx_init(&l->rx[1], l->rxbuf, 0u);
	}
	t->link = li;
	t->rssi = rssi;
}

static void link_unbind(struct gw_core *g, struct gw_rtunnel *t)
{
	if (t->link < SCHED_LINKS) {
		struct gw_rlink *l = &g->radio.links[t->link];

		/* Completions of fragments still in flight find no tunnel. */
		l->tunnel = -1;
		l->q_head = l->q_count = 0u;
		l->off = 0u;
		l->inflight = 0u;
		l->retry_at = 0;
	}
	t->link = SCHED_NO_LINK;
}

/*
 * End a tunnel: EVT_TUNNEL CLOSED with status (< 0: no event, as for
 * TUNNEL_CLOSE and RELEASE), the slot freed, and its connection handed back
 * to the scheduler with link_status, which takes it down (a failure also
 * starts the tag's back-off, 5.2 step 2).
 */
static void rt_close(struct gw_core *g, struct gw_rtunnel *t, int status, uint8_t link_status)
{
	uint8_t li = t->link;

	if (status >= 0) {
		tunnel_event(g, t, CTAG_TUNNEL_CLOSED, NULL, 0u, status);
	}
	link_unbind(g, t);
	t->state = GW_RT_FREE;
	if (li != SCHED_NO_LINK) {
		sched_session_done(&g->radio.sched, li, link_status);
	}
	listen_update(g);
}

/* ---- Sending (11.3) ---- */

static bool host_busy(int err)
{
	return err == -EAGAIN || err == -ENOMEM || err == -ENOBUFS;
}

/* Hand the next fragments of the queued messages to the host. */
static void pump(struct gw_core *g, uint8_t li)
{
	struct gw_rlink *l = &g->radio.links[li];
	struct gw_rtunnel *t = bound(g, li);

	while (t != NULL && t->state == GW_RT_OPEN && l->retry_at == 0 && l->q_count > 0u) {
		const struct gw_rmsg *m = &l->q[l->q_head];
		uint8_t window = m->chr == GW_CHR_DATA ? CONFIG_CTAG_GW_LINK_INFLIGHT : 1u;
		uint8_t frag[CTAG_FRAG_PAYLOAD_MAX + 1u];
		struct ctag_frag_tx seq = l->tx[m->chr];
		size_t off = l->off;
		int n;
		int err;

		if (l->off >= m->len) {
			if (l->inflight > 0u) {
				return; /* its last fragments complete first */
			}
			/* Written: the next message may start (one at a time). */
			l->q_head = (uint8_t)((l->q_head + 1u) % GW_RADIO_TXQ);
			l->q_count--;
			l->off = 0u;
			touch(g, t);
			continue;
		}
		if (l->inflight >= window) {
			return;
		}
		n = ctag_frag_next(&l->tx[m->chr], m->data, m->len, &off, CTAG_FRAG_PAYLOAD_MAX,
				   frag);
		err = n < 0 ? -EINVAL : g->be->link_write(g->be->ctx, li, m->chr, frag, (size_t)n);
		if (host_busy(err)) {
			/* No buffer: the same fragment (same SEQ) again later. */
			l->tx[m->chr] = seq;
			l->retry_at = g->now + GW_RETRY_MS;
			return;
		}
		if (err != 0) {
			rt_close(g, t, CTAG_STATUS_DISCONNECTED, CTAG_STATUS_DISCONNECTED);
			return;
		}
		l->off = (uint16_t)off;
		l->inflight++;
	}
}

static struct gw_req_result send(struct gw_core *g, struct gw_rtunnel *t, const uint8_t *data,
				  size_t len)
{
	struct gw_rlink *l;
	struct gw_rmsg *m;
	uint8_t chr = GW_CHR_PAIR;
	size_t max = CTAG_PAIR_MSG_MAX;

	if (len == 0u) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, NULL};
	}
	if (t->mode == CTAG_TUNNEL_MODE_SESSION) {
		if (data[0] >= SEND_TYPE_CTRL_LO && data[0] <= SEND_TYPE_CTRL_HI) {
			chr = GW_CHR_CTRL;
			max = CTAG_TAG_CTRL_MSG_MAX;
		} else if (data[0] >= SEND_TYPE_DATA_LO && data[0] <= SEND_TYPE_DATA_HI) {
			chr = GW_CHR_DATA;
			max = CTAG_TAG_RECORD_WIRE_MAX;
		} else {
			return (struct gw_req_result){CTAG_STATUS_INVALID, T_TYPE};
		}
	}
	if (len > max) {
		return (struct gw_req_result){CTAG_STATUS_TOO_LARGE, NULL};
	}
	if (t->state != GW_RT_OPEN) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, T_NOT_OPEN};
	}
	l = &g->radio.links[t->link];
	if (l->q_count == GW_RADIO_TXQ) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL};
	}
	m = &l->q[(l->q_head + l->q_count) % GW_RADIO_TXQ];
	m->chr = chr;
	m->len = (uint16_t)len;
	memcpy(m->data, data, len);
	l->q_count++;
	touch(g, t);
	pump(g, t->link); /* may close the tunnel (a failed link): the event follows the answer */
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

/* ---- Requests ---- */

void gw_radio_discover(struct gw_core *g, uint32_t duration_s, uint32_t tag_id)
{
	struct gw_radio *r = &g->radio;

	/* 11.2: a window like a bridge's (duration_s + the same grace). */
	r->disc_until = duration_s > 0u
				? g->now + (int64_t)duration_s * 1000 + GW_DISCOVER_GRACE_MS
				: 0;
	r->disc_tag = tag_id;
	listen_update(g);
}

struct gw_req_result gw_radio_open(struct gw_core *g, uint32_t tag_id, uint32_t duration_s,
				   uint32_t mode, uint16_t *tunnel)
{
	struct gw_radio *r = &g->radio;
	struct gw_rtunnel *t = NULL;

	if (tag_id == 0u) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_TAG};
	}
	if (duration_s == 0u || duration_s > UINT8_MAX) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_DURATION};
	}
	if (mode != CTAG_TUNNEL_MODE_PAIR && mode != CTAG_TUNNEL_MODE_SESSION) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_MODE};
	}
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		struct gw_rtunnel *c = &r->tunnels[i];

		if (c->state != GW_RT_FREE && c->tag_id == tag_id) {
			g->c.busy++;
			return (struct gw_req_result){CTAG_STATUS_BUSY, T_TAG_BUSY};
		}
		if (t == NULL && c->state == GW_RT_FREE) {
			t = c;
		}
	}
	if (t == NULL) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL};
	}
	memset(t, 0, sizeof(*t));
	t->id = gw_tunnel_new_id(g); /* the mesh tunnels' id space */
	t->state = GW_RT_WAITING;
	t->mode = (uint8_t)mode;
	t->timeout_s = (uint8_t)duration_s;
	t->link = SCHED_NO_LINK;
	t->tag_id = tag_id;
	t->deadline = g->now + (int64_t)duration_s * 1000;
	r->c.tunnels++;
	*tunnel = t->id;
	listen_update(g);
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

bool gw_radio_send(struct gw_core *g, uint32_t tunnel, const uint8_t *data, size_t len,
		   struct gw_req_result *r)
{
	struct gw_rtunnel *t = tunnel_by_id(g, tunnel);

	if (t == NULL) {
		return false;
	}
	*r = send(g, t, data, len);
	return true;
}

bool gw_radio_close(struct gw_core *g, uint32_t tunnel, struct gw_req_result *r)
{
	struct gw_rtunnel *t = tunnel_by_id(g, tunnel);

	if (t == NULL) {
		return false;
	}
	/* 11.3: the host closed it: the link goes down, no event. */
	rt_close(g, t, -1, CTAG_STATUS_OK);
	*r = (struct gw_req_result){CTAG_STATUS_OK, NULL};
	return true;
}

/* ---- Radio input ---- */

void gw_core_tag_adv(struct gw_core *g, const struct sched_peer *peer, uint32_t tag_id, uint8_t ver,
		     uint8_t flags, int8_t rssi, int64_t now)
{
	struct gw_radio *r = &g->radio;

	g->now = now;
	r->c.adverts++;
	if (ver != 1u && ver != CTAG_SECURE_PROTO_VERSION) {
		goto out;
	}
	/* 11.2: v2 tags in setup mode (the filter's tag) are candidates. */
	if (r->disc_until != 0 && now < r->disc_until && ver == CTAG_SECURE_PROTO_VERSION &&
	    (flags & CTAG_ADV_FLAG_SETUP) != 0u && (r->disc_tag == 0u || r->disc_tag == tag_id)) {
		gw_discovered(g, GW_ADDR, tag_id, rssi, flags);
	}
	/* 11.3: a waiting tunnel's tag: the scheduler decides (5.2). */
	if (waiting_for(g, tag_id) != NULL) {
		uint8_t before = sched_link_of(&r->sched, tag_id);
		uint8_t li;

		sched_advert(&r->sched, tag_id, peer);
		li = sched_link_of(&r->sched, tag_id);
		if (before == SCHED_NO_LINK && li != SCHED_NO_LINK) {
			r->links[li].adv_rssi = rssi; /* this advertisement started the attempt */
		}
	}
out:
	gw_serial_pump(g);
}

void gw_core_link_connected(struct gw_core *g, uint8_t link, uint8_t hci_err, int64_t now)
{
	g->now = now;
	sched_connected(&g->radio.sched, link, hci_err);
	gw_serial_pump(g);
}

void gw_core_link_disconnected(struct gw_core *g, uint8_t link, uint8_t reason, int64_t now)
{
	(void)reason;
	g->now = now;
	/* A link under a tunnel: session_abort closes it DISCONNECTED. */
	sched_disconnected(&g->radio.sched, link);
	gw_serial_pump(g);
}

void gw_core_link_ready(struct gw_core *g, uint8_t link, uint8_t status, const uint8_t *value,
			size_t len, int64_t now)
{
	struct gw_rtunnel *t;

	g->now = now;
	t = bound(g, link);
	if (t == NULL || t->state != GW_RT_SETUP) {
		g->radio.c.stray++;
		goto out;
	}
	if (status == CTAG_STATUS_OK && (value == NULL || len == 0u)) {
		status = CTAG_STATUS_INVALID; /* nothing read: nothing the companion can check */
	}
	if (status != CTAG_STATUS_OK) {
		/* UNSUPPORTED: no tag service or characteristics of the mode;
		 * INVALID: a GATT step failed; TIMEOUT. */
		rt_close(g, t, status, status);
		goto out;
	}
	t->state = GW_RT_OPEN;
	touch(g, t);
	tunnel_event(g, t, CTAG_TUNNEL_OPEN, value, len, -1);
out:
	gw_serial_pump(g);
}

void gw_core_link_rx(struct gw_core *g, uint8_t link, uint8_t chr, const uint8_t *value, size_t len,
		     int64_t now)
{
	struct gw_rtunnel *t;
	struct ctag_frag_rx *rx = NULL;
	int n;

	g->now = now;
	t = bound(g, link);
	if (t == NULL || t->state != GW_RT_OPEN) {
		g->radio.c.stray++; /* nothing is relayed before OPEN */
		goto out;
	}
	if (t->mode == CTAG_TUNNEL_MODE_SESSION) {
		rx = chr == GW_CHR_CTRL     ? &g->radio.links[link].rx[0]
		     : chr == GW_CHR_STATUS ? &g->radio.links[link].rx[1]
					    : NULL;
	} else if (chr == GW_CHR_PAIR) {
		rx = &g->radio.links[link].rx[0];
	}
	if (rx == NULL) {
		g->radio.c.stray++;
		goto out;
	}
	/* 5.3 per characteristic; a violation (or a value too long for the
	 * event, value NULL) ends the tunnel. */
	n = value != NULL ? ctag_frag_rx_put(rx, value, len) : -EINVAL;
	if (n < 0) {
		rt_close(g, t, CTAG_STATUS_INVALID, CTAG_STATUS_INVALID);
		goto out;
	}
	if (n > 0) {
		touch(g, t);
		tunnel_event(g, t, CTAG_TUNNEL_DATA, rx->buf, (size_t)n, -1);
	}
out:
	gw_serial_pump(g);
}

void gw_core_link_written(struct gw_core *g, uint8_t link, uint8_t chr, int err, int64_t now)
{
	struct gw_rtunnel *t;

	g->now = now;
	t = bound(g, link);
	if (t == NULL || t->state != GW_RT_OPEN || g->radio.links[link].inflight == 0u) {
		g->radio.c.stray++;
		goto out;
	}
	g->radio.links[link].inflight--;
	if (err != 0) {
		/* The tag refused a write with response (an ATT error): INVALID;
		 * anything else lost the link. */
		uint8_t st = err > 0 && chr != GW_CHR_DATA ? CTAG_STATUS_INVALID
							   : CTAG_STATUS_DISCONNECTED;

		rt_close(g, t, st, st);
		goto out;
	}
	pump(g, link);
out:
	gw_serial_pump(g);
}

/* ---- The scheduler's side (sched_ops) ---- */

/* Everything that waited for the resume goes at the next poll. */
static void mesh_kick(struct gw_core *g)
{
	if (g->lane.retry_at != 0) {
		g->lane.retry_at = g->now;
	}
	if (g->unseg.retry_at != 0) {
		g->unseg.retry_at = g->now;
	}
	if (g->cfg.kind != GW_CFGOP_NONE && g->cfg.retry_at != 0) {
		g->cfg.retry_at = g->now;
	}
}

static bool op_ready(void *ctx)
{
	const struct gw_core *g = ctx;

	/* 5.2 step 3: never while it provisions or configures a node (or is
	 * about to reboot). */
	return !g->prov.active && g->cfg.kind == GW_CFGOP_NONE && !g->cfg.lane_busy &&
	       g->s.reboot_at == 0;
}

static bool op_work(void *ctx, uint32_t tag_id)
{
	return waiting_for(ctx, tag_id) != NULL;
}

static bool op_busy(void *ctx)
{
	const struct gw_core *g = ctx;

	/* Own segmented sends (the lane) and queued unsegmented ones finish first. */
	return g->lane.active >= 0 || g->lane.count > 0u || g->unseg.count > 0u;
}

static bool op_link_idle(void *ctx, uint8_t link)
{
	struct gw_core *g = ctx;
	const struct gw_rtunnel *t = bound(g, link);
	const struct gw_rlink *l = &g->radio.links[link];

	/* 11.3: a further connection only while every connected tunnel has
	 * nothing left to write. */
	return t != NULL && t->state == GW_RT_OPEN && l->q_count == 0u && l->inflight == 0u;
}

static int op_suspend(void *ctx)
{
	struct gw_core *g = ctx;
	int err = g->be->radio_suspend != NULL ? g->be->radio_suspend(g->be->ctx) : -EINVAL;

	if (err == 0 || err == -EALREADY) {
		g->radio.mesh_suspended = true;
	}
	return err;
}

static int op_resume(void *ctx)
{
	struct gw_core *g = ctx;
	int err = g->be->radio_resume != NULL ? g->be->radio_resume(g->be->ctx) : 0;

	if (err == 0 || err == -EALREADY) {
		g->radio.mesh_suspended = false;
		mesh_kick(g);
	}
	return err;
}

static int op_create(void *ctx, uint8_t link, const struct sched_peer *peer, uint32_t timeout_ms)
{
	struct gw_core *g = ctx;

	return g->be->link_connect != NULL
		       ? g->be->link_connect(g->be->ctx, link, peer, timeout_ms)
		       : -ENOTSUP;
}

static int op_disconnect(void *ctx, uint8_t link)
{
	struct gw_core *g = ctx;

	return g->be->link_disconnect != NULL ? g->be->link_disconnect(g->be->ctx, link)
					      : -ENOTSUP;
}

static void op_start(void *ctx, uint8_t link, uint32_t tag_id, uint32_t suspend_ms)
{
	struct gw_core *g = ctx;
	struct gw_rtunnel *t = waiting_for(g, tag_id);

	(void)suspend_ms;
	if (t == NULL) {
		/* Closed (TUNNEL_CLOSE, RELEASE) while the attempt ran: nobody wants
		 * the connection. Not a failure of the tag: no back-off. */
		sched_session_done(&g->radio.sched, link, CTAG_STATUS_OK);
		return;
	}
	link_bind(g, link, t);
	t->state = GW_RT_SETUP;
	t->deadline = g->now + SETUP_MS;
	listen_update(g);
	if (g->be->link_setup == NULL || g->be->link_setup(g->be->ctx, link, t->mode) != 0) {
		rt_close(g, t, CTAG_STATUS_INVALID, CTAG_STATUS_INVALID);
	}
}

static void op_abort(void *ctx, uint8_t link, uint8_t status)
{
	struct gw_core *g = ctx;
	struct gw_rtunnel *t = bound(g, link);

	if (t != NULL) {
		rt_close(g, t, status, status); /* the link dropped: DISCONNECTED */
	} else {
		sched_session_done(&g->radio.sched, link, status);
	}
}

static void op_timer(void *ctx, uint32_t delay_ms)
{
	struct gw_core *g = ctx;

	g->radio.sched_armed = delay_ms != SCHED_TIMER_OFF;
	g->radio.sched_at = g->radio.sched_armed ? g->now + delay_ms : 0;
}

static uint32_t op_now(void *ctx)
{
	const struct gw_core *g = ctx;

	return (uint32_t)g->now;
}

static void op_reboot(void *ctx)
{
	struct gw_core *g = ctx;

	/* 5.2 step 7: the mesh would not resume; it reloads from settings. */
	if (g->be->reboot != NULL) {
		g->be->reboot(g->be->ctx);
	}
}

static const struct sched_ops radio_sched_ops = {
	.node_ready = op_ready,
	.has_work = op_work,
	.mesh_busy = op_busy,
	.link_idle = op_link_idle,
	.mesh_suspend = op_suspend,
	.mesh_resume = op_resume,
	.conn_create = op_create,
	.conn_cancel = op_disconnect,
	.disconnect = op_disconnect,
	.session_start = op_start,
	.session_abort = op_abort,
	.timer = op_timer,
	.now = op_now,
	.reboot = op_reboot,
};

/* ---- Timers, reset, counters ---- */

void gw_radio_init(struct gw_core *g)
{
	struct gw_radio *r = &g->radio;

	memset(r, 0, sizeof(*r));
	sched_init(&r->sched, &radio_sched_ops, g);
	for (int i = 0; i < SCHED_LINKS; i++) {
		r->links[i].tunnel = -1;
	}
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		r->tunnels[i].link = SCHED_NO_LINK;
	}
}

void gw_radio_timers(struct gw_core *g)
{
	struct gw_radio *r = &g->radio;

	if (r->sched_armed && g->now >= r->sched_at) {
		r->sched_armed = false;
		sched_timeout(&r->sched); /* re-arms through op_timer */
	}
	for (uint8_t i = 0; i < SCHED_LINKS; i++) {
		if (r->links[i].retry_at != 0 && g->now >= r->links[i].retry_at) {
			r->links[i].retry_at = 0;
			pump(g, i);
		}
	}
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		struct gw_rtunnel *t = &r->tunnels[i];

		if (t->state == GW_RT_FREE || g->now < t->deadline) {
			continue;
		}
		if (t->state == GW_RT_WAITING && attempting(g, t->tag_id)) {
			continue; /* the attempt in progress decides (at most ~3.5 s) */
		}
		/* WAITING: the tag not reached in time; SETUP: GATT took too long;
		 * OPEN: nothing either way for duration_s. */
		rt_close(g, t, CTAG_STATUS_TIMEOUT, CTAG_STATUS_TIMEOUT);
	}
	if (r->disc_until != 0 && g->now >= r->disc_until) {
		r->disc_until = 0;
		listen_update(g);
	}
}

int64_t gw_radio_deadline(const struct gw_core *g)
{
	const struct gw_radio *r = &g->radio;
	int64_t d = r->sched_armed ? r->sched_at : GW_NEVER;

	for (int i = 0; i < SCHED_LINKS; i++) {
		if (r->links[i].retry_at != 0) {
			d = gw_min_deadline(d, r->links[i].retry_at);
		}
	}
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		const struct gw_rtunnel *t = &r->tunnels[i];

		/* A waiting tunnel whose tag is being connected waits for the
		 * attempt, which the scheduler's timer bounds. */
		if (t->state != GW_RT_FREE &&
		    !(t->state == GW_RT_WAITING && attempting(g, t->tag_id))) {
			d = gw_min_deadline(d, t->deadline);
		}
	}
	if (r->disc_until != 0) {
		d = gw_min_deadline(d, r->disc_until);
	}
	return d;
}

void gw_radio_reset(struct gw_core *g)
{
	struct gw_radio *r = &g->radio;

	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		if (r->tunnels[i].state != GW_RT_FREE) {
			rt_close(g, &r->tunnels[i], -1, CTAG_STATUS_OK);
		}
	}
	r->disc_until = 0;
	listen_update(g);
}

size_t gw_radio_counters(const struct gw_core *g, struct ctag_cbor_counter *items, size_t max)
{
	const struct gw_radio_counters *c = &g->radio.c;
	const struct sched_counters *s = &g->radio.sched.c;
	const struct ctag_cbor_counter all[] = {
		CTAG_CBOR_COUNTER("radio_adverts", c->adverts),
		CTAG_CBOR_COUNTER("radio_tunnels", c->tunnels),
		CTAG_CBOR_COUNTER("radio_stray", c->stray),
		CTAG_CBOR_COUNTER("mesh_paused", c->mesh_paused),
		CTAG_CBOR_COUNTER("attempts", s->attempts),
		CTAG_CBOR_COUNTER("suspend_count", s->suspend_count),
		CTAG_CBOR_COUNTER("suspend_fail", s->suspend_fail),
		CTAG_CBOR_COUNTER("suspend_max_ms", s->suspend_max_ms),
		CTAG_CBOR_COUNTER("resume_fail", s->resume_fail),
		CTAG_CBOR_COUNTER("connect_failed", s->connect_failed),
		CTAG_CBOR_COUNTER("quick_retries", s->quick_retries),
		CTAG_CBOR_COUNTER("deferred", s->deferred),
		CTAG_CBOR_COUNTER("rate_limited", s->rate_limited),
		CTAG_CBOR_COUNTER("backoff_skips", s->backoff_skips),
		CTAG_CBOR_COUNTER("sessions_ok", s->sessions_ok),
		CTAG_CBOR_COUNTER("sessions_fail", s->sessions_fail),
	};
	size_t n = sizeof(all) / sizeof(all[0]) < max ? sizeof(all) / sizeof(all[0]) : max;

	memcpy(items, all, n * sizeof(all[0]));
	return n;
}
