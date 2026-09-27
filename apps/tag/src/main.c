/*
 * Cremind Tag firmware (docs/tag-firmware.md).
 *
 * main() only checks the enrollment blob and starts Bluetooth asynchronously;
 * everything else runs on the system work queue: bt_ready() (crypto, NVS,
 * panel, core, advertising), the GATT callbacks (BT_RECV_WORKQ_SYS), the core
 * work item, the session timeout and the panel's BUSY poll. A single
 * cooperative context means the core needs no locking and main's stack stays
 * small.
 */
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/poweroff.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_enroll.h>

#include "app.h"
#include "panel.h"

LOG_MODULE_REGISTER(tag, LOG_LEVEL_INF);

struct tag_core app_core;

static struct tag_core_cfg cfg;

static void core_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(core_work, core_fn);

#define LINGER_MS 2000 /* ERROR / last RESULT in flight before a forced disconnect */

/*
 * The one work item of the core: the panel's BUSY poll (while a refresh
 * runs), record processing, then the GATT transmit pump. Kicked by writes,
 * ATT completions and the panel; re-armed by -ENOMEM and the BUSY poll.
 */
static void core_fn(struct k_work *work)
{
	(void)work;
	panel_poll();
	tag_core_poll(&app_core);
	gatt_pump();
	if (tag_core_closing(&app_core)) {
		/* Bound the wait for the last indication's confirmation. */
		adv_touch(LINGER_MS);
	}
}

void app_kick(void)
{
	k_work_reschedule(&core_work, K_NO_WAIT);
}

void app_kick_after(uint32_t ms)
{
	/* Keeps an earlier deadline: never postpones pending work. */
	k_work_schedule(&core_work, K_MSEC(ms));
}

void app_kick_later(void)
{
	app_kick_after(10);
}

void app_touch(void)
{
	adv_touch(CTAG_TAG_SESSION_TIMEOUT_MS);
}

void app_session_timer(void)
{
	if (tag_core_refreshing(&app_core)) {
		app_touch(); /* the tag stays connected through the refresh */
		return;
	}
	if (tag_core_closing(&app_core)) {
		gatt_disconnect();
		return;
	}
	tag_core_timeout(&app_core);
	app_kick();
}

void app_link_up(void)
{
	adv_link_up(); /* also arms the session timeout */
	tag_core_link_up(&app_core);
}

void app_link_down(void)
{
	tag_core_link_down(&app_core);
#if TAG_SLEEP_OK
	if (app_core.sleep) {
		static const struct gpio_dt_spec wake = GPIO_DT_SPEC_GET(TAG_BOARD_NODE, wake_gpios);

		/* CMD{SLEEP}: System OFF until the verified wake input fires. */
		(void)gpio_pin_configure_dt(&wake, GPIO_INPUT);
		(void)gpio_pin_interrupt_configure_dt(&wake, GPIO_INT_LEVEL_ACTIVE);
		sys_poweroff();
	}
#endif
	adv_link_down();
}

void app_refresh_done(uint8_t status)
{
	tag_core_refresh_done(&app_core, status);
	if (gatt_connected()) {
		app_touch();
	}
	app_kick();
}

/* ---- platform hooks of the core ---- */

uint32_t tag_hal_uptime_ms(void)
{
	return k_uptime_get_32();
}

int tag_hal_random(uint8_t *buf, size_t len)
{
	return ctag_crypto_random(buf, len);
}

/* ---- boot ---- */

static void wipe(void *p, size_t len)
{
	volatile uint8_t *v = p;

	while (len-- > 0u) {
		*v++ = 0u;
	}
}

static void bt_ready(int err)
{
	if (err != 0 || ctag_crypto_init() != 0) {
		LOG_ERR("bt %d or crypto init failed: not advertising", err);
		return; /* no radio or no crypto: never advertise */
	}
	(void)store_init(); /* on failure every save reports STORAGE_ERROR */
	(void)battery_init();
	if (panel_init(TAG_PANEL_ID) == 0) {
		cfg.flags |= TAG_CFG_PANEL_OK;
	}
	if (TAG_SLEEP_OK) {
		cfg.flags |= TAG_CFG_SLEEP_OK;
	}
	tag_core_init(&app_core, &cfg);
	LOG_INF("tag %08x board %u panel %u (usable %u) epoch %u rec %u", cfg.tag_id, TAG_BOARD_ID,
		TAG_PANEL_ID, cfg.flags & TAG_CFG_PANEL_OK, app_core.stored_epoch,
		app_core.rec_state);
	adv_start();
}

#if DT_NODE_EXISTS(DT_ALIAS(led0))
static void security_config_blink(void)
{
	static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

	if (!gpio_is_ready_dt(&led) || gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) != 0) {
		return;
	}
	/* Ten slow blinks, then dark: the tag stays silent and never advertises. */
	for (int i = 0; i < 20; i++) {
		(void)gpio_pin_toggle_dt(&led);
		k_msleep(250);
	}
	(void)gpio_pin_set_dt(&led, 0);
}
#else
static void security_config_blink(void)
{
}
#endif

int main(void)
{
	struct ctag_enrollment e;
	int err = enroll_load(&e);

	cfg.tag_id = e.tag_id;
	wipe(&e, sizeof(e)); /* the secret stays in UICR only */
	/* 9: no valid enrollment (or one for another board/panel) -> SECURITY_CONFIG. */
	if (err != 0) {
		LOG_ERR("SECURITY_CONFIG: enrollment blob missing, corrupt or for another board");
		security_config_blink();
		return 0;
	}
	cfg.secret = enroll_secret();
	cfg.planes = TAG_PLANES;
	cfg.plane_flags = TAG_PLANE_FLAGS;
	cfg.plane_len = (uint16_t)(((TAG_WIDTH + 7) / 8) * TAG_HEIGHT);
	(void)bt_enable(bt_ready);
	return 0;
}
