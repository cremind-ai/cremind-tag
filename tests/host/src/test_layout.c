/* ctag_layout against layouts.json, plus the LAYOUT_BEGIN/CHUNK/COMMIT assembler. */
#include <string.h>

#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_layout.h>

#include "check.h"
#include "suites.h"
#include "support.h"
#include "v_fontpack.h"
#include "v_layouts.h"

static struct ctag_fontpack fp;
static struct ctag_glyph_source src;
static struct t_mem mem;
static uint8_t data[CTAG_LAYOUT_HARD_MAX];
static uint8_t asm_buf[CTAG_LAYOUT_HARD_MAX];

static bool open_pack(void)
{
	mem.data = V_FONTPACK_DATA;
	mem.len = V_FONTPACK_LEN;
	if (ctag_fontpack_open(&fp, t_mem_read, &mem, V_FONTPACK_LEN) != CTAG_STATUS_OK) {
		return false;
	}
	ctag_fontpack_glyph_source(&fp, &src);
	return true;
}

void test_layout_valid(void)
{
	uint8_t digest[32];
	size_t i;

	CHECK(open_pack());
	for (i = 0u; i < V_COUNT(v_layouts_valid); i++) {
		const struct v_layout *v = &v_layouts_valid[i];

		CHECK_CASE(ctag_layout_validate(v->data, v->len, src.has_strike, src.ctx) ==
				   CTAG_STATUS_OK,
			   v->name);
		CHECK_CASE(ctag_layout_validate(v->data, v->len, NULL, NULL) == CTAG_STATUS_OK,
			   v->name);
		t_sha256(v->data, v->len, digest);
		CHECK_CASE(memcmp(digest, v->digest, CTAG_LAYOUT_DIGEST_LEN) == 0, v->name);
	}
}

void test_layout_invalid(void)
{
	size_t i;

	CHECK(open_pack());
	for (i = 0u; i < V_COUNT(v_layouts_invalid); i++) {
		const struct v_layout *v = &v_layouts_invalid[i];

		CHECK_CASE(ctag_layout_validate(v->data, v->len, src.has_strike, src.ctx) ==
				   v->status,
			   v->name);
	}
	/* QR text bytes at both ends of 0x21..0x7E (the fixtures cover 0x20 only). */
	for (i = 0u; i < V_COUNT(v_layouts_valid); i++) {
		const struct v_layout *v = &v_layouts_valid[i];

		if (strcmp(v->name, "qr_only") == 0) {
			memcpy(data, v->data, v->len);
			data[v->len - 1u] = 0x7Eu;
			CHECK(ctag_layout_validate(data, v->len, NULL, NULL) == CTAG_STATUS_OK);
			data[v->len - 1u] = 0x7Fu;
			CHECK(ctag_layout_validate(data, v->len, NULL, NULL) ==
			      CTAG_STATUS_INVALID);
			data[v->len - 1u] = 0x21u;
			CHECK(ctag_layout_validate(data, v->len, NULL, NULL) == CTAG_STATUS_OK);
		}
	}
}

void test_layout_panel(void)
{
	struct ctag_layout_header h;
	struct ctag_layout_iter it;
	size_t i;

	for (i = 0u; i < V_COUNT(v_panel_checks); i++) {
		const struct v_panel_check *v = &v_panel_checks[i];
		const struct v_layout *l = &v_layouts_valid[v->layout];

		CHECK_CASE(ctag_layout_iter_init(&it, l->data, l->len, &h) == 0, l->name);
		CHECK_CASE(ctag_layout_check_panel(&h, v->width, v->height) == v->status, l->name);
	}
}

void test_layout_iter(void)
{
	struct ctag_layout_header h;
	struct ctag_layout_iter it;
	struct ctag_layout_command cmd;
	unsigned int seen = 0u;
	size_t i;

	for (i = 0u; i < V_COUNT(v_layouts_valid); i++) {
		const struct v_layout *v = &v_layouts_valid[i];
		unsigned int n = 0u;

		CHECK_CASE(ctag_layout_iter_init(&it, v->data, v->len, &h) == 0, v->name);
		while (ctag_layout_iter_next(&it, &cmd)) {
			CHECK_CASE(cmd.op >= CTAG_LAYOUT_CMD_CLEAR && cmd.op <= CTAG_LAYOUT_CMD_QR,
				   v->name);
			CHECK_CASE(v->data[cmd.offset] == cmd.op, v->name);
			seen |= 1u << cmd.op;
			n++;
		}
		CHECK_CASE(n == h.cmd_count && it.pos == v->len, v->name);
	}
	CHECK(seen == 0xFEu); /* every command op 1..7 appears */
	CHECK(ctag_layout_iter_init(&it, data, CTAG_LAYOUT_HEADER_LEN - 1u, &h) != 0);
}

