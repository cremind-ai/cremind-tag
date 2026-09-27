/* Shared fixtures of the bridge core tests. */
#include <string.h>

#include <zephyr/drivers/flash.h>
#include <zephyr/drivers/flash/flash_simulator.h>
#include <zephyr/sys/util.h>

#include "common.h"
#include "v_fontpack.h"
#include "v_layouts.h"

struct bridge_env benv;
uint8_t scratch[CTAG_LAYOUT_HARD_MAX] __aligned(4);

const uint8_t *fixture_pack = V_FONTPACK_DATA;
size_t fixture_pack_len = V_FONTPACK_LEN;
const uint8_t *fixture_pack_id = V_FONTPACK_PACK_ID;
const uint8_t *small_layout;
size_t small_layout_len;

/* ---- SHA-256 over PSA ---- */

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

const struct ctag_sha256_ops test_sha = {sha_init, sha_update, sha_finish, &sha_ctx};

void sha256(const void *data, size_t len, uint8_t out[32])
{
	ctag_sha256_ctx c;

	zassert_ok(ctag_crypto_sha256_init(&c));
	zassert_ok(ctag_crypto_sha256_update(&c, data, len));
	zassert_ok(ctag_crypto_sha256_finish(&c, out));
}

/* ---- Flash ---- */

static int32_t cut = -1;

static int cut_write(const struct device *dev, off_t offset, uint8_t data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(offset);
	if (cut == 0) {
		return -EIO;
	}
	if (cut > 0) {
		cut--;
	}
	return data;
}

static int cut_erase(const struct device *dev, off_t unit)
{
	size_t size;
	uint8_t *mem = flash_simulator_get_memory(dev, &size);

	if (cut == 0) {
		return -EIO; /* power lost before the erase */
	}
	memset(mem + unit, 0xFF, 4096u);
	return 0;
}

static const struct flash_simulator_cb cut_cb = {.write_byte = cut_write, .erase_unit = cut_erase};

void flash_cut_after(int32_t bytes)
{
	cut = bytes;
}

uint8_t *flash_mem(void)
{
	size_t size;

	return flash_simulator_get_memory(EXT_FLASH_DEV, &size);
}

void flash_wipe(void)
{
	size_t size;
	uint8_t *mem = flash_simulator_get_memory(EXT_FLASH_DEV, &size);

	memset(mem, 0xFF, size);
	flash_simulator_set_callbacks(EXT_FLASH_DEV, &cut_cb);
	cut = -1;
}

/* ---- Persistence fake ---- */

struct kv {
	bool used;
	char name[8];
	uint8_t data[64];
	size_t len;
};

static struct kv kv[96];
void (*save_probe)(const char *name);

void kv_clear(void)
{
	memset(kv, 0, sizeof(kv));
}

int kv_save(void *ctx, const char *name, const void *data, size_t len)
{
	struct kv *free_slot = NULL;
	size_t i;

	ARG_UNUSED(ctx);
	if (save_probe != NULL) {
		save_probe(name);
	}
	zassert_true(len <= sizeof(kv[0].data), "record %s too long (%zu)", name, len);
	for (i = 0; i < ARRAY_SIZE(kv); i++) {
		if (kv[i].used && strcmp(kv[i].name, name) == 0) {
			free_slot = &kv[i];
			break;
		}
		if (!kv[i].used && free_slot == NULL) {
			free_slot = &kv[i];
		}
	}
	zassert_not_null(free_slot);
	if (len == 0u) {
		free_slot->used = false;
		return 0;
	}
	free_slot->used = true;
	strncpy(free_slot->name, name, sizeof(free_slot->name) - 1u);
	memcpy(free_slot->data, data, len);
	free_slot->len = len;
	return 0;
}

void kv_replay(struct dlv *d)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(kv); i++) {
		if (kv[i].used) {
			dlv_restore(d, kv[i].name, kv[i].data, kv[i].len);
		}
	}
}

size_t kv_count(void)
{
	size_t i, n = 0u;

	for (i = 0; i < ARRAY_SIZE(kv); i++) {
		n += kv[i].used ? 1u : 0u;
	}
	return n;
}

bool kv_get(const char *name, void *data, size_t *len)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(kv); i++) {
		if (kv[i].used && strcmp(kv[i].name, name) == 0) {
			memcpy(data, kv[i].data, kv[i].len);
			*len = kv[i].len;
			return true;
		}
	}
	return false;
}

/* ---- Outbound mesh messages ---- */

struct sent_msg sent_log[128];
size_t sent_n;
void (*send_probe)(uint8_t op, const uint8_t *params, size_t len);

void sent_clear(void)
{
	sent_n = 0u;
}

