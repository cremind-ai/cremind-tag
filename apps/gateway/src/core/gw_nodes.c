/*
 * The network side (docs/protocol.md 2, 10): the node table (mirror of the
 * CDB's bridges with their names and the CAPS/HEALTH cache), provisioning,
 * configuration and removal through the configuration client, assignments
 * and tag commands (MGMT_CLI), SCAN_UNPROV beacons, TAG_SEEN and the
 * LIST_NODES / GET_INVENTORY answers. Mirrors sim/gateway.py.
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

static const char T_NO_NODE[] = "no such node";
static const char T_UNCONFIGURED[] = "node is not configured";
static const char T_FULL[] = "MAX_BRIDGES reached";
static const char T_TTL[] = "ttl must be 0 or 2..127";
static const char T_PROV_START[] = "provisioning could not start";

/* Configuration Server status codes (Mesh Profile 4.3.5). */
#define CFG_INVALID_MODEL          0x02
#define CFG_INSUFFICIENT_RESOURCES 0x05
#define RELAY_NOT_SUPPORTED        0x02

/* ---- Node table ---- */

struct gw_node *gw_node_get(struct gw_core *g, uint16_t addr)
{
	for (int i = 0; i < GW_NODES; i++) {
		if (g->nodes[i].used && g->nodes[i].addr == addr) {
			return &g->nodes[i];
		}
	}
	return NULL;
}

static struct gw_node *node_by_uuid(struct gw_core *g, const uint8_t *uuid)
{
	for (int i = 0; i < GW_NODES; i++) {
		if (g->nodes[i].used && memcmp(g->nodes[i].uuid, uuid, GW_UUID_LEN) == 0) {
			return &g->nodes[i];
		}
	}
	return NULL;
}

uint8_t gw_node_count(const struct gw_core *g)
{
	uint8_t n = 0u;

	for (int i = 0; i < GW_NODES; i++) {
		n += g->nodes[i].used ? 1u : 0u;
	}
	return n;
}

static struct gw_node *node_add(struct gw_core *g, uint16_t addr, const uint8_t *uuid,
				uint8_t elements, bool configured)
{
	struct gw_node *n = gw_node_get(g, addr);

	if (addr == GW_ADDR) {
		return NULL; /* the gateway itself is not listed (as in the simulator) */
	}
	for (int i = 0; n == NULL && i < GW_NODES; i++) {
		if (!g->nodes[i].used) {
			n = &g->nodes[i];
			memset(n, 0, sizeof(*n));
		}
	}
	if (n == NULL) {
		return NULL;
	}
	n->used = true;
	n->addr = addr;
	n->elements = elements;
	n->configured = configured;
	memcpy(n->uuid, uuid, GW_UUID_LEN);
	n->last_seen = g->now;
	return n;
}

/* Keep at most CONFIG_CTAG_GW_NAME_MAX bytes, never splitting a UTF-8 sequence. */
static size_t name_fit(const char *name, size_t len)
{
	if (len <= CONFIG_CTAG_GW_NAME_MAX) {
		return len;
	}
	len = CONFIG_CTAG_GW_NAME_MAX;
	while (len > 0u && ((uint8_t)name[len] & 0xC0u) == 0x80u) {
		len--;
	}
	return len;
}

static void node_name(struct gw_node *n, const char *name, size_t len)
{
	len = name_fit(name, len);
	memcpy(n->name, name, len);
	n->name_len = (uint8_t)len;
}

void gw_core_add_node(struct gw_core *g, uint16_t addr, const uint8_t uuid[GW_UUID_LEN],
		      uint8_t elements, bool configured)
{
	(void)node_add(g, addr, uuid, elements, configured);
}

void gw_core_set_name(struct gw_core *g, uint16_t addr, const char *name, size_t len)
{
	struct gw_node *n = gw_node_get(g, addr);

	if (n != NULL) {
		node_name(n, name, len);
	}
}

void gw_core_set_assignments(struct gw_core *g, const struct gw_assign *a, size_t n)
{
	g->assign_count = (uint8_t)(n > CONFIG_CTAG_GW_ASSIGN_MAX ? CONFIG_CTAG_GW_ASSIGN_MAX : n);
	memcpy(g->assign, a, g->assign_count * sizeof(*a));
}

struct gw_req_result gw_node_usable(struct gw_core *g, uint16_t addr)
{
	struct gw_node *n = gw_node_get(g, addr);

	if (n == NULL) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_NO_NODE};
	}
	if (!n->configured) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_UNCONFIGURED};
	}
	return (struct gw_req_result){CTAG_STATUS_OK, NULL};
}

static void refresh_node(struct gw_core *g, uint16_t addr)
{
	(void)gw_unseg_send(g, addr, CTAG_MESH_OP_CAPS_GET, NULL, 0u);
	(void)gw_unseg_send(g, addr, CTAG_MESH_OP_HEALTH_GET, NULL, 0u);
}

void gw_refresh_inventory(struct gw_core *g)
{
	for (int i = 0; i < GW_NODES; i++) {
		if (g->nodes[i].used && g->nodes[i].configured) {
			refresh_node(g, g->nodes[i].addr);
		}
	}
}

/* ---- Assignments (gateway bookkeeping for the inventory) ---- */

