/*
 * Serial driver glue: an interrupt-driven UART (the nRF52 DK's UART / CH340,
 * a USB CDC ACM UART on the nRF52840, the native PTY UART in the interop
 * build) with a receive ring filled from the interrupt and a transmit ring
 * drained by it. Both sides wake the gateway thread; nothing here parses.
 */
#include <errno.h>

#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/ring_buffer.h>

#include "gw_app.h"

RING_BUF_DECLARE(rx_ring, CONFIG_CTAG_GW_UART_RX_RING);
RING_BUF_DECLARE(tx_ring, CONFIG_CTAG_GW_UART_TX_RING);

static const struct device *uart;
static uint32_t rx_overflow, rx_paused_count;
static uint32_t rx_bytes, tx_bytes;
static volatile bool rx_paused;

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	while (uart_irq_update(dev) > 0 && uart_irq_is_pending(dev) > 0) {
		if (uart_irq_rx_ready(dev) > 0) {
			uint8_t buf[64];
			uint32_t room = ring_buf_space_get(&rx_ring);
			int n;

			if (room == 0u) {
				/* Back-pressure until the thread drained the ring: USB NAKs
				 * (lossless); a plain UART's FIFO may overflow. */
				uart_irq_rx_disable(dev);
				rx_paused = true;
				rx_paused_count++;
				gw_wake();
				break; /* a pending transmit interrupt calls again */
			}
			n = uart_fifo_read(dev, buf, room < sizeof(buf) ? (int)room : (int)sizeof(buf));
			if (n > 0) {
				uint32_t put = ring_buf_put(&rx_ring, buf, (uint32_t)n);

				rx_bytes += (uint32_t)n;
				rx_overflow += (uint32_t)n - put;
				gw_wake();
			}
		}
		if (uart_irq_tx_ready(dev) > 0) {
			uint8_t *p;
			uint32_t n = ring_buf_get_claim(&tx_ring, &p, 64u);

			if (n == 0u) {
				(void)ring_buf_get_finish(&tx_ring, 0u);
				uart_irq_tx_disable(dev);
			} else {
				int sent = uart_fifo_fill(dev, p, (int)n);

				(void)ring_buf_get_finish(&tx_ring, sent > 0 ? (uint32_t)sent : 0u);
				tx_bytes += sent > 0 ? (uint32_t)sent : 0u;
				gw_wake(); /* room for the rest of the frame */
			}
		}
	}
}

int uart_io_init(const struct device *dev)
{
	uint8_t c;

	if (!device_is_ready(dev)) {
		return -ENODEV;
	}
	uart = dev;
	uart_irq_rx_disable(dev);
	uart_irq_tx_disable(dev);
	while (uart_fifo_read(dev, &c, 1) == 1) {
		/* drain */
	}
	(void)uart_irq_callback_user_data_set(dev, uart_isr, NULL);
	uart_irq_rx_enable(dev);
	return 0;
}

size_t uart_io_read(uint8_t *buf, size_t max)
{
	unsigned int key = irq_lock();
	uint32_t n = ring_buf_get(&rx_ring, buf, (uint32_t)max);

	irq_unlock(key);
	if (rx_paused && n > 0u) {
		rx_paused = false;
		uart_irq_rx_enable(uart);
	}
	return n;
}

size_t uart_io_write(const uint8_t *data, size_t len)
{
	unsigned int key;
	uint32_t n;

	if (uart == NULL) {
		return len; /* no port: discard */
	}
	key = irq_lock();
	n = ring_buf_put(&tx_ring, data, (uint32_t)len);
	irq_unlock(key);
	if (n > 0u) {
		uart_irq_tx_enable(uart);
	}
	return n;
}

void uart_io_flush(k_timeout_t timeout)
{
	k_timepoint_t end = sys_timepoint_calc(timeout);

	while (!ring_buf_is_empty(&tx_ring) && !sys_timepoint_expired(end)) {
		k_sleep(K_MSEC(5));
	}
}

size_t uart_io_counters(struct ctag_cbor_counter *items, size_t max)
{
	const struct ctag_cbor_counter all[] = {
		CTAG_CBOR_COUNTER("uart_rx_bytes", rx_bytes),
		CTAG_CBOR_COUNTER("uart_tx_bytes", tx_bytes),
		CTAG_CBOR_COUNTER("uart_rx_overflow", rx_overflow),
		CTAG_CBOR_COUNTER("uart_rx_paused", rx_paused_count),
	};
	size_t n = ARRAY_SIZE(all) < max ? ARRAY_SIZE(all) : max;

	for (size_t i = 0; i < n; i++) {
		items[i] = all[i];
	}
	return n;
}
