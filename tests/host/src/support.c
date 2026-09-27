/*
 * Test-only helpers. The SHA-256 below is written for these tests from FIPS
 * 180-4 and is placed in the public domain; firmware uses PSA instead.
 */
#include <errno.h>
#include <string.h>

#include "support.h"

static const uint32_t k256[64] = {
	0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u,
	0xab1c5ed5u, 0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu,
	0x9bdc06a7u, 0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu,
	0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
	0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
	0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u, 0xa2bfe8a1u, 0xa81a664bu,
	0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u,
	0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
	0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u,
	0xc67178f2u,
};

static uint32_t ror(uint32_t x, unsigned int n)
{
	return (x >> n) | (x << (32u - n));
}

static void block(struct t_sha256 *s, const uint8_t *p)
{
	uint32_t w[64];
	uint32_t v[8];
	unsigned int i;

	for (i = 0u; i < 16u; i++) {
		w[i] = (uint32_t)p[4u * i] << 24 | (uint32_t)p[4u * i + 1u] << 16 |
		       (uint32_t)p[4u * i + 2u] << 8 | p[4u * i + 3u];
	}
	for (; i < 64u; i++) {
		uint32_t s0 = ror(w[i - 15u], 7) ^ ror(w[i - 15u], 18) ^ (w[i - 15u] >> 3);
		uint32_t s1 = ror(w[i - 2u], 17) ^ ror(w[i - 2u], 19) ^ (w[i - 2u] >> 10);

		w[i] = w[i - 16u] + s0 + w[i - 7u] + s1;
	}
	memcpy(v, s->h, sizeof(v));
	for (i = 0u; i < 64u; i++) {
		uint32_t t1 = v[7] + (ror(v[4], 6) ^ ror(v[4], 11) ^ ror(v[4], 25)) +
			      ((v[4] & v[5]) ^ (~v[4] & v[6])) + k256[i] + w[i];
		uint32_t t2 = (ror(v[0], 2) ^ ror(v[0], 13) ^ ror(v[0], 22)) +
			      ((v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]));

		memmove(&v[1], &v[0], 7u * sizeof(v[0]));
		v[4] += t1;
		v[0] = t1 + t2;
	}
	for (i = 0u; i < 8u; i++) {
		s->h[i] += v[i];
	}
}

void t_sha256_init(struct t_sha256 *s)
{
	static const uint32_t iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
				       0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};

	memcpy(s->h, iv, sizeof(iv));
	s->bits = 0u;
	s->n = 0u;
}

void t_sha256_update(struct t_sha256 *s, const uint8_t *data, size_t len)
{
	s->bits += (uint64_t)len * 8u;
	while (len > 0u) {
		size_t take = 64u - s->n < len ? 64u - s->n : len;

		memcpy(&s->buf[s->n], data, take);
		s->n += take;
		data += take;
		len -= take;
		if (s->n == 64u) {
			block(s, s->buf);
			s->n = 0u;
		}
	}
}

void t_sha256_final(struct t_sha256 *s, uint8_t digest[32])
{
	uint64_t bits = s->bits;
	uint8_t pad = 0x80u;
	unsigned int i;

	t_sha256_update(s, &pad, 1u);
	pad = 0u;
	while (s->n != 56u) {
		t_sha256_update(s, &pad, 1u);
	}
	for (i = 0u; i < 8u; i++) {
		s->buf[56u + i] = (uint8_t)(bits >> (56u - 8u * i));
	}
	block(s, s->buf);
	for (i = 0u; i < 32u; i++) {
		digest[i] = (uint8_t)(s->h[i / 4u] >> (24u - 8u * (i % 4u)));
	}
}

void t_sha256(const uint8_t *data, size_t len, uint8_t digest[32])
{
	struct t_sha256 s;

	t_sha256_init(&s);
	t_sha256_update(&s, data, len);
	t_sha256_final(&s, digest);
}

static int ops_init(void *ctx)
{
	t_sha256_init(ctx);
	return 0;
}

static int ops_update(void *ctx, const uint8_t *data, size_t len)
{
	t_sha256_update(ctx, data, len);
	return 0;
}

static int ops_finish(void *ctx, uint8_t digest[32])
{
	t_sha256_final(ctx, digest);
	return 0;
}

void t_sha256_ops(struct ctag_sha256_ops *ops, struct t_sha256 *ctx)
{
	ops->init = ops_init;
	ops->update = ops_update;
	ops->finish = ops_finish;
	ops->ctx = ctx;
}

int t_mem_read(void *ctx, uint32_t offset, void *buf, size_t len)
{
	struct t_mem *m = ctx;

	m->reads++;
	if (offset > m->len || len > m->len - offset) {
		return -EIO;
	}
	memcpy(buf, &m->data[offset], len);
	return 0;
}
