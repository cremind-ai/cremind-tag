/* ctag_render against render.json (glyphs from fontpack_test.ctfp) and qr.json. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_render.h>

#include "check.h"
#include "qrcodegen.h"
#include "suites.h"
#include "support.h"
#include "v_fontpack.h"
#include "v_qr.h"
#include "v_render.h"

#define PLANE_MAX 15000u

static struct ctag_fontpack fp;
static struct ctag_glyph_source src;
static struct t_mem mem;
static struct ctag_render_work work;
static struct ctag_render r;
static uint8_t full[PLANE_MAX];
static uint8_t strips[PLANE_MAX];
static uint8_t qr[CTAG_RENDER_QR_BUF_LEN];
static uint8_t tmp[CTAG_RENDER_QR_BUF_LEN];

static bool init(const struct v_render *v)
{
	struct ctag_render_panel panel = {v->width, v->height, v->planes, v->plane_flags};

	mem.data = V_FONTPACK_DATA;
	mem.len = V_FONTPACK_LEN;
	if (ctag_fontpack_open(&fp, t_mem_read, &mem, V_FONTPACK_LEN) != CTAG_STATUS_OK) {
		return false;
	}
	ctag_fontpack_glyph_source(&fp, &src);
	return ctag_render_init(&r, v->layout, v->len, &panel, &src, &work) == CTAG_STATUS_OK;
}

void test_qr_vectors(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_qr); i++) {
		const struct v_qr *v = &v_qr[i];
		size_t rb = ((size_t)v->size + 7u) / 8u;
		int x, y;

		/* Exactly the call of docs/protocol.md 4.4 made by ctag_render. */
		CHECK_CASE(qrcodegen_encodeText(v->text, tmp, qr, (enum qrcodegen_Ecc)v->ecc, 1, 10,
						qrcodegen_Mask_AUTO, true),
			   v->text);
		CHECK_CASE(qrcodegen_getSize(qr) == v->size && (v->size - 17) / 4 == v->version,
			   v->text);
		for (y = 0; y < v->size; y++) {
			for (x = 0; x < v->size; x++) {
				bool dark = (v->rows[(size_t)y * rb + (size_t)x / 8u] &
					     (0x80u >> (x % 8))) != 0u;

				CHECK_CASE(qrcodegen_getModule(qr, x, y) == dark, v->text);
			}
		}
	}
}

void test_render_scenarios(void)
{
	uint8_t digest[32];
	size_t i;

	for (i = 0u; i < V_COUNT(v_render); i++) {
		const struct v_render *v = &v_render[i];
		struct ctag_sha256_ops sha;
		struct t_sha256 ctx;
		uint8_t plane;

		CHECK_CASE(init(v), v->name);
		CHECK_CASE(ctag_render_plane_len(&r.panel) == v->plane_len, v->name);
		for (plane = 0u; plane < v->planes; plane++) {
			int n = ctag_render_strip(&r, plane, 0u, v->height, full, sizeof(full));

			CHECK_CASE(n == (int)v->plane_len, v->name);
			t_sha256(full, v->plane_len, digest);
			CHECK_CASE(memcmp(digest, v->plane_sha256[plane], 32u) == 0, v->name);
			if (v->plane_bytes[plane] != NULL) {
				CHECK_CASE(memcmp(full, v->plane_bytes[plane], v->plane_len) == 0,
					   v->name);
			}
		}
		/* The bridge pre-pass: strip by strip into a BRIDGE_STRIP_ROWS buffer. */
		t_sha256_ops(&sha, &ctx);
		CHECK_CASE(ctag_render_frame_digest(&r, strips,
						    V_RENDER_STRIP_ROWS *
							    ctag_render_row_bytes(&r.panel),
						    &sha, digest) == 0,
			   v->name);
		CHECK_CASE(memcmp(digest, v->frame_digest, 32u) == 0, v->name);
	}
}

void test_render_strips(void)
{
	static const uint16_t heights[] = {1u, 3u, 7u, V_RENDER_STRIP_ROWS, 37u, 64u};
	size_t i, h;

	for (i = 0u; i < V_COUNT(v_render); i++) {
		const struct v_render *v = &v_render[i];
		size_t rb = (v->width + 7u) / 8u;
		uint8_t plane;

		CHECK_CASE(init(v), v->name);
		for (plane = 0u; plane < v->planes; plane++) {
			CHECK_CASE(ctag_render_strip(&r, plane, 0u, v->height, full,
						     sizeof(full)) == (int)v->plane_len,
				   v->name);
			for (h = 0u; h < V_COUNT(heights); h++) {
				uint32_t y;

				memset(strips, 0xA5, sizeof(strips));
				for (y = 0u; y < v->height; y += heights[h]) {
					uint16_t rows = heights[h];
					size_t want =
						(y + rows > v->height ? v->height - y : rows) * rb;
					int n = ctag_render_strip(&r, plane, (uint16_t)y, rows,
								  &strips[y * rb], want);

					CHECK_CASE(n == (int)want, v->name);
				}
				CHECK_CASE(memcmp(strips, full, v->plane_len) == 0, v->name);
			}
		}
	}
}

void test_render_errors(void)
{
	const struct v_render *v = &v_render[0];
	struct ctag_render_panel panel = {v->width, v->height, v->planes, v->plane_flags};
	size_t rb = (v->width + 7u) / 8u;

	CHECK(init(v));
	CHECK(ctag_render_strip(&r, v->planes, 0u, 1u, full, sizeof(full)) == -EINVAL);
	CHECK(ctag_render_strip(&r, 0u, v->height, 1u, full, sizeof(full)) == -EINVAL);
	CHECK(ctag_render_strip(&r, 0u, 0u, 0u, full, sizeof(full)) == -EINVAL);
	CHECK(ctag_render_strip(&r, 0u, 0u, 2u, full, 2u * rb - 1u) == -EMSGSIZE);
	/* The last strip is clipped to the panel. */
	CHECK(ctag_render_strip(&r, 0u, (uint16_t)(v->height - 1u), 16u, full, rb) == (int)rb);
	CHECK(ctag_render_frame_digest(&r, full, rb - 1u, NULL, NULL) == -EMSGSIZE);
	/* Panel rule, planes, missing strike. */
	panel.width = v->height;
	panel.height = v->width;
	CHECK(ctag_render_init(&r, v->layout, v->len, &panel, &src, &work) == CTAG_STATUS_INVALID);
	panel.width = v->width;
	panel.height = v->height;
	panel.planes = 3u;
	CHECK(ctag_render_init(&r, v->layout, v->len, &panel, &src, &work) == CTAG_STATUS_INVALID);
	panel.planes = 1u;
	fp.strike_count = 2u; /* hides the text strikes */
	fp.hit_state = 0u;
	CHECK(ctag_render_init(&r, v->layout, v->len, &panel, &src, &work) ==
	      CTAG_STATUS_FONTPACK_MISMATCH);
}
