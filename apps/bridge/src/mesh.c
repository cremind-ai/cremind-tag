/*
 * Mesh node (docs/protocol.md 2-3): PB-ADV provisionee without OOB, Config and
 * Health servers, the vendor models LAYOUT_SRV and MGMT_SRV on the primary
 * element, and the outbound queue (one send at a time, held while the mesh
 * is suspended for a tag connection).
 */
#include <errno.h>
#include <string.h>

#include <zephyr/bluetooth/mesh.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include "bridge.h"

LOG_MODULE_REGISTER(bridge_mesh, LOG_LEVEL_INF);

#define TX_RETRY_MS    50
#define TX_WATCHDOG_MS 10000
#define NET_PRIMARY    0x000

/* ---- Inbound: model handlers only queue the message ---- */

struct mesh_rx {
	uint8_t op;
	uint8_t len;
	uint16_t src;
	uint8_t data[CTAG_MESH_MAX_VENDOR_PARAMS];
};

K_MSGQ_DEFINE(mesh_rx_q, sizeof(struct mesh_rx), CONFIG_CTAG_BRIDGE_MESH_RXQ, 4);

static struct {
	uint32_t rx;
	uint32_t rx_dropped;
	uint32_t tx;
	uint32_t tx_failed;
	uint32_t tx_dropped;
	uint32_t tx_busy;
} cnt;

static void rx_work_fn(struct k_work *work);
static K_WORK_DEFINE(rx_work, rx_work_fn);

static int queue_rx(uint8_t op, struct bt_mesh_msg_ctx *ctx, struct net_buf_simple *buf)
{
	struct mesh_rx m = {.op = op, .src = ctx->addr};

	if (buf->len > sizeof(m.data)) {
		return -EINVAL;
	}
	m.len = (uint8_t)buf->len;
	memcpy(m.data, buf->data, buf->len);
	if (k_msgq_put(&mesh_rx_q, &m, K_NO_WAIT) != 0) {
		cnt.rx_dropped++; /* chunks are re-sent after LAYOUT_STATUS INCOMPLETE */
	}
	(void)k_work_submit_to_queue(&bwq, &rx_work);
	return 0;
}