static int assign_find(struct gw_core *g, uint16_t bridge, uint32_t tag_id)
{
	for (int i = 0; i < g->assign_count; i++) {
		if (g->assign[i].bridge == bridge && g->assign[i].tag_id == tag_id) {
			return i;
		}
	}
	return -1;
}

static void assign_remove_at(struct gw_core *g, int i)
{
	g->assign[i] = g->assign[g->assign_count - 1u];
	g->assign_count--;
}

static void assign_store(struct gw_core *g)
{
	if (g->be->store_assignments != NULL) {
		g->be->store_assignments(g->be->ctx, g->assign, g->assign_count);
	}
}

static void assign_apply(struct gw_core *g, const struct gw_op *op)
{
	int i = assign_find(g, op->bridge, op->tag_id);

	if (op->kind == GW_OP_ASSIGN) {
		if (i < 0 && g->assign_count < CONFIG_CTAG_GW_ASSIGN_MAX) {
			i = g->assign_count++;
			g->assign[i].bridge = op->bridge;
			g->assign[i].tag_id = op->tag_id;
		}
		if (i >= 0) {
			g->assign[i].epoch = op->epoch;
		}
	} else if (i >= 0 && g->assign[i].epoch <= op->epoch) {
		assign_remove_at(g, i);
	}
	assign_store(g);
}

/* ---- Provisioning ---- */

static void prov_event(struct gw_core *g, uint64_t op_id, const uint8_t *uuid, uint16_t addr,
		       uint8_t elements, uint8_t status)
{
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, GW_UUID_LEN),
		GW_F_UINT(CTAG_CBOR_KEY_ADDR, addr),
		GW_F_UINT(CTAG_CBOR_KEY_ELEMENTS, elements),
		GW_F_UINT(CTAG_CBOR_KEY_STATUS, status),
	};

	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_PROVISIONED, f, 5u, true);
}