/* A structurally valid layout of count one-step LINE commands (points, within
 * the LAYOUT_MAX_LINE_STEPS render-cost bound); returns its length. */
static size_t make_lines(uint8_t *out, uint16_t count)
{
	struct ctag_layout_header h = {
		CTAG_LAYOUT_MAGIC, CTAG_PROTO_VERSION, 0u, 400u, 300u, 0u, 0u, count};
	struct ctag_layout_cmd_line l = {0, 0, 0, 0, 1u, CTAG_COLOR_BLACK};
	size_t pos = (size_t)ctag_layout_header_pack(&h, out, CTAG_LAYOUT_HEADER_LEN);
	uint16_t i;

	for (i = 0u; i < count; i++) {
		out[pos++] = CTAG_LAYOUT_CMD_LINE;
		l.x0 = (int16_t)i;
		l.x1 = (int16_t)i;
		pos += (size_t)ctag_layout_cmd_line_pack(&l, &out[pos], CTAG_LAYOUT_CMD_LINE_LEN);
	}
	return pos;
}

static void begin(struct ctag_layout_asm *a, uint16_t xfer, size_t total, uint8_t chunks,
		  const uint8_t *layout)
{
	struct ctag_mesh_layout_begin b;
	uint8_t digest[32];

	memset(&b, 0, sizeof(b));
	b.xfer_id = xfer;
	b.total_len = (uint16_t)total;
	b.chunk_count = chunks;
	t_sha256(layout, total, digest);
	memcpy(b.digest, digest, sizeof(b.digest));
	ctag_layout_asm_begin(a, &b);
}

static uint8_t chunk(struct ctag_layout_asm *a, uint16_t xfer, uint8_t index, const uint8_t *layout,
		     size_t len)
{
	struct ctag_mesh_layout_chunk c = {xfer, index, &layout[index * CTAG_LAYOUT_CHUNK_DATA_MAX],
					   len};

	return ctag_layout_asm_chunk(a, &c);
}

static size_t chunk_len(size_t total, uint8_t index)
{
	size_t off = (size_t)index * CTAG_LAYOUT_CHUNK_DATA_MAX;

	return total - off < CTAG_LAYOUT_CHUNK_DATA_MAX ? total - off : CTAG_LAYOUT_CHUNK_DATA_MAX;
}

void test_layout_asm(void)
{
	static struct ctag_layout_asm a;
	struct ctag_sha256_ops sha;
	struct t_sha256 ctx;
	size_t len = make_lines(data, 250u);
	uint8_t n = (uint8_t)((len + CTAG_LAYOUT_CHUNK_DATA_MAX - 1u) / CTAG_LAYOUT_CHUNK_DATA_MAX);
	uint32_t missing;
	int i;

	t_sha256_ops(&sha, &ctx);
	CHECK(ctag_layout_validate(data, len, NULL, NULL) == CTAG_STATUS_OK);
	CHECK(n == 19u);
	ctag_layout_asm_init(&a, asm_buf, sizeof(asm_buf));
	CHECK(ctag_layout_asm_commit(&a, 7u, &missing, &sha) == CTAG_STATUS_NOT_FOUND);
	begin(&a, 7u, len, n, data);
	/* Out of order, chunk 5 held back. */
	for (i = n - 1; i >= 0; i--) {
		if (i != 5) {
			CHECK(chunk(&a, 7u, (uint8_t)i, data, chunk_len(len, (uint8_t)i)) ==
			      CTAG_STATUS_OK);
		}
	}
	CHECK(chunk(&a, 8u, 5u, data, CTAG_LAYOUT_CHUNK_DATA_MAX) == CTAG_STATUS_NOT_FOUND);
	CHECK(chunk(&a, 7u, n, data, 10u) == CTAG_STATUS_INVALID);
	CHECK(ctag_layout_asm_commit(&a, 8u, &missing, &sha) == CTAG_STATUS_NOT_FOUND &&
	      missing == 0u);
	CHECK(ctag_layout_asm_commit(&a, 7u, &missing, &sha) == CTAG_STATUS_INCOMPLETE);
	CHECK(missing == (uint32_t)1u << 5);
	CHECK(chunk(&a, 7u, 5u, data, CTAG_LAYOUT_CHUNK_DATA_MAX) == CTAG_STATUS_OK);
	CHECK(ctag_layout_asm_commit(&a, 7u, &missing, &sha) == CTAG_STATUS_OK && missing == 0u);
	CHECK(memcmp(asm_buf, data, len) == 0);
	/* A repeated commit answers the same. */
	CHECK(ctag_layout_asm_commit(&a, 7u, &missing, &sha) == CTAG_STATUS_OK);
	/* A new transfer replaces the old one. */
	begin(&a, 9u, len, n, data);
	CHECK(chunk(&a, 7u, 0u, data, CTAG_LAYOUT_CHUNK_DATA_MAX) == CTAG_STATUS_NOT_FOUND);
	CHECK(ctag_layout_asm_commit(&a, 9u, &missing, &sha) == CTAG_STATUS_INCOMPLETE);
	CHECK(missing == ((uint32_t)1u << n) - 1u);
	/* Digest mismatch. */
	for (i = 0; i < n; i++) {
		CHECK(chunk(&a, 9u, (uint8_t)i, data, chunk_len(len, (uint8_t)i)) ==
		      CTAG_STATUS_OK);
	}
	a.begin.digest[0] ^= 1u;
	CHECK(ctag_layout_asm_commit(&a, 9u, &missing, &sha) == CTAG_STATUS_DIGEST_MISMATCH);
	ctag_layout_asm_cancel(&a);
	CHECK(ctag_layout_asm_commit(&a, 9u, &missing, &sha) == CTAG_STATUS_NOT_FOUND);
}

