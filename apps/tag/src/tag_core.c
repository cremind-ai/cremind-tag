/*
 * Cremind Tag protocol core: handshake, records, credits, the FRAME_BEGIN
 * decision, the incremental frame digest and the display transaction
 * (docs/protocol.md 5.3-6; behaviour documented in docs/tag-firmware.md).
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>

#include "panel.h"
#include "tag_core.h"

#ifdef CONFIG_APP_LOW_BATTERY_MV
#define LOW_BATTERY_MV CONFIG_APP_LOW_BATTERY_MV
#else
#define LOW_BATTERY_MV 2400
#endif

/*
 * Handlers that own large locals or sit above a deep callee (NVS with garbage
 * collection, PSA) stay out of line, so their frames never add up in one
 * inlined function: the system work queue's worst chain is the sum along one
 * path, not of every handler (docs/tag-firmware.md "Stack budget").
 */
#define FRAME __attribute__((noinline))

/* Consecutive handshake AUTH failures that skip the next wake window (5.4). */
#define AUTH_FAILURES_BEFORE_SKIP 3u

static int store_read(void *ctx, void *buf, size_t len)
{
	(void)ctx;
	return tag_hal_store_read(TAG_STORE_ID_RECORD, buf, len);
}

static int store_write(void *ctx, const void *buf, size_t len)
{
	(void)ctx;
	return tag_hal_store_write(TAG_STORE_ID_RECORD, buf, len);
}

static const struct ctag_txn_store store = {store_read, store_write, NULL};

static bool low_battery(uint16_t mv)
{
	return mv != 0u && mv < LOW_BATTERY_MV;
}

/* ---- persisted epoch (5.4): tag_id | epoch | crc32 ---- */

static uint32_t epoch_crc(const uint8_t *e)
{
	return ctag_crc32(0u, e, TAG_EPOCH_ENTRY_LEN - 4u);
}

static FRAME int save_epoch(struct tag_core *c, uint32_t epoch)
{
	uint8_t e[TAG_EPOCH_ENTRY_LEN];

	ctag_put_le32(e, c->cfg.tag_id);
	ctag_put_le32(&e[4], epoch);
	ctag_put_le32(&e[8], epoch_crc(e));
	if (tag_hal_store_write(TAG_STORE_ID_EPOCH, e, sizeof(e)) < 0) {
		return -EIO;
	}
	c->stored_epoch = epoch;
	c->epoch_error = 0u;
	return 0;
}

/* The display record: 1 loaded, 0 none, < 0 corrupt or unreadable. */
static FRAME int load_record(struct tag_core *c)
{
	return ctag_txn_load(&store, &c->rec);
}

static FRAME void load_epoch(struct tag_core *c)
{
	uint8_t e[TAG_EPOCH_ENTRY_LEN + 1u]; /* one spare byte detects longer entries */
	int n = tag_hal_store_read(TAG_STORE_ID_EPOCH, e, sizeof(e));

	if (n == (int)TAG_EPOCH_ENTRY_LEN && ctag_get_le32(&e[8]) == epoch_crc(e)) {
		if (ctag_get_le32(e) == c->cfg.tag_id) {
			c->stored_epoch = ctag_get_le32(&e[4]);
		}
	} else if (n != -ENOENT) {
		c->epoch_error = 1u;
	}
}

/* Out of line with small helpers: the boot-rule save below runs on the
 * system work queue with the NVS garbage collection underneath. */
FRAME void tag_core_init(struct tag_core *c, const struct tag_core_cfg *cfg)
{
	int n;

	memset(c, 0, sizeof(*c));
	c->cfg = *cfg;

	n = load_record(c);
	if (n == 1 && c->rec.tag_id == cfg->tag_id) {
		c->rec_state = TAG_REC_VALID;
		/* 6: REFRESH_INTENT at boot -> DISPLAY_STATE_UNKNOWN, persisted. */
		if (ctag_txn_boot(&c->rec)) {
			(void)ctag_txn_save(&store, &c->rec);
		}
	} else if (n < 0) {
		c->rec_state = TAG_REC_CORRUPT;
	}
	load_epoch(c);
	/* An epoch that displayed a frame was authenticated: never go below it. */
	if (c->rec_state == TAG_REC_VALID && c->rec.epoch > c->stored_epoch) {
		c->stored_epoch = c->rec.epoch;
	}
}