void record_send(void *ctx, uint8_t op, const uint8_t *params, size_t len)
{
	ARG_UNUSED(ctx);
	zassert_true(len <= CTAG_MESH_MAX_VENDOR_PARAMS);
	if (send_probe != NULL) {
		send_probe(op, params, len);
	}
	if (sent_n < ARRAY_SIZE(sent_log)) {
		sent_log[sent_n].op = op;
		sent_log[sent_n].len = (uint8_t)len;
		memcpy(sent_log[sent_n].data, params, len);
		sent_n++;
	}
}

size_t sent_count(uint8_t op)
{
	size_t i, n = 0u;

	for (i = 0; i < sent_n; i++) {
		n += sent_log[i].op == op ? 1u : 0u;
	}
	return n;
}

bool last_result(uint64_t update_id, struct ctag_mesh_delivery_result *r)
{
	bool found = false;
	size_t i;

	for (i = 0; i < sent_n; i++) {
		struct ctag_mesh_delivery_result m;

		if (sent_log[i].op == CTAG_MESH_OP_DELIVERY_RESULT &&
		    ctag_mesh_delivery_result_unpack(&m, sent_log[i].data, sent_log[i].len) == 0 &&
		    m.update_id == update_id) {
			*r = m;
			found = true;
		}
	}
	return found;
}

size_t results_for(uint64_t update_id)
{
	size_t i, n = 0u;

	for (i = 0; i < sent_n; i++) {
		struct ctag_mesh_delivery_result m;

		if (sent_log[i].op == CTAG_MESH_OP_DELIVERY_RESULT &&
		    ctag_mesh_delivery_result_unpack(&m, sent_log[i].data, sent_log[i].len) == 0 &&
		    m.update_id == update_id) {
			n++;
		}
	}
	return n;
}

void assert_one_seq_per_update(void)
{
	size_t i, k;

	for (i = 0; i < sent_n; i++) {
		struct ctag_mesh_delivery_result a, b;

		if (sent_log[i].op != CTAG_MESH_OP_DELIVERY_RESULT ||
		    ctag_mesh_delivery_result_unpack(&a, sent_log[i].data, sent_log[i].len) != 0) {
			continue;
		}
		for (k = i + 1u; k < sent_n; k++) {
			if (sent_log[k].op != CTAG_MESH_OP_DELIVERY_RESULT ||
			    ctag_mesh_delivery_result_unpack(&b, sent_log[k].data, sent_log[k].len) !=
				    0) {
				continue;
			}
			if (a.update_id == b.update_id) {
				zassert_equal(a.result_seq, b.result_seq,
					      "update_id %llu: result_seq %u and %u",
					      (unsigned long long)a.update_id, a.result_seq,
					      b.result_seq);
				zassert_true(a.status == b.status && a.tag_id == b.tag_id &&
						     a.epoch == b.epoch && a.revision == b.revision &&
						     memcmp(a.digest, b.digest, sizeof(a.digest)) == 0,
					     "update_id %llu: another result",
					     (unsigned long long)a.update_id);
			} else {
				zassert_not_equal(a.result_seq, b.result_seq,
						  "result_seq %u for two update_ids", a.result_seq);
			}
		}
	}
}

/* ---- Environment ---- */

void env_boot(uint32_t now)
{
	struct dlv_env env = {
		.send = record_send,
		.save = kv_save,
		.flash = &benv.flash,
		.fonts = &benv.fonts,
		.sha = &test_sha,
	};

	zassert_ok(ctag_crypto_init());
	benv.flash_ok = bflash_init(&benv.flash, EXT_FLASH_DEV, CONFIG_CTAG_BRIDGE_WORKING_SPACE) == 0;
	fontstore_init(&benv.fonts, &benv.flash, benv.flash_ok);
	env.flash_ok = benv.flash_ok;
	dlv_init(&benv.dlv, &env);
	kv_replay(&benv.dlv);
	dlv_start(&benv.dlv, now);
	small_layout = v_layouts_valid[0].data;
	small_layout_len = v_layouts_valid[0].len;
}

void env_fresh(uint32_t now)
{
	save_probe = NULL;
	send_probe = NULL;
	flash_wipe();
	kv_clear();
	sent_clear();
	env_boot(now);
}

uint8_t install_pack(const uint8_t *pack, size_t len, size_t chunk)
{
	uint8_t digest[32];
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	uint8_t slot;
	size_t off;
	uint8_t st;

	sha256(pack, len, digest);
	st = fontstore_begin(&benv.fonts, (uint32_t)len, digest, &pack[16], &slot);
	for (off = 0u; st == CTAG_STATUS_OK && off < len; off += chunk) {
		st = fontstore_data(&benv.fonts, (uint32_t)off, &pack[off], MIN(chunk, len - off));
	}
	if (st == CTAG_STATUS_OK) {
		st = fontstore_commit(&benv.fonts, &test_sha, scratch, 1000u, id);
	}
	return st;
}

