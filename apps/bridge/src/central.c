/*
 * BLE central (docs/protocol.md 5): tag advertisements from the mesh's own
 * passive scan (bt_le_scan_cb_register; the bridge never starts or stops
 * scanning), the connection attempt of the 5.2 window, GATT discovery with a
 * per-tag handle cache, CCC subscriptions, and the Bluetooth side of the
 * scheduler (sched_ops) and of the session (tsess_io).
 *
 * Callbacks run in Bluetooth contexts and only post events (bev_post); all
 * state below is owned by the bridge work queue.
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
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/util.h>

#include "bridge.h"

LOG_MODULE_REGISTER(bridge_central, LOG_LEVEL_INF);

#define GATEWAY_ADDR    0x0001
/* 10: CAPS to AUTH_OK within 5 s of the connection, GATT setup included. */
#define GATT_SETUP_MS   TSESS_HANDSHAKE_MS
#define LIVENESS_MS     ((uint32_t)CONFIG_CTAG_BRIDGE_LIVENESS_S * 1000u)
#define LIVENESS_CHECK_MS 60000u
#define ADV_VERSION     1u
#define MSD_LEN         10u /* company u16, ver u8, tag_id u32, flags u8, disp_rev u16 */
#define CONN_INT_MIN    24  /* 30 ms (1.25 ms units) */
#define CONN_INT_MAX    40  /* 50 ms */
#define CONN_TIMEOUT    400 /* 4 s (10 ms units) */

static const struct bt_uuid_128 svc_uuid = BT_UUID_INIT_128(CTAG_GATT_SERVICE_UUID_VAL);
static const struct bt_uuid_128 caps_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CAPS_UUID_VAL);
static const struct bt_uuid_128 ctrl_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CTRL_UUID_VAL);
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_DATA_UUID_VAL);
static const struct bt_uuid_128 status_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_STATUS_UUID_VAL);

struct handles {
	uint32_t tag_id;
	uint16_t start;
	uint16_t end;
	uint16_t caps;
	uint16_t ctrl;
	uint16_t ctrl_ccc;
	uint16_t data;
	uint16_t status;
	uint16_t status_ccc;
	bool valid;
};

enum gatt_phase {
	GATT_IDLE,
	GATT_PRIMARY,
	GATT_CHRC,
	GATT_CCC,
	GATT_SUBSCRIBE,
	GATT_READY,
};

static struct bt_conn *conn;
static struct handles cache[CONFIG_CTAG_BRIDGE_MAX_TAGS];
static uint8_t cache_next;
static struct handles cur;
static uint8_t phase;
static uint8_t subscribed;
static uint32_t cur_tag;
static uint32_t cur_suspend_ms;
static uint32_t cur_conn_ms;
/* Uptime of the last advertising report of any kind (liveness). */
static atomic_t heard_ms;
static struct bt_gatt_discover_params disc;
static struct bt_gatt_subscribe_params sub_ctrl;
static struct bt_gatt_subscribe_params sub_status;
static struct bt_gatt_read_params rd;
static uint8_t caps_buf[24];
static uint8_t caps_len;
static struct bt_gatt_write_params wr;
static uint8_t wr_buf[CTAG_ATT_VALUE_MAX];

static struct {
	uint32_t tag_id;
	uint32_t at;
	bool used;
} seen[CONFIG_CTAG_BRIDGE_MAX_TAGS];

static struct {
	uint32_t adverts;
	uint32_t tag_seen;
	uint32_t discoveries;
	uint32_t cache_hits;
	uint32_t gatt_failures;
	uint32_t stray_events;
} cnt;

static void gatt_timeout_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(gatt_timeout, gatt_timeout_fn);

/* ---- Advertisements (5.1) ---- */

