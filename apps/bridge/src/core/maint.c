/* Bridge maintenance port (docs/protocol.md 1.6, docs/fontpack.md 4). */
#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#include "maint.h"

#define F_UINT(k, val) {.key = (k), .kind = CTAG_CBOR_UINT, .v.u = (val)}
#define F_TSTR(k, s)                                                                            \
	{.key = (k), .kind = CTAG_CBOR_TSTR, .v.str = {(const uint8_t *)(s), strlen(s)}}
#define F_BSTR(k, p, n) {.key = (k), .kind = CTAG_CBOR_BSTR, .v.str = {(p), (n)}}

void maint_init(struct maint *m, const struct maint_io *io, void *ctx, struct fontstore *fonts,
		const struct ctag_sha256_ops *sha, uint32_t boot_id, const char *fw,
		const char *build, uint8_t board)
{
	memset(m, 0, sizeof(*m));
	m->io = io;
	m->ctx = ctx;
	m->fonts = fonts;
	m->sha = sha;
	m->boot_id = boot_id;
	m->fw = fw;
	m->build = build;
	m->board = board;
	ctag_serial_rx_init(&m->rx, m->rxbuf, sizeof(m->rxbuf));
	ctag_credits_reset(&m->cr);
}

/* Written straight into items (no copy of the list on the thread's stack). */
#define COUNTER(lit, val)                                                                          \
	do {                                                                                       \
		if (n < max) {                                                                     \
			items[n++] = (struct ctag_cbor_counter)CTAG_CBOR_COUNTER(lit, (val));      \
		}                                                                                  \
	} while (0)

size_t maint_counters(const struct maint *m, struct ctag_cbor_counter *items, size_t max)
{
	size_t n = 0u;

	COUNTER("frames_rx", m->c.frames_rx);
	COUNTER("frames_tx", m->c.frames_tx);
	COUNTER("len_errors", m->rx.len_errors);
	COUNTER("crc_errors", m->rx.crc_errors);
	COUNTER("version_errors", m->rx.version_errors);
	COUNTER("oversize", m->rx.cobs.oversize);
	COUNTER("overruns", m->c.overruns);
	COUNTER("unsupported", m->c.unsupported);
	COUNTER("invalid", m->c.invalid);
	COUNTER("hellos", m->c.hellos);
	COUNTER("credit_violations", m->c.credit_violations);
	COUNTER("fonts_installed", m->fonts->installs);
	return n;
}

/* ---- Sending ---- */

/*
 * COBS-encode the decoded frame in tx straight to the port, block by block
 * (no second buffer), with the 0x00 delimiter: the same bytes as
 * ctag_serial_wire_encode() (no trailing 0x01 block after a final 0xFF block).
 */
void maint_write_frame(struct maint *m, size_t len)
{
	static const uint8_t delimiter;
	size_t start = 0u;

	for (;;) {
		size_t i = start;
		uint8_t code;

		while (i < len && m->tx[i] != 0u && i - start < 254u) {
			i++;
		}
		code = (uint8_t)(i - start + 1u);
		m->io->write(m->ctx, &code, 1u);
		m->io->write(m->ctx, &m->tx[start], i - start);
		if (code == 0xFFu) {
			start = i; /* a full block implies no zero */
			if (start == len) {
				break;
			}
			continue;
		}
		if (i == len) {
			break;
		}
		start = i + 1u; /* skip the zero this block stands for */
	}
	m->io->write(m->ctx, &delimiter, 1u);
	m->c.frames_tx++;
}

static void flush_held(struct maint *m)
{
	if (m->held > 0u && ctag_credits_take(&m->cr)) {
		maint_write_frame(m, m->held);
		m->held = 0u;
	}
}

