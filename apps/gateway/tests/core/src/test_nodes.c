/* Provisioning, configuration, removal, assignments, tag commands, scanning,
 * TAG_SEEN and the inventory (2, 10). */
#include <string.h>

#include "common.h"

static const uint8_t new_uuid[16] = {0xC0, 0xFF, 0xEE, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};

static void before(void *f)
{
	(void)f;
	core_reset();
	core_session();
}

ZTEST_SUITE(gw_nodes, NULL, NULL, before, NULL, NULL);

static uint16_t provision(uint64_t op_id, const uint8_t *uuid, const char *name)
{
	struct ctag_cbor_field f[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, 16u),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, name, name != NULL ? strlen(name) : 0u),
	};

	return host_send(CTAG_SERIAL_MSG_PROVISION, f, name != NULL ? 3u : 2u, 1u);
}

static uint8_t status_of(uint16_t rid)
{
	host_read();
	zassert_not_null(response(rid));
	return (uint8_t)field_u(response(rid), CTAG_CBOR_KEY_STATUS);
}

static uint16_t configure(uint64_t op_id, uint16_t addr, bool relay, uint32_t ttl)
{
	struct ctag_cbor_field f[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id), GW_F_UINT(CTAG_CBOR_KEY_ADDR, addr),
		GW_F_BOOL(CTAG_CBOR_KEY_RELAY, relay), GW_F_UINT(CTAG_CBOR_KEY_TTL, ttl),
	};

	return host_send(CTAG_SERIAL_MSG_CONFIGURE_NODE, f, 4u, 1u);
}

static uint16_t op_addr(uint8_t type, uint64_t op_id, uint16_t addr)
{
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
				       GW_F_UINT(CTAG_CBOR_KEY_ADDR, addr)};

	return host_send(type, f, 2u, 1u);
}

static uint16_t assign(uint8_t type, uint64_t op_id, uint16_t bridge, uint32_t tag, uint32_t epoch)
{
	static const uint8_t key[16] = {0x11, 0x22};
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id), GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag),  GW_F_UINT(CTAG_CBOR_KEY_EPOCH, epoch),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};

	return host_send(type, f, type == CTAG_SERIAL_MSG_ASSIGN_TAG ? 5u : 4u, 1u);
}

ZTEST(gw_nodes, test_provision_adds_the_node_and_reports_it)
{
	struct ctag_cbor_field f[5] = {{.key = CTAG_CBOR_KEY_OP_ID}, {.key = CTAG_CBOR_KEY_UUID},
				       {.key = CTAG_CBOR_KEY_ADDR},  {.key = CTAG_CBOR_KEY_ELEMENTS},
				       {.key = CTAG_CBOR_KEY_STATUS}};
	const struct frame *e;

	zassert_equal(status_of(provision(11u, new_uuid, "kitchen")), CTAG_STATUS_ACCEPTED);
	zassert_equal(mock.n_provision, 1u);
	zassert_mem_equal(mock.provisioned[0], new_uuid, 16u);
	/* A second provisioning while one runs: transient, not remembered. */
	zassert_equal(status_of(provision(12u, new_uuid, NULL)), CTAG_STATUS_PROVISIONING_ACTIVE);
	gw_core_prov_link_open(&core, now_ms);
	gw_core_prov_added(&core, new_uuid, 0x0004u, 1u, now_ms);
	gw_core_prov_closed(&core, now_ms);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_not_null(e);
	decode(e, f, 5u);
	zassert_equal(f[0].v.u, 11u);
	zassert_mem_equal(f[1].v.str.ptr, new_uuid, 16u);
	zassert_equal(f[2].v.u, 0x0004u);
	zassert_equal(f[3].v.u, 1u);
	zassert_equal(f[4].v.u, CTAG_STATUS_OK);
	zassert_true(field_has(e, CTAG_CBOR_KEY_SEQ));
	zassert_equal(mock.stored_name_len, 7u);
	zassert_mem_equal(mock.stored_name, "kitchen", 7u);
	zassert_equal(counter("nodes"), 3u);
	/* The retry of the refused request now reports the node where it lives. */
	zassert_equal(status_of(provision(12u, new_uuid, NULL)), CTAG_STATUS_ACCEPTED);
	host_read();
	zassert_equal(mock.n_provision, 1u, "known UUID: no new provisioning");
}