static void scan_recv(const struct bt_le_scan_recv_info *info, struct net_buf_simple *buf)
{
	struct net_buf_simple_state state;
	struct bev e = {.type = BEV_ADVERT};
	bool found = false;

	(void)atomic_set(&heard_ms, (atomic_val_t)k_uptime_get_32()); /* the scanner runs */
	if (info->adv_type != BT_GAP_ADV_TYPE_ADV_IND) {
		return; /* mesh traffic is non-connectable */
	}
	net_buf_simple_save(buf, &state);
	while (buf->len > 1u) {
		uint8_t len = net_buf_simple_pull_u8(buf);

		if (len == 0u || len > buf->len) {
			break;
		}
		if (buf->data[0] == BT_DATA_MANUFACTURER_DATA && len == 1u + MSD_LEN &&
		    ctag_get_le16(&buf->data[1]) == CTAG_MESH_COMPANY_ID &&
		    buf->data[3] == ADV_VERSION) {
			e.tag_id = ctag_get_le32(&buf->data[4]);
			e.flags = buf->data[8];
			found = true;
		}
		(void)net_buf_simple_pull(buf, len);
	}
	net_buf_simple_restore(buf, &state);
	if (found) {
		e.rssi = info->rssi;
		bt_addr_le_copy(&e.addr, info->addr);
		bev_post(&e);
	}
}

static struct bt_le_scan_cb scan_cb = {.recv = scan_recv};

static void tag_seen(const struct bev *e, uint32_t now)
{
	struct ctag_mesh_tag_seen m = {
		.tag_id = e->tag_id,
		.rssi = e->rssi,
		.battery_mv = dlv_battery(&br.dlv, e->tag_id),
		.flags = e->flags,
	};
	uint8_t buf[CTAG_MESH_TAG_SEEN_LEN];
	size_t i, slot = 0;

	for (i = 0; i < ARRAY_SIZE(seen); i++) {
		if (seen[i].used && seen[i].tag_id == e->tag_id) {
			if (now - seen[i].at < CONFIG_CTAG_BRIDGE_TAG_SEEN_INTERVAL_MS) {
				return;
			}
			slot = i;
			break;
		}
		if (!seen[i].used || (seen[slot].used && seen[i].at < seen[slot].at)) {
			slot = i;
		}
	}
	seen[slot].used = true;
	seen[slot].tag_id = e->tag_id;
	seen[slot].at = now;
	cnt.tag_seen++;
	if (ctag_mesh_tag_seen_pack(&m, buf, sizeof(buf)) > 0) {
		mesh_send(CTAG_MESH_OP_TAG_SEEN, buf, sizeof(buf), GATEWAY_ADDR);
	}
}

static void on_advert(const struct bev *e)
{
	struct sched_peer p = {.type = e->addr.type};

	if (dlv_assignment(&br.dlv, e->tag_id) == NULL) {
		return; /* not ours */
	}
	cnt.adverts++;
	tag_seen(e, k_uptime_get_32());
	memcpy(p.a, e->addr.a.val, sizeof(p.a));
	sched_advert(&br.sched, e->tag_id, &p);
}

/* ---- Connection (5.2) ---- */

static bool ours(struct bt_conn *c)
{
	struct bt_conn_info info;

	return bt_conn_get_info(c, &info) == 0 && info.role == BT_CONN_ROLE_CENTRAL;
}

static void connected(struct bt_conn *c, uint8_t err)
{
	struct bev e = {.type = BEV_CONNECTED, .err = err};

	if (ours(c)) {
		e.ptr = c;
		bev_post(&e);
	}
}

static void disconnected(struct bt_conn *c, uint8_t reason)
{
	struct bev e = {.type = BEV_DISCONNECTED, .err = reason};

	if (ours(c)) {
		e.ptr = c;
		bev_post(&e);
	}
}

BT_CONN_CB_DEFINE(conn_cbs) = {
	.connected = connected,
	.disconnected = disconnected,
};

/* ---- GATT setup: discovery (cached per tag) and subscriptions ---- */

static void gatt_fail(uint8_t status)
{
	size_t i;

	cnt.gatt_failures++;
	phase = GATT_IDLE;
	(void)k_work_cancel_delayable(&gatt_timeout);
	for (i = 0; i < ARRAY_SIZE(cache); i++) {
		if (cache[i].tag_id == cur_tag) {
			cache[i].valid = false;
		}
	}
	sched_session_done(&br.sched, status);
}

static void gatt_timeout_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (phase != GATT_IDLE && phase != GATT_READY) {
		gatt_fail(CTAG_STATUS_TIMEOUT);
	}
}