/* Encode fields as the response to (type, rid); exempt = HELLO (no credit). */
static void respond(struct maint *m, uint8_t type, uint16_t rid, const struct ctag_cbor_field *f,
		    size_t n, bool exempt)
{
	struct ctag_serial_header hdr = {
		.version = CTAG_PROTO_VERSION,
		.type = type,
		.request_id = rid,
		.flags = CTAG_SERIAL_FLAG_RESPONSE,
	};
	const struct ctag_cbor_field internal = F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_INTERNAL);
	bool credit = exempt || ctag_credits_take(&m->cr);
	int len = ctag_cbor_encode(f, n, &m->tx[CTAG_SERIAL_HEADER_LEN],
				   sizeof(m->tx) - CTAG_SERIAL_MIN_FRAME);

	if (len < 0) {
		len = ctag_cbor_encode(&internal, 1u, &m->tx[CTAG_SERIAL_HEADER_LEN],
				       sizeof(m->tx) - CTAG_SERIAL_MIN_FRAME);
	}
	if (credit && !exempt) {
		hdr.credits = ctag_credits_grant(&m->cr);
		m->host_budget += hdr.credits;
	}
	len = ctag_serial_frame_build(m->tx, sizeof(m->tx), &hdr, NULL, (size_t)len);
	if (len <= 0) {
		return;
	}
	if (credit) {
		maint_write_frame(m, (size_t)len);
		return;
	}
	/* No credit from the host: hold this one response in tx (1.3); a newer
	 * response replaces it. */
	m->c.tx_held++;
	if (m->held > 0u) {
		m->c.tx_dropped++;
	}
	m->held = (size_t)len;
}

static void respond_status(struct maint *m, const struct ctag_serial_header *h, uint8_t status,
			   const char *text)
{
	struct ctag_cbor_field f[2] = {F_UINT(CTAG_CBOR_KEY_STATUS, status)};
	size_t n = 1u;

	if (text != NULL) {
		f[n++] = (struct ctag_cbor_field)F_TSTR(CTAG_CBOR_KEY_TEXT, text);
	}
	respond(m, h->type, h->request_id, f, n, false);
}

/* ---- Requests ---- */

/* Decode the wanted keys; false (and INVALID answered) when malformed or a
 * required key is missing. required: bit i = fields[i] must be present. */
static bool decode(struct maint *m, const struct ctag_serial_header *h, const uint8_t *payload,
		   struct ctag_cbor_field *f, size_t n, uint32_t required)
{
	size_t i;

	if (h->length > 0u && ctag_cbor_decode(payload, h->length, f, n) != 0) {
		m->c.invalid++;
		respond_status(m, h, CTAG_STATUS_INVALID, "malformed CBOR payload");
		return false;
	}
	for (i = 0; i < n; i++) {
		if ((required & BIT(i)) && !f[i].present) {
			m->c.invalid++;
			respond_status(m, h, CTAG_STATUS_INVALID, "missing field");
			return false;
		}
	}
	return true;
}

static void caps_fields(const struct maint *m, struct ctag_cbor_field caps[4])
{
	caps[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_MAX_FRAME, MAINT_MAX_FRAME);
	caps[1] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_CREDITS, MAINT_CREDITS);
	caps[2] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_ROLE, CTAG_NODE_ROLE_BRIDGE);
	caps[3] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_BOARD, m->board);
}

static void on_hello(struct maint *m, const struct ctag_serial_header *h, const uint8_t *payload)
{
	struct ctag_cbor_field in[2] = {{.key = CTAG_CBOR_KEY_PROTO}, {.key = CTAG_CBOR_KEY_NAME}};
	struct ctag_cbor_field caps[4];
	struct ctag_cbor_field f[6];
	uint8_t status = CTAG_STATUS_OK;
	size_t n;

	if (h->length > 0u && ctag_cbor_decode(payload, h->length, in, ARRAY_SIZE(in)) != 0) {
		status = CTAG_STATUS_INVALID;
	} else if (!in[0].present || in[0].v.u != CTAG_PROTO_VERSION) {
		status = in[0].present ? CTAG_STATUS_VERSION_MISMATCH : CTAG_STATUS_INVALID;
	}
	/* 10: HELLO resets the link: unsent answers are dropped, credits restart. */
	m->held = 0u;
	m->hello_done = status == CTAG_STATUS_OK;
	m->cr.tx = (uint16_t)(CTAG_SERIAL_DEFAULT_CREDITS + h->credits);
	m->cr.owed = 0u;
	m->host_budget = MAINT_CREDITS;
	m->c.hellos++;
	f[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, status);
	f[1] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION);
	n = 2u;
	if (status == CTAG_STATUS_OK) {
		caps_fields(m, caps);
		f[n++] = (struct ctag_cbor_field)F_TSTR(CTAG_CBOR_KEY_FW, m->fw);
		f[n++] = (struct ctag_cbor_field)F_TSTR(CTAG_CBOR_KEY_BUILD, m->build);
		f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_BOOT_ID, m->boot_id);
		f[n].key = CTAG_CBOR_KEY_CAPS;
		f[n].kind = CTAG_CBOR_MAP;
		f[n].v.map.fields = caps;
		f[n++].v.map.count = ARRAY_SIZE(caps);
	}
	respond(m, CTAG_SERIAL_MSG_HELLO, h->request_id, f, n, true);
}

