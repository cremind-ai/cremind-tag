/*
 * Bridge external flash (docs/fontpack.md 3, docs/bridge-firmware.md "Flash
 * map"): geometry, the A/B slot directory, pending-layout records and the
 * renderer's read cache. Works on any Zephyr flash device (QSPI/SPI NOR on the
 * boards, the flash simulator in tests).
 *
 *   0            slot_size      2*slot_size                                   flash_size
 *   | slot 0     | slot 1       | dir A | dir B | .. | pending-layout ring | spare |
 *                               |<- FONTPACK_DIR_SIZE ->|                  |<-64K->|
 *
 * Pending layouts are 8 KiB records (a LAYOUT_HARD_MAX layout plus its header
 * does not fit one 4 KiB sector) written round-robin over the ring, so the
 * erase wear of frequent deliveries spreads over the whole working space
 * instead of one sector per tag.
 */
#ifndef BRIDGE_BFLASH_H_
#define BRIDGE_BFLASH_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/kernel.h>

#include <ctag/proto_ids.h>

#define BFLASH_SECTOR     4096u
#define BFLASH_BLOCK      65536u
#define BFLASH_DIR_LEN    64u
#define BFLASH_DIR_VERSION 1u

/* One pending-layout record: a 64-byte header, the layout (<= LAYOUT_HARD_MAX)
 * and a consumed marker in the slot's last word. */
#define BFLASH_PENDING_SLOT     (2u * BFLASH_SECTOR)
#define BFLASH_PENDING_HDR      64u
#define BFLASH_PENDING_MAGIC    0x4C505443u /* bytes 'C','T','P','L' */
#define BFLASH_PENDING_VERSION  1u
/* At most two live records per tag (one in a session, one waiting) plus room
 * to write the next one: the smallest ring that always has a free slot. */
#define BFLASH_PENDING_MIN      (2u * CTAG_MAX_TAGS_PER_BRIDGE + 2u)
#define BFLASH_PENDING_MAX      4096u
/* The device's last 64 KiB stay free (FLASH_TEST's last sector, margin). */
#define BFLASH_SPARE            BFLASH_BLOCK

/* All writes are made in 4-byte units at 4-byte offsets (QSPI requirement). */
#define BFLASH_WRITE_UNIT 4u

struct bflash_geom {
	uint32_t flash_size;
	uint32_t working_space; /* configured minimum */
	uint32_t slot_size;     /* 0 = the part is too small for two pack slots */
	uint32_t dir_off;       /* = 2 * slot_size: start of the working space */
	uint32_t pending_off;   /* dir_off + FONTPACK_DIR_SIZE */
	uint32_t pending_end;   /* pending_off + pending_slots * BFLASH_PENDING_SLOT */
	uint16_t pending_slots; /* ring size */
};

/*
 * slot_size = align_down_64K((flash_size - working_space) / 2); the working
 * space starts at 2 * slot_size and holds the directory area, the ring (up to
 * BFLASH_PENDING_MAX records) and the spare block. Returns 0, -EINVAL
 * (flash_size 0, above 4 GiB - 4 KiB or not sector aligned) or -ENOSPC (the
 * working space cannot hold BFLASH_PENDING_MIN records).
 */
int bflash_geom_compute(uint64_t flash_size, uint32_t working_space, struct bflash_geom *g);

struct bflash_line {
	uint32_t addr; /* UINT32_MAX = empty */
	uint32_t stamp;
};

struct bflash {
	const struct device *dev;
	struct bflash_geom geom;
	struct k_mutex lock; /* the read cache */
	struct bflash_line line[CONFIG_CTAG_BRIDGE_READ_CACHE_LINES];
	uint8_t data[CONFIG_CTAG_BRIDGE_READ_CACHE_LINES][CONFIG_CTAG_BRIDGE_READ_CACHE_LINE]
		__aligned(4);
	uint32_t stamp;
	uint32_t hits;
	uint32_t misses;
	uint32_t errors;
};

int bflash_init(struct bflash *f, const struct device *dev, uint32_t working_space);

/* Raw access; offsets are device offsets. Writes: off and len multiples of 4. */
int bflash_read(struct bflash *f, uint32_t off, void *buf, size_t len);
int bflash_write(struct bflash *f, uint32_t off, const void *buf, size_t len);
int bflash_erase(struct bflash *f, uint32_t off, size_t len);

/* Cached read for the renderer (small reads; larger ones bypass the cache). */
int bflash_cached_read(struct bflash *f, uint32_t off, void *buf, size_t len);
void bflash_cache_invalidate(struct bflash *f);

/* ---- Slot directory (docs/fontpack.md 3) ---- */

struct bflash_dir_record {
	uint32_t seq;
	uint8_t slot;
	uint8_t pack_id[CTAG_FONTPACK_ID_LEN];
	uint32_t size;
	uint8_t content_hash[32];
};

void bflash_dir_encode(const struct bflash_dir_record *r, uint8_t out[BFLASH_DIR_LEN]);
/* Valid record: magic, version, CRC-32, slot <= 1, size <= slot_size. */
bool bflash_dir_decode(const uint8_t in[BFLASH_DIR_LEN], uint32_t slot_size,
		       struct bflash_dir_record *r);

/* The valid record with the highest seq (*which = 0 for sector A, 1 for B); -ENOENT. */
int bflash_dir_active(struct bflash *f, struct bflash_dir_record *r, uint8_t *which);

/*
 * Activate a pack: write the directory sector that does not hold the active
 * record with seq + 1 (erase, then one 64-byte write). A power loss at any
 * moment leaves the previous record valid.
 */
int bflash_dir_activate(struct bflash *f, uint8_t slot, const uint8_t pack_id[8], uint32_t size,
			const uint8_t content_hash[32]);

static inline uint32_t bflash_slot_offset(const struct bflash *f, uint8_t slot)
{
	return (uint32_t)slot * f->geom.slot_size;
}

/* ---- Pending layouts ---- */

struct bflash_pending {
	uint32_t seq; /* bridge-wide, increasing: restore order */
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint64_t update_id;
	uint8_t fontpack_id[CTAG_FONTPACK_ID_LEN];
	uint8_t digest[CTAG_LAYOUT_DIGEST_LEN];
	uint16_t len;
};

static inline uint32_t bflash_pending_offset(const struct bflash *f, unsigned int index)
{
	return f->geom.pending_off + index * BFLASH_PENDING_SLOT;
}

/* Erase the slot, write the layout, then the header (CRC over header + layout). */
int bflash_pending_write(struct bflash *f, unsigned int index, const struct bflash_pending *h,
			 const uint8_t *layout);

enum bflash_pending_state {
	BFLASH_PENDING_EMPTY = 0,  /* erased, torn or foreign */
	BFLASH_PENDING_LIVE = 1,
	BFLASH_PENDING_CONSUMED = 2,
};

/* Header only: the slot's state; *h is filled for LIVE and CONSUMED. */
int bflash_pending_peek(struct bflash *f, unsigned int index, struct bflash_pending *h);

/*
 * Read a live record: 1 = live (layout in buf, which holds at least
 * CTAG_LAYOUT_HARD_MAX bytes), 0 = empty, consumed or failing its CRC,
 * negative = read error.
 */
int bflash_pending_read(struct bflash *f, unsigned int index, struct bflash_pending *h,
			uint8_t *buf, size_t size);

/* Mark a record consumed (one write of its last word; no erase). */
int bflash_pending_consume(struct bflash *f, unsigned int index);

#endif /* BRIDGE_BFLASH_H_ */
