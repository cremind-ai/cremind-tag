/*
 * Platform glue of the vendored Noise* / HACL* code (lib/third_party/noise_ik/
 * README.md): the KaRaMeL allocator on a bounded heap, the abort guard,
 * Lib_Memzero0_memzero and Lib_RandomBuffer_System_crypto_random.
 *
 * Heap: a sys_heap on a static buffer of CONFIG_CTAG_SECURE_HEAP_SIZE bytes
 * under Zephyr, a first-fit pool of CTAG_SECURE_HEAP_SIZE bytes elsewhere
 * (host tests). Every block is wiped when it is freed. The generated code does
 * not check allocation results, so a failed allocation never returns: it
 * leaves the guarded Noise* call through longjmp after the whole heap has been
 * wiped and re-initialised (every Noise object of the old epoch is gone), and
 * that call reports -ENOMEM. The device keeps running.
 */
#include <errno.h>
#include <setjmp.h>
#include <string.h>

#include <ctag/ctag_secure.h>

#include "ctag_krml.h"
#include "secure_int.h"

#ifdef __ZEPHYR__
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/sys_heap.h>
#define HEAP_SIZE CONFIG_CTAG_SECURE_HEAP_SIZE
#else
#include <stdlib.h>
#ifndef CTAG_SECURE_HEAP_SIZE
#define CTAG_SECURE_HEAP_SIZE 16384
#endif
#define HEAP_SIZE CTAG_SECURE_HEAP_SIZE
#endif

/* Declared by Noise* (lib/third_party/noise_ik/Hacl.h) and HACL* (Lib_Memzero0.h). */
void Lib_Memzero0_memzero(void *dst, uint64_t len);
void Lib_RandomBuffer_System_crypto_random(uint8_t *buf, uint32_t len);

jmp_buf ctag_krml_env;
volatile int ctag_krml_reason;
static bool armed;
static uint32_t epoch = 1u;
static size_t used, peak;
static uint32_t failures;
static int32_t fail_after = -1;

/* ---- Wiping ---- */

void ctag_secure_wipe(void *p, size_t len)
{
	volatile uint8_t *v = (volatile uint8_t *)p;

	while (len-- > 0u) {
		*v++ = 0u;
	}
}

void Lib_Memzero0_memzero(void *dst, uint64_t len)
{
	ctag_secure_wipe(dst, (size_t)len);
}

/* ---- Heap ---- */

#ifdef __ZEPHYR__

static struct sys_heap heap;
static uint8_t heap_mem[HEAP_SIZE] __aligned(8);
static bool heap_ready;

static void heap_init(void)
{
	ctag_secure_wipe(heap_mem, sizeof(heap_mem));
	sys_heap_init(&heap, heap_mem, sizeof(heap_mem));
	heap_ready = true;
}

static void *heap_alloc(size_t size, size_t *got)
{
	void *p;

	if (!heap_ready) {
		heap_init();
	}
	p = sys_heap_alloc(&heap, size == 0u ? 1u : size);
	*got = p != NULL ? sys_heap_usable_size(&heap, p) : 0u;
	return p;
}

static size_t heap_release(void *p)
{
	size_t n = sys_heap_usable_size(&heap, p);

	ctag_secure_wipe(p, n);
	sys_heap_free(&heap, p);
	return n;
}

#else /* host: first-fit pool with 8-byte headers */

struct block {
	uint32_t size; /* payload bytes */
	uint32_t free;
};

static union {
	uint64_t align;
	uint8_t bytes[HEAP_SIZE];
} pool;
static bool heap_ready;

#define HDR sizeof(struct block)

static struct block *blk(size_t off)
{
	return (struct block *)(void *)&pool.bytes[off];
}

static void heap_init(void)
{
	ctag_secure_wipe(pool.bytes, sizeof(pool.bytes));
	blk(0)->size = (uint32_t)(sizeof(pool.bytes) - HDR);
	blk(0)->free = 1u;
	heap_ready = true;
}

static void *heap_alloc(size_t size, size_t *got)
{
	size_t need = (size + 7u) & ~(size_t)7u;
	size_t off = 0u;

	if (!heap_ready) {
		heap_init();
	}
	if (need == 0u) {
		need = 8u;
	}
	while (off < sizeof(pool.bytes)) {
		struct block *b = blk(off);

		if (b->free != 0u && b->size >= need) {
			if (b->size >= need + HDR + 8u) {
				struct block *rest = blk(off + HDR + need);

				rest->size = (uint32_t)(b->size - need - HDR);
				rest->free = 1u;
				b->size = (uint32_t)need;
			}
			b->free = 0u;
			*got = b->size + HDR;
			return &pool.bytes[off + HDR];
		}
		off += HDR + b->size;
	}
	*got = 0u;
	return NULL;
}

