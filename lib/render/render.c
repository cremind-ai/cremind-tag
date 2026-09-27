/*
 * Strip renderer (docs/protocol.md 4.4); mirrors render/reference.py.
 *
 * Every command paints logical rectangles clipped to the canvas and to the
 * logical region that maps onto the native rows of the strip; each rectangle is
 * mapped to native coordinates and written straight into the plane bits, so a
 * strip equals the same rows of a full frame. Clipping never alters a
 * command's geometry (the Bresenham path is walked in full).
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_render.h>

#include "qrcodegen.h"

/* Bearings are i8 and bitmaps at most 255 x 255: a glyph lies within this box. */
#define GLYPH_REACH_LO 128
#define GLYPH_REACH_HI (128 + 255)
#define QR_MAX_MODULES 57 /* version 10 */
#define QR_NONE        0xFFFFu /* no command: layout offsets are below LAYOUT_HARD_MAX */

struct strip {
	const struct ctag_render *r;
	uint8_t *out;
	size_t row_bytes;
	int32_t y0;  /* first native row */
	int32_t cx0; /* logical clip rectangle, half-open */
	int32_t cy0;
	int32_t cx1;
	int32_t cy1;
	uint8_t bits; /* bit c = plane bit of colour c */
};

static int32_t max32(int32_t a, int32_t b)
{
	return a > b ? a : b;
}

static int32_t min32(int32_t a, int32_t b)
{
	return a < b ? a : b;
}

static void set_span(uint8_t *row, uint32_t x0, uint32_t x1, bool on)
{
	while (x0 < x1) {
		uint32_t bit = x0 & 7u;
		uint32_t n = 8u - bit;
		uint8_t mask;

		if (n > x1 - x0) {
			n = x1 - x0;
		}
		mask = (uint8_t)((0xFFu >> bit) & ~(0xFFu >> (bit + n)));
		if (on) {
			row[x0 >> 3] |= mask;
		} else {
			row[x0 >> 3] &= (uint8_t)~mask;
		}
		x0 += n;
	}
}

/* The plane bit of a colour. */
static bool color_bit(const struct strip *s, uint8_t color)
{
	return (((unsigned int)s->bits >> color) & 1u) != 0u;
}

static bool box_hits(const struct strip *s, int32_t x0, int32_t y0, int32_t x1, int32_t y1)
{
	return x0 < s->cx1 && x1 > s->cx0 && y0 < s->cy1 && y1 > s->cy0;
}

/* Paint logical [x0, x1) x [y0, y1), clipped, into the native strip. */
static void fill(const struct strip *s, int32_t x0, int32_t y0, int32_t x1, int32_t y1,
		 uint8_t color)
{
	int32_t wn = s->r->panel.width;
	int32_t hn = s->r->panel.height;
	int32_t nx0, nx1, ny0, ny1;
	bool on = color_bit(s, color);

	x0 = max32(x0, s->cx0);
	y0 = max32(y0, s->cy0);
	x1 = min32(x1, s->cx1);
	y1 = min32(y1, s->cy1);
	if (x0 >= x1 || y0 >= y1) {
		return;
	}
	switch (s->r->hdr.rotation) {
	case 0: /* nx = lx, ny = ly */
		nx0 = x0;
		nx1 = x1;
		ny0 = y0;
		ny1 = y1;
		break;
	case 1: /* nx = Wn-1-ly, ny = lx */
		nx0 = wn - y1;
		nx1 = wn - y0;
		ny0 = x0;
		ny1 = x1;
		break;
	case 2: /* nx = Wn-1-lx, ny = Hn-1-ly */
		nx0 = wn - x1;
		nx1 = wn - x0;
		ny0 = hn - y1;
		ny1 = hn - y0;
		break;
	default: /* nx = ly, ny = Hn-1-lx */
		nx0 = y0;
		nx1 = y1;
		ny0 = hn - x1;
		ny1 = hn - x0;
		break;
	}
	for (; ny0 < ny1; ny0++) {
		set_span(&s->out[(size_t)(ny0 - s->y0) * s->row_bytes], (uint32_t)nx0,
			 (uint32_t)nx1, on);
	}
}

