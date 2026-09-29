/*
 * Tags on the gateway's own radio (docs/protocol.md 11; CONFIG_CTAG_GW_RADIO):
 * caps.tag_links, TUNNEL_OPEN refusals and the shared id space, the mesh
 * suspend window around each connection (lib/sched with the core's mesh
 * state), the GATT setup's outcome (EVT_TUNNEL OPEN with CAPS or ident2 and
 * the advertisement's RSSI), messages both ways fragmented per 5.3, the
 * closing rules (TUNNEL_CLOSE, link drop, violations, write failures, idle
 * and waiting timeouts), DISCOVER on the own radio, the mesh sends held back
 * while the mesh is suspended, and RELEASE. The mocked backend asserts that
 * nothing reaches the mesh while it is suspended.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_frag.h>

#include "common.h"

#define TAG_A    0x7A600001u
#define TAG_B    0x7A600002u
#define RSSI_A   (-57)
#define HCI_FAIL 0x3Eu /* connection failed to be established */
#define HCI_TERM 0x13u /* remote user terminated */
#define ATT_ERR  0x0Eu /* unlikely error */
#define SESSION  CTAG_TUNNEL_MODE_SESSION
#define PAIR     CTAG_TUNNEL_MODE_PAIR
#define NO_MODE  (-1)

static const uint8_t caps_value[CTAG_TAG_CAPS_LEN] = {
	1, 0x01, 0x00, 0x60, 0x7A, 20, 1, 0x90, 0x01, 0x2C, 0x01, 1, 1, 0, 1, 0, 192, 4};
static uint8_t ident_value[CTAG_IDENT2_LEN];

static void before(void *f)
{
	(void)f;
	core_reset();
	for (size_t i = 0; i < sizeof(ident_value); i++) {
		ident_value[i] = (uint8_t)(0x40u + i);
	}
	core_session();
}

ZTEST_SUITE(gw_radio, NULL, NULL, before, NULL, NULL);

/* ---- Helpers ---- */

static void peer_of(uint32_t tag, struct sched_peer *p)
{
	p->type = 1u; /* random static, as tags advertise (5.1) */
	p->a[0] = (uint8_t)tag;
	p->a[1] = (uint8_t)(tag >> 8);
	p->a[2] = (uint8_t)(tag >> 16);
	p->a[3] = (uint8_t)(tag >> 24);
	p->a[4] = 0x5Au;
	p->a[5] = 0xC0u;
}

static void adv(uint32_t tag, uint8_t ver, uint8_t flags, int8_t rssi)
{
	struct sched_peer p;

	peer_of(tag, &p);
	gw_core_tag_adv(&core, &p, tag, ver, flags, rssi, now_ms);
}

static uint16_t radio_open(uint64_t op_id, uint32_t tag, uint32_t duration, int mode,
			   uint8_t *status)
{
	struct ctag_cbor_field f[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, GW_ADDR),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, duration),
		GW_F_UINT(CTAG_CBOR_KEY_MODE, mode < 0 ? 0u : (uint32_t)mode),
	};
	uint16_t rid = host_send(CTAG_SERIAL_MSG_TUNNEL_OPEN, f, mode < 0 ? 4u : 5u, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no TUNNEL_OPEN answer");
	*status = (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
	return field_has(r, CTAG_CBOR_KEY_TUNNEL) ? (uint16_t)field_u(r, CTAG_CBOR_KEY_TUNNEL) : 0u;
}

static uint8_t tunnel_send(uint32_t tunnel, const uint8_t *data, size_t len)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, tunnel),
		GW_F_BSTR(CTAG_CBOR_KEY_DATA, data, len),
	};

	return request_status(CTAG_SERIAL_MSG_TUNNEL_SEND, f, 2u);
}

static uint8_t tunnel_close(uint32_t tunnel)
{
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, tunnel);

	return request_status(CTAG_SERIAL_MSG_TUNNEL_CLOSE, &f, 1u);
}

struct tev { /* a decoded EVT_TUNNEL */
	bool present;
	uint32_t tunnel, bridge, tag_id, state;
	bool has_data, has_status, has_rssi;
	const uint8_t *data;
	size_t len;
	uint32_t status;
	int64_t rssi;
};

static struct tev tunnel_ev(size_t i)
{
	struct ctag_cbor_field f[7] = {
		{.key = CTAG_CBOR_KEY_TUNNEL}, {.key = CTAG_CBOR_KEY_BRIDGE},
		{.key = CTAG_CBOR_KEY_TAG_ID}, {.key = CTAG_CBOR_KEY_STATE},
		{.key = CTAG_CBOR_KEY_DATA},   {.key = CTAG_CBOR_KEY_STATUS},
		{.key = CTAG_CBOR_KEY_RSSI},
	};
	const struct frame *e = event(CTAG_SERIAL_MSG_EVT_TUNNEL, i);
	struct tev t = {0};

	if (e == NULL) {
		return t;
	}
	decode(e, f, 7u);
	t.present = true;
	t.tunnel = (uint32_t)f[0].v.u;
	t.bridge = (uint32_t)f[1].v.u;
	t.tag_id = (uint32_t)f[2].v.u;
	t.state = (uint32_t)f[3].v.u;
	t.has_data = f[4].present;
	t.data = f[4].v.str.ptr;
	t.len = f[4].v.str.len;
	t.has_status = f[5].present;
	t.status = (uint32_t)f[5].v.u;
	t.has_rssi = f[6].present;
	t.rssi = f[6].v.i;
	return t;
}

/* The single EVT_TUNNEL just read is CLOSED with status. */
static void expect_closed(uint16_t tunnel, uint8_t status)
{
	struct tev e;

	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	e = tunnel_ev(0);
	zassert_equal(e.tunnel, tunnel);
	zassert_equal(e.bridge, GW_ADDR);
	zassert_equal(e.state, CTAG_TUNNEL_CLOSED);
	zassert_true(e.has_status);
	zassert_equal(e.status, status, "closed %u, expected %u", e.status, status);
	zassert_false(e.has_data || e.has_rssi);
}

/* The tag's advertisement starts an attempt: suspend, connect. */
static uint8_t attempt(uint32_t tag, int8_t rssi)
{
	size_t connects = mock.connects;
	struct sched_peer p;

	adv(tag, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_OWNED, rssi);
	zassert_equal(mock.connects, connects + 1u, "no connection attempt");
	zassert_true(mock.suspended, "5.2: the mesh is suspended for the attempt");
	peer_of(tag, &p);
	zassert_mem_equal(&mock.connect_peer, &p, sizeof(p));
	zassert_equal(mock.connect_timeout, CTAG_BRIDGE_CONN_ATTEMPT_MS);
	return mock.connect_link;
}

/* The attempt connected: the mesh resumed, then GATT setup (5.2 step 8). */
static void connect_ok(uint8_t link, uint8_t mode)
{
	size_t setups = mock.setups;

	gw_core_link_connected(&core, link, 0u, now_ms);
	zassert_false(mock.suspended, "resumed after the connection");
	zassert_equal(mock.setups, setups + 1u, "no GATT setup");
	zassert_equal(mock.setup_link, link);
	zassert_equal(mock.setup_mode, mode);
}

