/*
 * The gateway's own radio, Bluetooth side (docs/protocol.md 11,
 * docs/gateway-firmware.md 16; CONFIG_CTAG_GW_RADIO): tag advertisements
 * from the mesh's own scan (bt_le_scan_cb_register: the gateway never starts
 * or stops scanning), the connections of the 5.2 window, per link the GATT
 * setup of the tunnel's mode (discovery of the tag service, the CCC
 * subscriptions, the read of CAPS or IDENT), and the fragments the core
 * writes. The core (src/core/gw_radio.c, lib/sched) decides everything; this
 * file only runs the Bluetooth procedures it asks for (struct gw_backend).
 *
 * Callbacks run in Bluetooth contexts and only post events (gw_post); the
 * discovery and read callbacks also collect handles and value bytes into
 * their link's record, which the gateway thread reads only after that
 * procedure's completion event (the event queue orders them). Everything
 * else here is owned by the gateway thread.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/mesh.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/util.h>

#include "gw_app.h"
#include "gw_mesh.h"

LOG_MODULE_REGISTER(gw_central, LOG_LEVEL_INF);

#define LINKS        CONFIG_CTAG_GW_TAG_LINKS
#define ADV_MSD_LEN  10u /* company u16, ver u8, tag_id u32, flags u8, disp_rev u16 (5.1) */
#define CONN_INT_MIN 24  /* 30 ms (1.25 ms units) */
#define CONN_INT_MAX 40  /* 50 ms */
#define CONN_TIMEOUT 400 /* 4 s (10 ms units) */
#define READ_MAX     GW_EVT_DATA /* the value read goes up in one event */

/* One connection per link; the DATA fragments of every link in flight at once
 * stay below the ATT, L2CAP and ACL buffers, so a write from the gateway loop
 * never waits for one (the host allocates them with K_FOREVER there). */
BUILD_ASSERT(CONFIG_BT_MAX_CONN >= LINKS, "a Bluetooth connection per tag link");
BUILD_ASSERT(LINKS * CONFIG_CTAG_GW_LINK_INFLIGHT < CONFIG_BT_ATT_TX_COUNT,
	     "DATA fragments in flight must stay below the ATT buffers");
BUILD_ASSERT(LINKS * CONFIG_CTAG_GW_LINK_INFLIGHT < CONFIG_BT_L2CAP_TX_BUF_COUNT,
	     "DATA fragments in flight must stay below the L2CAP buffers");
BUILD_ASSERT(LINKS * CONFIG_CTAG_GW_LINK_INFLIGHT < CONFIG_BT_BUF_ACL_TX_COUNT,
	     "DATA fragments in flight must stay below the ACL buffers");
BUILD_ASSERT(CTAG_TAG_CAPS_LEN <= READ_MAX && CTAG_IDENT2_LEN <= READ_MAX,
	     "the CAPS or IDENT value fits one event");
BUILD_ASSERT(CTAG_ATT_VALUE_MAX <= GW_EVT_DATA, "a notified value fits one event");
BUILD_ASSERT(sizeof(struct bt_conn *) <= GW_EVT_DATA, "a connection pointer fits one event");

static const struct bt_uuid_128 svc_uuid = BT_UUID_INIT_128(CTAG_GATT_SERVICE_UUID_VAL);
static const struct bt_uuid_128 caps_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CAPS_UUID_VAL);
static const struct bt_uuid_128 ctrl_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CTRL_UUID_VAL);
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_DATA_UUID_VAL);
static const struct bt_uuid_128 status_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_STATUS_UUID_VAL);
static const struct bt_uuid_128 ident_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_IDENT_UUID_VAL);
static const struct bt_uuid_128 pair_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_PAIR_UUID_VAL);

enum phase {
	PH_IDLE = 0,
	PH_PRIMARY,   /* the tag service */
	PH_CHRC,      /* its characteristics */
	PH_CCC,       /* their CCC descriptors */
	PH_SUBSCRIBE, /* CTRL + STATUS, or PAIR */
	PH_READ,      /* CAPS or IDENT */
	PH_READY,
};

enum step { /* GW_EVT_GATT u16 */
	STEP_DISCOVERED = 1,
	STEP_SUBSCRIBED,
	STEP_READ,
};

