/* Font store: active pack, installation, FLASH_TEST (docs/fontpack.md 3-4). */
#include <errno.h>
#include <string.h>

#include <zephyr/sys/util.h>

#include "fontstore.h"

#define MIB16 (16u * 1024u * 1024u)

static int reader_read(void *ctx, uint32_t off, void *buf, size_t len)
{
	struct fontstore_reader *rd = ctx;

	if (off > rd->limit || len > rd->limit - off) {
		return -EINVAL;
	}
	return rd->cached ? bflash_cached_read(rd->flash, rd->base + off, buf, len)
			  : bflash_read(rd->flash, rd->base + off, buf, len);
}

/* Directory -> active pack, validated as at boot (no content hash). Locked. */
static void load_active(struct fontstore *fs)
{
	uint8_t st;

	fs->has_record = false;
	fs->valid = false;
	if (!fs->usable || bflash_dir_active(fs->flash, &fs->active, NULL) != 0) {
		return;
	}
	fs->has_record = true;
	fs->rd.flash = fs->flash;
	fs->rd.base = bflash_slot_offset(fs->flash, fs->active.slot);
	fs->rd.limit = fs->active.size;
	fs->rd.cached = true;
	st = ctag_fontpack_open(&fs->pack, reader_read, &fs->rd, fs->active.size);
	fs->last_open_status = st;
	fs->valid = st == CTAG_STATUS_OK && fs->pack.total == fs->active.size &&
		    memcmp(fs->pack.pack_id, fs->active.pack_id, CTAG_FONTPACK_ID_LEN) == 0;
}

void fontstore_init(struct fontstore *fs, struct bflash *flash, bool usable)
{
	memset(fs, 0, sizeof(*fs));
	fs->flash = flash;
	k_mutex_init(&fs->lock);
	fs->usable = usable && flash->geom.slot_size > 0u;
	load_active(fs);
}

bool fontstore_active_id(struct fontstore *fs, uint8_t id[CTAG_FONTPACK_ID_LEN])
{
	bool valid;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	valid = fs->valid;
	if (valid) {
		memcpy(id, fs->pack.pack_id, CTAG_FONTPACK_ID_LEN);
	}
	(void)k_mutex_unlock(&fs->lock);
	return valid;
}

bool fontstore_has_strike(void *ctx, uint16_t face, uint8_t size_px)
{
	struct fontstore *fs = ctx;
	struct ctag_glyph_source src;
	bool has = false;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	if (fs->valid) {
		ctag_fontpack_glyph_source(&fs->pack, &src);
		has = src.has_strike(src.ctx, face, size_px);
	}
	(void)k_mutex_unlock(&fs->lock);
	return has;
}

bool fontstore_view_open(struct fontstore *fs, struct fontstore_view *v)
{
	bool ok;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	ok = fs->valid;
	if (ok) {
		v->rd = fs->rd;
		v->pack = fs->pack;
		v->pack.ctx = &v->rd;
		v->pack.hit_state = 0u;
		v->slot = fs->active.slot;
		v->open = true;
		fs->views[v->slot]++;
		ctag_fontpack_glyph_source(&v->pack, &v->src);
	}
	(void)k_mutex_unlock(&fs->lock);
	return ok;
}

void fontstore_view_close(struct fontstore *fs, struct fontstore_view *v)
{
	if (!v->open) {
		return;
	}
	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	if (fs->views[v->slot] > 0u) {
		fs->views[v->slot]--;
	}
	v->open = false;
	(void)k_mutex_unlock(&fs->lock);
}

/* ---- Installation ---- */

uint8_t fontstore_begin(struct fontstore *fs, uint32_t size, const uint8_t digest[32],
			const uint8_t pack_id[CTAG_FONTPACK_ID_LEN], uint8_t *slot)
{
	uint8_t st = CTAG_STATUS_OK;
	uint8_t target;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	target = fs->has_record ? (uint8_t)(1u - fs->active.slot) : 0u;
	if (!fs->usable) {
		st = CTAG_STATUS_NO_RESOURCES;
	} else if (size == 0u) {
		st = CTAG_STATUS_INVALID;
	} else if (size > fs->flash->geom.slot_size) {
		st = CTAG_STATUS_TOO_LARGE;
	} else if (fs->views[target] > 0u) {
		st = CTAG_STATUS_BUSY; /* a tag session still reads the previous pack there */
	}
	if (st == CTAG_STATUS_OK) {
		memset(&fs->inst, 0, sizeof(fs->inst));
		fs->inst.active = true;
		fs->inst.slot = target;
		fs->inst.size = size;
		memcpy(fs->inst.digest, digest, sizeof(fs->inst.digest));
		memcpy(fs->inst.pack_id, pack_id, sizeof(fs->inst.pack_id));
		*slot = target;
		bflash_cache_invalidate(fs->flash);
	}
	(void)k_mutex_unlock(&fs->lock);
	return st;
}