ZTEST(gw_nodes, test_provision_of_an_absent_device_fails)
{
	const struct frame *e;

	zassert_equal(status_of(provision(13u, new_uuid, NULL)), CTAG_STATUS_ACCEPTED);
	gw_core_prov_closed(&core, now_ms); /* the link never opened */
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_NOT_FOUND);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_ADDR), 0u);
	/* A link that opened but never completed times out. */
	zassert_equal(status_of(provision(14u, new_uuid, NULL)), CTAG_STATUS_ACCEPTED);
	gw_core_prov_link_open(&core, now_ms);
	advance(CONFIG_CTAG_GW_PROVISION_TIMEOUT_MS);
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_PROVISIONED, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
}

ZTEST(gw_nodes, test_provision_refuses_beyond_max_bridges)
{
	uint8_t u[16] = {0x55};

	for (uint16_t a = 4; a < 4 + CTAG_MAX_BRIDGES - 2; a++) {
		u[1] = (uint8_t)a;
		gw_core_add_node(&core, a, u, 1u, true);
	}
	zassert_equal(status_of(provision(15u, new_uuid, NULL)), CTAG_STATUS_NO_RESOURCES);
	zassert_equal(mock.n_provision, 0u);
}

ZTEST(gw_nodes, test_configure_runs_every_step_in_order)
{
	static const uint8_t order[] = {GW_CFG_APP_KEY_ADD, GW_CFG_BIND_LAYOUT, GW_CFG_BIND_MGMT,
					GW_CFG_RELAY,       GW_CFG_TTL,         GW_CFG_NET_TX};
	const struct frame *e;

	zassert_equal(status_of(configure(21u, BRIDGE_B, true, 5u)), CTAG_STATUS_ACCEPTED);
	zassert_equal(mock.n_cfg, 1u);
	zassert_not_equal(mock.cfg[0].tag, 0u, "AppKey Add is segmented: through the lane");
	for (size_t i = 0; i < ARRAY_SIZE(order); i++) {
		const struct cfg_call *c = &mock.cfg[i];

		zassert_equal(mock.n_cfg, i + 1u);
		zassert_equal(c->step, order[i]);
		zassert_equal(c->addr, BRIDGE_B);
		if (c->step == GW_CFG_RELAY) {
			zassert_equal(c->arg, 1u);
		}
		if (c->step == GW_CFG_TTL) {
			zassert_equal(c->arg, 5u);
		}
		if (c->tag != 0u) {
			end_tag(c->tag, 0);
		}
		gw_core_cfg_status(&core, BRIDGE_B, c->step, 0u,
				   c->step == GW_CFG_RELAY ? 1u : c->step == GW_CFG_TTL ? 5u : 0u,
				   now_ms);
	}
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_NODE_CONFIGURED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_OP_ID), 21u);
	zassert_equal(mock.n_configured, 1u);
	zassert_equal(mock.configured[0], BRIDGE_B);
	zassert_true(core.nodes[1].configured);
	zassert_equal(last_sent(CTAG_MESH_OP_CAPS_GET)->dst, BRIDGE_B, "then its inventory");
	zassert_equal(last_sent(CTAG_MESH_OP_HEALTH_GET)->dst, BRIDGE_B);
}

ZTEST(gw_nodes, test_configure_status_before_the_segment_ack)
{
	zassert_equal(status_of(configure(22u, BRIDGE_B, true, 5u)), CTAG_STATUS_ACCEPTED);
	/* AppKey Status arrives before the lower transport's end callback. */
	gw_core_cfg_status(&core, BRIDGE_B, GW_CFG_APP_KEY_ADD, 0u, 0u, now_ms);
	zassert_equal(mock.n_cfg, 2u);
	zassert_equal(mock.cfg[1].step, GW_CFG_BIND_LAYOUT);
	end_tag(mock.cfg[0].tag, 0); /* late: ignored */
	zassert_equal(mock.n_cfg, 2u);
}

