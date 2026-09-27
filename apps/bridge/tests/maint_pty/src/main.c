/*
 * The bridge's maintenance-port core (src/core/maint.c, fontstore.c,
 * bflash.c) on native_sim: uart0 is a pseudo-terminal, the external flash a
 * simulated 8 MiB NOR. interop.py drives it with the companion's client.
 */
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include <ctag/ctag_crypto.h>

#include "bflash.h"
#include "fontstore.h"
#include "maint.h"

static const struct device *const uart = DEVICE_DT_GET(DT_CHOSEN(cremind_bridge_maint));
static struct bflash flash;
static struct fontstore fonts;
static struct maint mt;
static ctag_sha256_ctx sha_ctx;

static int sha_init(void *ctx)
{
	return ctag_crypto_sha256_init(ctx);
}

static int sha_update(void *ctx, const uint8_t *data, size_t len)
{
	return ctag_crypto_sha256_update(ctx, data, len);
}

static int sha_finish(void *ctx, uint8_t digest[32])
{
	return ctag_crypto_sha256_finish(ctx, digest);
}

static const struct ctag_sha256_ops sha = {sha_init, sha_update, sha_finish, &sha_ctx};

static void io_write(void *ctx, const uint8_t *data, size_t len)
{
	ARG_UNUSED(ctx);
	for (size_t i = 0; i < len; i++) {
		uart_poll_out(uart, data[i]);
	}
}

static void io_reboot(void *ctx)
{
	ARG_UNUSED(ctx);
	printk("maint_pty: REBOOT requested\n");
}

static size_t io_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	const struct ctag_cbor_counter c = CTAG_CBOR_COUNTER("sessions_ok", 0);

	ARG_UNUSED(ctx);
	if (max == 0) {
		return 0;
	}
	items[0] = c;
	return 1;
}

static const struct maint_io io = {.write = io_write, .reboot = io_reboot, .counters = io_counters};

int main(void)
{
	int err;

	(void)ctag_crypto_init();
	err = bflash_init(&flash, DEVICE_DT_GET(DT_CHOSEN(cremind_bridge_flash)),
			  CONFIG_CTAG_BRIDGE_WORKING_SPACE);
	fontstore_init(&fonts, &flash, err == 0);
	maint_init(&mt, &io, NULL, &fonts, &sha, 0xB0070001u, "0.1.0", "native_sim",
		   CTAG_BOARD_NRF52840_BRIDGE);
	printk("maint_pty: ready (flash %u B, slots of %u B)\n", flash.geom.flash_size,
	       flash.geom.slot_size);
	for (;;) {
		unsigned char c;
		bool any = false;

		while (uart_poll_in(uart, &c) == 0) {
			maint_rx(&mt, &c, 1u);
			any = true;
		}
		if (!any) {
			k_sleep(K_MSEC(1));
		}
	}
	return 0;
}
