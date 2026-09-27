/* GATT fragmentation (docs/protocol.md 5.3). */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_frag.h>

#define SEQ_ABORTED 0xFFu

int ctag_frag_next(struct ctag_frag_tx *tx, const uint8_t *msg, size_t len, size_t *off,
		   size_t max_payload, uint8_t *out)
{
	uint8_t hdr = tx->seq;
	size_t n;

	if (*off >= len || max_payload == 0u) {
		return -EINVAL;
	}
	n = len - *off;
	if (*off == 0u) {
		hdr |= CTAG_FRAG_START;
	}
	if (n <= max_payload) {
		hdr |= CTAG_FRAG_END;
	} else {
		n = max_payload;
	}
	out[0] = hdr;
	memcpy(&out[1], &msg[*off], n);
	*off += n;
	tx->seq = (uint8_t)((tx->seq + 1u) & CTAG_FRAG_SEQ_MASK);
	return (int)(n + 1u);
}

void ctag_frag_rx_init(struct ctag_frag_rx *rx, uint8_t *buf, uint16_t max_msg)
{
	rx->buf = buf;
	rx->max_msg = max_msg;
	rx->len = 0u;
	rx->seq = 0u;
	rx->active = 0u;
}

int ctag_frag_rx_put(struct ctag_frag_rx *rx, const uint8_t *value, size_t len)
{
	uint8_t hdr;

	/* Checked in the order of fragments.py: empty, SEQ, START, length. */
	if (len < 2u || (value[0] & CTAG_FRAG_SEQ_MASK) != rx->seq) {
		goto abort;
	}
	hdr = value[0];
	rx->seq = (uint8_t)((rx->seq + 1u) & CTAG_FRAG_SEQ_MASK);
	if ((hdr & CTAG_FRAG_START) != 0u) {
		if (rx->active) {
			goto abort;
		}
		rx->active = 1u;
		rx->len = 0u;
	} else if (!rx->active) {
		goto abort;
	}
	len--;
	if (len > (size_t)(rx->max_msg - rx->len)) {
		goto abort;
	}
	memcpy(&rx->buf[rx->len], &value[1], len);
	rx->len = (uint16_t)(rx->len + len);
	if ((hdr & CTAG_FRAG_END) == 0u) {
		return 0;
	}
	rx->active = 0u;
	return rx->len;

abort:
	rx->seq = SEQ_ABORTED;
	return -EINVAL;
}
