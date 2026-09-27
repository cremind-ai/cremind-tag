/*
 * Font store (docs/fontpack.md 3-4): the active pack validated at boot, the
 * maintenance-port installation (FONT_BEGIN/DATA/COMMIT/ABORT) into the
 * inactive slot with an atomic directory flip, FLASH_TEST, and snapshots of
 * the active pack for tag sessions.
 *
 * Threads: installation runs on the maintenance thread, validation and
 * rendering on the bridge work queue; the store's mutex serialises them.
 */
#ifndef BRIDGE_FONTSTORE_H_
#define BRIDGE_FONTSTORE_H_

#include <stdbool.h>
#include <stdint.h>

#include <ctag/ctag_fontpack.h>
#include <ctag/ctag_layout.h>

#include "bflash.h"

/* Reads a pack in one slot (through the cache when cached). */
struct fontstore_reader {
	struct bflash *flash;
	uint32_t base;
	uint32_t limit;
	bool cached;
};

/* A tag session's snapshot of the active pack: it keeps reading its slot even
 * after a directory flip; FONT_BEGIN refuses (BUSY) to overwrite that slot. */
struct fontstore_view {
	struct ctag_fontpack pack;
	struct fontstore_reader rd;
	struct ctag_glyph_source src;
	uint8_t slot;
	bool open;
};

struct fontstore_install {
	bool active;
	uint8_t slot;
	uint32_t size;
	uint32_t written;   /* bytes accepted (offset of the next FONT_DATA) */
	uint32_t flushed;   /* bytes written to flash (4-byte units) */
	uint32_t erased_to; /* slot bytes erased so far */
	uint8_t digest[32];
	uint8_t pack_id[CTAG_FONTPACK_ID_LEN];
	uint8_t carry[BFLASH_WRITE_UNIT] __aligned(4);
};

#define FONTSTORE_TEST_MAX 5u

struct fontstore_test_item {
	uint32_t offset;
	uint8_t status;
};

struct fontstore {
	struct bflash *flash;
	struct k_mutex lock;
	bool usable;     /* the flash and its geometry are usable */
	bool has_record; /* the directory names an active pack */
	bool valid;      /* ... and it passed the boot validation */
	struct bflash_dir_record active;
	struct ctag_fontpack pack;
	struct fontstore_reader rd;
	struct fontstore_install inst;
	uint8_t views[2]; /* open session views per slot */
	uint32_t installs;
	uint8_t last_open_status;
};

/* Read the directory and validate the active pack (fontpack.md 2 "Validation"). */
void fontstore_init(struct fontstore *fs, struct bflash *flash, bool usable);

/* The active pack id; false when there is no valid pack. */
bool fontstore_active_id(struct fontstore *fs, uint8_t id[CTAG_FONTPACK_ID_LEN]);

/* Strike lookup in the active pack (layout validation step 5). */
bool fontstore_has_strike(void *fs, uint16_t face, uint8_t size_px);

/* Snapshot the active pack for a session; false when there is none. */
bool fontstore_view_open(struct fontstore *fs, struct fontstore_view *v);
void fontstore_view_close(struct fontstore *fs, struct fontstore_view *v);

/* ---- Installation (maintenance port); each returns a ctag_status ---- */

uint8_t fontstore_begin(struct fontstore *fs, uint32_t size, const uint8_t digest[32],
			const uint8_t pack_id[CTAG_FONTPACK_ID_LEN], uint8_t *slot);
uint8_t fontstore_data(struct fontstore *fs, uint32_t offset, const uint8_t *data, size_t len);
/* SHA-256 of the slot, format validation incl. the content hash, directory flip. */
uint8_t fontstore_commit(struct fontstore *fs, const struct ctag_sha256_ops *sha, uint8_t *scratch,
			 size_t scratch_size, uint8_t pack_id[CTAG_FONTPACK_ID_LEN]);
void fontstore_abort(struct fontstore *fs);

/*
 * FLASH_TEST (spec serial FLASH_TEST, docs/protocol.md 10): erase / pattern /
 * read-back / erase at the first and last sectors of the inactive slot, the
 * sectors either side of the 16 MiB boundary and the last sector of the
 * device. A position inside the active slot, a slot a session is reading, the
 * directory area or the pending-layout area, or any position while an
 * installation is open, reports BUSY. Returns the item count.
 */
size_t fontstore_flash_test(struct fontstore *fs, struct fontstore_test_item *items, size_t max);

#endif /* BRIDGE_FONTSTORE_H_ */
