/*
 * Secure-message framing, tunnel fragments (messages.py; docs/connect-setup.md
 * 3.2, 6) and the canonical CBOR answer of a secure-endpoint message
 * (cbor_msgs.encode_response of {status, **Outcome.fields}).
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_secure.h>

#include "secure_int.h"

void ctag_secure_hdr_pack(const struct ctag_secure_hdr *h, uint8_t out[CTAG_SECURE_HEADER_LEN])
{
	out[0] = h->type;
	out[1] = h->flags;
	ctag_put_le16(&out[2], h->request_id);
}

int ctag_secure_hdr_unpack(struct ctag_secure_hdr *h, const uint8_t *in, size_t len)
{
	if (len < CTAG_SECURE_HEADER_LEN) {
		return -EBADMSG;
	}
	h->type = in[0];
	h->flags = in[1];
	h->request_id = ctag_get_le16(&in[2]);
	return 0;
}

/* ---- Tunnel fragments ---- */

int ctag_tunnel_frag(size_t len, size_t off, uint8_t *seq, uint8_t *flags)
{
	size_t n;

	if (len == 0u || len > CTAG_TUNNEL_MSG_MAX) {
		return -EINVAL;
	}
	if (off >= len) {
		return 0;
	}
	if (off % CTAG_TUNNEL_DATA_MAX != 0u) {
		return -EINVAL;
	}
	n = len - off < CTAG_TUNNEL_DATA_MAX ? len - off : CTAG_TUNNEL_DATA_MAX;
	*seq = (uint8_t)(off / CTAG_TUNNEL_DATA_MAX);
	*flags = (uint8_t)((off == 0u ? CTAG_TUNNEL_FRAG_START : 0u) |
			   (off + n == len ? CTAG_TUNNEL_FRAG_END : 0u));
	return (int)n;
}

void ctag_tunnel_rx_init(struct ctag_tunnel_rx *rx, uint8_t *buf, uint16_t limit)
{
	rx->buf = buf;
	rx->limit = limit;
	rx->len = 0u;
	rx->next = 0u;
	rx->active = false;
}

static void rx_reset(struct ctag_tunnel_rx *rx)
{
	rx->len = 0u;
	rx->next = 0u;
	rx->active = false;
}

int ctag_tunnel_rx_feed(struct ctag_tunnel_rx *rx, uint8_t seq, uint8_t flags,
			const uint8_t *data, size_t len, size_t *msg_len)
{
	if ((flags & CTAG_TUNNEL_FRAG_START) != 0u) {
		rx->active = true;
		rx->len = 0u;
		rx->next = 0u;
	}
	if (!rx->active || seq != rx->next) {
		rx_reset(rx);
		return -EBADMSG; /* out of order: the message is lost */
	}
	if (len > (size_t)(rx->limit - rx->len)) {
		rx_reset(rx);
		return -EBADMSG;
	}
	if (len > 0u) {
		memcpy(&rx->buf[rx->len], data, len);
	}
	rx->len = (uint16_t)(rx->len + len);
	rx->next++;
	if ((flags & CTAG_TUNNEL_FRAG_END) != 0u) {
		*msg_len = rx->len;
		rx_reset(rx);
		return 1;
	}
	return 0;
}

/* ---- Answers ---- */

struct wr {
	uint8_t *p;
	size_t size;
	size_t off;
	bool full;
};

static void put(struct wr *w, const uint8_t *src, size_t n)
{
	if (w->full || w->size - w->off < n) {
		w->full = true;
		return;
	}
	memcpy(&w->p[w->off], src, n);
	w->off += n;
}

static void put_head(struct wr *w, uint8_t major, uint32_t v)
{
	uint8_t h[5];
	size_t n;
	uint8_t m = (uint8_t)(major << 5);

	if (v < 24u) {
		h[0] = (uint8_t)(m | v);
		n = 1u;
	} else if (v <= 0xFFu) {
		h[0] = (uint8_t)(m | 24u);
		h[1] = (uint8_t)v;
		n = 2u;
	} else if (v <= 0xFFFFu) {
		h[0] = (uint8_t)(m | 25u);
		h[1] = (uint8_t)(v >> 8);
		h[2] = (uint8_t)v;
		n = 3u;
	} else {
		h[0] = (uint8_t)(m | 26u);
		h[1] = (uint8_t)(v >> 24);
		h[2] = (uint8_t)(v >> 16);
		h[3] = (uint8_t)(v >> 8);
		h[4] = (uint8_t)v;
		n = 5u;
	}
	put(w, h, n);
}

static void put_bstr(struct wr *w, uint8_t key, const uint8_t *p, size_t n)
{
	put_head(w, 0u, key);
	put_head(w, 2u, (uint32_t)n);
	put(w, p, n);
}

int ctag_secure_answer_encode(const struct ctag_secure_answer *a, uint8_t *buf, size_t size)
{
	struct wr w = {.p = buf, .size = size};
	uint8_t f = a->fields;
	uint32_t count = 1u;
	static const uint8_t f_false = 0xF4u, f_true = 0xF5u;

	count += (f & CTAG_SECURE_F_DATA) != 0u ? 1u : 0u;
	count += (f & CTAG_SECURE_F_STATE) != 0u ? 3u : 0u;
	count += (f & CTAG_SECURE_F_GEN) != 0u ? 1u : 0u;
	count += (f & CTAG_SECURE_F_OWNED) != 0u ? 1u : 0u;
	count += (f & CTAG_SECURE_F_OWNER) != 0u ? 1u : 0u;
	count += (f & CTAG_SECURE_F_PROOF) != 0u ? 1u : 0u;
	count += (f & CTAG_SECURE_F_ROOT_PROOF) != 0u ? 1u : 0u;
	/* Keys ascending: status 0, data 41, owner_state 66, gen 67,
	 * authority_id 68, challenge 69, proof 75, owner 76,
	 * controller_match 77, root_proof 78. */
	put_head(&w, 5u, count);
	put_head(&w, 0u, CTAG_CBOR_KEY_STATUS);
	put_head(&w, 0u, a->status);
	if ((f & CTAG_SECURE_F_DATA) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_DATA, a->data, a->data_len);
	}
	if ((f & CTAG_SECURE_F_STATE) != 0u) {
		put_head(&w, 0u, CTAG_CBOR_KEY_OWNER_STATE);
		put_head(&w, 0u, a->owner_state);
	}
	if ((f & CTAG_SECURE_F_GEN) != 0u) {
		put_head(&w, 0u, CTAG_CBOR_KEY_GEN);
		put_head(&w, 0u, a->gen);
	}
	if ((f & CTAG_SECURE_F_OWNED) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_AUTHORITY_ID, a->authority_id, sizeof(a->authority_id));
	}
	if ((f & CTAG_SECURE_F_STATE) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_CHALLENGE, a->challenge, sizeof(a->challenge));
	}
	if ((f & CTAG_SECURE_F_PROOF) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_PROOF, a->proof, sizeof(a->proof));
	}
	if ((f & CTAG_SECURE_F_OWNER) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_OWNER, a->owner, sizeof(a->owner));
	}
	if ((f & CTAG_SECURE_F_STATE) != 0u) {
		put_head(&w, 0u, CTAG_CBOR_KEY_CONTROLLER_MATCH);
		put(&w, a->controller_match ? &f_true : &f_false, 1u);
	}
	if ((f & CTAG_SECURE_F_ROOT_PROOF) != 0u) {
		put_bstr(&w, CTAG_CBOR_KEY_ROOT_PROOF, a->root_proof, sizeof(a->root_proof));
	}
	return w.full ? -EMSGSIZE : (int)w.off;
}
