/*
 * Bridge delivery core (docs/protocol.md 3, 10; Cremind's app/tags/runtime/sim/bridge.py):
 * assignment table, the one layout being assembled, LAYOUT_COMMIT validation
 * in the order of 3.3, per-tag history (DUPLICATE / STALE_REVISION), jobs
 * (pending layouts persisted in external flash, tag commands), SUPERSEDED /
 * CANCELLED, and DELIVERY_RESULT with a persisted result_seq, re-sent every
 * MESH_RESULT_RETRY_MS up to MESH_RESULT_RETRIES times until RESULT_ACK.
 *
 * Memory: the transfer being assembled lives in its external-flash ring slot
 * (bflash.h), not in RAM. One LAYOUT_HARD_MAX buffer (dlv.layout) is shared:
 * LAYOUT_COMMIT reads the transfer into it to check the digest and validate
 * it, and the tag session renders the current job's layout from it. Both run
 * on the same thread; a commit invalidates the session's copy and the session
 * reloads it from the job's record (dlv_job_layout_held()).
 *
 * One result_seq per update_id (10): a DELIVERY_RESULT for an update_id that
 * already has one re-sends that first result under its result_seq. The
 * results table keeps acknowledged results for this; the history keeps each
 * tag's last result (update_id, result_seq) across resets.
 *
 * Pure logic: no Bluetooth. Time is passed in (ms); outbound mesh messages and
 * persistence go through the dlv_env callbacks. Runs on one thread (the
 * bridge work queue).
 */
#ifndef BRIDGE_DELIVERY_H_
#define BRIDGE_DELIVERY_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_layout.h>
#include <ctag/proto_msgs.h>

#include "bflash.h"
#include "fontstore.h"

#define DLV_NO_SLOT 0xFFFFu

enum dlv_kind {
	DLV_LAYOUT = 1,
	DLV_CMD = 2,
};

#define DLV_JOB_IN_SESSION     0x01u
#define DLV_JOB_FRAME_END_SENT 0x02u

struct dlv_job {
	uint8_t used;
	uint8_t kind;
	uint8_t cmd;
	uint8_t flags;
	uint16_t slot; /* pending-layout ring slot of a layout job */
	uint16_t len;  /* layout bytes */
	uint32_t order;
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint64_t update_id;
	uint32_t validated_ms;
	uint8_t digest[CTAG_LAYOUT_DIGEST_LEN];
	uint8_t fontpack_id[CTAG_FONTPACK_ID_LEN];
};

struct dlv_assignment {
	uint8_t used;
	uint8_t flags; /* bit0 primary bridge */
	/* RAM only (10): the unauthenticated status of the last sessions and how
	 * many consecutive sessions ended with it (this epoch). */
	uint8_t unauth_status;
	uint8_t unauth_count;
	uint32_t tag_id;
	uint32_t epoch;
	uint8_t key[CTAG_TAG_KEY_LEN];
};

/* 10: sessions in a row ending with the same unauthenticated status before
 * it ends the tag's jobs. */
#define DLV_UNAUTH_REPEATS 3u

/*
 * What a tag session reports with a result (DELIVERY_RESULT): the timing, the
 * tag's stored epoch (from its CHALLENGE or ERROR, after AUTH_OK at least the
 * session's epoch; 0 = unknown) and the CTAG_RESULT_FLAG_* flags.
 */
struct dlv_report {
	uint16_t wake_ms;
	uint16_t suspend_ms;
	uint16_t transfer_ms;
	uint16_t refresh_ms;
	uint32_t stored_epoch;
	uint8_t flags;
};

/* Last accepted revision of the assigned tag and its final result (3.3, 10).
 * Ordered for size (72 bytes on 32-bit targets). */
struct dlv_history {
	uint64_t update_id; /* the revision's current update_id */
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	struct dlv_report report;
	uint16_t result_seq; /* of the result reported for update_id */
	uint16_t battery_mv;
	uint8_t valid;
	uint8_t has_result;
	uint8_t status;
	uint8_t digest[CTAG_LAYOUT_DIGEST_LEN];
	uint8_t digest8[8];
};

enum dlv_result_state {
	DLV_RES_FREE = 0,
	DLV_RES_SENDING,
	DLV_RES_ACKED,
	DLV_RES_GAVE_UP, /* MESH_RESULT_RETRIES re-sends without RESULT_ACK */
};