struct gw_req_result gw_provision(struct gw_core *g, uint64_t op_id, const uint8_t *uuid,
				  const char *name, size_t name_len)
{
	struct gw_prov *p = &g->prov;
	struct gw_node *known;
	int err;

	if (p->active || g->cfg.kind != GW_CFGOP_NONE) {
		return (struct gw_req_result){CTAG_STATUS_PROVISIONING_ACTIVE, NULL};
	}
	known = node_by_uuid(g, uuid);
	if (known != NULL) {
		/* Already in the CDB: report where it lives (sim/gateway.py). */
		prov_event(g, op_id, uuid, known->addr, known->elements, CTAG_STATUS_OK);
		return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
	}
	if (gw_node_count(g) >= CTAG_MAX_BRIDGES) {
		return (struct gw_req_result){CTAG_STATUS_NO_RESOURCES, T_FULL};
	}
	err = g->be->provision(g->be->ctx, uuid);
	if (gw_is_retryable(err) || err == -EALREADY) {
		return (struct gw_req_result){CTAG_STATUS_PROVISIONING_ACTIVE, NULL};
	}
	if (err != 0) {
		return (struct gw_req_result){CTAG_STATUS_INTERNAL, T_PROV_START};
	}
	memset(p, 0, sizeof(*p));
	p->active = true;
	p->op_id = op_id;
	memcpy(p->uuid, uuid, GW_UUID_LEN);
	if (name != NULL) {
		p->name_len = (uint8_t)name_fit(name, name_len);
		memcpy(p->name, name, p->name_len);
	}
	p->deadline = g->now + CONFIG_CTAG_GW_PROVISION_TIMEOUT_MS;
	g->c.provisions++;
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

static void prov_finish(struct gw_core *g, uint8_t status)
{
	struct gw_prov *p = &g->prov;

	p->active = false;
	if (p->added) {
		status = CTAG_STATUS_OK;
	}
	prov_event(g, p->op_id, p->uuid, p->added ? p->addr : 0u, p->added ? p->elements : 0u,
		   status);
}

void gw_core_prov_link_open(struct gw_core *g, int64_t now)
{
	g->now = now;
	if (g->prov.active) {
		g->prov.link_open = true;
	}
}

void gw_core_prov_added(struct gw_core *g, const uint8_t uuid[GW_UUID_LEN], uint16_t addr,
			uint8_t elements, int64_t now)
{
	struct gw_prov *p = &g->prov;
	struct gw_node *n;

	g->now = now;
	n = node_add(g, addr, uuid, elements, false);
	if (!p->active || memcmp(p->uuid, uuid, GW_UUID_LEN) != 0) {
		return; /* in the CDB and listed, but nobody is waiting for it */
	}
	p->added = true;
	p->addr = addr;
	p->elements = elements;
	if (n != NULL && p->name_len > 0u) {
		node_name(n, p->name, p->name_len);
		if (g->be->store_name != NULL) {
			g->be->store_name(g->be->ctx, addr, n->name, n->name_len);
		}
	}
	gw_serial_pump(g);
}

void gw_core_prov_closed(struct gw_core *g, int64_t now)
{
	g->now = now;
	if (g->prov.active) {
		/* The link never opened: nobody answered with that UUID. */
		prov_finish(g, g->prov.link_open ? CTAG_STATUS_TIMEOUT : CTAG_STATUS_NOT_FOUND);
	}
	gw_serial_pump(g);
}

/* ---- Configuration and removal (configuration client) ---- */

static uint8_t cfg_arg(const struct gw_cfgop *c)
{
	switch (c->step) {
	case GW_CFG_RELAY:
		return c->relay ? 1u : 0u;
	case GW_CFG_TTL:
		return c->ttl;
	default:
		return 0u;
	}
}

static void cfg_issue(struct gw_core *g)
{
	struct gw_cfgop *c = &g->cfg;
	int err;

	c->attempts++;
	c->waiting = false;
	c->retry_at = 0;
	if (c->step == GW_CFG_APP_KEY_ADD) {
		/* Segmented (19 parameter bytes): through the lane. */
		c->lane_busy = true;
		gw_lane_request(g, GW_REQ_CFG);
		return;
	}
	err = g->be->mesh_cfg(g->be->ctx, c->addr, c->step, cfg_arg(c), 0u);
	if (gw_is_retryable(err)) {
		c->attempts--;
		c->retry_at = g->now + GW_RETRY_MS;
		g->c.mesh_busy++;
		return;
	}
	/* A refused send is retried like a lost reply. */
	c->waiting = true;
	c->deadline = err == 0 ? g->now + CONFIG_CTAG_GW_REPLY_TIMEOUT_MS : g->now + GW_RETRY_MS;
}

static void cfg_start(struct gw_core *g, uint8_t kind, uint64_t op_id, uint16_t addr, bool relay,
		      uint8_t ttl)
{
	struct gw_cfgop *c = &g->cfg;
	bool lane_busy = c->lane_busy;

	memset(c, 0, sizeof(*c));
	c->lane_busy = lane_busy;
	c->kind = kind;
	c->op_id = op_id;
	c->addr = addr;
	c->relay = relay;
	c->ttl = ttl;
	c->step = kind == GW_CFGOP_REMOVE ? GW_CFG_RESET : GW_CFG_APP_KEY_ADD;
	cfg_issue(g);
}

static void remove_node(struct gw_core *g, uint16_t addr)
{
	struct gw_node *n = gw_node_get(g, addr);
	bool changed = false;

	if (g->be->node_delete != NULL) {
		g->be->node_delete(g->be->ctx, addr);
	}
	if (n != NULL) {
		n->used = false;
	}
	for (int i = g->assign_count - 1; i >= 0; i--) {
		if (g->assign[i].bridge == addr) {
			assign_remove_at(g, i);
			changed = true;
		}
	}
	if (changed) {
		assign_store(g);
	}
}

static void cfg_done(struct gw_core *g, uint8_t status)
{
	struct gw_cfgop *c = &g->cfg;
	uint8_t kind = c->kind;
	uint16_t addr = c->addr;
	struct ctag_cbor_field f[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, c->op_id),
		GW_F_UINT(CTAG_CBOR_KEY_ADDR, addr),
		GW_F_UINT(CTAG_CBOR_KEY_STATUS, status),
	};

	c->kind = GW_CFGOP_NONE;
	c->waiting = false;
	c->retry_at = 0;
	if (c->lane_busy && gw_lane_cancel(g, GW_REQ_CFG)) {
		c->lane_busy = false;
	}
	if (kind == GW_CFGOP_REMOVE) {
		/* The CDB entry goes whether or not the node answered its reset. */
		remove_node(g, addr);
		f[2] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_NODE_REMOVED, f, 3u, true);
		return;
	}
	if (status == CTAG_STATUS_OK) {
		struct gw_node *n = gw_node_get(g, addr);

		if (n != NULL) {
			n->configured = true;
			n->last_seen = g->now;
		}
		if (g->be->node_configured != NULL) {
			g->be->node_configured(g->be->ctx, addr);
		}
	}
	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_NODE_CONFIGURED, f, 3u, true);
	if (status == CTAG_STATUS_OK) {
		refresh_node(g, addr);
	}
}

static void cfg_next(struct gw_core *g)
{
	struct gw_cfgop *c = &g->cfg;

	if (c->kind == GW_CFGOP_REMOVE || c->step == GW_CFG_NET_TX) {
		cfg_done(g, CTAG_STATUS_OK);
		return;
	}
	c->step++;
	c->attempts = 0u;
	cfg_issue(g);
}

void gw_cfg_lane_go(struct gw_core *g)
{
	struct gw_cfgop *c = &g->cfg;

	if (c->kind == GW_CFGOP_NONE) {
		gw_lane_issued(g, -EINVAL);
		return;
	}
	gw_lane_issued(g, g->be->mesh_cfg(g->be->ctx, c->addr, GW_CFG_APP_KEY_ADD, 0u, g->lane.tag));
}

void gw_cfg_lane_done(struct gw_core *g, bool ok)
{
	struct gw_cfgop *c = &g->cfg;

	c->lane_busy = false;
	if (c->kind == GW_CFGOP_NONE || c->step != GW_CFG_APP_KEY_ADD) {
		return; /* the reply came first, or the operation ended */
	}
	if (!ok) {
		cfg_done(g, CTAG_STATUS_TIMEOUT);
		return;
	}
	c->waiting = true;
	c->deadline = g->now + CONFIG_CTAG_GW_REPLY_TIMEOUT_MS;
}

