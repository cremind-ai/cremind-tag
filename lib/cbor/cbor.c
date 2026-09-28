/* Serial CBOR maps (docs/protocol.md 1.1); mirrors companion protocol/cbor_msgs.py. */
#include <errno.h>
#include <string.h>

#include <zcbor_decode.h>
#include <zcbor_encode.h>

#include <ctag/ctag_cbor.h>
#include <ctag/ctag_utf8.h>

#ifndef ZCBOR_CANONICAL
#error "ctag_cbor needs CONFIG_ZCBOR_CANONICAL (definite-length maps)"
#endif

#define MAX_DEPTH 8u /* cbor_msgs.py MAX_DEPTH */
#define ENC_DEPTH 4u /* map -> array of maps -> map -> counters map */

/* Value limits, low nibble of the key table. */
enum limit {
	L_NONE,
	L_U16,
	L_U32,
	L_U64,
	L_I32,
	L_EQ8,
	L_EQ16,
	L_MAX16,
	L_MAX32,
	L_PAYLOAD,
	L_LAYOUT,
	L_EQ32,
	L_EQ64,
	L_GRANT,
};

#define K(kind, limit) (uint8_t)((kind) << 4 | (limit))
#define U32            K(CTAG_CBOR_UINT, L_U32)

/* Kinds from the comments of spec serial.cbor_keys (cbor_msgs.py KEYS). */
static const uint8_t key_spec[] = {
	[CTAG_CBOR_KEY_STATUS] = U32,
	[CTAG_CBOR_KEY_OP_ID] = K(CTAG_CBOR_UINT, L_U64),
	[CTAG_CBOR_KEY_ADDR] = K(CTAG_CBOR_UINT, L_U16),
	[CTAG_CBOR_KEY_UUID] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_TAG_ID] = U32,
	[CTAG_CBOR_KEY_EPOCH] = U32,
	[CTAG_CBOR_KEY_REVISION] = U32,
	[CTAG_CBOR_KEY_UPDATE_ID] = K(CTAG_CBOR_UINT, L_U64),
	[CTAG_CBOR_KEY_FONTPACK_ID] = K(CTAG_CBOR_BSTR, L_EQ8),
	[CTAG_CBOR_KEY_LAYOUT] = K(CTAG_CBOR_BSTR, L_LAYOUT),
	[CTAG_CBOR_KEY_STAGE] = U32,
	[CTAG_CBOR_KEY_DETAIL] = U32,
	[CTAG_CBOR_KEY_RSSI] = K(CTAG_CBOR_INT, L_I32),
	[CTAG_CBOR_KEY_OOB] = U32,
	[CTAG_CBOR_KEY_DURATION_S] = U32,
	[CTAG_CBOR_KEY_RELAY] = K(CTAG_CBOR_BOOL, L_NONE),
	[CTAG_CBOR_KEY_TTL] = U32,
	[CTAG_CBOR_KEY_KEY] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_FW] = K(CTAG_CBOR_TSTR, L_NONE),
	[CTAG_CBOR_KEY_BUILD] = K(CTAG_CBOR_TSTR, L_NONE),
	[CTAG_CBOR_KEY_BOOT_ID] = U32,
	[CTAG_CBOR_KEY_CAPS] = K(CTAG_CBOR_MAP, L_NONE),
	[CTAG_CBOR_KEY_NODES] = K(CTAG_CBOR_MAPS, L_NONE),
	[CTAG_CBOR_KEY_ELEMENTS] = U32,
	[CTAG_CBOR_KEY_NAME] = K(CTAG_CBOR_TSTR, L_NONE),
	[CTAG_CBOR_KEY_COUNTERS] = K(CTAG_CBOR_COUNTERS, L_NONE),
	[CTAG_CBOR_KEY_BATTERY_MV] = U32,
	[CTAG_CBOR_KEY_TIMING] = K(CTAG_CBOR_MAP, L_NONE),
	[CTAG_CBOR_KEY_DIGEST] = K(CTAG_CBOR_BSTR, L_MAX32),
	[CTAG_CBOR_KEY_PROTO] = U32,
	[CTAG_CBOR_KEY_MAX_FRAME] = U32,
	[CTAG_CBOR_KEY_CREDITS] = U32,
	[CTAG_CBOR_KEY_FLAGS] = U32,
	[CTAG_CBOR_KEY_BRIDGE] = K(CTAG_CBOR_UINT, L_U16),
	[CTAG_CBOR_KEY_ITEMS] = K(CTAG_CBOR_MAPS, L_NONE),
	[CTAG_CBOR_KEY_CMD] = U32,
	[CTAG_CBOR_KEY_SEQ] = U32,
	[CTAG_CBOR_KEY_TIME_MS] = U32,
	[CTAG_CBOR_KEY_TEXT] = K(CTAG_CBOR_TSTR, L_NONE),
	[CTAG_CBOR_KEY_SIZE] = U32,
	[CTAG_CBOR_KEY_OFFSET] = U32,
	[CTAG_CBOR_KEY_DATA] = K(CTAG_CBOR_BSTR, L_PAYLOAD),
	[CTAG_CBOR_KEY_ROLE] = U32,
	[CTAG_CBOR_KEY_CONFIGURED] = K(CTAG_CBOR_BOOL, L_NONE),
	[CTAG_CBOR_KEY_LAST_SEEN_S] = U32,
	[CTAG_CBOR_KEY_BOARD] = U32,
	[CTAG_CBOR_KEY_PANEL] = U32,
	[CTAG_CBOR_KEY_FLASH_SIZE] = U32,
	[CTAG_CBOR_KEY_SLOT] = U32,
	[CTAG_CBOR_KEY_ASSIGNED] = K(CTAG_CBOR_MAPS, L_NONE),
	[CTAG_CBOR_KEY_UPTIME_S] = U32,
	[CTAG_CBOR_KEY_WAKE_MS] = U32,
	[CTAG_CBOR_KEY_MESH_MS] = U32,
	[CTAG_CBOR_KEY_TRANSFER_MS] = U32,
	[CTAG_CBOR_KEY_REFRESH_MS] = U32,
	[CTAG_CBOR_KEY_SUSPEND_MS] = U32,
	[CTAG_CBOR_KEY_QUEUE_DEPTH] = U32,
	[CTAG_CBOR_KEY_MAX_BRIDGES] = U32,
	[CTAG_CBOR_KEY_MAX_TAGS] = U32,
	[CTAG_CBOR_KEY_UUID_FILTER] = K(CTAG_CBOR_BSTR, L_MAX16),
	[CTAG_CBOR_KEY_NET_IDX] = K(CTAG_CBOR_UINT, L_U16),
	[CTAG_CBOR_KEY_APP_IDX] = K(CTAG_CBOR_UINT, L_U16),
	[CTAG_CBOR_KEY_STORED_EPOCH] = U32,
	[CTAG_CBOR_KEY_ASSIGNED_COUNT] = U32,
	/* protocol v2 (docs/connect-setup.md 5.3) */
	[CTAG_CBOR_KEY_DEVICE_ID] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_IK] = K(CTAG_CBOR_BSTR, L_EQ32),
	[CTAG_CBOR_KEY_OWNER_STATE] = U32,
	[CTAG_CBOR_KEY_GEN] = U32,
	[CTAG_CBOR_KEY_AUTHORITY_ID] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_CHALLENGE] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_GRANT] = K(CTAG_CBOR_BSTR, L_GRANT),
	[CTAG_CBOR_KEY_SIG] = K(CTAG_CBOR_BSTR, L_EQ64),
	[CTAG_CBOR_KEY_STATIC_OOB] = K(CTAG_CBOR_BSTR, L_EQ32),
	[CTAG_CBOR_KEY_TUNNEL] = K(CTAG_CBOR_UINT, L_U16),
	[CTAG_CBOR_KEY_STATE] = U32,
	[CTAG_CBOR_KEY_PROOF] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_OWNER] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_CONTROLLER_MATCH] = K(CTAG_CBOR_BOOL, L_NONE),
	[CTAG_CBOR_KEY_ROOT_PROOF] = K(CTAG_CBOR_BSTR, L_EQ16),
	[CTAG_CBOR_KEY_RELEASE_STAGE] = U32,
	[CTAG_CBOR_KEY_OP_KEY] = K(CTAG_CBOR_BSTR, L_EQ32),
};