struct handles {
	uint16_t start, end;
	uint16_t caps, ctrl, ctrl_ccc, data, status, status_ccc; /* SESSION */
	uint16_t ident, pair, pair_ccc;                          /* PAIR */
};

/* One tag connection. */
struct clink {
	struct bt_conn *conn;
	struct handles h; /* written by disc_cb during a discovery phase */
	uint8_t mode;     /* enum ctag_tunnel_mode */
	uint8_t phase;
	uint8_t subs;       /* subscriptions asked for */
	uint8_t subscribed; /* ... and confirmed */
	uint8_t sub_chr[2]; /* enum gw_chr of each subscription */
	uint8_t wr_chr;     /* the write with response in flight */
	uint16_t read_len;  /* written by read_cb during the read */
	struct bt_gatt_discover_params disc;
	struct bt_gatt_subscribe_params sub[2];
	struct bt_gatt_read_params rd;
	struct bt_gatt_write_params wr;
	uint8_t wr_buf[CTAG_ATT_VALUE_MAX];
	uint8_t read_buf[READ_MAX];
};

static struct clink links[LINKS];
static atomic_t listening;
static atomic_t adv_dropped;
static uint32_t gatt_failures;

static uint8_t index_of(const struct clink *cl)
{
	return (uint8_t)(cl - links);
}

/* The link whose record holds p (a callback's params). */
static struct clink *owner_of(const void *p)
{
	for (uint8_t i = 0; i < LINKS; i++) {
		const uint8_t *base = (const uint8_t *)&links[i];

		if ((const uint8_t *)p >= base && (const uint8_t *)p < base + sizeof(links[i])) {
			return &links[i];
		}
	}
	return NULL;
}

static uint8_t link_of_conn(const struct bt_conn *c)
{
	for (uint8_t i = 0; i < LINKS; i++) {
		if (c != NULL && links[i].conn == c) {
			return i;
		}
	}
	return SCHED_NO_LINK;
}

/* ---- Advertisements (5.1), only while the core listens ---- */

static void scan_recv(const struct bt_le_scan_recv_info *info, struct net_buf_simple *buf)
{
	const uint8_t *p = buf->data;
	size_t left = buf->len;

	if (!atomic_get(&listening) || info->adv_type != BT_GAP_ADV_TYPE_ADV_IND) {
		return; /* mesh traffic is non-connectable */
	}
	while (left >= 2u) {
		size_t len = p[0];

		if (len == 0u || len + 1u > left) {
			return;
		}
		/* [11][0xFF][company u16][ver][tag_id u32][flags][disp_rev u16] */
		if (p[1] == BT_DATA_MANUFACTURER_DATA && len == 1u + ADV_MSD_LEN &&
		    sys_get_le16(&p[2]) == CTAG_MESH_COMPANY_ID &&
		    (p[4] == 1u || p[4] == CTAG_SECURE_PROTO_VERSION)) {
			struct gw_evt e = {
				.type = GW_EVT_TAG_ADV,
				.op = p[4],
				.rssi = info->rssi,
				.u16 = p[9],
				.tag = sys_get_le32(&p[5]),
				.len = 7u,
			};

			e.data[0] = info->addr->type;
			memcpy(&e.data[1], info->addr->a.val, sizeof(info->addr->a.val));
			if (!gw_post_advert(&e)) {
				atomic_inc(&adv_dropped);
			}
			return;
		}
		p += len + 1u;
		left -= len + 1u;
	}
}

static struct bt_le_scan_cb scan_cb = {.recv = scan_recv};

/* ---- Connections (5.2) ---- */

static bool ours(struct bt_conn *c)
{
	struct bt_conn_info info;

	return bt_conn_get_info(c, &info) == 0 && info.role == BT_CONN_ROLE_CENTRAL;
}

static void post_conn(uint8_t type, struct bt_conn *c, uint8_t err)
{
	struct gw_evt e = {.type = type, .err = err};

	memcpy(e.data, &c, sizeof(c));
	gw_post(&e);
}

static void connected(struct bt_conn *c, uint8_t err)
{
	if (ours(c)) {
		post_conn(GW_EVT_CONN, c, err);
	}
}