#define HANDLER(name)                                                                            \
	static int h_##name(const struct bt_mesh_model *model, struct bt_mesh_msg_ctx *ctx,     \
			    struct net_buf_simple *buf)                                          \
	{                                                                                        \
		ARG_UNUSED(model);                                                               \
		return queue_rx(CTAG_MESH_OP_##name, ctx, buf);                                  \
	}

HANDLER(LAYOUT_BEGIN)
HANDLER(LAYOUT_CHUNK)
HANDLER(LAYOUT_COMMIT)
HANDLER(LAYOUT_CANCEL)
HANDLER(RESULT_ACK)
HANDLER(CAPS_GET)
HANDLER(HEALTH_GET)
HANDLER(ASSIGN_SET)
HANDLER(ASSIGN_DEL)
HANDLER(TAG_CMD)
HANDLER(IDENTIFY)

#define OP(name, len) {CTAG_MESH_OPCODE_##name, (len), h_##name}

static const struct bt_mesh_model_op layout_ops[] = {
	OP(LAYOUT_BEGIN, BT_MESH_LEN_EXACT(CTAG_MESH_LAYOUT_BEGIN_LEN)),
	OP(LAYOUT_CHUNK, BT_MESH_LEN_MIN(CTAG_MESH_LAYOUT_CHUNK_LEN)),
	OP(LAYOUT_COMMIT, BT_MESH_LEN_EXACT(CTAG_MESH_LAYOUT_COMMIT_LEN)),
	OP(LAYOUT_CANCEL, BT_MESH_LEN_EXACT(CTAG_MESH_LAYOUT_CANCEL_LEN)),
	OP(RESULT_ACK, BT_MESH_LEN_EXACT(CTAG_MESH_RESULT_ACK_LEN)),
	BT_MESH_MODEL_OP_END,
};

static const struct bt_mesh_model_op mgmt_ops[] = {
	OP(CAPS_GET, BT_MESH_LEN_EXACT(CTAG_MESH_CAPS_GET_LEN)),
	OP(HEALTH_GET, BT_MESH_LEN_EXACT(CTAG_MESH_HEALTH_GET_LEN)),
	OP(ASSIGN_SET, BT_MESH_LEN_EXACT(CTAG_MESH_ASSIGN_SET_LEN)),
	OP(ASSIGN_DEL, BT_MESH_LEN_EXACT(CTAG_MESH_ASSIGN_DEL_LEN)),
	OP(TAG_CMD, BT_MESH_LEN_EXACT(CTAG_MESH_TAG_CMD_LEN)),
	OP(IDENTIFY, BT_MESH_LEN_EXACT(CTAG_MESH_IDENTIFY_LEN)),
	BT_MESH_MODEL_OP_END,
};

/* ---- Composition ---- */

static void attention_on(const struct bt_mesh_model *model)
{
	ARG_UNUSED(model);
	identify_start(10);
}

static const struct bt_mesh_health_srv_cb health_cb = {.attn_on = attention_on};
static struct bt_mesh_health_srv health_srv = {.cb = &health_cb};
BT_MESH_HEALTH_PUB_DEFINE(health_pub, 0);

static const struct bt_mesh_model sig_models[] = {
	BT_MESH_MODEL_CFG_SRV,
	BT_MESH_MODEL_HEALTH_SRV(&health_srv, &health_pub),
};

static const struct bt_mesh_model vnd_models[] = {
	BT_MESH_MODEL_VND_CB(CTAG_MESH_COMPANY_ID, CTAG_MESH_MODEL_LAYOUT_SRV, layout_ops, NULL, NULL,
			     NULL),
	BT_MESH_MODEL_VND_CB(CTAG_MESH_COMPANY_ID, CTAG_MESH_MODEL_MGMT_SRV, mgmt_ops, NULL, NULL,
			     NULL),
};

#define LAYOUT_MODEL (&vnd_models[0])
#define MGMT_MODEL   (&vnd_models[1])

static const struct bt_mesh_elem elements[] = {
	BT_MESH_ELEM(0, sig_models, vnd_models),
};

static const struct bt_mesh_comp comp = {
	.cid = CTAG_MESH_COMPANY_ID,
	.pid = CTAG_NODE_ROLE_BRIDGE,
	.vid = (BRIDGE_FW_MAJOR << 8) | BRIDGE_FW_MINOR,
	.elem = elements,
	.elem_count = ARRAY_SIZE(elements),
};

/* ---- Provisioning (PB-ADV, no OOB: docs/protocol.md 2) ---- */

static uint8_t dev_uuid[16];
static atomic_t configured_at; /* uptime (ms) of provisioning; 0 = provisioned before boot */

static void node_reset_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	/* A running session ends first: its job is about to be forgotten. */
	if (tsess_active(&br.sess)) {
		tsess_abort(&br.sess, CTAG_STATUS_CANCELLED);
	}
	dlv_reset(&br.dlv);
	(void)bt_mesh_prov_enable(BT_MESH_PROV_ADV);
}

static K_WORK_DEFINE(node_reset_work, node_reset_fn);

static void prov_complete(uint16_t net_idx, uint16_t addr)
{
	ARG_UNUSED(net_idx);
	atomic_set(&configured_at, (atomic_val_t)MAX(k_uptime_get_32(), 1u));
	LOG_INF("provisioned as 0x%04x", addr);
}

static void prov_reset(void)
{
	/* Config Node Reset (REMOVE_NODE): forget the tags and beacon again. */
	(void)k_work_submit_to_queue(&bwq, &node_reset_work);
}

static const struct bt_mesh_prov prov = {
	.uuid = dev_uuid,
	.complete = prov_complete,
	.reset = prov_reset,
};

bool mesh_node_ready(void)
{
	uint32_t at = (uint32_t)atomic_get(&configured_at);

	/* Configured = both vendor models bound to an app key; and not within the
	 * quiet window after provisioning, while CONFIGURE_NODE is still running. */
	return bt_mesh_is_provisioned() && LAYOUT_MODEL->keys[0] != BT_MESH_KEY_UNUSED &&
	       MGMT_MODEL->keys[0] != BT_MESH_KEY_UNUSED &&
	       (at == 0u || k_uptime_get_32() - at >= CONFIG_CTAG_BRIDGE_CFG_QUIET_MS);
}

/* ---- Outbound queue ---- */

