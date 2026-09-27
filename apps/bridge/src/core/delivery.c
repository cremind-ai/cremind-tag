/* Bridge delivery core (docs/protocol.md 3, 10); mirrors companion sim/bridge.py. */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "delivery.h"

#define SENDS_MAX (1u + CTAG_MESH_RESULT_RETRIES)
#define ASG_LEN   28u
#define HIST_LEN  64u
#define NEW_SEQ   (-1)
#define RESTORED  0x80u /* job flag during dlv_start: ordered by restore_order() */

/* Every layout job can hold a ring slot while a transfer takes one more. */
BUILD_ASSERT(DLV_MAX_JOBS < BFLASH_PENDING_MIN, "pending ring smaller than the job table");
BUILD_ASSERT(DLV_MAX_TAGS >= 1 && DLV_MAX_TAGS <= CTAG_MAX_TAGS_PER_BRIDGE, "assignment table");

static bool due(uint32_t now, uint32_t at)
{
	return (int32_t)(now - at) >= 0;
}

static int find_asg(const struct dlv *d, uint32_t tag_id)
{
	int i;

	for (i = 0; i < DLV_MAX_TAGS; i++) {
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
	ctag_put_le64(&buf[48], h->update_id);
	ctag_put_le16(&buf[56], h->result_seq);
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
	return v < DLV_MAX_TAGS ? v : -1;
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
		h->update_id = ctag_get_le64(&p[48]);
		h->result_seq = ctag_get_le16(&p[56]);
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

static void job_free(struct dlv *d, struct dlv_job *job)
{
	if (d->lb_job == job) {
		d->lb_job = NULL;
	}
	job->used = 0u;
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

static struct dlv_job *find_update_job(struct dlv *d, uint64_t update_id)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (d->jobs[i].used && d->jobs[i].update_id == update_id) {
			return &d->jobs[i];
		}
	}
	return NULL;
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

static void send_result_msg(struct dlv *d, struct dlv_result *r, uint32_t now)
{
	uint8_t buf[CTAG_MESH_DELIVERY_RESULT_LEN];

	send_msg(d, CTAG_MESH_OP_DELIVERY_RESULT, buf,
		 ctag_mesh_delivery_result_pack(&r->msg, buf, sizeof(buf)));
	r->sends++;
	r->due_ms = now + CTAG_MESH_RESULT_RETRY_MS;
}

static struct dlv_result *result_find(struct dlv *d, uint64_t update_id)
{
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		if (d->results[i].state != DLV_RES_FREE && d->results[i].msg.update_id == update_id) {
			return &d->results[i];
		}
	}
	return NULL;
}

/* The result_seq of update_id's result: the one it already has, else a new one. */
static uint16_t result_seq_for(struct dlv *d, uint64_t update_id)
{
	const struct dlv_result *r = result_find(d, update_id);

	return r != NULL ? r->msg.result_seq : alloc_seq(d);
}

/* A table entry for a new result: a free one, else the oldest finished one
 * (acknowledged or given up), else the one closest to giving up. */
static struct dlv_result *result_slot(struct dlv *d)
{
	struct dlv_result *best = NULL;
	uint16_t best_age = 0u;
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		struct dlv_result *r = &d->results[i];
		uint16_t age = (uint16_t)(d->next_seq - r->msg.result_seq);

		if (r->state == DLV_RES_FREE) {
			return r;
		}
		if (r->state != DLV_RES_SENDING && (best == NULL || best->state == DLV_RES_SENDING ||
						    age > best_age)) {
			best = r;
			best_age = age;
		} else if (r->state == DLV_RES_SENDING && (best == NULL ||
			   (best->state == DLV_RES_SENDING && r->sends > best->sends))) {
			best = r;
		}
	}
	if (best->state == DLV_RES_SENDING) {
		d->c.results_dropped++;
	}
	return best;
}

