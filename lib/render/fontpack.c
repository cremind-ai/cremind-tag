/* Font-pack reader (docs/fontpack.md 2); mirrors fontpack/format.py FontPack. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_utf8.h>

#define HEADER_CRC_OFF 124u
#define INDEX_CHUNK    (20u * CTAG_FONTPACK_GLYPH_ENTRY_SIZE)
#define STRING_CHUNK   32u

static bool inside(const struct ctag_fontpack *fp, uint32_t off, uint64_t size)
{
	return off >= CTAG_FONTPACK_HEADER_SIZE && off <= fp->total && size <= fp->total - off;
}

/* A NUL-terminated UTF-8 string at off within the string table. */
static uint8_t check_string(const struct ctag_fontpack *fp, uint32_t table, uint32_t table_size,
			    uint32_t off)
{
	struct ctag_utf8 u;
	uint8_t buf[STRING_CHUNK];

	ctag_utf8_init(&u);
	while (off < table_size) {
		size_t n = table_size - off < STRING_CHUNK ? table_size - off : STRING_CHUNK;
		const uint8_t *nul;

		if (fp->read(fp->ctx, table + off, buf, n) != 0) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		nul = memchr(buf, 0, n);
		if (nul != NULL) {
			ctag_utf8_feed(&u, buf, (size_t)(nul - buf));
			return ctag_utf8_valid(&u) ? CTAG_STATUS_OK : CTAG_STATUS_INVALID;
		}
		ctag_utf8_feed(&u, buf, n);
		off += (uint32_t)n;
	}
	return CTAG_STATUS_INVALID;
}

static uint8_t check_faces(const struct ctag_fontpack *fp, uint32_t face_off, uint16_t face_count,
			   uint32_t string_off, uint32_t string_size)
{
	uint16_t prev = 0u;
	uint16_t i;

	for (i = 0u; i < face_count; i++) {
		uint8_t rec[CTAG_FONTPACK_FACE_RECORD_SIZE];
		uint16_t id;
		uint8_t st;

		if (fp->read(fp->ctx, face_off + (uint32_t)i * CTAG_FONTPACK_FACE_RECORD_SIZE, rec,
			     sizeof(rec)) != 0) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		id = ctag_get_le16(rec);
		if (i > 0u && id <= prev) {
			return CTAG_STATUS_INVALID;
		}
		prev = id;
		st = check_string(fp, string_off, string_size, ctag_get_le32(&rec[4]));
		if (st == CTAG_STATUS_OK) {
			st = check_string(fp, string_off, string_size, ctag_get_le32(&rec[8]));
		}
		if (st != CTAG_STATUS_OK) {
			return st;
		}
	}
	return CTAG_STATUS_OK;
}

/* Index CRC first, then every non-empty bitmap inside the bitmap area. */
static uint8_t check_index(const struct ctag_fontpack *fp, uint32_t off, uint32_t glyphs,
			   uint32_t expect)
{
	uint64_t left = (uint64_t)glyphs * CTAG_FONTPACK_GLYPH_ENTRY_SIZE;
	uint32_t crc = 0u;
	bool outside = false;
	uint8_t buf[INDEX_CHUNK];

	while (left > 0u) {
		size_t n = left < INDEX_CHUNK ? (size_t)left : INDEX_CHUNK;
		size_t e;

		if (fp->read(fp->ctx, off, buf, n) != 0) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		crc = ctag_crc32(crc, buf, n);
		for (e = 0u; e < n; e += CTAG_FONTPACK_GLYPH_ENTRY_SIZE) {
			uint32_t bm = ctag_get_le32(&buf[e]);
			uint32_t size = (uint32_t)buf[e + 5u] * ((buf[e + 4u] + 7u) / 8u);

			if (bm != CTAG_FONTPACK_EMPTY_BITMAP &&
			    (uint64_t)bm + size > fp->bitmap_size) {
				outside = true;
			}
		}
		off += (uint32_t)n;
		left -= n;
	}
	if (crc != expect) {
		return CTAG_STATUS_CRC_ERROR;
	}
	return outside ? CTAG_STATUS_INVALID : CTAG_STATUS_OK;
}

