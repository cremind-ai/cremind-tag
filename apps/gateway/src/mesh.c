/*
 * Bluetooth Mesh provisioner (docs/protocol.md 2): composition, the vendor
 * client models LAYOUT_CLI and MGMT_CLI, the Configuration and Health
 * clients, PB-ADV provisioning, the CDB, and the backend functions the core
 * calls. Every Bluetooth callback copies what it got into a gw_evt for the
 * gateway thread and returns.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/mesh.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/byteorder.h>

#include <psa/crypto.h>

#include "gw_app.h"
#include "gw_mesh.h"

LOG_MODULE_REGISTER(gw_mesh, LOG_LEVEL_INF);

#define NET_IDX           BT_MESH_NET_PRIMARY
#define APP_IDX           0x000
#define OP_APP_KEY_ADD    BT_MESH_MODEL_OP_1(0x00)
#define AD_MESH_BEACON    0x2B /* Mesh Beacon AD type */
#define BEACON_UNPROV     0x00
#define RELAY_TRANSMIT    BT_MESH_TRANSMIT(2, 20) /* 2 retransmissions, 20 ms (2) */
#define NET_TRANSMIT      BT_MESH_TRANSMIT(3, 20) /* 3 retransmissions, 20 ms (2) */
#define SELF_CFG_TIMEOUT  2000

static uint8_t dev_uuid[16];
static int mesh_err;
static uint32_t start_errors, cfg_errors;

/* ---- Inbound vendor messages: queued for the gateway thread ---- */

static int queue_rx(uint8_t op, struct bt_mesh_msg_ctx *ctx, struct net_buf_simple *buf)
{
	struct gw_evt e = {.type = GW_EVT_MESH_RX, .op = op, .addr = ctx->addr};

	if (buf->len > sizeof(e.data)) {
		return -EINVAL;
	}
	e.len = (uint8_t)buf->len;
	memcpy(e.data, buf->data, buf->len);
	gw_post(&e);
	return 0;
}