/* Paint the 1 bits of a glyph bitmap with its top-left at (left, top). */
static int blit(const struct strip *s, const struct ctag_glyph *g, int32_t left, int32_t top,
		uint8_t color)
{
	const struct ctag_glyph_source *src = s->r->glyphs;
	size_t rb = ((size_t)g->width + 7u) / 8u;
	int32_t r0 = max32(0, s->cy0 - top);
	int32_t r1 = min32(g->height, s->cy1 - top);
	int32_t c0 = max32(0, s->cx0 - left);
	int32_t c1 = min32(g->width, s->cx1 - left);
	uint8_t row[32];

	if (g->bitmap == CTAG_FONTPACK_EMPTY_BITMAP || c0 >= c1) {
		return 0;
	}
	for (; r0 < r1; r0++) {
		int32_t c = c0;
		int err = src->read(src->ctx, g->bitmap + (uint32_t)r0 * (uint32_t)rb, row, rb);

		if (err != 0) {
			return err;
		}
		while (c < c1) {
			int32_t start = c;

			while (c < c1 && (row[c >> 3] & (0x80u >> (c & 7))) != 0u) {
				c++;
			}
			if (c > start) {
				fill(s, left + start, top + r0, left + c, top + r0 + 1, color);
			} else {
				c++;
			}
		}
	}
	return 0;
}

static int paint_glyphs(const struct strip *s, const struct ctag_layout_command *cmd)
{
	const struct ctag_layout_cmd_glyphs *run = &cmd->u.glyphs;
	const struct ctag_glyph_source *src = s->r->glyphs;
	int32_t pen_x = run->origin_x;
	int32_t pen_y = run->origin_y;
	uint8_t i;

	for (i = 0u; i < run->count; i++) {
		struct ctag_layout_glyph e;
		struct ctag_glyph g;
		int err;

		ctag_layout_glyph_at(cmd, i, &e);
		pen_x += e.dx;
		pen_y += e.dy;
		if (!box_hits(s, pen_x - GLYPH_REACH_LO, pen_y - GLYPH_REACH_LO,
			      pen_x + GLYPH_REACH_HI, pen_y + GLYPH_REACH_HI)) {
			continue;
		}
		err = src->glyph(src->ctx, run->face, run->size_px, e.glyph_id, &g);
		if (err == -ENOENT) {
			continue;
		}
		if (err == 0) {
			err = blit(s, &g, pen_x + g.bearing_x, pen_y - g.bearing_y, run->color);
		}
		if (err != 0) {
			return err;
		}
	}
	return 0;
}

static int paint_icon(const struct strip *s, const struct ctag_layout_cmd_icon *icon)
{
	const struct ctag_glyph_source *src = s->r->glyphs;
	struct ctag_glyph g;
	int err;

	if (!box_hits(s, icon->x, icon->y, icon->x + 255, icon->y + 255)) {
		return 0;
	}
	err = src->glyph(src->ctx, CTAG_LAYOUT_ICON_FACE, icon->size_px, icon->icon, &g);
	if (err == -ENOENT) {
		return 0;
	}
	/* Icons are drawn at (x, y); stored bearings are not applied. */
	return err != 0 ? err : blit(s, &g, icon->x, icon->y, icon->color);
}

static void paint_line(const struct strip *s, const struct ctag_layout_cmd_line *l)
{
	int32_t x0 = l->x0;
	int32_t y0 = l->y0;
	int32_t x1 = l->x1;
	int32_t y1 = l->y1;
	int32_t w = l->width;
	int32_t o = (w - 1) / 2;
	int32_t dx = x1 > x0 ? x1 - x0 : x0 - x1;
	int32_t dy = y1 > y0 ? y0 - y1 : y1 - y0;
	int32_t sx = x0 < x1 ? 1 : -1;
	int32_t sy = y0 < y1 ? 1 : -1;
	int32_t err = dx + dy;

	if (!box_hits(s, min32(x0, x1) - o, min32(y0, y1) - o, max32(x0, x1) - o + w,
		      max32(y0, y1) - o + w)) {
		return;
	}
	for (;;) {
		int32_t e2;

		fill(s, x0 - o, y0 - o, x0 - o + w, y0 - o + w, l->color);
		if (x0 == x1 && y0 == y1) {
			break;
		}
		e2 = 2 * err;
		if (e2 >= dy) {
			err += dy;
			x0 += sx;
		}
		if (e2 <= dx) {
			err += dx;
			y0 += sy;
		}
	}
}