ZTEST(gw_nodes, test_configure_times_out_after_three_attempts)
{
	zassert_equal(status_of(configure(23u, BRIDGE_B, true, 5u)), CTAG_STATUS_ACCEPTED);
	end_tag(mock.cfg[0].tag, 0);
	gw_core_cfg_status(&core, BRIDGE_B, GW_CFG_APP_KEY_ADD, 0u, 0u, now_ms);
	for (int i = 0; i < (int)GW_CFG_ATTEMPTS; i++) {
		zassert_equal(mock.n_cfg, 2u + (size_t)i);
		advance(CONFIG_CTAG_GW_REPLY_TIMEOUT_MS);
	}
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_NODE_CONFIGURED, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
	zassert_false(core.nodes[1].configured);
}

ZTEST(gw_nodes, test_configure_refusals)
{
	zassert_equal(status_of(configure(24u, 0x0042u, true, 5u)), CTAG_STATUS_NOT_FOUND);
	zassert_equal(status_of(configure(25u, BRIDGE_B, true, 1u)), CTAG_STATUS_INVALID);
	zassert_equal(status_of(provision(26u, new_uuid, NULL)), CTAG_STATUS_ACCEPTED);
	zassert_equal(status_of(configure(27u, BRIDGE_B, true, 5u)),
		      CTAG_STATUS_PROVISIONING_ACTIVE);
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_REMOVE_NODE, 28u, BRIDGE_B)),
		      CTAG_STATUS_PROVISIONING_ACTIVE);
}

ZTEST(gw_nodes, test_configure_reports_a_missing_vendor_model)
{
	zassert_equal(status_of(configure(29u, BRIDGE_B, true, 5u)), CTAG_STATUS_ACCEPTED);
	end_tag(mock.cfg[0].tag, 0);
	gw_core_cfg_status(&core, BRIDGE_B, GW_CFG_APP_KEY_ADD, 0u, 0u, now_ms);
	gw_core_cfg_status(&core, BRIDGE_B, GW_CFG_BIND_LAYOUT, 0x02u, 0u, now_ms);
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_NODE_CONFIGURED, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_UNSUPPORTED);
}

ZTEST(gw_nodes, test_remove_resets_and_deletes)
{
	const struct frame *e;

	zassert_equal(status_of(assign(CTAG_SERIAL_MSG_ASSIGN_TAG, 30u, BRIDGE_A, 77u, 2u)),
		      CTAG_STATUS_ACCEPTED);
	end_last(0);
	mesh_assign_status(BRIDGE_A, 77u, 2u, CTAG_STATUS_OK);
	zassert_equal(core.assign_count, 1u);
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_REMOVE_NODE, 31u, BRIDGE_A)),
		      CTAG_STATUS_ACCEPTED);
	zassert_equal(mock.cfg[mock.n_cfg - 1u].step, GW_CFG_RESET);
	gw_core_cfg_status(&core, BRIDGE_A, GW_CFG_RESET, 0u, 0u, now_ms);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_NODE_REMOVED, 0);
	zassert_not_null(e);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_ADDR), BRIDGE_A);
	zassert_equal(mock.n_deleted, 1u);
	zassert_is_null(gw_node_get(&core, BRIDGE_A));
	zassert_equal(core.assign_count, 0u, "its assignments go with it");
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_REMOVE_NODE, 32u, BRIDGE_A)),
		      CTAG_STATUS_NOT_FOUND);
}

ZTEST(gw_nodes, test_remove_of_a_silent_node_still_deletes_it)
{
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_REMOVE_NODE, 33u, BRIDGE_B)),
		      CTAG_STATUS_ACCEPTED);
	for (int i = 0; i < (int)GW_CFG_ATTEMPTS; i++) {
		advance(CONFIG_CTAG_GW_REPLY_TIMEOUT_MS);
	}
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_NODE_REMOVED, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_OK);
	zassert_equal(mock.n_cfg, GW_CFG_ATTEMPTS);
	zassert_is_null(gw_node_get(&core, BRIDGE_B));
}