#define HANDLER(name)                                                                            \
	static int h_##name(const struct bt_mesh_model *model, struct bt_mesh_msg_ctx *ctx,     \
			    struct net_buf_simple *buf)                                          \
	{                                                                                        \
		ARG_UNUSED(model);                                                               \
		return queue_rx(CTAG_MESH_OP_##name, ctx, buf);                                  \
	}

HANDLER(LAYOUT_STATUS)
HANDLER(DELIVERY_STAGE)
HANDLER(DELIVERY_RESULT)
HANDLER(CAPS_STATUS)
HANDLER(HEALTH_STATUS)
HANDLER(ASSIGN_STATUS)
HANDLER(TAG_SEEN)

#define OP(name) {CTAG_MESH_OPCODE_##name, BT_MESH_LEN_EXACT(CTAG_MESH_##name##_LEN), h_##name}

static const struct bt_mesh_model_op layout_cli_ops[] = {
	OP(LAYOUT_STATUS),
	OP(DELIVERY_STAGE),
	OP(DELIVERY_RESULT),
	BT_MESH_MODEL_OP_END,
};

static const struct bt_mesh_model_op mgmt_cli_ops[] = {
	OP(CAPS_STATUS),
	OP(HEALTH_STATUS),
	OP(ASSIGN_STATUS),
	OP(TAG_SEEN),
	BT_MESH_MODEL_OP_END,
};

/* ---- Configuration client callbacks ---- */

static void cfg_post(uint16_t addr, uint8_t step, uint8_t status, uint8_t value)
{
	struct gw_evt e = {.type = GW_EVT_CFG_STATUS, .op = step, .addr = addr};

	e.data[0] = status;
	e.data[1] = value;
	gw_post(&e);
}

static void cb_app_key(struct bt_mesh_cfg_cli *cli, uint16_t addr, uint8_t status,
		       uint16_t net_idx, uint16_t app_idx)
{
	ARG_UNUSED(cli);
	if (net_idx == NET_IDX && app_idx == APP_IDX) {
		cfg_post(addr, GW_CFG_APP_KEY_ADD, status, 0u);
	}
}

static void cb_mod_app(struct bt_mesh_cfg_cli *cli, uint16_t addr, uint8_t status,
		       uint16_t elem_addr, uint16_t app_idx, uint32_t mod_id)
{
	uint16_t id = (uint16_t)(mod_id & 0xFFFFu);

	ARG_UNUSED(cli);
	ARG_UNUSED(elem_addr);
	ARG_UNUSED(app_idx);
	if (id == CTAG_MESH_MODEL_LAYOUT_SRV) {
		cfg_post(addr, GW_CFG_BIND_LAYOUT, status, 0u);
	} else if (id == CTAG_MESH_MODEL_MGMT_SRV) {
		cfg_post(addr, GW_CFG_BIND_MGMT, status, 0u);
	}
}

static void cb_relay(struct bt_mesh_cfg_cli *cli, uint16_t addr, uint8_t status, uint8_t transmit)
{
	ARG_UNUSED(cli);
	ARG_UNUSED(transmit);
	cfg_post(addr, GW_CFG_RELAY, 0u, status); /* status = the relay state */
}

static void cb_ttl(struct bt_mesh_cfg_cli *cli, uint16_t addr, uint8_t status)
{
	ARG_UNUSED(cli);
	cfg_post(addr, GW_CFG_TTL, 0u, status); /* status = the default TTL */
}

static void cb_net_tx(struct bt_mesh_cfg_cli *cli, uint16_t addr, uint8_t status)
{
	ARG_UNUSED(cli);
	cfg_post(addr, GW_CFG_NET_TX, 0u, status);
}

static void cb_reset(struct bt_mesh_cfg_cli *cli, uint16_t addr)
{
	ARG_UNUSED(cli);
	cfg_post(addr, GW_CFG_RESET, 0u, 0u);
}

static const struct bt_mesh_cfg_cli_cb cfg_cli_cb = {
	.app_key_status = cb_app_key,
	.mod_app_status = cb_mod_app,
	.relay_status = cb_relay,
	.ttl_status = cb_ttl,
	.network_transmit_status = cb_net_tx,
	.node_reset_status = cb_reset,
};

static struct bt_mesh_cfg_cli cfg_cli = {.cb = &cfg_cli_cb};
static struct bt_mesh_health_cli health_cli;

/* ---- Composition ---- */

static const struct bt_mesh_model sig_models[] = {
	BT_MESH_MODEL_CFG_SRV,
	BT_MESH_MODEL_CFG_CLI(&cfg_cli),
	BT_MESH_MODEL_HEALTH_CLI(&health_cli),
};

static const struct bt_mesh_model vnd_models[] = {
	BT_MESH_MODEL_VND_CB(CTAG_MESH_COMPANY_ID, CTAG_MESH_MODEL_LAYOUT_CLI, layout_cli_ops, NULL,
			     NULL, NULL),
	BT_MESH_MODEL_VND_CB(CTAG_MESH_COMPANY_ID, CTAG_MESH_MODEL_MGMT_CLI, mgmt_cli_ops, NULL, NULL,
			     NULL),
};

#define LAYOUT_CLI_MODEL (&vnd_models[0])
#define MGMT_CLI_MODEL   (&vnd_models[1])

static const struct bt_mesh_elem elements[] = {
	BT_MESH_ELEM(0, sig_models, vnd_models),
};

static const struct bt_mesh_comp comp = {
	.cid = CTAG_MESH_COMPANY_ID,
	.pid = CTAG_NODE_ROLE_GATEWAY,
	.vid = 1,
	.elem = elements,
	.elem_count = ARRAY_SIZE(elements),
};

/* ---- Provisioning callbacks ---- */

static void prov_link_open(bt_mesh_prov_bearer_t bearer)
{
	struct gw_evt e = {.type = GW_EVT_PROV_OPEN};

	ARG_UNUSED(bearer);
	gw_post(&e);
}

static void prov_link_close(bt_mesh_prov_bearer_t bearer)
{
	struct gw_evt e = {.type = GW_EVT_PROV_CLOSED};

	ARG_UNUSED(bearer);
	gw_post(&e);
}

static void prov_node_added(uint16_t net_idx, uint8_t uuid[16], uint16_t addr, uint8_t num_elem)
{
	struct gw_evt e = {.type = GW_EVT_PROV_ADDED, .addr = addr, .u16 = num_elem};

	ARG_UNUSED(net_idx);
	memcpy(e.data, uuid, 16);
	gw_post(&e);
}

static const struct bt_mesh_prov prov = {
	.uuid = dev_uuid,
	.link_open = prov_link_open,
	.link_close = prov_link_close,
	.node_added = prov_node_added,
};

/* Unprovisioned beacons with their RSSI, from the scan the mesh runs anyway
 * (the provisioner's unprovisioned_beacon callback carries no RSSI). */
static void scan_recv(const struct bt_le_scan_recv_info *info, struct net_buf_simple *buf)
{
	const uint8_t *p = buf->data;
	size_t left = buf->len;

	while (left >= 2u) {
		uint8_t len = p[0];

		if (len == 0u || len + 1u > left) {
			return;
		}
		/* [len][0x2B][type 0x00][uuid 16][oob 2][uri hash 4]? */
		if (p[1] == AD_MESH_BEACON && len >= 20u && p[2] == BEACON_UNPROV) {
			struct gw_evt e = {.type = GW_EVT_BEACON, .rssi = info->rssi};

			memcpy(e.data, &p[3], 16);
			e.u16 = sys_get_be16(&p[19]);
			gw_post(&e);
			return;
		}
		p += len + 1u;
		left -= len + 1u;
	}
}

static struct bt_le_scan_cb scan_cb = {.recv = scan_recv};

/* ---- Sending ---- */

static void send_start(uint16_t duration, int err, void *cb_data)
{
	ARG_UNUSED(duration);
	if (err != 0) {
		/* The advertiser refused it; no end callback follows. */
		struct gw_evt e = {.type = GW_EVT_SEND_END, .tag = (uint32_t)(uintptr_t)cb_data,
				   .err = err};

		gw_post(&e);
	}
}

static void send_end(int err, void *cb_data)
{
	struct gw_evt e = {.type = GW_EVT_SEND_END, .tag = (uint32_t)(uintptr_t)cb_data, .err = err};

	gw_post(&e);
}

static const struct bt_mesh_send_cb send_cb = {.start = send_start, .end = send_end};

int gw_mesh_send(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
		 uint32_t tag)
{
	BT_MESH_MODEL_BUF_DEFINE(msg, CTAG_MESH_VENDOR_OPCODE(0), CTAG_MESH_MAX_VENDOR_PARAMS);
	struct bt_mesh_msg_ctx mctx = BT_MESH_MSG_CTX_INIT_APP(APP_IDX, dst);
	const struct bt_mesh_model *model = op < CTAG_MESH_OP_CAPS_GET ? LAYOUT_CLI_MODEL
								      : MGMT_CLI_MODEL;

	ARG_UNUSED(ctx);
	if (len > CTAG_MESH_MAX_VENDOR_PARAMS) {
		return -EMSGSIZE;
	}
	bt_mesh_model_msg_init(&msg, CTAG_MESH_VENDOR_OPCODE(op));
	net_buf_simple_add_mem(&msg, params, len);
	return bt_mesh_model_send(model, &mctx, &msg, tag != 0u ? &send_cb : NULL,
				  (void *)(uintptr_t)tag);
}

static int app_key(uint8_t key[16])
{
	struct bt_mesh_cdb_app_key *k = bt_mesh_cdb_app_key_get(APP_IDX);

	return k == NULL ? -ENOENT : bt_mesh_cdb_app_key_export(k, 0, key);
}

/* Config AppKey Add, built here so its (segmented) send reports its end. */
static int app_key_add(uint16_t addr, uint32_t tag)
{
	BT_MESH_MODEL_BUF_DEFINE(msg, OP_APP_KEY_ADD, 19);
	struct bt_mesh_msg_ctx mctx = BT_MESH_MSG_CTX_INIT_DEV(NET_IDX, addr);
	uint8_t key[16];
	int err = app_key(key);

	if (err != 0) {
		return err;
	}
	bt_mesh_model_msg_init(&msg, OP_APP_KEY_ADD);
	net_buf_simple_add_le16(&msg, NET_IDX | ((APP_IDX & 0x00Fu) << 12));
	net_buf_simple_add_u8(&msg, APP_IDX >> 4);
	net_buf_simple_add_mem(&msg, key, sizeof(key));
	err = bt_mesh_model_send(cfg_cli.model, &mctx, &msg, tag != 0u ? &send_cb : NULL,
				 (void *)(uintptr_t)tag);
	memset(key, 0, sizeof(key));
	return err;
}

int gw_mesh_cfg(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag)
{
	int err;

	ARG_UNUSED(ctx);
	switch (step) {
	case GW_CFG_APP_KEY_ADD:
		err = app_key_add(addr, tag);
		break;
	case GW_CFG_BIND_LAYOUT:
	case GW_CFG_BIND_MGMT:
		err = bt_mesh_cfg_cli_mod_app_bind_vnd(
			NET_IDX, addr, addr, APP_IDX,
			step == GW_CFG_BIND_LAYOUT ? CTAG_MESH_MODEL_LAYOUT_SRV : CTAG_MESH_MODEL_MGMT_SRV,
			CTAG_MESH_COMPANY_ID, NULL);
		break;
	case GW_CFG_RELAY:
		err = bt_mesh_cfg_cli_relay_set(NET_IDX, addr,
						arg ? BT_MESH_RELAY_ENABLED : BT_MESH_RELAY_DISABLED,
						RELAY_TRANSMIT, NULL, NULL);
		break;
	case GW_CFG_TTL:
		err = bt_mesh_cfg_cli_ttl_set(NET_IDX, addr, arg, NULL);
		break;
	case GW_CFG_NET_TX:
		err = bt_mesh_cfg_cli_net_transmit_set(NET_IDX, addr, NET_TRANSMIT, NULL);
		break;
	case GW_CFG_RESET:
		err = bt_mesh_cfg_cli_node_reset(NET_IDX, addr, NULL);
		break;
	default:
		err = -EINVAL;
		break;
	}
	if (err != 0) {
		cfg_errors++;
	}
	return err;
}

int gw_mesh_provision(void *ctx, const uint8_t uuid[16])
{
	ARG_UNUSED(ctx);
	/* Address 0: the CDB allocator picks the next free unicast range. */
	return bt_mesh_provision_adv(uuid, NET_IDX, 0, 0);
}

void gw_mesh_node_configured(void *ctx, uint16_t addr)
{
	struct bt_mesh_cdb_node *node = bt_mesh_cdb_node_get(addr);

	ARG_UNUSED(ctx);
	if (node != NULL) {
		atomic_set_bit(node->flags, BT_MESH_CDB_NODE_CONFIGURED);
		bt_mesh_cdb_node_store(node);
	}
}

void gw_mesh_node_delete(void *ctx, uint16_t addr)
{
	struct bt_mesh_cdb_node *node = bt_mesh_cdb_node_get(addr);

	ARG_UNUSED(ctx);
	if (node != NULL) {
		bt_mesh_cdb_node_del(node, true);
	}
	gw_store_name(NULL, addr, NULL, 0u);
}

int gw_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32])
{
	size_t olen;

	ARG_UNUSED(ctx);
	return psa_hash_compute(PSA_ALG_SHA_256, data, len, out, 32, &olen) == PSA_SUCCESS ? 0
											   : -EIO;
}

