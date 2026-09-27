/* ctag_enroll against enrollment.json. */
#include <string.h>

#include <ctag/ctag_enroll.h>

#include "check.h"
#include "suites.h"
#include "v_enrollment.h"

void test_enroll_valid(void)
{
	struct ctag_enrollment e;

	CHECK(ctag_enroll_parse(V_ENROLL_BLOB, V_ENROLL_BLOB_LEN, &e) == CTAG_STATUS_OK);
	CHECK(e.magic == V_ENROLL_MAGIC && e.version == V_ENROLL_VERSION);
	CHECK(e.board == V_ENROLL_BOARD && e.panel == V_ENROLL_PANEL && e.flags == V_ENROLL_FLAGS);
	CHECK(e.tag_id == V_ENROLL_TAG_ID && e.crc32 == V_ENROLL_CRC32);
	CHECK(memcmp(e.secret, V_ENROLL_SECRET, sizeof(e.secret)) == 0);
}

void test_enroll_invalid(void)
{
	static const uint8_t erased[CTAG_ENROLLMENT_LEN] = {
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
	};
	static const uint8_t zero[sizeof(struct ctag_enrollment)];
	struct ctag_enrollment e;
	size_t i;

	for (i = 0u; i < V_COUNT(v_enroll_invalid); i++) {
		const struct v_enroll_invalid *v = &v_enroll_invalid[i];

		memset(&e, 0x55, sizeof(e));
		CHECK_CASE(ctag_enroll_parse(v->blob, v->len, &e) == v->status, v->name);
		CHECK_CASE(memcmp(&e, zero, sizeof(e)) == 0, v->name); /* no secret left behind */
	}
	CHECK(ctag_enroll_parse(erased, sizeof(erased), &e) == CTAG_STATUS_SECURITY_CONFIG);
}
