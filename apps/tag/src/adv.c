/*
 * Wake cycle and advertising (docs/protocol.md 5.1): every
 * TAG_WAKE_PERIOD_MS +/- TAG_WAKE_JITTER_MS (uniform) the tag advertises
 * connectably for TAG_ADV_WINDOW_MS at TAG_ADV_INTERVAL_MS, legacy 1M from
 * its static random identity address, with Flags and the manufacturer data
 * below; no scan response. Between windows the system idles in System ON
 * (tickless kernel, RTC running). A window is skipped while a bridge is
 * connected or a refresh runs, and once after three consecutive handshake
 * AUTH failures (5.4 pacing). While connected, the same timer is the
 * session timeout (app_session_timer()).
 */
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>

#include <ctag/proto_ids.h>

#include "app.h"

#define ADV_INTERVAL BT_GAP_MS_TO_ADV_INTERVAL(CTAG_TAG_ADV_INTERVAL_MS)

/* company u16 | ver u8 | tag_id u32 | flags u8 | disp_rev u16 */
static uint8_t mfg[10] = {(uint8_t)CTAG_MESH_COMPANY_ID, (uint8_t)(CTAG_MESH_COMPANY_ID >> 8), 1};

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
	BT_DATA(BT_DATA_MANUFACTURER_DATA, mfg, sizeof(mfg)),
};

static bool advertising;
static uint32_t wake_at;

static void adv_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(adv_work, adv_fn);

static void next_wake(void)
{
	int32_t jitter = (int32_t)(sys_rand32_get() % (2u * CTAG_TAG_WAKE_JITTER_MS + 1u)) -
			 CTAG_TAG_WAKE_JITTER_MS;
	int32_t delay = (int32_t)(wake_at + CTAG_TAG_WAKE_PERIOD_MS - k_uptime_get_32()) + jitter;

	k_work_reschedule(&adv_work, K_MSEC(delay > 0 ? delay : 0));
}

static void adv_fn(struct k_work *work)
{
	(void)work;
	if (gatt_connected()) {
		app_session_timer();
		return;
	}
	if (advertising) { /* end of the window */
		(void)bt_le_adv_stop();
		advertising = false;
		next_wake();
		return;
	}
	wake_at = k_uptime_get_32();
	if (!gatt_connected() && !tag_core_refreshing(&app_core) &&
	    !tag_core_take_skip(&app_core)) {
		mfg[7] = tag_core_adv_flags(&app_core, tag_hal_battery_mv());
		ctag_put_le16(&mfg[8], tag_core_disp_rev(&app_core));
		if (bt_le_adv_start(BT_LE_ADV_PARAM(BT_LE_ADV_OPT_CONN, ADV_INTERVAL, ADV_INTERVAL,
						    NULL),
				    ad, ARRAY_SIZE(ad), NULL, 0) == 0) {
			advertising = true;
			k_work_schedule(&adv_work, K_MSEC(CTAG_TAG_ADV_WINDOW_MS));
			return;
		}
	}
	next_wake();
}

void adv_start(void)
{
	ctag_put_le32(&mfg[3], app_core.cfg.tag_id);
	k_work_schedule(&adv_work, K_NO_WAIT); /* first window right after boot */
}

void adv_link_up(void)
{
	/* A connectable legacy advertiser stops when the connection is made. */
	advertising = false;
	adv_touch(CTAG_TAG_SESSION_TIMEOUT_MS);
}

void adv_touch(uint32_t ms)
{
	k_work_reschedule(&adv_work, K_MSEC(ms));
}

void adv_link_down(void)
{
	next_wake();
}