static void on_info(struct maint *m, const struct ctag_serial_header *h)
{
	struct ctag_cbor_counter items[MAINT_COUNTERS];
	struct ctag_cbor_field caps[4];
	struct ctag_cbor_field f[6];
	size_t n = maint_counters(m, items, ARRAY_SIZE(items));

	if (m->io->counters != NULL) {
		n += m->io->counters(m->ctx, &items[n], ARRAY_SIZE(items) - n);
	}
	caps_fields(m, caps);
	f[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	f[1] = (struct ctag_cbor_field)F_TSTR(CTAG_CBOR_KEY_FW, m->fw);
	f[2] = (struct ctag_cbor_field)F_TSTR(CTAG_CBOR_KEY_BUILD, m->build);
	f[3] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_BOOT_ID, m->boot_id);
	f[4].key = CTAG_CBOR_KEY_CAPS;
	f[4].kind = CTAG_CBOR_MAP;
	f[4].v.map.fields = caps;
	f[4].v.map.count = ARRAY_SIZE(caps);
	f[5].key = CTAG_CBOR_KEY_COUNTERS;
	f[5].kind = CTAG_CBOR_COUNTERS;
	f[5].v.counters.items = items;
	f[5].v.counters.count = n;
	/* INFO always answers: counters that would not fit the frame are left
	 * out (tx is free to encode into: see on_font_commit()). */
	while (f[5].v.counters.count > 0u &&
	       ctag_cbor_encode(f, ARRAY_SIZE(f), &m->tx[CTAG_SERIAL_HEADER_LEN],
				sizeof(m->tx) - CTAG_SERIAL_MIN_FRAME) < 0) {
		f[5].v.counters.count--;
	}
	respond(m, h->type, h->request_id, f, ARRAY_SIZE(f), false);
}

static void on_font_status(struct maint *m, const struct ctag_serial_header *h)
{
	struct fontstore *fs = m->fonts;
	struct ctag_cbor_field f[5];
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	size_t n = 0u;

	f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_FLASH_SIZE, fs->flash->geom.flash_size);
	/* Only a pack that passed the boot validation is reported as active. */
	if (fontstore_active_id(fs, id)) {
		f[n++] = (struct ctag_cbor_field)F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, id, sizeof(id));
		f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_SLOT, fs->active.slot);
		f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_SIZE, fs->active.size);
	}
	respond(m, h->type, h->request_id, f, n, false);
}

static const char *status_text(uint8_t st)
{
	switch (st) {
	case CTAG_STATUS_NO_RESOURCES:
		return "flash too small for two slots and the working space";
	case CTAG_STATUS_TOO_LARGE:
		return "pack larger than a slot";
	case CTAG_STATUS_BUSY:
		return "a tag session still reads that slot";
	case CTAG_STATUS_NOT_FOUND:
		return "no installation in progress";
	case CTAG_STATUS_INCOMPLETE:
		return "not every byte was written";
	case CTAG_STATUS_DIGEST_MISMATCH:
		return "SHA-256 differs";
	case CTAG_STATUS_STORAGE_ERROR:
		return "flash error";
	default:
		return NULL;
	}
}

