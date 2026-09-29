/*
 * Serial protocol server (docs/protocol.md 1.1-1.5, 10): framing, credits,
 * HELLO, responses, retained and best-effort events, idempotency, and the
 * request dispatcher. Mirrors Cremind's app/tags/runtime/sim/device.py (DeviceEndpoint)
 * and sim/gateway.py (_handle).
 *
 * Protocol v2 (CONFIG_CTAG_GW_SECURE, docs/connect-setup.md 4.2, 5): in
 * plaintext only HELLO, PING, IDENTIFY, SECURE_OPEN and SECURE_DATA are
 * served (everything else: AUTH_REQUIRED). A request that arrives inside the
 * session (SECURE_DATA) is dispatched here as usual once the access table
 * allows it, and its answer is sealed into the session when it is sent;
 * events go out only sealed into a privileged session (owned gateway, the
 * pinned controller). gw_secure.c holds the v2 messages themselves.
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

#define REQ_FIELDS 8u

/* Every request is answered from a response slot; a request's credit is
 * returned (owed) when its answer is sent, so a host that respects credits
 * never has more requests outstanding than there are slots. */
#define N_RESP CONFIG_CTAG_GW_SERIAL_CREDITS

#ifdef CONFIG_CTAG_GW_SECURE
/* SECURE_DATA payload {41: bstr}: map head, key 41 (0x18 0x29), bstr head <= 3. */
#define OUTER_MAX 6u
/* Everything a sealed frame adds around its inner payload. */
#define SEAL_ROOM (CTAG_SERIAL_HEADER_LEN + OUTER_MAX + CTAG_SECURE_HEADER_LEN + \
		   CTAG_SECURE_TAG_LEN + CTAG_SERIAL_CRC_LEN)
#endif

static const char T_MALFORMED[] = "malformed CBOR payload";
static const char T_MISSING[] = "missing field";
static const char T_TOO_LARGE_ANSWER[] = "answer too large";

/* ---- Request field specifications (Cremind's app/tags/runtime/protocol/cbor_msgs.py REQUESTS) ---- */

struct req_spec {
	uint8_t type;
	uint8_t n;
	uint8_t required; /* bit i: keys[i] required */
	uint8_t keys[REQ_FIELDS];
};

#define K(name) CTAG_CBOR_KEY_##name

static const struct req_spec specs[] = {
	{CTAG_SERIAL_MSG_HELLO, 2, 0x03, {K(PROTO), K(NAME)}},
	{CTAG_SERIAL_MSG_PING, 0, 0, {0}},
	{CTAG_SERIAL_MSG_REBOOT, 1, 0x01, {K(OP_ID)}},
	{CTAG_SERIAL_MSG_EVENT_ACK, 1, 0x01, {K(SEQ)}},
	{CTAG_SERIAL_MSG_INFO, 0, 0, {0}},
	{CTAG_SERIAL_MSG_SCAN_UNPROV, 2, 0x01, {K(DURATION_S), K(UUID_FILTER)}},
#ifdef CONFIG_CTAG_GW_SECURE
	/* v2: static_oob is required (connect-setup.md 5.2) */
	{CTAG_SERIAL_MSG_PROVISION, 4, 0x0B, {K(OP_ID), K(UUID), K(NAME), K(STATIC_OOB)}},
#else
	{CTAG_SERIAL_MSG_PROVISION, 3, 0x03, {K(OP_ID), K(UUID), K(NAME)}},
#endif
	{CTAG_SERIAL_MSG_CONFIGURE_NODE, 4, 0x0F, {K(OP_ID), K(ADDR), K(RELAY), K(TTL)}},
	{CTAG_SERIAL_MSG_REMOVE_NODE, 2, 0x03, {K(OP_ID), K(ADDR)}},
	{CTAG_SERIAL_MSG_LIST_NODES, 0, 0, {0}},
	{CTAG_SERIAL_MSG_ASSIGN_TAG, 5, 0x1F, {K(OP_ID), K(BRIDGE), K(TAG_ID), K(EPOCH), K(KEY)}},
	{CTAG_SERIAL_MSG_UNASSIGN_TAG, 4, 0x0F, {K(OP_ID), K(BRIDGE), K(TAG_ID), K(EPOCH)}},
	{CTAG_SERIAL_MSG_DELIVER_LAYOUT,
	 8,
	 0xFF,
	 {K(OP_ID), K(BRIDGE), K(TAG_ID), K(EPOCH), K(REVISION), K(UPDATE_ID), K(FONTPACK_ID),
	  K(LAYOUT)}},
	{CTAG_SERIAL_MSG_CANCEL_DELIVERY, 2, 0x03, {K(OP_ID), K(UPDATE_ID)}},
	{CTAG_SERIAL_MSG_TAG_COMMAND, 5, 0x1F, {K(OP_ID), K(BRIDGE), K(TAG_ID), K(EPOCH), K(CMD)}},
	{CTAG_SERIAL_MSG_GET_INVENTORY, 0, 0, {0}},
	{CTAG_SERIAL_MSG_GET_COUNTERS, 0, 0, {0}},
	{CTAG_SERIAL_MSG_IDENTIFY_NODE, 2, 0x03, {K(OP_ID), K(ADDR)}},
#ifdef CONFIG_CTAG_GW_SECURE
	{CTAG_SERIAL_MSG_DISCOVER, 4, 0x0F, {K(OP_ID), K(BRIDGE), K(DURATION_S), K(TAG_ID)}},
	/* mode (optional, absent = PAIR): protocol.md 11.3 */
	{CTAG_SERIAL_MSG_TUNNEL_OPEN,
	 5,
	 0x0F,
	 {K(OP_ID), K(BRIDGE), K(TAG_ID), K(DURATION_S), K(MODE)}},
	{CTAG_SERIAL_MSG_TUNNEL_SEND, 2, 0x03, {K(TUNNEL), K(DATA)}},
	{CTAG_SERIAL_MSG_TUNNEL_CLOSE, 1, 0x01, {K(TUNNEL)}},
#endif
};