static void disconnected(struct bt_conn *c, uint8_t reason)
{
	if (ours(c)) {
		post_conn(GW_EVT_DISCONN, c, reason);
	}
}

BT_CONN_CB_DEFINE(gw_conn_cbs) = {
	.connected = connected,
	.disconnected = disconnected,
};

/* ---- GATT setup ---- */

static void post_step(const struct clink *cl, uint16_t step, int err)
{
	struct gw_evt e = {.type = GW_EVT_GATT, .addr = index_of(cl), .u16 = step, .err = err};

	gw_post(&e);
}

static uint8_t disc_cb(struct bt_conn *c, const struct bt_gatt_attr *attr,
		       struct bt_gatt_discover_params *params)
{
	struct clink *cl = CONTAINER_OF(params, struct clink, disc);
	struct handles *h = &cl->h;

	ARG_UNUSED(c);
	if (attr == NULL) {
		post_step(cl, STEP_DISCOVERED, 0); /* this discovery phase is complete */
		return BT_GATT_ITER_STOP;
	}
	switch (params->type) {
	case BT_GATT_DISCOVER_PRIMARY: {
		const struct bt_gatt_service_val *svc = attr->user_data;

		h->start = attr->handle;
		h->end = svc->end_handle;
		post_step(cl, STEP_DISCOVERED, 0);
		return BT_GATT_ITER_STOP;
	}
	case BT_GATT_DISCOVER_CHARACTERISTIC: {
		const struct bt_gatt_chrc *chrc = attr->user_data;

		if (bt_uuid_cmp(chrc->uuid, &caps_uuid.uuid) == 0) {
			h->caps = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &ctrl_uuid.uuid) == 0) {
			h->ctrl = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &data_uuid.uuid) == 0) {
			h->data = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &status_uuid.uuid) == 0) {
			h->status = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &ident_uuid.uuid) == 0) {
			h->ident = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &pair_uuid.uuid) == 0) {
			h->pair = chrc->value_handle;
		}
		return BT_GATT_ITER_CONTINUE;
	}
	default: {
		/* A CCC belongs to the closest characteristic value before it. */
		const uint16_t values[] = {h->caps, h->ctrl, h->data, h->status, h->ident, h->pair};
		uint16_t owner = 0u;

		for (size_t i = 0; i < ARRAY_SIZE(values); i++) {
			if (values[i] < attr->handle && values[i] > owner) {
				owner = values[i];
			}
		}
		if (owner != 0u && owner == h->ctrl) {
			h->ctrl_ccc = attr->handle;
		} else if (owner != 0u && owner == h->status) {
			h->status_ccc = attr->handle;
		} else if (owner != 0u && owner == h->pair) {
			h->pair_ccc = attr->handle;
		}
		return BT_GATT_ITER_CONTINUE;
	}
	}
}

static int discover(struct clink *cl, uint8_t type, const struct bt_uuid *uuid, uint16_t start,
		    uint16_t end)
{
	memset(&cl->disc, 0, sizeof(cl->disc));
	cl->disc.uuid = uuid;
	cl->disc.func = disc_cb;
	cl->disc.start_handle = start;
	cl->disc.end_handle = end;
	cl->disc.type = type;
	return bt_gatt_discover(cl->conn, &cl->disc);
}

static uint8_t value_cb(struct bt_conn *c, struct bt_gatt_subscribe_params *params,
			const void *data, uint16_t len)
{
	struct clink *cl = owner_of(params);
	struct gw_evt e = {.type = GW_EVT_LINK_VALUE};

	ARG_UNUSED(c);
	if (data == NULL || cl == NULL) {
		return BT_GATT_ITER_STOP; /* unsubscribed (the link went down) */
	}
	e.addr = index_of(cl);
	e.op = cl->sub_chr[params == &cl->sub[0] ? 0 : 1];
	if (len > sizeof(e.data)) {
		e.err = -EMSGSIZE; /* the core ends the tunnel (5.3: an overlong fragment) */
	} else {
		e.len = (uint8_t)len;
		memcpy(e.data, data, len);
	}
	gw_post(&e);
	return BT_GATT_ITER_CONTINUE;
}

