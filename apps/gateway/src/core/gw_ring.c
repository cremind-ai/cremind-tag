/* FIFO byte arena for variable-size records (see gw_ring.h). */
#include <string.h>

#include "gw_ring.h"

#define HDR      4u
#define F_LIVE   0x01u
#define F_WRAP   0x02u
#define ROUND4(n) (((n) + 3u) & ~3u)

static uint32_t rec_size(uint32_t len)
{
	return HDR + ROUND4(len);
}

static void hdr_put(struct gw_ring *r, uint32_t at, uint32_t len, uint8_t flags)
{
	r->buf[at] = (uint8_t)len;
	r->buf[at + 1u] = (uint8_t)(len >> 8);
	r->buf[at + 2u] = flags;
	r->buf[at + 3u] = 0u;
}

static uint32_t hdr_len(const struct gw_ring *r, uint32_t at)
{
	return (uint32_t)r->buf[at] | ((uint32_t)r->buf[at + 1u] << 8);
}

/* Move head past the end of the buffer or a wrap marker. */
static void normalize(struct gw_ring *r)
{
	if (r->head == r->size || (r->buf[r->head + 2u] & F_WRAP) != 0u) {
		r->head = 0u;
	}
}

static void reclaim(struct gw_ring *r)
{
	while (r->count > 0u) {
		normalize(r);
		if ((r->buf[r->head + 2u] & F_LIVE) != 0u) {
			break;
		}
		r->head += rec_size(hdr_len(r, r->head));
		r->count--;
	}
	if (r->count == 0u) {
		r->head = r->tail = 0u; /* empty: the whole buffer is contiguous again */
	}
}

void gw_ring_init(struct gw_ring *r, uint8_t *buf, uint32_t size)
{
	r->buf = buf;
	r->size = size & ~3u;
	r->head = r->tail = 0u;
	r->count = 0u;
	r->reserved = 0u;
	r->res_at = 0u;
}

uint8_t *gw_ring_reserve(struct gw_ring *r, uint32_t min, uint32_t *cap)
{
	uint32_t at = 0u, room = 0u;

	r->reserved = 0u;
	if (r->count == 0u) {
		at = 0u;
		room = r->size;
	} else {
		normalize(r);
		if (r->tail > r->head) {
			uint32_t end = r->size - r->tail;

			/* The end of the buffer, or the start before the oldest record. */
			if (end >= r->head) {
				at = r->tail;
				room = end;
			} else {
				at = 0u;
				room = r->head;
			}
		} else if (r->tail < r->head) {
			at = r->tail;
			room = r->head - r->tail;
		} /* tail == head: full */
	}
	if (room < HDR || room - HDR < ROUND4(min)) {
		return NULL;
	}
	r->reserved = room;
	r->res_at = at;
	*cap = room - HDR;
	return &r->buf[at + HDR];
}

void gw_ring_commit(struct gw_ring *r, uint32_t len)
{
	uint32_t at = r->res_at;

	if (r->reserved == 0u || ROUND4(len) > r->reserved - HDR) {
		return;
	}
	if (r->count > 0u && at != r->tail && r->tail < r->size) {
		hdr_put(r, r->tail, 0u, F_WRAP); /* the record wrapped to the start */
	}
	hdr_put(r, at, len, F_LIVE);
	r->tail = at + rec_size(len);
	r->count++;
	r->reserved = 0u;
}

uint8_t *gw_ring_alloc(struct gw_ring *r, uint32_t len)
{
	uint32_t cap;
	uint8_t *p = gw_ring_reserve(r, len, &cap);

	if (p != NULL) {
		gw_ring_commit(r, len);
	}
	return p;
}

void gw_ring_free(struct gw_ring *r, uint8_t *rec)
{
	uint32_t at;

	if (rec == NULL || rec < r->buf + HDR || rec >= r->buf + r->size) {
		return;
	}
	at = (uint32_t)(rec - r->buf) - HDR;
	r->buf[at + 2u] &= (uint8_t)~F_LIVE;
	reclaim(r);
}

uint8_t *gw_ring_first(struct gw_ring *r, uint32_t *len)
{
	if (r->count == 0u) {
		return NULL;
	}
	normalize(r);
	*len = hdr_len(r, r->head);
	return &r->buf[r->head + HDR];
}

bool gw_ring_empty(const struct gw_ring *r)
{
	return r->count == 0u;
}

uint32_t gw_ring_used(const struct gw_ring *r)
{
	if (r->count == 0u) {
		return 0u;
	}
	return r->tail > r->head ? r->tail - r->head : r->size - r->head + r->tail;
}
