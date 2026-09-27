/*
 * Incremental strict UTF-8 validation (RFC 3629: no overlong forms, no
 * surrogates, nothing above U+10FFFF), the same acceptance as Python's
 * bytes.decode("utf-8"). Used by the font-pack reader for face strings.
 */
#ifndef CTAG_UTF8_H_
#define CTAG_UTF8_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct ctag_utf8 {
	uint8_t need; /* continuation bytes still expected */
	uint8_t lo;   /* bounds of the next continuation byte */
	uint8_t hi;
	bool bad;
};

static inline void ctag_utf8_init(struct ctag_utf8 *u)
{
	u->need = 0;
	u->bad = false;
}

/* Feed bytes; the state may be fed across several calls. */
void ctag_utf8_feed(struct ctag_utf8 *u, const uint8_t *data, size_t len);

/* True when everything fed so far is complete, valid UTF-8. */
static inline bool ctag_utf8_valid(const struct ctag_utf8 *u)
{
	return !u->bad && u->need == 0;
}

#ifdef __cplusplus
}
#endif

#endif /* CTAG_UTF8_H_ */