static uint8_t cfg_status_map(uint8_t st)
{
	switch (st) {
	case CFG_INVALID_MODEL:
		return CTAG_STATUS_UNSUPPORTED;
	case CFG_INSUFFICIENT_RESOURCES:
		return CTAG_STATUS_NO_RESOURCES;
	default:
		return CTAG_STATUS_INTERNAL;
	}
}

void gw_core_cfg_status(struct gw_core *g, uint16_t addr, uint8_t step, uint8_t status,
			uint8_t value, int64_t now)
{
	struct gw_cfgop *c = &g->cfg;

	g->now = now;
	if (c->kind == GW_CFGOP_NONE || addr != c->addr || step != c->step) {
		return;
	}
	switch (step) {
	case GW_CFG_APP_KEY_ADD:
	case GW_CFG_BIND_LAYOUT:
	case GW_CFG_BIND_MGMT:
		if (status != 0u) {
			cfg_done(g, cfg_status_map(status));
			goto out;
		}
		break;
	case GW_CFG_RELAY:
		if (c->relay && value != 1u) {
			cfg_done(g, value == RELAY_NOT_SUPPORTED ? CTAG_STATUS_UNSUPPORTED
								 : CTAG_STATUS_INTERNAL);
			goto out;
		}
		break;
	case GW_CFG_TTL:
		if (value != c->ttl) {
			cfg_done(g, CTAG_STATUS_INTERNAL);
			goto out;
		}
		break;
	default: /* NET_TX, RESET */
		break;
	}
	{
		struct gw_node *n = gw_node_get(g, addr);

		if (n != NULL) {
			n->last_seen = now;
		}
	}
	cfg_next(g);
out:
	gw_serial_pump(g);
}

struct gw_req_result gw_configure(struct gw_core *g, uint64_t op_id, uint16_t addr, bool relay,
				  uint32_t ttl)
{
	if (gw_node_get(g, addr) == NULL) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_NO_NODE};
	}
	if (g->prov.active || g->cfg.kind != GW_CFGOP_NONE || g->cfg.lane_busy) {
		return (struct gw_req_result){CTAG_STATUS_PROVISIONING_ACTIVE, NULL};
	}
	if (ttl == 1u || ttl > 127u) {
		return (struct gw_req_result){CTAG_STATUS_INVALID, T_TTL};
	}
	cfg_start(g, GW_CFGOP_CONFIGURE, op_id, addr, relay, (uint8_t)ttl);
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

struct gw_req_result gw_remove(struct gw_core *g, uint64_t op_id, uint16_t addr)
{
	if (gw_node_get(g, addr) == NULL) {
		return (struct gw_req_result){CTAG_STATUS_NOT_FOUND, T_NO_NODE};
	}
	if (g->prov.active || g->cfg.kind != GW_CFGOP_NONE) {
		return (struct gw_req_result){CTAG_STATUS_PROVISIONING_ACTIVE, NULL};
	}
	cfg_start(g, GW_CFGOP_REMOVE, op_id, addr, false, 0u);
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

struct gw_req_result gw_identify(struct gw_core *g, uint16_t addr)
{
	struct gw_req_result r = gw_node_usable(g, addr);
	uint8_t p[CTAG_MESH_IDENTIFY_LEN];
	struct ctag_mesh_identify m = {.seconds = GW_IDENTIFY_S};

	if (r.status != CTAG_STATUS_OK) {
		return r;
	}
	(void)ctag_mesh_identify_pack(&m, p, sizeof(p));
	(void)gw_unseg_send(g, addr, CTAG_MESH_OP_IDENTIFY, p, sizeof(p));
	return r;
}

/* ---- Assignments and tag commands (MGMT_CLI) ---- */

static void op_send(struct gw_core *g, uint8_t slot)
{
	struct gw_op *op = &g->ops[slot];

	op->sends++;
	op->waiting = false;
	if (op->kind == GW_OP_UNASSIGN) {
		struct ctag_mesh_assign_del m = {.tag_id = op->tag_id, .epoch = op->epoch};
		uint8_t p[CTAG_MESH_ASSIGN_DEL_LEN];

		(void)ctag_mesh_assign_del_pack(&m, p, sizeof(p));
		(void)gw_unseg_send(g, op->bridge, CTAG_MESH_OP_ASSIGN_DEL, p, sizeof(p));
		op->waiting = true;
		op->deadline = g->now + CONFIG_CTAG_GW_REPLY_TIMEOUT_MS;
		return;
	}
	op->lane_busy = true;
	gw_lane_request(g, (uint8_t)(GW_REQ_OP0 + slot));
}

static void assign_done(struct gw_core *g, struct gw_op *op, uint8_t status)
{
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op->op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, op->bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, op->tag_id),
		GW_F_UINT(CTAG_CBOR_KEY_EPOCH, op->epoch),
		GW_F_UINT(CTAG_CBOR_KEY_STATUS, status),
	};

	if (status == CTAG_STATUS_OK) {
		assign_apply(g, op);
	}
	op->kind = GW_OP_NONE;
	op->waiting = false;
	if (op->lane_busy && gw_lane_cancel(g, (uint8_t)(GW_REQ_OP0 + (op - g->ops)))) {
		op->lane_busy = false;
	}
	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, f, 5u, true);
}