static uint8_t check_strikes(const struct ctag_fontpack *fp, uint32_t face_off, uint16_t face_count)
{
	uint32_t prev = 0u;
	uint16_t face = 0u;
	uint16_t j = 0u;
	uint16_t i;

	for (i = 0u; i < fp->strike_count; i++) {
		uint8_t rec[CTAG_FONTPACK_STRIKE_RECORD_SIZE];
		uint32_t key;
		uint8_t st;

		if (fp->read(fp->ctx,
			     fp->strike_off + (uint32_t)i * CTAG_FONTPACK_STRIKE_RECORD_SIZE, rec,
			     sizeof(rec)) != 0) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		key = (uint32_t)ctag_get_le16(rec) << 8 | rec[2];
		if (i > 0u && key <= prev) {
			return CTAG_STATUS_INVALID;
		}
		prev = key;
		/* Strikes ascend by face and faces are sorted: walk the face table once. */
		for (; j < face_count; j++) {
			uint8_t id[2];

			if (fp->read(fp->ctx,
				     face_off + (uint32_t)j * CTAG_FONTPACK_FACE_RECORD_SIZE, id,
				     sizeof(id)) != 0) {
				return CTAG_STATUS_STORAGE_ERROR;
			}
			face = ctag_get_le16(id);
			if (face >= (key >> 8)) {
				break;
			}
		}
		if (j == face_count || face != (key >> 8)) {
			return CTAG_STATUS_INVALID;
		}
		if (!inside(fp, ctag_get_le32(&rec[8]),
			    (uint64_t)ctag_get_le32(&rec[4]) * CTAG_FONTPACK_GLYPH_ENTRY_SIZE)) {
			return CTAG_STATUS_INVALID;
		}
		st = check_index(fp, ctag_get_le32(&rec[8]), ctag_get_le32(&rec[4]),
				 ctag_get_le32(&rec[20]));
		if (st != CTAG_STATUS_OK) {
			return st;
		}
	}
	return CTAG_STATUS_OK;
}

uint8_t ctag_fontpack_open(struct ctag_fontpack *fp, ctag_fontpack_read_fn read, void *ctx,
			   uint32_t avail)
{
	uint8_t h[CTAG_FONTPACK_HEADER_SIZE];
	uint32_t face_off, string_off, string_size;
	uint16_t face_count;
	uint8_t st;

	memset(fp, 0, sizeof(*fp));
	fp->read = read;
	fp->ctx = ctx;
	if (avail < CTAG_FONTPACK_HEADER_SIZE) {
		return CTAG_STATUS_INVALID;
	}
	if (read(ctx, 0u, h, sizeof(h)) != 0) {
		return CTAG_STATUS_STORAGE_ERROR;
	}
	if (ctag_get_le32(h) != CTAG_FONTPACK_MAGIC) {
		return CTAG_STATUS_INVALID;
	}
	if (ctag_get_le16(&h[4]) != CTAG_FONTPACK_VERSION ||
	    ctag_get_le16(&h[6]) != CTAG_FONTPACK_HEADER_SIZE) {
		return CTAG_STATUS_UNSUPPORTED;
	}
	if (ctag_crc32(0u, h, HEADER_CRC_OFF) != ctag_get_le32(&h[HEADER_CRC_OFF])) {
		return CTAG_STATUS_CRC_ERROR;
	}
	fp->total = ctag_get_le32(&h[12]);
	if (fp->total < CTAG_FONTPACK_HEADER_SIZE || fp->total > avail) {
		return CTAG_STATUS_INVALID;
	}
	memcpy(fp->pack_id, &h[16], sizeof(fp->pack_id));
	memcpy(fp->content_hash, &h[24], sizeof(fp->content_hash));
	face_count = ctag_get_le16(&h[56]);
	fp->strike_count = ctag_get_le16(&h[58]);
	face_off = ctag_get_le32(&h[60]);
	fp->strike_off = ctag_get_le32(&h[64]);
	string_off = ctag_get_le32(&h[68]);
	string_size = ctag_get_le32(&h[72]);
	fp->bitmap_off = ctag_get_le32(&h[76]);
	fp->bitmap_size = ctag_get_le32(&h[80]);
	if (!inside(fp, face_off, (uint64_t)face_count * CTAG_FONTPACK_FACE_RECORD_SIZE) ||
	    !inside(fp, fp->strike_off,
		    (uint64_t)fp->strike_count * CTAG_FONTPACK_STRIKE_RECORD_SIZE) ||
	    !inside(fp, string_off, string_size) || !inside(fp, fp->bitmap_off, fp->bitmap_size)) {
		return CTAG_STATUS_INVALID;
	}
	st = check_faces(fp, face_off, face_count, string_off, string_size);
	return st != CTAG_STATUS_OK ? st : check_strikes(fp, face_off, face_count);
}