static void subscribe_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_subscribe_params *params)
{
	struct clink *cl = owner_of(params);

	ARG_UNUSED(c);
	if (cl != NULL) {
		post_step(cl, STEP_SUBSCRIBED, err);
	}
}

static int subscribe(struct clink *cl, uint8_t k, uint8_t chr, uint16_t value, uint16_t ccc,
		     uint16_t v)
{
	struct bt_gatt_subscribe_params *p = &cl->sub[k];

	memset(p, 0, sizeof(*p));
	cl->sub_chr[k] = chr;
	p->notify = value_cb;
	p->subscribe = subscribe_cb;
	p->value_handle = value;
	p->ccc_handle = ccc;
	p->value = v;
	return bt_gatt_subscribe(cl->conn, p);
}

static uint8_t read_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_read_params *params,
		       const void *data, uint16_t len)
{
	struct clink *cl = CONTAINER_OF(params, struct clink, rd);
	struct gw_evt e = {.type = GW_EVT_GATT, .addr = index_of(cl), .u16 = STEP_READ, .err = err};

	ARG_UNUSED(c);
	if (err == 0u && data != NULL) {
		if (len <= sizeof(cl->read_buf) - cl->read_len) {
			memcpy(&cl->read_buf[cl->read_len], data, len);
			cl->read_len += len;
			return BT_GATT_ITER_CONTINUE; /* a long read goes on with Read Blob */
		}
		e.err = -EMSGSIZE; /* never relayed cut: the companion binds the exact value */
	} else if (err == 0u) {
		e.len = (uint8_t)cl->read_len; /* complete */
		memcpy(e.data, cl->read_buf, cl->read_len);
	}
	gw_post(&e);
	return BT_GATT_ITER_STOP;
}

static int read_value(struct clink *cl, uint16_t handle)
{
	cl->read_len = 0u;
	memset(&cl->rd, 0, sizeof(cl->rd));
	cl->rd.func = read_cb;
	cl->rd.handle_count = 1;
	cl->rd.single.handle = handle;
	cl->rd.single.offset = 0;
	return bt_gatt_read(cl->conn, &cl->rd);
}

/* The setup ended: the core opens the tunnel or closes it with status. */
static void setup_done(struct gw_core *g, uint8_t i, uint8_t status, const uint8_t *value,
		       size_t len, int64_t now)
{
	links[i].phase = status == CTAG_STATUS_OK ? PH_READY : PH_IDLE;
	if (status != CTAG_STATUS_OK) {
		gatt_failures++;
		LOG_INF("link %u: GATT setup failed (%u)", i, status);
	}
	gw_core_link_ready(g, i, status, value, len, now);
}

