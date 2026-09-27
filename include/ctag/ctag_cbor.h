/*
 * CBOR payloads of serial frames (docs/protocol.md 1.1-1.2) for the gateway
 * and the bridge maintenance port; Zephyr + zcbor (CONFIG_ZCBOR_CANONICAL).
 * Mirrors companion protocol/cbor_msgs.py: one map keyed by
 * enum ctag_cbor_key, canonical on encode (keys ascending, shortest forms,
 * definite lengths), any key order on decode, unknown keys skipped, value
 * kinds of known keys checked, nested maps (caps, timing, entries of nodes,
 * items, assigned) use the same keys, counters maps text -> uint32.
 */
#ifndef CTAG_CBOR_H_
#define CTAG_CBOR_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_ids.h>

#ifdef __cplusplus
extern "C" {
#endif

enum ctag_cbor_kind {
	CTAG_CBOR_UINT = 1, /* v.u (uint16/32/64 per key) */
	CTAG_CBOR_INT,      /* v.i (int32) */
	CTAG_CBOR_BOOL,     /* v.b */
	CTAG_CBOR_BSTR,     /* v.str */
	CTAG_CBOR_TSTR,     /* v.str (UTF-8, not NUL-terminated) */
	CTAG_CBOR_MAP,      /* encode v.map; decode v.str = the encoded map */
	CTAG_CBOR_MAPS,     /* encode v.maps; decode v.str = the encoded array */
	CTAG_CBOR_COUNTERS, /* encode v.counters; decode v.str = the encoded map */
};

struct ctag_cbor_str {
	const uint8_t *ptr;
	size_t len;
};

struct ctag_cbor_field;

struct ctag_cbor_map {
	const struct ctag_cbor_field *fields;
	size_t count;
};

struct ctag_cbor_counter {
	const char *name;
	size_t name_len;
	uint32_t value;
};

#define CTAG_CBOR_COUNTER(lit, val) {.name = (lit), .name_len = sizeof(lit) - 1u, .value = (val)}

struct ctag_cbor_field {
	uint8_t key;  /* enum ctag_cbor_key */
	uint8_t kind; /* enum ctag_cbor_kind; set by ctag_cbor_decode() */
	bool present; /* set by ctag_cbor_decode() */
	union {
		uint64_t u;
		int64_t i;
		bool b;
		struct ctag_cbor_str str;
		struct ctag_cbor_map map;
		struct {
			const struct ctag_cbor_map *items;
			size_t count;
		} maps;
		struct {
			const struct ctag_cbor_counter *items;
			size_t count;
		} counters;
	} v;
};

/* Kind of a known key, or 0 for an unknown key. */
uint8_t ctag_cbor_key_kind(uint32_t key);

/*
 * Canonical encoding of fields (any order; each key once, kind and range as
 * the key requires). An empty set is an empty payload. Returns the length,
 * -EINVAL for a bad field or -EMSGSIZE.
 */
int ctag_cbor_encode(const struct ctag_cbor_field *fields, size_t count, uint8_t *buf, size_t size);

/*
 * Decode a payload map: the caller sets the key of each wanted field; kind,
 * present and v are filled. The whole payload is checked as cbor_msgs.py does
 * (well-formed, canonical integers and lengths, no tags/floats, UTF-8 text,
 * no duplicate keys, known keys well-typed). Returns 0 or -EBADMSG. Nested
 * values point into buf.
 */
int ctag_cbor_decode(const uint8_t *buf, size_t len, struct ctag_cbor_field *fields, size_t count);

/* Split a decoded CTAG_CBOR_MAPS value into its maps; returns the count or -ENOSPC/-EBADMSG. */
int ctag_cbor_maps(const struct ctag_cbor_str *maps, struct ctag_cbor_str *items, size_t max);

/* Entries of a decoded CTAG_CBOR_COUNTERS value; returns the count or -ENOSPC/-EBADMSG. */
int ctag_cbor_counters(const struct ctag_cbor_str *counters, struct ctag_cbor_counter *items,
		       size_t max);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_CBOR_H_ */
