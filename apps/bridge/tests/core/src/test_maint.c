/*
 * Maintenance port as the companion's bridge_maint/client.py drives it:
 * framing, HELLO and credits (docs/protocol.md 1.1-1.3, 10), font
 * installation (docs/fontpack.md 4), FLASH_TEST idempotency, REBOOT.
 */
#include <string.h>

#include <zephyr/sys/printk.h>

#include <ctag/ctag_cbor.h>
#include <ctag/ctag_frame.h>

#include "common.h"
#include "maint.h"

static struct maint mt;
static uint8_t out[16384];
static size_t out_len;
static bool rebooted;
static uint16_t rid;

static void io_write(void *ctx, const uint8_t *d, size_t n)
{
	zassert_true(out_len + n <= sizeof(out));
	memcpy(&out[out_len], d, n);
	out_len += n;
}

static void io_reboot(void *ctx)
{
	rebooted = true;
}

static bool many_counters;
static char many_names[MAINT_COUNTERS][24];

static size_t io_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	const struct ctag_cbor_counter c = CTAG_CBOR_COUNTER("sessions_ok", 3);
	size_t n = 1;

	zassert_true(max > 0);
	items[0] = c;
	/* Worst case: every slot, long names, values that need five bytes. */
	for (; many_counters && n < max; n++) {
		snprintk(many_names[n], sizeof(many_names[n]), "counter_%02u_with_long_name",
			 (unsigned int)n);
		items[n] = (struct ctag_cbor_counter){many_names[n], strlen(many_names[n]),
						      UINT32_MAX};
	}
	return n;
}

static const struct maint_io io = {.write = io_write, .reboot = io_reboot, .counters = io_counters};

/* ---- Host side ---- */

static uint16_t send_frame(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits)
{
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	static uint8_t wire[CTAG_SERIAL_MAX_ENCODED];
	struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION, .type = type,
				       .request_id = ++rid, .credits = credits};
	int len = ctag_cbor_encode(f, n, &frame[CTAG_SERIAL_HEADER_LEN],
				   sizeof(frame) - CTAG_SERIAL_MIN_FRAME);

	zassert_true(len >= 0);
	len = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, (size_t)len);
	zassert_true(len > 0);
	len = ctag_serial_wire_encode(frame, (size_t)len, wire, sizeof(wire));
	zassert_true(len > 0);
	maint_rx(&mt, wire, (size_t)len);
	return h.request_id;
}

struct resp {
	struct ctag_serial_header h;
	uint8_t payload[2048];
};

static struct resp resp[80];

/* Decode every frame the device wrote since the last call. */
static size_t responses(void)
{
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	size_t n = 0, start = 0;

	for (size_t i = 0; i < out_len; i++) {
		if (out[i] != 0) {
			continue;
		}
		int len = ctag_cobs_decode(&out[start], i - start, frame, sizeof(frame));

		zassert_true(len > 0);
		zassert_equal(ctag_serial_frame_check(frame, (size_t)len, &resp[n].h),
			      CTAG_SERIAL_CHECK_OK);
		zassert_true(resp[n].h.length <= sizeof(resp[n].payload));
		memcpy(resp[n].payload, &frame[CTAG_SERIAL_HEADER_LEN], resp[n].h.length);
		zassert_true(n + 1 < ARRAY_SIZE(resp));
		n++;
		start = i + 1;
	}
	zassert_equal(start, out_len, "a partial frame");
	out_len = 0;
	return n;
}

enum { K_STATUS, K_DETAIL, K_TEXT, K_SLOT, K_FLASH, K_ID, K_SIZE, K_ITEMS, K_CAPS, K_COUNTERS,
       K_PROTO, K_BOOT, K_FW, K_UPTIME, K_N };

static struct ctag_cbor_field fields[K_N];