/* ---- outgoing messages ---- */

/* Reserve room for a message of up to len bytes at the tail; NULL when full. */
static uint8_t *txq_begin(struct tag_core *c, uint8_t chr, size_t len)
{
	if ((size_t)c->txq_tail + 2u + len > TAG_TXQ_SIZE && c->txq_head != 0u) {
		memmove(c->txq, &c->txq[c->txq_head], (size_t)(c->txq_tail - c->txq_head));
		c->txq_tail = (uint8_t)(c->txq_tail - c->txq_head);
		c->txq_head = 0u;
	}
	if ((size_t)c->txq_tail + 2u + len > TAG_TXQ_SIZE) {
		c->closing = 1u;
		return NULL;
	}
	c->txq[c->txq_tail] = chr;
	return &c->txq[c->txq_tail + 2u];
}

static void txq_commit(struct tag_core *c, size_t len)
{
	c->txq[c->txq_tail + 1u] = (uint8_t)len;
	c->txq_tail = (uint8_t)(c->txq_tail + 2u + len);
}

/* A two-byte plaintext message: ERROR on CTRL or CREDIT on STATUS. */
static void send_plain(struct tag_core *c, uint8_t chr, uint8_t type, uint8_t arg)
{
	uint8_t *p = txq_begin(c, chr, 2u);

	if (p != NULL) {
		p[0] = type;
		p[1] = arg;
		txq_commit(c, 2u);
	}
}

/* Seal and queue a record on STATUS; false when it was not queued (the session ends). */
static FRAME bool send_record(struct tag_core *c, uint8_t type, const uint8_t *pt, size_t len)
{
	uint8_t *p = txq_begin(c, TAG_CHR_STATUS, len + CTAG_RECORD_OVERHEAD);
	int n;

	if (p == NULL) {
		return false;
	}
	n = ctag_record_seal(&c->s.tx, type, pt, len, p, len + CTAG_RECORD_OVERHEAD);
	if (n < 0) {
		c->closing = 1u;
		return false;
	}
	txq_commit(c, (size_t)n);
	return true;
}

/* ERROR{status} on CTRL, then disconnect (5.4: any failure ends the session). */
static void fail(struct tag_core *c, uint8_t status)
{
	send_plain(c, TAG_CHR_CTRL, CTAG_CTRL_ERROR, status);
	memset(&c->s, 0, sizeof(c->s)); /* record keys are dead from here on */
	c->closing = 1u;
}

static void credit(struct tag_core *c)
{
	c->bridge_credits++;
	send_plain(c, TAG_CHR_STATUS, CTAG_PLAIN_CREDIT, 1u);
}

static FRAME void result(struct tag_core *c, uint64_t update_id, uint32_t revision, uint8_t status,
		   const uint8_t *digest, uint32_t refresh_ms, uint8_t flags)
{
	struct ctag_rec_result r;
	uint8_t pt[CTAG_REC_RESULT_LEN];

	memset(&r, 0, sizeof(r));
	r.update_id = update_id;
	r.epoch = c->s.epoch;
	r.revision = revision;
	r.status = status;
	if (digest != NULL) {
		memcpy(r.digest, digest, sizeof(r.digest));
	}
	r.battery_mv = tag_hal_battery_mv();
	r.refresh_ms = (uint16_t)(refresh_ms > 0xFFFFu ? 0xFFFFu : refresh_ms);
	r.flags = flags;
	(void)ctag_rec_result_pack(&r, pt, sizeof(pt));
	/* Every RESULT queued clears "result pending", the stored ACK of a
	 * re-delivery included (sim/tag.py send_result()). */
	if (send_record(c, CTAG_REC_RESULT, pt, sizeof(pt))) {
		c->result_pending = 0u;
	}
}

/* ---- handshake (5.4) ---- */