static uint8_t spec_of(uint64_t key)
{
	return key < sizeof(key_spec) ? key_spec[key] : 0u;
}

uint8_t ctag_cbor_key_kind(uint32_t key)
{
	return (uint8_t)(spec_of(key) >> 4);
}

static bool uint_ok(uint8_t limit, uint64_t v)
{
	return limit == L_U64 || v <= (limit == L_U16 ? UINT16_MAX : UINT32_MAX);
}

static bool size_ok(uint8_t limit, size_t n)
{
	switch (limit) {
	case L_EQ8:
		return n == 8u;
	case L_EQ16:
		return n == 16u;
	case L_MAX16:
		return n <= 16u;
	case L_MAX32:
		return n <= 32u;
	case L_PAYLOAD:
		return n <= CTAG_SERIAL_MAX_PAYLOAD;
	case L_LAYOUT:
		return n <= CTAG_LAYOUT_HARD_MAX;
	case L_EQ32:
		return n == 32u;
	case L_EQ64:
		return n == 64u;
	case L_GRANT:
		return n <= CTAG_GRANT_MAX;
	default:
		return true;
	}
}

/* ---- Encoding ---- */

static bool encode_map(zcbor_state_t *zs, const struct ctag_cbor_field *f, size_t n);

/* Counter names in canonical order: shorter first, then bytewise. */
static bool name_less(const struct ctag_cbor_counter *a, const struct ctag_cbor_counter *b)
{
	if (a->name_len != b->name_len) {
		return a->name_len < b->name_len;
	}
	return memcmp(a->name, b->name, a->name_len) < 0;
}