static size_t heap_release(void *p)
{
	size_t off = (size_t)((uint8_t *)p - pool.bytes) - HDR;
	struct block *b = blk(off);
	size_t n = b->size + HDR;
	size_t prev = SIZE_MAX, cur = 0u;

	ctag_secure_wipe(p, b->size);
	b->free = 1u;
	/* Merge with the following free blocks, then with a free predecessor. */
	while (off + HDR + b->size < sizeof(pool.bytes) && blk(off + HDR + b->size)->free != 0u) {
		struct block *next = blk(off + HDR + b->size);

		b->size += (uint32_t)HDR + next->size;
		ctag_secure_wipe(next, HDR);
	}
	while (cur < off) {
		prev = cur;
		cur += HDR + blk(cur)->size;
	}
	if (prev != SIZE_MAX && blk(prev)->free != 0u) {
		blk(prev)->size += (uint32_t)HDR + b->size;
		ctag_secure_wipe(b, HDR);
	}
	return n;
}

#endif

static void heap_reset(void)
{
	heap_init();
	used = 0u;
	epoch++;
	if (epoch == 0u) {
		epoch = 1u;
	}
}

static void abort_call(int reason) __attribute__((noreturn));

static void abort_call(int reason)
{
	failures++;
	if (!armed) {
		/* Only guarded Noise* calls allocate or abort; nothing to unwind to. */
#ifdef __ZEPHYR__
		k_panic();
		CODE_UNREACHABLE;
#else
		abort();
#endif
	}
	armed = false;
	ctag_krml_reason = reason;
	heap_reset();
	longjmp(ctag_krml_env, 1);
}

void *ctag_krml_malloc(size_t size)
{
	size_t got = 0u;
	void *p = NULL;

	if (fail_after != 0) {
		p = heap_alloc(size, &got);
		if (fail_after > 0) {
			fail_after--;
		}
	}
	if (p == NULL) {
		abort_call(-ENOMEM);
	}
	used += got;
	if (used > peak) {
		peak = used;
	}
	return p;
}

void *ctag_krml_calloc(size_t n, size_t size)
{
	void *p;

	if (size != 0u && n > SIZE_MAX / size) {
		abort_call(-ENOMEM);
	}
	p = ctag_krml_malloc(n * size);
	memset(p, 0, n * size);
	return p;
}

void ctag_krml_free(void *p)
{
	if (p != NULL) {
		size_t n = heap_release(p);

		used = n <= used ? used - n : 0u;
	}
}

void ctag_krml_exit(int code)
{
	(void)code; /* KaRaMeL's unreachable / size-check paths */
	abort_call(-EFAULT);
}

void ctag_krml_arm(void)
{
	armed = true;
}

void ctag_krml_disarm(void)
{
	armed = false;
}

uint32_t ctag_krml_epoch(void)
{
	return epoch;
}

void ctag_secure_heap_stats(struct ctag_secure_heap_stats *s)
{
	s->size = HEAP_SIZE;
	s->used = used;
	s->peak = peak;
	s->failures = failures;
}

void ctag_secure_heap_reset_peak(void)
{
	peak = used;
}

void ctag_secure_heap_fail_after(int32_t after)
{
	fail_after = after;
}

/* ---- Randomness ---- */

#ifdef __ZEPHYR__
static int csrand(void *ctx, uint8_t *buf, size_t len)
{
	(void)ctx;
	return sys_csrand_get(buf, len);
}

static ctag_secure_rng_fn rng_fn = csrand;
#else
static ctag_secure_rng_fn rng_fn;
#endif
static void *rng_ctx;
static uint8_t test_eph[32];
static bool test_eph_armed;

void ctag_secure_rng_set(ctag_secure_rng_fn fn, void *ctx)
{
	rng_fn = fn;
	rng_ctx = ctx;
}

void ctag_secure_test_ephemeral(const uint8_t e[32])
{
	memcpy(test_eph, e, sizeof(test_eph));
	test_eph_armed = true;
}

/* Noise* draws each session's ephemeral key here (the only call site). */
void Lib_RandomBuffer_System_crypto_random(uint8_t *buf, uint32_t len)
{
	if (test_eph_armed && len == sizeof(test_eph)) {
		memcpy(buf, test_eph, sizeof(test_eph));
		ctag_secure_wipe(test_eph, sizeof(test_eph));
		test_eph_armed = false;
		return;
	}
	if (rng_fn == NULL || rng_fn(rng_ctx, buf, len) != 0) {
		ctag_secure_wipe(buf, len);
		abort_call(-EIO); /* never a weak ephemeral */
	}
}
