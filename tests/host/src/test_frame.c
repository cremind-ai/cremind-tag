/* ctag_frame against cobs.json and serial_frames.json. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_frame.h>

#include "check.h"
#include "suites.h"
#include "v_cobs.h"
#include "v_serial.h"

static uint8_t buf[CTAG_SERIAL_MAX_ENCODED];
static uint8_t frame[CTAG_SERIAL_MAX_FRAME];

void test_cobs_vectors(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_cobs); i++) {
		const struct v_cobs *v = &v_cobs[i];
		int n = ctag_cobs_encode(v->dec, v->dec_len, buf,
					 CTAG_COBS_MAX_ENCODED(v->dec_len));

		CHECK_CASE(n == (int)v->enc_len && memcmp(buf, v->enc, v->enc_len) == 0, v->name);
		n = ctag_cobs_decode(v->enc, v->enc_len, buf, v->dec_len);
		CHECK_CASE(n == (int)v->dec_len && memcmp(buf, v->dec, v->dec_len) == 0, v->name);
	}
}

void test_cobs_decode_only(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_cobs_decode_only); i++) {
		const struct v_cobs *v = &v_cobs_decode_only[i];
		int n = ctag_cobs_decode(v->enc, v->enc_len, buf, sizeof(buf));

		CHECK_CASE(n == (int)v->dec_len && memcmp(buf, v->dec, v->dec_len) == 0, v->name);
	}
}

void test_cobs_decode_errors(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_cobs_errors); i++) {
		const struct v_cobs *v = &v_cobs_errors[i];

		CHECK_CASE(ctag_cobs_decode(v->enc, v->enc_len, buf, sizeof(buf)) == -EINVAL,
			   v->name);
	}
}

void test_cobs_buffer_limits(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_cobs); i++) {
		const struct v_cobs *v = &v_cobs[i];

		CHECK_CASE(v->enc_len <= CTAG_COBS_MAX_ENCODED(v->dec_len), v->name);
		CHECK_CASE(ctag_cobs_encode(v->dec, v->dec_len, buf, v->enc_len - 1u) == -EMSGSIZE,
			   v->name);
		if (v->dec_len > 0u) {
			CHECK_CASE(ctag_cobs_decode(v->enc, v->enc_len, buf, v->dec_len - 1u) ==
					   -EMSGSIZE,
				   v->name);
		}
	}
	/* The worst case of the spec constant: 4096 non-zero bytes and the delimiter. */
	CHECK(CTAG_COBS_MAX_ENCODED(CTAG_SERIAL_MAX_FRAME) + 1u <= CTAG_SERIAL_MAX_ENCODED);
}

void test_cobs_stream(void)
{
	static struct ctag_cobs_decoder d;
	size_t i, j;

	for (i = 0u; i < V_COUNT(v_cobs_stream); i++) {
		const struct v_cobs_stream *v = &v_cobs_stream[i];
		size_t got = 0u;

		ctag_cobs_decoder_init(&d, frame, sizeof(frame));
		for (j = 0u; j < v->in_len; j++) {
			int n = ctag_cobs_decoder_put(&d, v->in[j]);

			if (n == -EAGAIN) {
				continue;
			}
			CHECK_CASE(got < v->n_frames, v->name);
			CHECK_CASE(n == (int)v->frame_lens[got] &&
					   memcmp(frame, v->frames[got], v->frame_lens[got]) == 0,
				   v->name);
			got++;
		}
		CHECK_CASE(got == v->n_frames, v->name);
		CHECK_CASE(d.errors == v->errors && d.oversize == v->oversize, v->name);
	}
}

static void header_of(const struct v_serial_frame *v, struct ctag_serial_header *h)
{
	h->version = CTAG_PROTO_VERSION;
	h->type = v->type;
	h->request_id = v->request_id;
	h->length = 0u; /* set by ctag_serial_frame_build() */
	h->flags = v->flags;
	h->credits = v->credits;
}

