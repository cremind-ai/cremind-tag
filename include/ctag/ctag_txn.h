/*
 * Tag display transaction (docs/protocol.md 5.6 and 6): the FRAME_BEGIN
 * decision table, the boot recovery rule and the persisted display record,
 * stored through a callback (NVS on the tag). Mirrors protocol/tag_txn.py.
 */
#ifndef CTAG_TXN_H_
#define CTAG_TXN_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Persisted state of the display record. */
enum ctag_txn_state {
	CTAG_TXN_DISPLAYED = 0,
	CTAG_TXN_REFRESH_INTENT = 1,
};

struct ctag_txn_record {
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint64_t update_id;
	uint8_t digest[CTAG_FRAME_DIGEST_LEN];
	uint8_t status; /* enum ctag_status */
	uint8_t state;  /* enum ctag_txn_state */
};

/* ctag_txn_frame_begin(): no immediate RESULT, call begin_frame(). */
#define CTAG_TXN_ACCEPT 0xFFu

/*
 * Apply the FRAME_BEGIN table of 5.6 (first matching row wins); stored is NULL
 * when the tag has no record, epoch is the session's. Returns CTAG_TXN_ACCEPT
 * or the RESULT status to answer now; CTAG_STATUS_OK means "re-send the stored
 * RESULT OK with flags.bit0 (duplicate)".
 */
uint8_t ctag_txn_frame_begin(const struct ctag_txn_record *stored, uint32_t epoch,
			     const struct ctag_rec_frame_begin *fb, uint8_t panel_planes,
			     uint16_t panel_plane_len);

/*
 * Boot rule of 6: a record in REFRESH_INTENT gets status DISPLAY_STATE_UNKNOWN.
 * Returns true when the record changed and must be persisted.
 */
bool ctag_txn_boot(struct ctag_txn_record *rec);

/* CHALLENGE flags.bit0: set while the stored state is REFRESH_INTENT. */
static inline bool ctag_txn_unknown_pending(const struct ctag_txn_record *rec)
{
	return rec != NULL && rec->state == CTAG_TXN_REFRESH_INTENT;
}

/* The record to persist before commit_refresh(). */
void ctag_txn_intent(struct ctag_txn_record *rec, uint32_t tag_id, uint32_t epoch,
		     uint32_t revision, uint64_t update_id,
		     const uint8_t digest[CTAG_FRAME_DIGEST_LEN]);

/*
 * After wait_refresh_complete(): OK makes the record DISPLAYED; a failure
 * (REFRESH_TIMEOUT, PANEL_ERROR) stays in REFRESH_INTENT with that status.
 */
void ctag_txn_complete(struct ctag_txn_record *rec, uint8_t status);

/* RESULT plaintext for the record; battery_mv and refresh_ms are the caller's. */
void ctag_txn_result(const struct ctag_txn_record *rec, uint8_t flags, struct ctag_rec_result *res);

/*
 * Persisted layout (little-endian): version u8 (1), state u8, status u8,
 * reserved u8, tag_id u32, epoch u32, revision u32, update_id u64,
 * digest[32], crc32 u32 over the previous 56 bytes.
 */
#define CTAG_TXN_RECORD_LEN     60u
#define CTAG_TXN_RECORD_VERSION 1u

void ctag_txn_record_encode(const struct ctag_txn_record *rec, uint8_t out[CTAG_TXN_RECORD_LEN]);

/* 0, or -EBADMSG for a wrong length, version, state or CRC. */
int ctag_txn_record_decode(struct ctag_txn_record *rec, const uint8_t *in, size_t len);

/* Storage for the single record (e.g. one NVS id). */
struct ctag_txn_store {
	/* Bytes read (>= 0), -ENOENT when nothing is stored, or a negative error. */
	int (*read)(void *ctx, void *buf, size_t len);
	/* Atomic write of the whole record; >= 0 on success. */
	int (*write)(void *ctx, const void *buf, size_t len);
	void *ctx;
};

/* 1 = loaded, 0 = no record, -EIO = storage error, -EBADMSG = corrupt record. */
int ctag_txn_load(const struct ctag_txn_store *store, struct ctag_txn_record *rec);

/* 0 or -EIO. */
int ctag_txn_save(const struct ctag_txn_store *store, const struct ctag_txn_record *rec);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_TXN_H_ */
