/* The FIFO byte arena (layouts, best-effort events) and the streaming COBS
 * transmitter. */
#include <string.h>

#include "common.h"

ZTEST_SUITE(gw_ring, NULL, NULL, NULL, NULL, NULL);

static uint8_t mem[256] __aligned(4);

ZTEST(gw_ring, test_fifo_alloc_free_and_wrap)
{
	struct gw_ring r;
	uint8_t *a, *b, *c, *d;
	uint32_t len;

	gw_ring_init(&r, mem, sizeof(mem));
	zassert_true(gw_ring_empty(&r));
	a = gw_ring_alloc(&r, 100u); /* 104 */
	b = gw_ring_alloc(&r, 100u); /* 208 */
	zassert_not_null(a);
	zassert_not_null(b);
	zassert_is_null(gw_ring_alloc(&r, 60u), "48 bytes left");
	c = gw_ring_alloc(&r, 40u); /* 252 */
	zassert_not_null(c);
	zassert_equal(gw_ring_first(&r, &len), a);
	zassert_equal(len, 100u);
	/* Released out of order: b's space returns only after a's. */
	gw_ring_free(&r, b);
	zassert_is_null(gw_ring_alloc(&r, 60u));
	gw_ring_free(&r, a);
	zassert_equal(gw_ring_first(&r, &len), c, "a and b reclaimed together");
	d = gw_ring_alloc(&r, 150u); /* wraps to the start */
	zassert_equal(d, &mem[4]);
	memset(d, 0xAB, 150u);
	gw_ring_free(&r, c);
	zassert_equal(gw_ring_first(&r, &len), d, "the wrap marker is skipped");
	zassert_equal(len, 150u);
	gw_ring_free(&r, d);
	zassert_true(gw_ring_empty(&r));
	zassert_not_null(gw_ring_alloc(&r, 252u), "empty: the whole arena is contiguous again");
}

ZTEST(gw_ring, test_reserve_then_commit_less)
{
	struct gw_ring r;
	uint32_t cap, len;
	uint8_t *p;

	gw_ring_init(&r, mem, sizeof(mem));
	p = gw_ring_reserve(&r, 10u, &cap);
	zassert_not_null(p);
	zassert_equal(cap, 252u);
	gw_ring_commit(&r, 9u);
	zassert_equal(gw_ring_used(&r), 16u);
	p = gw_ring_reserve(&r, 300u, &cap);
	zassert_is_null(p);
	zassert_equal(gw_ring_first(&r, &len), &mem[4]);
	zassert_equal(len, 9u);
}

/* The streaming COBS output equals the library's encoding of the same frame,
 * across the 254-byte block boundary. */
ZTEST(gw_ring, test_streaming_cobs_matches_the_library)
{
	static char text[600];
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	static uint8_t wire[CTAG_SERIAL_MAX_ENCODED];

	core_reset();
	core_session();
	memset(text, 'a', sizeof(text));
	for (size_t n = 240u; n <= 520u; n += 7u) {
		struct ctag_cbor_field f = GW_F_TSTR(CTAG_CBOR_KEY_TEXT, text, n);
		struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION,
					       .type = CTAG_SERIAL_MSG_EVT_LOG,
					       .flags = CTAG_SERIAL_FLAG_EVENT};
		const uint8_t *got;
		size_t got_len;
		int len, wlen;

		zassert_true(gw_emit(&core, CTAG_SERIAL_MSG_EVT_LOG, &f, 1u, false));
		gw_core_poll(&core, now_ms);
		got_len = capture_peek(&got);
		len = ctag_cbor_encode(&f, 1u, &frame[CTAG_SERIAL_HEADER_LEN], 1024u);
		len = ctag_serial_frame_build(frame, sizeof(frame), &h, NULL, (size_t)len);
		wlen = ctag_serial_wire_encode(frame, (size_t)len, wire, sizeof(wire));
		zassert_equal(got_len, (size_t)wlen, "length at text %u", n);
		zassert_mem_equal(got, wire, (size_t)wlen, "bytes at text %u", n);
		host_read();
		zassert_equal(events_of(CTAG_SERIAL_MSG_EVT_LOG), 1u);
	}
}