static const struct req_spec *spec_of(uint8_t type)
{
	for (size_t i = 0; i < sizeof(specs) / sizeof(specs[0]); i++) {
		if (specs[i].type == type) {
			return &specs[i];
		}
	}
	return NULL; /* unknown, FONT_* (bridge maintenance port only) and event types */
}

/* 1.4: side-effecting requests carry an op_id and are idempotent. */
static bool side_effecting(uint8_t type)
{
	switch (type) {
	case CTAG_SERIAL_MSG_PROVISION:
	case CTAG_SERIAL_MSG_CONFIGURE_NODE:
	case CTAG_SERIAL_MSG_REMOVE_NODE:
	case CTAG_SERIAL_MSG_ASSIGN_TAG:
	case CTAG_SERIAL_MSG_UNASSIGN_TAG:
	case CTAG_SERIAL_MSG_DELIVER_LAYOUT:
	case CTAG_SERIAL_MSG_CANCEL_DELIVERY:
	case CTAG_SERIAL_MSG_TAG_COMMAND:
	case CTAG_SERIAL_MSG_REBOOT:
	case CTAG_SERIAL_MSG_IDENTIFY_NODE:
#ifdef CONFIG_CTAG_GW_SECURE
	case CTAG_SERIAL_MSG_DISCOVER:
	case CTAG_SERIAL_MSG_TUNNEL_OPEN:
#endif
		return true;
	default:
		return false;
	}
}

/* 10: transient refusals are not remembered, so a retry can succeed. */
static bool transient(uint8_t status)
{
	return status == CTAG_STATUS_BUSY || status == CTAG_STATUS_NO_RESOURCES ||
	       status == CTAG_STATUS_PROVISIONING_ACTIVE;
}

/* Answers built from live state when they are sent. */
static bool is_query(uint8_t type)
{
	switch (type) {
	case CTAG_SERIAL_MSG_PING:
	case CTAG_SERIAL_MSG_INFO:
	case CTAG_SERIAL_MSG_LIST_NODES:
	case CTAG_SERIAL_MSG_GET_INVENTORY:
	case CTAG_SERIAL_MSG_GET_COUNTERS:
		return true;
	default:
		return false;
	}
}

/* ---- Init ---- */

void gw_serial_init(struct gw_core *g)
{
	struct gw_serial *s = &g->s;

	ctag_serial_rx_init(&s->rx, s->rxbuf, sizeof(s->rxbuf));
	s->hello_done = false;
	s->send_credits = 0u;
	s->host_budget = 0;
	s->owed = 0u;
	s->hello_pending = false;
	s->resp_head = s->resp_count = 0u;
	s->ret_head = s->ret_count = 0u;
	s->seq = 0u;
	s->next_retained = 1u;
	gw_ring_init(&s->evq, s->evq_buf, sizeof(s->evq_buf));
	memset(s->idem, 0, sizeof(s->idem));
	s->idem_next = 0u;
	s->tx_len = 0u;
	s->cobs_phase = 0u;
	s->reboot_after_frame = false;
	s->reboot_at = 0;
#ifdef CONFIG_CTAG_GW_SECURE
	s->in_secure = false;
#endif
}

/* ---- Idempotency (1.4, 10) ---- */

static const struct gw_idem *idem_find(const struct gw_serial *s, uint64_t op_id)
{
	for (size_t i = 0; i < CTAG_SERIAL_IDEMPOTENCY_SLOTS; i++) {
		if (s->idem[i].used && s->idem[i].op_id == op_id) {
			return &s->idem[i];
		}
	}
	return NULL;
}

static void idem_put(struct gw_serial *s, uint64_t op_id, uint8_t status, const char *text,
		     uint16_t tunnel)
{
	struct gw_idem *e = &s->idem[s->idem_next];

	/* The oldest entry goes (the simulator's OrderedDict eviction). */
	e->used = true;
	e->op_id = op_id;
	e->status = status;
	e->text = text;
	e->tunnel = tunnel;
	s->idem_next = (uint8_t)((s->idem_next + 1u) % CTAG_SERIAL_IDEMPOTENCY_SLOTS);
}

/* ---- Events ---- */

/* Events may be sent now (v2: only into a privileged session). */
static bool events_open(const struct gw_core *g)
{
#ifdef CONFIG_CTAG_GW_SECURE
	return gw_v2_privileged(g);
#else
	(void)g;
	return true;
#endif
}