static void paint_rect(const struct strip *s, int32_t x, int32_t y, int32_t w, int32_t h, int32_t b,
		       uint8_t color)
{
	if (w == 0 || h == 0) {
		return;
	}
	if (b == 0) {
		fill(s, x, y, x + w, y + h, color);
		return;
	}
	/* The bands lx < x+b, lx >= x+w-b, ly < y+b, ly >= y+h-b. */
	fill(s, x, y, x + w, min32(y + b, y + h), color);
	fill(s, x, max32(y + h - b, y), x + w, y + h, color);
	fill(s, x, y, min32(x + b, x + w), y + h, color);
	fill(s, max32(x + w - b, x), y, x + w, y + h, color);
}

static void paint_progress(const struct strip *s, const struct ctag_layout_cmd_progress *p)
{
	paint_rect(s, p->x, p->y, p->w, p->h, 1, p->color);
	if (p->w > 4u && p->h > 4u) {
		uint32_t v = p->value < p->max ? p->value : p->max;
		uint32_t f = p->max == 0u ? 0u : (uint32_t)(p->w - 4u) * v / p->max;

		fill(s, p->x + 2, p->y + 2, p->x + 2 + (int32_t)f, p->y + p->h - 2, p->color);
	}
}

/* The memo entry of a QR command (4.3: at most LAYOUT_MAX_QR per layout, so
 * a validated layout always finds one; NO_MEMO otherwise). */
#define NO_MEMO ((size_t)CTAG_LAYOUT_MAX_QR)

static size_t qr_memo(struct ctag_render_work *w, uint16_t offset)
{
	size_t i;

	for (i = 0; i < CTAG_LAYOUT_MAX_QR; i++) {
		if (w->memo_cmd[i] == offset || w->memo_cmd[i] == QR_NONE) {
			w->memo_cmd[i] = offset;
			return i;
		}
	}
	return NO_MEMO;
}

static uint8_t memo_size(const struct ctag_render_work *w, size_t memo)
{
	return memo < NO_MEMO ? w->memo_size[memo] : 0u;
}

/*
 * The mask Nayuki's automatic choice took, read back from the symbol's first
 * format-bit copy: bits = (data << 10 | BCH) ^ 0x5412 with data = ecl << 3 |
 * mask, and bits 10, 11, 12 are drawn at (4, 8), (3, 8), (2, 8).
 */
static uint8_t qr_mask(const uint8_t *qr)
{
	return (uint8_t)((qrcodegen_getModule(qr, 4, 8) ? 0u : 1u) |
			 (qrcodegen_getModule(qr, 3, 8) ? 2u : 0u) |
			 (qrcodegen_getModule(qr, 2, 8) ? 0u : 4u));
}

/* The command's symbol: from its slot, else encoded into a free slot or the
 * shared last one (the remembered mask gives the same symbol faster). */
static const uint8_t *qr_symbol(struct ctag_render_work *w, const struct ctag_layout_command *cmd,
				size_t memo)
{
	const struct ctag_layout_cmd_qr *q = &cmd->u.qr;
	enum qrcodegen_Mask mask = qrcodegen_Mask_AUTO;
	size_t slot = CTAG_RENDER_QR_SLOTS - 1u;
	size_t i;

	for (i = 0; i < CTAG_RENDER_QR_SLOTS; i++) {
		if (w->qr_cmd[i] == cmd->offset) {
			return w->qr[i];
		}
	}
	for (i = 0; i < CTAG_RENDER_QR_SLOTS; i++) {
		if (w->qr_cmd[i] == QR_NONE) {
			slot = i;
			break;
		}
	}
	if (memo_size(w, memo) != 0u) {
		mask = (enum qrcodegen_Mask)w->memo_mask[memo];
	}
	memcpy(w->text, cmd->var, q->len);
	w->text[q->len] = '\0';
	w->qr_cmd[slot] = QR_NONE;
	if (!qrcodegen_encodeText(w->text, w->tmp, w->qr[slot], (enum qrcodegen_Ecc)q->ecc, 1, 10,
				  mask, true)) {
		return NULL;
	}
	w->qr_cmd[slot] = cmd->offset;
	if (memo < NO_MEMO) {
		w->memo_size[memo] = (uint8_t)qrcodegen_getSize(w->qr[slot]);
		w->memo_mask[memo] = qr_mask(w->qr[slot]);
	}
	return w->qr[slot];
}

static int32_t floor_div(int32_t a, int32_t m)
{
	return a >= 0 ? a / m : -((-a + m - 1) / m);
}

