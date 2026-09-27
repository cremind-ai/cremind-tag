/* Tag display transaction (docs/protocol.md 5.6, 6). */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_txn.h>

#define CRC_OFF (CTAG_TXN_RECORD_LEN - 4u)

uint8_t ctag_txn_frame_begin(const struct ctag_txn_record *stored, uint32_t epoch,
			     const struct ctag_rec_frame_begin *fb, uint8_t panel_planes,
			     uint16_t panel_plane_len)
{
	if (stored != NULL) {
		/* (epoch, revision) compared lexicographically. */
		if (epoch < stored->epoch ||
		    (epoch == stored->epoch && fb->revision < stored->revision)) {
			return CTAG_STATUS_STALE_REVISION;
		}
		if (epoch == stored->epoch && fb->revision == stored->revision) {
			if (memcmp(fb->digest, stored->digest, CTAG_FRAME_DIGEST_LEN) != 0) {
				return CTAG_STATUS_REVISION_CONFLICT;
			}
			if (stored->state == CTAG_TXN_DISPLAYED) {
				return CTAG_STATUS_OK;
			}
		}
	}
	if (fb->planes != panel_planes || fb->plane_len != panel_plane_len) {
		return CTAG_STATUS_INVALID;
	}
	return CTAG_TXN_ACCEPT;
}

bool ctag_txn_boot(struct ctag_txn_record *rec)
{
	if (rec == NULL || rec->state != CTAG_TXN_REFRESH_INTENT ||
	    rec->status == CTAG_STATUS_DISPLAY_STATE_UNKNOWN) {
		return false;
	}
	rec->status = CTAG_STATUS_DISPLAY_STATE_UNKNOWN;
	return true;
}

void ctag_txn_intent(struct ctag_txn_record *rec, uint32_t tag_id, uint32_t epoch,
		     uint32_t revision, uint64_t update_id,
		     const uint8_t digest[CTAG_FRAME_DIGEST_LEN])
{
	rec->tag_id = tag_id;
	rec->epoch = epoch;
	rec->revision = revision;
	rec->update_id = update_id;
	memcpy(rec->digest, digest, CTAG_FRAME_DIGEST_LEN);
	rec->status = CTAG_STATUS_OK;
	rec->state = CTAG_TXN_REFRESH_INTENT;
}

void ctag_txn_complete(struct ctag_txn_record *rec, uint8_t status)
{
	rec->status = status;
	if (status == CTAG_STATUS_OK) {
		rec->state = CTAG_TXN_DISPLAYED;
	}
}

void ctag_txn_result(const struct ctag_txn_record *rec, uint8_t flags, struct ctag_rec_result *res)
{
	memset(res, 0, sizeof(*res));
	res->update_id = rec->update_id;
	res->epoch = rec->epoch;
	res->revision = rec->revision;
	res->status = rec->status;
	memcpy(res->digest, rec->digest, sizeof(res->digest));
	res->flags = flags;
}

void ctag_txn_record_encode(const struct ctag_txn_record *rec, uint8_t out[CTAG_TXN_RECORD_LEN])
{
	out[0] = CTAG_TXN_RECORD_VERSION;
	out[1] = rec->state;
	out[2] = rec->status;
	out[3] = 0u;
	ctag_put_le32(&out[4], rec->tag_id);
	ctag_put_le32(&out[8], rec->epoch);
	ctag_put_le32(&out[12], rec->revision);
	ctag_put_le64(&out[16], rec->update_id);
	memcpy(&out[24], rec->digest, CTAG_FRAME_DIGEST_LEN);
	ctag_put_le32(&out[CRC_OFF], ctag_crc32(0u, out, CRC_OFF));
}

int ctag_txn_record_decode(struct ctag_txn_record *rec, const uint8_t *in, size_t len)
{
	if (len != CTAG_TXN_RECORD_LEN || in[0] != CTAG_TXN_RECORD_VERSION ||
	    in[1] > CTAG_TXN_REFRESH_INTENT ||
	    ctag_crc32(0u, in, CRC_OFF) != ctag_get_le32(&in[CRC_OFF])) {
		return -EBADMSG;
	}
	rec->state = in[1];
	rec->status = in[2];
	rec->tag_id = ctag_get_le32(&in[4]);
	rec->epoch = ctag_get_le32(&in[8]);
	rec->revision = ctag_get_le32(&in[12]);
	rec->update_id = ctag_get_le64(&in[16]);
	memcpy(rec->digest, &in[24], CTAG_FRAME_DIGEST_LEN);
	return 0;
}

int ctag_txn_load(const struct ctag_txn_store *store, struct ctag_txn_record *rec)
{
	uint8_t buf[CTAG_TXN_RECORD_LEN + 1u]; /* one spare byte detects longer entries */
	int n = store->read(store->ctx, buf, sizeof(buf));

	if (n == -ENOENT) {
		return 0;
	}
	if (n < 0) {
		return -EIO;
	}
	return ctag_txn_record_decode(rec, buf, (size_t)n) == 0 ? 1 : -EBADMSG;
}

int ctag_txn_save(const struct ctag_txn_store *store, const struct ctag_txn_record *rec)
{
	uint8_t buf[CTAG_TXN_RECORD_LEN];

	ctag_txn_record_encode(rec, buf);
	return store->write(store->ctx, buf, sizeof(buf)) < 0 ? -EIO : 0;
}