struct mesh_tx {
	uint8_t op;
	uint8_t len;
	uint16_t dst;
	uint8_t data[CTAG_MESH_DELIVERY_RESULT_LEN];
};

static struct mesh_tx txq[CONFIG_CTAG_BRIDGE_MESH_TXQ];
static uint8_t tx_head, tx_count;
static bool tx_inflight;
static bool suspended;
static atomic_t tx_ended;

static void tx_work_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(tx_work, tx_work_fn);

/* Mesh context: the send is over (segments acknowledged or given up). */
static void send_end(int err, void *cb_data)
{
	ARG_UNUSED(cb_data);
	if (err != 0) {
		cnt.tx_failed++;
	}
	atomic_set(&tx_ended, 1);
	(void)k_work_reschedule_for_queue(&bwq, &tx_work, K_NO_WAIT);
}

static const struct bt_mesh_send_cb send_cb = {.end = send_end};

static const struct bt_mesh_model *model_for(uint8_t op)
{
	switch (op) {
	case CTAG_MESH_OP_LAYOUT_STATUS:
	case CTAG_MESH_OP_DELIVERY_STAGE:
	case CTAG_MESH_OP_DELIVERY_RESULT:
		return LAYOUT_MODEL;
	default:
		return MGMT_MODEL;
	}
}

static void tx_kick(void)
{
	while (!tx_inflight && tx_count > 0u && !suspended) {
		const struct mesh_tx *t = &txq[tx_head];
		const struct bt_mesh_model *model = model_for(t->op);
		struct bt_mesh_msg_ctx ctx = {
			.net_idx = NET_PRIMARY,
			.app_idx = model->keys[0],
			.addr = t->dst,
			.send_ttl = BT_MESH_TTL_DEFAULT,
		};
		BT_MESH_MODEL_BUF_DEFINE(msg, CTAG_MESH_OPCODE_DELIVERY_RESULT,
					 CTAG_MESH_DELIVERY_RESULT_LEN);
		int err;

		if (!bt_mesh_is_provisioned() || ctx.app_idx == BT_MESH_KEY_UNUSED) {
			err = -EACCES; /* not configured: nothing can be sent */
		} else {
			bt_mesh_model_msg_init(&msg, CTAG_MESH_VENDOR_OPCODE(t->op));
			net_buf_simple_add_mem(&msg, t->data, t->len);
			tx_inflight = true;
			atomic_clear(&tx_ended);
			err = bt_mesh_model_send(model, &ctx, &msg, &send_cb, NULL);
		}
		if (err == 0) {
			cnt.tx++;
			/* The watchdog, unless the end callback already came. */
			(void)k_work_reschedule_for_queue(
				&bwq, &tx_work,
				atomic_get(&tx_ended) ? K_NO_WAIT : K_MSEC(TX_WATCHDOG_MS));
		} else {
			tx_inflight = false;
			if (err == -EBUSY || err == -ENOBUFS || err == -EAGAIN) {
				cnt.tx_busy++; /* a segmented send is still running: retry */
				(void)k_work_reschedule_for_queue(&bwq, &tx_work, K_MSEC(TX_RETRY_MS));
				return;
			}
			cnt.tx_dropped++;
		}
		tx_head = (uint8_t)((tx_head + 1u) % ARRAY_SIZE(txq));
		tx_count--;
	}
}

static void tx_work_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	/* Also the watchdog: an end callback that never came frees the slot. */
	tx_inflight = false;
	tx_kick();
}

void mesh_send(uint8_t op, const uint8_t *params, size_t len, uint16_t dst)
{
	struct mesh_tx *t;

	if (len > sizeof(t->data)) {
		return;
	}
	if (tx_count == ARRAY_SIZE(txq)) {
		cnt.tx_dropped++; /* results are re-sent; stages and sightings are best effort */
		return;
	}
	t = &txq[(tx_head + tx_count) % ARRAY_SIZE(txq)];
	t->op = op;
	t->len = (uint8_t)len;
	t->dst = dst;
	memcpy(t->data, params, len);
	tx_count++;
	tx_kick();
}

bool mesh_busy(void)
{
	return tx_inflight || tx_count > 0u;
}

void mesh_set_suspended(bool s)
{
	suspended = s;
	if (!s) {
		tx_kick();
	}
}

/* ---- Inbound processing on the work queue ---- */