bool gw_emit(struct gw_core *g, uint8_t type, const struct ctag_cbor_field *f, size_t n,
	     bool retained)
{
	struct gw_serial *s = &g->s;
	struct ctag_cbor_field all[12];
	int len;

	if (n + 1u > sizeof(all) / sizeof(all[0])) {
		g->c.internal_errors++;
		return false;
	}
	if (retained) {
		struct gw_retained *e;

		memcpy(all, f, n * sizeof(*f));
		all[n] = GW_F_UINT(CTAG_CBOR_KEY_SEQ, s->seq + 1u);
		if (s->ret_count == CTAG_SERIAL_EVENT_RETAIN) {
			/* 1.2: the ring overflows, the oldest event is dropped. */
			s->ret_head = (uint8_t)((s->ret_head + 1u) % CTAG_SERIAL_EVENT_RETAIN);
			s->ret_count--;
			g->c.events_dropped++;
		}
		e = &s->ret[(s->ret_head + s->ret_count) % CTAG_SERIAL_EVENT_RETAIN];
		len = ctag_cbor_encode(all, n + 1u, e->payload, sizeof(e->payload));
		if (len < 0) {
			g->c.internal_errors++;
			return false;
		}
		s->seq++;
		e->seq = s->seq;
		e->type = type;
		e->len = (uint8_t)len;
		s->ret_count++;
		return true; /* sent by the pump that ends every core entry point */
	}
	/* Best effort: never queued without a session and a credit (sim/device.py emit). */
	if (!s->hello_done || s->send_credits == 0u || !events_open(g)) {
		g->c.events_discarded++;
		return false;
	}
	/* Encoded straight into the FIFO: [type][payload]. */
	{
		uint32_t cap;
		uint8_t *rec = gw_ring_reserve(&s->evq, 2u, &cap);
#ifdef CONFIG_CTAG_GW_SECURE
		size_t room = sizeof(s->tx) - SEAL_ROOM;
#else
		size_t room = sizeof(s->tx) - CTAG_SERIAL_MIN_FRAME;
#endif

		if (rec == NULL) {
			g->c.events_discarded++;
			return false;
		}
		len = ctag_cbor_encode(f, n, &rec[1], cap - 1u < room ? cap - 1u : room);
		if (len < 0) {
			/* No room now (-EMSGSIZE) or a malformed event (-EINVAL). */
			if (len == -EMSGSIZE) {
				g->c.events_discarded++;
			} else {
				g->c.internal_errors++;
			}
			return false;
		}
		rec[0] = type;
		gw_ring_commit(&s->evq, 1u + (uint32_t)len);
	}
	return true;
}

uint32_t gw_retained_count(const struct gw_core *g)
{
	return g->s.ret_count;
}

static void release_retained(struct gw_serial *s, uint32_t seq)
{
	while (s->ret_count > 0u && s->ret[s->ret_head].seq <= seq) {
		s->ret_head = (uint8_t)((s->ret_head + 1u) % CTAG_SERIAL_EVENT_RETAIN);
		s->ret_count--;
	}
}

/* ---- Responses ---- */

static struct gw_resp *respond(struct gw_core *g, const struct ctag_serial_header *h,
			       uint8_t status, const char *text, bool duplicate)
{
	struct gw_serial *s = &g->s;
	struct gw_resp *r;

	/* A slot is always free: on_frame() drops a request that finds none. */
	r = &s->resp[(s->resp_head + s->resp_count) % N_RESP];
	memset(r, 0, sizeof(*r));
	r->rid = h->request_id;
	r->type = h->type;
	r->status = status;
	r->text = text;
	r->duplicate = duplicate;
	r->reboot = false;
#ifdef CONFIG_CTAG_GW_SECURE
	r->secure = s->in_secure;
	r->session = g->v2.ep.session_serial;
#endif
	s->resp_count++;
	return r;
}

/* HELLO / INFO caps; with tag links (protocol.md 11) also tag_links. */
#ifdef CONFIG_CTAG_GW_RADIO
#define CAPS_FIELDS 7
#else
#define CAPS_FIELDS 6
#endif

static int encode_caps(const struct gw_core *g, struct ctag_cbor_field caps[CAPS_FIELDS])
{
	caps[0] = GW_F_UINT(CTAG_CBOR_KEY_MAX_FRAME, CTAG_SERIAL_MAX_FRAME);
	caps[1] = GW_F_UINT(CTAG_CBOR_KEY_CREDITS, N_RESP);
	caps[2] = GW_F_UINT(CTAG_CBOR_KEY_ROLE, CTAG_NODE_ROLE_GATEWAY);
	caps[3] = GW_F_UINT(CTAG_CBOR_KEY_BOARD, g->info.board);
	caps[4] = GW_F_UINT(CTAG_CBOR_KEY_MAX_BRIDGES, CTAG_MAX_BRIDGES);
	caps[5] = GW_F_UINT(CTAG_CBOR_KEY_MAX_TAGS, CTAG_MAX_TAGS);
#ifdef CONFIG_CTAG_GW_RADIO
	caps[6] = GW_F_UINT(CTAG_CBOR_KEY_TAG_LINKS, CONFIG_CTAG_GW_TAG_LINKS);
#endif
	return CAPS_FIELDS;
}

