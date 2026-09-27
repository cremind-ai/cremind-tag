/*
 * CRC-32/IEEE (docs/protocol.md 1.1): polynomial 0x04C11DB7 reflected, init and
 * xorout 0xFFFFFFFF. Used by serial frames, the enrollment blob, font packs and
 * the tag's persisted display record.
 */
#ifndef CTAG_CRC32_H_
#define CTAG_CRC32_H_

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * CRC-32 of data. Start with crc = 0; pass a previous result to continue over
 * more data (zlib crc32() semantics, so ctag_crc32(0, "123456789", 9) is
 * 0xCBF43926).
 */
uint32_t ctag_crc32(uint32_t crc, const void *data, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_CRC32_H_ */