static bool encode_counters(zcbor_state_t *zs, const struct ctag_cbor_counter *c, size_t n)
{
	const struct ctag_cbor_counter *last = NULL;
	size_t i, j;

	if (!zcbor_map_start_encode(zs, n)) {
		return false;
	}
	for (i = 0u; i < n; i++) {
		const struct ctag_cbor_counter *next = NULL;

		for (j = 0u; j < n; j++) {
			if ((last == NULL || name_less(last, &c[j])) &&
			    (next == NULL || name_less(&c[j], next))) {
				next = &c[j];
			}
		}
		/* No successor: two counters share a name. */
		if (next == NULL || !zcbor_tstr_encode_ptr(zs, next->name, next->name_len) ||
		    !zcbor_uint32_put(zs, next->value)) {
			return false;
		}
		last = next;
	}
	return zcbor_map_end_encode(zs, n);
}

static bool encode_value(zcbor_state_t *zs, const struct ctag_cbor_field *f)
{
	uint8_t spec = spec_of(f->key);
	uint8_t limit = spec & 0x0Fu;
	size_t i;

	if (f->kind != spec >> 4) {
		return false;
	}
	switch (f->kind) {
	case CTAG_CBOR_UINT:
		return uint_ok(limit, f->v.u) && zcbor_uint64_put(zs, f->v.u);
	case CTAG_CBOR_INT:
		return f->v.i >= INT32_MIN && f->v.i <= INT32_MAX && zcbor_int64_put(zs, f->v.i);
	case CTAG_CBOR_BOOL:
		return zcbor_bool_put(zs, f->v.b);
	case CTAG_CBOR_BSTR:
		return size_ok(limit, f->v.str.len) &&
		       zcbor_bstr_encode_ptr(zs, (const char *)f->v.str.ptr, f->v.str.len);
	case CTAG_CBOR_TSTR:
		return zcbor_tstr_encode_ptr(zs, (const char *)f->v.str.ptr, f->v.str.len);
	case CTAG_CBOR_MAP:
		return encode_map(zs, f->v.map.fields, f->v.map.count);
	case CTAG_CBOR_MAPS:
		if (!zcbor_list_start_encode(zs, f->v.maps.count)) {
			return false;
		}
		for (i = 0u; i < f->v.maps.count; i++) {
			if (!encode_map(zs, f->v.maps.items[i].fields, f->v.maps.items[i].count)) {
				return false;
			}
		}
		return zcbor_list_end_encode(zs, f->v.maps.count);
	default:
		return encode_counters(zs, f->v.counters.items, f->v.counters.count);
	}
}