static int encode_hello(struct gw_core *g, uint8_t *buf, size_t size)
{
	struct ctag_cbor_field caps[CAPS_FIELDS];
	struct ctag_cbor_field f[6];
	size_t n = 0u;

	f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, g->s.hello_status);
	f[n++] = GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION);
	if (g->s.hello_status == CTAG_STATUS_OK) {
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_FW, g->info.fw, strlen(g->info.fw));
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_BUILD, g->info.build, strlen(g->info.build));
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_BOOT_ID, g->info.boot_id);
		f[n++] = GW_F_MAP(CTAG_CBOR_KEY_CAPS, caps, (size_t)encode_caps(g, caps));
	}
	return ctag_cbor_encode(f, n, buf, size);
}

/* Core, v2, own-radio and backend counters; INFO's 8 top-level keys plus
 * these stay within ctag_cbor's 96 keys held at once (docs/firmware-libs.md). */
#if defined(CONFIG_CTAG_GW_RADIO)
#define MAX_COUNTERS 88u
#elif defined(CONFIG_CTAG_GW_SECURE)
#define MAX_COUNTERS 72u
#else
#define MAX_COUNTERS 64u
#endif

static int encode_query(struct gw_core *g, uint8_t type, uint8_t *buf, size_t size)
{
	struct ctag_cbor_counter items[MAX_COUNTERS];
	struct ctag_cbor_field caps[CAPS_FIELDS];
	struct ctag_cbor_field f[6];
	size_t n = 0u;

	switch (type) {
	case CTAG_SERIAL_MSG_PING:
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_UPTIME_S, gw_uptime_s(g));
		return ctag_cbor_encode(f, n, buf, size);
	case CTAG_SERIAL_MSG_LIST_NODES:
		return gw_encode_nodes(g, buf, size);
	case CTAG_SERIAL_MSG_GET_INVENTORY:
		return gw_encode_inventory(g, buf, size);
	default:
		break;
	}
	f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	if (type == CTAG_SERIAL_MSG_INFO) {
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_FW, g->info.fw, strlen(g->info.fw));
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_BUILD, g->info.build, strlen(g->info.build));
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_BOOT_ID, g->info.boot_id);
		f[n++] = GW_F_MAP(CTAG_CBOR_KEY_CAPS, caps, (size_t)encode_caps(g, caps));
	}
	f[n].key = CTAG_CBOR_KEY_COUNTERS;
	f[n].kind = CTAG_CBOR_COUNTERS;
	f[n].v.counters.items = items;
	f[n++].v.counters.count = gw_core_counters(g, items, MAX_COUNTERS);
	return ctag_cbor_encode(f, n, buf, size);
}

static int encode_status(const struct gw_resp *r, uint8_t *buf, size_t size)
{
	struct ctag_cbor_field f[4];
	size_t n = 0u;

	f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, r->status);
	if (r->duplicate) {
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_DETAIL, CTAG_STATUS_DUPLICATE);
	}
	if (r->text != NULL) {
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_TEXT, r->text, strlen(r->text));
	}
	return ctag_cbor_encode(f, n, buf, size);
}

/* The payload of an answer. */
static int encode_answer(struct gw_core *g, const struct gw_resp *r, uint8_t *buf, size_t size)
{
	int len;

#ifdef CONFIG_CTAG_GW_SECURE
	if (r->v2 != GW_V2_NONE) {
		len = gw_v2_encode(g, r, buf, size);
	} else
#endif
		if (r->status == CTAG_STATUS_OK && !r->duplicate && is_query(r->type)) {
		len = encode_query(g, r->type, buf, size);
	} else {
		len = encode_status(r, buf, size);
	}
	if (len < 0) {
		struct gw_resp internal = {.status = CTAG_STATUS_INTERNAL, .text = T_TOO_LARGE_ANSWER};

		g->c.internal_errors++;
		len = encode_status(&internal, buf, size);
	}
	return len;
}

/* ---- Transmit ---- */

enum { COBS_IDLE = 0, COBS_CODE, COBS_DATA, COBS_DELIM };

static bool finish_frame(struct gw_core *g, struct ctag_serial_header *h, int len)
{
	struct gw_serial *s = &g->s;
	int n = ctag_serial_frame_build(s->tx, sizeof(s->tx), h, NULL, (size_t)len);

	if (n <= 0) {
		g->c.internal_errors++;
		return false;
	}
	s->tx_len = (size_t)n;
	s->cobs_start = 0u;
	s->cobs_phase = COBS_CODE;
	g->c.frames_tx++;
	return true;
}

/*
 * COBS-encode s->tx straight into the driver (the same encoding as
 * ctag_cobs_encode(): no trailing 0x01 after a final 254-byte block), then the
 * 0x00 delimiter. Code bytes are computed by looking ahead in the frame, so no
 * encoded copy is kept. true = the frame is out; false = the driver is full.
 */
