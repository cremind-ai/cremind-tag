/* Bridge delivery core (docs/protocol.md 3, 10); mirrors companion sim/bridge.py. */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "delivery.h"

#define SENDS_MAX (1u + CTAG_MESH_RESULT_RETRIES)
#define ASG_LEN   28u
#define HIST_LEN  52u

static bool due(uint32_t now, uint32_t at)
{
	return (int32_t)(now - at) >= 0;
}

static int find_asg(const struct dlv *d, uint32_t tag_id)
{
	int i;

	for (i = 0; i < CTAG_MAX_TAGS_PER_BRIDGE; i++) {
		if (d->asg[i].used && d->asg[i].tag_id == tag_id) {
			return i;
		}
	}
	return -1;
}

/* ---- Persistence ---- */

static void save(struct dlv *d, char kind, int idx, const void *data, size_t len)
{
	char name[8];

	if (d->env.save == NULL) {
		return;
	}
	if (idx < 0) {
		(void)snprintk(name, sizeof(name), "rs");
	} else {
		(void)snprintk(name, sizeof(name), "%c/%d", kind, idx);
	}
	(void)d->env.save(d->env.ctx, name, data, len);
}

static void save_asg(struct dlv *d, int idx)
{
	const struct dlv_assignment *a = &d->asg[idx];
	uint8_t buf[ASG_LEN] = {0};

	if (!a->used) {
		save(d, 'a', idx, NULL, 0u);
		return;
	}
	buf[0] = a->flags;
	ctag_put_le32(&buf[4], a->tag_id);
	ctag_put_le32(&buf[8], a->epoch);
	memcpy(&buf[12], a->key, sizeof(a->key));
	save(d, 'a', idx, buf, sizeof(buf));
}

static void save_hist(struct dlv *d, int idx)
{
	const struct dlv_history *h = &d->hist[idx];
	uint8_t buf[HIST_LEN] = {0};

	if (!h->valid) {
		save(d, 'h', idx, NULL, 0u);
		return;
	}
	buf[0] = h->has_result;
	buf[1] = h->status;
	ctag_put_le16(&buf[2], h->battery_mv);
	ctag_put_le32(&buf[4], h->tag_id);
	ctag_put_le32(&buf[8], h->epoch);
	ctag_put_le32(&buf[12], h->revision);
	memcpy(&buf[16], h->digest, sizeof(h->digest));
	memcpy(&buf[32], h->digest8, sizeof(h->digest8));
	ctag_put_le16(&buf[40], h->timing.wake_ms);
	ctag_put_le16(&buf[42], h->timing.suspend_ms);
	ctag_put_le16(&buf[44], h->timing.transfer_ms);
	ctag_put_le16(&buf[46], h->timing.refresh_ms);
	save(d, 'h', idx, buf, sizeof(buf));
}

static void save_seq(struct dlv *d)
{
	uint8_t buf[2];

	ctag_put_le16(buf, d->reserve_end);
	save(d, 0, -1, buf, sizeof(buf));
}

static int parse_index(const char *s)
{
	int v = 0;

	if (*s == '\0') {
		return -1;
	}
	for (; *s != '\0'; s++) {
		if (*s < '0' || *s > '9' || v > 1000) {
			return -1;
		}
		v = v * 10 + (*s - '0');
	}
	return v < CTAG_MAX_TAGS_PER_BRIDGE ? v : -1;
}

void dlv_restore(struct dlv *d, const char *name, const void *data, size_t len)
{
	const uint8_t *p = data;
	int idx;

	if (strcmp(name, "rs") == 0 && len == 2u) {
		d->reserve_end = ctag_get_le16(p);
		return;
	}
	if ((name[0] != 'a' && name[0] != 'h') || name[1] != '/') {
		return;
	}
	idx = parse_index(&name[2]);
	if (idx < 0) {
		return;
	}
	if (name[0] == 'a' && len == ASG_LEN) {
		struct dlv_assignment *a = &d->asg[idx];

		a->used = 1u;
		a->flags = p[0];
		a->tag_id = ctag_get_le32(&p[4]);
		a->epoch = ctag_get_le32(&p[8]);
		memcpy(a->key, &p[12], sizeof(a->key));
	} else if (name[0] == 'h' && len == HIST_LEN) {
		struct dlv_history *h = &d->hist[idx];

		h->valid = 1u;
		h->has_result = p[0];
		h->status = p[1];
		h->battery_mv = ctag_get_le16(&p[2]);
		h->tag_id = ctag_get_le32(&p[4]);
		h->epoch = ctag_get_le32(&p[8]);
		h->revision = ctag_get_le32(&p[12]);
		memcpy(h->digest, &p[16], sizeof(h->digest));
		memcpy(h->digest8, &p[32], sizeof(h->digest8));
		h->timing.wake_ms = ctag_get_le16(&p[40]);
		h->timing.suspend_ms = ctag_get_le16(&p[42]);
		h->timing.transfer_ms = ctag_get_le16(&p[44]);
		h->timing.refresh_ms = ctag_get_le16(&p[46]);
	}
}

