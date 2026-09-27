/*
 * The gateway loop: the only context that touches the core. It runs on the
 * main thread once start-up is done (cooperative, CONFIG_MAIN_THREAD_PRIORITY,
 * so no second stack is spent). Bluetooth callbacks (mesh model handlers,
 * send callbacks, provisioning and configuration-client callbacks, the scan
 * listener) copy what they got into a message queue and return at once; the
 * UART interrupt fills a ring. The loop drains both, feeds the core, and
 * sleeps until the core's next deadline or the next wake-up. Nothing here
 * blocks the Bluetooth stack.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

#include "gw_app.h"

K_MSGQ_DEFINE(gw_evq, sizeof(struct gw_evt), CONFIG_CTAG_GW_EVQ_DEPTH, 4);
static K_SEM_DEFINE(gw_wake_sem, 0, 1);
static atomic_t gw_evq_dropped;
static struct gw_core *gw;

void gw_post(const struct gw_evt *e)
{
	if (k_msgq_put(&gw_evq, e, K_NO_WAIT) != 0) {
		atomic_inc(&gw_evq_dropped); /* the protocol's retries recover */
	}
	k_sem_give(&gw_wake_sem);
}

void gw_wake(void)
{
	k_sem_give(&gw_wake_sem);
}

uint32_t gw_thread_dropped(void)
{
	return (uint32_t)atomic_get(&gw_evq_dropped);
}

static void dispatch(const struct gw_evt *e, int64_t now)
{
	switch (e->type) {
	case GW_EVT_MESH_RX:
		gw_core_mesh_rx(gw, e->addr, e->op, e->data, e->len, now);
		break;
	case GW_EVT_SEND_END:
		gw_core_send_end(gw, e->tag, e->err, now);
		break;
	case GW_EVT_CFG_STATUS:
		gw_core_cfg_status(gw, e->addr, e->op, e->data[0], e->data[1], now);
		break;
	case GW_EVT_BEACON:
		gw_core_beacon(gw, e->data, e->u16, e->rssi, now);
		break;
	case GW_EVT_PROV_OPEN:
		gw_core_prov_link_open(gw, now);
		break;
	case GW_EVT_PROV_ADDED:
		gw_core_prov_added(gw, e->data, e->addr, (uint8_t)e->u16, now);
		break;
	case GW_EVT_PROV_CLOSED:
		gw_core_prov_closed(gw, now);
		break;
	default:
		break;
	}
}

void gw_run(struct gw_core *g)
{
	uint8_t buf[128];
	struct gw_evt e;

	gw = g;
	gw_core_start(gw, k_uptime_get());
	for (;;) {
		int64_t now = k_uptime_get();
		int64_t due = gw_core_next_deadline(gw);
		k_timeout_t wait = due == GW_NEVER ? K_FOREVER : K_MSEC(due > now ? due - now : 0);
		size_t n;

		(void)k_sem_take(&gw_wake_sem, wait);
		now = k_uptime_get();
		while ((n = uart_io_read(buf, sizeof(buf))) > 0u) {
			gw_core_rx(gw, buf, n, now);
		}
		while (k_msgq_get(&gw_evq, &e, K_NO_WAIT) == 0) {
			dispatch(&e, now);
		}
		gw_core_poll(gw, k_uptime_get());
	}
}
