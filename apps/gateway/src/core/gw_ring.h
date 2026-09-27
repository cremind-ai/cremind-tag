/*
 * A FIFO byte arena for variable-size records (queued layouts, best-effort
 * events): records are contiguous, may be released in any order, and their
 * space is reclaimed in allocation order. Records are 4-byte aligned behind a
 * 4-byte header; a record that does not fit before the end wraps to the start.
 */
#ifndef GW_RING_H_
#define GW_RING_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct gw_ring {
	uint8_t *buf;
	uint32_t size;  /* multiple of 4 */
	uint32_t head;  /* oldest record (valid when count > 0) */
	uint32_t tail;  /* where the next record goes */
	uint32_t count; /* records not yet reclaimed (live or released) */
	uint32_t reserved; /* bytes offered by the last reserve (0 = none) */
	uint32_t res_at;   /* where that reservation starts */
};

void gw_ring_init(struct gw_ring *r, uint8_t *buf, uint32_t size);
/* Offer the largest contiguous room for one record (at least min bytes);
 * NULL when there is none. *cap receives its size. Finish with commit. */
uint8_t *gw_ring_reserve(struct gw_ring *r, uint32_t min, uint32_t *cap);
/* Keep len bytes (<= cap) of the reservation as a record. */
void gw_ring_commit(struct gw_ring *r, uint32_t len);
/* A record of exactly len bytes, or NULL when it does not fit now. */
uint8_t *gw_ring_alloc(struct gw_ring *r, uint32_t len);
/* Release a record (any order); its space returns in allocation order. */
void gw_ring_free(struct gw_ring *r, uint8_t *rec);
/* The oldest live record and its length, or NULL. */
uint8_t *gw_ring_first(struct gw_ring *r, uint32_t *len);
bool gw_ring_empty(const struct gw_ring *r);
/* Bytes held (headers, padding and released records not yet reclaimed). */
uint32_t gw_ring_used(const struct gw_ring *r);

#endif /* GW_RING_H_ */
