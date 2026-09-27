/* IDENTIFY / Health attention: blink the board LED (DT alias led0) for a while. */
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include "bridge.h"

#define BLINK_MS 250

#if DT_NODE_EXISTS(DT_ALIAS(led0))
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static uint32_t toggles_left;
static bool configured;

static void blink_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(blink, blink_fn);

static void blink_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (toggles_left == 0u) {
		(void)gpio_pin_set_dt(&led, 0);
		return;
	}
	toggles_left--;
	(void)gpio_pin_toggle_dt(&led);
	(void)k_work_reschedule_for_queue(&bwq, &blink, K_MSEC(BLINK_MS));
}

void identify_start(uint8_t seconds)
{
	if (!configured) {
		configured = gpio_is_ready_dt(&led) &&
			     gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) == 0;
		if (!configured) {
			return;
		}
	}
	/* Even: the LED ends where it started. */
	toggles_left = (uint32_t)seconds * (1000u / BLINK_MS) & ~1u;
	(void)k_work_reschedule_for_queue(&bwq, &blink, K_NO_WAIT);
}
#else
void identify_start(uint8_t seconds)
{
	ARG_UNUSED(seconds); /* no LED on this board */
}
#endif