size_t gw_mesh_counters(struct ctag_cbor_counter *items, size_t max)
{
	const struct ctag_cbor_counter all[] = {
		CTAG_CBOR_COUNTER("mesh_init", (uint32_t)-mesh_err),
		CTAG_CBOR_COUNTER("mesh_start_errors", start_errors),
		CTAG_CBOR_COUNTER("cfg_send_errors", cfg_errors),
		CTAG_CBOR_COUNTER("evq_dropped", gw_thread_dropped()),
	};
	size_t n = ARRAY_SIZE(all) < max ? ARRAY_SIZE(all) : max;

	memcpy(items, all, n * sizeof(all[0]));
	return n;
}

/* ---- Start-up ---- */

static void make_uuid(void)
{
	static const uint8_t prefix[6] = {'C', 'T', 'A', 'G', 'G', 'W'};
	uint8_t id[10] = {0};

	(void)hwinfo_get_device_id(id, sizeof(id));
	memcpy(dev_uuid, prefix, sizeof(prefix));
	memcpy(&dev_uuid[6], id, sizeof(id));
}

/* First boot: a random app key in the CDB. */
static int create_app_key(void)
{
	struct bt_mesh_cdb_app_key *k = bt_mesh_cdb_app_key_alloc(NET_IDX, APP_IDX);
	uint8_t key[16];
	int err;

	if (k == NULL) {
		return -ENOMEM;
	}
	err = sys_csrand_get(key, sizeof(key));
	if (err == 0) {
		err = bt_mesh_cdb_app_key_import(k, 0, key);
	}
	memset(key, 0, sizeof(key));
	if (err == 0) {
		bt_mesh_cdb_app_key_store(k);
	}
	return err;
}