/* ---- Jobs ---- */

static struct dlv_job *job_alloc(struct dlv *d)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (!d->jobs[i].used) {
			memset(&d->jobs[i], 0, sizeof(d->jobs[i]));
			d->jobs[i].used = 1u;
			d->jobs[i].slot = DLV_NO_SLOT;
			d->jobs[i].order = ++d->order;
			return &d->jobs[i];
		}
	}
	d->c.jobs_dropped++;
	return NULL;
}

static bool slot_in_use(const struct dlv *d, uint16_t slot)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (d->jobs[i].used && d->jobs[i].slot == slot) {
			return true;
		}
	}
	return false;
}

/* The next free ring slot, round-robin (wear levelling). */
static uint16_t ring_alloc(struct dlv *d)
{
	uint16_t n = d->env.flash->geom.pending_slots;
	uint16_t i;

	for (i = 0u; i < n; i++) {
		uint16_t s = (uint16_t)((d->ring_head + i) % n);

		if (!slot_in_use(d, s)) {
			d->ring_head = (uint16_t)((s + 1u) % n);
			return s;
		}
	}
	return DLV_NO_SLOT;
}

static void release_slot(struct dlv *d, struct dlv_job *job)
{
	if (job->slot != DLV_NO_SLOT && d->env.flash_ok) {
		if (bflash_pending_consume(d->env.flash, job->slot) != 0) {
			d->c.storage_errors++;
		}
	}
	job->slot = DLV_NO_SLOT;
}

static struct dlv_job *find_layout_job(struct dlv *d, uint32_t tag_id, uint32_t epoch,
				       uint32_t revision)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->kind == DLV_LAYOUT && j->tag_id == tag_id && j->epoch == epoch &&
		    j->revision == revision) {
			return j;
		}
	}
	return NULL;
}

/* Write a layout job's record (from buf) into a fresh ring slot. */
static int store_layout(struct dlv *d, struct dlv_job *job, const uint8_t *buf)
{
	struct bflash_pending h = {
		.seq = d->pending_seq + 1u,
		.tag_id = job->tag_id,
		.epoch = job->epoch,
		.revision = job->revision,
		.update_id = job->update_id,
		.len = job->len,
	};
	uint16_t slot;
	int err;

	if (!d->env.flash_ok) {
		return -ENODEV;
	}
	slot = ring_alloc(d);
	if (slot == DLV_NO_SLOT) {
		return -ENOSPC;
	}
	memcpy(h.fontpack_id, job->fontpack_id, sizeof(h.fontpack_id));
	memcpy(h.digest, job->digest, sizeof(h.digest));
	err = bflash_pending_write(d->env.flash, slot, &h, buf);
	if (err != 0) {
		d->c.storage_errors++;
		return err;
	}
	d->pending_seq = h.seq;
	release_slot(d, job); /* the previous record of this job, if any */
	job->slot = slot;
	return 0;
}

/* ---- Results ---- */

static uint16_t alloc_seq(struct dlv *d)
{
	if (d->next_seq == d->reserve_end) {
		d->reserve_end = (uint16_t)(d->next_seq + DLV_SEQ_RESERVE);
		save_seq(d);
	}
	return d->next_seq++;
}

static void send_msg(struct dlv *d, uint8_t op, const uint8_t *buf, int len)
{
	if (len > 0 && d->env.send != NULL) {
		d->env.send(d->env.ctx, op, buf, (size_t)len);
	}
}

static void send_result_msg(struct dlv *d, const struct ctag_mesh_delivery_result *m)
{
	uint8_t buf[CTAG_MESH_DELIVERY_RESULT_LEN];

	send_msg(d, CTAG_MESH_OP_DELIVERY_RESULT, buf,
		 ctag_mesh_delivery_result_pack(m, buf, sizeof(buf)));
}

