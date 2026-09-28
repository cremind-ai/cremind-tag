/*
 * KaRaMeL configuration for the vendored Noise* and HACL* sources
 * (lib/third_party/noise_ik/README.md). Force-included (-include) into every
 * vendored translation unit, and included first by the two ctag_secure files
 * that call them, so krml/internal/target.h sees these definitions instead of
 * its libc defaults:
 *
 * - allocation goes to the bounded secure heap (hacl_glue.c); a failed
 *   allocation or a KaRaMeL abort leaves the Noise* call in progress through
 *   ctag_krml_exit(), which never returns (the call's guard resets the heap
 *   and fails the session);
 * - no stdio (the generated code only prints on its unreachable paths);
 * - no time();
 * - Noise* was generated for Hacl_Curve25519_64 (x86-64 Vale assembly); ARM
 *   and the host tests use the portable Hacl_Curve25519_51, which has the same
 *   signatures and the same results.
 */
#ifndef CTAG_KRML_H_
#define CTAG_KRML_H_

#include <stddef.h>

void *ctag_krml_malloc(size_t size);
void *ctag_krml_calloc(size_t n, size_t size);
void ctag_krml_free(void *p);
void ctag_krml_exit(int code) __attribute__((noreturn));

#define KRML_HOST_MALLOC        ctag_krml_malloc
#define KRML_HOST_CALLOC        ctag_krml_calloc
#define KRML_HOST_FREE          ctag_krml_free
#define KRML_HOST_EXIT          ctag_krml_exit
#define KRML_HOST_PRINTF(...)   ((void)0)
#define KRML_HOST_EPRINTF(...)  ((void)0)
#define KRML_HOST_TIME          ctag_krml_no_time

#define Hacl_Curve25519_64_secret_to_public Hacl_Curve25519_51_secret_to_public
#define Hacl_Curve25519_64_ecdh             Hacl_Curve25519_51_ecdh

#endif /* CTAG_KRML_H_ */