static FRAME void on_hello(struct tag_core *c)
{
	struct ctag_ctrl_challenge ch;
	uint8_t *out = txq_begin(c, TAG_CHR_CTRL, CTAG_SESSION_CHALLENGE_LEN);
	size_t n = 0;

	if (out == NULL) {
		return;
	}
	memset(&ch, 0, sizeof(ch));
	ch.stored_epoch = c->stored_epoch;
	if (c->rec_state == TAG_REC_VALID) {
		ch.displayed_rev = c->rec.revision;
		ch.last_status = c->rec.status;
		ch.flags = ctag_txn_unknown_pending(&c->rec) ? 0x01u : 0u;
	}
	if (c->rec_state == TAG_REC_CORRUPT || c->epoch_error) {
		ch.last_status = CTAG_STATUS_STORAGE_ERROR;
	}
	ch.battery_mv = tag_hal_battery_mv();
	if (low_battery(ch.battery_mv)) {
		ch.flags |= 0x02u;
	}
	if (tag_hal_random(ch.nonce_t, sizeof(ch.nonce_t)) != 0) {
		c->closing = 1u;
		return;
	}
	if (ctag_session_tag_hello(&c->s, c->cfg.tag_id, c->cfg.secret, &ch, c->pt, c->ctrl_len, out,
				   &n) != CTAG_STATUS_OK) {
		c->closing = 1u;
	}
	txq_commit(c, n);
}

static FRAME void on_auth(struct tag_core *c)
{
	uint8_t *out = txq_begin(c, TAG_CHR_CTRL, CTAG_SESSION_AUTH_OK_LEN);
	size_t n = 0;
	uint8_t st;

	if (out == NULL) {
		return;
	}
	st = ctag_session_tag_auth(&c->s, c->pt, c->ctrl_len, out, &n);
	/* 5.4: a newer epoch is persisted only now, and before AUTH_OK leaves. */
	if (st == CTAG_STATUS_OK && c->s.epoch > c->stored_epoch && save_epoch(c, c->s.epoch) != 0) {
		memset(&c->s, 0, sizeof(c->s));
		st = CTAG_STATUS_STORAGE_ERROR;
		out[0] = CTAG_CTRL_ERROR;
		out[1] = st;
		n = CTAG_SESSION_ERROR_LEN;
	}
	txq_commit(c, n);
	if (st == CTAG_STATUS_OK) {
		c->auth_failures = 0u;
		c->bridge_credits = TAG_CREDITS;
		send_plain(c, TAG_CHR_STATUS, CTAG_PLAIN_CREDIT, TAG_CREDITS);
		return;
	}
	if (st == CTAG_STATUS_AUTH_FAILED && ++c->auth_failures >= AUTH_FAILURES_BEFORE_SKIP) {
		c->auth_failures = 0u;
		c->skip_window = 1u;
	}
	c->closing = 1u;
}

static void on_ctrl(struct tag_core *c)
{
	if (c->pt[0] == CTAG_CTRL_HELLO && c->s.state == CTAG_SESSION_IDLE) {
		on_hello(c);
	} else if (c->pt[0] == CTAG_CTRL_AUTH && c->s.state == CTAG_SESSION_CHALLENGED) {
		on_auth(c);
	} else {
		fail(c, CTAG_STATUS_INVALID);
	}
}

/* ---- frames (5.6) and the display transaction (6) ---- */

/* Abort a frame being received: the panel never refreshes it. */
static void frame_drop(struct tag_core *c)
{
	if (c->frame == TAG_FRAME_RECEIVING || c->frame == TAG_FRAME_VALIDATED) {
		panel_abort_frame();
		ctag_crypto_sha256_abort(&c->sha);
		c->frame = TAG_FRAME_IDLE;
	}
}

static void frame_reject(struct tag_core *c, uint8_t status)
{
	frame_drop(c);
	result(c, c->update_id, c->revision, status, NULL, 0u, 0u);
}

/* 6: REFRESH_INTENT is durable before commit_refresh(). 0 or -EIO. */
static FRAME int persist_intent(struct tag_core *c)
{
	struct ctag_txn_record r;

	ctag_txn_intent(&r, c->cfg.tag_id, c->s.epoch, c->revision, c->update_id, c->digest);
	if (ctag_txn_save(&store, &r) != 0) {
		return -EIO;
	}
	c->rec = r;
	c->rec_state = TAG_REC_VALID;
	return 0;
}