/* Modules [*lo, *hi) of a row or column of n squares of m pixels from o that
 * reach the clip range [c0, c1): (k + 1) * m > c0 - o and k * m < c1 - o. */
static void module_span(int32_t o, int32_t m, int32_t n, int32_t c0, int32_t c1, int32_t *lo,
			int32_t *hi)
{
	*lo = max32(0, floor_div(c0 - o, m));
	*hi = min32(n, -floor_div(o - c1, m));
}

static int paint_qr(const struct strip *s, const struct ctag_layout_command *cmd)
{
	const struct ctag_layout_cmd_qr *q = &cmd->u.qr;
	struct ctag_render_work *w = s->r->work;
	int32_t m = q->module_px;
	size_t memo = qr_memo(w, cmd->offset);
	int32_t n = memo_size(w, memo) != 0u ? memo_size(w, memo) : QR_MAX_MODULES;
	int32_t mx, my, mx0, mx1, my1;
	const uint8_t *qr;

	if (!box_hits(s, q->x, q->y, q->x + n * m, q->y + n * m)) {
		return 0;
	}
	qr = qr_symbol(w, cmd, memo);
	if (qr == NULL) {
		return -EINVAL;
	}
	n = qrcodegen_getSize(qr);
	module_span(q->x, m, n, s->cx0, s->cx1, &mx0, &mx1);
	module_span(q->y, m, n, s->cy0, s->cy1, &my, &my1);
	for (; my < my1; my++) {
		for (mx = mx0; mx < mx1; mx++) {
			if (qrcodegen_getModule(qr, mx, my)) {
				fill(s, q->x + mx * m, q->y + my * m, q->x + (mx + 1) * m,
				     q->y + (my + 1) * m, q->color);
			}
		}
	}
	return 0;
}

static int paint(const struct strip *s)
{
	const struct ctag_render *r = s->r;
	struct ctag_layout_header h;
	struct ctag_layout_iter it;
	struct ctag_layout_command cmd;
	int err = 0;

	(void)ctag_layout_iter_init(&it, r->layout, r->len, &h);
	while (err == 0 && ctag_layout_iter_next(&it, &cmd)) {
		switch (cmd.op) {
		case CTAG_LAYOUT_CMD_CLEAR:
			fill(s, 0, 0, h.width, h.height, cmd.u.clear.color);
			break;
		case CTAG_LAYOUT_CMD_GLYPHS:
			err = paint_glyphs(s, &cmd);
			break;
		case CTAG_LAYOUT_CMD_ICON:
			err = paint_icon(s, &cmd.u.icon);
			break;
		case CTAG_LAYOUT_CMD_LINE:
			paint_line(s, &cmd.u.line);
			break;
		case CTAG_LAYOUT_CMD_RECT:
			paint_rect(s, cmd.u.rect.x, cmd.u.rect.y, cmd.u.rect.w, cmd.u.rect.h,
				   cmd.u.rect.border, cmd.u.rect.color);
			break;
		case CTAG_LAYOUT_CMD_PROGRESS:
			paint_progress(s, &cmd.u.progress);
			break;
		default:
			err = paint_qr(s, &cmd);
			break;
		}
	}
	return err;
}

/* Colour -> bit for one plane (4.4 Planes); *pad = the plane's white value. */
static uint8_t plane_bits(const struct ctag_render_panel *p, uint8_t plane, bool *pad)
{
	unsigned int white0 = p->plane_flags & 1u;
	unsigned int red1 = (p->plane_flags >> 1) & 1u;

	if (plane == 0u) {
		/* planes = 1: red is black; planes = 2: red takes plane 0's white value. */
		unsigned int red = p->planes == 2u ? white0 : white0 ^ 1u;

		*pad = white0 != 0u;
		return (uint8_t)(white0 << CTAG_COLOR_WHITE | (white0 ^ 1u) << CTAG_COLOR_BLACK |
				 red << CTAG_COLOR_RED);
	}
	*pad = red1 == 0u;
	return (uint8_t)((red1 ^ 1u) << CTAG_COLOR_WHITE | (red1 ^ 1u) << CTAG_COLOR_BLACK |
			 red1 << CTAG_COLOR_RED);
}

