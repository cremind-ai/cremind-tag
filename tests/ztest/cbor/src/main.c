/*
 * ctag_cbor against the payloads of protocol/fixtures/serial_frames.json
 * (canonical encode, decode in any order) and the strictness rules of
 * companion protocol/cbor_msgs.py.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/ztest.h>

#include <ctag/ctag_cbor.h>

#include "v_serial_cbor.h"

#define MAX_FIELDS 16

static uint8_t buf[1024];

/* Decode buf against the expected fields, recursing into nested values. */
static void check_decoded(const uint8_t *data, size_t len, const struct ctag_cbor_field *want,
			  size_t n, const char *name)
{
	struct ctag_cbor_field got[MAX_FIELDS];
	size_t i, j, k;

	zassert_true(n <= MAX_FIELDS);
	for (i = 0; i < n; i++) {
		got[i].key = want[i].key;
	}
	zassert_ok(ctag_cbor_decode(data, len, got, n), "%s", name);
	for (i = 0; i < n; i++) {
		const struct ctag_cbor_field *w = &want[i];
		const struct ctag_cbor_field *g = &got[i];

		zassert_true(g->present, "%s key %u", name, (unsigned int)w->key);
		zassert_equal(g->kind, w->kind, "%s key %u", name, (unsigned int)w->key);
		switch (w->kind) {
		case CTAG_CBOR_UINT:
			zassert_equal(g->v.u, w->v.u, "%s key %u", name, (unsigned int)w->key);
			break;
		case CTAG_CBOR_INT:
			zassert_equal(g->v.i, w->v.i, "%s key %u", name, (unsigned int)w->key);
			break;
		case CTAG_CBOR_BOOL:
			zassert_equal(g->v.b, w->v.b, "%s key %u", name, (unsigned int)w->key);
			break;
		case CTAG_CBOR_BSTR:
		case CTAG_CBOR_TSTR:
			zassert_equal(g->v.str.len, w->v.str.len, "%s key %u", name,
				      (unsigned int)w->key);
			zassert_mem_equal(g->v.str.ptr, w->v.str.ptr, w->v.str.len, "%s", name);
			break;
		case CTAG_CBOR_MAP:
			check_decoded(g->v.str.ptr, g->v.str.len, w->v.map.fields, w->v.map.count,
				      name);
			break;
		case CTAG_CBOR_MAPS: {
			struct ctag_cbor_str items[4];

			zassert_equal(ctag_cbor_maps(&g->v.str, items, ARRAY_SIZE(items)),
				      (int)w->v.maps.count, "%s", name);
			for (j = 0; j < w->v.maps.count; j++) {
				check_decoded(items[j].ptr, items[j].len, w->v.maps.items[j].fields,
					      w->v.maps.items[j].count, name);
			}
			break;
		}
		default: {
			struct ctag_cbor_counter c[MAX_FIELDS];

			zassert_equal(ctag_cbor_counters(&g->v.str, c, ARRAY_SIZE(c)),
				      (int)w->v.counters.count, "%s", name);
			for (j = 0; j < w->v.counters.count; j++) {
				const struct ctag_cbor_counter *e = &w->v.counters.items[j];

				for (k = 0; k < w->v.counters.count; k++) {
					if (c[k].name_len == e->name_len &&
					    memcmp(c[k].name, e->name, e->name_len) == 0) {
						break;
					}
				}
				zassert_true(k < w->v.counters.count, "%s %s", name, e->name);
				zassert_equal(c[k].value, e->value, "%s %s", name, e->name);
			}
			break;
		}
		}
	}
}

ZTEST(ctag_cbor, test_encode_fixture_payloads)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(v_cbor_frames); i++) {
		const struct v_cbor_frame *v = &v_cbor_frames[i];
		int n = ctag_cbor_encode(v->fields, v->count, buf, sizeof(buf));

		zassert_equal(n, (int)v->len, "%s", v->name);
		zassert_mem_equal(buf, v->payload, v->len, "%s", v->name);
		if (v->len > 0) {
			zassert_equal(ctag_cbor_encode(v->fields, v->count, buf, v->len - 1),
				      -EMSGSIZE, "%s", v->name);
		}
	}
}

ZTEST(ctag_cbor, test_decode_fixture_payloads)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(v_cbor_frames); i++) {
		const struct v_cbor_frame *v = &v_cbor_frames[i];

		check_decoded(v->payload, v->len, v->fields, v->count, v->name);
	}
}

ZTEST(ctag_cbor, test_encode_order_independent)
{
	const struct v_cbor_frame *v = &v_cbor_frames[4]; /* deliver layout request */
	struct ctag_cbor_field rev[MAX_FIELDS];
	size_t i;

	zassert_true(v->count <= MAX_FIELDS && v->count > 2);
	for (i = 0; i < v->count; i++) {
		rev[i] = v->fields[v->count - 1 - i];
	}
	zassert_equal(ctag_cbor_encode(rev, v->count, buf, sizeof(buf)), (int)v->len);
	zassert_mem_equal(buf, v->payload, v->len);
}