/*
 * The display transaction of 6 for a VALIDATED frame. Runs from the top of
 * tag_core_poll(), so the NVS write (and its garbage collection) never sits
 * under the record handlers' frames. Returns non-zero when the panel refused
 * the refresh (the caller completes with PANEL_ERROR).
 */
static FRAME int transaction(struct tag_core *c)
{
	static const uint8_t refreshing = CTAG_STAGE_REFRESHING;
	int err;

	if (persist_intent(c) != 0) {
		c->frame = TAG_FRAME_IDLE;
		panel_abort_frame();
		result(c, c->update_id, c->revision, CTAG_STATUS_STORAGE_ERROR, NULL, 0u, 0u);
		credit(c);
		return 0;
	}
	c->result_pending = 1u;
	send_record(c, CTAG_REC_PROGRESS, &refreshing, sizeof(refreshing));
	c->frame = TAG_FRAME_REFRESHING;
	c->refresh_t0 = tag_hal_uptime_ms();
	err = panel_commit_refresh();
	if (err == 0) {
		err = panel_wait_refresh_complete();
	}
	return err;
}

void tag_core_refresh_done(struct tag_core *c, uint8_t status)
{
	uint32_t ms = tag_hal_uptime_ms() - c->refresh_t0;

	if (c->frame != TAG_FRAME_REFRESHING) {
		return;
	}
	c->frame = TAG_FRAME_IDLE;
	/* OK -> DISPLAYED; a failure stays REFRESH_INTENT with its status. */
	ctag_txn_complete(&c->rec, status);
	if (ctag_txn_save(&store, &c->rec) != 0 && status == CTAG_STATUS_OK) {
		/* The panel refreshed but DISPLAYED is not durable: no ACK. */
		c->rec.state = CTAG_TXN_REFRESH_INTENT;
		c->rec.status = CTAG_STATUS_STORAGE_ERROR;
		status = CTAG_STATUS_STORAGE_ERROR;
	}
	panel_sleep();
	if (c->link && !c->closing && c->s.state == CTAG_SESSION_ESTABLISHED) {
		result(c, c->rec.update_id, c->rec.revision, status,
		       status == CTAG_STATUS_OK ? c->rec.digest : NULL, ms, 0u);
		credit(c);
	}
}

static FRAME int frame_begin(struct tag_core *c, size_t n)
{
	struct ctag_rec_frame_begin fb;
	uint8_t st = CTAG_STATUS_UNSUPPORTED;

	if (ctag_rec_frame_begin_unpack(&fb, c->pt, n) != 0) {
		return -EINVAL;
	}
	frame_drop(c);
	if (c->cfg.flags & TAG_CFG_PANEL_OK) {
		st = ctag_txn_frame_begin(c->rec_state == TAG_REC_VALID ? &c->rec : NULL, c->s.epoch,
					  &fb, c->cfg.planes, c->cfg.plane_len);
	}
	if (st == CTAG_TXN_ACCEPT) {
		st = CTAG_STATUS_PANEL_ERROR;
		if (panel_begin_frame() == 0) {
			st = CTAG_STATUS_INTERNAL;
			if (ctag_crypto_sha256_init(&c->sha) == 0) {
				c->frame = TAG_FRAME_RECEIVING;
				c->plane = 0u;
				c->received = 0u;
				c->revision = fb.revision;
				c->update_id = fb.update_id;
				memcpy(c->digest, fb.digest, sizeof(c->digest));
				return 0;
			}
		}
		panel_abort_frame(); /* back to deep sleep after a failed start */
	}
	if (st == CTAG_STATUS_OK) {
		/* Duplicate of the displayed revision: the stored ACK (flags.bit0). */
		result(c, c->rec.update_id, c->rec.revision, CTAG_STATUS_OK, c->rec.digest, 0u, 1u);
	} else {
		result(c, fb.update_id, fb.revision, st, NULL, 0u, 0u);
	}
	return 0;
}

