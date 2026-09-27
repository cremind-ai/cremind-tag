/* Font-pack reader against fontpack.json / fontpack_test.ctfp. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_fontpack.h>

#include "check.h"
#include "suites.h"
#include "support.h"
#include "v_fontpack.h"

#define STRIKE(i) (V_FONTPACK_STRIKE_OFF + (i) * CTAG_FONTPACK_STRIKE_RECORD_SIZE)
#define FACE(i)   (128u + (i) * CTAG_FONTPACK_FACE_RECORD_SIZE)

static struct ctag_fontpack fp;
static struct t_mem mem;
static uint8_t pack[V_FONTPACK_LEN];
static uint8_t scratch[100];

static uint8_t open_copy(size_t avail)
{
	mem.data = pack;
	mem.len = sizeof(pack);
	mem.reads = 0u;
	return ctag_fontpack_open(&fp, t_mem_read, &mem, (uint32_t)avail);
}

static uint8_t verify(void)
{
	struct ctag_sha256_ops sha;
	struct t_sha256 ctx;

	t_sha256_ops(&sha, &ctx);
	return ctag_fontpack_verify_content(&fp, &sha, scratch, sizeof(scratch));
}

static void fix_header_crc(void)
{
	ctag_put_le32(&pack[124], ctag_crc32(0u, pack, 124u));
}

static void fix_index_crc(uint32_t strike)
{
	const uint8_t *rec = &pack[STRIKE(strike)];
	uint32_t n = ctag_get_le32(&rec[4]) * CTAG_FONTPACK_GLYPH_ENTRY_SIZE;

	ctag_put_le32(&pack[STRIKE(strike) + 20u],
		      ctag_crc32(0u, &pack[ctag_get_le32(&rec[8])], n));
}

static void reset(void)
{
	memcpy(pack, V_FONTPACK_DATA, sizeof(pack));
}

void test_fontpack_parse(void)
{
	size_t i;

	reset();
	CHECK(open_copy(sizeof(pack)) == CTAG_STATUS_OK);
	CHECK(fp.total == V_FONTPACK_TOTAL && fp.strike_count == V_FONTPACK_STRIKE_COUNT);
	CHECK(fp.strike_off == V_FONTPACK_STRIKE_OFF && fp.bitmap_off == V_FONTPACK_BITMAP_OFF);
	CHECK(fp.bitmap_size == V_FONTPACK_BITMAP_SIZE);
	CHECK(memcmp(fp.pack_id, V_FONTPACK_PACK_ID, sizeof(fp.pack_id)) == 0);
	CHECK(memcmp(fp.content_hash, V_FONTPACK_CONTENT_HASH, sizeof(fp.content_hash)) == 0);
	CHECK(verify() == CTAG_STATUS_OK);
	/* A slot larger than the pack is fine. */
	CHECK(open_copy(sizeof(pack) + 4096u) == CTAG_STATUS_OK);
	for (i = 0u; i < V_COUNT(v_fontpack_strikes); i++) {
		struct ctag_glyph_source src;

		ctag_fontpack_glyph_source(&fp, &src);
		CHECK(src.has_strike(src.ctx, v_fontpack_strikes[i].face,
				     v_fontpack_strikes[i].size_px));
		CHECK(fp.hit_glyphs == v_fontpack_strikes[i].glyph_count);
		CHECK(fp.hit_index_off == v_fontpack_strikes[i].index_off);
	}
}

