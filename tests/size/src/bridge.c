/* Calls the gateway/bridge library API (see tag.c). */
#include <ctag/ctag_cbor.h>
#include <ctag/ctag_crypto.h>
#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_frame.h>
#include <ctag/ctag_render.h>
#include <ctag/ctag_session.h>

void size_bridge(void);

/* Small buffers: only the code size matters here (and the M0 has 16 KiB RAM). */
static uint8_t rx_buf[256];
static uint8_t tx_buf[256];
static uint8_t wire[CTAG_COBS_MAX_ENCODED(256u) + 1u];
static uint8_t layout[256];
static uint8_t strip[4 * 50];
static struct ctag_serial_rx serial;
static struct ctag_layout_asm assembler;
static struct ctag_fontpack pack;
static struct ctag_render_work work;
static struct ctag_render render;
static struct ctag_session session;
static ctag_sha256_ctx sha_ctx;

static int sha_init(void *ctx)
{
	return ctag_crypto_sha256_init(ctx);
}

static int sha_update(void *ctx, const uint8_t *data, size_t len)
{
	return ctag_crypto_sha256_update(ctx, data, len);
}

static int sha_finish(void *ctx, uint8_t digest[32])
{
	return ctag_crypto_sha256_finish(ctx, digest);
}

static int flash_read(void *ctx, uint32_t offset, void *buf, size_t len)
{
	(void)ctx;
	(void)offset;
	(void)buf;
	(void)len;
	return 0;
}

void size_bridge(void)
{
	const struct ctag_sha256_ops sha = {sha_init, sha_update, sha_finish, &sha_ctx};
	struct ctag_render_panel panel = {400, 300, 2, 3};
	struct ctag_cbor_field fields[4] = {{.key = CTAG_CBOR_KEY_STATUS}};
	struct ctag_cbor_counter counters[4];
	struct ctag_cbor_str items[4];
	struct ctag_serial_header hdr;
	struct ctag_mesh_layout_begin begin = {0};
	struct ctag_mesh_layout_chunk chunk = {0};
	struct ctag_glyph_source src;
	struct ctag_ctrl_challenge ch;
	uint8_t digest[32];
	uint32_t missing;
	int n;

	ctag_serial_rx_init(&serial, rx_buf, sizeof(rx_buf));
	if (ctag_serial_rx_put(&serial, wire[0], &hdr) &&
	    ctag_cbor_decode(&rx_buf[CTAG_SERIAL_HEADER_LEN], hdr.length, fields, 4) == 0) {
		(void)ctag_cbor_maps(&fields[0].v.str, items, 4);
		(void)ctag_cbor_counters(&fields[0].v.str, counters, 4);
	}
	n = ctag_cbor_encode(fields, 1, &tx_buf[CTAG_SERIAL_HEADER_LEN],
			     sizeof(tx_buf) - CTAG_SERIAL_MIN_FRAME);
	n = ctag_serial_frame_build(tx_buf, sizeof(tx_buf), &hdr, NULL, (size_t)n);
	(void)ctag_serial_wire_encode(tx_buf, (size_t)n, wire, sizeof(wire));
	(void)ctag_cobs_decode(wire, sizeof(wire), tx_buf, sizeof(tx_buf));

	ctag_layout_asm_init(&assembler, layout, sizeof(layout));
	ctag_layout_asm_begin(&assembler, &begin);
	(void)ctag_layout_asm_chunk(&assembler, &chunk);
	(void)ctag_layout_asm_commit(&assembler, 0, &missing, &sha);
	if (ctag_fontpack_open(&pack, flash_read, NULL, 1u << 24) == CTAG_STATUS_OK) {
		(void)ctag_fontpack_verify_content(&pack, &sha, strip, sizeof(strip));
	}
	ctag_fontpack_glyph_source(&pack, &src);
	if (ctag_render_init(&render, layout, begin.total_len, &panel, &src, &work) ==
	    CTAG_STATUS_OK) {
		(void)ctag_render_frame_digest(&render, strip, sizeof(strip), &sha, digest);
		(void)ctag_render_strip(&render, 0, 0, 4, strip, sizeof(strip));
	}

	(void)ctag_session_bridge_hello(&session, 1, 1, digest, digest, tx_buf);
	(void)ctag_session_bridge_challenge(&session, rx_buf, 18, rx_buf, 30, &ch, tx_buf);
	(void)ctag_session_bridge_auth_ok(&session, rx_buf, 17);
}