void test_serial_frames(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_serial_frames); i++) {
		const struct v_serial_frame *v = &v_serial_frames[i];
		struct ctag_serial_header h, got;
		int n;

		header_of(v, &h);
		n = ctag_serial_frame_build(frame, sizeof(frame), &h, v->payload, v->payload_len);
		CHECK_CASE(n == (int)v->decoded_len &&
				   memcmp(frame, v->decoded, v->decoded_len) == 0,
			   v->name);
		/* Payload already in place. */
		memset(frame, 0xA5, sizeof(frame));
		memcpy(&frame[CTAG_SERIAL_HEADER_LEN], v->payload, v->payload_len);
		n = ctag_serial_frame_build(frame, v->decoded_len, &h, NULL, v->payload_len);
		CHECK_CASE(n == (int)v->decoded_len &&
				   memcmp(frame, v->decoded, v->decoded_len) == 0,
			   v->name);
		CHECK_CASE(ctag_serial_frame_build(frame, v->decoded_len - 1u, &h, NULL,
						   v->payload_len) == -EMSGSIZE,
			   v->name);
		n = ctag_serial_wire_encode(v->decoded, v->decoded_len, buf, sizeof(buf));
		CHECK_CASE(n == (int)v->wire_len && memcmp(buf, v->wire, v->wire_len) == 0,
			   v->name);
		CHECK_CASE(ctag_serial_frame_check(v->decoded, v->decoded_len, &got) ==
				   CTAG_SERIAL_CHECK_OK,
			   v->name);
		CHECK_CASE(got.type == v->type && got.request_id == v->request_id &&
				   got.length == v->payload_len && got.flags == v->flags &&
				   got.credits == v->credits,
			   v->name);
	}
}

void test_serial_invalid(void)
{
	struct ctag_serial_header h;
	size_t i;

	for (i = 0u; i < V_COUNT(v_serial_invalid); i++) {
		const struct v_serial_invalid *v = &v_serial_invalid[i];

		CHECK_CASE((int)ctag_serial_frame_check(v->decoded, v->len, &h) == v->check,
			   v->name);
	}
	/* Decoded size above SERIAL_MAX_FRAME. */
	memset(frame, 0, sizeof(frame));
	CHECK(ctag_serial_frame_check(frame, sizeof(frame) + 1u, &h) == CTAG_SERIAL_CHECK_LEN);
}

void test_serial_rx(void)
{
	static struct ctag_serial_rx rx;
	struct ctag_serial_header h;
	unsigned int frames = 0u;
	size_t i, j;

	ctag_serial_rx_init(&rx, frame, sizeof(frame));
	for (i = 0u; i < V_COUNT(v_serial_frames); i++) {
		const struct v_serial_frame *v = &v_serial_frames[i];

		for (j = 0u; j < v->wire_len; j++) {
			bool done = ctag_serial_rx_put(&rx, v->wire[j], &h);

			CHECK_CASE(done == (j + 1u == v->wire_len), v->name);
		}
		CHECK_CASE(h.type == v->type && h.request_id == v->request_id, v->name);
		CHECK_CASE(memcmp(&frame[CTAG_SERIAL_HEADER_LEN], v->payload, v->payload_len) == 0,
			   v->name);
		frames++;
	}
	for (i = 0u; i < V_COUNT(v_serial_invalid); i++) {
		const struct v_serial_invalid *v = &v_serial_invalid[i];
		int n = ctag_serial_wire_encode(v->decoded, v->len, buf, sizeof(buf));

		CHECK_CASE(n > 0, v->name);
		for (j = 0u; j < (size_t)n; j++) {
			CHECK_CASE(!ctag_serial_rx_put(&rx, buf[j], &h), v->name);
		}
	}
	CHECK(frames == V_COUNT(v_serial_frames));
	CHECK(rx.len_errors == 2u && rx.crc_errors == 2u && rx.version_errors == 1u);
}

void test_credits(void)
{
	struct ctag_credits c;
	unsigned int i;

	ctag_credits_reset(&c);
	for (i = 0u; i < CTAG_SERIAL_DEFAULT_CREDITS; i++) {
		CHECK(ctag_credits_take(&c));
	}
	CHECK(!ctag_credits_take(&c));
	ctag_credits_add(&c, 3u);
	CHECK(ctag_credits_take(&c) && c.tx == 2u);
	CHECK(ctag_credits_grant(&c) == 0u);
	ctag_credits_release(&c);
	ctag_credits_release(&c);
	CHECK(ctag_credits_grant(&c) == 2u && ctag_credits_grant(&c) == 0u);
	for (i = 0u; i < 300u; i++) {
		ctag_credits_release(&c);
	}
	CHECK(ctag_credits_grant(&c) == 255u && ctag_credits_grant(&c) == 45u);
	c.tx = UINT16_MAX - 1u;
	ctag_credits_add(&c, 200u);
	CHECK(c.tx == UINT16_MAX);
}