static void ready(uint8_t link, uint8_t mode)
{
	if (mode == SESSION) {
		gw_core_link_ready(&core, link, CTAG_STATUS_OK, caps_value, sizeof(caps_value), now_ms);
	} else {
		gw_core_link_ready(&core, link, CTAG_STATUS_OK, ident_value, sizeof(ident_value),
				   now_ms);
	}
}

/* An own-radio tunnel of mode to tag, OPEN on *link. */
static uint16_t open_tunnel(uint64_t op_id, uint32_t tag, uint8_t mode, uint32_t duration,
			    uint8_t *link)
{
	uint8_t status;
	uint16_t t = radio_open(op_id, tag, duration, mode, &status);
	struct tev e;

	zassert_equal(status, CTAG_STATUS_OK);
	*link = attempt(tag, RSSI_A);
	connect_ok(*link, mode);
	ready(*link, mode);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	e = tunnel_ev(0);
	zassert_equal(e.state, CTAG_TUNNEL_OPEN);
	zassert_equal(e.tunnel, t);
	return t;
}

/* The tag sends msg on chr, 5.3 fragments numbered from seq; the next seq. */
static uint8_t tag_sends(uint8_t link, uint8_t chr, const uint8_t *msg, size_t len, uint8_t seq)
{
	struct ctag_frag_tx tx = {.seq = seq};
	uint8_t v[CTAG_ATT_VALUE_MAX];
	size_t off = 0u;
	int n;

	while ((n = ctag_frag_next(&tx, msg, len, &off, CTAG_FRAG_PAYLOAD_MAX, v)) > 0) {
		gw_core_link_rx(&core, link, chr, v, (size_t)n, now_ms);
	}
	return tx.seq;
}

/* Complete every write from *done on (also those the completions start). */
static void complete_writes(size_t *done)
{
	while (*done < mock.n_writes) {
		const struct radio_write *w = &mock.writes[(*done)++];

		gw_core_link_written(&core, w->link, w->chr, 0, now_ms);
	}
}

/* Reassemble the writes on chr from index from (5.3, SEQ from seq). */
static size_t written_msg(uint8_t chr, size_t from, uint8_t seq, uint8_t *out, size_t max)
{
	struct ctag_frag_rx rx;
	int n = 0;

	ctag_frag_rx_init(&rx, out, (uint16_t)max);
	rx.seq = seq;
	for (size_t i = from; i < mock.n_writes && n == 0; i++) {
		if (mock.writes[i].chr == chr) {
			n = ctag_frag_rx_put(&rx, mock.writes[i].data, mock.writes[i].len);
			zassert_true(n >= 0, "a 5.3 violation in what the gateway wrote");
		}
	}
	return n > 0 ? (size_t)n : 0u;
}

static void pattern(uint8_t *buf, size_t len, uint8_t first, uint8_t mul)
{
	for (size_t i = 0; i < len; i++) {
		buf[i] = (uint8_t)(i * mul + 7u);
	}
	if (len > 0u) {
		buf[0] = first;
	}
}

/* ---- caps, refusals, ids ---- */

ZTEST(gw_radio, test_caps_report_tag_links)
{
	struct ctag_cbor_field hello[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, "t", 1u),
	};
	struct ctag_cbor_field caps = {.key = CTAG_CBOR_KEY_CAPS};
	struct ctag_cbor_field links = {.key = CTAG_CBOR_KEY_TAG_LINKS};
	uint16_t rid;

	rid = host_send(CTAG_SERIAL_MSG_INFO, NULL, 0u, 1u);
	host_read();
	decode(response(rid), &caps, 1u);
	zassert_ok(ctag_cbor_decode(caps.v.str.ptr, caps.v.str.len, &links, 1u));
	zassert_true(links.present);
	zassert_equal(links.v.u, CONFIG_CTAG_GW_TAG_LINKS);
	zassert_equal(links.v.u, 2u, "the nRF52840's two tag links");
	host_forget(); /* HELLO ends the session */
	rid = host_send(CTAG_SERIAL_MSG_HELLO, hello, 2u, 60u);
	host_read();
	decode(response(rid), &caps, 1u);
	zassert_ok(ctag_cbor_decode(caps.v.str.ptr, caps.v.str.len, &links, 1u));
	zassert_true(links.present);
	zassert_equal(links.v.u, CONFIG_CTAG_GW_TAG_LINKS);
}

ZTEST(gw_radio, test_open_refusals_and_idempotency)
{
	struct ctag_cbor_field bridge_session[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 9u),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, TAG_A),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, 30u),
		GW_F_UINT(CTAG_CBOR_KEY_MODE, SESSION),
	};
	uint8_t status;
	uint16_t t, again, ids[CONFIG_CTAG_GW_RADIO_TUNNELS];

	(void)radio_open(1u, 0u, 30u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_INVALID, "tag_id 0");
	(void)radio_open(2u, TAG_A, 0u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_INVALID, "duration_s 0");
	(void)radio_open(3u, TAG_A, 256u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_INVALID, "duration_s above 255");
	(void)radio_open(4u, TAG_A, 30u, 2, &status);
	zassert_equal(status, CTAG_STATUS_INVALID, "an unknown mode");
	zassert_equal(request_status(CTAG_SERIAL_MSG_TUNNEL_OPEN, bridge_session, 5u),
		      CTAG_STATUS_INVALID, "SESSION through a bridge");
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_OPEN), 0u);
	zassert_false(mock.listening, "nothing waits");

	/* mode absent = PAIR */
	t = radio_open(10u, TAG_A, 30u, NO_MODE, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	zassert_true(t != 0u);
	zassert_true(mock.listening, "a waiting tunnel wants advertisements");
	zassert_equal(mock.n_sent, 0u, "nothing on the mesh");
	/* a repeated op_id: the same tunnel, DUPLICATE */
	{
		struct ctag_cbor_field f[4] = {
			GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 10u),
			GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, GW_ADDR),
			GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, TAG_A),
			GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, 30u),
		};
		uint16_t rid = host_send(CTAG_SERIAL_MSG_TUNNEL_OPEN, f, 4u, 1u);

		host_read();
		zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
		zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_DETAIL), CTAG_STATUS_DUPLICATE);
		zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_TUNNEL), t);
	}
	/* one tunnel per tag; BUSY is not remembered */
	(void)radio_open(11u, TAG_A, 30u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_BUSY);
	zassert_equal(tunnel_close(t), CTAG_STATUS_OK);
	zassert_false(mock.listening);
	again = radio_open(11u, TAG_A, 30u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_OK, "the same op_id works once the tag is free");
	zassert_true(again != t, "a fresh id");
	/* CONFIG_CTAG_GW_RADIO_TUNNELS at once */
	ids[0] = again;
	for (uint32_t i = 1; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		ids[i] = radio_open(20u + i, TAG_A + i, 30u, PAIR, &status);
		zassert_equal(status, CTAG_STATUS_OK);
		for (uint32_t k = 0; k < i; k++) {
			zassert_true(ids[k] != ids[i], "ids are unique");
		}
	}
	(void)radio_open(40u, TAG_B + 100u, 30u, PAIR, &status);
	zassert_equal(status, CTAG_STATUS_BUSY, "every own-radio tunnel taken");
	zassert_equal(counter("radio_tunnels"), CONFIG_CTAG_GW_RADIO_TUNNELS + 1u);
}

