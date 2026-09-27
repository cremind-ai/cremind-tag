/* Bridge external flash: geometry, slot directory, pending layouts, read cache. */
#include <errno.h>
#include <string.h>

#include <zephyr/drivers/flash.h>
#include <zephyr/sys/util.h>

#include <ctag/ctag_crc32.h>
#include <ctag/proto_msgs.h>

#include "bflash.h"

#define LINE      CONFIG_CTAG_BRIDGE_READ_CACHE_LINE
#define LINES     CONFIG_CTAG_BRIDGE_READ_CACHE_LINES
#define NO_LINE   UINT32_MAX
#define CONSUMED  (BFLASH_PENDING_SLOT - 4u)

#define CHUNK     CTAG_LAYOUT_CHUNK_DATA_MAX

BUILD_ASSERT((LINE & (LINE - 1)) == 0 && LINE >= 32, "cache line: a power of two >= 32");
BUILD_ASSERT(BFLASH_PENDING_HDR + BFLASH_PENDING_CHUNKS * BFLASH_PENDING_STRIDE <= CONSUMED,
	     "pending slot too small");
BUILD_ASSERT(BFLASH_PENDING_CHUNKS * CHUNK >= CTAG_LAYOUT_HARD_MAX, "chunks cannot hold a layout");

int bflash_geom_compute(uint64_t flash_size, uint32_t working_space, struct bflash_geom *g)
{
	uint32_t need = CTAG_FONTPACK_DIR_SIZE + BFLASH_PENDING_MIN * BFLASH_PENDING_SLOT +
			BFLASH_SPARE;
	uint32_t slots;

	memset(g, 0, sizeof(*g));
	if (flash_size == 0u || flash_size > UINT32_MAX - BFLASH_SECTOR + 1u ||
	    flash_size % BFLASH_SECTOR != 0u) {
		return -EINVAL;
	}
	g->flash_size = (uint32_t)flash_size;
	g->working_space = working_space;
	if (working_space < need || working_space > g->flash_size) {
		return -ENOSPC;
	}
	g->slot_size = ROUND_DOWN((g->flash_size - working_space) / 2u, BFLASH_BLOCK);
	g->dir_off = 2u * g->slot_size;
	g->pending_off = g->dir_off + CTAG_FONTPACK_DIR_SIZE;
	slots = (g->flash_size - BFLASH_SPARE - g->pending_off) / BFLASH_PENDING_SLOT;
	g->pending_slots = (uint16_t)MIN(slots, BFLASH_PENDING_MAX);
	g->pending_end = g->pending_off + (uint32_t)g->pending_slots * BFLASH_PENDING_SLOT;
	return 0;
}

int bflash_init(struct bflash *f, const struct device *dev, uint32_t working_space)
{
	uint64_t size = 0u;
	int err;

	memset(f, 0, sizeof(*f));
	f->dev = dev;
	k_mutex_init(&f->lock);
	bflash_cache_invalidate(f);
	if (!device_is_ready(dev)) {
		return -ENODEV;
	}
	err = flash_get_size(dev, &size);
	if (err != 0) {
		return err;
	}
	return bflash_geom_compute(size, working_space, &f->geom);
}

static bool in_range(const struct bflash *f, uint32_t off, size_t len)
{
	return off <= f->geom.flash_size && len <= f->geom.flash_size - off;
}

int bflash_read(struct bflash *f, uint32_t off, void *buf, size_t len)
{
	int err;

	if (!in_range(f, off, len)) {
		return -EINVAL;
	}
	err = len == 0u ? 0 : flash_read(f->dev, (off_t)off, buf, len);
	if (err != 0) {
		f->errors++;
	}
	return err;
}

int bflash_write(struct bflash *f, uint32_t off, const void *buf, size_t len)
{
	int err;

	if (!in_range(f, off, len) || off % BFLASH_WRITE_UNIT != 0u ||
	    len % BFLASH_WRITE_UNIT != 0u) {
		return -EINVAL;
	}
	err = len == 0u ? 0 : flash_write(f->dev, (off_t)off, buf, len);
	if (err != 0) {
		f->errors++;
	}
	return err;
}

int bflash_erase(struct bflash *f, uint32_t off, size_t len)
{
	int err;

	if (!in_range(f, off, len) || off % BFLASH_SECTOR != 0u || len % BFLASH_SECTOR != 0u) {
		return -EINVAL;
	}
	err = flash_erase(f->dev, (off_t)off, len);
	if (err != 0) {
		f->errors++;
	}
	return err;
}