static FRAME int plane_data(struct tag_core *c, size_t n)
{
	struct ctag_rec_plane_data pd;

	if (ctag_rec_plane_data_unpack(&pd, c->pt, n) != 0) {
		return -EINVAL;
	}
	if (c->frame != TAG_FRAME_RECEIVING) {
		return 0; /* the tail of a rejected or aborted frame */
	}
	if (pd.plane != c->plane || pd.offset != c->received || pd.data_len == 0u ||
	    pd.data_len > (size_t)(c->cfg.plane_len - c->received)) {
		frame_reject(c, CTAG_STATUS_INVALID);
		return 0;
	}
	/* Authenticated and complete before any byte reaches the controller (5.5). */
	if (ctag_crypto_sha256_update(&c->sha, pd.data, pd.data_len) != 0 ||
	    panel_write_plane_chunk(pd.plane, pd.offset, pd.data, pd.data_len) != 0) {
		frame_reject(c, CTAG_STATUS_PANEL_ERROR);
		return 0;
	}
	c->received = (uint16_t)(c->received + pd.data_len);
	if (c->received == c->cfg.plane_len && c->plane + 1u < c->cfg.planes) {
		c->plane++;
		c->received = 0u;
	}
	return 0;
}

/* Hash the staged frame and hand it to the transaction. */
static FRAME void frame_commit(struct tag_core *c)
{
	uint8_t *d = c->pt; /* FRAME_END has no plaintext: pt is free */

	if (ctag_crypto_sha256_finish(&c->sha, d) != 0) {
		frame_reject(c, CTAG_STATUS_INTERNAL);
	} else if (memcmp(d, c->digest, CTAG_FRAME_DIGEST_LEN) != 0) {
		frame_reject(c, CTAG_STATUS_DIGEST_MISMATCH);
	} else if (panel_validate_frame() != 0) {
		frame_reject(c, CTAG_STATUS_PANEL_ERROR);
	} else {
		c->frame = TAG_FRAME_VALIDATED;
	}
}

static int frame_end(struct tag_core *c, size_t n)
{
	if (n != 0u) { /* FRAME_END has no fields */
		return -EINVAL;
	}
	if (c->frame != TAG_FRAME_RECEIVING) {
		return 0;
	}
	if (c->plane + 1u != c->cfg.planes || c->received != c->cfg.plane_len) {
		frame_reject(c, CTAG_STATUS_INCOMPLETE);
		return 0;
	}
	frame_commit(c);
	return 0;
}

/* CMD{CLEAR}: stage white in every plane, then the same transaction. */
static FRAME bool clear(struct tag_core *c, uint64_t update_id)
{
	uint16_t off;
	uint16_t len;
	uint8_t p;

	if (panel_begin_frame() != 0 || ctag_crypto_sha256_init(&c->sha) != 0) {
		panel_abort_frame(); /* back to deep sleep after a failed start */
		return false;
	}
	c->frame = TAG_FRAME_RECEIVING;
	for (p = 0u; p < c->cfg.planes; p++) {
		/* 4.4: plane 0 bit 1 = white when plane_flags.bit0; plane 1 bit 1 =
		 * red when plane_flags.bit1, so its "not red" byte is the inverse. */
		uint8_t white = ((c->cfg.plane_flags >> p) & 1u) != 0u ? 0xFFu : 0x00u;

		memset(c->pt, p == 0u ? white : (uint8_t)~white, sizeof(c->pt));
		for (off = 0u; off < c->cfg.plane_len; off = (uint16_t)(off + len)) {
			len = (uint16_t)(c->cfg.plane_len - off);
			if (len > sizeof(c->pt)) {
				len = sizeof(c->pt);
			}
			if (ctag_crypto_sha256_update(&c->sha, c->pt, len) != 0 ||
			    panel_write_plane_chunk(p, off, c->pt, len) != 0) {
				frame_drop(c);
				return false;
			}
		}
	}
	c->revision = 0u;
	c->update_id = update_id;
	/* The digest of white is also the digest the frame must reproduce. */
	if (ctag_crypto_sha256_finish(&c->sha, c->digest) != 0 || panel_validate_frame() != 0) {
		frame_drop(c);
		return false;
	}
	c->frame = TAG_FRAME_VALIDATED;
	return true;
}