struct dlv_result {
	uint8_t state;
	uint8_t sends;
	uint32_t due_ms;
	struct ctag_mesh_delivery_result msg;
};

/* The transfer being assembled into its ring slot (3.2 rule 4: one at a time). */
struct dlv_xfer {
	struct ctag_mesh_layout_begin begin;
	uint32_t have;     /* bit i: chunk i received (and written) */
	uint16_t last_len; /* bytes in chunk chunk_count - 1 */
	uint16_t slot;     /* its ring slot; DLV_NO_SLOT: none or unusable */
	bool active;       /* chunks and commits for begin.xfer_id are taken */
	bool bad;          /* a chunk other than the last was not full */
	/* Committed OK or DUPLICATE: a repeated commit answers DUPLICATE (10).
	 * Also set at boot for the transfer of the newest ring record. */
	bool accepted;
};

struct dlv_counters {
	uint32_t layouts_accepted;
	uint32_t duplicates;
	uint32_t superseded;
	uint32_t cancelled;
	uint32_t incomplete;
	uint32_t stray_chunks;
	uint32_t results;
	uint32_t result_resends;
	uint32_t results_unacked;
	uint32_t results_dropped;
	uint32_t results_repeated; /* same update_id: the first result again */
	uint32_t storage_errors;
	uint32_t jobs_dropped;
	uint32_t unauth_final; /* unauthenticated statuses that ended jobs */
	uint32_t layout_loads; /* the shared buffer (re)loaded from a record */
};

struct dlv_env {
	/* An unsolicited message to the gateway (DELIVERY_RESULT, DELIVERY_STAGE). */
	void (*send)(void *ctx, uint8_t op, const uint8_t *params, size_t len);
	/* Persist a small record under a relative name ("a/3", "h/3", "rs");
	 * len 0 deletes it. */
	int (*save)(void *ctx, const char *name, const void *data, size_t len);
	void *ctx;
	/* Transfers and pending layouts; without usable flash every commit fails
	 * STORAGE_ERROR at the digest check (no pack is valid either). */
	struct bflash *flash;
	bool flash_ok;
	struct fontstore *fonts;
	const struct ctag_sha256_ops *sha;
};

#define DLV_MAX_JOBS    CONFIG_CTAG_BRIDGE_MAX_JOBS
#define DLV_RESULT_SLOTS CONFIG_CTAG_BRIDGE_RESULT_SLOTS
#define DLV_SEQ_RESERVE 16u
/* Assignment table: MAX_TAGS_PER_BRIDGE, or fewer on a small board (CAPS
 * reports it as max_tags). */
#define DLV_MAX_TAGS    CONFIG_CTAG_BRIDGE_MAX_TAGS

struct dlv {
	struct dlv_env env;
	struct dlv_xfer x;
	struct dlv_assignment asg[DLV_MAX_TAGS];
	struct dlv_history hist[DLV_MAX_TAGS];
	uint16_t battery[DLV_MAX_TAGS];
	struct dlv_job jobs[DLV_MAX_JOBS];
	struct dlv_result results[DLV_RESULT_SLOTS];
	uint32_t order;
	uint32_t pending_seq;
	uint16_t ring_head;   /* next pending-layout slot to try */
	uint16_t next_seq;    /* result_seq of the next DELIVERY_RESULT */
	uint16_t reserve_end; /* persisted: seqs below it may have been used */
	/* The shared layout buffer holds lb_job's layout (NULL: anything else). */
	const struct dlv_job *lb_job;
	struct dlv_counters c;
	uint8_t layout[CTAG_LAYOUT_HARD_MAX] __aligned(4);
};

void dlv_init(struct dlv *d, const struct dlv_env *env);

/* Boot: persisted records (settings), then dlv_start() restores the jobs. */
void dlv_restore(struct dlv *d, const char *name, const void *data, size_t len);
/* Reserve result_seqs, restore pending layouts and the last accepted transfer. */
void dlv_start(struct dlv *d, uint32_t now);
/* Config Node Reset: forget assignments, history, jobs and pending layouts. */
void dlv_reset(struct dlv *d);

/* ---- LAYOUT_SRV ---- */
/* Replaces any transfer; erases the next free ring slot for it. */
void dlv_layout_begin(struct dlv *d, const struct ctag_mesh_layout_begin *b);
/* Writes the chunk into the transfer's slot (a chunk already received is
 * ignored: flash is written once per erase). */