static bool cobs_out(struct gw_core *g)
{
	struct gw_serial *s = &g->s;
	const struct gw_backend *be = g->be;

	for (;;) {
		switch (s->cobs_phase) {
		case COBS_CODE: {
			size_t end = s->cobs_start;
			uint8_t code;

			while (end < s->tx_len && s->tx[end] != 0u && end - s->cobs_start < 254u) {
				end++;
			}
			s->cobs_n = end - s->cobs_start;
			code = (uint8_t)(s->cobs_n + 1u);
			if (be->write(be->ctx, &code, 1u) == 0u) {
				return false;
			}
			s->cobs_off = 0u;
			s->cobs_phase = COBS_DATA;
			break;
		}
		case COBS_DATA:
			if (s->cobs_off < s->cobs_n) {
				s->cobs_off += be->write(be->ctx, &s->tx[s->cobs_start + s->cobs_off],
							 s->cobs_n - s->cobs_off);
				if (s->cobs_off < s->cobs_n) {
					return false;
				}
			}
			if (s->cobs_start + s->cobs_n == s->tx_len) {
				s->cobs_phase = COBS_DELIM;
			} else {
				/* A full block implies no zero; any other block ends at one. */
				s->cobs_start += s->cobs_n == 254u ? s->cobs_n : s->cobs_n + 1u;
				s->cobs_phase = COBS_CODE;
			}
			break;
		case COBS_DELIM: {
			const uint8_t zero = 0u;

			if (be->write(be->ctx, &zero, 1u) == 0u) {
				return false;
			}
			s->cobs_phase = COBS_IDLE;
			s->tx_len = 0u;
			return true;
		}
		default:
			return true;
		}
	}
}

/* Take the grant for the frame about to be sent (it consumes a host credit). */
static void take_credit(struct gw_serial *s, struct ctag_serial_header *h)
{
	uint8_t grant = (uint8_t)(s->owed > 255u ? 255u : s->owed);

	s->owed = (uint16_t)(s->owed - grant);
	s->host_budget += grant;
	s->send_credits--;
	h->credits = grant;
}

static const struct gw_retained *next_retained(const struct gw_serial *s)
{
	for (uint8_t i = 0; i < s->ret_count; i++) {
		const struct gw_retained *e = &s->ret[(s->ret_head + i) % CTAG_SERIAL_EVENT_RETAIN];

		if (e->seq >= s->next_retained) {
			return e;
		}
	}
	return NULL;
}

#ifdef CONFIG_CTAG_GW_SECURE
/*
 * v2: the inner message is built at inner = tx + header + OUTER_MAX (its
 * 4-byte header, then its payload at inner + 4); this seals it into the
 * session and turns tx into the SECURE_DATA frame's payload. Returns the
 * payload length, or -errno (the frame is not sent).
 */
static int seal_frame(struct gw_core *g, uint8_t type, uint8_t flags, uint16_t rid, int len)
{
	struct gw_serial *s = &g->s;
	uint8_t *inner = &s->tx[CTAG_SERIAL_HEADER_LEN + OUTER_MAX];
	struct ctag_secure_hdr ih = {type, flags, rid};

	ctag_secure_hdr_pack(&ih, inner);
	return gw_v2_seal(g, &s->tx[CTAG_SERIAL_HEADER_LEN], inner,
			  CTAG_SECURE_HEADER_LEN + (size_t)len,
			  sizeof(s->tx) - CTAG_SERIAL_HEADER_LEN - OUTER_MAX - CTAG_SERIAL_CRC_LEN);
}
#endif

/* Build the next frame into s->tx (order: HELLO answer, answers, retained
 * events, best-effort events). false = nothing to send now. */
static bool next_frame(struct gw_core *g)
{
	struct gw_serial *s = &g->s;
	struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION};
	uint8_t *payload = &s->tx[CTAG_SERIAL_HEADER_LEN];
	size_t room = sizeof(s->tx) - CTAG_SERIAL_MIN_FRAME;
	const struct gw_retained *e;
	int len;

	if (s->hello_pending) {
		/* HELLO is exempt from credits; its grant byte stays 0 (10). */
		s->hello_pending = false;
		h.type = CTAG_SERIAL_MSG_HELLO;
		h.request_id = s->hello_rid;
		h.flags = CTAG_SERIAL_FLAG_RESPONSE;
		len = encode_hello(g, payload, room);
		if (len < 0) {
			g->c.internal_errors++;
			return true; /* dropped; look at the rest */
		}
		(void)finish_frame(g, &h, len);
		return true;
	}
	if (!s->hello_done || s->send_credits == 0u) {
		return false;
	}
	if (s->resp_count > 0u) {
		struct gw_resp *r = &s->resp[s->resp_head];

		s->resp_head = (uint8_t)((s->resp_head + 1u) % N_RESP);
		s->resp_count--;
		/* The request's slot is free again: its credit rides on this answer. */
		if (s->owed < UINT16_MAX) {
			s->owed++;
		}
		h.type = r->type;
		h.request_id = r->rid;
		h.flags = CTAG_SERIAL_FLAG_RESPONSE;
#ifdef CONFIG_CTAG_GW_SECURE
		if (r->secure) {
			/* Only into the session it answers; else dropped (credit owed). */
			if (!g->v2.ep.session || r->session != g->v2.ep.session_serial) {
				return true;
			}
			len = encode_answer(g, r, &s->tx[CTAG_SERIAL_HEADER_LEN + OUTER_MAX +
							 CTAG_SECURE_HEADER_LEN],
					    sizeof(s->tx) - SEAL_ROOM);
			len = seal_frame(g, r->type, CTAG_SERIAL_FLAG_RESPONSE, r->rid, len);
			if (len < 0) {
				return true; /* the session failed; the answer is lost with it */
			}
			h.type = CTAG_SERIAL_MSG_SECURE_DATA;
			h.request_id = 0u;
			h.flags = 0u;
			s->reboot_after_frame = r->reboot;
			take_credit(s, &h);
			(void)finish_frame(g, &h, len);
			return true;
		}
#endif
		len = encode_answer(g, r, payload, room);
		s->reboot_after_frame = r->reboot;
	} else if (events_open(g) && (e = next_retained(s)) != NULL) {
#ifdef CONFIG_CTAG_GW_SECURE
		memcpy(&s->tx[CTAG_SERIAL_HEADER_LEN + OUTER_MAX + CTAG_SECURE_HEADER_LEN], e->payload,
		       e->len);
		len = seal_frame(g, e->type, CTAG_SERIAL_FLAG_EVENT, 0u, e->len);
		if (len < 0) {
			return false; /* kept: re-sent in the next privileged session */
		}
		s->next_retained = (uint64_t)e->seq + 1u;
		h.type = CTAG_SERIAL_MSG_SECURE_DATA;
#else
		s->next_retained = (uint64_t)e->seq + 1u;
		h.type = e->type;
		h.flags = CTAG_SERIAL_FLAG_EVENT;
		memcpy(payload, e->payload, e->len);
		len = e->len;
#endif
	} else if (events_open(g) && !gw_ring_empty(&s->evq)) {
		uint32_t n;
		uint8_t *rec = gw_ring_first(&s->evq, &n);

#ifdef CONFIG_CTAG_GW_SECURE
		memcpy(&s->tx[CTAG_SERIAL_HEADER_LEN + OUTER_MAX + CTAG_SECURE_HEADER_LEN], &rec[1],
		       n - 1u);
		len = seal_frame(g, rec[0], CTAG_SERIAL_FLAG_EVENT, 0u, (int)(n - 1u));
		gw_ring_free(&s->evq, rec);
		if (len < 0) {
			return true; /* best effort: lost with the session */
		}
		h.type = CTAG_SERIAL_MSG_SECURE_DATA;
#else
		h.type = rec[0];
		h.flags = CTAG_SERIAL_FLAG_EVENT;
		len = (int)(n - 1u);
		memcpy(payload, &rec[1], n - 1u);
		gw_ring_free(&s->evq, rec);
#endif
	} else {
		return false;
	}
	take_credit(s, &h);
	(void)finish_frame(g, &h, len);
	return true;
}