ZTEST(ctag_cbor, test_array_of_maps)
{
	static const uint8_t uuid[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
	const struct ctag_cbor_field node0[] = {
		{.key = CTAG_CBOR_KEY_NAME,
		 .kind = CTAG_CBOR_TSTR,
		 .v.str = {(const uint8_t *)"b1", 2}},
		{.key = CTAG_CBOR_KEY_ADDR, .kind = CTAG_CBOR_UINT, .v.u = 2},
		{.key = CTAG_CBOR_KEY_UUID, .kind = CTAG_CBOR_BSTR, .v.str = {uuid, 16}},
		{.key = CTAG_CBOR_KEY_CONFIGURED, .kind = CTAG_CBOR_BOOL, .v.b = true},
	};
	const struct ctag_cbor_field node1[] = {
		{.key = CTAG_CBOR_KEY_ADDR, .kind = CTAG_CBOR_UINT, .v.u = 0xFFFF},
		{.key = CTAG_CBOR_KEY_CONFIGURED, .kind = CTAG_CBOR_BOOL, .v.b = false},
	};
	const struct ctag_cbor_map nodes[] = {{node0, ARRAY_SIZE(node0)},
					      {node1, ARRAY_SIZE(node1)}};
	const struct ctag_cbor_field msg[] = {
		{.key = CTAG_CBOR_KEY_NODES, .kind = CTAG_CBOR_MAPS, .v.maps = {nodes, 2}},
		{.key = CTAG_CBOR_KEY_STATUS, .kind = CTAG_CBOR_UINT, .v.u = 0},
		{.key = CTAG_CBOR_KEY_RSSI, .kind = CTAG_CBOR_INT, .v.i = -70},
	};
	/* {0: 0, 12: -70, 22: [{2: 2, 3: h'01..10', 24: "b1", 43: true}, {2: 65535, 43: false}]} */
	static const uint8_t expect[] = {
		0xa3, 0x00, 0x00, 0x0c, 0x38, 0x45, 0x16, 0x82, 0xa4, 0x02, 0x02, 0x03,
		0x50, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b,
		0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x18, 0x18, 0x62, 0x62, 0x31, 0x18, 0x2b,
		0xf5, 0xa2, 0x02, 0x19, 0xff, 0xff, 0x18, 0x2b, 0xf4,
	};
	int n = ctag_cbor_encode(msg, ARRAY_SIZE(msg), buf, sizeof(buf));

	zassert_equal(n, (int)sizeof(expect));
	zassert_mem_equal(buf, expect, sizeof(expect));
	check_decoded(buf, (size_t)n, msg, ARRAY_SIZE(msg), "nodes");
}

ZTEST(ctag_cbor, test_encode_rejects)
{
	static const uint8_t short_id[7];
	const struct ctag_cbor_counter dup_names[] = {CTAG_CBOR_COUNTER("a", 1),
						      CTAG_CBOR_COUNTER("a", 2)};
	const struct ctag_cbor_field dup[] = {
		{.key = CTAG_CBOR_KEY_STATUS, .kind = CTAG_CBOR_UINT},
		{.key = CTAG_CBOR_KEY_STATUS, .kind = CTAG_CBOR_UINT},
	};
	const struct ctag_cbor_field bad[][1] = {
		{{.key = CTAG_CBOR_KEY_STATUS, .kind = CTAG_CBOR_BSTR}},
		{{.key = 62, .kind = CTAG_CBOR_UINT}},
		{{.key = CTAG_CBOR_KEY_ADDR, .kind = CTAG_CBOR_UINT, .v.u = 0x10000}},
		{{.key = CTAG_CBOR_KEY_STATUS, .kind = CTAG_CBOR_UINT, .v.u = 0x100000000ull}},
		{{.key = CTAG_CBOR_KEY_RSSI, .kind = CTAG_CBOR_INT, .v.i = INT32_MIN - 1ll}},
		{{.key = CTAG_CBOR_KEY_FONTPACK_ID,
		  .kind = CTAG_CBOR_BSTR,
		  .v.str = {short_id, 7}}},
		{{.key = CTAG_CBOR_KEY_COUNTERS,
		  .kind = CTAG_CBOR_COUNTERS,
		  .v.counters = {dup_names, 2}}},
	};
	size_t i;

	zassert_equal(ctag_cbor_encode(dup, ARRAY_SIZE(dup), buf, sizeof(buf)), -EINVAL);
	for (i = 0; i < ARRAY_SIZE(bad); i++) {
		zassert_equal(ctag_cbor_encode(bad[i], 1, buf, sizeof(buf)), -EINVAL, "case %u",
			      (unsigned int)i);
	}
	zassert_equal(ctag_cbor_encode(NULL, 0, buf, sizeof(buf)), 0);
}

struct raw {
	const char *name;
	uint8_t len;
	uint8_t data[24];
};

#define RAW(n, ...)                                                                                \
	{                                                                                          \
		n, sizeof((uint8_t[]){__VA_ARGS__}),                                               \
		{                                                                                  \
			__VA_ARGS__                                                                \
		}                                                                                  \
	}

static const struct raw rejected[] = {
	RAW("key not shortest", 0xa1, 0x18, 0x05, 0x01),
	RAW("value not shortest", 0xa1, 0x00, 0x18, 0x01),
	RAW("uint16 not shortest", 0xa1, 0x00, 0x19, 0x00, 0x10),
	RAW("indefinite map", 0xbf, 0x00, 0x00, 0xff),
	RAW("indefinite bstr", 0xa1, 0x18, 0x40, 0x5f, 0x41, 0x00, 0xff),
	RAW("tag", 0xa1, 0x00, 0xc1, 0x00),
	RAW("float", 0xa1, 0x18, 0x40, 0xf9, 0x00, 0x00),
	RAW("null", 0xa1, 0x18, 0x40, 0xf6),
	RAW("text key", 0xa1, 0x61, 0x61, 0x01),
	RAW("negative key", 0xa1, 0x20, 0x01),
	RAW("duplicate key", 0xa2, 0x00, 0x00, 0x00, 0x01),
	RAW("duplicate unknown key", 0xa2, 0x18, 0x40, 0x00, 0x18, 0x40, 0x01),
	RAW("text not UTF-8", 0xa1, 0x18, 0x18, 0x62, 0xc3, 0x28),
	RAW("trailing byte", 0xa0, 0x00),
	RAW("truncated", 0xa1, 0x00),
	RAW("not a map", 0x80),
	RAW("status as bstr", 0xa1, 0x00, 0x41, 0x00),
	RAW("addr above u16", 0xa1, 0x02, 0x1a, 0x00, 0x01, 0x00, 0x00),
	RAW("uuid of 15 bytes", 0xa1, 0x03, 0x4f, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
	RAW("rssi above i32", 0xa1, 0x0c, 0x1a, 0x80, 0x00, 0x00, 0x00),
	RAW("relay as uint", 0xa1, 0x0f, 0x01),
	RAW("caps with a text key", 0xa1, 0x15, 0xa1, 0x61, 0x61, 0x00),
	RAW("counters with a uint key", 0xa1, 0x18, 0x19, 0xa1, 0x00, 0x00),
	RAW("nested too deep", 0xa1, 0x18, 0x40, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,
	    0x81, 0x00),
};

static const struct raw accepted[] = {
	RAW("empty map", 0xa0),
	RAW("unknown key skipped", 0xa2, 0x00, 0x01, 0x18, 0x63, 0x62, 0x68, 0x69),
	RAW("keys in any order", 0xa2, 0x05, 0x03, 0x00, 0x01),
	RAW("64-bit unknown key", 0xa2, 0x1b, 0, 0, 0, 1, 0, 0, 0, 0, 0x00, 0x00, 0x01),
	RAW("nested to depth 8", 0xa2, 0x00, 0x01, 0x18, 0x40, 0x81, 0x81, 0x81, 0x81, 0x81, 0x81,
	    0x81, 0x00),
};

ZTEST(ctag_cbor, test_decode_strictness)
{
	struct ctag_cbor_field f[2] = {{.key = CTAG_CBOR_KEY_STATUS}, {.key = CTAG_CBOR_KEY_EPOCH}};
	size_t i;

	for (i = 0; i < ARRAY_SIZE(rejected); i++) {
		zassert_equal(ctag_cbor_decode(rejected[i].data, rejected[i].len, f, 2), -EBADMSG,
			      "%s", rejected[i].name);
	}
	for (i = 0; i < ARRAY_SIZE(accepted); i++) {
		zassert_ok(ctag_cbor_decode(accepted[i].data, accepted[i].len, f, 2), "%s",
			   accepted[i].name);
	}
	zassert_ok(ctag_cbor_decode(accepted[2].data, accepted[2].len, f, 2));
	zassert_true(f[0].present && f[0].v.u == 1 && f[1].present && f[1].v.u == 3);
	zassert_equal(f[1].kind, CTAG_CBOR_UINT);
	zassert_ok(ctag_cbor_decode(NULL, 0, f, 2));
	zassert_false(f[0].present || f[1].present);
	zassert_equal(ctag_cbor_key_kind(CTAG_CBOR_KEY_CAPS), CTAG_CBOR_MAP);
	zassert_equal(ctag_cbor_key_kind(62), 0);
}

ZTEST_SUITE(ctag_cbor, NULL, NULL, NULL, NULL, NULL);