static void gatt_step(struct gw_core *g, uint8_t i, const struct gw_evt *e, int64_t now)
{
	struct clink *cl = &links[i];
	const struct handles *h = &cl->h;
	bool session = cl->mode == CTAG_TUNNEL_MODE_SESSION;
	int err;

	if (cl->conn == NULL) {
		return; /* the link went down meanwhile */
	}
	switch (cl->phase) {
	case PH_PRIMARY:
		if (e->u16 != STEP_DISCOVERED) {
			break;
		}
		if (h->start == 0u) {
			setup_done(g, i, CTAG_STATUS_UNSUPPORTED, NULL, 0u, now); /* no tag service */
		} else if (discover(cl, BT_GATT_DISCOVER_CHARACTERISTIC, NULL, h->start + 1u,
				    h->end) != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
		} else {
			cl->phase = PH_CHRC;
		}
		break;
	case PH_CHRC:
		if (e->u16 != STEP_DISCOVERED) {
			break;
		}
		if (session ? (h->caps == 0u || h->ctrl == 0u || h->data == 0u || h->status == 0u)
			    : (h->ident == 0u || h->pair == 0u)) {
			setup_done(g, i, CTAG_STATUS_UNSUPPORTED, NULL, 0u, now); /* 11.3 */
		} else if (discover(cl, BT_GATT_DISCOVER_DESCRIPTOR, BT_UUID_GATT_CCC, h->start + 1u,
				    h->end) != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
		} else {
			cl->phase = PH_CCC;
		}
		break;
	case PH_CCC:
		if (e->u16 != STEP_DISCOVERED) {
			break;
		}
		if (session ? (h->ctrl_ccc == 0u || h->status_ccc == 0u) : h->pair_ccc == 0u) {
			setup_done(g, i, CTAG_STATUS_UNSUPPORTED, NULL, 0u, now);
			break;
		}
		cl->phase = PH_SUBSCRIBE;
		cl->subscribed = 0u;
		if (session) {
			cl->subs = 2u;
			err = subscribe(cl, 0u, GW_CHR_CTRL, h->ctrl, h->ctrl_ccc, BT_GATT_CCC_INDICATE);
			if (err == 0) {
				err = subscribe(cl, 1u, GW_CHR_STATUS, h->status, h->status_ccc,
						BT_GATT_CCC_NOTIFY);
			}
		} else {
			cl->subs = 1u;
			err = subscribe(cl, 0u, GW_CHR_PAIR, h->pair, h->pair_ccc, BT_GATT_CCC_INDICATE);
		}
		if (err != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
		}
		break;
	case PH_SUBSCRIBE:
		if (e->u16 != STEP_SUBSCRIBED) {
			break;
		}
		if (e->err != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
			break;
		}
		if (++cl->subscribed < cl->subs) {
			break;
		}
		cl->phase = PH_READ;
		if (read_value(cl, session ? h->caps : h->ident) != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
		}
		break;
	case PH_READ:
		if (e->u16 != STEP_READ) {
			break;
		}
		if (e->err != 0) {
			setup_done(g, i, CTAG_STATUS_INVALID, NULL, 0u, now);
		} else {
			setup_done(g, i, CTAG_STATUS_OK, e->data, e->len, now);
		}
		break;
	default:
		break; /* a late step of a setup that already ended */
	}
}

void gw_central_event(struct gw_core *g, const struct gw_evt *e, int64_t now)
{
	struct bt_conn *c;
	uint8_t i;

	if (e->type == GW_EVT_GATT) {
		if (e->addr < LINKS) {
			gatt_step(g, (uint8_t)e->addr, e, now);
		}
		return;
	}
	memcpy(&c, e->data, sizeof(c));
	i = link_of_conn(c);
	if (i == SCHED_NO_LINK) {
		return; /* not a connection of ours (any more) */
	}
	if (e->type == GW_EVT_CONN) {
		if (e->err != 0) {
			bt_conn_unref(links[i].conn); /* the attempt failed or was cancelled */
			links[i].conn = NULL;
		}
		gw_core_link_connected(g, i, (uint8_t)e->err, now);
		return;
	}
	bt_conn_unref(links[i].conn);
	links[i].conn = NULL;
	links[i].phase = PH_IDLE;
	gw_core_link_disconnected(g, i, (uint8_t)e->err, now);
}

/* ---- Backend (struct gw_backend) ---- */

void gw_central_listen(void *ctx, bool on)
{
	ARG_UNUSED(ctx);
	(void)atomic_set(&listening, on ? 1 : 0);
}

int gw_central_suspend(void *ctx)
{
	ARG_UNUSED(ctx);
	return bt_mesh_suspend();
}

int gw_central_resume(void *ctx)
{
	ARG_UNUSED(ctx);
	return bt_mesh_resume();
}

int gw_central_connect(void *ctx, uint8_t link, const struct sched_peer *peer, uint32_t timeout_ms)
{
	struct bt_conn_le_create_param cp = BT_CONN_LE_CREATE_PARAM_INIT(
		BT_CONN_LE_OPT_NONE, BT_GAP_SCAN_FAST_WINDOW, BT_GAP_SCAN_FAST_WINDOW);
	struct bt_le_conn_param lp = BT_LE_CONN_PARAM_INIT(CONN_INT_MIN, CONN_INT_MAX, 0,
							   CONN_TIMEOUT);
	bt_addr_le_t addr = {.type = peer->type};
	struct clink *cl;

	ARG_UNUSED(ctx);
	if (link >= LINKS) {
		return -EINVAL;
	}
	cl = &links[link];
	memcpy(addr.a.val, peer->a, sizeof(addr.a.val));
	cp.timeout = (uint16_t)(timeout_ms / 10u); /* 10 ms units */
	if (cl->conn != NULL) {
		bt_conn_unref(cl->conn);
		cl->conn = NULL;
	}
	cl->phase = PH_IDLE;
	return bt_conn_le_create(&addr, &cp, &lp, &cl->conn);
}