/*
 * DELIVERY_RESULT for update_id (seq: its result_seq, or NEW_SEQ). 10: one
 * result_seq per update_id. When the table already has a result for it, that
 * first result stands: it is sent again under its own result_seq (with a
 * fresh retry budget) unless its re-sends are still running.
 */
static void send_result(struct dlv *d, uint64_t update_id, uint32_t tag_id, uint32_t epoch,
			uint32_t revision, uint8_t status, const uint8_t *digest8,
			uint16_t battery_mv, const struct dlv_timing *t, int32_t seq, uint32_t now)
{
	struct dlv_result *r = result_find(d, update_id);

	if (r != NULL) {
		d->c.results_repeated++;
		if (r->state != DLV_RES_SENDING) {
			r->state = DLV_RES_SENDING;
			r->sends = 0u;
			send_result_msg(d, r, now);
		}
		return;
	}
	r = result_slot(d);
	memset(r, 0, sizeof(*r));
	r->state = DLV_RES_SENDING;
	r->msg.result_seq = seq >= 0 ? (uint16_t)seq : alloc_seq(d);
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
	send_result_msg(d, r, now);
}

uint32_t dlv_tick(struct dlv *d, uint32_t now)
{
	uint32_t next = UINT32_MAX;
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		struct dlv_result *r = &d->results[i];

		if (r->state != DLV_RES_SENDING) {
			continue;
		}
		if (due(now, r->due_ms)) {
			if (r->sends >= SENDS_MAX) {
				r->state = DLV_RES_GAVE_UP; /* kept: its update_id keeps its seq */
				d->c.results_unacked++;
				continue;
			}
			d->c.result_resends++;
			send_result_msg(d, r, now);
		}
		next = MIN(next, r->due_ms - now);
	}
	return next;
}

void dlv_result_ack(struct dlv *d, uint16_t result_seq)
{
	size_t i;

	for (i = 0; i < DLV_RESULT_SLOTS; i++) {
		if (d->results[i].state != DLV_RES_FREE &&
		    d->results[i].msg.result_seq == result_seq) {
			d->results[i].state = DLV_RES_ACKED;
		}
	}
}

/*
 * A job's final result. Before the DELIVERY_RESULT goes out, whatever keeps
 * the layout from being delivered again after a reset is durable: the
 * history's stored result for the revision it records (its pending record,
 * consumed afterwards, is then dropped at boot), else the consumed record.
 */
void dlv_finish(struct dlv *d, struct dlv_job *job, uint8_t status, const uint8_t digest8[8],
		uint16_t battery_mv, const struct dlv_timing *timing, uint32_t now)
{
	struct dlv_job j = *job;
	int idx = find_asg(d, j.tag_id);
	struct dlv_history *h = idx >= 0 ? &d->hist[idx] : NULL;
	bool recorded = h != NULL && j.kind == DLV_LAYOUT && h->valid && h->tag_id == j.tag_id &&
			h->epoch == j.epoch && h->revision == j.revision &&
			memcmp(h->digest, j.digest, sizeof(h->digest)) == 0;
	int32_t seq = NEW_SEQ;

	if (!recorded) {
		release_slot(d, job);
	}
	if (h != NULL) {
		if (recorded) {
			/* Persisted before it is sent: the result and its seq. */
			seq = result_seq_for(d, j.update_id);
			h->has_result = 1u;
			h->status = status;
			h->update_id = j.update_id;
			h->result_seq = (uint16_t)seq;
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
		    timing, seq, now);
	release_slot(d, job);
	job_free(d, job);
}

/* CANCELLED for the tag's jobs of epoch <= upto (not one in a session:
 * dlv_session_over() ends it when its session does). */
static void cancel_tag(struct dlv *d, uint32_t tag_id, uint32_t upto, uint32_t now)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->tag_id == tag_id && j->epoch <= upto &&
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
	d->x.slot = DLV_NO_SLOT;
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

			if (j->used && (j->flags & RESTORED) &&
			    (best == NULL || j->order < best->order)) {
				best = j;
			}
		}
		if (best == NULL) {
			break;
		}
		best->flags &= (uint8_t)~RESTORED;
		for (k = 0; k < DLV_MAX_JOBS; k++) {
			struct dlv_job *o = &d->jobs[k];

			if (o != best && o->used && (o->flags & RESTORED) &&
			    o->tag_id == best->tag_id && o->epoch == best->epoch &&
			    o->revision == best->revision) {
				release_slot(d, best);
				job_free(d, best);
				break;
			}
		}
		if (best->used) {
			best->order = ++d->order;
		}
	}
}