static void reply(uint8_t op, const uint8_t *buf, int len, uint16_t dst)
{
	if (len >= 0) {
		mesh_send(op, buf, (size_t)len, dst);
	}
}

static void caps_status(uint16_t dst)
{
	struct ctag_mesh_caps_status m = {
		.proto = CTAG_PROTO_VERSION,
		.fw_major = BRIDGE_FW_MAJOR,
		.fw_minor = BRIDGE_FW_MINOR,
		.fw_patch = BRIDGE_FW_PATCH,
		.board = CONFIG_CTAG_BRIDGE_BOARD_ID,
		.flash_mib = (uint16_t)MIN(br.flash.geom.flash_size >> 20, UINT16_MAX),
		.max_tags = CONFIG_CTAG_BRIDGE_MAX_TAGS,
		.assigned = dlv_assigned_count(&br.dlv),
	};
	uint8_t buf[CTAG_MESH_CAPS_STATUS_LEN];

	if (fontstore_active_id(&br.fonts, m.fontpack_id)) {
		m.flags |= 0x01u;
	}
	if (sched_busy(&br.sched)) {
		m.flags |= 0x02u;
	}
	reply(CTAG_MESH_OP_CAPS_STATUS, buf, ctag_mesh_caps_status_pack(&m, buf, sizeof(buf)), dst);
}

static void health_status(uint16_t dst)
{
	const struct sched_counters *c = &br.sched.c;
	struct ctag_mesh_health_status m = {
		.uptime_s = (uint32_t)(k_uptime_get() / 1000),
		.sessions_ok = (uint16_t)MIN(c->sessions_ok, UINT16_MAX),
		.sessions_fail = (uint16_t)MIN(c->sessions_fail, UINT16_MAX),
		.suspend_count = (uint16_t)MIN(c->suspend_count, UINT16_MAX),
		.suspend_max_ms = (uint16_t)MIN(c->suspend_max_ms, UINT16_MAX),
		.resume_fail = (uint16_t)MIN(c->resume_fail, UINT16_MAX),
		.queue_depth = dlv_queue_depth(&br.dlv),
		.last_status = br.sched.last_status,
	};
	uint8_t buf[CTAG_MESH_HEALTH_STATUS_LEN];

	reply(CTAG_MESH_OP_HEALTH_STATUS, buf, ctag_mesh_health_status_pack(&m, buf, sizeof(buf)),
	      dst);
}

static void assign_status(uint32_t tag_id, uint32_t epoch, uint8_t status, uint16_t dst)
{
	struct ctag_mesh_assign_status m = {.tag_id = tag_id, .epoch = epoch, .status = status};
	uint8_t buf[CTAG_MESH_ASSIGN_STATUS_LEN];

	reply(CTAG_MESH_OP_ASSIGN_STATUS, buf, ctag_mesh_assign_status_pack(&m, buf, sizeof(buf)),
	      dst);
}

