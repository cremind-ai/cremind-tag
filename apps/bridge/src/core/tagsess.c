/* Bridge side of a tag session (docs/protocol.md 5.3-5.6, 10). */
#include <errno.h>
#include <string.h>

#include <zephyr/sys/util.h>

#include <ctag/ctag_crypto.h>

#include "tagsess.h"

static uint32_t now(const struct tsess *s)
{
	return s->io->now(s->ctx);
}

static void timer(struct tsess *s, uint8_t which, uint32_t ms)
{
	s->io->timer(s->ctx, which, ms);
}

static void step(struct tsess *s)
{
	if (s->state != TS_RESULT) {
		timer(s, TSESS_T_STEP, TSESS_STEP_TIMEOUT_MS);
	}
}

static uint16_t sat16(uint32_t v)
{
	return (uint16_t)MIN(v, UINT16_MAX);
}

static bool final_tag_error(uint8_t st)
{
	return st == CTAG_STATUS_AUTH_FAILED || st == CTAG_STATUS_STALE_EPOCH ||
	       st == CTAG_STATUS_VERSION_MISMATCH || st == CTAG_STATUS_NOT_FOUND;
}

void tsess_init(struct tsess *s, const struct tsess_io *io, void *ctx, struct dlv *dlv,
		struct fontstore *fonts, const struct ctag_sha256_ops *sha)
{
	memset(s, 0, sizeof(*s));
	s->io = io;
	s->ctx = ctx;
	s->dlv = dlv;
	s->fonts = fonts;
	s->sha = sha;
}

bool tsess_active(const struct tsess *s)
{
	return s->state != TS_IDLE && s->state != TS_DONE;
}

static void close_view(struct tsess *s)
{
	fontstore_view_close(s->fonts, &s->view);
}

static void end(struct tsess *s, uint8_t status)
{
	if (!tsess_active(s)) {
		return;
	}
	if (s->job != NULL) {
		/* Interrupted: the job stays pending and is retried later. */
		s->job->flags &= (uint8_t)~DLV_JOB_IN_SESSION;
		s->job = NULL;
	}
	close_view(s);
	s->state = TS_DONE;
	s->status = status;
	timer(s, TSESS_T_STEP, TSESS_TIMER_OFF);
	timer(s, TSESS_T_PACE, TSESS_TIMER_OFF);
	memset(&s->sess, 0, sizeof(s->sess));
	memset(s->key, 0, sizeof(s->key));
	if (status != CTAG_STATUS_OK && status != CTAG_STATUS_DISCONNECTED) {
		s->c.protocol_errors += status == CTAG_STATUS_INVALID ? 1u : 0u;
		s->c.timeouts += status == CTAG_STATUS_TIMEOUT ? 1u : 0u;
	}
	s->io->done(s->ctx, status);
}

void tsess_abort(struct tsess *s, uint8_t status)
{
	end(s, status);
}

/* ERROR from the tag (or a failed MAC): security/config errors end the tag's
 * jobs of this epoch, anything else is retried after the back-off (10). */
static void tag_error(struct tsess *s, uint8_t status)
{
	if (final_tag_error(status)) {
		if (s->job != NULL) {
			s->job->flags &= (uint8_t)~DLV_JOB_IN_SESSION;
			s->job = NULL;
		}
		close_view(s);
		dlv_fail_epoch(s->dlv, s->tag_id, s->epoch, status, now(s));
	}
	end(s, status);
}

/* ---- CTRL (write requests, one fragment at a time) ---- */

static void ctrl_pump(struct tsess *s)
{
	uint8_t frag[CTAG_ATT_VALUE_MAX];
	int n;

	if (s->ctrl_inflight || s->ctrl_off >= s->ctrl_len) {
		return;
	}
	n = ctag_frag_next(&s->ctrl_tx, s->ctrl_out, s->ctrl_len, &s->ctrl_off,
			   CTAG_FRAG_PAYLOAD_MAX, frag);
	if (n < 0 || s->io->write_ctrl(s->ctx, frag, (uint16_t)n) != 0) {
		end(s, CTAG_STATUS_DISCONNECTED);
		return;
	}
	s->ctrl_inflight = true;
}

