/*
 * Serial framing (docs/protocol.md 1.1, 1.3): COBS, CRC-32 framed messages and
 * credit accounting. Portable C99, caller-provided buffers.
 */
#ifndef CTAG_FRAME_H_
#define CTAG_FRAME_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Worst-case COBS encoding of n bytes, without the 0x00 delimiter. */
#define CTAG_COBS_MAX_ENCODED(n) ((n) + (n) / 254u + 1u)

/* Smallest decoded serial frame: header + CRC. */
#define CTAG_SERIAL_MIN_FRAME (CTAG_SERIAL_HEADER_LEN + CTAG_SERIAL_CRC_LEN)

/*
 * COBS-encode len bytes (no delimiter; 254 non-zero bytes encode to 255).
 * Returns the encoded length or -EMSGSIZE when out is too small.
 */
int ctag_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t size);

/*
 * Decode one COBS block sequence (without its delimiter). Accepts a trailing
 * 0x01 block after a final 0xFF block. Returns the decoded length, -EINVAL for
 * malformed input (empty, a zero byte, a truncated block) or -EMSGSIZE.
 */
int ctag_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t size);

/* Streaming decoder for a 0x00-delimited byte stream. */
struct ctag_cobs_decoder {
	uint8_t *buf; /* decoded frame; size = largest accepted frame */
	size_t size;
	size_t len;
	uint32_t errors;   /* frames ended inside a block */
	uint32_t oversize; /* frames discarded for exceeding size */
	uint8_t remaining; /* data bytes left in the current block */
	bool zero_pending; /* the current block implies a zero if another follows */
	bool started;
	bool discarding;
};

void ctag_cobs_decoder_init(struct ctag_cobs_decoder *d, uint8_t *buf, size_t size);

/*
 * Feed one byte. Returns the decoded length (>= 0) when a 0x00 completes a
 * frame, which is then in d->buf until the next call, or -EAGAIN. Empty frames
 * (consecutive delimiters) are ignored; a frame growing past size is discarded
 * up to the next 0x00 (oversize).
 */
int ctag_cobs_decoder_put(struct ctag_cobs_decoder *d, uint8_t byte);

/* Frame check results, in the order of 1.1: size, CRC, length, version. */
enum ctag_serial_check {
	CTAG_SERIAL_CHECK_OK = 0,
	CTAG_SERIAL_CHECK_LEN,     /* len_errors */
	CTAG_SERIAL_CHECK_CRC,     /* crc_errors */
	CTAG_SERIAL_CHECK_VERSION, /* version_errors */
};

/*
 * Build a decoded frame: header (length = payload_len), payload, CRC-32.
 * payload may be NULL when the caller already wrote it at
 * frame + CTAG_SERIAL_HEADER_LEN. Returns the frame length or -EMSGSIZE.
 */
int ctag_serial_frame_build(uint8_t *frame, size_t size, const struct ctag_serial_header *hdr,
			    const uint8_t *payload, size_t payload_len);

/* Check a decoded frame; on OK the payload is at frame + CTAG_SERIAL_HEADER_LEN. */
enum ctag_serial_check ctag_serial_frame_check(const uint8_t *frame, size_t len,
					       struct ctag_serial_header *hdr);

/* COBS-encode a decoded frame and append the 0x00 delimiter. */
int ctag_serial_wire_encode(const uint8_t *frame, size_t len, uint8_t *wire, size_t size);

/* Receiver: stream decoder plus the drop counters of 1.1. */
struct ctag_serial_rx {
	struct ctag_cobs_decoder cobs;
	uint32_t len_errors;
	uint32_t crc_errors;
	uint32_t version_errors;
};

/* buf should hold CTAG_SERIAL_MAX_FRAME bytes. */
void ctag_serial_rx_init(struct ctag_serial_rx *rx, uint8_t *buf, size_t size);

/*
 * Feed one byte. Returns true when a valid frame completed: *hdr is filled and
 * the payload is at rx->cobs.buf + CTAG_SERIAL_HEADER_LEN until the next call.
 */
bool ctag_serial_rx_put(struct ctag_serial_rx *rx, uint8_t byte, struct ctag_serial_header *hdr);

/*
 * Credit accounting (1.3). After HELLO both sides start with
 * CTAG_SERIAL_DEFAULT_CREDITS; every frame sent consumes one of the peer's
 * credits; the credits byte of every received frame adds to them; every freed
 * receive buffer is granted back in the next frame sent.
 */
struct ctag_credits {
	uint16_t tx;   /* frames we may still send */
	uint16_t owed; /* freed receive buffers not yet granted */
};

static inline void ctag_credits_reset(struct ctag_credits *c)
{
	c->tx = CTAG_SERIAL_DEFAULT_CREDITS;
	c->owed = 0u;
}

/* Consume a credit for a frame about to be sent; false = wait. */
static inline bool ctag_credits_take(struct ctag_credits *c)
{
	if (c->tx == 0u) {
		return false;
	}
	c->tx--;
	return true;
}

/* Apply the credits byte of a received frame. */
static inline void ctag_credits_add(struct ctag_credits *c, uint8_t grant)
{
	c->tx = (uint16_t)(c->tx > UINT16_MAX - grant ? UINT16_MAX : c->tx + grant);
}

/* A receive buffer was freed. */
static inline void ctag_credits_release(struct ctag_credits *c)
{
	if (c->owed < UINT16_MAX) {
		c->owed++;
	}
}

/* Grant to write into the credits byte of the next frame sent. */
static inline uint8_t ctag_credits_grant(struct ctag_credits *c)
{
	uint8_t g = (uint8_t)(c->owed > UINT8_MAX ? UINT8_MAX : c->owed);

	c->owed = (uint16_t)(c->owed - g);
	return g;
}

#ifdef __cplusplus
}
#endif

#endif /* CTAG_FRAME_H_ */
