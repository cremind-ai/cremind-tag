/* Internal interfaces of ctag_secure (not installed). */
#ifndef CTAG_SECURE_INT_H_
#define CTAG_SECURE_INT_H_

#include <setjmp.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Guard around every call into Noise*: ctag_krml_exit() (a failed
 * allocation, missing randomness or a KaRaMeL abort) longjmps back to the
 * setjmp of the guarded call after the heap was reset. Usage:
 *
 *     if (setjmp(ctag_krml_env) != 0) {
 *             return <cleanup>(ctag_krml_reason);   // -ENOMEM, -EIO, -EFAULT
 *     }
 *     ctag_krml_arm();
 *     ... Noise* calls ...
 *     ctag_krml_disarm();
 */
extern jmp_buf ctag_krml_env;
extern volatile int ctag_krml_reason;

void ctag_krml_arm(void);
void ctag_krml_disarm(void);
/* Changes whenever the heap is reset: Noise objects of an older epoch are gone. */
uint32_t ctag_krml_epoch(void);

/* Pointer to a buffer a vendored API declares non-const but only reads. */
static inline uint8_t *ctag_mut(const uint8_t *p)
{
	union {
		const uint8_t *c;
		uint8_t *m;
	} u;

	u.c = p;
	return u.m;
}

#endif /* CTAG_SECURE_INT_H_ */