void test_layout_asm_errors(void)
{
	static struct ctag_layout_asm a;
	struct ctag_sha256_ops sha;
	struct t_sha256 ctx;
	uint32_t missing;
	uint8_t i;

	t_sha256_ops(&sha, &ctx);
	memset(data, 0x5A, sizeof(data));
	ctag_layout_asm_init(&a, asm_buf, sizeof(asm_buf));
	/* 28 full chunks: 4200 bytes > LAYOUT_HARD_MAX; the tail is never written past the buffer.
	 */
	begin(&a, 1u, CTAG_LAYOUT_HARD_MAX, 28u, data);
	a.begin.total_len = 28u * CTAG_LAYOUT_CHUNK_DATA_MAX;
	for (i = 0u; i < 28u; i++) {
		struct ctag_mesh_layout_chunk c = {1u, i, data, CTAG_LAYOUT_CHUNK_DATA_MAX};

		CHECK(ctag_layout_asm_chunk(&a, &c) == CTAG_STATUS_OK);
	}
	CHECK(ctag_layout_asm_commit(&a, 1u, &missing, &sha) == CTAG_STATUS_TOO_LARGE);
	/* More chunks than the missing bitmap holds. */
	begin(&a, 2u, 100u, 40u, data);
	for (i = 0u; i < 40u; i++) {
		struct ctag_mesh_layout_chunk c = {2u, i, data, CTAG_LAYOUT_CHUNK_DATA_MAX};

		CHECK(ctag_layout_asm_chunk(&a, &c) ==
		      (i < 32u ? CTAG_STATUS_OK : CTAG_STATUS_INVALID));
	}
	CHECK(ctag_layout_asm_commit(&a, 2u, &missing, &sha) == CTAG_STATUS_TOO_LARGE);
	/* A chunk other than the last that is not full. */
	begin(&a, 3u, 150u, 2u, data);
	CHECK(chunk(&a, 3u, 0u, data, 100u) == CTAG_STATUS_OK);
	CHECK(chunk(&a, 3u, 1u, data, 50u) == CTAG_STATUS_OK);
	CHECK(ctag_layout_asm_commit(&a, 3u, &missing, &sha) == CTAG_STATUS_INVALID);
	/* Concatenated length differs from total_len. */
	begin(&a, 4u, 99u, 1u, data);
	CHECK(chunk(&a, 4u, 0u, data, 100u) == CTAG_STATUS_OK);
	CHECK(ctag_layout_asm_commit(&a, 4u, &missing, &sha) == CTAG_STATUS_INVALID);
	/* Exactly LAYOUT_HARD_MAX in LAYOUT_MAX_CHUNKS chunks. */
	begin(&a, 5u, CTAG_LAYOUT_HARD_MAX, CTAG_LAYOUT_MAX_CHUNKS, data);
	for (i = 0u; i < CTAG_LAYOUT_MAX_CHUNKS; i++) {
		CHECK(chunk(&a, 5u, i, data, chunk_len(CTAG_LAYOUT_HARD_MAX, i)) == CTAG_STATUS_OK);
	}
	CHECK(ctag_layout_asm_commit(&a, 5u, &missing, &sha) == CTAG_STATUS_OK);
}
