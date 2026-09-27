/* CRC-32 (crc32.json) and the UTF-8 validator. */
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_utf8.h>

#include "check.h"
#include "suites.h"
#include "v_crc32.h"

void test_crc32_vectors(void)
{
	size_t i;

	for (i = 0u; i < V_COUNT(v_crc32); i++) {
		const struct v_crc32 *v = &v_crc32[i];

		CHECK_CASE(ctag_crc32(0u, v->data, v->len) == v->crc, v->name);
	}
}

void test_crc32_chaining(void)
{
	const struct v_crc32 *v = NULL;
	size_t split;

	for (split = 0u; split < V_COUNT(v_crc32); split++) {
		if (v_crc32[split].len > 200u) {
			v = &v_crc32[split];
		}
	}
	CHECK(v != NULL);
	for (split = 0u; split <= v->len; split += 17u) {
		uint32_t crc = ctag_crc32(0u, v->data, split);

		CHECK(ctag_crc32(crc, &v->data[split], v->len - split) == v->crc);
	}
}

static bool utf8_ok(const char *s)
{
	struct ctag_utf8 u;

	ctag_utf8_init(&u);
	ctag_utf8_feed(&u, (const uint8_t *)s, strlen(s));
	return ctag_utf8_valid(&u);
}

void test_utf8(void)
{
	struct ctag_utf8 u;

	CHECK(utf8_ok(""));
	CHECK(utf8_ok("Noto Sans Arabic 2.013"));
	CHECK(utf8_ok("\xc3\xa9\xe4\xb8\xad\xf0\x9f\x98\x80\xf4\x8f\xbf\xbf"));
	CHECK(!utf8_ok("\x80"));             /* lone continuation */
	CHECK(!utf8_ok("\xc0\xaf"));         /* overlong 2-byte */
	CHECK(!utf8_ok("\xe0\x80\xaf"));     /* overlong 3-byte */
	CHECK(!utf8_ok("\xf0\x80\x80\xaf")); /* overlong 4-byte */
	CHECK(!utf8_ok("\xed\xa0\x80"));     /* surrogate */
	CHECK(!utf8_ok("\xf4\x90\x80\x80")); /* above U+10FFFF */
	CHECK(!utf8_ok("\xf5\x80\x80\x80"));
	CHECK(!utf8_ok("\xe4\xb8"));     /* truncated */
	CHECK(!utf8_ok("\xe4\x61\xad")); /* ASCII inside a sequence */
	/* Split across feeds. */
	ctag_utf8_init(&u);
	ctag_utf8_feed(&u, (const uint8_t *)"\xf0\x9f", 2u);
	CHECK(!ctag_utf8_valid(&u));
	ctag_utf8_feed(&u, (const uint8_t *)"\x98\x80", 2u);
	CHECK(ctag_utf8_valid(&u));
}