/* Write [flushed, flushed + len) of the slot, erasing 4 KiB sectors ahead. */
static int slot_write(struct fontstore *fs, const uint8_t *data, size_t len)
{
	struct fontstore_install *in = &fs->inst;
	uint32_t base = bflash_slot_offset(fs->flash, in->slot);
	uint32_t end = in->flushed + (uint32_t)len;
	int err = 0;

	while (err == 0 && in->erased_to < end) {
		err = bflash_erase(fs->flash, base + in->erased_to, BFLASH_SECTOR);
		in->erased_to += BFLASH_SECTOR;
	}
	if (err == 0) {
		err = bflash_write(fs->flash, base + in->flushed, data, len);
	}
	if (err == 0) {
		in->flushed = end;
	}
	return err;
}

uint8_t fontstore_data(struct fontstore *fs, uint32_t offset, const uint8_t *data, size_t len)
{
	struct fontstore_install *in = &fs->inst;
	size_t pos = 0u;
	uint8_t st = CTAG_STATUS_OK;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	if (!in->active) {
		st = CTAG_STATUS_NOT_FOUND;
	} else if (offset != in->written || len == 0u || len > in->size - in->written) {
		st = CTAG_STATUS_INVALID;
	}
	while (st == CTAG_STATUS_OK && pos < len) {
		uint32_t pending = in->written - in->flushed; /* bytes held in carry */
		size_t n;

		if (pending > 0u || len - pos < BFLASH_WRITE_UNIT) {
			n = MIN(len - pos, (size_t)(BFLASH_WRITE_UNIT - pending));
			memcpy(&in->carry[pending], &data[pos], n);
			if (pending + n == BFLASH_WRITE_UNIT &&
			    slot_write(fs, in->carry, BFLASH_WRITE_UNIT) != 0) {
				st = CTAG_STATUS_STORAGE_ERROR;
			}
		} else {
			n = ROUND_DOWN(len - pos, BFLASH_WRITE_UNIT);
			if (slot_write(fs, &data[pos], n) != 0) {
				st = CTAG_STATUS_STORAGE_ERROR;
			}
		}
		pos += n;
		in->written += (uint32_t)n;
	}
	if (st == CTAG_STATUS_STORAGE_ERROR) {
		in->active = false;
	}
	(void)k_mutex_unlock(&fs->lock);
	return st;
}

void fontstore_abort(struct fontstore *fs)
{
	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	fs->inst.active = false;
	(void)k_mutex_unlock(&fs->lock);
}

static uint8_t slot_sha256(struct fontstore *fs, uint32_t base, uint32_t size,
			   const struct ctag_sha256_ops *sha, uint8_t *buf, size_t bufsize,
			   uint8_t digest[32])
{
	uint32_t off = 0u;

	if (sha->init(sha->ctx) != 0) {
		return CTAG_STATUS_INTERNAL;
	}
	while (off < size) {
		size_t n = MIN(bufsize, (size_t)(size - off));

		if (bflash_read(fs->flash, base + off, buf, n) != 0) {
			(void)sha->finish(sha->ctx, digest);
			return CTAG_STATUS_STORAGE_ERROR;
		}
		if (sha->update(sha->ctx, buf, n) != 0) {
			return CTAG_STATUS_INTERNAL;
		}
		off += (uint32_t)n;
	}
	return sha->finish(sha->ctx, digest) == 0 ? CTAG_STATUS_OK : CTAG_STATUS_INTERNAL;
}

uint8_t fontstore_commit(struct fontstore *fs, const struct ctag_sha256_ops *sha, uint8_t *scratch,
			 size_t scratch_size, uint8_t pack_id[CTAG_FONTPACK_ID_LEN])
{
	struct fontstore_install in;
	struct fontstore_reader rd;
	struct ctag_fontpack fp;
	uint8_t digest[32];
	uint8_t st;

	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	in = fs->inst;
	fs->inst.active = false; /* the install ends here, whatever the outcome */
	(void)k_mutex_unlock(&fs->lock);

	if (!in.active) {
		return CTAG_STATUS_NOT_FOUND;
	}
	if (in.written != in.size) {
		return CTAG_STATUS_INCOMPLETE;
	}
	if (in.flushed < in.written) {
		/* The last partial unit, padded with the erased value. */
		memset(&in.carry[in.written - in.flushed], 0xFF,
		       BFLASH_WRITE_UNIT - (in.written - in.flushed));
		fs->inst = in;
		if (slot_write(fs, fs->inst.carry, BFLASH_WRITE_UNIT) != 0) {
			fs->inst.active = false;
			return CTAG_STATUS_STORAGE_ERROR;
		}
		fs->inst.active = false;
	}
	rd.flash = fs->flash;
	rd.base = bflash_slot_offset(fs->flash, in.slot);
	rd.limit = in.size;
	rd.cached = false;
	st = slot_sha256(fs, rd.base, in.size, sha, scratch, scratch_size, digest);
	if (st == CTAG_STATUS_OK && memcmp(digest, in.digest, sizeof(digest)) != 0) {
		st = CTAG_STATUS_DIGEST_MISMATCH;
	}
	if (st == CTAG_STATUS_OK) {
		st = ctag_fontpack_open(&fp, reader_read, &rd, in.size);
	}
	if (st == CTAG_STATUS_OK && fp.total != in.size) {
		st = CTAG_STATUS_INVALID;
	}
	if (st == CTAG_STATUS_OK) {
		st = ctag_fontpack_verify_content(&fp, sha, scratch, scratch_size);
	}
	if (st == CTAG_STATUS_OK && memcmp(fp.pack_id, in.pack_id, CTAG_FONTPACK_ID_LEN) != 0) {
		st = CTAG_STATUS_INVALID;
	}
	if (st != CTAG_STATUS_OK) {
		return st;
	}
	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	if (bflash_dir_activate(fs->flash, in.slot, fp.pack_id, in.size, fp.content_hash) != 0) {
		st = CTAG_STATUS_STORAGE_ERROR;
	} else {
		fs->installs++;
	}
	bflash_cache_invalidate(fs->flash);
	load_active(fs);
	if (st == CTAG_STATUS_OK && !fs->valid) {
		st = CTAG_STATUS_STORAGE_ERROR; /* the flip happened but the pack does not open */
	}
	(void)k_mutex_unlock(&fs->lock);
	memcpy(pack_id, fp.pack_id, CTAG_FONTPACK_ID_LEN);
	return st;
}

