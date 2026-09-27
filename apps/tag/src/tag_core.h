/*
 * Cremind Tag protocol core (docs/protocol.md 5.3-6, docs/tag-firmware.md).
 *
 * Bluetooth-independent: the application glue feeds it the ATT values written
 * to CTRL and DATA, link events and timer expiries, pulls the ATT values it
 * wants sent on CTRL (indications) and STATUS (notifications), and provides
 * the platform hooks declared at the end of this header (storage, panel,
 * battery, clock, randomness). The native_sim tests (tests/ztest/tag_core)
 * provide fakes for the same hooks.
 *
 * Threading: every function runs in one cooperative context (the system work
 * queue on the tag, where BT_RECV_WORKQ_SYS also delivers the GATT callbacks).
 * tag_core_rx() only reassembles and returns true when tag_core_poll() has
 * work; the glue then schedules its work item, which keeps the stack depth of
 * the host RX path and of the crypto path apart.
 */
#ifndef TAG_CORE_H_
#define TAG_CORE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_frag.h>
#include <ctag/ctag_session.h>
#include <ctag/ctag_txn.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Characteristic of an ATT value (GATT glue <-> core). */
enum tag_chr {
	TAG_CHR_CTRL = 0,
	TAG_CHR_DATA = 1,
	TAG_CHR_STATUS = 2,
};

/* NVS ids of the two persisted entries (store_nvs.c). */
#define TAG_STORE_ID_RECORD 1u /* ctag_txn display record, 60 bytes */
#define TAG_STORE_ID_EPOCH  2u /* tag_id u32 | stored_epoch u32 | crc32 u32 */
#define TAG_EPOCH_ENTRY_LEN 12u

/* Initial DATA credits after AUTH_OK = the two record buffers (5.5, CAPS.credits). */
#define TAG_CREDITS 2u

/*
 * Outgoing message queue, 2 bytes of framing per message. A bridge holds at
 * most two credits and a record's CREDIT is queued after its RESULT, so at
 * most two records' answers are pending: 2 x (RESULT 43 + CREDIT 2 + 4) = 98
 * bytes, or PROGRESS (14 + 2) with one of them. The handshake needs 32
 * (CHALLENGE). A full queue ends the session (the bridge retries).
 */
#define TAG_TXQ_SIZE 112u

/* Advertising flags (5.1 manufacturer data). */
#define TAG_ADV_RESULT_PENDING 0x01u
#define TAG_ADV_LOW_BATTERY    0x02u
#define TAG_ADV_STATE_UNKNOWN  0x04u

/* tag_core_cfg.flags */
#define TAG_CFG_PANEL_OK 0x01u /* frames and CLEAR may drive the panel */
#define TAG_CFG_SLEEP_OK 0x02u /* CMD{SLEEP}: external wake verified */

struct tag_core_cfg {
	uint32_t tag_id;
	const uint8_t *secret; /* CTAG_TAG_SECRET_LEN bytes, e.g. in UICR; never copied */
	uint16_t plane_len;    /* ceil(width / 8) * height */
	uint8_t planes;        /* 1 or 2 */
	uint8_t plane_flags;   /* CAPS plane_flags: white values for CLEAR */
	uint8_t flags;         /* TAG_CFG_* */
};

/* Persisted-record knowledge. */
enum tag_rec_state {
	TAG_REC_NONE = 0,
	TAG_REC_VALID = 1,
	TAG_REC_CORRUPT = 2, /* behave as if none, report STORAGE_ERROR (6) */
};

enum tag_frame_state {
	TAG_FRAME_IDLE = 0,
	TAG_FRAME_RECEIVING = 1,
	TAG_FRAME_VALIDATED = 2, /* staged and verified; the transaction runs next */
	TAG_FRAME_REFRESHING = 3,
};

/* Why tag_core_rx() stopped accepting input (handled by tag_core_poll()). */
enum tag_fatal {
	TAG_FATAL_NONE = 0,
	TAG_FATAL_SILENT = 1,  /* fragment rule broken (5.3): close without ERROR */
	TAG_FATAL_INVALID = 2, /* record without a free buffer, CTRL out of turn: ERROR{INVALID} */
};

struct tag_core {
	struct tag_core_cfg cfg;

	/* Persistent state as loaded/written (6). */
	struct ctag_txn_record rec;
	uint32_t stored_epoch;
	uint8_t rec_state;   /* enum tag_rec_state */
	uint8_t epoch_error; /* the epoch entry was unreadable at boot */

	/* Link and pacing. */
	uint8_t link;          /* a bridge is connected */
	uint8_t closing;       /* disconnect once the TX queue is drained */
	uint8_t fatal;         /* enum tag_fatal, set by tag_core_rx() */
	uint8_t auth_failures; /* consecutive handshake AUTH failures */
	uint8_t skip_window;   /* skip the next advertising window (5.4 pacing) */
	uint8_t result_pending;
	uint8_t bridge_credits; /* DATA records the bridge may still send */
	uint8_t sleep;          /* CMD{SLEEP} accepted: System OFF after the disconnect */

