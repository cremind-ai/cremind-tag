/*
 * The gateway loop: the only context that touches the core. It runs on the
 * main thread once start-up is done (cooperative, CONFIG_MAIN_THREAD_PRIORITY,
 * so no second stack is spent). Bluetooth callbacks (mesh model handlers,
 * send callbacks, provisioning and configuration-client callbacks, the scan
 * listeners, and with the own radio the connection, GATT and value
 * callbacks of central.c) copy what they got into a message queue and return
 * at once; the UART interrupt fills a ring. The loop drains both, feeds the
 * core, and sleeps until the core's next deadline or the next wake-up.
 * Nothing here blocks the Bluetooth stack.
 */
#include <string.h>

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

bool gw_post_advert(const struct gw_evt *e)
{
	/* A tag advertises again 250 ms later (5.1): never at the cost of a
	 * mesh, provisioning or link event. */
	if (k_msgq_num_used_get(&gw_evq) >= CONFIG_CTAG_GW_EVQ_DEPTH / 2) {
		return false;
	}
	gw_post(e);
	return true;
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
#ifdef CONFIG_CTAG_GW_SECURE
	case GW_EVT_PROV_SECURITY:
		gw_core_prov_security(gw, now);
		break;
	case GW_EVT_PROV_AUTH:
		gw_core_prov_auth(gw, now);
		break;
#endif
#ifdef CONFIG_CTAG_GW_RADIO
	case GW_EVT_TAG_ADV: {
		struct sched_peer peer = {.type = e->data[0]};

		memcpy(peer.a, &e->data[1], sizeof(peer.a));
		gw_core_tag_adv(gw, &peer, e->tag, e->op, (uint8_t)e->u16, e->rssi, now);
		break;
	}
	case GW_EVT_CONN:
	case GW_EVT_DISCONN:
	case GW_EVT_GATT:
		gw_central_event(gw, e, now);
		break;
	case GW_EVT_LINK_VALUE:
		gw_core_link_rx(gw, (uint8_t)e->addr, e->op, e->err == 0 ? e->data : NULL, e->len,
				now);
		break;
	case GW_EVT_LINK_WRITTEN:
		gw_core_link_written(gw, (uint8_t)e->addr, e->op, e->err, now);
		break;
#endif
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