void dlv_layout_chunk(struct dlv *d, const struct ctag_mesh_layout_chunk *c);
/* 3.3 in order; *missing is valid with INCOMPLETE. Returns the LAYOUT_STATUS
 * status; a repeated commit of an accepted transfer answers DUPLICATE (10). */
uint8_t dlv_layout_commit(struct dlv *d, uint16_t xfer_id, uint32_t now, uint32_t *missing);
void dlv_layout_cancel(struct dlv *d, uint64_t update_id, uint32_t now);
void dlv_result_ack(struct dlv *d, uint16_t result_seq);

/* ---- MGMT_SRV ---- */
uint8_t dlv_assign_set(struct dlv *d, const struct ctag_mesh_assign_set *m, uint32_t now);
uint8_t dlv_assign_del(struct dlv *d, const struct ctag_mesh_assign_del *m, uint32_t now);
void dlv_tag_cmd(struct dlv *d, const struct ctag_mesh_tag_cmd *m, uint32_t now);
uint8_t dlv_assigned_count(const struct dlv *d);
uint8_t dlv_queue_depth(const struct dlv *d);

/* Result re-sends; returns ms until the next one is due (UINT32_MAX = none). */
uint32_t dlv_tick(struct dlv *d, uint32_t now);

/* ---- Scheduler and session ---- */
const struct dlv_assignment *dlv_assignment(const struct dlv *d, uint32_t tag_id);
bool dlv_has_work(const struct dlv *d, uint32_t tag_id);
uint32_t dlv_last_order(const struct dlv *d);
/* The tag's next job with after < order <= upto, in arrival order. */
struct dlv_job *dlv_next_job(struct dlv *d, uint32_t tag_id, uint32_t after, uint32_t upto);
/*
 * A layout job's bytes in the shared buffer, loaded from its pending record
 * (CRC and identity checked) unless the buffer already holds them. Returns
 * the length (*layout = the buffer) or a negative error.
 */
int dlv_job_layout(struct dlv *d, const struct dlv_job *job, const uint8_t **layout);
/* Whether the shared buffer still holds the job's layout (a commit borrows it). */
bool dlv_job_layout_held(const struct dlv *d, const struct dlv_job *job);
void dlv_stage(struct dlv *d, const struct dlv_job *job, uint8_t stage);
/* Final result of a job (removes it, persists history, sends DELIVERY_RESULT);
 * report NULL: nothing from a tag session (zero timing, epoch and flags). */
void dlv_finish(struct dlv *d, struct dlv_job *job, uint8_t status, const uint8_t digest8[8],
		uint16_t battery_mv, const struct dlv_report *report, uint32_t now);
/* End the tag's jobs of that epoch with status (see dlv_unauth_status()),
 * reporting the tag's stored epoch and CTAG_RESULT_FLAG_ESCALATED. */
void dlv_fail_epoch(struct dlv *d, uint32_t tag_id, uint32_t epoch, uint8_t status,
		    uint32_t stored_epoch, uint32_t now);
/*
 * 10: a session ended with a security status that was not inside an
 * authenticated record (AUTH_FAILED, STALE_EPOCH, VERSION_MISMATCH,
 * NOT_FOUND from CAPS, CHALLENGE, a bad mac_t or a plaintext ERROR). True
 * when it is the DLV_UNAUTH_REPEATS-th consecutive session of this tag and
 * epoch ending with that status: only then does it end the jobs.
 */
bool dlv_unauth_status(struct dlv *d, uint32_t tag_id, uint32_t epoch, uint8_t status);
/* The tag authenticated (AUTH_OK verified): its count starts over. */
void dlv_tag_authenticated(struct dlv *d, uint32_t tag_id, uint32_t epoch);
/* A session with the tag ended: its jobs whose assignment went away (or is
 * now of a newer epoch) while the session held them end CANCELLED. */
void dlv_session_over(struct dlv *d, uint32_t tag_id, uint32_t now);
void dlv_note_battery(struct dlv *d, uint32_t tag_id, uint16_t mv);
uint16_t dlv_battery(const struct dlv *d, uint32_t tag_id);

#endif /* BRIDGE_DELIVERY_H_ */