static void send_result(struct dlv *d, uint64_t update_id, uint32_t tag_id, uint32_t epoch,
			uint32_t revision, uint8_t status, const uint8_t *digest8,
			uint16_t battery_mv, const struct dlv_timing *t, uint32_t now)
{
	struct dlv_result *r = NULL;
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		if (!d->results[i].used) {
			r = &d->results[i];
			break;
		}
		if (r == NULL || d->results[i].sends > r->sends) {
			r = &d->results[i];
		}
	}
	if (r->used) {
		d->c.results_dropped++; /* the one closest to giving up anyway */
	}
	memset(r, 0, sizeof(*r));
	r->used = 1u;
	r->msg.result_seq = alloc_seq(d);
	r->msg.update_id = update_id;
	r->msg.tag_id = tag_id;
	r->msg.epoch = epoch;
	r->msg.revision = revision;
	r->msg.status = status;
	if (digest8 != NULL) {
		memcpy(r->msg.digest, digest8, sizeof(r->msg.digest));
	}
	r->msg.battery_mv = battery_mv;
	if (t != NULL) {
		r->msg.wake_ms = t->wake_ms;
		r->msg.suspend_ms = t->suspend_ms;
		r->msg.transfer_ms = t->transfer_ms;
		r->msg.refresh_ms = t->refresh_ms;
	}
	d->c.results++;
	send_result_msg(d, &r->msg);
	r->sends = 1u;
	r->due_ms = now + CTAG_MESH_RESULT_RETRY_MS;
}

uint32_t dlv_tick(struct dlv *d, uint32_t now)
{
	uint32_t next = UINT32_MAX;
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		struct dlv_result *r = &d->results[i];

		if (!r->used) {
			continue;
		}
		if (due(now, r->due_ms)) {
			if (r->sends >= SENDS_MAX) {
				r->used = 0u;
				d->c.results_unacked++;
				continue;
			}
			d->c.result_resends++;
			send_result_msg(d, &r->msg);
			r->sends++;
			r->due_ms = now + CTAG_MESH_RESULT_RETRY_MS;
		}
		next = MIN(next, r->due_ms - now);
	}
	return next;
}

void dlv_result_ack(struct dlv *d, uint16_t result_seq)
{
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		if (d->results[i].used && d->results[i].msg.result_seq == result_seq) {
			d->results[i].used = 0u;
		}
	}
}

void dlv_finish(struct dlv *d, struct dlv_job *job, uint8_t status, const uint8_t digest8[8],
		uint16_t battery_mv, const struct dlv_timing *timing, uint32_t now)
{
	struct dlv_job j = *job;
	int idx = find_asg(d, j.tag_id);

	release_slot(d, job);
	job->used = 0u;
	if (idx >= 0) {
		struct dlv_history *h = &d->hist[idx];

		if (j.kind == DLV_LAYOUT && h->valid && h->tag_id == j.tag_id &&
		    h->epoch == j.epoch && h->revision == j.revision &&
		    memcmp(h->digest, j.digest, sizeof(h->digest)) == 0) {
			h->has_result = 1u;
			h->status = status;
			memset(h->digest8, 0, sizeof(h->digest8));
			if (digest8 != NULL) {
				memcpy(h->digest8, digest8, sizeof(h->digest8));
			}
			h->battery_mv = battery_mv;
			memset(&h->timing, 0, sizeof(h->timing));
			if (timing != NULL) {
				h->timing = *timing;
			}
			save_hist(d, idx);
		} else if (j.kind == DLV_CMD && j.cmd == CTAG_TAG_CMD_CLEAR &&
			   status == CTAG_STATUS_OK) {
			/* The tag now stores revision 0 for this epoch (5.6); mirror it so a
			 * re-delivery of the previously shown revision is drawn again (10). */
			memset(h, 0, sizeof(*h));
			h->valid = 1u;
			h->tag_id = j.tag_id;
			h->epoch = j.epoch;
			save_hist(d, idx);
		}
	}
	send_result(d, j.update_id, j.tag_id, j.epoch, j.revision, status, digest8, battery_mv,
		    timing, now);
}

