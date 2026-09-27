/* ctag_txn against tag_txn.json, plus the persisted record and its storage. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_txn.h>

#include "check.h"
#include "suites.h"
#include "v_tag_txn.h"

static void to_record(const struct v_txn_rec *v, struct ctag_txn_record *rec)
{
	memset(rec, 0, sizeof(*rec));
	rec->tag_id = v->tag_id;
	rec->epoch = v->epoch;
	rec->revision = v->revision;
	rec->update_id = v->update_id;
	memcpy(rec->digest, v->digest, sizeof(rec->digest));
	rec->status = (uint8_t)v->status;
	rec->state = (uint8_t)v->state;
}

static bool same(const struct ctag_txn_record *a, const struct ctag_txn_record *b)
{
	return a->tag_id == b->tag_id && a->epoch == b->epoch && a->revision == b->revision &&
	       a->update_id == b->update_id &&
	       memcmp(a->digest, b->digest, sizeof(a->digest)) == 0 && a->status == b->status &&
	       a->state == b->state;
}

void test_txn_frame_begin(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_txn_frame_begin); i++) {
		const struct v_txn_frame_begin *v = &v_txn_frame_begin[i];
		struct ctag_txn_record stored;
		struct ctag_rec_frame_begin fb;
		uint8_t got;

		memset(&fb, 0, sizeof(fb));
		fb.revision = v->revision;
		memcpy(fb.digest, v->digest, sizeof(fb.digest));
		fb.planes = v->planes;
		fb.plane_len = v->plane_len;
		if (v->stored.present) {
			to_record(&v->stored, &stored);
		}
		got = ctag_txn_frame_begin(v->stored.present ? &stored : NULL, v->epoch, &fb,
					   V_TXN_PANEL_PLANES, V_TXN_PANEL_PLANE_LEN);
		CHECK_CASE((got == CTAG_TXN_ACCEPT) == v->accept, v->name);
		CHECK_CASE((got == CTAG_STATUS_OK) == v->duplicate, v->name);
		CHECK_CASE(v->accept || got == v->status, v->name);
	}
}

void test_txn_boot(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_txn_boot); i++) {
		const struct v_txn_boot *v = &v_txn_boot[i];
		struct ctag_txn_record rec, want;
		struct ctag_txn_record *p = NULL;

		if (v->stored.present) {
			to_record(&v->stored, &rec);
			p = &rec;
		}
		CHECK_CASE(ctag_txn_boot(p) == v->persist, v->name);
		CHECK_CASE(ctag_txn_unknown_pending(p) == v->flag, v->name);
		CHECK_CASE(v->expect.present == (p != NULL), v->name);
		if (p != NULL) {
			to_record(&v->expect, &want);
			CHECK_CASE(same(p, &want), v->name);
		}
	}
}

void test_txn_record(void)
{
	struct ctag_txn_record rec, back;
	uint8_t buf[CTAG_TXN_RECORD_LEN];

	to_record(&v_txn_boot[2].stored, &rec);
	ctag_txn_record_encode(&rec, buf);
	CHECK(buf[0] == CTAG_TXN_RECORD_VERSION && buf[1] == rec.state && buf[2] == rec.status);
	CHECK(ctag_get_le32(&buf[4]) == rec.tag_id && ctag_get_le64(&buf[16]) == rec.update_id);
	CHECK(ctag_txn_record_decode(&back, buf, sizeof(buf)) == 0 && same(&rec, &back));
	CHECK(ctag_txn_record_decode(&back, buf, sizeof(buf) - 1u) == -EBADMSG);
	buf[30] ^= 0x01u; /* digest byte: CRC fails */
	CHECK(ctag_txn_record_decode(&back, buf, sizeof(buf)) == -EBADMSG);
	rec.state = 2u; /* no such state */
	ctag_txn_record_encode(&rec, buf);
	CHECK(ctag_txn_record_decode(&back, buf, sizeof(buf)) == -EBADMSG);
	rec.state = CTAG_TXN_DISPLAYED;
	ctag_txn_record_encode(&rec, buf);
	buf[0] = 2u;
	CHECK(ctag_txn_record_decode(&back, buf, sizeof(buf)) == -EBADMSG);
}

