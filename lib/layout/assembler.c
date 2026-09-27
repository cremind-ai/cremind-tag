/* Bridge layout assembler (docs/protocol.md 3.2-3.3). */
#include <string.h>

#include <ctag/ctag_layout.h>

void ctag_layout_asm_init(struct ctag_layout_asm *a, uint8_t *buf, size_t size)
{
	memset(a, 0, sizeof(*a));
	a->buf = buf;
	a->size = size;
}

void ctag_layout_asm_begin(struct ctag_layout_asm *a, const struct ctag_mesh_layout_begin *b)
{
	a->begin = *b;
	a->have = 0u;
	a->last_len = 0u;
	a->active = true;
	a->bad = false;
}

uint8_t ctag_layout_asm_chunk(struct ctag_layout_asm *a, const struct ctag_mesh_layout_chunk *c)
{
	size_t off = (size_t)c->index * CTAG_LAYOUT_CHUNK_DATA_MAX;

	if (!a->active || c->xfer_id != a->begin.xfer_id) {
		return CTAG_STATUS_NOT_FOUND;
	}
	if (c->index >= a->begin.chunk_count || c->index >= 32u) {
		return CTAG_STATUS_INVALID;
	}
	if (c->index + 1u == a->begin.chunk_count) {
		a->last_len = (uint16_t)c->data_len;
	} else if (c->data_len != CTAG_LAYOUT_CHUNK_DATA_MAX) {
		a->bad = true; /* 3.2 rule 2: every chunk but the last is full */
	}
	/* Data past the buffer can only belong to a TOO_LARGE transfer. */
	if (off + c->data_len <= a->size) {
		memcpy(&a->buf[off], c->data, c->data_len);
	}
	a->have |= (uint32_t)1u << c->index;
	return CTAG_STATUS_OK;
}

uint8_t ctag_layout_asm_commit(struct ctag_layout_asm *a, uint16_t xfer_id, uint32_t *missing,
			       const struct ctag_sha256_ops *sha)
{
	uint8_t n = a->begin.chunk_count;
	uint32_t want = n >= 32u ? UINT32_MAX : ((uint32_t)1u << n) - 1u;
	uint32_t len = n == 0u ? 0u : (uint32_t)(n - 1u) * CTAG_LAYOUT_CHUNK_DATA_MAX + a->last_len;
	uint8_t digest[32];

	*missing = 0u;
	if (!a->active || xfer_id != a->begin.xfer_id) {
		return CTAG_STATUS_NOT_FOUND;
	}
	if ((a->have & want) != want) {
		*missing = want & ~a->have;
		return CTAG_STATUS_INCOMPLETE;
	}
	if (a->begin.total_len > CTAG_LAYOUT_HARD_MAX || len > CTAG_LAYOUT_HARD_MAX ||
	    a->begin.total_len > a->size) {
		return CTAG_STATUS_TOO_LARGE;
	}
	if (a->bad || len != a->begin.total_len) {
		return CTAG_STATUS_INVALID;
	}
	if (sha->init(sha->ctx) != 0 || sha->update(sha->ctx, a->buf, len) != 0 ||
	    sha->finish(sha->ctx, digest) != 0) {
		return CTAG_STATUS_INTERNAL;
	}
	if (memcmp(digest, a->begin.digest, CTAG_LAYOUT_DIGEST_LEN) != 0) {
		return CTAG_STATUS_DIGEST_MISMATCH;
	}
	return CTAG_STATUS_OK;
}
