/*
 * Font-pack reader (docs/fontpack.md 2) over a read(offset, len) callback, and
 * a ctag_glyph_source backed by it. Part of the CTAG_RENDER library.
 */
#ifndef CTAG_FONTPACK_H_
#define CTAG_FONTPACK_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_render.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Read len bytes at offset of the pack; 0 on success. */
typedef int (*ctag_fontpack_read_fn)(void *ctx, uint32_t offset, void *buf, size_t len);

struct ctag_fontpack {
	ctag_fontpack_read_fn read;
	void *ctx;
	uint32_t total;
	uint32_t strike_off;
	uint32_t bitmap_off;
	uint32_t bitmap_size;
	uint16_t strike_count;
	uint8_t pack_id[CTAG_FONTPACK_ID_LEN];
	uint8_t content_hash[32];
	/* Last strike looked up: a GLYPHS run uses a single strike. */
	uint32_t hit_index_off;
	uint32_t hit_glyphs;
	uint16_t hit_face;
	uint8_t hit_size;
	uint8_t hit_state; /* 0 = nothing cached, 1 = present, 2 = absent */
};

/*
 * Validate a pack of at most avail bytes (the boot-time checks of
 * docs/fontpack.md 2 "Validation", in the order of fontpack/format.py):
 * returns OK, INVALID, UNSUPPORTED, CRC_ERROR, or STORAGE_ERROR when read fails.
 */
uint8_t ctag_fontpack_open(struct ctag_fontpack *fp, ctag_fontpack_read_fn read, void *ctx,
			   uint32_t avail);

/*
 * Install-time check: SHA-256 of bytes [128, total) equals content_hash and
 * pack_id is its first 8 bytes. buf is scratch for reading. Returns OK,
 * DIGEST_MISMATCH, STORAGE_ERROR or INTERNAL (hash failure).
 */
uint8_t ctag_fontpack_verify_content(struct ctag_fontpack *fp, const struct ctag_sha256_ops *sha,
				     uint8_t *buf, size_t size);

/* Fill src with a glyph source reading from fp (which must stay valid). */
void ctag_fontpack_glyph_source(struct ctag_fontpack *fp, struct ctag_glyph_source *src);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_FONTPACK_H_ */