static void do_reboot(struct gw_core *g)
{
	g->s.reboot_at = 0;
	g->s.reboot_after_frame = false;
	g->c.reboots++;
	if (g->be->reboot != NULL) {
		g->be->reboot(g->be->ctx);
	}
}

void gw_serial_pump(struct gw_core *g)
{
	struct gw_serial *s = &g->s;

	for (;;) {
		if (s->tx_len > 0u) {
			if (!cobs_out(g)) {
				return; /* the driver calls gw_core_poll() when it has room */
			}
			if (s->reboot_after_frame) {
				/* 10: REBOOT answers first, then the device resets. */
				do_reboot(g);
				return;
			}
		}
		if (!next_frame(g)) {
			return;
		}
	}
}

int64_t gw_serial_deadline(const struct gw_core *g)
{
	return g->s.reboot_at != 0 ? g->s.reboot_at : GW_NEVER;
}

void gw_serial_timers(struct gw_core *g)
{
	if (g->s.reboot_at != 0 && g->now >= g->s.reboot_at) {
		do_reboot(g); /* the answer was dropped (HELLO) or is stuck: reset anyway */
	}
}

void gw_serial_reboot_after_answer(struct gw_core *g)
{
	struct gw_serial *s = &g->s;

	if (s->resp_count > 0u) {
		s->resp[(s->resp_head + s->resp_count - 1u) % N_RESP].reboot = true;
	}
	s->reboot_at = g->now + GW_REBOOT_GRACE_MS;
}

/* ---- Requests ---- */

static void on_hello(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct gw_serial *s = &g->s;
	struct ctag_cbor_field f[2] = {{.key = CTAG_CBOR_KEY_PROTO}, {.key = CTAG_CBOR_KEY_NAME}};
	uint8_t status = CTAG_STATUS_OK;

	if (ctag_cbor_decode(p, h->length, f, 2u) != 0 || !f[0].present || !f[1].present) {
		status = CTAG_STATUS_INVALID;
	} else if (f[0].v.u != CTAG_PROTO_VERSION) {
		status = CTAG_STATUS_VERSION_MISMATCH;
	}
	/* 10: drop unsent answers and best-effort events, restart the credit
	 * windows, answer, then re-send every retained event. */
	s->resp_head = s->resp_count = 0u;
	gw_ring_init(&s->evq, s->evq_buf, sizeof(s->evq_buf));
	s->hello_done = status == CTAG_STATUS_OK;
	s->send_credits = (uint16_t)(CTAG_SERIAL_DEFAULT_CREDITS + h->credits);
	s->host_budget = N_RESP;
	s->owed = 0u;
	s->next_retained = s->ret_count > 0u ? s->ret[s->ret_head].seq : (uint64_t)s->seq + 1u;
	s->hello_pending = true;
	s->hello_rid = h->request_id;
	s->hello_status = status;
	g->c.hellos++;
#ifdef CONFIG_CTAG_GW_SECURE
	/* 5: HELLO drops the secure session; retained events wait for the next one. */
	ctag_secure_close(&g->v2.ep);
#endif
}