static void on_font_begin(struct maint *m, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field in[3] = {{.key = CTAG_CBOR_KEY_SIZE},
					{.key = CTAG_CBOR_KEY_DIGEST},
					{.key = CTAG_CBOR_KEY_FONTPACK_ID}};
	struct ctag_cbor_field f[3];
	uint8_t slot = 0u;
	uint8_t st;

	if (!decode(m, h, p, in, ARRAY_SIZE(in), BIT(0) | BIT(1) | BIT(2))) {
		return;
	}
	if (in[1].v.str.len != 32u) {
		respond_status(m, h, CTAG_STATUS_INVALID, "digest must be 32 bytes");
		return;
	}
	st = fontstore_begin(m->fonts, (uint32_t)in[0].v.u, in[1].v.str.ptr, in[2].v.str.ptr, &slot);
	if (st != CTAG_STATUS_OK) {
		respond_status(m, h, st, status_text(st));
		return;
	}
	f[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, st);
	f[1] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_SLOT, slot);
	f[2] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_FLASH_SIZE,
					      m->fonts->flash->geom.flash_size);
	respond(m, h->type, h->request_id, f, ARRAY_SIZE(f), false);
}

static void on_font_data(struct maint *m, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field in[2] = {{.key = CTAG_CBOR_KEY_OFFSET}, {.key = CTAG_CBOR_KEY_DATA}};
	uint8_t st;

	if (!decode(m, h, p, in, ARRAY_SIZE(in), BIT(0) | BIT(1))) {
		return;
	}
	st = fontstore_data(m->fonts, (uint32_t)in[0].v.u, in[1].v.str.ptr, in[1].v.str.len);
	respond_status(m, h, st,
		       st == CTAG_STATUS_INVALID ? "FONT_DATA offset or size" : status_text(st));
}

static void on_font_commit(struct maint *m, const struct ctag_serial_header *h)
{
	struct ctag_cbor_field f[2];
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	/* tx as the read buffer: nothing it holds survives this request's answer. */
	uint8_t st = fontstore_commit(m->fonts, m->sha, m->tx, sizeof(m->tx), id);

	if (st != CTAG_STATUS_OK) {
		respond_status(m, h, st, status_text(st));
		return;
	}
	f[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, st);
	f[1] = (struct ctag_cbor_field)F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, id, sizeof(id));
	respond(m, h->type, h->request_id, f, ARRAY_SIZE(f), false);
}

static void on_flash_test(struct maint *m, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field in[1] = {{.key = CTAG_CBOR_KEY_OP_ID}};
	struct ctag_cbor_field item_f[FONTSTORE_TEST_MAX][2];
	struct ctag_cbor_map items[FONTSTORE_TEST_MAX];
	struct ctag_cbor_field f[4];
	struct maint_flash_test *t = NULL;
	bool dup = false;
	size_t i, n = 0u;

	if (!decode(m, h, p, in, ARRAY_SIZE(in), BIT(0))) {
		return;
	}
	for (i = 0; i < MAINT_FT_CACHE; i++) {
		if (m->ft[i].used && m->ft[i].op_id == in[0].v.u) {
			t = &m->ft[i];
			dup = true; /* 1.4: the remembered result, no new work */
		}
	}
	if (t == NULL) {
		t = &m->ft[m->ft_next];
		m->ft_next = (uint8_t)((m->ft_next + 1u) % MAINT_FT_CACHE);
		t->used = true;
		t->op_id = in[0].v.u;
		t->n = (uint8_t)fontstore_flash_test(m->fonts, t->items, ARRAY_SIZE(t->items));
	}
	for (i = 0; i < t->n; i++) {
		item_f[i][0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, t->items[i].status);
		item_f[i][1] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_OFFSET, t->items[i].offset);
		items[i].fields = item_f[i];
		items[i].count = 2u;
	}
	f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
	f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_FLASH_SIZE,
						m->fonts->flash->geom.flash_size);
	f[n].key = CTAG_CBOR_KEY_ITEMS;
	f[n].kind = CTAG_CBOR_MAPS;
	f[n].v.maps.items = items;
	f[n++].v.maps.count = t->n;
	if (dup) {
		f[n++] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_DETAIL, CTAG_STATUS_DUPLICATE);
	}
	respond(m, h->type, h->request_id, f, n, false);
}

static bool supported(uint8_t type)
{
	switch (type) {
	case CTAG_SERIAL_MSG_PING:
	case CTAG_SERIAL_MSG_INFO:
	case CTAG_SERIAL_MSG_REBOOT:
	case CTAG_SERIAL_MSG_EVENT_ACK:
	case CTAG_SERIAL_MSG_FONT_BEGIN:
	case CTAG_SERIAL_MSG_FONT_DATA:
	case CTAG_SERIAL_MSG_FONT_COMMIT:
	case CTAG_SERIAL_MSG_FONT_STATUS:
	case CTAG_SERIAL_MSG_FONT_ABORT:
	case CTAG_SERIAL_MSG_FLASH_TEST:
		return true;
	default:
		return false;
	}
}

