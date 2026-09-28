/*
 * The physical factory reset (docs/connect-setup.md 4.3, protocol v2): the
 * board button (devicetree alias sw0) held through power-up for 10 s. While
 * it is held the LED (alias led0) blinks fast; after 10 s it stays on and the
 * reset runs (main.c: ownership, mesh and assignments cleared, identity key
 * and generation kept, then a reboot). Releasing the button earlier boots
 * normally. Never reachable over the radio or from software.
 *
 * nRF52840 DK: Button 1 and LED1. nRF52840 Dongle: SW1 (the side button,
 * not RESET) and the green LED LD1.
 */
#include <stdbool.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include "gw_app.h"

#define HOLD_MS  10000
#define BLINK_MS 50

#if DT_NODE_HAS_STATUS_OKAY(DT_ALIAS(sw0))

static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
#if DT_NODE_HAS_STATUS_OKAY(DT_ALIAS(led0))
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#define HAS_LED 1
#endif

static void led_set(int on)
{
#ifdef HAS_LED
	if (gpio_is_ready_dt(&led)) {
		(void)gpio_pin_set_dt(&led, on);
	}
#else
	(void)on;
#endif
}

bool gw_factory_reset_held(void)
{
	int on = 0;

	if (!gpio_is_ready_dt(&button) || gpio_pin_configure_dt(&button, GPIO_INPUT) != 0 ||
	    gpio_pin_get_dt(&button) <= 0) {
		return false;
	}
#ifdef HAS_LED
	if (gpio_is_ready_dt(&led)) {
		(void)gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	}
#endif
	for (int ms = 0; ms < HOLD_MS; ms += BLINK_MS) {
		if (gpio_pin_get_dt(&button) <= 0) {
			led_set(0); /* released early: a normal boot */
			return false;
		}
		on = !on;
		led_set(on);
		k_msleep(BLINK_MS);
	}
	led_set(1); /* solid: the reset runs */
	return true;
}

#else

bool gw_factory_reset_held(void)
{
	return false; /* no button on this board */
}

#endif