struct gw_req_result gw_mesh_op(struct gw_core *g, uint8_t kind, uint64_t op_id, uint16_t bridge,
				uint32_t tag_id, uint32_t epoch, const uint8_t *key, uint8_t cmd)
{
	struct gw_req_result r = gw_node_usable(g, bridge);
	struct gw_op *op = NULL;
	uint8_t slot = 0u;

	if (r.status != CTAG_STATUS_OK) {
		return r;
	}
	for (uint8_t i = 0; i < CONFIG_CTAG_GW_MESH_OPS; i++) {
		if (g->ops[i].kind == GW_OP_NONE && !g->ops[i].lane_busy) {
			op = &g->ops[i];
			slot = i;
			break;
		}
	}
	if (op == NULL) {
		g->c.busy++;
		return (struct gw_req_result){CTAG_STATUS_BUSY, NULL};
	}
	memset(op, 0, sizeof(*op));
	op->kind = kind;
	op->op_id = op_id;
	op->bridge = bridge;
	op->tag_id = tag_id;
	op->epoch = epoch;
	op->cmd = cmd;
	if (key != NULL) {
		memcpy(op->key, key, sizeof(op->key));
	}
	op_send(g, slot);
	return (struct gw_req_result){CTAG_STATUS_ACCEPTED, NULL};
}

void gw_op_lane_go(struct gw_core *g, uint8_t slot)
{
	struct gw_op *op = &g->ops[slot];
	uint8_t p[CTAG_MESH_ASSIGN_SET_LEN > CTAG_MESH_TAG_CMD_LEN ? CTAG_MESH_ASSIGN_SET_LEN
								   : CTAG_MESH_TAG_CMD_LEN];
	int n = -EINVAL;
	uint8_t code = CTAG_MESH_OP_ASSIGN_SET;

	if (op->kind == GW_OP_ASSIGN) {
		struct ctag_mesh_assign_set m = {.tag_id = op->tag_id, .epoch = op->epoch, .flags = 1u};

		memcpy(m.key, op->key, sizeof(m.key));
		n = ctag_mesh_assign_set_pack(&m, p, sizeof(p));
	} else if (op->kind == GW_OP_TAG_CMD) {
		struct ctag_mesh_tag_cmd m = {
			.update_id = op->op_id, .tag_id = op->tag_id, .epoch = op->epoch, .cmd = op->cmd};

		code = CTAG_MESH_OP_TAG_CMD;
		n = ctag_mesh_tag_cmd_pack(&m, p, sizeof(p));
	}
	if (n < 0) {
		gw_lane_issued(g, -EINVAL);
		return;
	}
	gw_lane_issued(g, g->be->mesh_send(g->be->ctx, op->bridge, code, p, (size_t)n, g->lane.tag));
}

void gw_op_lane_done(struct gw_core *g, uint8_t slot, bool ok)
{
	struct gw_op *op = &g->ops[slot];

	op->lane_busy = false;
	if (op->kind == GW_OP_TAG_CMD) {
		if (!ok) {
			/* The outcome would have come as DELIVERY_RESULT (update_id = op_id). */
			gw_result_event(g, op->op_id, op->bridge, op->tag_id, op->epoch, 0u,
					CTAG_STATUS_TIMEOUT, 0u);
		}
		op->kind = GW_OP_NONE;
		return;
	}
	if (op->kind != GW_OP_ASSIGN || op->waiting) {
		return; /* the ASSIGN_STATUS came first, or the operation ended */
	}
	if (!ok) {
		assign_done(g, op, CTAG_STATUS_TIMEOUT);
		return;
	}
	op->waiting = true;
	op->deadline = g->now + CONFIG_CTAG_GW_REPLY_TIMEOUT_MS;
}

static void assign_status(struct gw_core *g, uint16_t src, const struct ctag_mesh_assign_status *st)
{
	for (int i = 0; i < CONFIG_CTAG_GW_MESH_OPS; i++) {
		struct gw_op *op = &g->ops[i];

		if ((op->kind == GW_OP_ASSIGN || op->kind == GW_OP_UNASSIGN) && op->bridge == src &&
		    op->tag_id == st->tag_id && op->epoch == st->epoch && op->sends > 0u) {
			assign_done(g, op, st->status);
			return;
		}
	}
	g->c.unexpected_mesh++;
}

/* ---- Unprovisioned beacons (SCAN_UNPROV) ---- */

void gw_scan(struct gw_core *g, uint32_t duration_s, const uint8_t *filter, size_t filter_len)
{
	g->scan_until = duration_s > 0u ? g->now + (int64_t)duration_s * 1000 : 0;
	g->scan_filter_len = (uint8_t)(filter_len > GW_UUID_LEN ? GW_UUID_LEN : filter_len);
	if (g->scan_filter_len > 0u) {
		memcpy(g->scan_filter, filter, g->scan_filter_len);
	}
	g->scan_seen_count = 0u;
}

