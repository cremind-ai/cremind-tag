/*
 * Enrollment blob (docs/protocol.md 9) in UICR.CUSTOMER[0..11] at 0x10001080
 * on nRF51 and nRF52 alike. The secret is never copied out of UICR: the
 * session code reads it in place (enroll_secret()).
 */
#include <errno.h>
#include <stddef.h>

#include <soc.h>

#include <ctag/ctag_enroll.h>

#include "app.h"

#define UICR_BLOB ((const uint8_t *)&NRF_UICR->CUSTOMER[0])
#define SECRET_OFFSET 12u /* magic, version, board, panel, flags, tag_id */

BUILD_ASSERT(offsetof(NRF_UICR_Type, CUSTOMER) == 0x080, "UICR.CUSTOMER at 0x10001080");

int enroll_load(struct ctag_enrollment *e)
{
	if (ctag_enroll_parse(UICR_BLOB, CTAG_ENROLLMENT_LEN, e) != CTAG_STATUS_OK) {
		return -EINVAL;
	}
	/* The blob and this firmware must describe the same board and panel. */
	return e->board == TAG_BOARD_ID && e->panel == TAG_PANEL_ID ? 0 : -EINVAL;
}

const uint8_t *enroll_secret(void)
{
	return UICR_BLOB + SECRET_OFFSET;
}