void dlv_start(struct dlv *d, uint32_t now)
{
	struct bflash_pending h;
	uint32_t max_seq = 0u;
	uint16_t head = 0u, newest = DLV_NO_SLOT, s;
	int i;

	d->next_seq = d->reserve_end != 0u ? d->reserve_end : 1u;
	d->reserve_end = (uint16_t)(d->next_seq + DLV_SEQ_RESERVE);
	save_seq(d);
	for (i = 0; i < DLV_MAX_TAGS; i++) {
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
		if (newest == DLV_NO_SLOT || h.seq >= max_seq) {
			newest = s;
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
		/* The shared buffer is free at boot: the CRC check reads into it. */
		job = keep && bflash_pending_read(d->env.flash, s, &h, d->layout,
						  sizeof(d->layout)) == 1
			      ? job_alloc(d)
			      : NULL;
		if (job == NULL) {
			(void)bflash_pending_consume(d->env.flash, s);
			continue;
		}
		job->kind = DLV_LAYOUT;
		job->flags = RESTORED;
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
	/* The newest intact record names the last accepted transfer: a repeated
	 * commit of it answers DUPLICATE after the reset too (10). A torn one
	 * (power lost while sealing) was answered STORAGE_ERROR. */
	if (newest != DLV_NO_SLOT && bflash_pending_check(d->env.flash, newest, &h, d->layout,
							  sizeof(d->layout)) > 0) {
		d->x.begin.xfer_id = h.xfer_id;
		d->x.accepted = true;
	}
	d->pending_seq = max_seq;
	d->ring_head = head;
	d->order = 0u;
	d->lb_job = NULL;
	restore_order(d);
}

void dlv_reset(struct dlv *d)
{
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (d->jobs[i].used) {
			release_slot(d, &d->jobs[i]);
			job_free(d, &d->jobs[i]);
		}
	}
	for (i = 0; i < DLV_MAX_TAGS; i++) {
		memset(&d->asg[i], 0, sizeof(d->asg[i]));
		memset(&d->hist[i], 0, sizeof(d->hist[i]));
		save_asg(d, (int)i);
		save_hist(d, (int)i);
	}
	memset(d->results, 0, sizeof(d->results));
	memset(&d->x, 0, sizeof(d->x));
	d->x.slot = DLV_NO_SLOT;
}

/* ---- LAYOUT_SRV ---- */

void dlv_layout_begin(struct dlv *d, const struct ctag_mesh_layout_begin *b)
{
	struct dlv_xfer *x = &d->x;
	uint16_t slot;

	memset(x, 0, sizeof(*x));
	x->begin = *b;
	x->active = true;
	x->slot = DLV_NO_SLOT;
	/* A transfer that fails TOO_LARGE / INVALID on its length needs no slot. */
	if (!d->env.flash_ok || b->total_len == 0u || b->total_len > CTAG_LAYOUT_HARD_MAX) {
		return;
	}
	slot = ring_alloc(d);
	if (slot == DLV_NO_SLOT || bflash_pending_erase(d->env.flash, slot) != 0) {
		d->c.storage_errors++; /* the commit answers STORAGE_ERROR */
		return;
	}
	x->slot = slot;
}

void dlv_layout_chunk(struct dlv *d, const struct ctag_mesh_layout_chunk *c)
{
	struct dlv_xfer *x = &d->x;
	uint32_t bit;

	if (!x->active || c->xfer_id != x->begin.xfer_id || c->index >= x->begin.chunk_count ||
	    c->index >= BFLASH_PENDING_CHUNKS) {
		d->c.stray_chunks++; /* 3.3: another transfer, or an index outside it */
		return;
	}
	bit = (uint32_t)1u << c->index;
	if (x->have & bit) {
		return; /* a repeat: the chunk is in flash already */
	}
	if (c->index + 1u == x->begin.chunk_count) {
		x->last_len = (uint16_t)c->data_len;
	} else if (c->data_len != CTAG_LAYOUT_CHUNK_DATA_MAX) {
		x->bad = true; /* 3.2 rule 2: every chunk but the last is full */
	}
	/* Chunks past LAYOUT_HARD_MAX belong to a TOO_LARGE transfer: not stored. */
	if (x->slot != DLV_NO_SLOT && c->data_len > 0u &&
	    (size_t)c->index * CTAG_LAYOUT_CHUNK_DATA_MAX + c->data_len <= CTAG_LAYOUT_HARD_MAX &&
	    bflash_pending_put(d->env.flash, x->slot, c->index, c->data, c->data_len) != 0) {
		d->c.storage_errors++;
		x->slot = DLV_NO_SLOT;
	}
	x->have |= bit;
}

/*
 * The transfer into the shared buffer (the session's copy is gone until it
 * reloads) and its digest (3.3): OK, STORAGE_ERROR, INTERNAL or
 * DIGEST_MISMATCH.
 */
static uint8_t load_xfer(struct dlv *d, size_t len)
{
	const struct ctag_sha256_ops *sha = d->env.sha;
	uint8_t digest[32];

	d->lb_job = NULL;
	if (len > 0u) {
		if (d->x.slot == DLV_NO_SLOT) {
			return CTAG_STATUS_STORAGE_ERROR;
		}
		if (bflash_pending_body(d->env.flash, d->x.slot, d->layout, len) != 0) {
			d->c.storage_errors++;
			return CTAG_STATUS_STORAGE_ERROR;
		}
	}
	if (sha->init(sha->ctx) != 0 || sha->update(sha->ctx, d->layout, len) != 0 ||
	    sha->finish(sha->ctx, digest) != 0) {
		return CTAG_STATUS_INTERNAL;
	}
	return memcmp(digest, d->x.begin.digest, CTAG_LAYOUT_DIGEST_LEN) == 0
		       ? CTAG_STATUS_OK
		       : CTAG_STATUS_DIGEST_MISMATCH;
}

/*
 * Seal the accepted transfer's slot (header last; the layout is in the shared
 * buffer). With a job the record becomes the job's and its previous one is
 * consumed; without, it is consumed at once and only records, as the newest
 * record, which transfer was accepted last.
 */
static int seal_xfer(struct dlv *d, struct dlv_job *job)
{
	const struct ctag_mesh_layout_begin *b = &d->x.begin;
	struct bflash_pending h = {
		.seq = d->pending_seq + 1u,
		.tag_id = b->tag_id,
		.epoch = b->epoch,
		.revision = b->revision,
		.update_id = job != NULL ? job->update_id : b->update_id,
		.len = b->total_len,
		.xfer_id = b->xfer_id,
	};
	uint16_t slot = d->x.slot;
	int err;

	if (slot == DLV_NO_SLOT) {
		return -ENODEV;
	}
	memcpy(h.fontpack_id, b->fontpack_id, sizeof(h.fontpack_id));
	memcpy(h.digest, b->digest, sizeof(h.digest));
	err = bflash_pending_seal(d->env.flash, slot, &h, d->layout);
	if (err != 0) {
		d->c.storage_errors++;
		return err;
	}
	d->pending_seq = h.seq;
	d->x.slot = DLV_NO_SLOT;
	if (job != NULL) {
		release_slot(d, job); /* its previous record, if any */
		job->slot = slot;
	} else if (bflash_pending_consume(d->env.flash, slot) != 0) {
		d->c.storage_errors++;
	}
	return 0;
}

/*
 * Same revision and digest (3.3, 10): a displayed revision re-sends its stored
 * result under the new update_id; a pending one adopts the new update_id; the
 * update_id the stored result was reported for gets that result again (never
 * new work). False: the revision ended without being displayed and is
 * accepted again.
 */
static bool duplicate(struct dlv *d, int idx, const struct ctag_mesh_layout_begin *b, uint32_t now)
{
	struct dlv_history *h = &d->hist[idx];
	bool same = h->update_id == b->update_id;
	struct dlv_job *job;
	int32_t seq;

	if (h->has_result && (h->status == CTAG_STATUS_OK || same)) {
		d->c.duplicates++;
		seq = same ? h->result_seq : result_seq_for(d, b->update_id);
		if (!same) {
			h->update_id = b->update_id;
			h->result_seq = (uint16_t)seq;
			save_hist(d, idx);
		}
		(void)seal_xfer(d, NULL);
		send_result(d, b->update_id, b->tag_id, b->epoch, b->revision, h->status,
			    h->digest8, h->battery_mv, &h->timing, seq, now);
		return true;
	}
	job = find_layout_job(d, b->tag_id, b->epoch, b->revision);
	if (job == NULL) {
		return false;
	}
	d->c.duplicates++;
	if (job->len == b->total_len) {
		/* The new record (same bytes) carries the adopted update_id across a
		 * reset; the session, if any, reloads from it. */
		job->update_id = b->update_id;
		if (!same) {
			h->update_id = b->update_id;
			save_hist(d, idx);
		}
		(void)seal_xfer(d, job);
	}
	return true;
}

/* A layout job of the tag that a newly accepted layout replaces. */
static bool supersedable(const struct dlv_job *j, uint32_t tag_id)
{
	return j->used && j->kind == DLV_LAYOUT && j->tag_id == tag_id &&
	       !(j->flags & DLV_JOB_IN_SESSION);
}

/*
 * Accept the committed transfer. Nothing changes unless its record is sealed
 * first: a full job table (with no older layout of the tag to replace)
 * answers NO_RESOURCES and a flash failure STORAGE_ERROR, with the history
 * and the older jobs untouched. Then the history takes the revision (saved
 * before any SUPERSEDED result goes out), the older pending layouts of the
 * tag end SUPERSEDED, and the new job takes the sealed record.
 */
static uint8_t accept(struct dlv *d, int idx, const struct ctag_mesh_layout_begin *b, uint32_t now)
{
	struct dlv_history *h = &d->hist[idx];
	struct dlv_job sealed = {.slot = DLV_NO_SLOT, .update_id = b->update_id};
	struct dlv_job *job = NULL;
	bool room = false;
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS && !room; i++) {
		room = !d->jobs[i].used || supersedable(&d->jobs[i], b->tag_id);
	}
	if (!room) {
		d->c.jobs_dropped++;
		return CTAG_STATUS_NO_RESOURCES;
	}
	if (seal_xfer(d, &sealed) != 0) {
		return CTAG_STATUS_STORAGE_ERROR;
	}
	memset(h, 0, sizeof(*h));
	h->valid = 1u;
	h->tag_id = b->tag_id;
	h->epoch = b->epoch;
	h->revision = b->revision;
	h->update_id = b->update_id;
	memcpy(h->digest, b->digest, sizeof(h->digest));
	save_hist(d, idx);
	/* An older pending layout of the tag is replaced (a session keeps its own). */
	for (i = 0; i < DLV_MAX_JOBS; i++) {
		if (supersedable(&d->jobs[i], b->tag_id)) {
			d->c.superseded++;
			dlv_finish(d, &d->jobs[i], CTAG_STATUS_SUPERSEDED, NULL, 0u, NULL, now);
		}
	}
	job = job_alloc(d); /* there is room: checked above */
	if (job == NULL) {
		(void)bflash_pending_consume(d->env.flash, sealed.slot);
		return CTAG_STATUS_INTERNAL;
	}
	job->kind = DLV_LAYOUT;
	job->slot = sealed.slot;
	job->tag_id = b->tag_id;
	job->epoch = b->epoch;
	job->revision = b->revision;
	job->update_id = b->update_id;
	job->len = b->total_len;
	job->validated_ms = now;
	memcpy(job->digest, b->digest, sizeof(job->digest));
	memcpy(job->fontpack_id, b->fontpack_id, sizeof(job->fontpack_id));
	d->c.layouts_accepted++;
	return CTAG_STATUS_OK;
}