static void handle(const struct mesh_rx *m)
{
	uint32_t now = k_uptime_get_32();

	switch (m->op) {
	case CTAG_MESH_OP_LAYOUT_BEGIN: {
		struct ctag_mesh_layout_begin b;

		if (ctag_mesh_layout_begin_unpack(&b, m->data, m->len) == 0) {
			dlv_layout_begin(&br.dlv, &b);
		}
		break;
	}
	case CTAG_MESH_OP_LAYOUT_CHUNK: {
		struct ctag_mesh_layout_chunk c;

		if (ctag_mesh_layout_chunk_unpack(&c, m->data, m->len) == 0) {
			dlv_layout_chunk(&br.dlv, &c);
		}
		break;
	}
	case CTAG_MESH_OP_LAYOUT_COMMIT: {
		struct ctag_mesh_layout_commit c;
		struct ctag_mesh_layout_status s = {0};
		uint8_t buf[CTAG_MESH_LAYOUT_STATUS_LEN];

		if (ctag_mesh_layout_commit_unpack(&c, m->data, m->len) == 0) {
			s.xfer_id = c.xfer_id;
			s.status = dlv_layout_commit(&br.dlv, c.xfer_id, now, &s.missing);
			reply(CTAG_MESH_OP_LAYOUT_STATUS, buf,
			      ctag_mesh_layout_status_pack(&s, buf, sizeof(buf)), m->src);
		}
		break;
	}
	case CTAG_MESH_OP_LAYOUT_CANCEL: {
		struct ctag_mesh_layout_cancel c;

		if (ctag_mesh_layout_cancel_unpack(&c, m->data, m->len) == 0) {
			dlv_layout_cancel(&br.dlv, c.update_id, now);
		}
		break;
	}
	case CTAG_MESH_OP_RESULT_ACK: {
		struct ctag_mesh_result_ack a;

		if (ctag_mesh_result_ack_unpack(&a, m->data, m->len) == 0) {
			dlv_result_ack(&br.dlv, a.result_seq);
		}
		break;
	}
	case CTAG_MESH_OP_CAPS_GET:
		caps_status(m->src);
		break;
	case CTAG_MESH_OP_HEALTH_GET:
		health_status(m->src);
		break;
	case CTAG_MESH_OP_ASSIGN_SET: {
		struct ctag_mesh_assign_set a;

		if (ctag_mesh_assign_set_unpack(&a, m->data, m->len) == 0) {
			assign_status(a.tag_id, a.epoch, dlv_assign_set(&br.dlv, &a, now), m->src);
		}
		break;
	}
	case CTAG_MESH_OP_ASSIGN_DEL: {
		struct ctag_mesh_assign_del a;

		if (ctag_mesh_assign_del_unpack(&a, m->data, m->len) == 0) {
			assign_status(a.tag_id, a.epoch, dlv_assign_del(&br.dlv, &a, now), m->src);
		}
		break;
	}
	case CTAG_MESH_OP_TAG_CMD: {
		struct ctag_mesh_tag_cmd t;

		if (ctag_mesh_tag_cmd_unpack(&t, m->data, m->len) == 0) {
			dlv_tag_cmd(&br.dlv, &t, now);
		}
		break;
	}
	case CTAG_MESH_OP_IDENTIFY: {
		struct ctag_mesh_identify i;

		if (ctag_mesh_identify_unpack(&i, m->data, m->len) == 0) {
			identify_start(i.seconds);
		}
		break;
	}
	default:
		break;
	}
}

static void rx_work_fn(struct k_work *work)
{
	struct mesh_rx m;

	ARG_UNUSED(work);
	while (k_msgq_get(&mesh_rx_q, &m, K_NO_WAIT) == 0) {
		cnt.rx++;
		handle(&m);
	}
}

void mesh_rx_drain(void)
{
	(void)k_work_submit_to_queue(&bwq, &rx_work);
}

/* ---- Init ---- */

int mesh_init(void)
{
	uint8_t id[8] = {0};

	/* UUID: "CTBR", board id, 3 zero bytes, then the FICR device id. The
	 * prefix lets `cremind-tag mesh scan --filter 43544252` list bridges. */
	(void)hwinfo_get_device_id(id, sizeof(id));
	memcpy(dev_uuid, "CTBR", 4);
	dev_uuid[4] = CONFIG_CTAG_BRIDGE_BOARD_ID;
	memcpy(&dev_uuid[8], id, sizeof(id));
	return bt_mesh_init(&prov, &comp);
}

void mesh_start(void)
{
	if (!bt_mesh_is_provisioned()) {
		int err = bt_mesh_prov_enable(BT_MESH_PROV_ADV);

		LOG_INF("unprovisioned: beaconing (%d)", err);
	} else {
		LOG_INF("provisioned; %s", mesh_node_ready() ? "configured" : "not configured yet");
	}
}

size_t mesh_counters(struct ctag_cbor_counter *items, size_t max)
{
	size_t n = 0u;

	BRIDGE_COUNTER("mesh_rx", cnt.rx);
	BRIDGE_COUNTER("mesh_rx_dropped", cnt.rx_dropped);
	BRIDGE_COUNTER("mesh_tx", cnt.tx);
	BRIDGE_COUNTER("mesh_tx_failed", cnt.tx_failed);
	BRIDGE_COUNTER("mesh_tx_dropped", cnt.tx_dropped);
	BRIDGE_COUNTER("mesh_tx_busy", cnt.tx_busy);
	BRIDGE_COUNTER("provisioned", bt_mesh_is_provisioned() ? 1u : 0u);
	BRIDGE_COUNTER("configured", mesh_node_ready() ? 1u : 0u);
	return n;
}