static void send_ctrl(struct tsess *s, const uint8_t *msg, size_t len)
{
	memcpy(s->ctrl_out, msg, len);
	s->ctrl_len = len;
	s->ctrl_off = 0u;
	step(s);
	ctrl_pump(s);
}

void tsess_ctrl_written(struct tsess *s, int err)
{
	if (!tsess_active(s)) {
		return;
	}
	s->ctrl_inflight = false;
	if (err != 0) {
		end(s, CTAG_STATUS_INVALID);
		return;
	}
	ctrl_pump(s);
}

/* ---- DATA (authenticated records, write without response) ---- */

static bool rec_busy(const struct tsess *s)
{
	return s->rec_off < s->rec_len;
}

static void advance(struct tsess *s);

static void data_pump(struct tsess *s)
{
	uint8_t frag[CTAG_ATT_VALUE_MAX];

	while (tsess_active(s) && rec_busy(s) && s->inflight < CONFIG_CTAG_BRIDGE_ATT_INFLIGHT) {
		struct ctag_frag_tx saved = s->data_tx;
		size_t off = s->rec_off;
		int n = ctag_frag_next(&s->data_tx, s->rec, s->rec_len, &s->rec_off,
				       CTAG_FRAG_PAYLOAD_MAX, frag);
		int err = n < 0 ? -EINVAL : s->io->write_data(s->ctx, frag, (uint16_t)n);

		if (err == -ENOMEM || err == -EAGAIN) {
			s->data_tx = saved;
			s->rec_off = off;
			if (s->inflight == 0u) {
				timer(s, TSESS_T_PACE, 5u); /* no completion will wake us */
				s->pacing = true;
			}
			return;
		}
		if (err != 0) {
			end(s, CTAG_STATUS_DISCONNECTED);
			return;
		}
		s->inflight++;
	}
}

void tsess_data_sent(struct tsess *s)
{
	if (s->inflight > 0u) {
		s->inflight--;
	}
	if (!tsess_active(s)) {
		return;
	}
	data_pump(s);
	if (!rec_busy(s)) {
		advance(s);
	}
}

/* At most 4 records per connection event (5.2 step 8): after the fourth,
 * wait one connection interval. */
static bool pace_ok(struct tsess *s)
{
	if (s->pacing) {
		return false;
	}
	if (s->records_in_event >= TSESS_RECORDS_PER_EVENT) {
		s->pacing = true;
		timer(s, TSESS_T_PACE, s->pace_ms);
		return false;
	}
	return true;
}

static bool send_record(struct tsess *s, uint8_t type, const uint8_t *pt, size_t len)
{
	int n = ctag_record_seal(&s->sess.tx, type, pt, len, s->rec, sizeof(s->rec));

	if (n < 0) {
		end(s, CTAG_STATUS_INTERNAL);
		return false;
	}
	s->rec_len = (size_t)n;
	s->rec_off = 0u;
	s->credits--;
	s->records_in_event++;
	s->c.records_tx++;
	step(s);
	data_pump(s);
	return tsess_active(s);
}

/* ---- Jobs ---- */

static void finish_job(struct tsess *s, uint8_t status, const uint8_t *digest8,
		       uint16_t battery_mv, uint16_t refresh_ms)
{
	struct dlv_job *job = s->job;
	struct dlv_timing t = {
		.wake_ms = sat16((uint32_t)MAX((int32_t)(s->connected_ms - job->validated_ms), 0)),
		.suspend_ms = sat16(s->suspend_ms),
		.transfer_ms = sat16(now(s) - s->job_started),
		.refresh_ms = refresh_ms,
	};

	s->job = NULL;
	close_view(s);
	dlv_finish(s->dlv, job, status, digest8, battery_mv, &t, now(s));
}