static bool encode_map(zcbor_state_t *zs, const struct ctag_cbor_field *f, size_t n)
{
	int last = -1;
	size_t i, j;

	if (!zcbor_map_start_encode(zs, n)) {
		return false;
	}
	for (i = 0u; i < n; i++) {
		const struct ctag_cbor_field *next = NULL;

		for (j = 0u; j < n; j++) {
			if ((int)f[j].key > last && (next == NULL || f[j].key < next->key)) {
				next = &f[j];
			}
		}
		/* No successor: a key appears twice. */
		if (next == NULL || !zcbor_uint32_put(zs, next->key) || !encode_value(zs, next)) {
			return false;
		}
		last = next->key;
	}
	return zcbor_map_end_encode(zs, n);
}

int ctag_cbor_encode(const struct ctag_cbor_field *fields, size_t count, uint8_t *buf, size_t size)
{
	ZCBOR_STATE_E(zs, ENC_DEPTH + 1u, buf, size, 0);

	if (count == 0u) {
		return 0;
	}
	if (!encode_map(zs, fields, count)) {
		return zcbor_peek_error(zs) == ZCBOR_ERR_NO_PAYLOAD ? -EMSGSIZE : -EINVAL;
	}
	return (int)(zs->payload - buf);
}

/* ---- Well-formedness (cbor_msgs.py check_well_formed) ---- */

/*
 * Work bounds of the scan (docs/firmware-libs.md). Every item is visited
 * once, and a key is compared only with the earlier keys of its own map: the
 * scan records each key as (start, end) on a stack shared by the maps being
 * scanned, so a map's keys and the keys its enclosing maps had read before it
 * must fit MAX_KEYS together. Spec payloads need at most 72 at once (8
 * top-level keys before a counters map of up to 64: MAINT_COUNTERS, the
 * gateway's MAX_COUNTERS) and at most 383 map entries (GET_INVENTORY with 5
 * bridges and CONFIG_CTAG_GW_ASSIGN_MAX = 128). Anything larger is refused.
 */
#define MAX_KEYS    96u  /* keys recorded at one time */
#define MAX_ENTRIES 512u /* map entries in one payload */

#ifdef CTAG_CBOR_STEPS
unsigned long ctag_cbor_steps;
#define STEP() (ctag_cbor_steps++)
#else
#define STEP() ((void)0)
#endif

struct wf {
	const uint8_t *d;
	size_t len;
	size_t pos;
	uint16_t entries;          /* map entries so far */
	uint16_t keys;             /* recorded keys */
	uint16_t key[MAX_KEYS][2]; /* start, end: len <= UINT16_MAX */
};

/* Item head: definite, shortest form, no floats or simple values but false/true. */
static bool head(struct wf *s, uint8_t *major, uint64_t *arg)
{
	uint8_t info;
	size_t w;

	if (s->pos >= s->len) {
		return false;
	}
	*major = s->d[s->pos] >> 5;
	info = s->d[s->pos] & 0x1Fu;
	s->pos++;
	if (info < 24u) {
		*arg = info;
		return *major != 7u || info == 20u || info == 21u;
	}
	if (info > 27u || *major == 7u) {
		return false;
	}
	w = (size_t)1u << (info - 24u);
	if (s->len - s->pos < w) {
		return false;
	}
	for (*arg = 0u; w > 0u; w--) {
		*arg = *arg << 8 | s->d[s->pos++];
	}
	w = (size_t)1u << (info - 24u);
	return *arg >= (w == 1u ? 24u : (uint64_t)1u << (4u * w));
}

static bool text_ok(const uint8_t *p, size_t n)
{
	struct ctag_utf8 u;

	ctag_utf8_init(&u);
	ctag_utf8_feed(&u, p, n);
	return ctag_utf8_valid(&u);
}

