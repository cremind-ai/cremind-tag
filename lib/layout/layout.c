/* Layout validation and iteration (docs/protocol.md 4.3); mirrors protocol/layout.py. */
#include <errno.h>

#include <ctag/ctag_layout.h>

/* Fixed part length (after the op byte) per command op; 0 = unknown op. */
static const uint8_t cmd_fixed_len[] = {
	[CTAG_LAYOUT_CMD_CLEAR] = CTAG_LAYOUT_CMD_CLEAR_LEN,
	[CTAG_LAYOUT_CMD_GLYPHS] = CTAG_LAYOUT_CMD_GLYPHS_LEN,
	[CTAG_LAYOUT_CMD_ICON] = CTAG_LAYOUT_CMD_ICON_LEN,
	[CTAG_LAYOUT_CMD_LINE] = CTAG_LAYOUT_CMD_LINE_LEN,
	[CTAG_LAYOUT_CMD_RECT] = CTAG_LAYOUT_CMD_RECT_LEN,
	[CTAG_LAYOUT_CMD_PROGRESS] = CTAG_LAYOUT_CMD_PROGRESS_LEN,
	[CTAG_LAYOUT_CMD_QR] = CTAG_LAYOUT_CMD_QR_LEN,
};

/* Parse the command at *pos: known op, then fixed and variable parts present. */
static uint8_t read_cmd(const uint8_t *data, size_t len, size_t *pos,
			struct ctag_layout_command *cmd)
{
	size_t p = *pos;
	size_t fixed;
	size_t var = 0u;

	if (p >= len) {
		return CTAG_STATUS_INVALID;
	}
	cmd->op = data[p];
	cmd->offset = (uint16_t)p;
	p++;
	if (cmd->op >= sizeof(cmd_fixed_len) || cmd_fixed_len[cmd->op] == 0u) {
		return CTAG_STATUS_UNSUPPORTED;
	}
	fixed = cmd_fixed_len[cmd->op];
	if (len - p < fixed) {
		return CTAG_STATUS_INVALID;
	}
	switch (cmd->op) {
	case CTAG_LAYOUT_CMD_CLEAR:
		(void)ctag_layout_cmd_clear_unpack(&cmd->u.clear, &data[p], fixed);
		break;
	case CTAG_LAYOUT_CMD_GLYPHS:
		(void)ctag_layout_cmd_glyphs_unpack(&cmd->u.glyphs, &data[p], fixed);
		var = (size_t)cmd->u.glyphs.count * CTAG_LAYOUT_GLYPH_LEN;
		break;
	case CTAG_LAYOUT_CMD_ICON:
		(void)ctag_layout_cmd_icon_unpack(&cmd->u.icon, &data[p], fixed);
		break;
	case CTAG_LAYOUT_CMD_LINE:
		(void)ctag_layout_cmd_line_unpack(&cmd->u.line, &data[p], fixed);
		break;
	case CTAG_LAYOUT_CMD_RECT:
		(void)ctag_layout_cmd_rect_unpack(&cmd->u.rect, &data[p], fixed);
		break;
	case CTAG_LAYOUT_CMD_PROGRESS:
		(void)ctag_layout_cmd_progress_unpack(&cmd->u.progress, &data[p], fixed);
		break;
	default:
		(void)ctag_layout_cmd_qr_unpack(&cmd->u.qr, &data[p], fixed);
		var = cmd->u.qr.len;
		break;
	}
	p += fixed;
	if (len - p < var) {
		return CTAG_STATUS_INVALID;
	}
	cmd->var = &data[p];
	*pos = p + var;
	return CTAG_STATUS_OK;
}

static bool bad_color(uint8_t color)
{
	return color > CTAG_COLOR_RED;
}

/* Field bounds of 4.3 in field order. */
static uint8_t check_fields(const struct ctag_layout_command *cmd)
{
	uint8_t color;
	uint8_t i;

	switch (cmd->op) {
	case CTAG_LAYOUT_CMD_CLEAR:
		color = cmd->u.clear.color;
		break;
	case CTAG_LAYOUT_CMD_GLYPHS:
		color = cmd->u.glyphs.color;
		break;
	case CTAG_LAYOUT_CMD_ICON:
		color = cmd->u.icon.color;
		break;
	case CTAG_LAYOUT_CMD_LINE:
		if (cmd->u.line.width < 1u || cmd->u.line.width > 8u) {
			return CTAG_STATUS_INVALID;
		}
		color = cmd->u.line.color;
		break;
	case CTAG_LAYOUT_CMD_RECT:
		color = cmd->u.rect.color;
		break;
	case CTAG_LAYOUT_CMD_PROGRESS:
		color = cmd->u.progress.color;
		break;
	default:
		if (cmd->u.qr.module_px < 1u || cmd->u.qr.module_px > 8u ||
		    cmd->u.qr.ecc > CTAG_QR_ECC_HIGH || bad_color(cmd->u.qr.color) ||
		    cmd->u.qr.len < 1u || cmd->u.qr.len > CTAG_LAYOUT_QR_MAX_TEXT) {
			return CTAG_STATUS_INVALID;
		}
		for (i = 0u; i < cmd->u.qr.len; i++) {
			if (cmd->var[i] < 0x21u || cmd->var[i] > 0x7Eu) {
				return CTAG_STATUS_INVALID;
			}
		}
		/*
		 * The version-10 fit rule of 4.4 cannot fail: 96 bytes always fit
		 * (4.3), so no QR encoding happens here.
		 */
		return CTAG_STATUS_OK;
	}
	return bad_color(color) ? CTAG_STATUS_INVALID : CTAG_STATUS_OK;
}

