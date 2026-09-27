/*
 * Fakes for the tag core's platform hooks (tag_core.h) and panel interface
 * (panel.h): an in-memory NVS with the nvs_read()/nvs_write() semantics, a
 * controllable clock, battery and nonce source, and a panel that records
 * every call and the staged plane bytes.
 */
#include <errno.h>
#include <string.h>

#include "fakes.h"
#include "panel.h"
#include "tag_core.h"

struct fake_panel fake_panel;
struct fake_store fake_store;
uint32_t fake_now;
uint16_t fake_battery;
uint8_t fake_caps[CTAG_TAG_CAPS_LEN];

static uint8_t nonce[16];
static bool nonce_set;
static uint8_t nonce_counter;

void fakes_reset(void)
{
	memset(&fake_panel, 0, sizeof(fake_panel));
	memset(&fake_store, 0, sizeof(fake_store));
	fake_now = 1000u;
	fake_battery = 2950u;
	nonce_set = false;
}

void fake_set_nonce(const uint8_t *n, size_t len)
{
	memcpy(nonce, n, len < sizeof(nonce) ? len : sizeof(nonce));
	nonce_set = true;
}

void fake_caps_for(uint32_t tag_id, uint8_t planes, uint16_t plane_len, uint8_t plane_flags)
{
	const struct ctag_tag_caps caps = {
		.proto = CTAG_PROTO_VERSION,
		.tag_id = tag_id,
		.board = CTAG_BOARD_NRF52DK_TAG,
		.panel = CTAG_PANEL_NONE,
		.width = 64u,
		.height = (uint16_t)(plane_len / 8u),
		.planes = planes,
		.plane_flags = plane_flags,
		.fw_minor = 1u,
		.max_record = CTAG_TAG_RECORD_PAYLOAD_MAX,
		.credits = TAG_CREDITS,
	};

	(void)ctag_tag_caps_pack(&caps, fake_caps, sizeof(fake_caps));
}

/* ---- platform hooks ---- */

void tag_hal_caps(uint8_t caps[CTAG_TAG_CAPS_LEN])
{
	memcpy(caps, fake_caps, CTAG_TAG_CAPS_LEN);
}

uint16_t tag_hal_battery_mv(void)
{
	return fake_battery;
}

uint32_t tag_hal_uptime_ms(void)
{
	return fake_now;
}

int tag_hal_random(uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		buf[i] = nonce_set && i < sizeof(nonce) ? nonce[i] : (uint8_t)(nonce_counter + i);
	}
	nonce_set = false;
	nonce_counter += 0x10u;
	return 0;
}

int tag_hal_store_read(uint16_t id, void *buf, size_t len)
{
	struct fake_entry *e;

	if (id >= FAKE_STORE_IDS) {
		return -ENOENT;
	}
	e = &fake_store.e[id];
	if (!e->present) {
		return -ENOENT;
	}
	memcpy(buf, e->data, len < e->len ? len : e->len);
	return (int)e->len; /* nvs_read(): the stored length, which may exceed len */
}

int tag_hal_store_write(uint16_t id, const void *buf, size_t len)
{
	struct fake_entry *e;

	if (id >= FAKE_STORE_IDS || len > sizeof(e->data)) {
		return -EINVAL;
	}
	if (fake_store.fail_write) {
		return -EIO;
	}
	e = &fake_store.e[id];
	memcpy(e->data, buf, len);
	e->len = len;
	e->present = true;
	fake_store.writes[id]++;
	return (int)len;
}

/* ---- panel ---- */

int panel_init(uint8_t panel_id)
{
	(void)panel_id;
	fake_panel.init++;
	return 0;
}

int panel_begin_frame(void)
{
	fake_panel.begin++;
	fake_panel.len[0] = 0u;
	fake_panel.len[1] = 0u;
	fake_panel.active = true;
	return fake_panel.fail_begin ? -EIO : 0;
}

int panel_write_plane_chunk(uint8_t plane, uint16_t offset, const uint8_t *data, size_t len)
{
	fake_panel.write++;
	if (!fake_panel.active || plane > 1u || offset != fake_panel.len[plane] ||
	    offset + len > FAKE_PLANE_MAX || fake_panel.refreshing) {
		fake_panel.misuse++;
		return -EINVAL;
	}
	memcpy(&fake_panel.staged[plane][offset], data, len);
	fake_panel.len[plane] = (uint16_t)(offset + len);
	return 0;
}

int panel_validate_frame(void)
{
	fake_panel.validate++;
	return 0;
}

int panel_commit_refresh(void)
{
	fake_panel.commit++;
	if (!fake_panel.active) {
		fake_panel.misuse++;
	}
	fake_panel.refreshing = true;
	return fake_panel.fail_commit ? -EIO : 0;
}

int panel_wait_refresh_complete(void)
{
	fake_panel.wait++;
	return 0;
}

void panel_poll(void)
{
}

void panel_sleep(void)
{
	fake_panel.sleep++;
	fake_panel.active = false;
	fake_panel.refreshing = false;
}

void panel_abort_frame(void)
{
	fake_panel.abort++;
	if (fake_panel.refreshing) {
		fake_panel.misuse++; /* a refresh must never be aborted */
	}
	fake_panel.active = false;
}