static bool decode_req(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p,
		       const struct req_spec *sp, struct ctag_cbor_field *f)
{
	for (uint8_t i = 0; i < sp->n; i++) {
		f[i].key = sp->keys[i];
	}
	if (ctag_cbor_decode(p, h->length, f, sp->n) != 0) {
		g->c.invalid++;
		respond(g, h, CTAG_STATUS_INVALID, T_MALFORMED, false);
		return false;
	}
	for (uint8_t i = 0; i < sp->n; i++) {
		if ((sp->required & (1u << i)) != 0u && !f[i].present) {
			g->c.invalid++;
			respond(g, h, CTAG_STATUS_INVALID, T_MISSING, false);
			return false;
		}
	}
	return true;
}

static struct gw_req_result side_effect(struct gw_core *g, uint8_t type,
					 const struct ctag_cbor_field *f, uint16_t *tunnel)
{
	struct gw_req_result r = {CTAG_STATUS_UNSUPPORTED, NULL};

	(void)tunnel;
	switch (type) {
	case CTAG_SERIAL_MSG_REBOOT:
		r.status = CTAG_STATUS_OK;
		break;
	case CTAG_SERIAL_MSG_PROVISION:
		r = gw_provision(g, f[0].v.u, f[1].v.str.ptr, f[2].present ? (const char *)f[2].v.str.ptr : NULL,
				 f[2].present ? f[2].v.str.len : 0u,
#ifdef CONFIG_CTAG_GW_SECURE
				 f[3].v.str.ptr
#else
				 NULL
#endif
		);
		break;
	case CTAG_SERIAL_MSG_CONFIGURE_NODE:
		r = gw_configure(g, f[0].v.u, (uint16_t)f[1].v.u, f[2].v.b, (uint32_t)f[3].v.u);
		break;
	case CTAG_SERIAL_MSG_REMOVE_NODE:
		r = gw_remove(g, f[0].v.u, (uint16_t)f[1].v.u);
		break;
	case CTAG_SERIAL_MSG_ASSIGN_TAG:
		r = gw_mesh_op(g, GW_OP_ASSIGN, f[0].v.u, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u,
			       (uint32_t)f[3].v.u, f[4].v.str.ptr, 0u);
		break;
	case CTAG_SERIAL_MSG_UNASSIGN_TAG:
		r = gw_mesh_op(g, GW_OP_UNASSIGN, f[0].v.u, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u,
			       (uint32_t)f[3].v.u, NULL, 0u);
		break;
	case CTAG_SERIAL_MSG_TAG_COMMAND:
		if (f[4].v.u > UINT8_MAX) {
			r.status = CTAG_STATUS_INVALID;
			r.text = "cmd out of range";
			break;
		}
		r = gw_mesh_op(g, GW_OP_TAG_CMD, f[0].v.u, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u,
			       (uint32_t)f[3].v.u, NULL, (uint8_t)f[4].v.u);
		break;
	case CTAG_SERIAL_MSG_DELIVER_LAYOUT:
		r = gw_deliver(g, f[0].v.u, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u,
			       (uint32_t)f[3].v.u, (uint32_t)f[4].v.u, f[5].v.u, f[6].v.str.ptr,
			       f[7].v.str.ptr, f[7].v.str.len);
		break;
	case CTAG_SERIAL_MSG_CANCEL_DELIVERY:
		r = gw_cancel(g, f[1].v.u);
		break;
	case CTAG_SERIAL_MSG_IDENTIFY_NODE:
		r = gw_identify(g, (uint16_t)f[1].v.u);
		break;
#ifdef CONFIG_CTAG_GW_SECURE
	case CTAG_SERIAL_MSG_DISCOVER:
		r = gw_discover(g, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u, (uint32_t)f[3].v.u);
		break;
	case CTAG_SERIAL_MSG_TUNNEL_OPEN:
		r = gw_tunnel_open(g, (uint16_t)f[1].v.u, (uint32_t)f[2].v.u, (uint32_t)f[3].v.u,
				   f[4].present ? (uint32_t)f[4].v.u : CTAG_TUNNEL_MODE_PAIR, tunnel);
		break;
#endif
	default:
		break;
	}
	return r;
}

static void answer_side_effect(struct gw_core *g, const struct ctag_serial_header *h,
			       uint8_t status, const char *text, bool duplicate, uint16_t tunnel)
{
	struct gw_resp *r = respond(g, h, status, text, duplicate);

#ifdef CONFIG_CTAG_GW_SECURE
	if (h->type == CTAG_SERIAL_MSG_TUNNEL_OPEN && tunnel != 0u) {
		r->v2 = GW_V2_TUNNEL;
		r->tunnel = tunnel;
	}
#else
	(void)r;
	(void)tunnel;
#endif
}