static void dispatch(struct maint *m, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field f[2];
	struct ctag_cbor_field in[1];

	if (!supported(h->type)) {
		/* Unknown types, and mesh/delivery requests (1.6: never on this port). */
		m->c.unsupported++;
		respond_status(m, h, CTAG_STATUS_UNSUPPORTED, NULL);
		return;
	}
	/* Every payload must be a well-formed, well-typed map (1.1). */
	if (h->length > 0u && ctag_cbor_decode(p, h->length, NULL, 0u) != 0) {
		m->c.invalid++;
		respond_status(m, h, CTAG_STATUS_INVALID, "malformed CBOR payload");
		return;
	}
	switch (h->type) {
	case CTAG_SERIAL_MSG_PING:
		f[0] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
		f[1] = (struct ctag_cbor_field)F_UINT(CTAG_CBOR_KEY_UPTIME_S,
						      (uint64_t)(k_uptime_get() / 1000));
		respond(m, h->type, h->request_id, f, 2u, false);
		break;
	case CTAG_SERIAL_MSG_INFO:
		on_info(m, h);
		break;
	case CTAG_SERIAL_MSG_REBOOT:
		in[0] = (struct ctag_cbor_field){.key = CTAG_CBOR_KEY_OP_ID};
		if (decode(m, h, p, in, 1u, BIT(0))) {
			respond_status(m, h, CTAG_STATUS_OK, NULL);
			/* 10: answer first, then reset (USB re-enumerates, new boot_id). */
			if (m->io->reboot != NULL) {
				m->io->reboot(m->ctx);
			}
		}
		break;
	case CTAG_SERIAL_MSG_EVENT_ACK:
		in[0] = (struct ctag_cbor_field){.key = CTAG_CBOR_KEY_SEQ};
		if (decode(m, h, p, in, 1u, BIT(0))) {
			respond_status(m, h, CTAG_STATUS_OK, NULL); /* the port retains no events */
		}
		break;
	case CTAG_SERIAL_MSG_FONT_BEGIN:
		on_font_begin(m, h, p);
		break;
	case CTAG_SERIAL_MSG_FONT_DATA:
		on_font_data(m, h, p);
		break;
	case CTAG_SERIAL_MSG_FONT_COMMIT:
		on_font_commit(m, h);
		break;
	case CTAG_SERIAL_MSG_FONT_STATUS:
		on_font_status(m, h);
		break;
	case CTAG_SERIAL_MSG_FONT_ABORT:
		fontstore_abort(m->fonts);
		respond_status(m, h, CTAG_STATUS_OK, NULL);
		break;
	default:
		on_flash_test(m, h, p);
		break;
	}
}

static void on_frame(struct maint *m, const struct ctag_serial_header *h, const uint8_t *payload)
{
	m->c.frames_rx++;
	if (h->flags & (CTAG_SERIAL_FLAG_RESPONSE | CTAG_SERIAL_FLAG_EVENT)) {
		m->c.unexpected++;
		return;
	}
	if (h->type == CTAG_SERIAL_MSG_HELLO) {
		on_hello(m, h, payload); /* exempt from credits; resets the session */
		return;
	}
	if (!m->hello_done) {
		m->c.overruns++; /* nothing is answered before a HELLO */
		return;
	}
	ctag_credits_add(&m->cr, h->credits);
	flush_held(m);
	if (--m->host_budget < 0) {
		m->c.credit_violations++;
	}
	/* Processed synchronously: the receive buffer is free again before the
	 * answer, whose credits byte grants it back. */
	ctag_credits_release(&m->cr);
	dispatch(m, h, payload);
}

void maint_rx(struct maint *m, const uint8_t *data, size_t len)
{
	struct ctag_serial_header h;
	size_t i;

	for (i = 0; i < len; i++) {
		if (ctag_serial_rx_put(&m->rx, data[i], &h)) {
			on_frame(m, &h, &m->rxbuf[CTAG_SERIAL_HEADER_LEN]);
		}
	}
}