ZTEST(gw_radio, test_ids_shared_with_mesh_tunnels)
{
	struct ctag_cbor_field f[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 1u),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 0u),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, 30u),
	};
	static const uint8_t msg[10] = {1, 2, 3};
	uint8_t status;
	uint16_t rid, mesh, radio;

	rid = host_send(CTAG_SERIAL_MSG_TUNNEL_OPEN, f, 4u, 1u);
	host_read();
	mesh = (uint16_t)field_u(response(rid), CTAG_CBOR_KEY_TUNNEL);
	radio = radio_open(2u, TAG_A, 30u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	zassert_true(mesh != 0u && radio != 0u && mesh != radio);
	/* each id reaches its own table */
	zassert_equal(tunnel_send(mesh, msg, sizeof(msg)), CTAG_STATUS_OK);
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_DATA), 1u);
	end_last(0);
	zassert_equal(tunnel_send(radio, msg, sizeof(msg)), CTAG_STATUS_BUSY, "not open yet");
	zassert_equal(mock.n_writes, 0u);
	zassert_equal(tunnel_close(radio), CTAG_STATUS_OK);
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_CLOSE), 0u, "the own radio sends no mesh close");
	zassert_equal(tunnel_close(radio), CTAG_STATUS_NOT_FOUND);
	zassert_equal(tunnel_close(mesh), CTAG_STATUS_OK);
	zassert_equal(sent_count(CTAG_MESH_OP_TUNNEL_CLOSE), 1u);
	zassert_equal(tunnel_send(0x7777u, msg, sizeof(msg)), CTAG_STATUS_NOT_FOUND);
}

ZTEST(gw_radio, test_mesh_requests_to_the_gateway_addr_are_not_found)
{
	static const uint8_t key[16];
	static const uint8_t layout[16] = {0x43, 0x4C};
	struct ctag_cbor_field assign[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 1u),    GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, GW_ADDR),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, TAG_A), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_BSTR(CTAG_CBOR_KEY_KEY, key, 16u),
	};
	struct ctag_cbor_field cmd[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 3u),    GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, GW_ADDR),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, TAG_A), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_UINT(CTAG_CBOR_KEY_CMD, CTAG_TAG_CMD_CLEAR),
	};
	struct ctag_cbor_field node[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 5u),
		GW_F_UINT(CTAG_CBOR_KEY_ADDR, GW_ADDR),
	};
	uint16_t rid;

	/* 11.1: the companion keeps these for its own-radio tags itself. */
	zassert_equal(request_status(CTAG_SERIAL_MSG_ASSIGN_TAG, assign, 5u), CTAG_STATUS_NOT_FOUND);
	assign[0] = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 2u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_UNASSIGN_TAG, assign, 4u),
		      CTAG_STATUS_NOT_FOUND);
	zassert_equal(request_status(CTAG_SERIAL_MSG_TAG_COMMAND, cmd, 5u), CTAG_STATUS_NOT_FOUND);
	rid = send_deliver(4u, GW_ADDR, 77u, layout, sizeof(layout));
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_NOT_FOUND);
	zassert_equal(request_status(CTAG_SERIAL_MSG_IDENTIFY_NODE, node, 2u), CTAG_STATUS_NOT_FOUND);
	node[0] = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 6u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_REMOVE_NODE, node, 2u), CTAG_STATUS_NOT_FOUND);
	zassert_equal(mock.n_sent, 0u);
	zassert_equal(mock.connects, 0u);
}

/* ---- Opening ---- */

ZTEST(gw_radio, test_session_opens_with_caps_and_the_advert_rssi)
{
	static const uint8_t msg[4] = {CTAG_CTRL_HELLO, 1, 2, 3};
	uint8_t status, link;
	uint16_t t;
	struct tev e;

	t = radio_open(1u, TAG_A, 30u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	/* other tags and other versions do nothing */
	adv(TAG_B, 1u, 0u, -40);
	adv(TAG_A, 3u, 0u, -40);
	zassert_equal(mock.suspends, 0u);
	/* 5.1 ver 1 is a tag too: the advertisement starts the attempt */
	{
		struct sched_peer p;

		peer_of(TAG_A, &p);
		gw_core_tag_adv(&core, &p, TAG_A, 1u, 0u, RSSI_A, now_ms);
	}
	zassert_equal(mock.suspends, 1u);
	zassert_equal(mock.connects, 1u);
	zassert_true(mock.suspended);
	link = mock.connect_link;
	zassert_equal(link, 0u, "the first free link");
	zassert_equal(mock.connect_timeout, CTAG_BRIDGE_CONN_ATTEMPT_MS);
	adv(TAG_A, 1u, 0u, -80); /* one attempt at a time */
	zassert_equal(mock.connects, 1u);
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_BUSY, "not OPEN yet");
	now_ms += 300;
	gw_core_link_connected(&core, link, 0u, now_ms);
	zassert_equal(mock.resumes, 1u);
	zassert_false(mock.suspended);
	zassert_equal(mock.setups, 1u);
	zassert_equal(mock.setup_mode, SESSION);
	zassert_false(mock.listening, "nothing waits any more");
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u, "nothing before the setup");
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_BUSY, "not OPEN yet");
	/* a value before OPEN is not relayed */
	gw_core_link_rx(&core, link, GW_CHR_CTRL, (const uint8_t[]){0xC0, 1}, 2u, now_ms);
	ready(link, SESSION);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	e = tunnel_ev(0);
	zassert_equal(e.tunnel, t);
	zassert_equal(e.bridge, GW_ADDR);
	zassert_equal(e.tag_id, TAG_A);
	zassert_equal(e.state, CTAG_TUNNEL_OPEN);
	zassert_true(e.has_data);
	zassert_equal(e.len, sizeof(caps_value));
	zassert_mem_equal(e.data, caps_value, sizeof(caps_value));
	zassert_true(e.has_rssi, "OPEN carries the advertisement's RSSI");
	zassert_equal(e.rssi, RSSI_A);
	zassert_false(e.has_status);
	zassert_false(field_has(event(CTAG_SERIAL_MSG_EVT_TUNNEL, 0), CTAG_CBOR_KEY_SEQ),
		      "best effort");
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	zassert_equal(counter("attempts"), 1u);
	zassert_equal(counter("suspend_count"), 1u);
	zassert_equal(counter("radio_stray"), 1u);
	zassert_true(counter("radio_adverts") >= 4u);
}