/* ---- FLASH_TEST ---- */

static bool test_busy(const struct fontstore *fs, uint32_t off)
{
	const struct bflash_geom *g = &fs->flash->geom;
	uint8_t slot;

	if (fs->inst.active || (off >= g->dir_off && off < g->pending_end)) {
		return true;
	}
	for (slot = 0u; slot < 2u && g->slot_size > 0u; slot++) {
		uint32_t base = (uint32_t)slot * g->slot_size;

		if (off >= base && off < base + g->slot_size &&
		    ((fs->has_record && fs->active.slot == slot) || fs->views[slot] > 0u)) {
			return true;
		}
	}
	return false;
}

static uint8_t test_sector(struct fontstore *fs, uint32_t off)
{
	uint8_t pat[256] __aligned(4);
	uint8_t back[256] __aligned(4);
	uint8_t st = CTAG_STATUS_OK;
	uint32_t pos;
	size_t i;

	if (bflash_erase(fs->flash, off, BFLASH_SECTOR) != 0) {
		return CTAG_STATUS_STORAGE_ERROR;
	}
	for (pos = 0u; pos < BFLASH_SECTOR && st == CTAG_STATUS_OK; pos += sizeof(pat)) {
		for (i = 0u; i < sizeof(pat); i++) {
			pat[i] = (uint8_t)(((pos + i) * 37u + (off >> 12)) & 0xFFu);
		}
		if (bflash_write(fs->flash, off + pos, pat, sizeof(pat)) != 0) {
			st = CTAG_STATUS_STORAGE_ERROR;
		}
	}
	for (pos = 0u; pos < BFLASH_SECTOR && st == CTAG_STATUS_OK; pos += sizeof(pat)) {
		for (i = 0u; i < sizeof(pat); i++) {
			pat[i] = (uint8_t)(((pos + i) * 37u + (off >> 12)) & 0xFFu);
		}
		if (bflash_read(fs->flash, off + pos, back, sizeof(back)) != 0 ||
		    memcmp(pat, back, sizeof(pat)) != 0) {
			st = CTAG_STATUS_STORAGE_ERROR;
		}
	}
	if (bflash_erase(fs->flash, off, BFLASH_SECTOR) != 0) {
		st = CTAG_STATUS_STORAGE_ERROR;
	}
	return st;
}

size_t fontstore_flash_test(struct fontstore *fs, struct fontstore_test_item *items, size_t max)
{
	const struct bflash_geom *g = &fs->flash->geom;
	uint32_t cand[FONTSTORE_TEST_MAX];
	size_t n = 0u, out = 0u, i, j;

	if (g->flash_size < BFLASH_SECTOR) {
		return 0u;
	}
	(void)k_mutex_lock(&fs->lock, K_FOREVER);
	if (g->slot_size > 0u) {
		uint8_t inactive = fs->has_record ? (uint8_t)(1u - fs->active.slot) : 0u;
		uint32_t base = (uint32_t)inactive * g->slot_size;

		cand[n++] = base;
		cand[n++] = base + g->slot_size - BFLASH_SECTOR;
	}
	cand[n++] = MIB16 - BFLASH_SECTOR;
	cand[n++] = MIB16;
	cand[n++] = g->flash_size - BFLASH_SECTOR;
	/* Ascending, unique, inside the device. */
	for (i = 1u; i < n; i++) {
		for (j = i; j > 0u && cand[j - 1u] > cand[j]; j--) {
			uint32_t t = cand[j];

			cand[j] = cand[j - 1u];
			cand[j - 1u] = t;
		}
	}
	for (i = 0u; i < n && out < max; i++) {
		if (cand[i] >= g->flash_size || (out > 0u && items[out - 1u].offset == cand[i])) {
			continue;
		}
		items[out].offset = cand[i];
		items[out].status = test_busy(fs, cand[i]) ? CTAG_STATUS_BUSY : test_sector(fs, cand[i]);
		out++;
	}
	bflash_cache_invalidate(fs->flash);
	(void)k_mutex_unlock(&fs->lock);
	return out;
}
