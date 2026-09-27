/* Mocked backend and host for the gateway core tests. */
#include <string.h>

#include <psa/crypto.h>

#include "common.h"

struct gw_core core;
int64_t now_ms;
struct mock mock;

static uint8_t capture[256 * 1024];
static size_t cap_len, cap_read;
static struct ctag_serial_rx host_rx;
static uint8_t host_rxbuf[CTAG_SERIAL_MAX_FRAME];
static uint16_t next_rid;
static size_t send_cursor;

struct frame rx_frames[64];
size_t rx_count;

/* ---- Backend ---- */

static size_t be_write(void *ctx, const uint8_t *data, size_t len)
{
	(void)ctx;
	if (len > mock.tx_room) {
		len = mock.tx_room;
	}
	zassert_true(cap_len + len <= sizeof(capture), "capture full");
	memcpy(&capture[cap_len], data, len);
	cap_len += len;
	return len;
}

static void be_reboot(void *ctx)
{
	(void)ctx;
	mock.reboots++;
}

static int next_rc(void)
{
	int rc = 0;

	if (mock.send_rc_n > 0u) {
		rc = mock.send_rc[0];
		memmove(&mock.send_rc[0], &mock.send_rc[1], (mock.send_rc_n - 1u) * sizeof(int));
		mock.send_rc_n--;
	}
	return rc;
}

static int be_send(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
		   uint32_t tag)
{
	struct sent *s;
	int rc = next_rc();

	(void)ctx;
	zassert_true(mock.n_sent < ARRAY_SIZE(mock.sent), "send log full");
	zassert_true(len <= CTAG_MESH_MAX_VENDOR_PARAMS, "params too long");
	if (rc != 0) {
		return rc; /* refused: not recorded */
	}
	s = &mock.sent[mock.n_sent++];
	s->dst = dst;
	s->op = op;
	s->len = (uint8_t)len;
	s->tag = tag;
	if (len > 0u) {
		memcpy(s->data, params, len);
	}
	return 0;
}

static int be_cfg(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag)
{
	(void)ctx;
	zassert_true(mock.n_cfg < ARRAY_SIZE(mock.cfg), "cfg log full");
	if (mock.cfg_rc != 0) {
		return mock.cfg_rc;
	}
	mock.cfg[mock.n_cfg++] = (struct cfg_call){addr, step, arg, tag};
	return 0;
}

static int be_provision(void *ctx, const uint8_t uuid[16])
{
	(void)ctx;
	if (mock.provision_rc != 0) {
		return mock.provision_rc;
	}
	memcpy(mock.provisioned[mock.n_provision++ % 8], uuid, 16);
	return 0;
}

static void be_configured(void *ctx, uint16_t addr)
{
	(void)ctx;
	mock.configured[mock.n_configured++ % 8] = addr;
}

static void be_delete(void *ctx, uint16_t addr)
{
	(void)ctx;
	mock.deleted[mock.n_deleted++ % 8] = addr;
}

static void be_name(void *ctx, uint16_t addr, const char *name, size_t len)
{
	(void)ctx;
	(void)addr;
	memcpy(mock.stored_name, name, len);
	mock.stored_name_len = len;
}

static void be_assign(void *ctx, const struct gw_assign *a, size_t n)
{
	(void)ctx;
	mock.assign_stores++;
	memcpy(mock.stored_assign, a, n * sizeof(*a));
	mock.stored_assign_n = n;
}

static int be_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32])
{
	size_t olen;

	(void)ctx;
	return psa_hash_compute(PSA_ALG_SHA_256, data, len, out, 32, &olen) == PSA_SUCCESS ? 0 : -EIO;
}

static size_t be_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	(void)ctx;
	if (max == 0u) {
		return 0u;
	}
	items[0] = (struct ctag_cbor_counter)CTAG_CBOR_COUNTER("uart_rx_overflow", 0u);
	return 1u;
}

static const struct gw_backend backend = {
	.write = be_write,
	.reboot = be_reboot,
	.mesh_send = be_send,
	.mesh_cfg = be_cfg,
	.provision = be_provision,
	.node_configured = be_configured,
	.node_delete = be_delete,
	.store_name = be_name,
	.store_assignments = be_assign,
	.sha256 = be_sha256,
	.counters = be_counters,
};

/* ---- Core ---- */

