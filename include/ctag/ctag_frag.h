/*
 * GATT fragmentation of CTRL, DATA and STATUS messages (docs/protocol.md 5.3).
 * One sender and one reassembler per characteristic and direction; SEQ starts
 * at 0 on connect. Mirrors Cremind's app/tags/runtime/protocol/fragments.py.
 */
#ifndef CTAG_FRAG_H_
#define CTAG_FRAG_H_

#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_ids.h>

#ifdef __cplusplus
extern "C" {
#endif

struct ctag_frag_tx {
	uint8_t seq;
};

static inline void ctag_frag_tx_init(struct ctag_frag_tx *tx)
{
	tx->seq = 0u;
}

/*
 * Write the fragment of msg[0..len) starting at *off into out (room for
 * max_payload + 1 bytes) and advance *off; every fragment but the last carries
 * max_payload bytes. Returns the ATT value length, or -EINVAL when there is
 * nothing left to send (len = 0 or *off = len). The caller keeps len within
 * the characteristic's maximum message.
 */
int ctag_frag_next(struct ctag_frag_tx *tx, const uint8_t *msg, size_t len, size_t *off,
		   size_t max_payload, uint8_t *out);

struct ctag_frag_rx {
	uint8_t *buf;     /* max_msg bytes; may be swapped between messages */
	uint16_t max_msg; /* TAG_CTRL_MSG_MAX (CTRL) or TAG_RECORD_WIRE_MAX */
	uint16_t len;
	uint8_t seq;    /* expected next; 0xFF once aborted */
	uint8_t active; /* inside a message */
};

void ctag_frag_rx_init(struct ctag_frag_rx *rx, uint8_t *buf, uint16_t max_msg);

/*
 * Consume one ATT value. Returns the message length (> 0) when the value
 * completes a message (in rx->buf), 0 when more fragments are needed, or
 * -EINVAL when a 5.3 rule is broken (the session aborts with INVALID; the
 * reassembler then rejects everything until re-initialised).
 */
int ctag_frag_rx_put(struct ctag_frag_rx *rx, const uint8_t *value, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_FRAG_H_ */