uint8_t ctag_fontpack_verify_content(struct ctag_fontpack *fp, const struct ctag_sha256_ops *sha,
				     uint8_t *buf, size_t size)
{
	uint32_t off = CTAG_FONTPACK_HEADER_SIZE;
	uint8_t digest[32];

	if (size == 0u || sha->init(sha->ctx) != 0) {
		return CTAG_STATUS_INTERNAL;
	}
	while (off < fp->total) {
		size_t n = fp->total - off < size ? fp->total - off : size;

		if (fp->read(fp->ctx, off, buf, n) != 0) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		if (sha->update(sha->ctx, buf, n) != 0) {
			return CTAG_STATUS_INTERNAL;
		}
		off += (uint32_t)n;
	}
	if (sha->finish(sha->ctx, digest) != 0) {
		return CTAG_STATUS_INTERNAL;
	}
	if (memcmp(digest, fp->content_hash, sizeof(digest)) != 0 ||
	    memcmp(digest, fp->pack_id, sizeof(fp->pack_id)) != 0) {
		return CTAG_STATUS_DIGEST_MISMATCH;
	}
	return CTAG_STATUS_OK;
}

/* Binary search of the strike table, remembering the last answer. */
static int find_strike(struct ctag_fontpack *fp, uint16_t face, uint8_t size_px)
{
	uint32_t key = (uint32_t)face << 8 | size_px;
	uint32_t lo = 0u;
	uint32_t hi = fp->strike_count;

	if (fp->hit_state != 0u && fp->hit_face == face && fp->hit_size == size_px) {
		return fp->hit_state == 1u ? 0 : -ENOENT;
	}
	fp->hit_state = 0u;
	while (lo < hi) {
		uint32_t mid = lo + (hi - lo) / 2u;
		uint8_t rec[12];
		uint32_t k;

		if (fp->read(fp->ctx, fp->strike_off + mid * CTAG_FONTPACK_STRIKE_RECORD_SIZE, rec,
			     sizeof(rec)) != 0) {
			return -EIO;
		}
		k = (uint32_t)ctag_get_le16(rec) << 8 | rec[2];
		if (k == key) {
			fp->hit_glyphs = ctag_get_le32(&rec[4]);
			fp->hit_index_off = ctag_get_le32(&rec[8]);
			break;
		}
		if (k < key) {
			lo = mid + 1u;
		} else {
			hi = mid;
		}
	}
	fp->hit_face = face;
	fp->hit_size = size_px;
	fp->hit_state = lo < hi ? 1u : 2u;
	return lo < hi ? 0 : -ENOENT;
}

static bool fp_has_strike(void *ctx, uint16_t face, uint8_t size_px)
{
	return find_strike(ctx, face, size_px) == 0;
}

static int fp_glyph(void *ctx, uint16_t face, uint8_t size_px, uint16_t glyph_id,
		    struct ctag_glyph *g)
{
	struct ctag_fontpack *fp = ctx;
	uint8_t e[10];
	uint32_t bm;
	int err = find_strike(fp, face, size_px);

	if (err != 0) {
		return err;
	}
	if (glyph_id >= fp->hit_glyphs) {
		return -ENOENT;
	}
	if (fp->read(fp->ctx,
		     fp->hit_index_off + (uint32_t)glyph_id * CTAG_FONTPACK_GLYPH_ENTRY_SIZE, e,
		     sizeof(e)) != 0) {
		return -EIO;
	}
	bm = ctag_get_le32(e);
	g->bitmap = bm == CTAG_FONTPACK_EMPTY_BITMAP ? bm : fp->bitmap_off + bm;
	g->width = e[4];
	g->height = e[5];
	g->bearing_x = ctag_get_i8(&e[6]);
	g->bearing_y = ctag_get_i8(&e[7]);
	g->advance = ctag_get_le16(&e[8]);
	return 0;
}

static int fp_read(void *ctx, uint32_t offset, void *buf, size_t len)
{
	struct ctag_fontpack *fp = ctx;

	return fp->read(fp->ctx, offset, buf, len);
}

void ctag_fontpack_glyph_source(struct ctag_fontpack *fp, struct ctag_glyph_source *src)
{
	src->has_strike = fp_has_strike;
	src->glyph = fp_glyph;
	src->read = fp_read;
	src->ctx = fp;
}