static void cancel_tag(struct dlv *d, uint32_t tag_id, uint32_t older_than, uint32_t now)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->tag_id == tag_id && j->epoch < older_than &&
		    !(j->flags & DLV_JOB_IN_SESSION)) {
			d->c.cancelled++;
			dlv_finish(d, j, CTAG_STATUS_CANCELLED, NULL, 0u, NULL, now);
		}
	}
}

/* ---- Lifecycle ---- */

void dlv_init(struct dlv *d, const struct dlv_env *env)
{
	memset(d, 0, sizeof(*d));
	d->env = *env;
	d->env.flash_ok = env->flash_ok && env->flash != NULL && env->flash->geom.pending_slots > 0u;
	ctag_layout_asm_init(&d->asm_, d->asm_buf, sizeof(d->asm_buf));
	d->next_seq = 1u;
}

/* Restored records: renumber in their write order; drop an older record of
 * the same revision (a rewrite that adopted a newer update_id). */
static void restore_order(struct dlv *d)
{
	size_t i, k;

	for (;;) {
		struct dlv_job *best = NULL;

		for (i = 0; i < DLV_MAX_JOBS; i++) {
			struct dlv_job *j = &d->jobs[i];

			if (j->used && (j->flags & 0x80u) && (best == NULL || j->order < best->order)) {
				best = j;
			}
		}
		if (best == NULL) {
			break;
		}
		best->flags &= (uint8_t)~0x80u;
		for (k = 0; k < DLV_MAX_JOBS; k++) {
			struct dlv_job *o = &d->jobs[k];

			if (o != best && o->used && (o->flags & 0x80u) && o->tag_id == best->tag_id &&
			    o->epoch == best->epoch && o->revision == best->revision) {
				release_slot(d, best);
				best->used = 0u;
				break;
			}
		}
		if (best->used) {
			best->order = ++d->order;
		}
	}
}

void dlv_start(struct dlv *d, uint32_t now, uint8_t *scratch, size_t size)
{
	struct bflash_pending h;
	uint32_t max_seq = 0u;
	uint16_t head = 0u, s;
	int i;

	d->next_seq = d->reserve_end != 0u ? d->reserve_end : 1u;
	d->reserve_end = (uint16_t)(d->next_seq + DLV_SEQ_RESERVE);
	save_seq(d);
	for (i = 0; i < CTAG_MAX_TAGS_PER_BRIDGE; i++) {
		if (d->hist[i].valid && (!d->asg[i].used || d->hist[i].tag_id != d->asg[i].tag_id)) {
			memset(&d->hist[i], 0, sizeof(d->hist[i]));
			save_hist(d, i);
		}
	}
	if (!d->env.flash_ok) {
		return;
	}
	for (s = 0u; s < d->env.flash->geom.pending_slots; s++) {
		const struct dlv_history *hi;
		struct dlv_job *job;
		int st = bflash_pending_peek(d->env.flash, s, &h);
		bool keep;

		if (st <= 0) {
			continue;
		}
		if (h.seq >= max_seq) {
			max_seq = h.seq;
			head = (uint16_t)((s + 1u) % d->env.flash->geom.pending_slots);
		}
		if (st != BFLASH_PENDING_LIVE) {
			continue;
		}
		i = find_asg(d, h.tag_id);
		keep = i >= 0 && d->asg[i].epoch == h.epoch;
		if (keep) {
			hi = &d->hist[i];
			/* Finished before the reset (its result was persisted first). */
			keep = !(hi->valid && hi->has_result && hi->epoch == h.epoch &&
				 hi->revision == h.revision &&
				 memcmp(hi->digest, h.digest, sizeof(h.digest)) == 0);
		}
		job = keep && bflash_pending_read(d->env.flash, s, &h, scratch, size) == 1
			      ? job_alloc(d)
			      : NULL;
		if (job == NULL) {
			(void)bflash_pending_consume(d->env.flash, s);
			continue;
		}
		job->kind = DLV_LAYOUT;
		job->flags = 0x80u; /* restored: ordered below */
		job->slot = s;
		job->len = h.len;
		job->order = h.seq;
		job->tag_id = h.tag_id;
		job->epoch = h.epoch;
		job->revision = h.revision;
		job->update_id = h.update_id;
		job->validated_ms = now;
		memcpy(job->digest, h.digest, sizeof(job->digest));
		memcpy(job->fontpack_id, h.fontpack_id, sizeof(job->fontpack_id));
	}
	d->pending_seq = max_seq;
	d->ring_head = head;
	d->order = 0u;
	restore_order(d);
}