ZTEST(gw_radio, test_pair_opens_with_ident2_and_relays_pair_messages)
{
	static uint8_t msg[CTAG_PAIR_MSG_MAX + 1u];
	uint8_t out[CTAG_PAIR_MSG_MAX];
	uint8_t link, status;
	uint16_t t;
	size_t done = 0u;
	struct tev e;

	pattern(msg, sizeof(msg), CTAG_PAIR_KIND_HANDSHAKE, 3u);
	t = radio_open(1u, TAG_A, 60u, NO_MODE, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	link = attempt(TAG_A, -66);
	connect_ok(link, PAIR);
	ready(link, PAIR);
	host_read();
	e = tunnel_ev(0);
	zassert_equal(e.state, CTAG_TUNNEL_OPEN);
	zassert_equal(e.len, CTAG_IDENT2_LEN);
	zassert_mem_equal(e.data, ident_value, CTAG_IDENT2_LEN);
	zassert_equal(e.rssi, -66);

	/* PAIR: any first byte, at most PAIR_MSG_MAX, written with response */
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_TOO_LARGE);
	zassert_equal(tunnel_send(t, msg, 100u), CTAG_STATUS_OK);
	zassert_equal(mock.n_writes, 1u, "one outstanding write with response");
	zassert_equal(mock.writes[0].chr, GW_CHR_PAIR);
	zassert_equal(mock.writes[0].data[0], CTAG_FRAG_START | 0u);
	complete_writes(&done);
	zassert_equal(mock.n_writes, 6u, "100 bytes: 5 x 19 + 5");
	zassert_equal(written_msg(GW_CHR_PAIR, 0u, 0u, out, sizeof(out)), 100u);
	zassert_mem_equal(out, msg, 100u);
	for (size_t i = 0; i < mock.n_writes; i++) {
		zassert_equal(mock.writes[i].chr, GW_CHR_PAIR);
	}
	/* PAIR indications from the tag: reassembled into one message */
	(void)tag_sends(link, GW_CHR_PAIR, msg, CTAG_PAIR_MSG_MAX, 0u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u);
	e = tunnel_ev(0);
	zassert_equal(e.state, CTAG_TUNNEL_DATA);
	zassert_equal(e.len, CTAG_PAIR_MSG_MAX);
	zassert_mem_equal(e.data, msg, CTAG_PAIR_MSG_MAX);
	zassert_false(e.has_rssi, "only OPEN carries the RSSI");
	/* a SESSION characteristic's value is not this tunnel's */
	gw_core_link_rx(&core, link, GW_CHR_CTRL, (const uint8_t[]){0xC0, 1}, 2u, now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
}

ZTEST(gw_radio, test_setup_failures_close_the_tunnel)
{
	static const uint8_t statuses[] = {CTAG_STATUS_UNSUPPORTED, CTAG_STATUS_INVALID};
	uint8_t status, link;
	uint16_t t;

	for (size_t i = 0; i < ARRAY_SIZE(statuses); i++) {
		t = radio_open(1u + i, TAG_A + i, 30u, SESSION, &status);
		link = attempt(TAG_A + i, RSSI_A);
		connect_ok(link, SESSION);
		gw_core_link_ready(&core, link, statuses[i], NULL, 0u, now_ms);
		expect_closed(t, statuses[i]);
		zassert_equal(mock.disconnects, i + 1u, "the link goes down");
		gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);
	}
	/* OK with nothing read: nothing the companion could check */
	t = radio_open(5u, TAG_B + 5u, 30u, PAIR, &status);
	link = attempt(TAG_B + 5u, RSSI_A);
	connect_ok(link, PAIR);
	gw_core_link_ready(&core, link, CTAG_STATUS_OK, NULL, 0u, now_ms);
	expect_closed(t, CTAG_STATUS_INVALID);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);
	/* the platform refuses the setup */
	mock.setup_rc = -ENOTCONN;
	t = radio_open(6u, TAG_B + 6u, 30u, PAIR, &status);
	link = attempt(TAG_B + 6u, RSSI_A);
	gw_core_link_connected(&core, link, 0u, now_ms);
	expect_closed(t, CTAG_STATUS_INVALID);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);
	mock.setup_rc = 0;
	/* no answer within the setup bound (5 s): TIMEOUT */
	t = radio_open(7u, TAG_B + 7u, 30u, SESSION, &status);
	link = attempt(TAG_B + 7u, RSSI_A);
	connect_ok(link, SESSION);
	advance(4999);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	advance(1);
	expect_closed(t, CTAG_STATUS_TIMEOUT);
	zassert_equal(counter("sessions_fail"), 5u);
}

/* ---- Sending ---- */

ZTEST(gw_radio, test_ctrl_one_at_a_time_data_in_flight_bounded)
{
	uint8_t ctrl[45], data[100], out[CTAG_TAG_RECORD_WIRE_MAX];
	uint8_t link;
	uint16_t t;
	size_t done = 0u, first;

	pattern(ctrl, sizeof(ctrl), CTAG_CTRL_HELLO, 5u);
	pattern(data, sizeof(data), CTAG_REC_FRAME_BEGIN, 7u);
	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	/* CTRL: written with response, one fragment outstanding */
	zassert_equal(tunnel_send(t, ctrl, sizeof(ctrl)), CTAG_STATUS_OK);
	zassert_equal(mock.n_writes, 1u);
	zassert_equal(mock.writes[0].link, link);
	zassert_equal(mock.writes[0].chr, GW_CHR_CTRL);
	zassert_equal(mock.writes[0].len, CTAG_ATT_VALUE_MAX);
	zassert_equal(mock.writes[0].data[0], CTAG_FRAG_START | 0u, "SEQ from 0 at the connection");
	gw_core_link_written(&core, link, GW_CHR_CTRL, 0, now_ms);
	done = 1u;
	zassert_equal(mock.n_writes, 2u);
	zassert_equal(mock.writes[1].data[0], 1u);
	complete_writes(&done);
	zassert_equal(mock.n_writes, 3u, "45 bytes: 19 + 19 + 7");
	zassert_equal(mock.writes[2].data[0], CTAG_FRAG_END | 2u);
	zassert_equal(mock.writes[2].len, 8u);
	zassert_equal(written_msg(GW_CHR_CTRL, 0u, 0u, out, CTAG_TAG_CTRL_MSG_MAX), sizeof(ctrl));
	zassert_mem_equal(out, ctrl, sizeof(ctrl));
	/* DATA: without response, CONFIG_CTAG_GW_LINK_INFLIGHT at once, its own SEQ */
	first = mock.n_writes;
	zassert_equal(tunnel_send(t, data, sizeof(data)), CTAG_STATUS_OK);
	zassert_equal(mock.n_writes, first + CONFIG_CTAG_GW_LINK_INFLIGHT);
	for (size_t i = first; i < mock.n_writes; i++) {
		zassert_equal(mock.writes[i].chr, GW_CHR_DATA);
		zassert_equal(mock.writes[i].data[0] & CTAG_FRAG_SEQ_MASK, i - first);
	}
	zassert_equal(mock.writes[first].data[0] & CTAG_FRAG_START, CTAG_FRAG_START);
	gw_core_link_written(&core, link, GW_CHR_DATA, 0, now_ms);
	zassert_equal(mock.n_writes, first + CONFIG_CTAG_GW_LINK_INFLIGHT + 1u, "one more");
	done = first + 1u;
	complete_writes(&done);
	zassert_equal(mock.n_writes, first + 6u, "100 bytes: 5 x 19 + 5");
	zassert_equal(written_msg(GW_CHR_DATA, first, 0u, out, sizeof(out)), sizeof(data));
	zassert_mem_equal(out, data, sizeof(data));
	/* the next CTRL message continues CTRL's SEQ */
	first = mock.n_writes;
	zassert_equal(tunnel_send(t, ctrl, 10u), CTAG_STATUS_OK);
	zassert_equal(mock.writes[first].data[0], CTAG_FRAG_START | CTAG_FRAG_END | 3u);
	done = first;
	complete_writes(&done);
	zassert_equal(mock.n_writes, first + 1u);
}