void bflash_cache_invalidate(struct bflash *f)
{
	size_t i;

	if (f->dev != NULL) {
		(void)k_mutex_lock(&f->lock, K_FOREVER);
	}
	for (i = 0; i < LINES; i++) {
		f->line[i].addr = NO_LINE;
		f->line[i].stamp = 0u;
	}
	if (f->dev != NULL) {
		(void)k_mutex_unlock(&f->lock);
	}
}

/* The cache line holding addr (loaded on a miss); NULL on a read error. */
static const uint8_t *line_for(struct bflash *f, uint32_t addr)
{
	size_t i, victim = 0u;

	for (i = 0; i < LINES; i++) {
		if (f->line[i].addr == addr) {
			f->line[i].stamp = ++f->stamp;
			f->hits++;
			return f->data[i];
		}
		if (f->line[i].stamp < f->line[victim].stamp) {
			victim = i;
		}
	}
	f->misses++;
	f->line[victim].addr = NO_LINE;
	if (bflash_read(f, addr, f->data[victim], MIN((size_t)LINE, f->geom.flash_size - addr)) !=
	    0) {
		return NULL;
	}
	f->line[victim].addr = addr;
	f->line[victim].stamp = ++f->stamp;
	return f->data[victim];
}

int bflash_cached_read(struct bflash *f, uint32_t off, void *buf, size_t len)
{
	uint8_t *out = buf;
	int err = 0;

	if (!in_range(f, off, len)) {
		return -EINVAL;
	}
	if (len > LINE) {
		return bflash_read(f, off, buf, len);
	}
	(void)k_mutex_lock(&f->lock, K_FOREVER);
	while (len > 0u) {
		uint32_t base = ROUND_DOWN(off, LINE);
		size_t n = MIN(len, (size_t)(base + LINE - off));
		const uint8_t *line = line_for(f, base);

		if (line == NULL) {
			err = -EIO;
			break;
		}
		memcpy(out, &line[off - base], n);
		out += n;
		off += (uint32_t)n;
		len -= n;
	}
	(void)k_mutex_unlock(&f->lock);
	return err;
}

/* ---- Slot directory ---- */

void bflash_dir_encode(const struct bflash_dir_record *r, uint8_t out[BFLASH_DIR_LEN])
{
	memset(out, 0, BFLASH_DIR_LEN);
	ctag_put_le32(&out[0], CTAG_FONTPACK_SLOT_DIR_MAGIC);
	ctag_put_le16(&out[4], BFLASH_DIR_VERSION);
	ctag_put_le32(&out[8], r->seq);
	out[12] = r->slot;
	memcpy(&out[16], r->pack_id, CTAG_FONTPACK_ID_LEN);
	ctag_put_le32(&out[24], r->size);
	memcpy(&out[28], r->content_hash, 32);
	ctag_put_le32(&out[60], ctag_crc32(0u, out, 60u));
}

bool bflash_dir_decode(const uint8_t in[BFLASH_DIR_LEN], uint32_t slot_size,
		       struct bflash_dir_record *r)
{
	if (ctag_get_le32(&in[0]) != CTAG_FONTPACK_SLOT_DIR_MAGIC ||
	    ctag_get_le16(&in[4]) != BFLASH_DIR_VERSION ||
	    ctag_get_le32(&in[60]) != ctag_crc32(0u, in, 60u) || in[12] > 1u) {
		return false;
	}
	r->seq = ctag_get_le32(&in[8]);
	r->slot = in[12];
	memcpy(r->pack_id, &in[16], CTAG_FONTPACK_ID_LEN);
	r->size = ctag_get_le32(&in[24]);
	memcpy(r->content_hash, &in[28], 32);
	return r->size > 0u && r->size <= slot_size;
}

int bflash_dir_active(struct bflash *f, struct bflash_dir_record *r, uint8_t *which)
{
	struct bflash_dir_record rec[2];
	bool ok[2];
	uint8_t buf[BFLASH_DIR_LEN] __aligned(4);
	uint8_t i;

	if (f->geom.slot_size == 0u) {
		return -ENOENT;
	}
	for (i = 0u; i < 2u; i++) {
		int err = bflash_read(f, f->geom.dir_off + i * BFLASH_SECTOR, buf, sizeof(buf));

		if (err != 0) {
			return err;
		}
		ok[i] = bflash_dir_decode(buf, f->geom.slot_size, &rec[i]);
	}
	if (!ok[0] && !ok[1]) {
		return -ENOENT;
	}
	i = (!ok[0] || (ok[1] && rec[1].seq > rec[0].seq)) ? 1u : 0u;
	*r = rec[i];
	if (which != NULL) {
		*which = i;
	}
	return 0;
}