int gw_central_disconnect(void *ctx, uint8_t link)
{
	ARG_UNUSED(ctx);
	if (link >= LINKS || links[link].conn == NULL) {
		return -ENOTCONN;
	}
	/* On an attempt still initiating this is the cancellation (connected()
	 * then reports it with an error). */
	return bt_conn_disconnect(links[link].conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

int gw_central_setup(void *ctx, uint8_t link, uint8_t mode)
{
	struct clink *cl;

	ARG_UNUSED(ctx);
	if (link >= LINKS || links[link].conn == NULL) {
		return -ENOTCONN;
	}
	cl = &links[link];
	memset(&cl->h, 0, sizeof(cl->h));
	cl->mode = mode;
	cl->phase = PH_PRIMARY;
	return discover(cl, BT_GATT_DISCOVER_PRIMARY, &svc_uuid.uuid, BT_ATT_FIRST_ATTRIBUTE_HANDLE,
			BT_ATT_LAST_ATTRIBUTE_HANDLE);
}

static void write_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_write_params *params)
{
	struct clink *cl = CONTAINER_OF(params, struct clink, wr);
	struct gw_evt e = {
		.type = GW_EVT_LINK_WRITTEN, .addr = index_of(cl), .op = cl->wr_chr, .err = err};

	ARG_UNUSED(c);
	gw_post(&e);
}

static void data_sent_cb(struct bt_conn *c, void *user_data)
{
	struct gw_evt e = {
		.type = GW_EVT_LINK_WRITTEN, .addr = (uint16_t)(uintptr_t)user_data, .op = GW_CHR_DATA};

	ARG_UNUSED(c);
	gw_post(&e);
}

int gw_central_write(void *ctx, uint8_t link, uint8_t chr, const uint8_t *value, size_t len)
{
	struct clink *cl;

	ARG_UNUSED(ctx);
	if (link >= LINKS || links[link].conn == NULL || links[link].phase != PH_READY) {
		return -ENOTCONN;
	}
	cl = &links[link];
	if (len > sizeof(cl->wr_buf)) {
		return -EMSGSIZE;
	}
	if (chr == GW_CHR_DATA) {
		/* Copied into an ATT buffer at once; its sent callback completes it. */
		return bt_gatt_write_without_response_cb(cl->conn, cl->h.data, value, (uint16_t)len,
							 false, data_sent_cb,
							 (void *)(uintptr_t)link);
	}
	if (chr != GW_CHR_CTRL && chr != GW_CHR_PAIR) {
		return -EINVAL;
	}
	/* With response, one at a time per link (the core's rule): the buffer
	 * stays valid until write_cb. */
	memcpy(cl->wr_buf, value, len);
	memset(&cl->wr, 0, sizeof(cl->wr));
	cl->wr_chr = chr;
	cl->wr.func = write_cb;
	cl->wr.handle = chr == GW_CHR_CTRL ? cl->h.ctrl : cl->h.pair;
	cl->wr.offset = 0;
	cl->wr.data = cl->wr_buf;
	cl->wr.length = (uint16_t)len;
	return bt_gatt_write(cl->conn, &cl->wr);
}

void gw_central_init(void)
{
	bt_le_scan_cb_register(&scan_cb);
}

size_t gw_central_counters(struct ctag_cbor_counter *items, size_t max)
{
	const struct ctag_cbor_counter all[] = {
		CTAG_CBOR_COUNTER("radio_adv_dropped", (uint32_t)atomic_get(&adv_dropped)),
		CTAG_CBOR_COUNTER("gatt_failures", gatt_failures),
	};
	size_t n = ARRAY_SIZE(all) < max ? ARRAY_SIZE(all) : max;

	memcpy(items, all, n * sizeof(all[0]));
	return n;
}