	/* Reassembly (5.3, 5.5): CTRL into pt, records into rbuf[rxb]. */
	uint8_t ctrl_ready;
	uint8_t ctrl_len;
	uint8_t rxb;    /* record buffer being reassembled */
	uint8_t proc;   /* next record buffer to process */
	uint8_t rdy[2]; /* one byte per buffer: record complete, not processed */
	uint8_t rlen[2];
	struct ctag_frag_rx ctrl_rx;
	struct ctag_frag_rx data_rx;

	/* Session (5.4-5.5). */
	struct ctag_session s;

	/* Frame being received or refreshed (5.6, 6). */
	uint8_t frame;       /* enum tag_frame_state */
	uint8_t plane;       /* plane being received */
	uint16_t received;   /* bytes of that plane */
	uint32_t revision;
	uint64_t update_id;
	uint8_t digest[CTAG_FRAME_DIGEST_LEN];
	uint32_t refresh_t0;
	ctag_sha256_ctx sha;

	/* Outgoing messages, fragmented on demand. */
	struct ctag_frag_tx ctrl_tx;
	struct ctag_frag_tx status_tx;
	uint8_t txq_head;
	uint8_t txq_tail;
	uint8_t txq_off; /* bytes of the head message already fragmented */
	uint8_t txq[TAG_TXQ_SIZE];

	/* Record plaintext; CTRL reassembly before the session is established;
	 * white fill for CLEAR. */
	uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
	/* Two record buffers (5.5). A record is at most TAG_RECORD_WIRE_MAX
	 * (205) bytes, which the reassembler enforces, so the buffers are that
	 * size rather than the TAG_RECORD_BUF (256) budget. */
	uint8_t rbuf[2][CTAG_TAG_RECORD_WIRE_MAX];
};

/* Boot: load the persisted record and epoch, apply the boot rule of 6. */
void tag_core_init(struct tag_core *c, const struct tag_core_cfg *cfg);

/* A bridge connected / the link dropped. */
void tag_core_link_up(struct tag_core *c);
void tag_core_link_down(struct tag_core *c);

/* One ATT value written to CTRL or DATA. true = call tag_core_poll(). */
bool tag_core_rx(struct tag_core *c, uint8_t chr, const uint8_t *value, size_t len);

/* Process completed messages and records. */
void tag_core_poll(struct tag_core *c);

/* No progress for TAG_SESSION_TIMEOUT_MS (the glue does not call it while
 * tag_core_refreshing()). */
void tag_core_timeout(struct tag_core *c);

/*
 * The panel finished (or failed) the refresh started by
 * panel_wait_refresh_complete(): persists the result (6), sends RESULT and
 * the credit, and resumes record processing (call tag_core_poll() after).
 */
void tag_core_refresh_done(struct tag_core *c, uint8_t status);

/* Next ATT value to send: its length with *chr set, or 0 when nothing is queued. */
int tag_core_tx_next(struct tag_core *c, uint8_t *chr, uint8_t out[CTAG_ATT_VALUE_MAX]);

static inline bool tag_core_tx_empty(const struct tag_core *c)
{
	return c->txq_head == c->txq_tail;
}

/* The session is over: disconnect once tag_core_tx_empty(). */
static inline bool tag_core_closing(const struct tag_core *c)
{
	return c->closing != 0u;
}

static inline bool tag_core_refreshing(const struct tag_core *c)
{
	return c->frame >= TAG_FRAME_VALIDATED;
}

/* Advertising: flags byte, low 16 bits of the displayed revision. */
uint8_t tag_core_adv_flags(const struct tag_core *c, uint16_t battery_mv);

static inline uint16_t tag_core_disp_rev(const struct tag_core *c)
{
	return c->rec_state == TAG_REC_VALID ? (uint16_t)c->rec.revision : 0u;
}

/* 5.4 pacing: true (once) when the next advertising window must be skipped. */
bool tag_core_take_skip(struct tag_core *c);

/* ---- Platform hooks (the application glue or the test fakes) ---- */

/* VDD in mV, 0 = unknown. */
uint16_t tag_hal_battery_mv(void);
uint32_t tag_hal_uptime_ms(void);
/* Fresh nonce bytes; 0 or a negative error. */
int tag_hal_random(uint8_t *buf, size_t len);
/* nvs_read()/nvs_write() semantics: bytes of the stored entry (may exceed
 * len) or -ENOENT; bytes written (0 = unchanged) or a negative error. */
int tag_hal_store_read(uint16_t id, void *buf, size_t len);
int tag_hal_store_write(uint16_t id, const void *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* TAG_CORE_H_ */
