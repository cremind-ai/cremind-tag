/*
 * Cremind Tag bridge application (docs/bridge-firmware.md): the portable core
 * (src/core) wired to Bluetooth Mesh, the BLE central, the external flash
 * and the maintenance port.
 *
 * Threads: Bluetooth callbacks and mesh model handlers only copy events into
 * queues; everything else runs on the bridge work queue (`bwq`), so the core
 * needs no locks. The maintenance port runs on the main thread; it shares
 * only the font store (which has a mutex) with the work queue, and reads the
 * counters for INFO.
 */
#ifndef BRIDGE_APP_H_
#define BRIDGE_APP_H_

#include <stdbool.h>
#include <stdint.h>

#include <app_version.h>
#include <zephyr/bluetooth/addr.h>
#include <zephyr/kernel.h>

#include <ctag/ctag_cbor.h>

#include "bflash.h"
#include "delivery.h"
#include "fontstore.h"
#include "sched.h"
#include "tagsess.h"

/* The firmware version: apps/bridge/VERSION (tools/version.py keeps it equal
 * to the repository's VERSION), as the tag's CAPS and the gateway's HELLO
 * report theirs. CAPS_STATUS and the Health server carry the numbers, the
 * maintenance port's HELLO/INFO the string. */
#define BRIDGE_FW_MAJOR APP_VERSION_MAJOR
#define BRIDGE_FW_MINOR APP_VERSION_MINOR
#define BRIDGE_FW_PATCH APP_PATCHLEVEL
#define BRIDGE_FW       APP_VERSION_STRING

/* INFO counters, written straight into items[max] (n: entries used) so the
 * lists are not copied on the maintenance stack. */
#define BRIDGE_COUNTER(lit, val)                                                                   \
	do {                                                                                       \
		if (n < max) {                                                                     \
			items[n++] = (struct ctag_cbor_counter)CTAG_CBOR_COUNTER(lit, (val));      \
		}                                                                                  \
	} while (0)

struct bridge {
	struct bflash flash;
	struct fontstore fonts;
	struct dlv dlv;
	struct sched sched;
	struct tsess sess[SCHED_LINKS]; /* one per link (CONFIG_CTAG_BRIDGE_SESSIONS) */
	bool flash_ok;
	uint32_t boot_id;
};

extern struct bridge br;
extern struct k_work_q bwq;
extern const struct ctag_sha256_ops bridge_sha;

/* Events from Bluetooth callbacks to the work queue. */
enum bev_type {
	BEV_ADVERT,
	BEV_CONNECTED,
	BEV_DISCONNECTED,
	BEV_GATT_STEP,
	BEV_CAPS,
	BEV_CTRL_WRITTEN,
	BEV_DATA_SENT,
	BEV_CTRL_VALUE,
	BEV_STATUS_VALUE,
};

struct bev {
	uint8_t type;
	uint8_t len;
	int8_t rssi;
	uint8_t flags;
	int err;
	uint32_t tag_id;
	void *ptr; /* the bt_conn of connected / disconnected */
	bt_addr_le_t addr;
	uint8_t link; /* the link a GATT event belongs to (connected/disconnected: by ptr) */
	uint8_t data[24];
};

void bev_post(const struct bev *e);
void central_event(const struct bev *e); /* on the work queue */

/* mesh.c */
int mesh_init(void);
void mesh_start(void);
/* Queue a vendor message to dst (0x0001 = the gateway) from the right model. */
void mesh_send(uint8_t op, const uint8_t *params, size_t len, uint16_t dst);
bool mesh_busy(void);
bool mesh_node_ready(void);
void mesh_set_suspended(bool suspended);
size_t mesh_counters(struct ctag_cbor_counter *items, size_t max);
void mesh_rx_drain(void);

/* central.c */
int central_init(void);
extern const struct sched_ops central_sched_ops;
extern const struct tsess_io central_tsess_io;
/* The tsess_io context of link i (br.sess[i]). */
void *central_link_ctx(uint8_t i);
size_t central_counters(struct ctag_cbor_counter *items, size_t max);

/* main.c */
void bridge_dlv_tick(void);
size_t bridge_counters(struct ctag_cbor_counter *items, size_t max);

/* maint_port.c */
/* On the work queue at the end of the boot: the port's receiver. */
int maint_port_start(void);
/* The main thread, once the work queue runs: the maintenance thread (never
 * returns once the port started). */
void maint_port_run(void);

/* persist.c */
int persist_save(void *ctx, const char *name, const void *data, size_t len);

/* identify.c */
void identify_start(uint8_t seconds);

#endif /* BRIDGE_APP_H_ */
