/*
 * Deterministic mutation tests: malformed input is rejected without reading
 * or writing out of bounds (meaningful under -fsanitize=address,undefined),
 * and whatever validates also renders.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_frag.h>
#include <ctag/ctag_frame.h>
#include <ctag/ctag_render.h>

#include "check.h"
#include "suites.h"
#include "support.h"
#include "v_fontpack.h"
#include "v_render.h"

#define STRIP_MAX 4096u

static uint32_t rng = 0x12345678u;
static uint8_t buf[CTAG_LAYOUT_HARD_MAX];
static uint8_t pack[V_FONTPACK_LEN];
static uint8_t strip[STRIP_MAX];
static struct ctag_fontpack fp;
static struct ctag_glyph_source src;
static struct ctag_render_work work;
static struct ctag_render r;
static struct t_mem mem;

static uint32_t rnd(void)
{
	rng ^= rng << 13;
	rng ^= rng >> 17;
	rng ^= rng << 5;
	return rng;
}

/* Flip, overwrite or truncate a few bytes. */
static size_t mutate(uint8_t *data, size_t len)
{
	uint32_t n = 1u + rnd() % 4u;

	while (n-- > 0u && len > 0u) {
		size_t at = rnd() % len;

		switch (rnd() % 4u) {
		case 0:
			data[at] ^= (uint8_t)(1u << (rnd() % 8u));
			break;
		case 1:
			data[at] = (uint8_t)rnd();
			break;
		case 2:
			data[at] = (uint8_t)(rnd() % 2u ? 0xFFu : 0x00u);
			break;
		default:
			len = at;
			break;
		}
	}
	return len;
}

void test_fuzz_layouts(void)
{
	unsigned int rendered = 0u;
	uint32_t i;

	mem.data = V_FONTPACK_DATA;
	mem.len = V_FONTPACK_LEN;
	CHECK(ctag_fontpack_open(&fp, t_mem_read, &mem, V_FONTPACK_LEN) == CTAG_STATUS_OK);
	ctag_fontpack_glyph_source(&fp, &src);
	for (i = 0u; i < 4000u; i++) {
		const struct v_render *v = &v_render[i % V_COUNT(v_render)];
		struct ctag_render_panel panel = {0u, 0u, v->planes, v->plane_flags};
		struct ctag_layout_header h;
		struct ctag_layout_iter it;
		size_t len;

		memcpy(buf, v->layout, v->len);
		len = mutate(buf, v->len);
		if (ctag_layout_validate(buf, len, src.has_strike, src.ctx) != CTAG_STATUS_OK) {
			continue;
		}
		/* A valid layout renders on the panel its size and rotation imply. */
		(void)ctag_layout_iter_init(&it, buf, len, &h);
		panel.width = (h.rotation & 1u) != 0u ? h.height : h.width;
		panel.height = (h.rotation & 1u) != 0u ? h.width : h.height;
		CHECK(ctag_render_init(&r, buf, len, &panel, &src, &work) == CTAG_STATUS_OK);
		CHECK(ctag_render_strip(&r, 0u, (uint16_t)(rnd() % panel.height), 4u, strip,
					sizeof(strip)) > 0);
		rendered++;
	}
	CHECK(rendered > 100u);
}

void test_fuzz_fontpack(void)
{
	unsigned int opened = 0u;
	uint32_t i, k;

	mem.data = pack;
	mem.len = sizeof(pack);
	for (i = 0u; i < 3000u; i++) {
		memcpy(pack, V_FONTPACK_DATA, sizeof(pack));
		(void)mutate(pack, sizeof(pack));
		if (i % 2u == 0u) {
			/* Half the time, get past the header CRC. */
			ctag_put_le32(&pack[124], ctag_crc32(0u, pack, 124u));
		}
		if (ctag_fontpack_open(&fp, t_mem_read, &mem, sizeof(pack)) != CTAG_STATUS_OK) {
			continue;
		}
		/* An accepted pack never makes the reader leave it. */
		ctag_fontpack_glyph_source(&fp, &src);
		for (k = 0u; k < 64u; k++) {
			uint32_t rb, last;
			struct ctag_glyph g;
			uint8_t row[32];
			int err = src.glyph(src.ctx, (uint16_t)(rnd() % 3u),
					    (uint8_t)(8u * (rnd() % 5u)), (uint16_t)(rnd() % 24u),
					    &g);

			CHECK(err == 0 || err == -ENOENT);
			if (err != 0 || g.bitmap == CTAG_FONTPACK_EMPTY_BITMAP || g.width == 0u ||
			    g.height == 0u) {
				continue;
			}
			rb = ((uint32_t)g.width + 7u) / 8u;
			last = g.bitmap + (uint32_t)(g.height - 1u) * rb;
			CHECK(src.read(src.ctx, last, row, rb) == 0);
		}
		opened++;
	}
	CHECK(opened > 100u);
}

void test_fuzz_streams(void)
{
	static struct ctag_serial_rx rx;
	struct ctag_serial_header h;
	struct ctag_frag_rx frx;
	uint8_t value[24];
	uint32_t i, k;

	ctag_serial_rx_init(&rx, buf, 64u);
	for (i = 0u; i < 200000u; i++) {
		(void)ctag_serial_rx_put(&rx, (uint8_t)(rnd() % 3u == 0u ? 0u : rnd()), &h);
		CHECK(rx.cobs.len <= 64u);
	}
	for (i = 0u; i < 20000u; i++) {
		int n = 0;

		ctag_frag_rx_init(&frx, buf, CTAG_TAG_CTRL_MSG_MAX);
		frx.seq = (uint8_t)(rnd() & CTAG_FRAG_SEQ_MASK);
		for (k = 0u; k < 8u && n >= 0; k++) {
			size_t len = rnd() % sizeof(value);
			size_t j;

			for (j = 0u; j < len; j++) {
				value[j] = (uint8_t)rnd();
			}
			n = ctag_frag_rx_put(&frx, value, len);
			CHECK(n <= (int)CTAG_TAG_CTRL_MSG_MAX && frx.len <= CTAG_TAG_CTRL_MSG_MAX);
		}
	}
	for (i = 0u; i < 20000u; i++) {
		size_t len = 1u + rnd() % 300u;
		size_t j;
		int n;

		for (j = 0u; j < len; j++) {
			buf[j] = (uint8_t)(rnd() % 4u == 0u ? 0u : rnd());
		}
		n = ctag_cobs_decode(buf, len, strip, 280u);
		CHECK(n == -EINVAL || n == -EMSGSIZE || (n >= 0 && n <= 280));
	}
}