void dlv_reset(struct dlv *d)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (d->jobs[i].used) {
			release_slot(d, &d->jobs[i]);
			d->jobs[i].used = 0u;
		}
	}
	for (i = 0; i < CTAG_MAX_TAGS_PER_BRIDGE; i++) {
		memset(&d->asg[i], 0, sizeof(d->asg[i]));
		memset(&d->hist[i], 0, sizeof(d->hist[i]));
		save_asg(d, (int)i);
		save_hist(d, (int)i);
	}
	memset(d->results, 0, sizeof(d->results));
	ctag_layout_asm_cancel(&d->asm_);
}

/* ---- LAYOUT_SRV ---- */

void dlv_layout_begin(struct dlv *d, const struct ctag_mesh_layout_begin *b)
{
	ctag_layout_asm_begin(&d->asm_, b);
}

void dlv_layout_chunk(struct dlv *d, const struct ctag_mesh_layout_chunk *c)
{
	if (ctag_layout_asm_chunk(&d->asm_, c) != CTAG_STATUS_OK) {
		d->c.stray_chunks++;
	}
}

/*
 * Same revision and digest (3.3, 10): a displayed revision re-sends its stored
 * result under the new update_id; a pending one adopts the new update_id.
 * False: the revision ended without being displayed and is accepted again.
 */
static bool duplicate(struct dlv *d, int idx, const struct ctag_mesh_layout_begin *b, uint32_t now)
{
	const struct dlv_history *h = &d->hist[idx];
	struct dlv_job *job;

	if (h->has_result && h->status == CTAG_STATUS_OK) {
		d->c.duplicates++;
		send_result(d, b->update_id, b->tag_id, b->epoch, b->revision, CTAG_STATUS_OK,
			    h->digest8, h->battery_mv, &h->timing, now);
		return true;
	}
	job = find_layout_job(d, b->tag_id, b->epoch, b->revision);
	if (job == NULL) {
		return false;
	}
	d->c.duplicates++;
	if (job->update_id != b->update_id) {
		job->update_id = b->update_id;
		/* Persist the adoption (best effort) unless a session holds the record. */
		if (!(job->flags & DLV_JOB_IN_SESSION) && job->len == b->total_len) {
			(void)store_layout(d, job, d->asm_buf);
		}
	}
	return true;
}

static uint8_t accept(struct dlv *d, int idx, const struct ctag_mesh_layout_begin *b, uint32_t now)
{
	struct dlv_history *h = &d->hist[idx];
	struct dlv_job *job;
	size_t i;

	memset(h, 0, sizeof(*h));
	h->valid = 1u;
	h->tag_id = b->tag_id;
	h->epoch = b->epoch;
	h->revision = b->revision;
	memcpy(h->digest, b->digest, sizeof(h->digest));
	/* An older pending layout of the tag is replaced (a session keeps its own). */
	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->kind == DLV_LAYOUT && j->tag_id == b->tag_id &&
		    !(j->flags & DLV_JOB_IN_SESSION)) {
			d->c.superseded++;
			dlv_finish(d, j, CTAG_STATUS_SUPERSEDED, NULL, 0u, NULL, now);
		}
	}
	job = job_alloc(d);
	if (job == NULL) {
		return CTAG_STATUS_NO_RESOURCES;
	}
	job->kind = DLV_LAYOUT;
	job->tag_id = b->tag_id;
	job->epoch = b->epoch;
	job->revision = b->revision;
	job->update_id = b->update_id;
	job->len = b->total_len;
	job->validated_ms = now;
	memcpy(job->digest, b->digest, sizeof(job->digest));
	memcpy(job->fontpack_id, b->fontpack_id, sizeof(job->fontpack_id));
	if (store_layout(d, job, d->asm_buf) != 0) {
		job->used = 0u;
		return CTAG_STATUS_STORAGE_ERROR;
	}
	save_hist(d, idx);
	d->c.layouts_accepted++;
	return CTAG_STATUS_OK;
}

