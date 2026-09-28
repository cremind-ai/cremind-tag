/*
 * The ownership record (docs/connect-setup.md 4.1): one atomic write, CRC-32
 * protected, and a separately stored generation floor so that a corrupt or
 * missing record can never rewind the generation. Layout in ctag_secure.h.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crc32.h>
#include <ctag/ctag_secure.h>

#include "secure_int.h"

#define OFF_GEN        4u
#define OFF_AUTHORITY  8u
#define OFF_OWNER      40u
#define OFF_CONTROLLER 56u
#define OFF_OP_KEY     88u
#define OFF_OVERRIDE   120u
#define OFF_PENDING    130u
#define OFF_PENDING_C  140u
#define OFF_CRC        172u

#define F_LOCKED     0x01u
#define F_OP_KEY     0x02u
#define F_OVERRIDE   0x04u
#define F_PENDING    0x08u
#define F_PENDING_C  0x10u
#define F_ALL        0x1Fu

void ctag_owner_record_encode(const struct ctag_owner_record *r, uint8_t out[CTAG_OWNER_RECORD_LEN])
{
	uint8_t flags = (uint8_t)((r->locked ? F_LOCKED : 0u) | (r->has_op_key ? F_OP_KEY : 0u) |
				  (r->has_override ? F_OVERRIDE : 0u) |
				  (r->has_pending_override ? F_PENDING : 0u) |
				  (r->has_pending_controller ? F_PENDING_C : 0u));

	memset(out, 0, CTAG_OWNER_RECORD_LEN);
	out[0] = CTAG_OWNER_RECORD_VERSION;
	out[1] = r->state;
	out[2] = flags;
	ctag_put_le32(&out[OFF_GEN], r->gen);
	if (r->state == CTAG_OWNER_OWNED) {
		memcpy(&out[OFF_AUTHORITY], r->authority_pub, sizeof(r->authority_pub));
		memcpy(&out[OFF_OWNER], r->owner, sizeof(r->owner));
		memcpy(&out[OFF_CONTROLLER], r->controller, sizeof(r->controller));
	}
	if (r->has_op_key) {
		memcpy(&out[OFF_OP_KEY], r->op_key, sizeof(r->op_key));
	}
	if (r->has_override) {
		memcpy(&out[OFF_OVERRIDE], r->override_secret, sizeof(r->override_secret));
	}
	if (r->has_pending_override) {
		memcpy(&out[OFF_PENDING], r->pending_override, sizeof(r->pending_override));
	}
	if (r->has_pending_controller) {
		memcpy(&out[OFF_PENDING_C], r->pending_controller, sizeof(r->pending_controller));
	}
	ctag_put_le32(&out[OFF_CRC], ctag_crc32(0u, out, OFF_CRC));
}

int ctag_owner_record_decode(struct ctag_owner_record *r, const uint8_t *in, size_t len)
{
	uint8_t flags;

	memset(r, 0, sizeof(*r));
	if (in == NULL || len != CTAG_OWNER_RECORD_LEN || in[0] != CTAG_OWNER_RECORD_VERSION ||
	    ctag_get_le32(&in[OFF_CRC]) != ctag_crc32(0u, in, OFF_CRC)) {
		return -EBADMSG;
	}
	flags = in[2];
	if (in[1] > CTAG_OWNER_RELEASED || (flags & (uint8_t)~F_ALL) != 0u || in[3] != 0u) {
		return -EBADMSG;
	}
	r->state = in[1];
	r->gen = ctag_get_le32(&in[OFF_GEN]);
	r->locked = (flags & F_LOCKED) != 0u;
	r->has_op_key = (flags & F_OP_KEY) != 0u;
	r->has_override = (flags & F_OVERRIDE) != 0u;
	r->has_pending_override = (flags & F_PENDING) != 0u;
	r->has_pending_controller = (flags & F_PENDING_C) != 0u;
	memcpy(r->authority_pub, &in[OFF_AUTHORITY], sizeof(r->authority_pub));
	memcpy(r->owner, &in[OFF_OWNER], sizeof(r->owner));
	memcpy(r->controller, &in[OFF_CONTROLLER], sizeof(r->controller));
	memcpy(r->op_key, &in[OFF_OP_KEY], sizeof(r->op_key));
	memcpy(r->override_secret, &in[OFF_OVERRIDE], sizeof(r->override_secret));
	memcpy(r->pending_override, &in[OFF_PENDING], sizeof(r->pending_override));
	memcpy(r->pending_controller, &in[OFF_PENDING_C], sizeof(r->pending_controller));
	return 0;
}

bool ctag_owner_record_load(struct ctag_owner_record *r, const uint8_t *in, size_t len,
			    uint32_t gen_floor)
{
	bool good = len > 0u && ctag_owner_record_decode(r, in, len) == 0;

	if (!good) {
		/* Unreadable: UNOWNED, and the floor keeps the generation. */
		memset(r, 0, sizeof(*r));
		r->state = CTAG_OWNER_UNOWNED;
		r->gen = gen_floor;
		return false;
	}
	if (r->gen < gen_floor) {
		r->gen = gen_floor;
	}
	return true;
}