/* The gateway binds its own client models to app key 0 once (the
 * configuration client talking to the local configuration server). */
static int configure_self(void)
{
	struct bt_mesh_cdb_node *self = bt_mesh_cdb_node_get(GW_ADDR);
	uint8_t key[16];
	uint8_t status = 0;
	int err;

	if (self == NULL) {
		return -ENOENT;
	}
	if (atomic_test_bit(self->flags, BT_MESH_CDB_NODE_CONFIGURED)) {
		return 0;
	}
	err = app_key(key);
	if (err == 0) {
		err = bt_mesh_cfg_cli_app_key_add(NET_IDX, GW_ADDR, NET_IDX, APP_IDX, key, &status);
	}
	memset(key, 0, sizeof(key));
	if (err == 0 && status == 0) {
		err = bt_mesh_cfg_cli_mod_app_bind_vnd(NET_IDX, GW_ADDR, GW_ADDR, APP_IDX,
						       CTAG_MESH_MODEL_LAYOUT_CLI,
						       CTAG_MESH_COMPANY_ID, &status);
	}
	if (err == 0 && status == 0) {
		err = bt_mesh_cfg_cli_mod_app_bind_vnd(NET_IDX, GW_ADDR, GW_ADDR, APP_IDX,
						       CTAG_MESH_MODEL_MGMT_CLI,
						       CTAG_MESH_COMPANY_ID, &status);
	}
	if (err == 0 && status == 0) {
		err = bt_mesh_cfg_cli_mod_app_bind(NET_IDX, GW_ADDR, GW_ADDR, APP_IDX,
						   BT_MESH_MODEL_ID_HEALTH_CLI, &status);
	}
	if (err != 0 || status != 0) {
		LOG_ERR("self configuration failed (err %d, status %u)", err, status);
		return err != 0 ? err : -EIO;
	}
	atomic_set_bit(self->flags, BT_MESH_CDB_NODE_CONFIGURED);
	bt_mesh_cdb_node_store(self);
	return 0;
}