int ctag_layout_iter_init(struct ctag_layout_iter *it, const uint8_t *data, size_t len,
			  struct ctag_layout_header *hdr)
{
	if (len < CTAG_LAYOUT_HEADER_LEN) {
		return -EINVAL;
	}
	(void)ctag_layout_header_unpack(hdr, data, CTAG_LAYOUT_HEADER_LEN);
	it->data = data;
	it->len = len;
	it->pos = CTAG_LAYOUT_HEADER_LEN;
	it->left = hdr->cmd_count;
	return 0;
}

bool ctag_layout_iter_next(struct ctag_layout_iter *it, struct ctag_layout_command *cmd)
{
	if (it->left == 0u || read_cmd(it->data, it->len, &it->pos, cmd) != CTAG_STATUS_OK) {
		return false;
	}
	it->left--;
	return true;
}

uint8_t ctag_layout_validate(const uint8_t *data, size_t len, ctag_layout_has_strike_fn has_strike,
			     void *ctx)
{
	struct ctag_layout_header h;
	struct ctag_layout_iter it;
	struct ctag_layout_command cmd;
	uint32_t glyphs = 0u;
	uint8_t st;

	if (len > CTAG_LAYOUT_HARD_MAX) {
		return CTAG_STATUS_TOO_LARGE;
	}
	if (ctag_layout_iter_init(&it, data, len, &h) != 0 || h.magic != CTAG_LAYOUT_MAGIC) {
		return CTAG_STATUS_INVALID;
	}
	if (h.version != CTAG_PROTO_VERSION) {
		return CTAG_STATUS_UNSUPPORTED;
	}
	if (h.width < 1u || h.width > CTAG_LAYOUT_MAX_SIDE || h.height < 1u ||
	    h.height > CTAG_LAYOUT_MAX_SIDE || h.rotation > 3u || bad_color(h.background)) {
		return CTAG_STATUS_INVALID;
	}
	if (h.cmd_count > CTAG_LAYOUT_MAX_COMMANDS) {
		return CTAG_STATUS_TOO_LARGE;
	}
	while (it.left > 0u) {
		st = read_cmd(data, len, &it.pos, &cmd);
		if (st == CTAG_STATUS_OK) {
			st = check_fields(&cmd);
		}
		if (st != CTAG_STATUS_OK) {
			return st;
		}
		if (cmd.op == CTAG_LAYOUT_CMD_GLYPHS) {
			glyphs += cmd.u.glyphs.count;
			if (glyphs > CTAG_LAYOUT_MAX_GLYPHS) {
				return CTAG_STATUS_TOO_LARGE;
			}
		}
		it.left--;
	}
	if (it.pos != len) {
		return CTAG_STATUS_INVALID;
	}
	if (has_strike == NULL) {
		return CTAG_STATUS_OK;
	}
	(void)ctag_layout_iter_init(&it, data, len, &h);
	while (ctag_layout_iter_next(&it, &cmd)) {
		if ((cmd.op == CTAG_LAYOUT_CMD_GLYPHS &&
		     !has_strike(ctx, cmd.u.glyphs.face, cmd.u.glyphs.size_px)) ||
		    (cmd.op == CTAG_LAYOUT_CMD_ICON &&
		     !has_strike(ctx, CTAG_LAYOUT_ICON_FACE, cmd.u.icon.size_px))) {
			return CTAG_STATUS_FONTPACK_MISMATCH;
		}
	}
	return CTAG_STATUS_OK;
}

uint8_t ctag_layout_check_panel(const struct ctag_layout_header *hdr, uint16_t native_width,
				uint16_t native_height)
{
	bool quarter = (hdr->rotation & 1u) != 0u;
	uint16_t w = quarter ? native_height : native_width;
	uint16_t h = quarter ? native_width : native_height;

	return hdr->width == w && hdr->height == h ? CTAG_STATUS_OK : CTAG_STATUS_INVALID;
}
