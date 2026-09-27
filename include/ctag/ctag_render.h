/*
 * Strip renderer (docs/protocol.md 4.4), bit-exact with the companion's
 * render/reference.py. Renders native rows [y0, y0 + rows) of one plane into a
 * caller buffer; glyphs come from an abstract source (ctag_fontpack.h provides
 * one over external flash); QR codes use the vendored Nayuki qrcodegen v1.8.0.
 */
#ifndef CTAG_RENDER_H_
#define CTAG_RENDER_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_layout.h>

#ifdef __cplusplus
extern "C" {
#endif

/* qrcodegen_BUFFER_LEN_FOR_VERSION(10): 4.4 encodes versions 1..10. */
#define CTAG_RENDER_QR_BUF_LEN 408u

/* One glyph of a strike (font-pack glyph index entry). */
struct ctag_glyph {
	uint32_t bitmap; /* offset for ctag_glyph_source.read, or CTAG_FONTPACK_EMPTY_BITMAP */
	uint8_t width;
	uint8_t height;
	int8_t bearing_x;
	int8_t bearing_y;
	uint16_t advance;
};

struct ctag_glyph_source {
	bool (*has_strike)(void *ctx, uint16_t face, uint8_t size_px);
	/* 0 with *g filled; -ENOENT for a missing strike or a glyph id out of range. */
	int (*glyph)(void *ctx, uint16_t face, uint8_t size_px, uint16_t glyph_id,
		     struct ctag_glyph *g);
	/* Read len bitmap bytes at offset; 0 on success. */
	int (*read)(void *ctx, uint32_t offset, void *buf, size_t len);
	void *ctx;
};

/* Native panel geometry and plane encoding (tag CAPS). */
struct ctag_render_panel {
	uint16_t width;
	uint16_t height;
	uint8_t planes;      /* 1 or 2 */
	uint8_t plane_flags; /* bit0: plane 0 bit 1 = white; bit1: plane 1 bit 1 = red */
};

/* Caller-provided work memory (the QR symbol is cached across strips). */
struct ctag_render_work {
	uint8_t qr[CTAG_RENDER_QR_BUF_LEN];
	uint8_t tmp[CTAG_RENDER_QR_BUF_LEN];
	char text[CTAG_LAYOUT_QR_MAX_TEXT + 1];
	int32_t qr_cmd; /* layout offset of the QR command held in qr, -1 = none */
};

struct ctag_render {
	const uint8_t *layout;
	size_t len;
	struct ctag_layout_header hdr;
	struct ctag_render_panel panel;
	const struct ctag_glyph_source *glyphs;
	struct ctag_render_work *work;
};

static inline size_t ctag_render_row_bytes(const struct ctag_render_panel *p)
{
	return ((size_t)p->width + 7u) / 8u;
}

static inline size_t ctag_render_plane_len(const struct ctag_render_panel *p)
{
	return ctag_render_row_bytes(p) * p->height;
}

/*
 * Validate and prepare: 4.3 including the strike check, the panel geometry
 * rule and planes in {1, 2}. Returns a ctag_status (OK, INVALID, UNSUPPORTED,
 * TOO_LARGE or FONTPACK_MISMATCH). The layout, source and work memory must
 * stay unchanged while rendering; call again after any change.
 */
uint8_t ctag_render_init(struct ctag_render *r, const uint8_t *layout, size_t len,
			 const struct ctag_render_panel *panel,
			 const struct ctag_glyph_source *glyphs, struct ctag_render_work *work);

/*
 * Render native rows [y0, min(y0 + rows, height)) of plane into out, exactly
 * the bytes of those rows of a full-frame render. Returns the bytes written,
 * -EINVAL (plane, y0 or rows out of range), -EMSGSIZE, or an error from the
 * glyph source.
 */
int ctag_render_strip(struct ctag_render *r, uint8_t plane, uint16_t y0, uint16_t rows,
		      uint8_t *out, size_t size);

/*
 * Frame digest (4.4): SHA-256 of plane 0 then plane 1, rendered strip by strip
 * into buf (as many whole rows as fit). Returns 0 or a negative error.
 */
int ctag_render_frame_digest(struct ctag_render *r, uint8_t *buf, size_t size,
			     const struct ctag_sha256_ops *sha, uint8_t digest[32]);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_RENDER_H_ */
