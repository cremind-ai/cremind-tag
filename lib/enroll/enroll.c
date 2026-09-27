/* Enrollment blob (docs/protocol.md 9). */
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_enroll.h>

#define CRC_OFF (CTAG_ENROLLMENT_LEN - 4u)

uint8_t ctag_enroll_parse(const uint8_t *blob, size_t len, struct ctag_enrollment *out)
{
	/* The generated unpack leaves the CRC to the caller. */
	if (ctag_enrollment_unpack(out, blob, len) == 0 && out->magic == CTAG_ENROLLMENT_MAGIC &&
	    out->crc32 == ctag_crc32(0u, blob, CRC_OFF) &&
	    out->version == CTAG_ENROLLMENT_VERSION) {
		return CTAG_STATUS_OK;
	}
	memset(out, 0, sizeof(*out));
	return CTAG_STATUS_SECURITY_CONFIG;
}
