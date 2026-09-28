/*
 * Grants: the server-signed authorisation of one ownership change (grants.py;
 * docs/connect-setup.md 3.1).
 *
 *     grant = canonical CBOR {0: 2, 1: op, 2: device_id, 3: role,
 *                             4: authority_pub, 5: owner, 6: controller,
 *                             7: gen_from, 8: gen_to, 9: challenge}
 *
 * Grant.decode accepts a grant only when re-encoding its values gives the
 * input back, so the decoder here accepts exactly that encoding: map(10) with
 * keys 0..9 in order, integers in their shortest form, byte strings of the
 * exact lengths, nothing after the map.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_secure.h>

#include "secure_int.h"

#define MAJOR_UINT 0u
#define MAJOR_BSTR 2u
#define MAJOR_MAP  5u

/* ---- Encoding ---- */

static size_t put_head(uint8_t *out, uint8_t major, uint32_t v)
{
	uint8_t m = (uint8_t)(major << 5);

	if (v < 24u) {
		out[0] = (uint8_t)(m | v);
		return 1u;
	}
	if (v <= 0xFFu) {
		out[0] = (uint8_t)(m | 24u);
		out[1] = (uint8_t)v;
		return 2u;
	}
	if (v <= 0xFFFFu) {
		out[0] = (uint8_t)(m | 25u);
		out[1] = (uint8_t)(v >> 8);
		out[2] = (uint8_t)v;
		return 3u;
	}
	out[0] = (uint8_t)(m | 26u);
	out[1] = (uint8_t)(v >> 24);
	out[2] = (uint8_t)(v >> 16);
	out[3] = (uint8_t)(v >> 8);
	out[4] = (uint8_t)v;
	return 5u;
}

int ctag_grant_encode(const struct ctag_grant *g, uint8_t *buf, size_t size)
{
	uint8_t tmp[CTAG_GRANT_MAX];
	size_t n = 0u;
	const struct {
		const uint8_t *p;
		size_t len;
	} blobs[] = {
		{g->device_id, sizeof(g->device_id)},   {g->authority_pub, sizeof(g->authority_pub)},
		{g->owner, sizeof(g->owner)},           {g->controller, sizeof(g->controller)},
		{g->challenge, sizeof(g->challenge)},
	};
	const uint32_t ints[] = {CTAG_GRANT_VERSION, g->op, g->role, g->gen_from, g->gen_to};
	/* key -> value source: ints[0..4] or blobs[0..4] */
	static const uint8_t src[10] = {0x00, 0x01, 0x80, 0x02, 0x81, 0x82, 0x83, 0x03, 0x04, 0x84};

	tmp[n++] = (uint8_t)(MAJOR_MAP << 5 | 10u);
	for (uint8_t key = 0u; key < 10u; key++) {
		tmp[n++] = key;
		if ((src[key] & 0x80u) != 0u) {
			const uint8_t b = (uint8_t)(src[key] & 0x7Fu);

			n += put_head(&tmp[n], MAJOR_BSTR, (uint32_t)blobs[b].len);
			memcpy(&tmp[n], blobs[b].p, blobs[b].len);
			n += blobs[b].len;
		} else {
			n += put_head(&tmp[n], MAJOR_UINT, ints[src[key]]);
		}
	}
	if (n > size) {
		return -EMSGSIZE;
	}
	memcpy(buf, tmp, n);
	return (int)n;
}

/* ---- Decoding ---- */

struct rd {
	const uint8_t *p;
	size_t len;
	size_t off;
	bool bad;
};

/* A canonical unsigned integer of at most 32 bits. */
static uint32_t get_uint(struct rd *r)
{
	uint8_t head;
	uint8_t info;
	uint32_t v = 0u;
	size_t width;

	if (r->bad || r->off >= r->len) {
		r->bad = true;
		return 0u;
	}
	head = r->p[r->off++];
	info = (uint8_t)(head & 0x1Fu);
	if ((head >> 5) != MAJOR_UINT) {
		r->bad = true;
		return 0u;
	}
	if (info < 24u) {
		return info;
	}
	width = info == 24u ? 1u : info == 25u ? 2u : info == 26u ? 4u : 0u;
	if (width == 0u || r->len - r->off < width) {
		r->bad = true; /* 8-byte values exceed a u32; 28..31 are not integers */
		return 0u;
	}
	for (size_t i = 0u; i < width; i++) {
		v = v << 8 | r->p[r->off++];
	}
	if (v < (width == 1u ? 24u : width == 2u ? 0x100u : 0x10000u)) {
		r->bad = true; /* not the shortest form */
	}
	return v;
}

/* A byte string of exactly n bytes (n < 256, so its head is canonical). */
static void get_bstr(struct rd *r, uint8_t *out, size_t n)
{
	size_t head = n < 24u ? 1u : 2u;

	if (r->bad || r->len - r->off < head + n) {
		r->bad = true;
		return;
	}
	if (head == 1u ? r->p[r->off] != (uint8_t)(MAJOR_BSTR << 5 | n)
		       : r->p[r->off] != (uint8_t)(MAJOR_BSTR << 5 | 24u) ||
				 r->p[r->off + 1u] != (uint8_t)n) {
		r->bad = true;
		return;
	}
	memcpy(out, &r->p[r->off + head], n);
	r->off += head + n;
}

