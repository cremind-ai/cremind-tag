/*
 * Enrollment blob (docs/protocol.md 9): 48 bytes in UICR.CUSTOMER[0..11].
 * Mirrors companion protocol/enrollment.py unpack_blob().
 */
#ifndef CTAG_ENROLL_H_
#define CTAG_ENROLL_H_

#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CTAG_ENROLLMENT_VERSION 1u

/*
 * Parse and verify a blob read from memory (e.g. (const uint8_t *)
 * &NRF_UICR->CUSTOMER[0]): length, magic, CRC-32 over bytes [0, 44), version.
 * Returns CTAG_STATUS_OK, or CTAG_STATUS_SECURITY_CONFIG with *out zeroed.
 */
uint8_t ctag_enroll_parse(const uint8_t *blob, size_t len, struct ctag_enrollment *out);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_ENROLL_H_ */