ZTEST(gw_radio, test_send_limits)
{
	static uint8_t big[CTAG_TAG_RECORD_WIRE_MAX + 1u];
	static const uint8_t bad_types[] = {0x00u, 0x30u, 0x7Fu, 0xC0u, 0xFFu};
	uint8_t msg[CTAG_TAG_CTRL_MSG_MAX + 1u] = {0};
	uint8_t link;
	uint16_t t;
	size_t done = 0u;

	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	for (size_t i = 0; i < ARRAY_SIZE(bad_types); i++) {
		msg[0] = bad_types[i];
		zassert_equal(tunnel_send(t, msg, 10u), CTAG_STATUS_INVALID, "type 0x%02x",
			      bad_types[i]);
	}
	zassert_equal(tunnel_send(t, msg, 0u), CTAG_STATUS_INVALID, "empty");
	pattern(msg, sizeof(msg), CTAG_CTRL_AUTH, 1u);
	zassert_equal(tunnel_send(t, msg, CTAG_TAG_CTRL_MSG_MAX + 1u), CTAG_STATUS_TOO_LARGE);
	pattern(big, sizeof(big), CTAG_REC_PLANE_DATA, 1u);
	zassert_equal(tunnel_send(t, big, sizeof(big)), CTAG_STATUS_TOO_LARGE);
	zassert_equal(mock.n_writes, 0u);
	/* two messages per tunnel: one being written, one waiting */
	zassert_equal(tunnel_send(t, msg, CTAG_TAG_CTRL_MSG_MAX), CTAG_STATUS_OK);
	zassert_equal(tunnel_send(t, big, CTAG_TAG_RECORD_WIRE_MAX), CTAG_STATUS_OK);
	zassert_equal(tunnel_send(t, msg, 5u), CTAG_STATUS_BUSY);
	complete_writes(&done);
	zassert_equal(mock.n_writes, 4u + 11u, "64 bytes (4 fragments), then 205 (11)");
	zassert_equal(tunnel_send(t, msg, 5u), CTAG_STATUS_OK, "room again");
	zassert_true(counter("busy") >= 1u);
}

ZTEST(gw_radio, test_host_buffer_shortage_and_write_failures)
{
	uint8_t msg[30];
	uint8_t link;
	uint16_t t;
	size_t done;

	pattern(msg, sizeof(msg), CTAG_CTRL_HELLO, 1u);
	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	/* no ATT buffer: the same fragment (same SEQ) again after GW_RETRY_MS */
	mock.write_rc[0] = -ENOMEM;
	mock.write_rc_n = 1u;
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	zassert_equal(mock.n_writes, 0u);
	advance(GW_RETRY_MS - 1);
	zassert_equal(mock.n_writes, 0u);
	advance(1);
	zassert_equal(mock.n_writes, 1u);
	zassert_equal(mock.writes[0].data[0], CTAG_FRAG_START | 0u);
	done = 0u;
	complete_writes(&done);
	zassert_equal(mock.n_writes, 2u);
	/* the tag refuses a write with response: INVALID */
	pattern(msg, sizeof(msg), CTAG_CTRL_AUTH, 1u);
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	gw_core_link_written(&core, link, GW_CHR_CTRL, ATT_ERR, now_ms);
	expect_closed(t, CTAG_STATUS_INVALID);
	zassert_equal(mock.disconnects, 1u);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);

	/* a DATA completion that failed: DISCONNECTED */
	t = open_tunnel(2u, TAG_B, SESSION, 30u, &link);
	pattern(msg, sizeof(msg), CTAG_REC_FRAME_END, 1u);
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_OK);
	gw_core_link_written(&core, link, GW_CHR_DATA, -ECONNRESET, now_ms);
	expect_closed(t, CTAG_STATUS_DISCONNECTED);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);

	/* the host refuses a fragment for good: DISCONNECTED, after the OK */
	t = open_tunnel(3u, TAG_B + 1u, SESSION, 30u, &link);
	mock.write_rc[0] = -EIO;
	mock.write_rc_n = 1u;
	{
		struct ctag_cbor_field f[2] = {
			GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, t),
			GW_F_BSTR(CTAG_CBOR_KEY_DATA, msg, sizeof(msg)),
		};
		uint16_t rid = host_send(CTAG_SERIAL_MSG_TUNNEL_SEND, f, 2u, 1u);
		struct tev e;

		host_read();
		zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
		e = tunnel_ev(0);
		zassert_equal(e.state, CTAG_TUNNEL_CLOSED);
		zassert_equal(e.status, CTAG_STATUS_DISCONNECTED);
	}
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_NOT_FOUND);
}

/* ---- Receiving ---- */

ZTEST(gw_radio, test_values_reassembled_per_characteristic)
{
	uint8_t challenge[40], record[150];
	static const uint8_t credit[2] = {CTAG_PLAIN_CREDIT, 4};
	uint8_t link, ctrl_seq, status_seq;
	uint16_t t;
	struct tev e;

	pattern(challenge, sizeof(challenge), CTAG_CTRL_CHALLENGE, 9u);
	pattern(record, sizeof(record), CTAG_REC_RESULT, 11u);
	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	ctrl_seq = tag_sends(link, GW_CHR_CTRL, challenge, sizeof(challenge), 0u);
	status_seq = tag_sends(link, GW_CHR_STATUS, credit, sizeof(credit), 0u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 2u);
	e = tunnel_ev(0);
	zassert_equal(e.state, CTAG_TUNNEL_DATA);
	zassert_equal(e.len, sizeof(challenge));
	zassert_mem_equal(e.data, challenge, sizeof(challenge));
	zassert_false(e.has_rssi || e.has_status);
	e = tunnel_ev(1);
	zassert_equal(e.len, sizeof(credit));
	zassert_mem_equal(e.data, credit, sizeof(credit));
	/* interleaved: STATUS completes first, CTRL keeps its own SEQ */
	{
		uint8_t v[CTAG_ATT_VALUE_MAX];
		struct ctag_frag_tx ctx = {.seq = ctrl_seq};
		size_t off = 0u;
		int n = ctag_frag_next(&ctx, challenge, sizeof(challenge), &off, CTAG_FRAG_PAYLOAD_MAX,
				       v);

		gw_core_link_rx(&core, link, GW_CHR_CTRL, v, (size_t)n, now_ms);
		status_seq = tag_sends(link, GW_CHR_STATUS, record, sizeof(record), status_seq);
		while ((n = ctag_frag_next(&ctx, challenge, sizeof(challenge), &off,
					   CTAG_FRAG_PAYLOAD_MAX, v)) > 0) {
			gw_core_link_rx(&core, link, GW_CHR_CTRL, v, (size_t)n, now_ms);
		}
		ctrl_seq = ctx.seq;
	}
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 2u);
	e = tunnel_ev(0);
	zassert_equal(e.len, sizeof(record));
	zassert_mem_equal(e.data, record, sizeof(record));
	e = tunnel_ev(1);
	zassert_equal(e.len, sizeof(challenge));
	/* a 5.3 violation (a gap in CTRL's SEQ) ends the tunnel INVALID */
	(void)tag_sends(link, GW_CHR_CTRL, challenge, 10u, (uint8_t)(ctrl_seq + 1u));
	expect_closed(t, CTAG_STATUS_INVALID);
	zassert_equal(mock.disconnects, 1u);
	zassert_equal(counter("sessions_fail"), 1u);
}