struct fake_store {
	uint8_t data[80];
	int len; /* -ENOENT = nothing stored */
	int fail;
	unsigned int writes;
};

static int store_read(void *ctx, void *buf, size_t len)
{
	struct fake_store *s = ctx;

	if (s->fail != 0 || s->len < 0) {
		return s->fail != 0 ? s->fail : s->len;
	}
	memcpy(buf, s->data, (size_t)s->len < len ? (size_t)s->len : len);
	return s->len;
}

static int store_write(void *ctx, const void *buf, size_t len)
{
	struct fake_store *s = ctx;

	if (s->fail != 0) {
		return s->fail;
	}
	memcpy(s->data, buf, len);
	s->len = (int)len;
	s->writes++;
	return (int)len;
}

void test_txn_store(void)
{
	struct fake_store fs = {.len = -ENOENT};
	struct ctag_txn_store st = {store_read, store_write, &fs};
	struct ctag_txn_record rec, back;

	CHECK(ctag_txn_load(&st, &back) == 0);
	to_record(&v_txn_boot[1].stored, &rec);
	CHECK(ctag_txn_save(&st, &rec) == 0 && fs.writes == 1u && fs.len == CTAG_TXN_RECORD_LEN);
	CHECK(ctag_txn_load(&st, &back) == 1 && same(&rec, &back));
	fs.data[10] ^= 0x01u;
	CHECK(ctag_txn_load(&st, &back) == -EBADMSG);
	fs.len = 70; /* a longer entry is not ours */
	CHECK(ctag_txn_load(&st, &back) == -EBADMSG);
	fs.fail = -EIO;
	CHECK(ctag_txn_load(&st, &back) == -EIO && ctag_txn_save(&st, &rec) == -EIO);
}

void test_txn_flow(void)
{
	const struct v_txn_rec *v = &v_txn_boot[1].stored;
	struct ctag_txn_record rec;
	struct ctag_rec_result res;

	/* REFRESH_INTENT before commit_refresh(), then a reset: boot marks it unknown. */
	ctag_txn_intent(&rec, v->tag_id, v->epoch, v->revision + 1u, 501u, v->digest);
	CHECK(rec.state == CTAG_TXN_REFRESH_INTENT && rec.status == CTAG_STATUS_OK);
	CHECK(ctag_txn_unknown_pending(&rec));
	CHECK(ctag_txn_boot(&rec) && rec.status == CTAG_STATUS_DISPLAY_STATE_UNKNOWN);
	/* The same frame again is accepted (the refresh may repeat). */
	{
		struct ctag_rec_frame_begin fb = {
			.revision = v->revision + 1u, .planes = 1u, .plane_len = 15000u};

		memcpy(fb.digest, v->digest, sizeof(fb.digest));
		CHECK(ctag_txn_frame_begin(&rec, v->epoch, &fb, 1u, 15000u) == CTAG_TXN_ACCEPT);
	}
	/* Refresh completes: DISPLAYED/OK, and the RESULT comes from the record. */
	ctag_txn_complete(&rec, CTAG_STATUS_OK);
	CHECK(rec.state == CTAG_TXN_DISPLAYED && !ctag_txn_unknown_pending(&rec));
	ctag_txn_result(&rec, 1u, &res);
	CHECK(res.update_id == 501u && res.epoch == v->epoch && res.revision == v->revision + 1u);
	CHECK(res.status == CTAG_STATUS_OK && res.flags == 1u && res.battery_mv == 0u);
	CHECK(memcmp(res.digest, v->digest, sizeof(res.digest)) == 0);
	/* A failed refresh keeps the intent. */
	ctag_txn_complete(&rec, CTAG_STATUS_OK);
	ctag_txn_intent(&rec, v->tag_id, v->epoch, v->revision + 2u, 502u, v->digest);
	ctag_txn_complete(&rec, CTAG_STATUS_REFRESH_TIMEOUT);
	CHECK(rec.state == CTAG_TXN_REFRESH_INTENT && rec.status == CTAG_STATUS_REFRESH_TIMEOUT);
}