static FRAME int command(struct tag_core *c, size_t n)
{
	struct ctag_rec_cmd cmd;
	uint8_t st = CTAG_STATUS_UNSUPPORTED;

	if (ctag_rec_cmd_unpack(&cmd, c->pt, n) != 0) {
		return -EINVAL;
	}
	if (cmd.cmd == CTAG_TAG_CMD_CLEAR && (c->cfg.flags & TAG_CFG_PANEL_OK)) {
		frame_drop(c);
		if (clear(c, cmd.update_id)) {
			return 0;
		}
		st = CTAG_STATUS_PANEL_ERROR;
	} else if (cmd.cmd == CTAG_TAG_CMD_SLEEP && (c->cfg.flags & TAG_CFG_SLEEP_OK)) {
		st = CTAG_STATUS_OK;
		c->closing = 1u;
		c->sleep = 1u;
	}
	/* IDENTIFY and REFRESH are companion-level: UNSUPPORTED here (10). */
	result(c, cmd.update_id, 0u, st, NULL, 0u, 0u);
	return 0;
}

static FRAME void on_record(struct tag_core *c, uint8_t b)
{
	uint8_t type = 0;
	int n;

	if (c->s.state != CTAG_SESSION_ESTABLISHED) {
		fail(c, CTAG_STATUS_INVALID);
		return;
	}
	n = ctag_record_open(&c->s.rx, c->rbuf[b], c->rlen[b], &type, c->pt, sizeof(c->pt));
	if (n < 0) {
		fail(c, CTAG_STATUS_AUTH_FAILED);
		return;
	}
	if (c->bridge_credits == 0u) {
		fail(c, CTAG_STATUS_INVALID);
		return;
	}
	c->bridge_credits--;
	switch (type) {
	case CTAG_REC_FRAME_BEGIN:
		n = frame_begin(c, (size_t)n);
		break;
	case CTAG_REC_PLANE_DATA:
		n = plane_data(c, (size_t)n);
		break;
	case CTAG_REC_FRAME_END:
		n = frame_end(c, (size_t)n);
		break;
	case CTAG_REC_FRAME_ABORT:
		if (n == CTAG_REC_FRAME_ABORT_LEN) {
			frame_drop(c);
			n = 0;
		} else {
			n = -EINVAL;
		}
		break;
	case CTAG_REC_CMD:
		n = command(c, (size_t)n);
		break;
	default:
		n = -EINVAL;
		break;
	}
	if (n != 0) {
		fail(c, CTAG_STATUS_INVALID);
	} else if (c->frame < TAG_FRAME_VALIDATED) {
		credit(c); /* this record's buffer is free again */
	}
	/* else the credit follows the RESULT of the transaction. */
}

/* ---- entry points ---- */

void tag_core_link_up(struct tag_core *c)
{
	memset(&c->s, 0, sizeof(c->s));
	c->link = 1u;
	c->closing = 0u;
	c->fatal = TAG_FATAL_NONE;
	c->sleep = 0u;
	c->bridge_credits = 0u;
	c->ctrl_ready = 0u;
	c->rxb = 0u;
	c->proc = 0u;
	c->rdy[0] = 0u;
	c->rdy[1] = 0u;
	c->txq_head = 0u;
	c->txq_tail = 0u;
	c->txq_off = 0u;
	ctag_frag_rx_init(&c->ctrl_rx, c->pt, CTAG_TAG_CTRL_MSG_MAX);
	ctag_frag_rx_init(&c->data_rx, c->rbuf[0], CTAG_TAG_RECORD_WIRE_MAX);
	ctag_frag_tx_init(&c->ctrl_tx);
	ctag_frag_tx_init(&c->status_tx);
}

void tag_core_link_down(struct tag_core *c)
{
	/* 5.6: a disconnect before FRAME_END aborts the frame; a refresh in
	 * flight completes and is persisted without a RESULT. */
	frame_drop(c);
	memset(&c->s, 0, sizeof(c->s));
	c->link = 0u;
	c->closing = 0u;
	c->txq_head = 0u;
	c->txq_tail = 0u;
	c->txq_off = 0u;
}