void test_fontpack_glyphs(void)
{
	struct ctag_glyph_source src;
	struct ctag_glyph g;
	uint8_t bm[128];
	unsigned int reads;
	size_t i;

	reset();
	CHECK(open_copy(sizeof(pack)) == CTAG_STATUS_OK);
	ctag_fontpack_glyph_source(&fp, &src);
	for (i = 0u; i < V_COUNT(v_fontpack_glyphs); i++) {
		const struct v_glyph *v = &v_fontpack_glyphs[i];

		CHECK(src.glyph(src.ctx, v->face, v->size_px, v->glyph_id, &g) == 0);
		CHECK(g.width == v->width && g.height == v->height && g.advance == v->advance);
		CHECK(g.bearing_x == v->bearing_x && g.bearing_y == v->bearing_y);
		if (v->bitmap_off == CTAG_FONTPACK_EMPTY_BITMAP) {
			CHECK(g.bitmap == CTAG_FONTPACK_EMPTY_BITMAP && v->bitmap_len == 0u);
			continue;
		}
		CHECK(g.bitmap == V_FONTPACK_BITMAP_OFF + v->bitmap_off);
		CHECK(v->bitmap_len <= sizeof(bm));
		CHECK(src.read(src.ctx, g.bitmap, bm, v->bitmap_len) == 0);
		CHECK(memcmp(bm, v->bitmap, v->bitmap_len) == 0);
	}
	/* Out-of-range ids and missing strikes. */
	CHECK(src.glyph(src.ctx, 0u, 16u, 7u, &g) == -ENOENT);
	CHECK(src.glyph(src.ctx, 1u, 16u, 20u, &g) == -ENOENT);
	CHECK(src.glyph(src.ctx, 0u, 32u, 1u, &g) == -ENOENT);
	CHECK(src.glyph(src.ctx, 7u, 16u, 1u, &g) == -ENOENT);
	CHECK(!src.has_strike(src.ctx, 0u, 48u) && !src.has_strike(src.ctx, 2u, 16u));
	CHECK(!src.has_strike(src.ctx, 0u, 0u) && !src.has_strike(src.ctx, 0xFFFFu, 0xFFu));
	/* Consecutive glyphs of one strike reuse the strike lookup: one read each. */
	CHECK(src.glyph(src.ctx, 1u, 24u, 3u, &g) == 0);
	reads = mem.reads;
	CHECK(src.glyph(src.ctx, 1u, 24u, 4u, &g) == 0 && mem.reads == reads + 1u);
}

void test_fontpack_corruptions(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_fontpack_corruptions); i++) {
		const struct v_corruption *v = &v_fontpack_corruptions[i];
		uint8_t st;

		reset();
		pack[v->offset] ^= 0x01u;
		st = open_copy(sizeof(pack));
		CHECK_CASE(st == v->boot_status, v->name);
		if (st == CTAG_STATUS_OK) {
			st = verify();
		}
		CHECK_CASE(st == v->install_status, v->name);
	}
}

static int fail_read(void *ctx, uint32_t offset, void *buf, size_t len)
{
	(void)ctx;
	(void)offset;
	(void)buf;
	(void)len;
	return -EIO;
}

void test_fontpack_invalid(void)
{
	uint32_t total = V_FONTPACK_TOTAL;

	reset();
	CHECK(open_copy(total - 1u) == CTAG_STATUS_INVALID);
	CHECK(open_copy(127u) == CTAG_STATUS_INVALID);
	CHECK(ctag_fontpack_open(&fp, fail_read, NULL, total) == CTAG_STATUS_STORAGE_ERROR);
	pack[0] ^= 0x01u;
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	pack[4] = 2u; /* version is checked before the header CRC */
	CHECK(open_copy(total) == CTAG_STATUS_UNSUPPORTED);
	reset();
	pack[6] = 64u;
	CHECK(open_copy(total) == CTAG_STATUS_UNSUPPORTED);
	reset();
	ctag_put_le32(&pack[12], 100u); /* total below the header */
	fix_header_crc();
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	ctag_put_le32(&pack[80], ctag_get_le32(&pack[80]) + 1u); /* bitmap area past total */
	fix_header_crc();
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	ctag_put_le32(&pack[72], 5u); /* string table ends inside the first name */
	fix_header_crc();
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	pack[ctag_get_le32(&pack[68])] = 0xFFu; /* face name not UTF-8 */
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	ctag_put_le16(&pack[FACE(0u)], 5u); /* faces out of order */
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	ctag_put_le16(&pack[STRIKE(3u)], 2u); /* strike of a face that does not exist */
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	pack[STRIKE(1u) + 2u] = 8u; /* strikes out of order */
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	ctag_put_le32(&pack[STRIKE(0u) + 8u], total); /* index outside the pack */
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	reset();
	/* Glyph 1 of strike (0, 16): bitmap past the bitmap area, index CRC fixed. */
	ctag_put_le32(&pack[ctag_get_le32(&pack[STRIKE(0u) + 8u]) + 12u], V_FONTPACK_BITMAP_SIZE);
	fix_index_crc(0u);
	CHECK(open_copy(total) == CTAG_STATUS_INVALID);
	/* The same entry without fixing the CRC: the CRC is checked first. */
	reset();
	ctag_put_le32(&pack[ctag_get_le32(&pack[STRIKE(0u) + 8u]) + 12u], V_FONTPACK_BITMAP_SIZE);
	CHECK(open_copy(total) == CTAG_STATUS_CRC_ERROR);
}
