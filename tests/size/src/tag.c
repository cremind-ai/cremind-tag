/*
 * Calls the library API a tag uses (and, with CONFIG_CTAG_SIZE_BRIDGE, the
 * gateway/bridge API in bridge.c) so the link keeps exactly that code. Never
 * run; built to measure the footprint.
 */
#include <errno.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_enroll.h>
#include <ctag/ctag_frag.h>
#include <ctag/ctag_session.h>
#include <ctag/ctag_txn.h>

void size_bridge(void);

static uint8_t rec_buf[2][CTAG_TAG_RECORD_BUF];
static uint8_t ctrl_buf[CTAG_TAG_CTRL_MSG_MAX];
static uint8_t value[CTAG_ATT_VALUE_MAX];
static uint8_t pt[CTAG_TAG_RECORD_PAYLOAD_MAX];
static uint8_t uicr[CTAG_ENROLLMENT_LEN];
static struct ctag_session session;
static struct ctag_txn_record record;
static ctag_sha256_ctx digest;

static int nvs_read(void *ctx, void *buf, size_t len)
{
	(void)ctx;
	(void)buf;
	(void)len;
	return -ENOENT;
}

static int nvs_write(void *ctx, const void *buf, size_t len)
{
	(void)ctx;
	(void)buf;
	return (int)len;
}

int main(void)
{
	struct ctag_txn_store store = {nvs_read, nvs_write, NULL};
	struct ctag_enrollment enroll;
	struct ctag_ctrl_challenge ch = {0};
	struct ctag_rec_frame_begin fb = {0};
	struct ctag_rec_result res;
	struct ctag_frag_rx rx;
	struct ctag_frag_tx tx;
	size_t off = 0;
	size_t len;
	uint8_t type;
	int n;

	(void)ctag_crypto_init();
	(void)ctag_enroll_parse(uicr, sizeof(uicr), &enroll);
	(void)ctag_crypto_random(ch.nonce_t, sizeof(ch.nonce_t));
	ctag_frag_rx_init(&rx, ctrl_buf, sizeof(ctrl_buf));
	ctag_frag_tx_init(&tx);
	n = ctag_frag_rx_put(&rx, value, sizeof(value));
	(void)ctag_session_tag_hello(&session, enroll.tag_id, enroll.secret, value, sizeof(value), &ch,
				     ctrl_buf, (size_t)n, rec_buf[0], &len);
	(void)ctag_session_tag_auth(&session, ch.stored_epoch, ctrl_buf, (size_t)n, rec_buf[0], &len);
	(void)ctag_frag_next(&tx, rec_buf[0], len, &off, CTAG_FRAG_PAYLOAD_MAX, value);
	n = ctag_record_open(&session.rx, rec_buf[1], sizeof(rec_buf[1]), &type, pt, sizeof(pt));
	/* The incremental digest of the plane bytes (5.6). */
	if (ctag_crypto_sha256_init(&digest) != 0 ||
	    ctag_crypto_sha256_update(&digest, pt, (size_t)n) != 0 ||
	    ctag_crypto_sha256_finish(&digest, fb.digest) != 0) {
		ctag_crypto_sha256_abort(&digest);
	}
	(void)ctag_txn_load(&store, &record);
	(void)ctag_txn_boot(&record);
	if (ctag_txn_frame_begin(&record, session.epoch, &fb, 1, 15000) == CTAG_TXN_ACCEPT) {
		ctag_txn_intent(&record, enroll.tag_id, session.epoch, fb.revision, fb.update_id,
				fb.digest);
		ctag_txn_complete(&record, CTAG_STATUS_OK);
		(void)ctag_txn_save(&store, &record);
	}
	ctag_txn_result(&record, 0, &res);
	(void)ctag_rec_result_pack(&res, pt, sizeof(pt));
	n = ctag_record_seal(&session.tx, CTAG_REC_RESULT, pt, CTAG_REC_RESULT_LEN, rec_buf[0],
			     sizeof(rec_buf[0]));
	(void)ctag_txn_unknown_pending(&record);
#ifdef CONFIG_CTAG_SIZE_BRIDGE
	size_bridge();
#endif
	return n;
}