static uint8_t disc_cb(struct bt_conn *c, const struct bt_gatt_attr *attr,
		       struct bt_gatt_discover_params *params)
{
	struct bev e = {.type = BEV_GATT_STEP};

	ARG_UNUSED(c);
	if (attr == NULL) {
		bev_post(&e); /* this discovery phase is complete */
		return BT_GATT_ITER_STOP;
	}
	switch (params->type) {
	case BT_GATT_DISCOVER_PRIMARY: {
		const struct bt_gatt_service_val *svc = attr->user_data;

		cur.start = attr->handle;
		cur.end = svc->end_handle;
		bev_post(&e);
		return BT_GATT_ITER_STOP;
	}
	case BT_GATT_DISCOVER_CHARACTERISTIC: {
		const struct bt_gatt_chrc *chrc = attr->user_data;

		if (bt_uuid_cmp(chrc->uuid, &caps_uuid.uuid) == 0) {
			cur.caps = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &ctrl_uuid.uuid) == 0) {
			cur.ctrl = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &data_uuid.uuid) == 0) {
			cur.data = chrc->value_handle;
		} else if (bt_uuid_cmp(chrc->uuid, &status_uuid.uuid) == 0) {
			cur.status = chrc->value_handle;
		}
		return BT_GATT_ITER_CONTINUE;
	}
	default: {
		/* A CCC belongs to the closest characteristic value before it. */
		uint16_t h = attr->handle;
		uint16_t owner = 0;
		const uint16_t values[] = {cur.caps, cur.ctrl, cur.data, cur.status};

		for (size_t i = 0; i < ARRAY_SIZE(values); i++) {
			if (values[i] < h && values[i] > owner) {
				owner = values[i];
			}
		}
		if (owner != 0 && owner == cur.ctrl) {
			cur.ctrl_ccc = h;
		} else if (owner != 0 && owner == cur.status) {
			cur.status_ccc = h;
		}
		return BT_GATT_ITER_CONTINUE;
	}
	}
}

static int discover(uint8_t type, const struct bt_uuid *uuid, uint16_t start, uint16_t end)
{
	memset(&disc, 0, sizeof(disc));
	disc.uuid = uuid;
	disc.func = disc_cb;
	disc.start_handle = start;
	disc.end_handle = end;
	disc.type = type;
	return bt_gatt_discover(conn, &disc);
}

static uint8_t value_cb(struct bt_conn *c, struct bt_gatt_subscribe_params *params,
			const void *data, uint16_t len)
{
	struct bev e = {.type = params == &sub_ctrl ? BEV_CTRL_VALUE : BEV_STATUS_VALUE};

	ARG_UNUSED(c);
	if (data == NULL) {
		return BT_GATT_ITER_STOP; /* unsubscribed (disconnect) */
	}
	if (len > sizeof(e.data)) {
		e.err = -EMSGSIZE;
	} else {
		e.len = (uint8_t)len;
		memcpy(e.data, data, len);
	}
	bev_post(&e);
	return BT_GATT_ITER_CONTINUE;
}

static void subscribe_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_subscribe_params *params)
{
	struct bev e = {.type = BEV_GATT_STEP, .err = err, .flags = 1};

	ARG_UNUSED(c);
	ARG_UNUSED(params);
	bev_post(&e);
}

static int subscribe(struct bt_gatt_subscribe_params *p, uint16_t value, uint16_t ccc, uint16_t v)
{
	memset(p, 0, sizeof(*p));
	p->notify = value_cb;
	p->subscribe = subscribe_cb;
	p->value_handle = value;
	p->ccc_handle = ccc;
	p->value = v;
	return bt_gatt_subscribe(conn, p);
}

static void start_subscriptions(void)
{
	phase = GATT_SUBSCRIBE;
	subscribed = 0;
	if (subscribe(&sub_ctrl, cur.ctrl, cur.ctrl_ccc, BT_GATT_CCC_INDICATE) != 0 ||
	    subscribe(&sub_status, cur.status, cur.status_ccc, BT_GATT_CCC_NOTIFY) != 0) {
		gatt_fail(CTAG_STATUS_INVALID);
	}
}