static const uint8_t uuid_a[16] = {0xA0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
static const uint8_t uuid_b[16] = {0xB0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};

void core_reset(void)
{
	static const struct gw_info info = {
		.fw = "0.1.0", .build = "test", .board = 1, .boot_id = BOOT_ID};

	zassert_equal(psa_crypto_init(), PSA_SUCCESS);
	memset(&mock, 0, sizeof(mock));
	mock.tx_room = SIZE_MAX;
	cap_len = cap_read = 0u;
	ctag_serial_rx_init(&host_rx, host_rxbuf, sizeof(host_rxbuf));
	rx_count = 0u;
	next_rid = 0u;
	send_cursor = 0u;
	now_ms = 1000;
	gw_core_init(&core, &backend, &info, now_ms);
	gw_core_add_node(&core, BRIDGE_A, uuid_a, 1u, true);
	gw_core_add_node(&core, BRIDGE_B, uuid_b, 1u, false);
	gw_core_set_name(&core, BRIDGE_A, "hall", 4u);
}

void core_session(void)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, "test", 4u),
	};
	uint16_t rid = host_send(CTAG_SERIAL_MSG_HELLO, f, 2u, 60u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no HELLO answer");
	zassert_equal(field_u(r, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
}

void advance(int64_t ms)
{
	now_ms += ms;
	gw_core_poll(&core, now_ms);
}

size_t sent_count(uint8_t op)
{
	size_t n = 0u;

	for (size_t i = 0; i < mock.n_sent; i++) {
		n += mock.sent[i].op == op ? 1u : 0u;
	}
	return n;
}

const struct sent *last_sent(uint8_t op)
{
	for (size_t i = mock.n_sent; i > 0u; i--) {
		if (mock.sent[i - 1u].op == op) {
			return &mock.sent[i - 1u];
		}
	}
	return NULL;
}

const struct sent *sent_at(size_t i)
{
	return i < mock.n_sent ? &mock.sent[i] : NULL;
}

void end_tag(uint32_t tag, int err)
{
	gw_core_send_end(&core, tag, err, now_ms);
}

void end_last(int err)
{
	for (size_t i = mock.n_sent; i > 0u; i--) {
		if (mock.sent[i - 1u].tag != 0u) {
			end_tag(mock.sent[i - 1u].tag, err);
			return;
		}
	}
	zassert_unreachable("no tagged send");
}

/* ---- Host ---- */

uint16_t host_send(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits)
{
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	static uint8_t wire[CTAG_SERIAL_MAX_ENCODED];
	struct ctag_serial_header h = {
		.version = CTAG_PROTO_VERSION, .type = type, .credits = credits};
	int len = n > 0u ? ctag_cbor_encode(f, n, &frame[CTAG_SERIAL_HEADER_LEN],
					    sizeof(frame) - CTAG_SERIAL_MIN_FRAME)
			 : 0;

	zassert_true(len >= 0, "encode failed: %d", len);
	next_rid = (uint16_t)(next_rid % 0xFFFFu + 1u);
	h.request_id = next_rid;
	len = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, (size_t)len);
	zassert_true(len > 0);
	len = ctag_serial_wire_encode(frame, (size_t)len, wire, sizeof(wire));
	zassert_true(len > 0);
	gw_core_rx(&core, wire, (size_t)len, now_ms);
	return h.request_id;
}

void host_bytes(const uint8_t *data, size_t len)
{
	gw_core_rx(&core, data, len, now_ms);
}

size_t capture_peek(const uint8_t **p)
{
	*p = &capture[cap_read];
	return cap_len - cap_read;
}

size_t host_read(void)
{
	struct ctag_serial_header h;
	size_t got = 0u;

	rx_count = 0u;
	for (; cap_read < cap_len; cap_read++) {
		if (ctag_serial_rx_put(&host_rx, capture[cap_read], &h)) {
			struct frame *fr = &rx_frames[rx_count];

			zassert_true(rx_count < ARRAY_SIZE(rx_frames), "too many frames");
			fr->h = h;
			fr->len = h.length;
			zassert_true(h.length <= sizeof(fr->payload));
			memcpy(fr->payload, &host_rxbuf[CTAG_SERIAL_HEADER_LEN], h.length);
			rx_count++;
			got++;
		}
	}
	zassert_equal(host_rx.crc_errors + host_rx.len_errors + host_rx.version_errors, 0u,
		      "gateway sent a malformed frame");
	return got;
}

const struct frame *response(uint16_t rid)
{
	for (size_t i = 0; i < rx_count; i++) {
		if ((rx_frames[i].h.flags & CTAG_SERIAL_FLAG_RESPONSE) &&
		    rx_frames[i].h.request_id == rid) {
			return &rx_frames[i];
		}
	}
	return NULL;
}

const struct frame *event(uint8_t type, size_t idx)
{
	for (size_t i = 0; i < rx_count; i++) {
		if ((rx_frames[i].h.flags & CTAG_SERIAL_FLAG_EVENT) && rx_frames[i].h.type == type) {
			if (idx-- == 0u) {
				return &rx_frames[i];
			}
		}
	}
	return NULL;
}

size_t events_of(uint8_t type)
{
	size_t n = 0u;

	while (event(type, n) != NULL) {
		n++;
	}
	return n;
}

void decode(const struct frame *fr, struct ctag_cbor_field *f, size_t n)
{
	zassert_equal(ctag_cbor_decode(fr->payload, fr->len, f, n), 0, "undecodable payload");
}

uint64_t field_u(const struct frame *fr, uint8_t key)
{
	struct ctag_cbor_field f = {.key = key};

	decode(fr, &f, 1u);
	zassert_true(f.present, "key %u missing", key);
	return f.kind == CTAG_CBOR_INT ? (uint64_t)f.v.i : f.v.u;
}

bool field_has(const struct frame *fr, uint8_t key)
{
	struct ctag_cbor_field f = {.key = key};

	decode(fr, &f, 1u);
	return f.present;
}

uint32_t counter(const char *name)
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_COUNTERS};
	struct ctag_cbor_counter items[80];
	uint16_t rid = host_send(CTAG_SERIAL_MSG_GET_COUNTERS, NULL, 0u, 1u);
	const struct frame *r;
	int n;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no GET_COUNTERS answer");
	decode(r, &f, 1u);
	zassert_true(f.present);
	n = ctag_cbor_counters(&f.v.str, items, ARRAY_SIZE(items));
	zassert_true(n > 0);
	for (int i = 0; i < n; i++) {
		if (items[i].name_len == strlen(name) &&
		    memcmp(items[i].name, name, items[i].name_len) == 0) {
			return items[i].value;
		}
	}
	zassert_unreachable("no counter %s", name);
	return 0u;
}