void gw_core_beacon(struct gw_core *g, const uint8_t uuid[GW_UUID_LEN], uint16_t oob, int8_t rssi,
		    int64_t now)
{
	g->now = now;
	g->c.beacons++;
	if (now >= g->scan_until || memcmp(uuid, g->scan_filter, g->scan_filter_len) != 0) {
		return;
	}
	for (uint8_t i = 0; i < g->scan_seen_count; i++) {
		if (memcmp(g->scan_seen[i], uuid, GW_UUID_LEN) == 0) {
			return; /* each device once per scan */
		}
	}
	if (g->scan_seen_count == GW_SCAN_SEEN) {
		return;
	}
	memcpy(g->scan_seen[g->scan_seen_count++], uuid, GW_UUID_LEN);
	{
		struct ctag_cbor_field f[3] = {
			GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, GW_UUID_LEN),
			GW_F_INT(CTAG_CBOR_KEY_RSSI, rssi),
			GW_F_UINT(CTAG_CBOR_KEY_OOB, oob),
		};

		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_UNPROV_BEACON, f, 3u, false);
	}
	gw_serial_pump(g);
}

/* ---- Bridge information ---- */

static size_t caps_fields(const struct gw_node *n, struct ctag_cbor_field *f)
{
	uint64_t flash = (uint64_t)n->caps.flash_mib << 20;

	f[0] = GW_F_UINT(CTAG_CBOR_KEY_FLAGS, n->caps.flags);
	f[1] = GW_F_UINT(CTAG_CBOR_KEY_PROTO, n->caps.proto);
	f[2] = GW_F_UINT(CTAG_CBOR_KEY_BOARD, n->caps.board);
	f[3] = GW_F_UINT(CTAG_CBOR_KEY_FLASH_SIZE, flash > UINT32_MAX ? UINT32_MAX : flash);
	f[4] = GW_F_UINT(CTAG_CBOR_KEY_MAX_TAGS, n->caps.max_tags);
	/* The bridge's own count (CAPS_STATUS.assigned), beside the gateway's
	 * assignment list: the companion reports this one as the bridge's use. */
	f[5] = GW_F_UINT(CTAG_CBOR_KEY_ASSIGNED_COUNT, n->caps.assigned);
	return GW_CAPS_FIELDS;
}

static size_t health_counters(const struct gw_node *n, struct ctag_cbor_counter *c)
{
	const struct ctag_mesh_health_status *h = &n->health;
	const struct ctag_cbor_counter all[8] = {
		CTAG_CBOR_COUNTER("uptime_s", h->uptime_s),
		CTAG_CBOR_COUNTER("sessions_ok", h->sessions_ok),
		CTAG_CBOR_COUNTER("sessions_fail", h->sessions_fail),
		CTAG_CBOR_COUNTER("suspend_count", h->suspend_count),
		CTAG_CBOR_COUNTER("suspend_max_ms", h->suspend_max_ms),
		CTAG_CBOR_COUNTER("resume_fail", h->resume_fail),
		CTAG_CBOR_COUNTER("queue_depth", h->queue_depth),
		CTAG_CBOR_COUNTER("last_status", h->last_status),
	};

	if (!n->health_valid) {
		return 0u;
	}
	memcpy(c, all, sizeof(all));
	return 8u;
}

static void fw_string(const struct gw_node *n, char out[GW_FW_STR])
{
	const uint8_t v[3] = {n->caps.fw_major, n->caps.fw_minor, n->caps.fw_patch};
	size_t o = 0u;

	for (int i = 0; i < 3; i++) {
		uint8_t x = v[i];
		char d[3];
		int k = 0;

		do {
			d[k++] = (char)('0' + x % 10u);
			x /= 10u;
		} while (x != 0u);
		while (k > 0) {
			out[o++] = d[--k];
		}
		if (i < 2) {
			out[o++] = '.';
		}
	}
	out[o] = '\0';
}

/* The node's assignments as maps in the scratch area, from index *used on. */
static size_t assigned_maps(struct gw_core *g, uint16_t addr, size_t *used)
{
	struct gw_scratch *s = &g->scratch;
	size_t first = *used;

	for (int i = 0; i < g->assign_count && *used < CONFIG_CTAG_GW_ASSIGN_MAX; i++) {
		if (g->assign[i].bridge != addr) {
			continue;
		}
		s->as_f[*used][0] = GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, g->assign[i].tag_id);
		s->as_f[*used][1] = GW_F_UINT(CTAG_CBOR_KEY_EPOCH, g->assign[i].epoch);
		s->as_maps[*used].fields = s->as_f[*used];
		s->as_maps[*used].count = 2u;
		(*used)++;
	}
	return *used - first;
}

static void bridge_info_event(struct gw_core *g, struct gw_node *n)
{
	struct gw_scratch *s = &g->scratch;
	struct ctag_cbor_field f[6];
	size_t used = 0u;
	size_t na = assigned_maps(g, n->addr, &used);

	if (!n->caps_valid) {
		return;
	}
	fw_string(n, s->fw[0]);
	f[0] = GW_F_UINT(CTAG_CBOR_KEY_ADDR, n->addr);
	f[1] = GW_F_TSTR(CTAG_CBOR_KEY_FW, s->fw[0], strlen(s->fw[0]));
	f[2] = GW_F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, n->caps.fontpack_id, sizeof(n->caps.fontpack_id));
	f[3] = GW_F_MAP(CTAG_CBOR_KEY_CAPS, s->caps[0], caps_fields(n, s->caps[0]));
	f[4].key = CTAG_CBOR_KEY_ASSIGNED;
	f[4].kind = CTAG_CBOR_MAPS;
	f[4].v.maps.items = s->as_maps;
	f[4].v.maps.count = na;
	f[5].key = CTAG_CBOR_KEY_COUNTERS;
	f[5].kind = CTAG_CBOR_COUNTERS;
	f[5].v.counters.items = s->health[0];
	f[5].v.counters.count = health_counters(n, s->health[0]);
	(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_BRIDGE_INFO, f, 6u, false);
}