static void get_key(struct rd *r, uint8_t key)
{
	if (r->bad || r->off >= r->len || r->p[r->off] != key) {
		r->bad = true;
		return;
	}
	r->off++;
}

int ctag_grant_decode(const uint8_t *raw, size_t len, struct ctag_grant *g)
{
	struct rd r = {.p = raw, .len = len};
	uint32_t version, op, role;

	memset(g, 0, sizeof(*g));
	if (len == 0u || len > CTAG_GRANT_MAX || raw[0] != (uint8_t)(MAJOR_MAP << 5 | 10u)) {
		return -EBADMSG;
	}
	r.off = 1u;
	get_key(&r, 0u);
	version = get_uint(&r);
	get_key(&r, 1u);
	op = get_uint(&r);
	get_key(&r, 2u);
	get_bstr(&r, g->device_id, sizeof(g->device_id));
	get_key(&r, 3u);
	role = get_uint(&r);
	get_key(&r, 4u);
	get_bstr(&r, g->authority_pub, sizeof(g->authority_pub));
	get_key(&r, 5u);
	get_bstr(&r, g->owner, sizeof(g->owner));
	get_key(&r, 6u);
	get_bstr(&r, g->controller, sizeof(g->controller));
	get_key(&r, 7u);
	g->gen_from = get_uint(&r);
	get_key(&r, 8u);
	g->gen_to = get_uint(&r);
	get_key(&r, 9u);
	get_bstr(&r, g->challenge, sizeof(g->challenge));
	if (r.bad || r.off != len || version != CTAG_GRANT_VERSION || op < CTAG_GRANT_OP_CLAIM ||
	    op > CTAG_GRANT_OP_MAINT || role < CTAG_NODE_ROLE_GATEWAY || role > CTAG_NODE_ROLE_TAG) {
		memset(g, 0, sizeof(*g));
		return -EBADMSG;
	}
	g->op = (uint8_t)op;
	g->role = (uint8_t)role;
	return 0;
}

/* ---- The device rules (check_grant) ---- */

static uint8_t owned_ops(uint8_t role)
{
	switch (role) {
	case CTAG_NODE_ROLE_GATEWAY:
		return CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RECOVER) | CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RELEASE);
	case CTAG_NODE_ROLE_BRIDGE:
		return CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_REKEY) | CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RELEASE) |
		       CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_MAINT);
	case CTAG_NODE_ROLE_TAG:
		return CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_REKEY) | CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RELEASE);
	default:
		return 0u;
	}
}

static uint8_t first_ownership_op(uint8_t role)
{
	return role == CTAG_NODE_ROLE_GATEWAY ? CTAG_GRANT_OP_CLAIM : CTAG_GRANT_OP_PAIR;
}

uint8_t ctag_grant_check(const struct ctag_grant_ctx *c, const uint8_t *grant, size_t len,
			 const uint8_t *sig, size_t sig_len, struct ctag_grant *g, bool *decoded)
{
	struct ctag_grant tmp;
	struct ctag_grant *gr = g != NULL ? g : &tmp;

	if (decoded != NULL) {
		*decoded = false;
	}
	/* 1. a canonical v2 grant and a signature of the right size */
	if (ctag_grant_decode(grant, len, gr) != 0) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	if (sig_len != CTAG_GRANT_SIG_LEN) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	if (decoded != NULL) {
		*decoded = true;
	}
	/* 1. this device, and an op the carrying message stands for */
	if (memcmp(gr->device_id, c->device_id, CTAG_DEVICE_ID_LEN) != 0 || gr->role != c->role ||
	    (c->ops & CTAG_GRANT_OP_BIT(gr->op)) == 0u) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	/* 2. the current single-use challenge */
	if (c->challenge == NULL ||
	    !ctag_secure_equal(gr->challenge, c->challenge, CTAG_CHALLENGE_LEN)) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	/* 3. gen_from = the stored generation, gen_to = gen_from + 1 */
	if (gr->gen_from != c->gen || (uint64_t)gr->gen_to != (uint64_t)gr->gen_from + 1u) {
		return CTAG_STATUS_STALE_GENERATION;
	}
	/* 4. the session's initiator */
	if (!ctag_secure_equal(gr->controller, c->session_controller, CTAG_IDENTITY_KEY_LEN)) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	/* 5. signed by the authority it names */
	if (!ctag_secure_grant_sig_ok(gr->authority_pub, grant, len, sig)) {
		return CTAG_STATUS_GRANT_INVALID;
	}
	/* 6. the pinned authority and owner, or the first-ownership op */
	if (c->state == CTAG_OWNER_OWNED) {
		if ((owned_ops(c->role) & CTAG_GRANT_OP_BIT(gr->op)) == 0u) {
			return CTAG_STATUS_NOT_OWNER;
		}
		if (!ctag_secure_equal(gr->authority_pub, c->authority_pub, CTAG_AUTHORITY_KEY_LEN) ||
		    !ctag_secure_equal(gr->owner, c->owner, CTAG_OWNER_LEN)) {
			return CTAG_STATUS_NOT_OWNER;
		}
	} else {
		if (gr->op != first_ownership_op(c->role)) {
			return CTAG_STATUS_NOT_OWNER;
		}
		if (c->role != CTAG_NODE_ROLE_GATEWAY && c->setup_proof_ok != 1) {
			return CTAG_STATUS_PROOF_FAILED;
		}
	}
	return CTAG_STATUS_OK;
}