/* Render pre-pass of a layout job: OK, or the status that ends the job. */
static uint8_t prepare_frame(struct tsess *s, struct dlv_job *job)
{
	uint32_t t0 = now(s);
	uint8_t st;
	int n;

	if (!fontstore_view_open(s->fonts, &s->view)) {
		return CTAG_STATUS_FONTPACK_MISMATCH;
	}
	if (memcmp(s->view.pack.pack_id, job->fontpack_id, CTAG_FONTPACK_ID_LEN) != 0) {
		return CTAG_STATUS_FONTPACK_MISMATCH;
	}
	n = dlv_job_layout(s->dlv, job, s->layout, sizeof(s->layout));
	if (n <= 0) {
		return CTAG_STATUS_STORAGE_ERROR;
	}
	s->panel.width = s->caps.width;
	s->panel.height = s->caps.height;
	s->panel.planes = s->caps.planes;
	s->panel.plane_flags = s->caps.plane_flags;
	s->row_bytes = (uint16_t)ctag_render_row_bytes(&s->panel);
	s->plane_len = (uint32_t)ctag_render_plane_len(&s->panel);
	if (s->plane_len == 0u || s->plane_len > UINT16_MAX) {
		return CTAG_STATUS_INVALID; /* FRAME_BEGIN.plane_len is a u16 */
	}
	s->strip_rows = (uint16_t)MIN((size_t)CTAG_BRIDGE_STRIP_ROWS, sizeof(s->strip) / s->row_bytes);
	if (s->strip_rows == 0u) {
		return CTAG_STATUS_INVALID;
	}
	st = ctag_render_init(&s->render, s->layout, (size_t)n, &s->panel, &s->view.src, &s->work);
	if (st != CTAG_STATUS_OK) {
		return st;
	}
	n = ctag_render_frame_digest(&s->render, s->strip, (size_t)s->strip_rows * s->row_bytes,
				     s->sha, s->digest);
	if (n != 0) {
		return n == -EIO ? CTAG_STATUS_STORAGE_ERROR : CTAG_STATUS_INTERNAL;
	}
	s->c.render_ms_max = MAX(s->c.render_ms_max, now(s) - t0);
	s->strip_y0 = -1;
	s->plane = 0u;
	s->offset = 0u;
	return CTAG_STATUS_OK;
}

static void next_job(struct tsess *s)
{
	struct dlv_job *job;

	while (tsess_active(s)) {
		uint8_t st;

		job = dlv_next_job(s->dlv, s->tag_id, s->last_order, s->upto);
		if (job == NULL) {
			end(s, CTAG_STATUS_OK);
			return;
		}
		s->last_order = job->order;
		if (job->epoch != s->epoch) {
			dlv_finish(s->dlv, job, CTAG_STATUS_NOT_ASSIGNED, NULL, 0u, NULL, now(s));
			continue;
		}
		job->flags |= DLV_JOB_IN_SESSION;
		s->job = job;
		s->job_started = now(s);
		s->refreshing_reported = false;
		if (job->kind == DLV_LAYOUT) {
			/* 10: FRAME_END went out, RESULT was lost, and the tag reports the
			 * unknown display state for this very (epoch, revision). */
			if ((job->flags & DLV_JOB_FRAME_END_SENT) && (s->ch.flags & 1u) &&
			    s->ch.stored_epoch == job->epoch && s->ch.displayed_rev == job->revision) {
				finish_job(s, CTAG_STATUS_DISPLAY_STATE_UNKNOWN, NULL, s->ch.battery_mv,
					   0u);
				continue;
			}
			st = prepare_frame(s, job);
			if (st != CTAG_STATUS_OK) {
				finish_job(s, st, NULL, 0u, 0u);
				continue;
			}
			s->job_started = now(s); /* time the transfer, not the pre-pass */
		}
		dlv_stage(s->dlv, job, CTAG_STAGE_TRANSFERRING);
		s->state = TS_JOB_CREDIT;
		step(s);
		advance(s);
		return;
	}
}