uint8_t ctag_render_init(struct ctag_render *r, const uint8_t *layout, size_t len,
			 const struct ctag_render_panel *panel,
			 const struct ctag_glyph_source *glyphs, struct ctag_render_work *work)
{
	struct ctag_layout_iter it;
	uint8_t st = ctag_layout_validate(layout, len, glyphs->has_strike, glyphs->ctx);

	if (st != CTAG_STATUS_OK) {
		return st;
	}
	(void)ctag_layout_iter_init(&it, layout, len, &r->hdr);
	st = ctag_layout_check_panel(&r->hdr, panel->width, panel->height);
	if (st != CTAG_STATUS_OK) {
		return st;
	}
	if (panel->planes < 1u || panel->planes > 2u) {
		return CTAG_STATUS_INVALID;
	}
	r->layout = layout;
	r->len = len;
	r->panel = *panel;
	r->glyphs = glyphs;
	r->work = work;
	memset(work->qr_cmd, 0xFF, sizeof(work->qr_cmd));
	memset(work->memo_cmd, 0xFF, sizeof(work->memo_cmd));
	memset(work->memo_size, 0, sizeof(work->memo_size));
	return CTAG_STATUS_OK;
}

int ctag_render_strip(struct ctag_render *r, uint8_t plane, uint16_t y0, uint16_t rows,
		      uint8_t *out, size_t size)
{
	int32_t wn = r->panel.width;
	int32_t hn = r->panel.height;
	int32_t w = r->hdr.width;
	int32_t h = r->hdr.height;
	int32_t y1 = min32((int32_t)y0 + rows, hn);
	struct strip s;
	bool pad;
	int32_t y;
	int err;

	if (plane >= r->panel.planes || y0 >= hn || rows == 0u) {
		return -EINVAL;
	}
	s.r = r;
	s.out = out;
	s.row_bytes = ctag_render_row_bytes(&r->panel);
	s.y0 = y0;
	if (size < (size_t)(y1 - y0) * s.row_bytes) {
		return -EMSGSIZE;
	}
	s.bits = plane_bits(&r->panel, plane, &pad);
	for (y = y0; y < y1; y++) {
		uint8_t *row = &out[(size_t)(y - y0) * s.row_bytes];

		set_span(row, 0u, (uint32_t)wn, color_bit(&s, r->hdr.background));
		set_span(row, (uint32_t)wn, (uint32_t)(s.row_bytes * 8u), pad);
	}
	/* Logical rectangle that maps onto native rows [y0, y1) (4.4 Rotation). */
	switch (r->hdr.rotation) {
	case 0:
		s.cx0 = 0;
		s.cy0 = y0;
		s.cx1 = w;
		s.cy1 = y1;
		break;
	case 1:
		s.cx0 = y0;
		s.cy0 = 0;
		s.cx1 = y1;
		s.cy1 = h;
		break;
	case 2:
		s.cx0 = 0;
		s.cy0 = hn - y1;
		s.cx1 = w;
		s.cy1 = hn - y0;
		break;
	default:
		s.cx0 = hn - y1;
		s.cy0 = 0;
		s.cx1 = hn - y0;
		s.cy1 = h;
		break;
	}
	s.cx0 = max32(s.cx0, 0);
	s.cy0 = max32(s.cy0, 0);
	s.cx1 = min32(s.cx1, w);
	s.cy1 = min32(s.cy1, h);
	err = paint(&s);
	return err != 0 ? err : (int)((size_t)(y1 - y0) * s.row_bytes);
}

int ctag_render_frame_digest(struct ctag_render *r, uint8_t *buf, size_t size,
			     const struct ctag_sha256_ops *sha, uint8_t digest[32])
{
	size_t rows = size / ctag_render_row_bytes(&r->panel);
	uint8_t plane;

	if (rows == 0u) {
		return -EMSGSIZE;
	}
	if (rows > UINT16_MAX) {
		rows = UINT16_MAX;
	}
	if (sha->init(sha->ctx) != 0) {
		return -EIO;
	}
	for (plane = 0u; plane < r->panel.planes; plane++) {
		uint32_t y;

		for (y = 0u; y < r->panel.height; y += (uint32_t)rows) {
			int n = ctag_render_strip(r, plane, (uint16_t)y, (uint16_t)rows, buf, size);

			if (n < 0) {
				return n;
			}
			if (sha->update(sha->ctx, buf, (size_t)n) != 0) {
				return -EIO;
			}
		}
	}
	return sha->finish(sha->ctx, digest) != 0 ? -EIO : 0;
}