ZTEST(gw_radio, test_overlong_value_and_message_close_invalid)
{
	uint8_t big[CTAG_TAG_CTRL_MSG_MAX + 1u];
	uint8_t link;
	uint16_t t;

	pattern(big, sizeof(big), CTAG_CTRL_ERROR, 1u);
	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	(void)tag_sends(link, GW_CHR_CTRL, big, sizeof(big), 0u); /* > TAG_CTRL_MSG_MAX */
	expect_closed(t, CTAG_STATUS_INVALID);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);
	/* a value that did not fit the platform's event (value NULL) */
	t = open_tunnel(2u, TAG_B, SESSION, 30u, &link);
	gw_core_link_rx(&core, link, GW_CHR_STATUS, NULL, 0u, now_ms);
	expect_closed(t, CTAG_STATUS_INVALID);
}

/* ---- Closing ---- */

ZTEST(gw_radio, test_tunnel_close_disconnects_without_an_event)
{
	static const uint8_t msg[5] = {CTAG_CTRL_HELLO};
	uint8_t link, status;
	uint16_t t;

	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	zassert_equal(tunnel_close(t), CTAG_STATUS_OK);
	zassert_equal(mock.disconnects, 1u);
	zassert_equal(mock.disconnect_link, link);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	zassert_equal(tunnel_send(t, msg, sizeof(msg)), CTAG_STATUS_NOT_FOUND);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	zassert_equal(counter("sessions_ok"), 1u);
	/* no back-off after a close the host asked for: the next advert connects */
	t = open_tunnel(2u, TAG_A, SESSION, 30u, &link);
	zassert_equal(mock.connects, 2u);

	/* closed while waiting: nothing to disconnect, nothing to report */
	t = radio_open(3u, TAG_B, 30u, PAIR, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	zassert_true(mock.listening);
	zassert_equal(tunnel_close(t), CTAG_STATUS_OK);
	zassert_false(mock.listening);
	zassert_equal(mock.disconnects, 1u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
}

ZTEST(gw_radio, test_closed_while_connecting_the_link_goes_down)
{
	uint8_t link, status;
	uint16_t t;

	t = radio_open(1u, TAG_A, 30u, SESSION, &status);
	link = attempt(TAG_A, RSSI_A);
	zassert_equal(tunnel_close(t), CTAG_STATUS_OK);
	gw_core_link_connected(&core, link, 0u, now_ms);
	zassert_false(mock.suspended, "resumed all the same");
	zassert_equal(mock.setups, 0u, "no GATT for a tunnel that is gone");
	zassert_equal(mock.disconnects, 1u);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
}

ZTEST(gw_radio, test_link_drop_closes_disconnected_and_backs_off)
{
	uint8_t link, status;
	uint16_t t;

	t = open_tunnel(1u, TAG_A, SESSION, 30u, &link);
	gw_core_link_disconnected(&core, link, 0x08u, now_ms); /* supervision timeout */
	expect_closed(t, CTAG_STATUS_DISCONNECTED);
	zassert_equal(mock.disconnects, 0u, "it dropped by itself");
	zassert_equal(counter("sessions_fail"), 1u);
	/* 5.2 step 2: the tag backs off (BRIDGE_TAG_BACKOFF_MS) */
	(void)radio_open(2u, TAG_A, 60u, SESSION, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.connects, 1u);
	zassert_equal(counter("backoff_skips"), 1u);
	advance(CTAG_BRIDGE_TAG_BACKOFF_MS);
	(void)attempt(TAG_A, RSSI_A);
}

ZTEST(gw_radio, test_waiting_and_idle_timeouts)
{
	static const uint8_t ctrl[3] = {CTAG_CTRL_HELLO, 1, 2};
	uint8_t link, status;
	uint16_t t;
	size_t done = 0u;

	/* the tag not reached within duration_s */
	t = radio_open(1u, TAG_A, 5u, SESSION, &status);
	zassert_true(mock.listening);
	advance(4999);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	advance(1);
	expect_closed(t, CTAG_STATUS_TIMEOUT);
	zassert_false(mock.listening);
	zassert_equal(mock.suspends, 0u);

	/* connected: duration_s (+ 5 s grace) without a message either way */
	t = open_tunnel(2u, TAG_B, SESSION, 10u, &link);
	advance(6000);
	zassert_equal(tunnel_send(t, ctrl, sizeof(ctrl)), CTAG_STATUS_OK); /* a message restarts it */
	complete_writes(&done);
	advance(10000 + GW_TUNNEL_GRACE_MS - 1);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	(void)tag_sends(link, GW_CHR_STATUS, (const uint8_t[]){CTAG_PLAIN_CREDIT, 1}, 2u, 0u);
	advance(10000 + GW_TUNNEL_GRACE_MS - 1);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 1u, "only the DATA");
	advance(1);
	expect_closed(t, CTAG_STATUS_TIMEOUT);
	zassert_equal(mock.disconnects, 1u);
}

ZTEST(gw_radio, test_waiting_deadline_waits_for_the_attempt)
{
	uint8_t link, status;
	uint16_t t;
	struct tev e;

	/* reached just in time: the attempt started before the deadline wins */
	t = radio_open(1u, TAG_A, 2u, SESSION, &status);
	advance(1900);
	link = attempt(TAG_A, RSSI_A);
	advance(300); /* past the deadline while connecting */
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u);
	connect_ok(link, SESSION);
	ready(link, SESSION);
	host_read();
	e = tunnel_ev(0);
	zassert_equal(e.tunnel, t);
	zassert_equal(e.state, CTAG_TUNNEL_OPEN);
	zassert_equal(tunnel_close(t), CTAG_STATUS_OK);
	gw_core_link_disconnected(&core, link, HCI_TERM, now_ms);

	/* the attempt fails after the deadline: TIMEOUT at once */
	t = radio_open(2u, TAG_B, 2u, SESSION, &status);
	advance(1990);
	link = attempt(TAG_B, RSSI_A);
	advance(20);
	gw_core_link_connected(&core, link, HCI_FAIL, now_ms);
	zassert_false(mock.suspended);
	advance(0);
	expect_closed(t, CTAG_STATUS_TIMEOUT);
	zassert_equal(counter("connect_failed"), 1u);
}