ZTEST(gw_nodes, test_assign_and_unassign)
{
	struct ctag_mesh_assign_set m;
	const struct frame *e;

	zassert_equal(status_of(assign(CTAG_SERIAL_MSG_ASSIGN_TAG, 40u, BRIDGE_A, 5u, 3u)),
		      CTAG_STATUS_ACCEPTED);
	zassert_equal(ctag_mesh_assign_set_unpack(&m, last_sent(CTAG_MESH_OP_ASSIGN_SET)->data,
						  CTAG_MESH_ASSIGN_SET_LEN),
		      0);
	zassert_equal(m.tag_id, 5u);
	zassert_equal(m.epoch, 3u);
	zassert_equal(m.key[0], 0x11u);
	zassert_equal(m.flags, 1u);
	end_last(0);
	mesh_assign_status(BRIDGE_A, 5u, 3u, CTAG_STATUS_OK);
	host_read();
	e = event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_OP_ID), 40u);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
	zassert_equal(mock.stored_assign_n, 1u);
	zassert_equal(mock.stored_assign[0].epoch, 3u);
	/* UNASSIGN: ASSIGN_DEL is unsegmented. */
	zassert_equal(status_of(assign(CTAG_SERIAL_MSG_UNASSIGN_TAG, 41u, BRIDGE_A, 5u, 3u)),
		      CTAG_STATUS_ACCEPTED);
	zassert_equal(last_sent(CTAG_MESH_OP_ASSIGN_DEL)->tag, 0u);
	mesh_assign_status(BRIDGE_A, 5u, 3u, CTAG_STATUS_OK);
	zassert_equal(mock.stored_assign_n, 0u);
}

ZTEST(gw_nodes, test_assign_retries_then_times_out)
{
	zassert_equal(status_of(assign(CTAG_SERIAL_MSG_ASSIGN_TAG, 42u, BRIDGE_A, 6u, 1u)),
		      CTAG_STATUS_ACCEPTED);
	for (int i = 1; i <= (int)GW_REPLY_ATTEMPTS; i++) {
		zassert_equal(sent_count(CTAG_MESH_OP_ASSIGN_SET), (size_t)i);
		end_last(0);
		advance(CONFIG_CTAG_GW_REPLY_TIMEOUT_MS);
	}
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_ASSIGN_RESULT, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
	zassert_equal(sent_count(CTAG_MESH_OP_ASSIGN_SET), GW_REPLY_ATTEMPTS);
}

ZTEST(gw_nodes, test_tag_command_send_failure_is_a_result)
{
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 4711u), GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 8u),   GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 2u),
		GW_F_UINT(CTAG_CBOR_KEY_CMD, CTAG_TAG_CMD_CLEAR),
	};
	struct ctag_mesh_tag_cmd m;

	zassert_equal(request_status(CTAG_SERIAL_MSG_TAG_COMMAND, f, 5u), CTAG_STATUS_ACCEPTED);
	zassert_equal(ctag_mesh_tag_cmd_unpack(&m, last_sent(CTAG_MESH_OP_TAG_CMD)->data,
					       CTAG_MESH_TAG_CMD_LEN),
		      0);
	zassert_equal(m.update_id, 4711u, "results come back under update_id = op_id");
	zassert_equal(m.cmd, CTAG_TAG_CMD_CLEAR);
	for (int i = 0; i <= (int)GW_SEND_RETRIES; i++) {
		end_last(-ETIMEDOUT);
	}
	host_read();
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_RESULT, 0), CTAG_CBOR_KEY_UPDATE_ID), 4711u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_RESULT, 0), CTAG_CBOR_KEY_STATUS),
		      CTAG_STATUS_TIMEOUT);
}

ZTEST(gw_nodes, test_mesh_ops_need_a_configured_bridge)
{
	zassert_equal(status_of(assign(CTAG_SERIAL_MSG_ASSIGN_TAG, 43u, BRIDGE_B, 6u, 1u)),
		      CTAG_STATUS_NOT_FOUND);
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_IDENTIFY_NODE, 44u, BRIDGE_B)),
		      CTAG_STATUS_NOT_FOUND);
	zassert_equal(status_of(op_addr(CTAG_SERIAL_MSG_IDENTIFY_NODE, 45u, BRIDGE_A)),
		      CTAG_STATUS_OK);
	zassert_equal(last_sent(CTAG_MESH_OP_IDENTIFY)->data[0], GW_IDENTIFY_S);
}