uint8_t dlv_layout_commit(struct dlv *d, uint16_t xfer_id, uint32_t now, uint32_t *missing)
{
	const struct ctag_mesh_layout_begin *b = &d->asm_.begin;
	const struct dlv_history *h;
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	uint8_t st = ctag_layout_asm_commit(&d->asm_, xfer_id, missing, d->env.sha);
	int idx;

	if (st != CTAG_STATUS_OK) {
		if (st == CTAG_STATUS_INCOMPLETE) {
			d->c.incomplete++;
		}
		return st;
	}
	idx = find_asg(d, b->tag_id);
	if (idx < 0 || d->asg[idx].epoch < b->epoch) {
		return CTAG_STATUS_NOT_ASSIGNED;
	}
	if (d->asg[idx].epoch > b->epoch) {
		return CTAG_STATUS_STALE_EPOCH;
	}
	h = &d->hist[idx];
	if (h->valid && h->tag_id == b->tag_id && h->epoch == b->epoch &&
	    b->revision <= h->revision) {
		if (b->revision != h->revision ||
		    memcmp(b->digest, h->digest, sizeof(h->digest)) != 0) {
			return CTAG_STATUS_STALE_REVISION;
		}
		if (duplicate(d, idx, b, now)) {
			return CTAG_STATUS_DUPLICATE;
		}
	}
	if (d->env.fonts == NULL || !fontstore_active_id(d->env.fonts, id) ||
	    memcmp(id, b->fontpack_id, sizeof(id)) != 0) {
		return CTAG_STATUS_FONTPACK_MISMATCH;
	}
	st = ctag_layout_validate(d->asm_buf, b->total_len, fontstore_has_strike, d->env.fonts);
	return st != CTAG_STATUS_OK ? st : accept(d, idx, b, now);
}

void dlv_layout_cancel(struct dlv *d, uint64_t update_id, uint32_t now)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->update_id == update_id && !(j->flags & DLV_JOB_IN_SESSION)) {
			d->c.cancelled++;
			dlv_finish(d, j, CTAG_STATUS_CANCELLED, NULL, 0u, NULL, now);
			return;
		}
	}
}

/* ---- MGMT_SRV ---- */

uint8_t dlv_assign_set(struct dlv *d, const struct ctag_mesh_assign_set *m, uint32_t now)
{
	int idx = find_asg(d, m->tag_id);
	int i;

	if (idx >= 0 && d->asg[idx].epoch > m->epoch) {
		return CTAG_STATUS_STALE_EPOCH;
	}
	if (idx < 0) {
		for (i = 0; i < CTAG_MAX_TAGS_PER_BRIDGE && idx < 0; i++) {
			if (!d->asg[i].used) {
				idx = i;
			}
		}
		if (idx < 0) {
			return CTAG_STATUS_NO_RESOURCES;
		}
		if (d->hist[idx].valid) {
			memset(&d->hist[idx], 0, sizeof(d->hist[idx]));
			save_hist(d, idx);
		}
		d->battery[idx] = 0u;
	} else if (d->asg[idx].epoch < m->epoch) {
		cancel_tag(d, m->tag_id, m->epoch, now);
	}
	d->asg[idx].used = 1u;
	d->asg[idx].flags = m->flags;
	d->asg[idx].tag_id = m->tag_id;
	d->asg[idx].epoch = m->epoch;
	memcpy(d->asg[idx].key, m->key, sizeof(d->asg[idx].key));
	save_asg(d, idx);
	return CTAG_STATUS_OK;
}

uint8_t dlv_assign_del(struct dlv *d, const struct ctag_mesh_assign_del *m, uint32_t now)
{
	int idx = find_asg(d, m->tag_id);

	if (idx < 0) {
		return CTAG_STATUS_OK; /* 10: idempotent */
	}
	if (d->asg[idx].epoch > m->epoch) {
		return CTAG_STATUS_STALE_EPOCH;
	}
	memset(&d->asg[idx], 0, sizeof(d->asg[idx]));
	memset(&d->hist[idx], 0, sizeof(d->hist[idx]));
	save_asg(d, idx);
	save_hist(d, idx);
	cancel_tag(d, m->tag_id, m->epoch + 1u, now);
	return CTAG_STATUS_OK;
}