/* PLANE_DATA bytes [offset, offset + n) of the current plane, strip by strip. */
static int fill_plane(struct tsess *s, uint8_t *out, size_t n)
{
	size_t pos = 0u;

	while (pos < n) {
		uint32_t abs = s->offset + (uint32_t)pos;
		uint32_t row = abs / s->row_bytes;
		size_t start, k;

		if (s->strip_y0 < 0 || s->strip_plane != s->plane || row < (uint32_t)s->strip_y0 ||
		    abs >= (uint32_t)s->strip_y0 * s->row_bytes + s->strip_len) {
			uint16_t y0 = (uint16_t)(row - row % s->strip_rows);
			int r = ctag_render_strip(&s->render, s->plane, y0, s->strip_rows, s->strip,
						  sizeof(s->strip));

			if (r <= 0) {
				s->strip_y0 = -1;
				return r < 0 ? r : -EIO;
			}
			s->strip_y0 = y0;
			s->strip_plane = s->plane;
			s->strip_len = (size_t)r;
		}
		start = abs - (uint32_t)s->strip_y0 * s->row_bytes;
		k = MIN(n - pos, s->strip_len - start);
		memcpy(&out[pos], &s->strip[start], k);
		pos += k;
	}
	return 0;
}

static void stream(struct tsess *s)
{
	while (s->state == TS_STREAM && tsess_active(s)) {
		size_t n;

		if (rec_busy(s) || s->pacing) {
			return;
		}
		if (s->plane >= s->panel.planes) {
			s->state = TS_END_CREDIT;
			advance(s);
			return;
		}
		if (s->credits == 0u) {
			return; /* the next CREDIT resumes (the step timer bounds the wait) */
		}
		if (!pace_ok(s)) {
			return;
		}
		n = MIN((size_t)CTAG_TAG_PLANE_DATA_MAX, (size_t)(s->plane_len - s->offset));
		/* PLANE_DATA{plane u8, offset u16, data}: rendered straight into place. */
		s->pt[0] = s->plane;
		ctag_put_le16(&s->pt[1], (uint16_t)s->offset);
		if (fill_plane(s, &s->pt[CTAG_REC_PLANE_DATA_LEN], n) != 0) {
			finish_job(s, CTAG_STATUS_STORAGE_ERROR, NULL, 0u, 0u);
			end(s, CTAG_STATUS_STORAGE_ERROR);
			return;
		}
		s->offset += (uint32_t)n;
		if (s->offset >= s->plane_len) {
			s->plane++;
			s->offset = 0u;
		}
		if (!send_record(s, CTAG_REC_PLANE_DATA, s->pt, CTAG_REC_PLANE_DATA_LEN + n)) {
			return;
		}
	}
}

static void advance(struct tsess *s)
{
	uint8_t buf[CTAG_REC_FRAME_BEGIN_LEN];
	struct dlv_job *job = s->job;
	int len;

	if (!tsess_active(s)) {
		return;
	}
	switch (s->state) {
	case TS_GRANT:
		if (s->grants > 0u) {
			next_job(s);
		}
		break;
	case TS_JOB_CREDIT:
		if (s->credits == 0u || rec_busy(s) || !pace_ok(s)) {
			break;
		}
		if (job->kind == DLV_CMD) {
			struct ctag_rec_cmd cmd = {.cmd = job->cmd, .update_id = job->update_id};

			len = ctag_rec_cmd_pack(&cmd, buf, sizeof(buf));
			s->state = TS_RESULT;
			timer(s, TSESS_T_STEP, TSESS_RESULT_TIMEOUT_MS);
			(void)send_record(s, CTAG_REC_CMD, buf, (size_t)len);
		} else {
			struct ctag_rec_frame_begin fb = {
				.revision = job->revision,
				.update_id = job->update_id,
				.planes = s->panel.planes,
				.plane_len = (uint16_t)s->plane_len,
			};

			memcpy(fb.digest, s->digest, sizeof(fb.digest));
			len = ctag_rec_frame_begin_pack(&fb, buf, sizeof(buf));
			s->fb_grants = s->grants;
			s->state = TS_FB_GRANT;
			s->c.frames++;
			(void)send_record(s, CTAG_REC_FRAME_BEGIN, buf, (size_t)len);
		}
		break;
	case TS_FB_GRANT:
		/* 10: FRAME_BEGIN's credit (or the tag's immediate RESULT) first. */
		if (s->grants != s->fb_grants) {
			s->state = TS_STREAM;
			stream(s);
		}
		break;
	case TS_STREAM:
		stream(s);
		break;
	case TS_END_CREDIT:
		if (s->credits == 0u || rec_busy(s) || !pace_ok(s)) {
			break;
		}
		job->flags |= DLV_JOB_FRAME_END_SENT;
		s->state = TS_RESULT;
		timer(s, TSESS_T_STEP, TSESS_RESULT_TIMEOUT_MS);
		(void)send_record(s, CTAG_REC_FRAME_END, NULL, 0u);
		break;
	default:
		break;
	}
}