int bflash_dir_activate(struct bflash *f, uint8_t slot, const uint8_t pack_id[8], uint32_t size,
			const uint8_t content_hash[32])
{
	struct bflash_dir_record cur, next = {.slot = slot, .size = size};
	uint8_t buf[BFLASH_DIR_LEN] __aligned(4);
	uint8_t which;
	uint32_t target;
	int err = bflash_dir_active(f, &cur, &which);

	if (err == -ENOENT) {
		next.seq = 1u;
		target = f->geom.dir_off;
	} else if (err == 0) {
		next.seq = cur.seq + 1u;
		target = f->geom.dir_off + (which == 0u ? BFLASH_SECTOR : 0u);
	} else {
		return err;
	}
	memcpy(next.pack_id, pack_id, sizeof(next.pack_id));
	memcpy(next.content_hash, content_hash, sizeof(next.content_hash));
	bflash_dir_encode(&next, buf);
	err = bflash_erase(f, target, BFLASH_SECTOR);
	return err != 0 ? err : bflash_write(f, target, buf, sizeof(buf));
}

/* ---- Pending layouts ---- */

static void pending_encode(const struct bflash_pending *h, uint8_t out[BFLASH_PENDING_HDR])
{
	memset(out, 0, BFLASH_PENDING_HDR);
	ctag_put_le32(&out[0], BFLASH_PENDING_MAGIC);
	out[4] = BFLASH_PENDING_VERSION;
	ctag_put_le16(&out[6], BFLASH_PENDING_HDR);
	ctag_put_le32(&out[8], h->seq);
	ctag_put_le32(&out[12], h->tag_id);
	ctag_put_le32(&out[16], h->epoch);
	ctag_put_le32(&out[20], h->revision);
	ctag_put_le64(&out[24], h->update_id);
	memcpy(&out[32], h->fontpack_id, CTAG_FONTPACK_ID_LEN);
	memcpy(&out[40], h->digest, CTAG_LAYOUT_DIGEST_LEN);
	ctag_put_le16(&out[56], h->len);
	ctag_put_le16(&out[58], h->xfer_id);
}

static bool pending_decode(const uint8_t in[BFLASH_PENDING_HDR], struct bflash_pending *h)
{
	if (ctag_get_le32(&in[0]) != BFLASH_PENDING_MAGIC || in[4] != BFLASH_PENDING_VERSION ||
	    ctag_get_le16(&in[6]) != BFLASH_PENDING_HDR) {
		return false;
	}
	h->seq = ctag_get_le32(&in[8]);
	h->tag_id = ctag_get_le32(&in[12]);
	h->epoch = ctag_get_le32(&in[16]);
	h->revision = ctag_get_le32(&in[20]);
	h->update_id = ctag_get_le64(&in[24]);
	memcpy(h->fontpack_id, &in[32], CTAG_FONTPACK_ID_LEN);
	memcpy(h->digest, &in[40], CTAG_LAYOUT_DIGEST_LEN);
	h->len = ctag_get_le16(&in[56]);
	h->xfer_id = ctag_get_le16(&in[58]);
	return h->len > 0u && h->len <= CTAG_LAYOUT_HARD_MAX;
}

static uint32_t chunk_offset(const struct bflash *f, unsigned int index, unsigned int chunk)
{
	return bflash_pending_offset(f, index) + BFLASH_PENDING_HDR + chunk * BFLASH_PENDING_STRIDE;
}

int bflash_pending_erase(struct bflash *f, unsigned int index)
{
	if (index >= f->geom.pending_slots) {
		return -EINVAL;
	}
	return bflash_erase(f, bflash_pending_offset(f, index), BFLASH_PENDING_SLOT);
}

int bflash_pending_put(struct bflash *f, unsigned int index, unsigned int chunk,
		       const uint8_t *data, size_t len)
{
	uint8_t buf[BFLASH_PENDING_STRIDE] __aligned(4);

	if (index >= f->geom.pending_slots || chunk >= BFLASH_PENDING_CHUNKS || len == 0u ||
	    len > CHUNK) {
		return -EINVAL;
	}
	memcpy(buf, data, len);
	memset(&buf[len], 0xFF, sizeof(buf) - len);
	return bflash_write(f, chunk_offset(f, index, chunk), buf,
			    ROUND_UP(len, BFLASH_WRITE_UNIT));
}