static void session_ready(void)
{
	struct bt_conn_info info;
	uint16_t pace = 50;

	phase = GATT_READY;
	(void)k_work_cancel_delayable(&gatt_timeout);
	if (bt_conn_get_info(conn, &info) == 0 && info.le.interval_us >= 1000u) {
		pace = (uint16_t)(info.le.interval_us / 1000u);
	}
	tsess_start(&br.sess, cur_tag, cur_suspend_ms, pace, cur_conn_ms);
}

static void gatt_step(const struct bev *e)
{
	size_t i;

	if (conn == NULL || phase == GATT_IDLE || phase == GATT_READY) {
		cnt.stray_events++;
		return;
	}
	switch (phase) {
	case GATT_PRIMARY:
		if (cur.start == 0u) {
			gatt_fail(CTAG_STATUS_NOT_FOUND); /* no tag service */
		} else if (discover(BT_GATT_DISCOVER_CHARACTERISTIC, NULL, cur.start + 1u,
				    cur.end) != 0) {
			gatt_fail(CTAG_STATUS_INVALID);
		} else {
			phase = GATT_CHRC;
		}
		break;
	case GATT_CHRC:
		if (cur.caps == 0u || cur.ctrl == 0u || cur.data == 0u || cur.status == 0u ||
		    discover(BT_GATT_DISCOVER_DESCRIPTOR, BT_UUID_GATT_CCC, cur.start + 1u,
			     cur.end) != 0) {
			gatt_fail(CTAG_STATUS_INVALID);
		} else {
			phase = GATT_CCC;
		}
		break;
	case GATT_CCC:
		if (cur.ctrl_ccc == 0u || cur.status_ccc == 0u) {
			gatt_fail(CTAG_STATUS_INVALID);
			break;
		}
		cur.valid = true;
		for (i = 0; i < ARRAY_SIZE(cache) && cache[i].tag_id != cur_tag; i++) {
		}
		if (i == ARRAY_SIZE(cache)) {
			i = cache_next;
			cache_next = (uint8_t)((cache_next + 1u) % ARRAY_SIZE(cache));
		}
		cache[i] = cur;
		start_subscriptions();
		break;
	case GATT_SUBSCRIBE:
		if (e->flags != 1u) {
			break;
		}
		if (e->err != 0) {
			gatt_fail(CTAG_STATUS_INVALID);
			break;
		}
		if (++subscribed == 2u) {
			session_ready();
		}
		break;
	default:
		break;
	}
}

static void gatt_setup(void)
{
	size_t i;

	memset(&cur, 0, sizeof(cur));
	cur.tag_id = cur_tag;
	(void)k_work_reschedule_for_queue(&bwq, &gatt_timeout, K_MSEC(GATT_SETUP_MS));
	for (i = 0; i < ARRAY_SIZE(cache); i++) {
		if (cache[i].valid && cache[i].tag_id == cur_tag) {
			cnt.cache_hits++;
			cur = cache[i];
			start_subscriptions();
			return;
		}
	}
	cnt.discoveries++;
	phase = GATT_PRIMARY;
	if (discover(BT_GATT_DISCOVER_PRIMARY, &svc_uuid.uuid, BT_ATT_FIRST_ATTRIBUTE_HANDLE,
		     BT_ATT_LAST_ATTRIBUTE_HANDLE) != 0) {
		gatt_fail(CTAG_STATUS_INVALID);
	}
}

/* ---- Events on the work queue ---- */

