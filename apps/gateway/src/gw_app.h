/*
 * Gateway application glue (docs/gateway-firmware.md): the thread that runs
 * the core, the serial driver and the events Bluetooth callbacks hand over.
 * Shared by the firmware and the native_sim interop build (tests/interop).
 */
#ifndef GW_APP_H_
#define GW_APP_H_

#include <stddef.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/kernel.h>

#include "gw_core.h"

/* Events posted to the gateway thread from Bluetooth (or simulator) context. */
enum gw_evt_type {
	GW_EVT_MESH_RX = 1, /* op, addr = src, data[len] */
	GW_EVT_SEND_END,    /* tag, err */
	GW_EVT_CFG_STATUS,  /* addr, op = step, data[0] = status, data[1] = value */
	GW_EVT_BEACON,      /* data = uuid, u16 = oob, rssi */
	GW_EVT_PROV_OPEN,
	GW_EVT_PROV_ADDED, /* data = uuid, addr, u16 = elements */
	GW_EVT_PROV_CLOSED,
};

#define GW_EVT_DATA 48u /* >= the largest inbound vendor message (DELIVERY_RESULT, 41) */

struct gw_evt {
	uint8_t type;
	uint8_t op;
	uint8_t len;
	int8_t rssi;
	uint16_t addr;
	uint16_t u16;
	uint32_t tag;
	int32_t err;
	uint8_t data[GW_EVT_DATA];
};

/* ---- gw_thread.c ---- */

/* Queue an event for the gateway thread (any context; never blocks). */
void gw_post(const struct gw_evt *e);
/* Wake the gateway thread (serial bytes arrived or transmit room freed). */
void gw_wake(void);
/* Run the core on the calling thread forever; gw_core_start() comes first. */
FUNC_NORETURN void gw_run(struct gw_core *g);
/* Events the queue refused (full). */
uint32_t gw_thread_dropped(void);

/* ---- uart_io.c: interrupt-driven UART / CDC ACM with two rings ---- */

int uart_io_init(const struct device *dev);
size_t uart_io_read(uint8_t *buf, size_t max);
size_t uart_io_write(const uint8_t *data, size_t len);
/* Wait until the transmit ring is empty (or the timeout elapsed). */
void uart_io_flush(k_timeout_t timeout);
size_t uart_io_counters(struct ctag_cbor_counter *items, size_t max);

#endif /* GW_APP_H_ */
