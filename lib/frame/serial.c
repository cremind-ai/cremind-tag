/* Serial frames (docs/protocol.md 1.1); mirrors companion protocol/serial_frame.py. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_frame.h>

int ctag_serial_frame_build(uint8_t *frame, size_t size, const struct ctag_serial_header *hdr,
			    const uint8_t *payload, size_t payload_len)
{
	struct ctag_serial_header h = *hdr;
	size_t body = CTAG_SERIAL_HEADER_LEN + payload_len;

	if (payload_len > CTAG_SERIAL_MAX_PAYLOAD || size < body + CTAG_SERIAL_CRC_LEN) {
		return -EMSGSIZE;
	}
	h.length = (uint16_t)payload_len;
	if (payload != NULL && payload_len > 0u) {
		memmove(&frame[CTAG_SERIAL_HEADER_LEN], payload, payload_len);
	}
	(void)ctag_serial_header_pack(&h, frame, CTAG_SERIAL_HEADER_LEN);
	ctag_put_le32(&frame[body], ctag_crc32(0u, frame, body));
	return (int)(body + CTAG_SERIAL_CRC_LEN);
}

enum ctag_serial_check ctag_serial_frame_check(const uint8_t *frame, size_t len,
					       struct ctag_serial_header *hdr)
{
	size_t body;

	if (len < CTAG_SERIAL_MIN_FRAME || len > CTAG_SERIAL_MAX_FRAME) {
		return CTAG_SERIAL_CHECK_LEN;
	}
	body = len - CTAG_SERIAL_CRC_LEN;
	if (ctag_crc32(0u, frame, body) != ctag_get_le32(&frame[body])) {
		return CTAG_SERIAL_CHECK_CRC;
	}
	(void)ctag_serial_header_unpack(hdr, frame, CTAG_SERIAL_HEADER_LEN);
	if (hdr->length != body - CTAG_SERIAL_HEADER_LEN) {
		return CTAG_SERIAL_CHECK_LEN;
	}
	if (hdr->version != CTAG_PROTO_VERSION) {
		return CTAG_SERIAL_CHECK_VERSION;
	}
	return CTAG_SERIAL_CHECK_OK;
}

int ctag_serial_wire_encode(const uint8_t *frame, size_t len, uint8_t *wire, size_t size)
{
	int n;

	if (size == 0u) {
		return -EMSGSIZE;
	}
	n = ctag_cobs_encode(frame, len, wire, size - 1u);
	if (n >= 0) {
		wire[n++] = 0u;
	}
	return n;
}

void ctag_serial_rx_init(struct ctag_serial_rx *rx, uint8_t *buf, size_t size)
{
	ctag_cobs_decoder_init(&rx->cobs, buf, size);
	rx->len_errors = 0u;
	rx->crc_errors = 0u;
	rx->version_errors = 0u;
}

bool ctag_serial_rx_put(struct ctag_serial_rx *rx, uint8_t byte, struct ctag_serial_header *hdr)
{
	int n = ctag_cobs_decoder_put(&rx->cobs, byte);

	if (n < 0) {
		return false;
	}
	switch (ctag_serial_frame_check(rx->cobs.buf, (size_t)n, hdr)) {
	case CTAG_SERIAL_CHECK_OK:
		return true;
	case CTAG_SERIAL_CHECK_LEN:
		rx->len_errors++;
		break;
	case CTAG_SERIAL_CHECK_CRC:
		rx->crc_errors++;
		break;
	default:
		rx->version_errors++;
		break;
	}
	return false;
}