uint8_t dlv_layout_commit(struct dlv *d, uint16_t xfer_id, uint32_t now, uint32_t *missing)
{
	struct dlv_xfer *x = &d->x;
	const struct ctag_mesh_layout_begin *b = &x->begin;
	uint8_t n = b->chunk_count;
	uint32_t want = n >= 32u ? UINT32_MAX : ((uint32_t)1u << n) - 1u;
	uint32_t len = n == 0u ? 0u : (uint32_t)(n - 1u) * CTAG_LAYOUT_CHUNK_DATA_MAX + x->last_len;
	const struct dlv_history *h;
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	uint8_t st;
	int idx;

	*missing = 0u;
	if (x->accepted && xfer_id == b->xfer_id) {
		/* 10: its OK was lost; the gateway treats DUPLICATE like OK. */
		d->c.duplicates++;
		return CTAG_STATUS_DUPLICATE;
	}
	if (!x->active || xfer_id != b->xfer_id) {
		return CTAG_STATUS_NOT_FOUND;
	}
	if ((x->have & want) != want) {
		*missing = want & ~x->have;
		d->c.incomplete++;
		return CTAG_STATUS_INCOMPLETE;
	}
	if (b->total_len > CTAG_LAYOUT_HARD_MAX || len > CTAG_LAYOUT_HARD_MAX) {
		return CTAG_STATUS_TOO_LARGE;
	}
	if (x->bad || len != b->total_len) {
		return CTAG_STATUS_INVALID;
	}
	st = load_xfer(d, len);
	if (st != CTAG_STATUS_OK) {
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
			x->accepted = true;
			return CTAG_STATUS_DUPLICATE;
		}
	}
	if (d->env.fonts == NULL || !fontstore_active_id(d->env.fonts, id) ||
	    memcmp(id, b->fontpack_id, sizeof(id)) != 0) {
		return CTAG_STATUS_FONTPACK_MISMATCH;
	}
	st = ctag_layout_validate(d->layout, len, fontstore_has_strike, d->env.fonts);
	if (st == CTAG_STATUS_OK) {
		st = accept(d, idx, b, now);
	}
	x->accepted = st == CTAG_STATUS_OK;
	return st;
}