static void decode(const struct resp *r)
{
	static const uint8_t keys[K_N] = {
		CTAG_CBOR_KEY_STATUS, CTAG_CBOR_KEY_DETAIL, CTAG_CBOR_KEY_TEXT,
		CTAG_CBOR_KEY_SLOT, CTAG_CBOR_KEY_FLASH_SIZE, CTAG_CBOR_KEY_FONTPACK_ID,
		CTAG_CBOR_KEY_SIZE, CTAG_CBOR_KEY_ITEMS, CTAG_CBOR_KEY_CAPS,
		CTAG_CBOR_KEY_COUNTERS, CTAG_CBOR_KEY_PROTO, CTAG_CBOR_KEY_BOOT_ID,
		CTAG_CBOR_KEY_FW, CTAG_CBOR_KEY_UPTIME_S,
	};

	memset(fields, 0, sizeof(fields));
	for (size_t i = 0; i < K_N; i++) {
		fields[i].key = keys[i];
	}
	zassert_ok(ctag_cbor_decode(r->payload, r->h.length, fields, K_N));
	zassert_true(fields[K_STATUS].present, "status is always present");
}

/* One request, exactly one response: its decoded fields. */
static void call(uint8_t type, const struct ctag_cbor_field *f, size_t n)
{
	uint16_t id = send_frame(type, f, n, 1);

	zassert_equal(responses(), 1);
	zassert_equal(resp[0].h.type, type);
	zassert_equal(resp[0].h.request_id, id);
	zassert_equal(resp[0].h.flags, CTAG_SERIAL_FLAG_RESPONSE);
	decode(&resp[0]);
}

static void hello(uint8_t credits)
{
	struct ctag_cbor_field f[2] = {
		{.key = CTAG_CBOR_KEY_PROTO, .kind = CTAG_CBOR_UINT, .v.u = CTAG_PROTO_VERSION},
		{.key = CTAG_CBOR_KEY_NAME, .kind = CTAG_CBOR_TSTR, .v.str = {(const uint8_t *)"t", 1}},
	};

	send_frame(CTAG_SERIAL_MSG_HELLO, f, 2, credits);
	zassert_equal(responses(), 1);
	decode(&resp[0]);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
}

static void before(void *f)
{
	env_fresh(0u);
	maint_init(&mt, &io, NULL, &benv.fonts, &test_sha, 0xB007u, "0.1.0", "test",
		   CTAG_BOARD_NRF52840_BRIDGE);
	out_len = 0;
	rebooted = false;
}

ZTEST(bridge_maint, test_hello_caps_and_credits)
{
	struct ctag_cbor_field caps[4] = {{.key = CTAG_CBOR_KEY_MAX_FRAME},
					  {.key = CTAG_CBOR_KEY_CREDITS},
					  {.key = CTAG_CBOR_KEY_ROLE},
					  {.key = CTAG_CBOR_KEY_BOARD}};

	/* Nothing is answered before a HELLO. */
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 1);
	zassert_equal(responses(), 0);
	zassert_equal(mt.c.overruns, 1);
	/* HELLO: exempt from credits; caps as the client reads them. */
	hello(0);
	zassert_equal(resp[0].h.credits, 0);
	zassert_equal(fields[K_PROTO].v.u, CTAG_PROTO_VERSION);
	zassert_equal(fields[K_BOOT].v.u, 0xB007u);
	zassert_equal(fields[K_FW].v.str.len, 5);
	zassert_ok(ctag_cbor_decode(fields[K_CAPS].v.str.ptr, fields[K_CAPS].v.str.len, caps, 4));
	zassert_equal(caps[0].v.u, CONFIG_CTAG_BRIDGE_MAINT_MAX_FRAME);
	zassert_equal(caps[1].v.u, CONFIG_CTAG_BRIDGE_MAINT_CREDITS);
	zassert_equal(caps[2].v.u, CTAG_NODE_ROLE_BRIDGE);
	zassert_equal(caps[3].v.u, CTAG_BOARD_NRF52840_BRIDGE);
	/* HELLO granted nothing: SERIAL_DEFAULT_CREDITS answers, then one waits. */
	for (int i = 0; i < CTAG_SERIAL_DEFAULT_CREDITS; i++) {
		send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 0);
		zassert_equal(responses(), 1);
		zassert_equal(resp[0].h.credits, 1, "the processed frame's buffer is granted back");
		decode(&resp[0]);
		zassert_true(fields[K_UPTIME].present);
	}
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 0);
	zassert_equal(responses(), 0, "no credit: the answer waits");
	/* A grant releases it, then the new request is answered too. */
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 2);
	zassert_equal(responses(), 2);
	/* A new HELLO resets the session and drops what was not sent. */
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 0);
	zassert_equal(responses(), 0);
	hello(60);
	zassert_equal(mt.held, 0);
	for (int i = 0; i < 64; i++) {
		send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 0);
	}
	zassert_equal(responses(), 64, "SERIAL_DEFAULT_CREDITS + the HELLO grant");
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 0);
	zassert_equal(responses(), 0);
	/* Each answered frame granted its buffer back: no overrun counted, except
	 * with a single credit, where the PING sent to release the held answer
	 * above already exceeded the host's budget. */
	zassert_equal(mt.c.credit_violations, CONFIG_CTAG_BRIDGE_MAINT_CREDITS > 1 ? 0 : 1);
}