uint8_t request_status(uint8_t type, const struct ctag_cbor_field *f, size_t n)
{
	uint16_t rid = host_send(type, f, n, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no answer to 0x%02x", type);
	return (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
}

uint16_t send_deliver(uint64_t op_id, uint16_t bridge, uint64_t update_id, const uint8_t *layout,
		      size_t len)
{
	static const uint8_t pack[8] = {1, 2, 3, 4, 5, 6, 7, 8};
	struct ctag_cbor_field f[8] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 0xCAFE0001u),
		GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 3u),
		GW_F_UINT(CTAG_CBOR_KEY_REVISION, 18u),
		GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, update_id),
		GW_F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, pack, 8u),
		GW_F_BSTR(CTAG_CBOR_KEY_LAYOUT, layout, len),
	};

	return host_send(CTAG_SERIAL_MSG_DELIVER_LAYOUT, f, 8u, 1u);
}

uint16_t send_ack(uint32_t seq)
{
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_SEQ, seq);

	return host_send(CTAG_SERIAL_MSG_EVENT_ACK, &f, 1u, 1u);
}

/* ---- Mesh ---- */

void mesh_status(uint16_t src, uint16_t xfer_id, uint8_t status, uint32_t missing)
{
	struct ctag_mesh_layout_status st = {.xfer_id = xfer_id, .status = status, .missing = missing};
	uint8_t p[CTAG_MESH_LAYOUT_STATUS_LEN];

	(void)ctag_mesh_layout_status_pack(&st, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_LAYOUT_STATUS, p, sizeof(p), now_ms);
}

void mesh_result(uint16_t src, uint16_t result_seq, uint64_t update_id, uint8_t status)
{
	struct ctag_mesh_delivery_result r = {
		.result_seq = result_seq,
		.update_id = update_id,
		.tag_id = 0xCAFE0001u,
		.epoch = 3u,
		.revision = 18u,
		.status = status,
		.digest = {9, 8, 7, 6, 5, 4, 3, 2},
		.battery_mv = 2900u,
		.wake_ms = 11u,
		.suspend_ms = 22u,
		.transfer_ms = 33u,
		.refresh_ms = 44u,
	};
	uint8_t p[CTAG_MESH_DELIVERY_RESULT_LEN];

	(void)ctag_mesh_delivery_result_pack(&r, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_DELIVERY_RESULT, p, sizeof(p), now_ms);
}

void mesh_assign_status(uint16_t src, uint32_t tag_id, uint32_t epoch, uint8_t status)
{
	struct ctag_mesh_assign_status st = {.tag_id = tag_id, .epoch = epoch, .status = status};
	uint8_t p[CTAG_MESH_ASSIGN_STATUS_LEN];

	(void)ctag_mesh_assign_status_pack(&st, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_ASSIGN_STATUS, p, sizeof(p), now_ms);
}

uint16_t drive_to_commit(void)
{
	for (int guard = 0; guard < 400; guard++) {
		bool acted = false;

		while (send_cursor < mock.n_sent) {
			const struct sent *s = &mock.sent[send_cursor++];

			if (s->op == CTAG_MESH_OP_LAYOUT_COMMIT) {
				return ctag_get_le16(s->data);
			}
			if (s->tag != 0u) {
				end_tag(s->tag, 0);
				acted = true;
				break;
			}
		}
		if (!acted) {
			break;
		}
	}
	zassert_unreachable("the transfer never reached LAYOUT_COMMIT");
	return 0u;
}
