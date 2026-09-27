/*
 * Golden: the bridge's strip rendering of every protocol/fixtures/render.json
 * layout, with protocol/fixtures/fontpack_test.ctfp installed in the bridge's
 * external-flash store and read back through its cache, yields the fixture's
 * frame digest (render pre-pass) and plane digests (strip by strip).
 */
#include <string.h>

#include <ctag/ctag_render.h>

#include "common.h"
#include "v_render.h"

static uint8_t strip[CTAG_BRIDGE_STRIP_ROWS * 256];

ZTEST(bridge_golden, test_render_fixtures)
{
	struct fontstore_view view;
	struct ctag_render_work work;
	struct ctag_render r;

	env_fresh(0u);
	install_fixture_pack();
	/* Pack in the second slot: offsets above 8 MiB on this part. */
	install_fixture_pack();
	zassert_equal(benv.fonts.active.slot, 1);
	zassert_true(fontstore_view_open(&benv.fonts, &view));
	for (size_t i = 0; i < ARRAY_SIZE(v_render); i++) {
		const struct v_render *v = &v_render[i];
		struct ctag_render_panel panel = {v->width, v->height, v->planes, v->plane_flags};
		size_t rb = ctag_render_row_bytes(&panel);
		size_t rows = MIN((size_t)CTAG_BRIDGE_STRIP_ROWS, sizeof(strip) / rb);
		uint8_t digest[32];

		zassert_equal(ctag_render_init(&r, v->layout, v->len, &panel, &view.src, &work),
			      CTAG_STATUS_OK, "%s", v->name);
		/* FRAME_BEGIN.digest: the pre-pass over BRIDGE_STRIP_ROWS strips. */
		zassert_ok(ctag_render_frame_digest(&r, strip, rows * rb, &test_sha, digest));
		zassert_mem_equal(digest, v->frame_digest, 32, "%s: frame digest", v->name);
		/* The strips streamed as PLANE_DATA give each plane's digest. */
		for (uint8_t p = 0; p < v->planes; p++) {
			ctag_sha256_ctx c;

			zassert_ok(ctag_crypto_sha256_init(&c));
			for (uint32_t y = 0; y < v->height; y += (uint32_t)rows) {
				int n = ctag_render_strip(&r, p, (uint16_t)y, (uint16_t)rows, strip,
							  sizeof(strip));

				zassert_true(n > 0);
				zassert_ok(ctag_crypto_sha256_update(&c, strip, (size_t)n));
			}
			zassert_ok(ctag_crypto_sha256_finish(&c, digest));
			zassert_mem_equal(digest, v->plane_sha256[p], 32, "%s: plane %u", v->name, p);
		}
	}
	fontstore_view_close(&benv.fonts, &view);
	zassert_true(benv.flash.hits > benv.flash.misses, "the cache serves the renderer");
}

ZTEST_SUITE(bridge_golden, NULL, NULL, NULL, NULL, NULL);