/* ---- DISCOVER on the own radio ---- */

static uint8_t discover(uint64_t op_id, uint16_t bridge, uint32_t duration, uint32_t tag_id)
{
	struct ctag_cbor_field f[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_DURATION_S, duration),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, tag_id),
	};

	return request_status(CTAG_SERIAL_MSG_DISCOVER, f, 4u);
}

ZTEST(gw_radio, test_discover_on_the_own_radio)
{
	const struct frame *e;
	struct ctag_mesh_discovered d = {.tag_id = TAG_B, .rssi = -70, .flags = CTAG_ADV_FLAG_SETUP};
	uint8_t p[CTAG_MESH_DISCOVERED_LEN];

	zassert_equal(discover(1u, GW_ADDR, 30u, 0u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_DISCOVER), 0u, "the own radio only");
	zassert_true(mock.listening);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -61);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -60); /* within 5 s */
	adv(TAG_B, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_OWNED, -60); /* not in setup mode */
	adv(TAG_B, 1u, CTAG_ADV_FLAG_SETUP, -60);                        /* not a v2 tag */
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 1u);
	e = event(CTAG_SERIAL_MSG_EVT_DISCOVERED, 0);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_BRIDGE), GW_ADDR);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_TAG_ID), TAG_A);
	zassert_equal((int64_t)field_u(e, CTAG_CBOR_KEY_RSSI), -61);
	zassert_equal(field_u(e, CTAG_CBOR_KEY_FLAGS), CTAG_ADV_FLAG_SETUP);
	zassert_false(field_has(e, CTAG_CBOR_KEY_SEQ), "not retained");
	advance(CTAG_DISCOVERED_MIN_INTERVAL_MS);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -59);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 1u);
	/* the window ends (duration_s + the bridges' grace): not listening any more */
	advance(30000 + GW_DISCOVER_GRACE_MS);
	zassert_false(mock.listening);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -58);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 0u);
	zassert_equal(counter("discovered_limited"), 1u);

	/* bridge 0: every bridge and the own radio; the tag filter; the (bridge,
	 * tag) rate limit shared with the bridges' candidates */
	zassert_equal(discover(2u, 0u, 30u, TAG_B), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_DISCOVER), 1u);
	zassert_true(mock.listening);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -61); /* another tag */
	adv(TAG_B, CTAG_SECURE_PROTO_VERSION, CTAG_ADV_FLAG_SETUP, -62);
	(void)ctag_mesh_discovered_pack(&d, p, sizeof(p));
	gw_core_mesh_rx(&core, BRIDGE_A, CTAG_MESH_OP_DISCOVERED, p, sizeof(p), now_ms);
	host_read();
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_DISCOVERED), 2u);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_DISCOVERED, 0), CTAG_CBOR_KEY_BRIDGE), GW_ADDR);
	zassert_equal(field_u(event(CTAG_SERIAL_MSG_EVT_DISCOVERED, 1), CTAG_CBOR_KEY_BRIDGE),
		      BRIDGE_A);
	/* duration 0 closes the own window at once */
	zassert_equal(discover(3u, GW_ADDR, 0u, 0u), CTAG_STATUS_ACCEPTED);
	zassert_false(mock.listening);
	zassert_equal(discover(4u, GW_ADDR, 121u, 0u), CTAG_STATUS_INVALID);
}

/* ---- The mesh around a connection (5.2) ---- */

ZTEST(gw_radio, test_mesh_sends_wait_while_suspended)
{
	static const uint8_t layout[300] = {0x43, 0x4C};
	struct ctag_cbor_field ident[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 3u),
		GW_F_UINT(CTAG_CBOR_KEY_ADDR, BRIDGE_A),
	};
	static const uint8_t uuid[16] = {0xC0, 0xFF, 0xEE, 9};
	static const uint8_t oob[32] = {0x5A};
	struct ctag_cbor_field prov[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 4u),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, 16u),
		GW_F_BSTR(CTAG_CBOR_KEY_STATIC_OOB, oob, 32u),
	};
	uint8_t link, status;
	uint16_t rid;

	(void)radio_open(1u, TAG_A, 30u, SESSION, &status);
	link = attempt(TAG_A, RSSI_A);
	/* the mesh is suspended: a delivery, an unsegmented message and a
	 * provisioning wait (the mock fails any mesh operation now) */
	rid = send_deliver(2u, BRIDGE_A, 55u, layout, sizeof(layout));
	host_read();
	zassert_equal(field_u(response(rid), CTAG_CBOR_KEY_STATUS), CTAG_STATUS_ACCEPTED);
	zassert_equal(request_status(CTAG_SERIAL_MSG_IDENTIFY_NODE, ident, 2u), CTAG_STATUS_OK);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, prov, 3u),
		      CTAG_STATUS_PROVISIONING_ACTIVE, "retried after the resume");
	advance(3 * GW_RETRY_MS);
	zassert_equal(mock.n_sent, 0u);
	zassert_equal(mock.n_provision, 0u);
	zassert_true(counter("mesh_paused") >= 2u);
	/* connected: the resume releases them at once */
	connect_ok(link, SESSION);
	advance(0);
	zassert_equal(sent_count(CTAG_MESH_OP_LAYOUT_BEGIN), 1u);
	zassert_equal(sent_count(CTAG_MESH_OP_IDENTIFY), 1u);
	zassert_equal(counter("mesh_send_failures"), 0u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, prov, 3u), CTAG_STATUS_ACCEPTED);
}

ZTEST(gw_radio, test_attempt_waits_for_own_segmented_sends)
{
	struct ctag_cbor_field cmd[5] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 2u),    GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, BRIDGE_A),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, TAG_B), GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 1u),
		GW_F_UINT(CTAG_CBOR_KEY_CMD, CTAG_TAG_CMD_CLEAR),
	};
	uint8_t status;

	(void)radio_open(1u, TAG_A, 60u, SESSION, &status);
	zassert_equal(request_status(CTAG_SERIAL_MSG_TAG_COMMAND, cmd, 5u), CTAG_STATUS_ACCEPTED);
	zassert_equal(sent_count(CTAG_MESH_OP_TAG_CMD), 1u, "in flight");
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.suspends, 0u, "5.2 step 3: own sends finish first");
	advance(40);
	zassert_equal(mock.suspends, 0u);
	end_last(0); /* the segmented send is through */
	advance(20);
	zassert_equal(mock.suspends, 1u);
	zassert_equal(mock.connects, 1u);
	gw_core_link_connected(&core, mock.connect_link, HCI_FAIL, now_ms);
	/* a send that does not finish within 500 ms defers the attempt */
	cmd[0] = GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 3u);
	zassert_equal(request_status(CTAG_SERIAL_MSG_TAG_COMMAND, cmd, 5u), CTAG_STATUS_ACCEPTED);
	advance(CTAG_TAG_ADV_WINDOW_MS); /* past the quick retry: a plain back-off, then */
	advance(CTAG_BRIDGE_TAG_BACKOFF_MS);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	advance(500);
	zassert_equal(mock.suspends, 1u);
	zassert_equal(counter("deferred"), 1u);
}