void central_event(const struct bev *e)
{
	switch (e->type) {
	case BEV_ADVERT:
		on_advert(e);
		break;
	case BEV_CONNECTED:
		if (e->ptr != conn || conn == NULL) {
			cnt.stray_events++;
			break;
		}
		if (e->err != 0) {
			bt_conn_unref(conn); /* the attempt failed or was cancelled */
			conn = NULL;
		}
		sched_connected(&br.sched, (uint8_t)e->err);
		break;
	case BEV_DISCONNECTED:
		if (e->ptr != conn || conn == NULL) {
			cnt.stray_events++;
			break;
		}
		bt_conn_unref(conn);
		conn = NULL;
		sched_disconnected(&br.sched); /* ends a session or GATT setup in progress */
		phase = GATT_IDLE;
		(void)k_work_cancel_delayable(&gatt_timeout);
		break;
	case BEV_GATT_STEP:
		gatt_step(e);
		break;
	case BEV_CAPS:
		tsess_caps(&br.sess, e->err, e->data, e->len);
		break;
	case BEV_CTRL_WRITTEN:
		tsess_ctrl_written(&br.sess, e->err);
		break;
	case BEV_DATA_SENT:
		tsess_data_sent(&br.sess);
		break;
	case BEV_CTRL_VALUE:
	case BEV_STATUS_VALUE:
		if (e->err != 0) {
			tsess_abort(&br.sess, CTAG_STATUS_INVALID);
		} else if (e->type == BEV_CTRL_VALUE) {
			tsess_ctrl_value(&br.sess, e->data, e->len);
		} else {
			tsess_status_value(&br.sess, e->data, e->len);
		}
		break;
	default:
		break;
	}
}

/* ---- sched_ops: the Bluetooth side of the 5.2 window ---- */

static void sched_timer_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	sched_timeout(&br.sched);
}

static K_WORK_DELAYABLE_DEFINE(sched_timer, sched_timer_fn);

static bool op_ready(void *ctx)
{
	ARG_UNUSED(ctx);
	return mesh_node_ready();
}

static bool op_work(void *ctx, uint32_t tag_id)
{
	ARG_UNUSED(ctx);
	return dlv_has_work(&br.dlv, tag_id);
}

static bool op_busy(void *ctx)
{
	ARG_UNUSED(ctx);
	return mesh_busy();
}

static int op_suspend(void *ctx)
{
	int err = bt_mesh_suspend();

	ARG_UNUSED(ctx);
	if (err == 0 || err == -EALREADY) {
		mesh_set_suspended(true);
	}
	return err;
}

static int op_resume(void *ctx)
{
	int err = bt_mesh_resume();

	ARG_UNUSED(ctx);
	if (err == 0 || err == -EALREADY) {
		mesh_set_suspended(false);
	}
	return err;
}

static int op_create(void *ctx, const struct sched_peer *peer, uint32_t timeout_ms)
{
	struct bt_conn_le_create_param cp = BT_CONN_LE_CREATE_PARAM_INIT(
		BT_CONN_LE_OPT_NONE, BT_GAP_SCAN_FAST_WINDOW, BT_GAP_SCAN_FAST_WINDOW);
	struct bt_le_conn_param lp = BT_LE_CONN_PARAM_INIT(CONN_INT_MIN, CONN_INT_MAX, 0,
							   CONN_TIMEOUT);
	bt_addr_le_t addr = {.type = peer->type};

	ARG_UNUSED(ctx);
	memcpy(addr.a.val, peer->a, sizeof(addr.a.val));
	cp.timeout = (uint16_t)(timeout_ms / 10u); /* 10 ms units */
	if (conn != NULL) {
		bt_conn_unref(conn);
		conn = NULL;
	}
	return bt_conn_le_create(&addr, &cp, &lp, &conn);
}

static int op_disconnect(void *ctx)
{
	ARG_UNUSED(ctx);
	/* On an attempt still initiating this is the cancellation (connected()
	 * then reports it with an error). */
	return conn != NULL ? bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN)
			    : -ENOTCONN;
}

static void op_start(void *ctx, uint32_t tag_id, uint32_t suspend_ms)
{
	ARG_UNUSED(ctx);
	cur_tag = tag_id;
	cur_suspend_ms = suspend_ms;
	cur_conn_ms = k_uptime_get_32(); /* connected; the mesh has just resumed */
	gatt_setup();
}

static void op_abort(void *ctx, uint8_t status)
{
	ARG_UNUSED(ctx);
	if (tsess_active(&br.sess)) {
		tsess_abort(&br.sess, status);
	} else if (phase != GATT_IDLE && phase != GATT_READY) {
		phase = GATT_IDLE;
		(void)k_work_cancel_delayable(&gatt_timeout);
		sched_session_done(&br.sched, status);
	}
}