bool tag_core_rx(struct tag_core *c, uint8_t chr, const uint8_t *value, size_t len)
{
	int n;

	if (!c->link || c->closing || c->fatal != TAG_FATAL_NONE) {
		return false;
	}
	if (chr == TAG_CHR_CTRL) {
		/* CTRL reassembles into pt, which records own once established. */
		if (c->ctrl_ready || c->s.state == CTAG_SESSION_ESTABLISHED) {
			c->fatal = TAG_FATAL_INVALID;
			return true;
		}
		n = ctag_frag_rx_put(&c->ctrl_rx, value, len);
		if (n > 0) {
			c->ctrl_len = (uint8_t)n;
			c->ctrl_ready = 1u;
			return true;
		}
	} else {
		/* Both buffers busy: a record the bridge had no credit for. */
		if (c->rdy[c->rxb]) {
			c->fatal = TAG_FATAL_INVALID;
			return true;
		}
		n = ctag_frag_rx_put(&c->data_rx, value, len);
		if (n > 0) {
			c->rlen[c->rxb] = (uint8_t)n;
			c->rdy[c->rxb] = 1u;
			c->rxb ^= 1u;
			c->data_rx.buf = c->rbuf[c->rxb];
			return true;
		}
	}
	if (n == 0) {
		return false;
	}
	c->fatal = TAG_FATAL_SILENT; /* 5.3 violation: the session ends */
	return true;
}

void tag_core_poll(struct tag_core *c)
{
	uint8_t b;

	if (!c->link) {
		return;
	}
	if (c->fatal == TAG_FATAL_INVALID) {
		fail(c, CTAG_STATUS_INVALID);
	} else if (c->fatal == TAG_FATAL_SILENT) {
		c->closing = 1u;
	}
	c->fatal = TAG_FATAL_NONE;
	if (c->closing) {
		return;
	}
	if (c->ctrl_ready) {
		on_ctrl(c);
		c->ctrl_ready = 0u;
	}
	/* In order; nothing is processed while a transaction is in flight. */
	while (!c->closing && c->frame < TAG_FRAME_VALIDATED && c->rdy[c->proc]) {
		b = c->proc;
		on_record(c, b);
		c->rdy[b] = 0u;
		c->proc ^= 1u;
		if (c->frame == TAG_FRAME_VALIDATED && transaction(c) != 0) {
			tag_core_refresh_done(c, CTAG_STATUS_PANEL_ERROR);
		}
	}
}

void tag_core_timeout(struct tag_core *c)
{
	if (c->link && c->frame < TAG_FRAME_VALIDATED) {
		c->closing = 1u;
	}
}

int tag_core_tx_next(struct tag_core *c, uint8_t *chr, uint8_t out[CTAG_ATT_VALUE_MAX])
{
	uint8_t h = c->txq_head;
	size_t off = c->txq_off;
	size_t len;
	int n;

	if (h == c->txq_tail) {
		return 0;
	}
	*chr = c->txq[h];
	len = c->txq[h + 1u];
	n = ctag_frag_next(*chr == TAG_CHR_CTRL ? &c->ctrl_tx : &c->status_tx, &c->txq[h + 2u], len,
			   &off, CTAG_FRAG_PAYLOAD_MAX, out);
	if (off < len) {
		c->txq_off = (uint8_t)off;
	} else {
		c->txq_off = 0u;
		c->txq_head = (uint8_t)(h + 2u + len);
		if (c->txq_head == c->txq_tail) {
			c->txq_head = 0u;
			c->txq_tail = 0u;
		}
	}
	return n;
}

uint8_t tag_core_adv_flags(const struct tag_core *c, uint16_t battery_mv)
{
	uint8_t f = c->result_pending ? TAG_ADV_RESULT_PENDING : 0u;

	if (low_battery(battery_mv)) {
		f |= TAG_ADV_LOW_BATTERY;
	}
	if (c->rec_state == TAG_REC_VALID && ctag_txn_unknown_pending(&c->rec)) {
		f |= TAG_ADV_STATE_UNKNOWN;
	}
	return f;
}

bool tag_core_take_skip(struct tag_core *c)
{
	bool skip = c->skip_window != 0u;

	c->skip_window = 0u;
	return skip;
}