void dlv_layout_cancel(struct dlv *d, uint64_t update_id, uint32_t now)
{
	struct dlv_job *j = find_update_job(d, update_id);

	if (j != NULL && !(j->flags & DLV_JOB_IN_SESSION)) {
		d->c.cancelled++;
		dlv_finish(d, j, CTAG_STATUS_CANCELLED, NULL, 0u, NULL, now);
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
		for (i = 0; i < DLV_MAX_TAGS && idx < 0; i++) {
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
		cancel_tag(d, m->tag_id, m->epoch - 1u, now);
	}
	if (d->asg[idx].epoch != m->epoch || !d->asg[idx].used) {
		d->asg[idx].unauth_status = 0u; /* 10: counted per tag and epoch */
		d->asg[idx].unauth_count = 0u;
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
	cancel_tag(d, m->tag_id, m->epoch, now); /* inclusive: also epoch 0xFFFFFFFF */
	return CTAG_STATUS_OK;
}

void dlv_tag_cmd(struct dlv *d, const struct ctag_mesh_tag_cmd *m, uint32_t now)
{
	int idx = find_asg(d, m->tag_id);
	uint8_t st = CTAG_STATUS_OK;
	struct dlv_result *r;
	struct dlv_job *job;

	/* A repeated TAG_CMD (the gateway re-sends it when the segment ACK was
	 * lost): no second job, and one result_seq for its update_id (10). */
	if (find_update_job(d, m->update_id) != NULL) {
		d->c.duplicates++;
		return;
	}
	r = result_find(d, m->update_id);
	if (r != NULL) {
		d->c.duplicates++;
		send_result(d, m->update_id, m->tag_id, m->epoch, 0u, r->msg.status, NULL, 0u, NULL,
			    NEW_SEQ, now);
		return;
	}
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
			    st == CTAG_STATUS_OK ? CTAG_STATUS_NO_RESOURCES : st, NULL, 0u, NULL,
			    NEW_SEQ, now);
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

	for (i = 0; i < DLV_MAX_TAGS; i++) {
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

bool dlv_job_layout_held(const struct dlv *d, const struct dlv_job *job)
{
	return job != NULL && d->lb_job == job;
}

int dlv_job_layout(struct dlv *d, const struct dlv_job *job, const uint8_t **layout)
{
	struct bflash_pending h;
	int st;

	*layout = d->layout;
	if (dlv_job_layout_held(d, job)) {
		return job->len;
	}
	d->lb_job = NULL;
	if (job->kind != DLV_LAYOUT || job->slot == DLV_NO_SLOT || !d->env.flash_ok) {
		return -ENOENT;
	}
	d->c.layout_loads++;
	st = bflash_pending_read(d->env.flash, job->slot, &h, d->layout, sizeof(d->layout));
	if (st < 0) {
		return st;
	}
	if (st != 1 || h.tag_id != job->tag_id || h.epoch != job->epoch ||
	    h.revision != job->revision || h.len != job->len ||
	    memcmp(h.digest, job->digest, sizeof(h.digest)) != 0) {
		return -EBADMSG;
	}
	d->lb_job = job;
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

bool dlv_unauth_status(struct dlv *d, uint32_t tag_id, uint32_t epoch, uint8_t status)
{
	int idx = find_asg(d, tag_id);
	struct dlv_assignment *a;

	if (idx < 0 || d->asg[idx].epoch != epoch) {
		return false; /* the assignment changed meanwhile: nothing to end */
	}
	a = &d->asg[idx];
	if (a->unauth_count > 0u && a->unauth_status == status) {
		a->unauth_count++;
	} else {
		a->unauth_status = status;
		a->unauth_count = 1u;
	}
	if (a->unauth_count < DLV_UNAUTH_REPEATS) {
		return false;
	}
	a->unauth_count = 0u;
	d->c.unauth_final++;
	return true;
}

void dlv_tag_authenticated(struct dlv *d, uint32_t tag_id, uint32_t epoch)
{
	int idx = find_asg(d, tag_id);

	if (idx >= 0 && d->asg[idx].epoch == epoch) {
		d->asg[idx].unauth_count = 0u;
	}
}

void dlv_session_over(struct dlv *d, uint32_t tag_id, uint32_t now)
{
	const struct dlv_assignment *a = dlv_assignment(d, tag_id);
	size_t i;

	for (i = 0; i < DLV_MAX_JOBS; i++) {
		struct dlv_job *j = &d->jobs[i];

		if (j->used && j->tag_id == tag_id && !(j->flags & DLV_JOB_IN_SESSION) &&
		    (a == NULL || j->epoch < a->epoch)) {
			d->c.cancelled++;
			dlv_finish(d, j, CTAG_STATUS_CANCELLED, NULL, 0u, NULL, now);
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