static void op_timer(void *ctx, uint32_t ms)
{
	ARG_UNUSED(ctx);
	if (ms == SCHED_TIMER_OFF) {
		(void)k_work_cancel_delayable(&sched_timer);
	} else {
		(void)k_work_reschedule_for_queue(&bwq, &sched_timer, K_MSEC(ms));
	}
}

static uint32_t op_now(void *ctx)
{
	ARG_UNUSED(ctx);
	return k_uptime_get_32();
}

static void op_reboot(void *ctx)
{
	ARG_UNUSED(ctx);
	LOG_ERR("mesh resume keeps failing: rebooting");
	sys_reboot(SYS_REBOOT_COLD);
}

const struct sched_ops central_sched_ops = {
	.node_ready = op_ready,
	.has_work = op_work,
	.mesh_busy = op_busy,
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

/* ---- tsess_io: GATT operations of the session ---- */

static void sess_step_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	tsess_timeout(&br.sess, TSESS_T_STEP);
}

static void sess_pace_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	tsess_timeout(&br.sess, TSESS_T_PACE);
}

static K_WORK_DELAYABLE_DEFINE(sess_step, sess_step_fn);
static K_WORK_DELAYABLE_DEFINE(sess_pace, sess_pace_fn);

static uint8_t read_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_read_params *params,
		       const void *data, uint16_t len)
{
	struct bev e = {.type = BEV_CAPS, .err = err};

	ARG_UNUSED(c);
	ARG_UNUSED(params);
	if (err == 0 && data != NULL) {
		uint16_t n = MIN(len, (uint16_t)(sizeof(caps_buf) - caps_len));

		memcpy(&caps_buf[caps_len], data, n);
		caps_len = (uint8_t)(caps_len + n);
		return BT_GATT_ITER_CONTINUE;
	}
	e.len = caps_len;
	memcpy(e.data, caps_buf, caps_len);
	bev_post(&e); /* complete (data == NULL) or failed */
	return BT_GATT_ITER_STOP;
}

static int io_read_caps(void *ctx)
{
	ARG_UNUSED(ctx);
	caps_len = 0;
	memset(&rd, 0, sizeof(rd));
	rd.func = read_cb;
	rd.handle_count = 1;
	rd.single.handle = cur.caps;
	rd.single.offset = 0;
	return conn != NULL ? bt_gatt_read(conn, &rd) : -ENOTCONN;
}

static void write_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_write_params *params)
{
	struct bev e = {.type = BEV_CTRL_WRITTEN, .err = err};

	ARG_UNUSED(c);
	ARG_UNUSED(params);
	bev_post(&e);
}

static int io_write_ctrl(void *ctx, const uint8_t *val, uint16_t len)
{
	ARG_UNUSED(ctx);
	if (conn == NULL || len > sizeof(wr_buf)) {
		return -ENOTCONN;
	}
	memcpy(wr_buf, val, len);
	memset(&wr, 0, sizeof(wr));
	wr.func = write_cb;
	wr.handle = cur.ctrl;
	wr.data = wr_buf;
	wr.length = len;
	return bt_gatt_write(conn, &wr);
}

static void data_sent_cb(struct bt_conn *c, void *user_data)
{
	struct bev e = {.type = BEV_DATA_SENT};

	ARG_UNUSED(c);
	ARG_UNUSED(user_data);
	bev_post(&e);
}

static int io_write_data(void *ctx, const uint8_t *val, uint16_t len)
{
	ARG_UNUSED(ctx);
	if (conn == NULL) {
		return -ENOTCONN;
	}
	return bt_gatt_write_without_response_cb(conn, cur.data, val, len, false, data_sent_cb,
						 NULL);
}

static void io_timer(void *ctx, uint8_t which, uint32_t ms)
{
	struct k_work_delayable *w = which == TSESS_T_STEP ? &sess_step : &sess_pace;

	ARG_UNUSED(ctx);
	if (ms == TSESS_TIMER_OFF) {
		(void)k_work_cancel_delayable(w);
	} else {
		(void)k_work_reschedule_for_queue(&bwq, w, K_MSEC(ms));
	}
}