static int start_network(void)
{
	uint8_t net_key[16];
	uint8_t dev_key[16];
	int err;

	err = sys_csrand_get(net_key, sizeof(net_key));
	if (err != 0) {
		return err;
	}
	err = bt_mesh_cdb_create(net_key);
	if (err == 0) {
		LOG_INF("new network: CDB created");
		err = create_app_key();
		if (err != 0) {
			return err;
		}
	} else if (err == -EALREADY) {
		/* Loaded from settings: the net key for a missing self entry. */
		struct bt_mesh_cdb_subnet *sub = bt_mesh_cdb_subnet_get(NET_IDX);

		if (sub == NULL || bt_mesh_cdb_subnet_key_export(sub, 0, net_key) != 0) {
			return -ENOENT;
		}
	} else {
		return err;
	}
	if (!bt_mesh_is_provisioned()) {
		struct bt_mesh_cdb_node *stale = bt_mesh_cdb_node_get(GW_ADDR);

		if (stale != NULL) {
			bt_mesh_cdb_node_del(stale, true); /* power lost mid first boot */
		}
		err = sys_csrand_get(dev_key, sizeof(dev_key));
		if (err == 0) {
			err = bt_mesh_provision(net_key, NET_IDX, 0, bt_mesh_cdb.iv_index, GW_ADDR,
						dev_key);
		}
		memset(dev_key, 0, sizeof(dev_key));
		if (err != 0 && err != -EALREADY) {
			return err;
		}
		LOG_INF("provisioned itself at 0x%04x", GW_ADDR);
	}
	memset(net_key, 0, sizeof(net_key));
	return configure_self();
}

int gw_mesh_start(void)
{
	int err;

	make_uuid();
	err = bt_enable(NULL);
	if (err == 0) {
		err = bt_mesh_init(&prov, &comp);
	}
	if (err == 0 && IS_ENABLED(CONFIG_SETTINGS)) {
		/* bt_enable -> bt_mesh_init -> settings_load (firmware-notes 3). */
		err = settings_load();
	}
	if (err == 0) {
		err = start_network();
	}
	if (err == 0) {
		bt_le_scan_cb_register(&scan_cb);
	} else {
		start_errors++;
		LOG_ERR("mesh start failed: %d", err);
	}
	mesh_err = err;
	return err;
}

static uint8_t add_node(struct bt_mesh_cdb_node *node, void *user_data)
{
	struct gw_core *g = user_data;

	if (node->addr != GW_ADDR) {
		gw_core_add_node(g, node->addr, node->uuid, node->num_elem,
				 atomic_test_bit(node->flags, BT_MESH_CDB_NODE_CONFIGURED));
	}
	return BT_MESH_CDB_ITER_CONTINUE;
}

void gw_mesh_load_nodes(struct gw_core *g)
{
	if (mesh_err == 0) {
		bt_mesh_cdb_node_foreach(add_node, g);
	}
}