/* Node indexes sorted by address. */
static size_t sorted_nodes(const struct gw_core *g, uint8_t idx[GW_NODES])
{
	size_t n = 0u;

	for (uint8_t i = 0; i < GW_NODES; i++) {
		if (!g->nodes[i].used) {
			continue;
		}
		size_t j = n++;

		while (j > 0u && g->nodes[idx[j - 1u]].addr > g->nodes[i].addr) {
			idx[j] = idx[j - 1u];
			j--;
		}
		idx[j] = i;
	}
	return n;
}

static uint32_t seen_s(const struct gw_core *g, const struct gw_node *n)
{
	return g->now > n->last_seen ? (uint32_t)((g->now - n->last_seen) / 1000) : 0u;
}

int gw_encode_nodes(struct gw_core *g, uint8_t *buf, size_t size)
{
	struct gw_scratch *s = &g->scratch;
	uint8_t idx[GW_NODES];
	size_t count = sorted_nodes(g, idx);
	struct ctag_cbor_field f[2];

	for (size_t k = 0; k < count; k++) {
		const struct gw_node *n = &g->nodes[idx[k]];
		struct ctag_cbor_field *it = s->item[k];

		it[0] = GW_F_UINT(CTAG_CBOR_KEY_ADDR, n->addr);
		it[1] = GW_F_BSTR(CTAG_CBOR_KEY_UUID, n->uuid, GW_UUID_LEN);
		it[2] = GW_F_UINT(CTAG_CBOR_KEY_ELEMENTS, n->elements);
		it[3] = GW_F_TSTR(CTAG_CBOR_KEY_NAME, n->name, n->name_len);
		it[4] = GW_F_BOOL(CTAG_CBOR_KEY_CONFIGURED, n->configured);
		it[5] = GW_F_UINT(CTAG_CBOR_KEY_LAST_SEEN_S, seen_s(g, n));
		s->items[k].fields = it;
		s->items[k].count = 6u;
	}
	f[0] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	f[1].key = CTAG_CBOR_KEY_NODES;
	f[1].kind = CTAG_CBOR_MAPS;
	f[1].v.maps.items = s->items;
	f[1].v.maps.count = count;
	return ctag_cbor_encode(f, 2u, buf, size);
}

int gw_encode_inventory(struct gw_core *g, uint8_t *buf, size_t size)
{
	struct gw_scratch *s = &g->scratch;
	uint8_t idx[GW_NODES];
	size_t count = sorted_nodes(g, idx);
	size_t used = 0u;
	struct ctag_cbor_field f[2];

	for (size_t k = 0; k < count; k++) {
		const struct gw_node *n = &g->nodes[idx[k]];
		struct ctag_cbor_field *it = s->item[k];
		size_t first = used;
		size_t na = assigned_maps(g, n->addr, &used);
		size_t m = 0u;

		it[m++] = GW_F_UINT(CTAG_CBOR_KEY_ADDR, n->addr);
		it[m++] = GW_F_BSTR(CTAG_CBOR_KEY_UUID, n->uuid, GW_UUID_LEN);
		it[m++] = GW_F_TSTR(CTAG_CBOR_KEY_NAME, n->name, n->name_len);
		it[m++] = GW_F_BOOL(CTAG_CBOR_KEY_CONFIGURED, n->configured);
		it[m++] = GW_F_UINT(CTAG_CBOR_KEY_LAST_SEEN_S, seen_s(g, n));
		it[m].key = CTAG_CBOR_KEY_ASSIGNED;
		it[m].kind = CTAG_CBOR_MAPS;
		it[m].v.maps.items = &s->as_maps[first];
		it[m++].v.maps.count = na;
		if (n->caps_valid) {
			uint64_t flash = (uint64_t)n->caps.flash_mib << 20;

			fw_string(n, s->fw[k]);
			it[m++] = GW_F_TSTR(CTAG_CBOR_KEY_FW, s->fw[k], strlen(s->fw[k]));
			it[m++] = GW_F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, n->caps.fontpack_id,
					    sizeof(n->caps.fontpack_id));
			it[m++] = GW_F_MAP(CTAG_CBOR_KEY_CAPS, s->caps[k], caps_fields(n, s->caps[k]));
			it[m++] = GW_F_UINT(CTAG_CBOR_KEY_BOARD, n->caps.board);
			it[m++] = GW_F_UINT(CTAG_CBOR_KEY_FLASH_SIZE,
					    flash > UINT32_MAX ? UINT32_MAX : flash);
		}
		if (n->health_valid) {
			it[m].key = CTAG_CBOR_KEY_COUNTERS;
			it[m].kind = CTAG_CBOR_COUNTERS;
			it[m].v.counters.items = s->health[k];
			it[m++].v.counters.count = health_counters(n, s->health[k]);
		}
		s->items[k].fields = it;
		s->items[k].count = m;
	}
	f[0] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	f[1].key = CTAG_CBOR_KEY_ITEMS;
	f[1].kind = CTAG_CBOR_MAPS;
	f[1].v.maps.items = s->items;
	f[1].v.maps.count = count;
	return ctag_cbor_encode(f, 2u, buf, size);
}