ZTEST(bridge_maint, test_version_mismatch)
{
	struct ctag_cbor_field f[1] = {
		{.key = CTAG_CBOR_KEY_PROTO, .kind = CTAG_CBOR_UINT, .v.u = 2},
	};

	send_frame(CTAG_SERIAL_MSG_HELLO, f, 1, 0);
	zassert_equal(responses(), 1);
	decode(&resp[0]);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_VERSION_MISMATCH);
	zassert_equal(fields[K_PROTO].v.u, CTAG_PROTO_VERSION);
	send_frame(CTAG_SERIAL_MSG_PING, NULL, 0, 1);
	zassert_equal(responses(), 0, "no session after a refused HELLO");
}

ZTEST(bridge_maint, test_unsupported_and_malformed)
{
	uint8_t wire[64];
	uint8_t frame[32];
	struct ctag_serial_header h = {.version = 1, .type = CTAG_SERIAL_MSG_PING, .request_id = 9,
				       .credits = 1};
	struct ctag_cbor_field layout[1] = {
		{.key = CTAG_CBOR_KEY_OP_ID, .kind = CTAG_CBOR_UINT, .v.u = 5},
	};
	int n;

	hello(60);
	/* Unknown types and mesh/delivery requests (1.6). */
	call(0x7F, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_UNSUPPORTED);
	call(CTAG_SERIAL_MSG_DELIVER_LAYOUT, layout, 1);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_UNSUPPORTED);
	call(CTAG_SERIAL_MSG_SCAN_UNPROV, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_UNSUPPORTED);
	/* A required field missing. */
	call(CTAG_SERIAL_MSG_FONT_BEGIN, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_INVALID);
	/* Malformed CBOR (indefinite-length map). */
	frame[CTAG_SERIAL_HEADER_LEN] = 0xBF;
	frame[CTAG_SERIAL_HEADER_LEN + 1] = 0xFF;
	n = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, 2);
	n = ctag_serial_wire_encode(frame, (size_t)n, wire, sizeof(wire));
	maint_rx(&mt, wire, (size_t)n);
	zassert_equal(responses(), 1);
	decode(&resp[0]);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_INVALID);
	zassert_true(fields[K_TEXT].present);
	/* A corrupted frame is dropped and counted (crc_errors). */
	h.request_id = 10;
	n = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, 0);
	frame[3] ^= 1;
	n = ctag_serial_wire_encode(frame, (size_t)n, wire, sizeof(wire));
	maint_rx(&mt, wire, (size_t)n);
	zassert_equal(responses(), 0);
	zassert_equal(mt.rx.crc_errors, 1);
}

