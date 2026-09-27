/*
 * Test-only helpers: a small public-domain SHA-256 (the libraries never hash
 * themselves) and a font-pack reader over the fixture pack.
 */
#ifndef CTAG_TEST_SUPPORT_H_
#define CTAG_TEST_SUPPORT_H_

#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_layout.h>

struct t_sha256 {
	uint32_t h[8];
	uint64_t bits;
	uint8_t buf[64];
	size_t n;
};

void t_sha256_init(struct t_sha256 *s);
void t_sha256_update(struct t_sha256 *s, const uint8_t *data, size_t len);
void t_sha256_final(struct t_sha256 *s, uint8_t digest[32]);
void t_sha256(const uint8_t *data, size_t len, uint8_t digest[32]);

/* ctag_sha256_ops over *ctx. */
void t_sha256_ops(struct ctag_sha256_ops *ops, struct t_sha256 *ctx);

/* A pack in memory, readable through ctag_fontpack_read_fn. */
struct t_mem {
	const uint8_t *data;
	size_t len;
	unsigned int reads;
};

int t_mem_read(void *ctx, uint32_t offset, void *buf, size_t len);

#endif /* CTAG_TEST_SUPPORT_H_ */