static void io_done(void *ctx, uint8_t status)
{
	size_t i;

	ARG_UNUSED(ctx);
	phase = GATT_IDLE;
	if (status == CTAG_STATUS_INVALID || status == CTAG_STATUS_TIMEOUT) {
		for (i = 0; i < ARRAY_SIZE(cache); i++) {
			if (cache[i].tag_id == cur_tag) {
				cache[i].valid = false; /* rediscover next time */
			}
		}
	}
	LOG_INF("session with %08x ended: %u", cur_tag, status);
	sched_session_done(&br.sched, status);
}

static uint32_t io_now(void *ctx)
{
	ARG_UNUSED(ctx);
	return k_uptime_get_32();
}

const struct tsess_io central_tsess_io = {
	.read_caps = io_read_caps,
	.write_ctrl = io_write_ctrl,
	.write_data = io_write_data,
	.timer = io_timer,
	.done = io_done,
	.now = io_now,
};

/*
 * Liveness (CONFIG_CTAG_BRIDGE_LIVENESS_S): a provisioned bridge that is idle
 * (its mesh scanning) and hears no advertising report at all for that long
 * has a scanner that stopped without an error the scheduler could see; it
 * reboots and the mesh reloads from settings. Every mesh node in range sends
 * a secure network beacon at least every 600 s, so a working scanner always
 * hears something within the default 1800 s.
 */
static void liveness_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(liveness, liveness_fn);

static void liveness_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (bt_mesh_is_provisioned() &&
	    sched_deaf(&br.sched, k_uptime_get_32(), (uint32_t)atomic_get(&heard_ms), LIVENESS_MS)) {
		LOG_ERR("no advertising report for %u s while idle: rebooting",
			CONFIG_CTAG_BRIDGE_LIVENESS_S);
		sys_reboot(SYS_REBOOT_COLD);
	}
	(void)k_work_reschedule_for_queue(&bwq, &liveness, K_MSEC(LIVENESS_CHECK_MS));
}

int central_init(void)
{
	(void)atomic_set(&heard_ms, (atomic_val_t)k_uptime_get_32());
	bt_le_scan_cb_register(&scan_cb);
	if (LIVENESS_MS != 0u) {
		(void)k_work_reschedule_for_queue(&bwq, &liveness, K_MSEC(LIVENESS_CHECK_MS));
	}
	return 0;
}

size_t central_counters(struct ctag_cbor_counter *items, size_t max)
{
	const struct sched_counters *s = &br.sched.c;
	const struct tsess_counters *t = &br.sess.c;
	size_t n = 0u;

	BRIDGE_COUNTER("adverts", cnt.adverts);
	BRIDGE_COUNTER("tag_seen", cnt.tag_seen);
	BRIDGE_COUNTER("gatt_discoveries", cnt.discoveries);
	BRIDGE_COUNTER("gatt_cache_hits", cnt.cache_hits);
	BRIDGE_COUNTER("gatt_failures", cnt.gatt_failures);
	BRIDGE_COUNTER("attempts", s->attempts);
	BRIDGE_COUNTER("suspend_count", s->suspend_count);
	BRIDGE_COUNTER("suspend_fail", s->suspend_fail);
	BRIDGE_COUNTER("suspend_max_ms", s->suspend_max_ms);
	BRIDGE_COUNTER("resume_fail", s->resume_fail);
	BRIDGE_COUNTER("connect_failed", s->connect_failed);
	BRIDGE_COUNTER("cancels", s->cancels);
	BRIDGE_COUNTER("deferred", s->deferred);
	BRIDGE_COUNTER("rate_limited", s->rate_limited);
	BRIDGE_COUNTER("backoff_skips", s->backoff_skips);
	BRIDGE_COUNTER("sessions_ok", s->sessions_ok);
	BRIDGE_COUNTER("sessions_fail", s->sessions_fail);
	BRIDGE_COUNTER("records_tx", t->records_tx);
	BRIDGE_COUNTER("frames", t->frames);
	BRIDGE_COUNTER("render_ms_max", t->render_ms_max);
	BRIDGE_COUNTER("layout_reloads", t->layout_reloads);
	BRIDGE_COUNTER("unauth_statuses", t->unauth_statuses);
	return n;
}