/* Is the key just scanned, [k, pos), one of the keys recorded since base? */
static bool duplicate_key(const struct wf *s, uint16_t base, size_t k)
{
	size_t n = s->pos - k;
	uint16_t i;

	for (i = base; i < s->keys; i++) {
		STEP();
		if ((size_t)(s->key[i][1] - s->key[i][0]) == n &&
		    memcmp(&s->d[s->key[i][0]], &s->d[k], n) == 0) {
			return true;
		}
	}
	return false;
}

static bool scan(struct wf *s, unsigned int depth)
{
	uint8_t major;
	uint64_t arg;
	uint16_t base;
	size_t k;

	STEP();
	if (depth > MAX_DEPTH || !head(s, &major, &arg)) {
		return false;
	}
	switch (major) {
	case 2:
	case 3:
		if (arg > s->len - s->pos || (major == 3u && !text_ok(&s->d[s->pos], (size_t)arg))) {
			return false;
		}
		s->pos += (size_t)arg;
		return true;
	case 4:
		for (; arg > 0u; arg--) {
			if (!scan(s, depth + 1u)) {
				return false;
			}
		}
		return true;
	case 5:
		base = s->keys;
		if (arg > MAX_KEYS - base || arg > MAX_ENTRIES - s->entries) {
			return false;
		}
		s->entries = (uint16_t)(s->entries + arg);
		for (; arg > 0u; arg--) {
			k = s->pos;
			if (!scan(s, depth + 1u) || duplicate_key(s, base, k)) {
				return false;
			}
			s->key[s->keys][0] = (uint16_t)k;
			s->key[s->keys][1] = (uint16_t)s->pos;
			s->keys++;
			if (!scan(s, depth + 1u)) {
				return false;
			}
		}
		s->keys = base;
		return true;
	case 6:
		return false; /* tags */
	default:
		return true; /* integers, false, true */
	}
}

/* Out of line: the key stack and the zcbor states are never on the stack together. */
static __attribute__((noinline)) bool well_formed(const uint8_t *buf, size_t len)
{
	struct wf s;

	if (len > UINT16_MAX) {
		return false;
	}
	s.d = buf;
	s.len = len;
	s.pos = 0u;
	s.entries = 0u;
	s.keys = 0u;
	return scan(&s, 0u) && s.pos == len;
}

/* ---- Typed decoding ---- */

static bool decode_map(zcbor_state_t *zs, struct ctag_cbor_field *fields, size_t count);

/* Check (and store, when f is not NULL) the value of a known key. */
static bool decode_value(zcbor_state_t *zs, uint8_t spec, struct ctag_cbor_field *f)
{
	const uint8_t *start = zs->payload;
	uint8_t limit = spec & 0x0Fu;
	struct zcbor_string s;
	uint64_t u = 0u;
	int64_t i = 0;
	bool b = false;
	uint32_t v;

	switch (spec >> 4) {
	case CTAG_CBOR_UINT:
		if (!zcbor_uint64_decode(zs, &u) || !uint_ok(limit, u)) {
			return false;
		}
		break;
	case CTAG_CBOR_INT:
		if (!zcbor_int64_decode(zs, &i) || i < INT32_MIN || i > INT32_MAX) {
			return false;
		}
		break;
	case CTAG_CBOR_BOOL:
		if (!zcbor_bool_decode(zs, &b)) {
			return false;
		}
		break;
	case CTAG_CBOR_BSTR:
	case CTAG_CBOR_TSTR:
		if (!((spec >> 4) == CTAG_CBOR_BSTR ? zcbor_bstr_decode(zs, &s)
						    : zcbor_tstr_decode(zs, &s)) ||
		    !size_ok(limit, s.len)) {
			return false;
		}
		break;
	case CTAG_CBOR_MAP:
		if (!decode_map(zs, NULL, 0u)) {
			return false;
		}
		break;
	case CTAG_CBOR_MAPS:
		if (!zcbor_list_start_decode(zs)) {
			return false;
		}
		while (!zcbor_array_at_end(zs)) {
			if (!decode_map(zs, NULL, 0u)) {
				return false;
			}
		}
		if (!zcbor_list_end_decode(zs)) {
			return false;
		}
		break;
	default:
		if (!zcbor_map_start_decode(zs)) {
			return false;
		}
		while (!zcbor_array_at_end(zs)) {
			if (!zcbor_tstr_decode(zs, &s) || !zcbor_uint32_decode(zs, &v)) {
				return false;
			}
		}
		if (!zcbor_map_end_decode(zs)) {
			return false;
		}
		break;
	}
	if (f == NULL) {
		return true;
	}
	f->present = true;
	switch (spec >> 4) {
	case CTAG_CBOR_UINT:
		f->v.u = u;
		break;
	case CTAG_CBOR_INT:
		f->v.i = i;
		break;
	case CTAG_CBOR_BOOL:
		f->v.b = b;
		break;
	case CTAG_CBOR_BSTR:
	case CTAG_CBOR_TSTR:
		f->v.str.ptr = s.value;
		f->v.str.len = s.len;
		break;
	default:
		f->v.str.ptr = start;
		f->v.str.len = (size_t)(zs->payload - start);
		break;
	}
	return true;
}