int bflash_pending_body(struct bflash *f, unsigned int index, uint8_t *buf, size_t len)
{
	unsigned int i;
	int err = 0;

	if (index >= f->geom.pending_slots || len > CTAG_LAYOUT_HARD_MAX) {
		return -EINVAL;
	}
	for (i = 0u; err == 0 && (size_t)i * CHUNK < len; i++) {
		err = bflash_read(f, chunk_offset(f, index, i), &buf[i * CHUNK],
				  MIN((size_t)CHUNK, len - (size_t)i * CHUNK));
	}
	return err;
}

int bflash_pending_seal(struct bflash *f, unsigned int index, const struct bflash_pending *h,
			const uint8_t *layout)
{
	uint8_t hdr[BFLASH_PENDING_HDR] __aligned(4);

	if (index >= f->geom.pending_slots || h->len == 0u || h->len > CTAG_LAYOUT_HARD_MAX) {
		return -EINVAL;
	}
	pending_encode(h, hdr);
	ctag_put_le32(&hdr[60], ctag_crc32(ctag_crc32(0u, hdr, 60u), layout, h->len));
	return bflash_write(f, bflash_pending_offset(f, index), hdr, sizeof(hdr));
}

int bflash_pending_write(struct bflash *f, unsigned int index, const struct bflash_pending *h,
			 const uint8_t *layout)
{
	unsigned int i;
	int err;

	if (index >= f->geom.pending_slots || h->len == 0u || h->len > CTAG_LAYOUT_HARD_MAX) {
		return -EINVAL;
	}
	err = bflash_pending_erase(f, index);
	for (i = 0u; err == 0 && (size_t)i * CHUNK < h->len; i++) {
		err = bflash_pending_put(f, index, i, &layout[i * CHUNK],
					 MIN((size_t)CHUNK, (size_t)h->len - i * CHUNK));
	}
	/* The header goes last: a reset before this point leaves no valid record. */
	return err != 0 ? err : bflash_pending_seal(f, index, h, layout);
}

static int pending_header(struct bflash *f, unsigned int index, uint8_t hdr[BFLASH_PENDING_HDR],
			  struct bflash_pending *h)
{
	uint32_t off = bflash_pending_offset(f, index);
	uint8_t mark[4] __aligned(4);
	int err;

	if (index >= f->geom.pending_slots) {
		return -EINVAL;
	}
	err = bflash_read(f, off, hdr, BFLASH_PENDING_HDR);
	if (err == 0) {
		err = bflash_read(f, off + CONSUMED, mark, sizeof(mark));
	}
	if (err != 0) {
		return err;
	}
	if (!pending_decode(hdr, h)) {
		return BFLASH_PENDING_EMPTY;
	}
	return ctag_get_le32(mark) == UINT32_MAX ? BFLASH_PENDING_LIVE : BFLASH_PENDING_CONSUMED;
}

int bflash_pending_peek(struct bflash *f, unsigned int index, struct bflash_pending *h)
{
	uint8_t hdr[BFLASH_PENDING_HDR] __aligned(4);

	return pending_header(f, index, hdr, h);
}

int bflash_pending_check(struct bflash *f, unsigned int index, struct bflash_pending *h,
			 uint8_t *buf, size_t size)
{
	uint8_t hdr[BFLASH_PENDING_HDR] __aligned(4);
	int st, err;

	if (buf == NULL || size < CTAG_LAYOUT_HARD_MAX) {
		return -EINVAL;
	}
	st = pending_header(f, index, hdr, h);
	if (st <= 0) {
		return st;
	}
	err = bflash_pending_body(f, index, buf, h->len);
	if (err != 0) {
		return err;
	}
	return ctag_crc32(ctag_crc32(0u, hdr, 60u), buf, h->len) == ctag_get_le32(&hdr[60])
		       ? st
		       : BFLASH_PENDING_EMPTY;
}

int bflash_pending_read(struct bflash *f, unsigned int index, struct bflash_pending *h,
			uint8_t *buf, size_t size)
{
	int st = bflash_pending_check(f, index, h, buf, size);

	return st < 0 ? st : (st == BFLASH_PENDING_LIVE ? 1 : 0);
}

int bflash_pending_consume(struct bflash *f, unsigned int index)
{
	static const uint8_t zero[4] __aligned(4);
	uint32_t off = bflash_pending_offset(f, index);
	uint8_t mark[4] __aligned(4);
	int err;

	if (index >= f->geom.pending_slots) {
		return -EINVAL;
	}
	err = bflash_read(f, off + CONSUMED, mark, sizeof(mark));
	if (err != 0 || ctag_get_le32(mark) != UINT32_MAX) {
		return err; /* already consumed: NOR bits cannot be cleared twice */
	}
	return bflash_write(f, off + CONSUMED, zero, sizeof(zero));
}