ZTEST(bridge_maint, test_font_install)
{
	uint8_t digest[32];
	struct ctag_cbor_field begin[3] = {
		{.key = CTAG_CBOR_KEY_SIZE, .kind = CTAG_CBOR_UINT, .v.u = fixture_pack_len},
		{.key = CTAG_CBOR_KEY_DIGEST, .kind = CTAG_CBOR_BSTR, .v.str = {digest, 32}},
		{.key = CTAG_CBOR_KEY_FONTPACK_ID, .kind = CTAG_CBOR_BSTR,
		 .v.str = {fixture_pack_id, 8}},
	};
	struct ctag_cbor_field data[2] = {
		{.key = CTAG_CBOR_KEY_OFFSET, .kind = CTAG_CBOR_UINT},
		{.key = CTAG_CBOR_KEY_DATA, .kind = CTAG_CBOR_BSTR},
	};
	struct ctag_cbor_counter counters[MAINT_COUNTERS];
	/* The client's chunk: what caps.max_frame leaves (bridge_maint/client.py). */
	const size_t chunk = MIN(2048u, CONFIG_CTAG_BRIDGE_MAINT_MAX_FRAME - 8u - 4u - 24u);
	int nc;

	sha256(fixture_pack, fixture_pack_len, digest);
	hello(60);
	/* The client's sequence (bridge_maint/client.py font_install). */
	call(CTAG_SERIAL_MSG_FONT_STATUS, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	zassert_false(fields[K_ID].present, "no pack yet");
	zassert_equal(fields[K_FLASH].v.u, 32u * MIB);
	call(CTAG_SERIAL_MSG_FONT_ABORT, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	call(CTAG_SERIAL_MSG_FONT_BEGIN, begin, 3);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	zassert_equal(fields[K_SLOT].v.u, 0);
	zassert_equal(fields[K_FLASH].v.u, 32u * MIB);
	/* Out of sequence: INVALID with a text. */
	data[0].v.u = 5;
	data[1].v.str = (struct ctag_cbor_str){fixture_pack, 10};
	call(CTAG_SERIAL_MSG_FONT_DATA, data, 2);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_INVALID);
	zassert_true(fields[K_TEXT].present);
	/* A frame above caps.max_frame is dropped and counted. */
	if (CONFIG_CTAG_BRIDGE_MAINT_MAX_FRAME + 64 <= CTAG_SERIAL_MAX_FRAME) {
		data[0].v.u = 0;
		data[1].v.str = (struct ctag_cbor_str){fixture_pack, CONFIG_CTAG_BRIDGE_MAINT_MAX_FRAME};
		(void)send_frame(CTAG_SERIAL_MSG_FONT_DATA, data, 2, 1);
		zassert_equal(responses(), 0);
		zassert_equal(mt.rx.cobs.oversize, 1);
	}
	for (size_t off = 0; off < fixture_pack_len; off += chunk) {
		data[0].v.u = off;
		data[1].v.str = (struct ctag_cbor_str){&fixture_pack[off],
						       MIN(chunk, fixture_pack_len - off)};
		call(CTAG_SERIAL_MSG_FONT_DATA, data, 2);
		zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	}
	call(CTAG_SERIAL_MSG_FONT_COMMIT, NULL, 0);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	zassert_mem_equal(fields[K_ID].v.str.ptr, fixture_pack_id, 8);
	call(CTAG_SERIAL_MSG_FONT_STATUS, NULL, 0);
	zassert_mem_equal(fields[K_ID].v.str.ptr, fixture_pack_id, 8);
	zassert_equal(fields[K_SLOT].v.u, 0);
	zassert_equal(fields[K_SIZE].v.u, fixture_pack_len);
	/* INFO: fw, caps and every counter, the bridge's included. */
	call(CTAG_SERIAL_MSG_INFO, NULL, 0);
	zassert_true(fields[K_CAPS].present);
	nc = ctag_cbor_counters(&fields[K_COUNTERS].v.str, counters, ARRAY_SIZE(counters));
	zassert_true(nc > 10);
	bool installed = false, sessions = false;

	for (int i = 0; i < nc; i++) {
		if (counters[i].name_len == 15 && memcmp(counters[i].name, "fonts_installed", 15) == 0) {
			installed = counters[i].value == 1;
		}
		if (counters[i].name_len == 11 && memcmp(counters[i].name, "sessions_ok", 11) == 0) {
			sessions = counters[i].value == 3;
		}
	}
	zassert_true(installed && sessions);
	/* However many counters there are, INFO answers within MAINT_TX_FRAME. */
	many_counters = true;
	call(CTAG_SERIAL_MSG_INFO, NULL, 0);
	many_counters = false;
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	nc = ctag_cbor_counters(&fields[K_COUNTERS].v.str, counters, ARRAY_SIZE(counters));
	zassert_true(nc > 20 && nc <= (int)MAINT_COUNTERS, "%d counters", nc);
}

ZTEST(bridge_maint, test_flash_test_idempotent)
{
	struct ctag_cbor_field f[1] = {
		{.key = CTAG_CBOR_KEY_OP_ID, .kind = CTAG_CBOR_UINT, .v.u = 0x1122334455667788ull},
	};
	struct ctag_cbor_str items[8];
	int n;

	hello(60);
	call(CTAG_SERIAL_MSG_FLASH_TEST, f, 1);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	zassert_equal(fields[K_FLASH].v.u, 32u * MIB);
	zassert_false(fields[K_DETAIL].present);
	n = ctag_cbor_maps(&fields[K_ITEMS].v.str, items, ARRAY_SIZE(items));
	zassert_equal(n, 5);
	for (int i = 0; i < n; i++) {
		struct ctag_cbor_field it[2] = {{.key = CTAG_CBOR_KEY_OFFSET},
						{.key = CTAG_CBOR_KEY_STATUS}};

		zassert_ok(ctag_cbor_decode(items[i].ptr, items[i].len, it, 2));
		zassert_true(it[0].present && it[1].present);
		if (it[0].v.u == 16u * MIB) {
			zassert_equal(it[1].v.u, CTAG_STATUS_BUSY, "the directory is never touched");
		} else {
			zassert_equal(it[1].v.u, CTAG_STATUS_OK);
		}
	}
	/* The same op_id: the remembered result, no new work (1.4). */
	uint32_t erases = benv.flash.errors;

	call(CTAG_SERIAL_MSG_FLASH_TEST, f, 1);
	zassert_equal(fields[K_DETAIL].v.u, CTAG_STATUS_DUPLICATE);
	zassert_equal(ctag_cbor_maps(&fields[K_ITEMS].v.str, items, ARRAY_SIZE(items)), 5);
	zassert_equal(benv.flash.errors, erases);
	f[0].v.u++;
	call(CTAG_SERIAL_MSG_FLASH_TEST, f, 1);
	zassert_false(fields[K_DETAIL].present);
}

ZTEST(bridge_maint, test_reboot_answers_first)
{
	struct ctag_cbor_field f[1] = {
		{.key = CTAG_CBOR_KEY_OP_ID, .kind = CTAG_CBOR_UINT, .v.u = 1},
	};

	hello(60);
	call(CTAG_SERIAL_MSG_REBOOT, f, 1);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
	zassert_true(rebooted);
	call(CTAG_SERIAL_MSG_EVENT_ACK, (struct ctag_cbor_field[]){
		{.key = CTAG_CBOR_KEY_SEQ, .kind = CTAG_CBOR_UINT, .v.u = 3}}, 1);
	zassert_equal(fields[K_STATUS].v.u, CTAG_STATUS_OK);
}

/* The streaming encoder emits exactly ctag_serial_wire_encode()'s bytes. */
ZTEST(bridge_maint, test_cobs_writer_matches_library)
{
	static uint8_t wire[CTAG_COBS_MAX_ENCODED(sizeof(mt.tx)) + 1u];
	static const size_t lens[] = {0, 1, 12, 253, 254, 255, 256, 508, 509, 510, 700,
				      sizeof(mt.tx)};
	uint32_t seed = 1;

	for (size_t k = 0; k < ARRAY_SIZE(lens); k++) {
		for (int pattern = 0; pattern < 4; pattern++) {
			size_t len = MIN(lens[k], sizeof(mt.tx));
			int n;

			for (size_t i = 0; i < len; i++) {
				seed = seed * 1103515245u + 12345u;
				/* 0: no zeros; 1: sparse zeros; 2: zeros at the ends; 3: all zero */
				mt.tx[i] = pattern == 3 ? 0
					   : pattern == 1 && (seed >> 24) < 8 ? 0
					   : (uint8_t)(1u + (seed >> 24) % 255u);
			}
			if (pattern == 2 && len > 0) {
				mt.tx[0] = 0;
				mt.tx[len - 1] = 0;
			}
			out_len = 0;
			maint_write_frame(&mt, len);
			n = ctag_serial_wire_encode(mt.tx, len, wire, sizeof(wire));
			zassert_true(n > 0);
			zassert_equal(out_len, (size_t)n, "len %zu pattern %d", len, pattern);
			zassert_mem_equal(out, wire, (size_t)n, "len %zu pattern %d", len, pattern);
		}
	}
	out_len = 0;
}

ZTEST_SUITE(bridge_maint, NULL, NULL, before, NULL, NULL);