void install_fixture_pack(void)
{
	zassert_equal(install_pack(fixture_pack, fixture_pack_len, 700u), CTAG_STATUS_OK);
}

/* ---- Layout delivery ---- */

void layout_digest(const uint8_t *layout, size_t len, uint8_t digest[CTAG_LAYOUT_DIGEST_LEN])
{
	uint8_t full[32];

	sha256(layout, len, full);
	memcpy(digest, full, CTAG_LAYOUT_DIGEST_LEN);
}

void transfer(struct dlv *d, const struct xfer *x, const uint8_t *layout, size_t len,
	      uint32_t skip)
{
	struct ctag_mesh_layout_begin b = {
		.xfer_id = x->xfer_id,
		.tag_id = x->tag_id,
		.epoch = x->epoch,
		.revision = x->revision,
		.update_id = x->update_id,
		.total_len = (uint16_t)len,
		.chunk_count = (uint8_t)DIV_ROUND_UP(len, CTAG_LAYOUT_CHUNK_DATA_MAX),
	};
	size_t i;

	memcpy(b.fontpack_id, x->fontpack_id != NULL ? x->fontpack_id : fixture_pack_id, 8);
	layout_digest(layout, len, b.digest);
	dlv_layout_begin(d, &b);
	for (i = 0; i < b.chunk_count; i++) {
		struct ctag_mesh_layout_chunk c = {
			.xfer_id = x->xfer_id,
			.index = (uint8_t)i,
			.data = &layout[i * CTAG_LAYOUT_CHUNK_DATA_MAX],
			.data_len = MIN((size_t)CTAG_LAYOUT_CHUNK_DATA_MAX,
					len - i * CTAG_LAYOUT_CHUNK_DATA_MAX),
		};

		if (!(skip & BIT(i))) {
			dlv_layout_chunk(d, &c);
		}
	}
}

uint8_t deliver(struct dlv *d, const struct xfer *x, const uint8_t *layout, size_t len,
		uint32_t skip, uint32_t now, uint32_t *missing)
{
	uint32_t dummy;

	transfer(d, x, layout, len, skip);
	return dlv_layout_commit(d, x->xfer_id, now, missing != NULL ? missing : &dummy);
}

void assign(struct dlv *d, uint32_t tag_id, uint32_t epoch, const uint8_t key[16])
{
	struct ctag_mesh_assign_set m = {.tag_id = tag_id, .epoch = epoch, .flags = 1u};

	if (key != NULL) {
		memcpy(m.key, key, sizeof(m.key));
	}
	zassert_equal(dlv_assign_set(d, &m, 0u), CTAG_STATUS_OK);
}

size_t big_layout(uint8_t *out)
{
	struct ctag_layout_header h = {
		.magic = CTAG_LAYOUT_MAGIC,
		.version = CTAG_PROTO_VERSION,
		.width = 400,
		.height = 300,
		.cmd_count = 2 + 184,
	};
	size_t pos = (size_t)ctag_layout_header_pack(&h, out, CTAG_LAYOUT_HEADER_LEN);
	int run, i;

	for (run = 0; run < 2; run++) {
		struct ctag_layout_cmd_glyphs g = {
			.face = 1, .size_px = 16, .color = 1, .origin_x = 10,
			.origin_y = (int16_t)(40 + run * 40), .count = 255,
		};

		out[pos++] = CTAG_LAYOUT_CMD_GLYPHS;
		pos += (size_t)ctag_layout_cmd_glyphs_pack(&g, &out[pos], CTAG_LAYOUT_CMD_GLYPHS_LEN);
		for (i = 0; i < 255; i++) {
			struct ctag_layout_glyph e = {.glyph_id = (uint16_t)(i % 20), .dx = 1, .dy = 0};

			pos += (size_t)ctag_layout_glyph_pack(&e, &out[pos], CTAG_LAYOUT_GLYPH_LEN);
		}
	}
	for (i = 0; i < 184; i++) {
		struct ctag_layout_cmd_rect r = {
			.x = (int16_t)(i * 2), .y = (int16_t)(i % 280), .w = 10, .h = 5,
			.border = (uint8_t)(i % 3), .color = (uint8_t)(1 + i % 2),
		};

		out[pos++] = CTAG_LAYOUT_CMD_RECT;
		pos += (size_t)ctag_layout_cmd_rect_pack(&r, &out[pos], CTAG_LAYOUT_CMD_RECT_LEN);
	}
	zassert_equal(pos, CTAG_LAYOUT_HARD_MAX);
	zassert_equal(ctag_layout_validate(out, pos, NULL, NULL), CTAG_STATUS_OK);
	return pos;
}