ZTEST(gw_nodes, test_scan_reports_each_matching_device_once)
{
	static const uint8_t prefix[2] = {0xC0, 0xFF};
	uint8_t other[16] = {0xD0};
	struct ctag_cbor_field f[2] = {GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, 10u),
				       GW_F_BSTR(CTAG_CBOR_KEY_UUID_FILTER, prefix, 2u)};

	gw_core_beacon(&core, new_uuid, 0x0020u, -50, now_ms); /* not scanning yet */
	zassert_equal(request_status(CTAG_SERIAL_MSG_SCAN_UNPROV, f, 2u), CTAG_STATUS_OK);
	gw_core_beacon(&core, new_uuid, 0x0020u, -50, now_ms);
	gw_core_beacon(&core, new_uuid, 0x0020u, -40, now_ms); /* again */
	gw_core_beacon(&core, other, 0u, -60, now_ms);         /* filtered out */
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_UNPROV_BEACON), 1u);
	zassert_equal((int64_t)field_u(event(CTAG_SERIAL_MSG_EVT_UNPROV_BEACON, 0), CTAG_CBOR_KEY_RSSI),
		      -50);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_UNPROV_BEACON, 0), CTAG_CBOR_KEY_OOB),
		      0x0020u);
	now_ms += 10000;
	gw_core_beacon(&core, (const uint8_t[16]){0xC0, 0xFF, 1}, 0u, -50, now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_UNPROV_BEACON), 0u, "the scan ended");
}

ZTEST(gw_nodes, test_tag_seen_is_rate_limited)
{
	struct ctag_mesh_tag_seen ts = {.tag_id = 0x77u, .rssi = -61, .battery_mv = 2800u};
	uint8_t p[CTAG_MESH_TAG_SEEN_LEN];

	(void)ctag_mesh_tag_seen_pack(&ts, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_TAG_SEEN, p, sizeof(p), now_ms);
	now_ms += CONFIG_CTAG_GW_TAG_SEEN_INTERVAL_MS - 1;
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_TAG_SEEN, p, sizeof(p), now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TAG_SEEN), 1u);
	zassert_equal((int64_t)field_u(event(CTAG_SERIAL_MSG_EVT_TAG_SEEN, 0), CTAG_CBOR_KEY_RSSI), -61);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_TAG_SEEN, 0), CTAG_CBOR_KEY_BRIDGE), BRIDGE_A);
	now_ms += 1;
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_TAG_SEEN, p, sizeof(p), now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TAG_SEEN), 1u);
	zassert_equal(core.c.tag_seen_limited, 1u);
}