/* ---- Session ---- */

void tsess_start(struct tsess *s, uint32_t tag_id, uint32_t suspend_ms, uint16_t pace_ms)
{
	const struct dlv_assignment *a = dlv_assignment(s->dlv, tag_id);

	memset(&s->caps, 0, offsetof(struct tsess, c) - offsetof(struct tsess, caps));
	s->tag_id = tag_id;
	s->suspend_ms = suspend_ms;
	s->pace_ms = pace_ms;
	s->connected_ms = now(s);
	s->status = CTAG_STATUS_OK;
	s->strip_y0 = -1;
	s->state = TS_CAPS;
	if (a == NULL) {
		end(s, CTAG_STATUS_OK); /* unassigned meanwhile: nothing to do */
		return;
	}
	s->epoch = a->epoch;
	memcpy(s->key, a->key, sizeof(s->key));
	s->upto = dlv_last_order(s->dlv);
	ctag_frag_tx_init(&s->ctrl_tx);
	ctag_frag_tx_init(&s->data_tx);
	ctag_frag_rx_init(&s->ctrl_rx, s->ctrl_in, sizeof(s->ctrl_in));
	ctag_frag_rx_init(&s->status_rx, s->status_in, sizeof(s->status_in));
	step(s);
	if (s->io->read_caps(s->ctx) != 0) {
		end(s, CTAG_STATUS_DISCONNECTED);
	}
}

void tsess_caps(struct tsess *s, int err, const uint8_t *data, uint16_t len)
{
	uint8_t hello[CTAG_SESSION_HELLO_LEN];
	uint8_t nonce[CTAG_TAG_NONCE_LEN];

	if (s->state != TS_CAPS) {
		return;
	}
	if (err != 0 || ctag_tag_caps_unpack(&s->caps, data, len) != 0) {
		end(s, CTAG_STATUS_INVALID);
		return;
	}
	if (s->caps.proto != CTAG_PROTO_VERSION) {
		end(s, CTAG_STATUS_VERSION_MISMATCH);
		return;
	}
	if (s->caps.tag_id != s->tag_id) {
		end(s, CTAG_STATUS_NOT_FOUND);
		return;
	}
	if (ctag_crypto_random(nonce, sizeof(nonce)) != 0) {
		end(s, CTAG_STATUS_INTERNAL);
		return;
	}
	(void)ctag_session_bridge_hello(&s->sess, s->tag_id, s->epoch, s->key, nonce, hello);
	memset(s->key, 0, sizeof(s->key));
	s->state = TS_HELLO;
	send_ctrl(s, hello, sizeof(hello));
}