void dlv_tag_cmd(struct dlv *d, const struct ctag_mesh_tag_cmd *m, uint32_t now)
{
	int idx = find_asg(d, m->tag_id);
	uint8_t st = CTAG_STATUS_OK;
	struct dlv_job *job;

	if (idx < 0 || d->asg[idx].epoch < m->epoch) {
		st = CTAG_STATUS_NOT_ASSIGNED;
	} else if (d->asg[idx].epoch > m->epoch) {
		st = CTAG_STATUS_STALE_EPOCH;
	} else if (m->cmd != CTAG_TAG_CMD_CLEAR && m->cmd != CTAG_TAG_CMD_SLEEP) {
		/* IDENTIFY / REFRESH are companion-level (a new revision, 5.6, 10). */
		st = CTAG_STATUS_UNSUPPORTED;
	}
	job = st == CTAG_STATUS_OK ? job_alloc(d) : NULL;
	if (job == NULL) {
		send_result(d, m->update_id, m->tag_id, m->epoch, 0u,
			    st == CTAG_STATUS_OK ? CTAG_STATUS_NO_RESOURCES : st, NULL, 0u, NULL, now);
		return;
	}
	job->kind = DLV_CMD;
	job->cmd = m->cmd;
	job->tag_id = m->tag_id;
	job->epoch = m->epoch;
	job->update_id = m->update_id;
	job->validated_ms = now;
}

uint8_t dlv_assigned_count(const struct dlv *d)
{
	uint8_t n = 0u;
	size_t i;

	for (i = 0; i < CTAG_MAX_TAGS_PER_BRIDGE; i++) {
		n += d->asg[i].used ? 1u : 0u;
	}
	return n;
}

uint8_t dlv_queue_depth(const struct dlv *d)
{
	size_t n = 0u, i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		n += d->jobs[i].used ? 1u : 0u;
	}
	return (uint8_t)MIN(n, 255u);
}

/* ---- Scheduler and session ---- */

const struct dlv_assignment *dlv_assignment(const struct dlv *d, uint32_t tag_id)
{
	int idx = find_asg(d, tag_id);

	return idx < 0 ? NULL : &d->asg[idx];
}

bool dlv_has_work(const struct dlv *d, uint32_t tag_id)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (d->jobs[i].used && d->jobs[i].tag_id == tag_id) {
			return true;
		}
	}
	return false;
}

uint32_t dlv_last_order(const struct dlv *d)
{
	return d->order;
}

struct dlv_job *dlv_next_job(struct dlv *d, uint32_t tag_id, uint32_t after, uint32_t upto)
{
	struct dlv_job *best = NULL;
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->tag_id == tag_id && j->order > after && j->order <= upto &&
		    (best == NULL || j->order < best->order)) {
			best = j;
		}
	}
	return best;
}

int dlv_job_layout(struct dlv *d, const struct dlv_job *job, uint8_t *buf, size_t size)
{
	struct bflash_pending h;
	int st;

	if (job->kind != DLV_LAYOUT || job->slot == DLV_NO_SLOT || !d->env.flash_ok) {
		return -ENOENT;
	}
	st = bflash_pending_read(d->env.flash, job->slot, &h, buf, size);
	if (st < 0) {
		return st;
	}
	if (st != 1 || h.tag_id != job->tag_id || h.epoch != job->epoch ||
	    h.revision != job->revision || h.len != job->len ||
	    memcmp(h.digest, job->digest, sizeof(h.digest)) != 0) {
		return -EBADMSG;
	}
	return h.len;
}

void dlv_stage(struct dlv *d, const struct dlv_job *job, uint8_t stage)
{
	struct ctag_mesh_delivery_stage m = {
		.update_id = job->update_id,
		.tag_id = job->tag_id,
		.revision = job->revision,
		.stage = stage,
	};
	uint8_t buf[CTAG_MESH_DELIVERY_STAGE_LEN];

	send_msg(d, CTAG_MESH_OP_DELIVERY_STAGE, buf,
		 ctag_mesh_delivery_stage_pack(&m, buf, sizeof(buf)));
}

void dlv_fail_epoch(struct dlv *d, uint32_t tag_id, uint32_t epoch, uint8_t status, uint32_t now)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->tag_id == tag_id && j->epoch == epoch) {
			dlv_finish(d, j, status, NULL, 0u, NULL, now);
		}
	}
}

void dlv_note_battery(struct dlv *d, uint32_t tag_id, uint16_t mv)
{
	int idx = find_asg(d, tag_id);

	if (idx >= 0) {
		d->battery[idx] = mv;
	}
}

uint16_t dlv_battery(const struct dlv *d, uint32_t tag_id)
{
	int idx = find_asg(d, tag_id);

	return idx < 0 ? 0u : d->battery[idx];
}