ZTEST(gw_radio, test_no_attempt_while_provisioning_or_configuring)
{
	static const uint8_t uuid[16] = {0xC0, 0xFF, 0xEE, 7};
	static const uint8_t oob[32] = {0x5A};
	struct ctag_cbor_field prov[3] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 2u),
		GW_F_BSTR(CTAG_CBOR_KEY_UUID, uuid, 16u),
		GW_F_BSTR(CTAG_CBOR_KEY_STATIC_OOB, oob, 32u),
	};
	struct ctag_cbor_field cfg[4] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, 3u),
		GW_F_UINT(CTAG_CBOR_KEY_ADDR, BRIDGE_B),
		GW_F_BOOL(CTAG_CBOR_KEY_RELAY, true),
		GW_F_UINT(CTAG_CBOR_KEY_TTL, 5u),
	};
	uint8_t status;

	(void)radio_open(1u, TAG_A, 120u, SESSION, &status);
	zassert_equal(request_status(CTAG_SERIAL_MSG_PROVISION, prov, 3u), CTAG_STATUS_ACCEPTED);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.suspends, 0u, "5.2 step 3: not while provisioning");
	gw_core_prov_closed(&core, now_ms); /* nobody answered */
	zassert_equal(request_status(CTAG_SERIAL_MSG_CONFIGURE_NODE, cfg, 4u), CTAG_STATUS_ACCEPTED);
	zassert_equal(mock.n_cfg, 1u);
	end_tag(mock.cfg[0].tag, 0); /* AppKey Add through the lane */
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.suspends, 0u, "nor while configuring");
	for (int i = 0; i < 6 && core.cfg.kind != GW_CFGOP_NONE; i++) {
		gw_core_cfg_status(&core, BRIDGE_B, core.cfg.step, 0u,
				   core.cfg.step == GW_CFG_RELAY ? 1u : 5u, now_ms);
	}
	zassert_equal(core.cfg.kind, GW_CFGOP_NONE);
	adv(TAG_A, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.suspends, 1u);
}

ZTEST(gw_radio, test_rate_limit_across_tags)
{
	uint8_t status;

	for (uint32_t i = 0; i < 7u; i++) {
		(void)radio_open(1u + i, TAG_A + i, 200u, SESSION, &status);
		zassert_equal(status, CTAG_STATUS_OK);
	}
	/* BRIDGE_MAX_SUSPENDS_PER_MIN attempts in a rolling minute, whatever the tags */
	for (uint32_t i = 0; i < CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN; i++) {
		uint8_t link = attempt(TAG_A + i, RSSI_A);

		gw_core_link_connected(&core, link, HCI_FAIL, now_ms);
		advance(1000);
	}
	adv(TAG_A + 6u, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.suspends, CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN);
	zassert_equal(counter("rate_limited"), 1u);
	advance(60000);
	(void)attempt(TAG_A + 6u, RSSI_A);
	zassert_equal(counter("connect_failed"), CTAG_BRIDGE_MAX_SUSPENDS_PER_MIN);
}

ZTEST(gw_radio, test_second_link_only_while_the_first_is_idle)
{
	static const uint8_t ctrl[30] = {CTAG_CTRL_AUTH};
	uint8_t link_a, link_b, status;
	uint16_t a, b;
	size_t done = 0u;
	struct tev e;

	a = open_tunnel(1u, TAG_A, SESSION, 60u, &link_a);
	b = radio_open(2u, TAG_B, 60u, PAIR, &status);
	zassert_equal(status, CTAG_STATUS_OK);
	/* A has something left to write: B waits */
	zassert_equal(tunnel_send(a, ctrl, sizeof(ctrl)), CTAG_STATUS_OK);
	adv(TAG_B, CTAG_SECURE_PROTO_VERSION, 0u, -70);
	zassert_equal(mock.connects, 1u);
	complete_writes(&done);
	/* A idle: B connects on the other link */
	link_b = attempt(TAG_B, -70);
	zassert_true(link_b != link_a);
	connect_ok(link_b, PAIR);
	ready(link_b, PAIR);
	host_read();
	e = tunnel_ev(0);
	zassert_equal(e.tunnel, b);
	zassert_equal(e.rssi, -70);
	/* both links busy: a third tag waits for a free one */
	(void)radio_open(3u, TAG_B + 1u, 60u, SESSION, &status);
	adv(TAG_B + 1u, CTAG_SECURE_PROTO_VERSION, 0u, RSSI_A);
	zassert_equal(mock.connects, 2u);
	/* the links are independent: B's drop leaves A open */
	gw_core_link_disconnected(&core, link_b, 0x08u, now_ms);
	expect_closed(b, CTAG_STATUS_DISCONNECTED);
	zassert_equal(tunnel_send(a, ctrl, sizeof(ctrl)), CTAG_STATUS_OK);
	zassert_equal(mock.writes[mock.n_writes - 1u].link, link_a);
}

ZTEST(gw_radio, test_release_closes_every_own_radio_tunnel)
{
	uint8_t challenge[16], grant[CTAG_GRANT_MAX], sig[64], pub[32];
	uint8_t link, status;
	size_t len;

	(void)open_tunnel(1u, TAG_A, SESSION, 60u, &link);
	(void)radio_open(2u, TAG_B, 60u, PAIR, &status);
	zassert_true(mock.listening);
	host_challenge(challenge);
	ctag_secure_x25519_public(test_worker, pub);
	host_grant(CTAG_GRANT_OP_RELEASE, pub, challenge, 1u, grant, &len, sig);
	{
		struct ctag_cbor_field f[2] = {GW_F_BSTR(CTAG_CBOR_KEY_GRANT, grant, len),
					       GW_F_BSTR(CTAG_CBOR_KEY_SIG, sig, 64u)};

		zassert_equal(request_status(CTAG_SERIAL_MSG_RELEASE, f, 2u), CTAG_STATUS_OK);
	}
	zassert_equal(mock.releases, 1u);
	zassert_equal(mock.disconnects, 1u, "the connected tag's link goes down");
	zassert_equal(mock.disconnect_link, link);
	zassert_false(mock.listening, "nothing waits");
	zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_TUNNEL), 0u, "without events");
	for (int i = 0; i < CONFIG_CTAG_GW_RADIO_TUNNELS; i++) {
		zassert_equal(core.radio.tunnels[i].state, GW_RT_FREE);
	}
}