void tsess_ctrl_value(struct tsess *s, const uint8_t *val, uint16_t len)
{
	uint8_t auth[CTAG_SESSION_AUTH_LEN];
	uint8_t st;
	int n;

	if (!tsess_active(s)) {
		return;
	}
	n = ctag_frag_rx_put(&s->ctrl_rx, val, len);
	if (n < 0) {
		end(s, CTAG_STATUS_INVALID);
		return;
	}
	step(s);
	if (n == 0) {
		return;
	}
	switch (s->state) {
	case TS_HELLO:
		st = ctag_session_bridge_challenge(&s->sess, s->ctrl_in, (size_t)n, &s->ch, auth);
		if (st != CTAG_STATUS_OK) {
			tag_error(s, st);
			return;
		}
		dlv_note_battery(s->dlv, s->tag_id, s->ch.battery_mv);
		s->state = TS_AUTH;
		send_ctrl(s, auth, sizeof(auth));
		break;
	case TS_AUTH:
		st = ctag_session_bridge_auth_ok(&s->sess, s->ctrl_in, (size_t)n);
		if (st != CTAG_STATUS_OK) {
			tag_error(s, st);
			return;
		}
		s->state = TS_GRANT;
		advance(s);
		break;
	case TS_CAPS:
		end(s, CTAG_STATUS_INVALID);
		break;
	default:
		/* Established: only a plaintext ERROR is expected on CTRL. */
		if (n == (int)CTAG_SESSION_ERROR_LEN && s->ctrl_in[0] == CTAG_CTRL_ERROR &&
		    s->ctrl_in[1] != CTAG_STATUS_OK) {
			tag_error(s, s->ctrl_in[1]);
		}
		break;
	}
}

static void on_result(struct tsess *s, const struct ctag_rec_result *res)
{
	s->c.results++;
	if (s->job == NULL) {
		return;
	}
	finish_job(s, res->status, res->digest, res->battery_mv, res->refresh_ms);
	next_job(s);
}

void tsess_status_value(struct tsess *s, const uint8_t *val, uint16_t len)
{
	struct ctag_rec_progress prog;
	struct ctag_rec_result res;
	uint8_t type;
	int n;

	if (!tsess_active(s)) {
		return;
	}
	n = ctag_frag_rx_put(&s->status_rx, val, len);
	if (n < 0) {
		end(s, CTAG_STATUS_INVALID);
		return;
	}
	step(s);
	if (n == 0) {
		return;
	}
	if (s->status_in[0] == CTAG_PLAIN_CREDIT) {
		if (n != 1 + CTAG_PLAIN_CREDIT_LEN) {
			end(s, CTAG_STATUS_INVALID);
			return;
		}
		s->credits += s->status_in[1];
		s->grants++;
		advance(s);
		return;
	}
	if (s->state < TS_GRANT) {
		end(s, CTAG_STATUS_INVALID); /* a record before the handshake finished */
		return;
	}
	n = ctag_record_open(&s->sess.rx, s->status_in, (size_t)n, &type, s->pt, sizeof(s->pt));
	if (n < 0) {
		end(s, CTAG_STATUS_INVALID);
		return;
	}
	s->c.records_rx++;
	if (type == CTAG_REC_PROGRESS && ctag_rec_progress_unpack(&prog, s->pt, (size_t)n) == 0) {
		if (prog.stage == CTAG_STAGE_REFRESHING && s->job != NULL && !s->refreshing_reported) {
			s->refreshing_reported = true;
			dlv_stage(s->dlv, s->job, CTAG_STAGE_REFRESHING);
		}
	} else if (type == CTAG_REC_RESULT && ctag_rec_result_unpack(&res, s->pt, (size_t)n) == 0) {
		on_result(s, &res);
	} else {
		end(s, CTAG_STATUS_INVALID);
	}
}

void tsess_timeout(struct tsess *s, uint8_t which)
{
	if (!tsess_active(s)) {
		return;
	}
	if (which == TSESS_T_STEP) {
		end(s, CTAG_STATUS_TIMEOUT);
		return;
	}
	s->pacing = false;
	s->records_in_event = 0u;
	data_pump(s);
	if (!rec_busy(s)) {
		advance(s);
	}
}
