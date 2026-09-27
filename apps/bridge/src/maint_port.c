/*
 * Maintenance port transport (docs/protocol.md 1.6): USB CDC ACM on the
 * nRF52840, the UART on the nRF52832 (chosen `cremind,bridge-maint`).
 * Interrupt-driven: the ISR moves bytes between the UART FIFO and two rings;
 * the maintenance thread feeds maint_rx() and so processes one frame at a
 * time. The receive ring holds the frames HELLO's caps.credits lets the host
 * send ahead.
 *
 * The maintenance thread is the main thread: once main() has started the
 * bridge work queue it runs maint_port_run(), so one stack
 * (CONFIG_MAIN_STACK_SIZE) serves the boot and then the port.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/ring_buffer.h>

#include <ctag/ctag_crypto.h>

#include "bridge.h"
#include "maint.h"

LOG_MODULE_REGISTER(bridge_maint, LOG_LEVEL_INF);

#define RX_RING (MAINT_CREDITS * CTAG_COBS_MAX_ENCODED(MAINT_MAX_FRAME) + 256)
#define TX_RING CONFIG_CTAG_BRIDGE_MAINT_TX_RING
#define TX_WAIT_MS 100
#define TX_GIVE_UP_MS 1000

static const struct device *const uart = DEVICE_DT_GET(DT_CHOSEN(cremind_bridge_maint));

RING_BUF_DECLARE(rx_ring, RX_RING);
RING_BUF_DECLARE(tx_ring, TX_RING);
static K_SEM_DEFINE(rx_sem, 0, 1);
static K_SEM_DEFINE(tx_sem, 0, 1);
static K_SEM_DEFINE(started, 0, 1);
static struct maint maint;
static uint32_t rx_overflow;
static uint32_t tx_timeouts;
static bool reboot_pending;

static void isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t buf[64];
			int n = uart_fifo_read(dev, buf, sizeof(buf));

			if (n > 0) {
				if (ring_buf_put(&rx_ring, buf, (uint32_t)n) < (uint32_t)n) {
					rx_overflow++; /* the frame fails its CRC; the host resyncs */
				}
				k_sem_give(&rx_sem);
			}
		}
		if (uart_irq_tx_ready(dev)) {
			uint8_t *p;
			uint32_t n = ring_buf_get_claim(&tx_ring, &p, 64);

			if (n == 0u) {
				uart_irq_tx_disable(dev);
				k_sem_give(&tx_sem);
			} else {
				int w = uart_fifo_fill(dev, p, (int)n);

				(void)ring_buf_get_finish(&tx_ring, w > 0 ? (uint32_t)w : 0u);
				k_sem_give(&tx_sem);
			}
		}
	}
}

static void port_write(void *ctx, const uint8_t *data, size_t len)
{
	int64_t deadline = k_uptime_get() + TX_GIVE_UP_MS;

	ARG_UNUSED(ctx);
	while (len > 0u) {
		uint32_t n = ring_buf_put(&tx_ring, data, (uint32_t)len);

		data += n;
		len -= n;
		uart_irq_tx_enable(uart);
		if (len == 0u) {
			break;
		}
		if (k_uptime_get() > deadline) {
			tx_timeouts++; /* nobody reads the port: drop the rest */
			return;
		}
		(void)k_sem_take(&tx_sem, K_MSEC(TX_WAIT_MS));
	}
}

static void port_reboot(void *ctx)
{
	ARG_UNUSED(ctx);
	reboot_pending = true;
}

static size_t port_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	size_t n = 0u;

	ARG_UNUSED(ctx);
	BRIDGE_COUNTER("rx_overflow", rx_overflow);
	BRIDGE_COUNTER("tx_timeouts", tx_timeouts);
	return n + bridge_counters(&items[n], max - n);
}

static const struct maint_io io = {
	.write = port_write,
	.reboot = port_reboot,
	.counters = port_counters,
};

/* SHA-256 of the maintenance thread (FONT_COMMIT). */
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

int maint_port_start(void)
{
	if (!device_is_ready(uart)) {
		return -ENODEV;
	}
	maint_init(&maint, &io, NULL, &br.fonts, &sha, br.boot_id, BRIDGE_FW, CTAG_BRIDGE_BUILD,
		   CONFIG_CTAG_BRIDGE_BOARD_ID);
	uart_irq_rx_disable(uart);
	uart_irq_tx_disable(uart);
	(void)uart_irq_callback_user_data_set(uart, isr, NULL);
	uart_irq_rx_enable(uart);
	k_sem_give(&started);
	return 0;
}

void maint_port_run(void)
{
	uint8_t buf[64];

	k_thread_priority_set(k_current_get(), CONFIG_CTAG_BRIDGE_MAINT_PRIORITY);
	k_thread_name_set(k_current_get(), "maint");
	(void)k_sem_take(&started, K_FOREVER);
	for (;;) {
		uint32_t n;

		(void)k_sem_take(&rx_sem, K_FOREVER);
		while ((n = ring_buf_get(&rx_ring, buf, sizeof(buf))) > 0u) {
			maint_rx(&maint, buf, n);
			if (reboot_pending) {
				/* 10: REBOOT answers first; let the answer leave the port. */
				for (int i = 0; i < 20 && !ring_buf_is_empty(&tx_ring); i++) {
					k_sleep(K_MSEC(10));
				}
				k_sleep(K_MSEC(50));
				sys_reboot(SYS_REBOOT_COLD);
			}
		}
	}
}