static void dispatch(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct gw_serial *s = &g->s;
	const struct req_spec *sp = spec_of(h->type);
	struct ctag_cbor_field f[REQ_FIELDS];

	if (sp == NULL) {
		g->c.unsupported++;
		respond(g, h, CTAG_STATUS_UNSUPPORTED, NULL, false);
		return;
	}
	if (!decode_req(g, h, p, sp, f)) {
		return;
	}
	if (side_effecting(h->type)) {
		uint64_t op_id = f[0].v.u;
		const struct gw_idem *seen = idem_find(s, op_id);
		struct gw_req_result r;
		uint16_t tunnel = 0u;

		if (seen != NULL) {
			/* 1.4: the remembered status, detail DUPLICATE, no new work. */
			g->c.duplicate_ops++;
			answer_side_effect(g, h, seen->status, seen->text, true, seen->tunnel);
			return;
		}
		r = side_effect(g, h->type, f, &tunnel);
		if (!transient(r.status)) {
			idem_put(s, op_id, r.status, r.text, tunnel);
		}
		answer_side_effect(g, h, r.status, r.text, false, tunnel);
		if (h->type == CTAG_SERIAL_MSG_REBOOT) {
			gw_serial_reboot_after_answer(g);
		}
		return;
	}
	switch (h->type) {
	case CTAG_SERIAL_MSG_EVENT_ACK:
		release_retained(s, (uint32_t)f[0].v.u);
		respond(g, h, CTAG_STATUS_OK, NULL, false);
		break;
	case CTAG_SERIAL_MSG_SCAN_UNPROV:
		gw_scan(g, (uint32_t)f[0].v.u, f[1].present ? f[1].v.str.ptr : NULL,
			f[1].present ? f[1].v.str.len : 0u);
		respond(g, h, CTAG_STATUS_OK, NULL, false);
		break;
	case CTAG_SERIAL_MSG_GET_INVENTORY:
		gw_refresh_inventory(g); /* fresher data follows as EVT_BRIDGE_INFO */
		respond(g, h, CTAG_STATUS_OK, NULL, false);
		break;
#ifdef CONFIG_CTAG_GW_SECURE
	case CTAG_SERIAL_MSG_TUNNEL_SEND: {
		struct gw_req_result r = gw_tunnel_send(g, (uint32_t)f[0].v.u, f[1].v.str.ptr,
							 f[1].v.str.len);

		respond(g, h, r.status, r.text, false);
		break;
	}
	case CTAG_SERIAL_MSG_TUNNEL_CLOSE: {
		struct gw_req_result r = gw_tunnel_close(g, (uint32_t)f[0].v.u);

		respond(g, h, r.status, r.text, false);
		break;
	}
#endif
	default: /* PING, INFO, LIST_NODES, GET_COUNTERS: answered when sent */
		respond(g, h, CTAG_STATUS_OK, NULL, false);
		break;
	}
}

#ifdef CONFIG_CTAG_GW_SECURE

struct gw_resp *gw_respond(struct gw_core *g, const struct ctag_serial_header *h, uint8_t status,
			   const char *text, bool duplicate)
{
	return respond(g, h, status, text, duplicate);
}

/* The v1 catalogue and the v2 operational messages, once the access table allowed them. */
void gw_dispatch_inner(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	dispatch(g, h, p);
}

void gw_serial_drop_secure(struct gw_core *g)
{
	struct gw_serial *s = &g->s;
	uint8_t kept = 0u;

	for (uint8_t i = 0; i < s->resp_count; i++) {
		struct gw_resp r = s->resp[(s->resp_head + i) % N_RESP];

		if (r.secure) {
			if (s->owed < UINT16_MAX) {
				s->owed++; /* the host's credit comes back all the same */
			}
			continue;
		}
		s->resp[(s->resp_head + kept) % N_RESP] = r;
		kept++;
	}
	s->resp_count = kept;
}

void gw_serial_resend_retained(struct gw_core *g)
{
	struct gw_serial *s = &g->s;

	s->next_retained = s->ret_count > 0u ? s->ret[s->ret_head].seq : (uint64_t)s->seq + 1u;
}

void gw_serial_forget(struct gw_core *g)
{
	struct gw_serial *s = &g->s;

	s->ret_head = s->ret_count = 0u;
	s->next_retained = (uint64_t)s->seq + 1u;
	gw_ring_init(&s->evq, s->evq_buf, sizeof(s->evq_buf));
	memset(s->idem, 0, sizeof(s->idem));
	s->idem_next = 0u;
}

#endif

static void on_frame(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct gw_serial *s = &g->s;

	g->c.frames_rx++;
	if ((h->flags & (CTAG_SERIAL_FLAG_RESPONSE | CTAG_SERIAL_FLAG_EVENT)) != 0u) {
		g->c.unexpected_frames++;
		return;
	}
	if (h->type == CTAG_SERIAL_MSG_HELLO) {
		on_hello(g, h, p); /* exempt from credits; always accepted */
		return;
	}
	if (!s->hello_done) {
		g->c.overruns++; /* nothing is answered before a HELLO */
		return;
	}
	s->send_credits = (uint16_t)(s->send_credits > UINT16_MAX - h->credits
					     ? UINT16_MAX
					     : s->send_credits + h->credits);
	if (--s->host_budget < 0) {
		g->c.credit_violations++;
	}
	if (s->resp_count >= N_RESP) {
		/* 1.3: sent without credit and no buffer free: dropped. */
		g->c.overruns++;
		return;
	}
#ifdef CONFIG_CTAG_GW_SECURE
	gw_v2_outer(g, h, p);
#else
	dispatch(g, h, p);
#endif
}

void gw_core_rx(struct gw_core *g, const uint8_t *data, size_t len, int64_t now)
{
	struct ctag_serial_header h;

	g->now = now;
	for (size_t i = 0; i < len; i++) {
		if (ctag_serial_rx_put(&g->s.rx, data[i], &h)) {
			on_frame(g, &h, &g->s.rxbuf[CTAG_SERIAL_HEADER_LEN]);
		}
	}
	gw_serial_pump(g);
}
