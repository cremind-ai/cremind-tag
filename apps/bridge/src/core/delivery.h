/*
 * Bridge delivery core (docs/protocol.md 3, 10; companion sim/bridge.py):
 * assignment table, the one layout being assembled, LAYOUT_COMMIT validation
 * in the order of 3.3, per-tag history (DUPLICATE / STALE_REVISION), jobs
 * (pending layouts persisted in external flash, tag commands), SUPERSEDED /
 * CANCELLED, and DELIVERY_RESULT with a persisted result_seq, re-sent every
 * MESH_RESULT_RETRY_MS up to MESH_RESULT_RETRIES times until RESULT_ACK.
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
	uint32_t tag_id;
	uint32_t epoch;
	uint8_t key[CTAG_TAG_KEY_LEN];
};

struct dlv_timing {
	uint16_t wake_ms;
	uint16_t suspend_ms;
	uint16_t transfer_ms;
	uint16_t refresh_ms;
};

/* Last accepted revision of the assigned tag and its final result (3.3, 10). */
struct dlv_history {
	uint8_t valid;
	uint8_t has_result;
	uint8_t status;
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint8_t digest[CTAG_LAYOUT_DIGEST_LEN];
	uint8_t digest8[8];
	uint16_t battery_mv;
	struct dlv_timing timing;
};

struct dlv_result {
	uint8_t used;
	uint8_t sends;
	uint32_t due_ms;
	struct ctag_mesh_delivery_result msg;
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
	uint32_t storage_errors;
	uint32_t jobs_dropped;
};

struct dlv_env {
	/* An unsolicited message to the gateway (DELIVERY_RESULT, DELIVERY_STAGE). */
	void (*send)(void *ctx, uint8_t op, const uint8_t *params, size_t len);
	/* Persist a small record under a relative name ("a/3", "h/3", "rs");
	 * len 0 deletes it. */
	int (*save)(void *ctx, const char *name, const void *data, size_t len);
	void *ctx;
	/* Pending layouts; without usable flash no pack is valid either, so every
	 * layout already fails FONTPACK_MISMATCH before it could be stored. */
	struct bflash *flash;
	bool flash_ok;
	struct fontstore *fonts;
	const struct ctag_sha256_ops *sha;
};

#define DLV_MAX_JOBS    CONFIG_CTAG_BRIDGE_MAX_JOBS
#define DLV_RESULT_SLOTS CONFIG_CTAG_BRIDGE_RESULT_SLOTS
#define DLV_SEQ_RESERVE 16u

struct dlv {
	struct dlv_env env;
	struct ctag_layout_asm asm_;
	uint8_t asm_buf[CTAG_LAYOUT_HARD_MAX] __aligned(4);
	struct dlv_assignment asg[CTAG_MAX_TAGS_PER_BRIDGE];
	struct dlv_history hist[CTAG_MAX_TAGS_PER_BRIDGE];
	uint16_t battery[CTAG_MAX_TAGS_PER_BRIDGE];
	struct dlv_job jobs[DLV_MAX_JOBS];
	struct dlv_result results[DLV_RESULT_SLOTS];
	uint32_t order;
	uint32_t pending_seq;
	uint16_t ring_head;   /* next pending-layout slot to try */
	uint16_t next_seq;    /* result_seq of the next DELIVERY_RESULT */
	uint16_t reserve_end; /* persisted: seqs below it may have been used */
	struct dlv_counters c;
};

void dlv_init(struct dlv *d, const struct dlv_env *env);

/* Boot: persisted records (settings), then dlv_start() restores the jobs. */
void dlv_restore(struct dlv *d, const char *name, const void *data, size_t len);
/* Reserve result_seqs and restore pending layouts; scratch >= LAYOUT_HARD_MAX. */
void dlv_start(struct dlv *d, uint32_t now, uint8_t *scratch, size_t size);
/* Config Node Reset: forget assignments, history, jobs and pending layouts. */
void dlv_reset(struct dlv *d);

/* ---- LAYOUT_SRV ---- */
void dlv_layout_begin(struct dlv *d, const struct ctag_mesh_layout_begin *b);
void dlv_layout_chunk(struct dlv *d, const struct ctag_mesh_layout_chunk *c);
/* 3.3 in order; *missing is valid with INCOMPLETE. Returns the LAYOUT_STATUS status. */
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
/* Read a layout job's bytes from its pending record (checks the CRC and identity). */
int dlv_job_layout(struct dlv *d, const struct dlv_job *job, uint8_t *buf, size_t size);
void dlv_stage(struct dlv *d, const struct dlv_job *job, uint8_t stage);
/* Final result of a job (removes it, persists history, sends DELIVERY_RESULT). */
void dlv_finish(struct dlv *d, struct dlv_job *job, uint8_t status, const uint8_t digest8[8],
		uint16_t battery_mv, const struct dlv_timing *timing, uint32_t now);
/* AUTH_FAILED, STALE_EPOCH, VERSION_MISMATCH, NOT_FOUND from a tag end its jobs of that epoch. */
void dlv_fail_epoch(struct dlv *d, uint32_t tag_id, uint32_t epoch, uint8_t status, uint32_t now);
void dlv_note_battery(struct dlv *d, uint32_t tag_id, uint16_t mv);
uint16_t dlv_battery(const struct dlv *d, uint32_t tag_id);

#endif /* BRIDGE_DELIVERY_H_ */
