/* Fakes of the tag core's platform hooks and panel (fakes.c). */
#ifndef TAG_CORE_FAKES_H_
#define TAG_CORE_FAKES_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#define FAKE_PLANE_MAX 15000u
#define FAKE_STORE_IDS 3u

struct fake_panel {
	int init, begin, write, validate, commit, wait, sleep, abort;
	int misuse; /* writes out of order, a write/abort during a refresh, ... */
	bool active;
	bool refreshing;
	bool fail_begin;
	bool fail_commit;
	uint16_t len[2];
	uint8_t staged[2][FAKE_PLANE_MAX];
};

struct fake_entry {
	bool present;
	size_t len;
	uint8_t data[64];
};

struct fake_store {
	struct fake_entry e[FAKE_STORE_IDS];
	int writes[FAKE_STORE_IDS];
	bool fail_write;
};

extern struct fake_panel fake_panel;
extern struct fake_store fake_store;
extern uint32_t fake_now;
extern uint16_t fake_battery;
/* The CAPS value tag_hal_caps() serves (and the core binds into the handshake). */
extern uint8_t fake_caps[CTAG_TAG_CAPS_LEN];

void fakes_reset(void);
/* The bytes the next tag_hal_random() returns (a CHALLENGE nonce). */
void fake_set_nonce(const uint8_t *nonce, size_t len);
/* A CAPS value for a tag with this id and plane geometry (8 bytes per row). */
void fake_caps_for(uint32_t tag_id, uint8_t planes, uint16_t plane_len, uint8_t plane_flags);

#endif /* TAG_CORE_FAKES_H_ */