static bool decode_map(zcbor_state_t *zs, struct ctag_cbor_field *fields, size_t count)
{
	if (!zcbor_map_start_decode(zs)) {
		return false;
	}
	while (!zcbor_array_at_end(zs)) {
		struct ctag_cbor_field *f = NULL;
		uint64_t key;
		uint8_t spec;
		size_t i;

		/* Keys are unsigned integers; unknown ones are skipped (1.1). */
		if (!zcbor_uint64_decode(zs, &key)) {
			return false;
		}
		spec = spec_of(key);
		if (spec == 0u) {
			if (!zcbor_any_skip(zs, NULL)) {
				return false;
			}
			continue;
		}
		for (i = 0u; i < count; i++) {
			if (fields[i].key == key) {
				f = &fields[i];
			}
		}
		if (!decode_value(zs, spec, f)) {
			return false;
		}
	}
	return zcbor_map_end_decode(zs);
}

static __attribute__((noinline)) bool decode_payload(const uint8_t *buf, size_t len,
						    struct ctag_cbor_field *fields, size_t count)
{
	ZCBOR_STATE_D(zs, MAX_DEPTH + 1u, buf, len, 1, 0);
	return decode_map(zs, fields, count);
}

int ctag_cbor_decode(const uint8_t *buf, size_t len, struct ctag_cbor_field *fields, size_t count)
{
	size_t i;

	for (i = 0u; i < count; i++) {
		fields[i].kind = ctag_cbor_key_kind(fields[i].key);
		fields[i].present = false;
	}
	if (len == 0u) {
		return 0;
	}
	return well_formed(buf, len) && decode_payload(buf, len, fields, count) ? 0 : -EBADMSG;
}

int ctag_cbor_maps(const struct ctag_cbor_str *maps, struct ctag_cbor_str *items, size_t max)
{
	size_t n = 0u;

	ZCBOR_STATE_D(zs, 2, maps->ptr, maps->len, 1, 0);
	if (!zcbor_list_start_decode(zs)) {
		return -EBADMSG;
	}
	while (!zcbor_array_at_end(zs)) {
		const uint8_t *start = zs->payload;

		if (n == max) {
			return -ENOSPC;
		}
		if (!zcbor_any_skip(zs, NULL)) {
			return -EBADMSG;
		}
		items[n].ptr = start;
		items[n].len = (size_t)(zs->payload - start);
		n++;
	}
	return zcbor_list_end_decode(zs) ? (int)n : -EBADMSG;
}

int ctag_cbor_counters(const struct ctag_cbor_str *counters, struct ctag_cbor_counter *items,
		       size_t max)
{
	size_t n = 0u;

	ZCBOR_STATE_D(zs, 2, counters->ptr, counters->len, 1, 0);
	if (!zcbor_map_start_decode(zs)) {
		return -EBADMSG;
	}
	while (!zcbor_array_at_end(zs)) {
		struct zcbor_string s;

		if (n == max) {
			return -ENOSPC;
		}
		if (!zcbor_tstr_decode(zs, &s) || !zcbor_uint32_decode(zs, &items[n].value)) {
			return -EBADMSG;
		}
		items[n].name = (const char *)s.value;
		items[n].name_len = s.len;
		n++;
	}
	return zcbor_map_end_decode(zs) ? (int)n : -EBADMSG;
}