/* ---- TAG_SEEN (rate-limited per bridge and tag) ---- */

static void tag_seen(struct gw_core *g, uint16_t src, const struct ctag_mesh_tag_seen *ts)
{
	struct gw_tag_seen *e = NULL;

	for (int i = 0; i < GW_TAG_SEEN_SLOTS; i++) {
		struct gw_tag_seen *c = &g->tag_seen[i];

		if (c->bridge == src && c->tag_id == ts->tag_id) {
			e = c;
			break;
		}
	}
	if (e != NULL && g->now - e->last < CONFIG_CTAG_GW_TAG_SEEN_INTERVAL_MS) {
		g->c.tag_seen_limited++;
		return;
	}
	if (e == NULL) {
		e = &g->tag_seen[0];
		for (int i = 1; i < GW_TAG_SEEN_SLOTS; i++) {
			if (g->tag_seen[i].last < e->last) {
				e = &g->tag_seen[i];
			}
		}
		e->bridge = src;
		e->tag_id = ts->tag_id;
	}
	e->last = g->now;
	{
		struct ctag_cbor_field f[5] = {
			GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, src),
			GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, ts->tag_id),
			GW_F_INT(CTAG_CBOR_KEY_RSSI, ts->rssi),
			GW_F_UINT(CTAG_CBOR_KEY_BATTERY_MV, ts->battery_mv),
			GW_F_UINT(CTAG_CBOR_KEY_FLAGS, ts->flags),
		};

		(void)gw_emit(g, CTAG_SERIAL_MSG_EVT_TAG_SEEN, f, 5u, false);
	}
}

/* ---- Mesh input for MGMT_CLI ---- */

void gw_nodes_mesh_rx(struct gw_core *g, uint16_t src, uint8_t op, const uint8_t *p, size_t len)
{
	struct gw_node *n = gw_node_get(g, src);

	switch (op) {
	case CTAG_MESH_OP_ASSIGN_STATUS: {
		struct ctag_mesh_assign_status st;

		if (ctag_mesh_assign_status_unpack(&st, p, len) == 0) {
			assign_status(g, src, &st);
			return;
		}
		break;
	}
	case CTAG_MESH_OP_CAPS_STATUS:
		if (n != NULL && ctag_mesh_caps_status_unpack(&n->caps, p, len) == 0) {
			n->caps_valid = true;
			bridge_info_event(g, n);
			return;
		}
		break;
	case CTAG_MESH_OP_HEALTH_STATUS:
		if (n != NULL && ctag_mesh_health_status_unpack(&n->health, p, len) == 0) {
			n->health_valid = true;
			bridge_info_event(g, n);
			return;
		}
		break;
	case CTAG_MESH_OP_TAG_SEEN: {
		struct ctag_mesh_tag_seen ts;

		if (ctag_mesh_tag_seen_unpack(&ts, p, len) == 0) {
			tag_seen(g, src, &ts);
			return;
		}
		break;
	}
	default:
		break;
	}
	g->c.unexpected_mesh++;
}

/* ---- Timers ---- */

void gw_nodes_timers(struct gw_core *g)
{
	struct gw_cfgop *c = &g->cfg;

	if (g->prov.active && g->now >= g->prov.deadline) {
		prov_finish(g, CTAG_STATUS_TIMEOUT);
	}
	if (c->kind != GW_CFGOP_NONE && c->retry_at != 0 && g->now >= c->retry_at) {
		cfg_issue(g);
	} else if (c->kind != GW_CFGOP_NONE && c->waiting && g->now >= c->deadline) {
		if (c->attempts < GW_CFG_ATTEMPTS) {
			cfg_issue(g); /* the same step again */
		} else {
			cfg_done(g, CTAG_STATUS_TIMEOUT);
		}
	}
	for (uint8_t i = 0; i < CONFIG_CTAG_GW_MESH_OPS; i++) {
		struct gw_op *op = &g->ops[i];

		if (op->kind == GW_OP_NONE || !op->waiting || g->now < op->deadline) {
			continue;
		}
		if (op->sends < GW_REPLY_ATTEMPTS) {
			op_send(g, i);
		} else {
			assign_done(g, op, CTAG_STATUS_TIMEOUT);
		}
	}
}

int64_t gw_nodes_deadline(const struct gw_core *g)
{
	const struct gw_cfgop *c = &g->cfg;
	int64_t d = GW_NEVER;

	if (g->prov.active) {
		d = g->prov.deadline;
	}
	if (c->kind != GW_CFGOP_NONE && c->retry_at != 0) {
		d = gw_min_deadline(d, c->retry_at);
	} else if (c->kind != GW_CFGOP_NONE && c->waiting) {
		d = gw_min_deadline(d, c->deadline);
	}
	for (int i = 0; i < CONFIG_CTAG_GW_MESH_OPS; i++) {
		if (g->ops[i].kind != GW_OP_NONE && g->ops[i].waiting) {
			d = gw_min_deadline(d, g->ops[i].deadline);
		}
	}
	return d;
}
