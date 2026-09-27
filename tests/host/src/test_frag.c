/* ctag_frag against fragments.json. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_frag.h>

#include "check.h"
#include "suites.h"
#include "v_fragments.h"

static uint8_t msg[CTAG_TAG_RECORD_WIRE_MAX];
static uint8_t value[CTAG_ATT_VALUE_MAX];

void test_frag_vectors(void)
{
	size_t i, k;

	for (i = 0u; i < V_COUNT(v_frag); i++) {
		const struct v_frag *v = &v_frag[i];
		struct ctag_frag_tx tx = {v->seq_start};
		struct ctag_frag_rx rx;
		size_t off = 0u;

		for (k = 0u; k < v->n_frags; k++) {
			int n = ctag_frag_next(&tx, v->msg, v->msg_len, &off, V_FRAG_PAYLOAD,
					       value);

			CHECK_CASE(n == (int)v->frag_lens[k] &&
					   memcmp(value, v->frags[k], (size_t)n) == 0,
				   v->name);
		}
		CHECK_CASE(off == v->msg_len && tx.seq == v->seq_next, v->name);
		CHECK_CASE(ctag_frag_next(&tx, v->msg, v->msg_len, &off, V_FRAG_PAYLOAD, value) ==
				   -EINVAL,
			   v->name);
		ctag_frag_rx_init(&rx, msg, v->max_message);
		rx.seq = v->seq_start;
		for (k = 0u; k < v->n_frags; k++) {
			int n = ctag_frag_rx_put(&rx, v->frags[k], v->frag_lens[k]);

			CHECK_CASE(n == (k + 1u == v->n_frags ? (int)v->msg_len : 0), v->name);
		}
		CHECK_CASE(memcmp(msg, v->msg, v->msg_len) == 0 && rx.seq == v->seq_next, v->name);
	}
}

void test_frag_errors(void)
{
	size_t i, k;

	for (i = 0u; i < V_COUNT(v_frag_errors); i++) {
		const struct v_frag *v = &v_frag_errors[i];
		struct ctag_frag_rx rx;

		ctag_frag_rx_init(&rx, msg, v->max_message);
		for (k = 0u; k < v->error_at; k++) {
			CHECK_CASE(ctag_frag_rx_put(&rx, v->frags[k], v->frag_lens[k]) >= 0,
				   v->name);
		}
		CHECK_CASE(ctag_frag_rx_put(&rx, v->frags[k], v->frag_lens[k]) == -EINVAL, v->name);
		/* Aborted: everything after is refused, even a valid first fragment. */
		value[0] = CTAG_FRAG_START | CTAG_FRAG_END;
		value[1] = 0x05u;
		CHECK_CASE(ctag_frag_rx_put(&rx, value, 2u) == -EINVAL, v->name);
	}
	/* Sender: nothing to send. */
	{
		struct ctag_frag_tx tx;
		size_t off = 0u;

		ctag_frag_tx_init(&tx);
		CHECK(ctag_frag_next(&tx, msg, 0u, &off, V_FRAG_PAYLOAD, value) == -EINVAL);
		CHECK(ctag_frag_next(&tx, msg, 1u, &off, 0u, value) == -EINVAL);
		CHECK(tx.seq == 0u);
	}
}

/* Messages keep flowing across SEQ wrap-around (fragments.py test_reassembler_continues). */
void test_frag_continuous(void)
{
	static const size_t sizes[] = {1u, 40u, 205u, 19u, 64u};
	struct ctag_frag_tx tx;
	struct ctag_frag_rx rx;
	static uint8_t in[CTAG_TAG_RECORD_WIRE_MAX];
	size_t round, i;

	for (i = 0u; i < sizeof(in); i++) {
		in[i] = (uint8_t)i;
	}
	ctag_frag_tx_init(&tx);
	ctag_frag_rx_init(&rx, msg, CTAG_TAG_RECORD_WIRE_MAX);
	for (round = 0u; round < 100u; round++) {
		size_t len = sizes[round % V_COUNT(sizes)];
		size_t off = 0u;
		int n = 0;

		while (off < len) {
			int v = ctag_frag_next(&tx, in, len, &off, CTAG_FRAG_PAYLOAD_MAX, value);

			CHECK(v >= 2 && v <= (int)CTAG_ATT_VALUE_MAX);
			n = ctag_frag_rx_put(&rx, value, (size_t)v);
			CHECK(n >= 0);
		}
		CHECK(n == (int)len && memcmp(msg, in, len) == 0);
	}
	/* One byte over the maximum aborts. */
	ctag_frag_rx_init(&rx, msg, CTAG_TAG_CTRL_MSG_MAX);
	ctag_frag_tx_init(&tx);
	{
		size_t off = 0u;
		int n = 0;

		while (off < CTAG_TAG_CTRL_MSG_MAX + 1u && n >= 0) {
			int v = ctag_frag_next(&tx, in, CTAG_TAG_CTRL_MSG_MAX + 1u, &off,
					       CTAG_FRAG_PAYLOAD_MAX, value);

			n = ctag_frag_rx_put(&rx, value, (size_t)v);
		}
		CHECK(n == -EINVAL);
	}
}