ZTEST(gw_nodes, test_list_nodes_and_inventory)
{
	struct ctag_mesh_caps_status caps = {.proto = 1u, .fw_major = 1u, .fw_minor = 2u,
					     .fw_patch = 13u, .board = 3u, .fontpack_id = {7},
					     .flash_mib = 8u, .max_tags = 20u, .assigned = 7u,
					     .flags = 1u};
	struct ctag_mesh_health_status health = {.uptime_s = 99u, .sessions_ok = 4u};
	uint8_t p[CTAG_MESH_CAPS_STATUS_LEN];
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_NODES};
	struct ctag_cbor_str items[8];
	struct ctag_cbor_field it[6] = {{.key = CTAG_CBOR_KEY_ADDR}, {.key = CTAG_CBOR_KEY_NAME},
					{.key = CTAG_CBOR_KEY_CONFIGURED}, {.key = CTAG_CBOR_KEY_FW},
					{.key = CTAG_CBOR_KEY_FLASH_SIZE},
					{.key = CTAG_CBOR_KEY_ASSIGNED}};
	uint16_t rid;
	const struct frame *e;

	rid = host_send(CTAG_SERIAL_MSG_LIST_NODES, NULL, 0u, 1u);
	host_read();
	decode(response(rid), &f, 1u);
	zassert_equal(ctag_cbor_maps(&f.v.str, items, 8u), 2, "the gateway itself is not listed");
	zassert_equal(ctag_cbor_decode(items[0].ptr, items[0].len, it, 3u), 0);
	zassert_equal(it[0].v.u, BRIDGE_A);
	zassert_mem_equal(it[1].v.str.ptr, "hall", 4u);
	zassert_true(it[2].v.b);

	(void)ctag_mesh_caps_status_pack(&caps, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_CAPS_STATUS, p, CTAG_MESH_CAPS_STATUS_LEN,
			now_ms);
	(void)ctag_mesh_health_status_pack(&health, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_HEALTH_STATUS, p, CTAG_MESH_HEALTH_STATUS_LEN,
			now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_BRIDGE_INFO), 2u);
	e = event(CTAG_SERIAL_MSG_EVT_BRIDGE_INFO, 1);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_ADDR), BRIDGE_A);
	zassert_true(field_has(e, CTAG_CBOR_KEY_COUNTERS));
	{
		/* The bridge's capacity and its own count of assigned tags (CAPS_STATUS),
		 * whatever the gateway's assignment table holds (nothing yet). */
		struct ctag_cbor_field ev[2] = {{.key = CTAG_CBOR_KEY_CAPS},
						{.key = CTAG_CBOR_KEY_ASSIGNED}};
		struct ctag_cbor_field c[2] = {{.key = CTAG_CBOR_KEY_MAX_TAGS},
					       {.key = CTAG_CBOR_KEY_ASSIGNED_COUNT}};
		struct ctag_cbor_str none[1];

		decode(e, ev, 2u);
		zassert_true(ev[0].present && ev[1].present);
		zassert_equal(ctag_cbor_decode(ev[0].v.str.ptr, ev[0].v.str.len, c, 2u), 0);
		zassert_equal(c[0].v.u, 20u);
		zassert_true(c[1].present, "caps carry the bridge's own count");
		zassert_equal(c[1].v.u, 7u);
		zassert_equal(ctag_cbor_maps(&ev[1].v.str, none, 1u), 0, "the gateway holds none");
	}

	/* Two assignments on BRIDGE_A: the inventory nests them five levels deep. */
	for (uint32_t t = 1; t <= 2; t++) {
		zassert_equal(status_of(assign(CTAG_SERIAL_MSG_ASSIGN_TAG, 60u + t, BRIDGE_A, 0x500u + t,
					       t)),
			      CTAG_STATUS_ACCEPTED);
		end_last(0);
		mesh_assign_status(BRIDGE_A, 0x500u + t, t, CTAG_STATUS_OK);
	}
	host_read();
	rid = host_send(CTAG_SERIAL_MSG_GET_INVENTORY, NULL, 0u, 1u);
	host_read();
	zassert_equal(sent_count(CTAG_MESH_OP_CAPS_GET), 1u, "only configured bridges are asked");
	zassert_equal(last_sent(CTAG_MESH_OP_CAPS_GET)->dst, BRIDGE_A);
	f.key = CTAG_CBOR_KEY_ITEMS;
	decode(response(rid), &f, 1u);
	zassert_equal(ctag_cbor_maps(&f.v.str, items, 8u), 2);
	zassert_equal(ctag_cbor_decode(items[0].ptr, items[0].len, it, 6u), 0);
	zassert_equal(it[3].v.str.len, 6u);
	zassert_mem_equal(it[3].v.str.ptr, "1.2.13", 6u);
	zassert_equal(it[4].v.u, 8u << 20);
	zassert_true(it[5].present);
	{
		struct ctag_cbor_str as[4];
		struct ctag_cbor_field a[2] = {{.key = CTAG_CBOR_KEY_TAG_ID}, {.key = CTAG_CBOR_KEY_EPOCH}};

		zassert_equal(ctag_cbor_maps(&it[5].v.str, as, 4u), 2, "two assignments listed");
		zassert_equal(ctag_cbor_decode(as[1].ptr, as[1].len, a, 2u), 0);
		zassert_equal(a[0].v.u + a[1].v.u, 0x502u + 2u);
	}
	{
		/* The inventory's caps map carries the bridge's count too. */
		struct ctag_cbor_field cf = {.key = CTAG_CBOR_KEY_CAPS};
		struct ctag_cbor_field c = {.key = CTAG_CBOR_KEY_ASSIGNED_COUNT};

		zassert_equal(ctag_cbor_decode(items[0].ptr, items[0].len, &cf, 1u), 0);
		zassert_true(cf.present);
		zassert_equal(ctag_cbor_decode(cf.v.str.ptr, cf.v.str.len, &c, 1u), 0);
		zassert_true(c.present);
		zassert_equal(c.v.u, 7u, "the bridge's count, not the gateway's two");
	}
	zassert_equal(ctag_cbor_decode(items[1].ptr, items[1].len, it, 6u), 0);
	zassert_false(it[3].present, "no CAPS yet for the unconfigured bridge");
}
