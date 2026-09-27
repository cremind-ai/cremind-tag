/* Cremind Tag bridge: boot and the bridge work queue (docs/bridge-firmware.md). */
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/mesh.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/util.h>

#include <ctag/ctag_crypto.h>

#include "bridge.h"

LOG_MODULE_REGISTER(bridge, LOG_LEVEL_INF);

#define GATEWAY_ADDR 0x0001

struct bridge br;
struct k_work_q bwq;
static K_THREAD_STACK_DEFINE(bwq_stack, CONFIG_CTAG_BRIDGE_WQ_STACK_SIZE);

/* ---- SHA-256 for the work queue (layout digests, frame pre-pass) ---- */

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

const struct ctag_sha256_ops bridge_sha = {sha_init, sha_update, sha_finish, &sha_ctx};

/* ---- Events from Bluetooth callbacks ---- */

K_MSGQ_DEFINE(bev_q, sizeof(struct bev), CONFIG_CTAG_BRIDGE_EVENT_QUEUE, 4);
static atomic_t bev_dropped;

static void bev_work_fn(struct k_work *work)
{
	struct bev e;

	ARG_UNUSED(work);
	while (k_msgq_get(&bev_q, &e, K_NO_WAIT) == 0) {
		central_event(&e);
	}
}

static K_WORK_DEFINE(bev_work, bev_work_fn);

void bev_post(const struct bev *e)
{
	/* Advertisements never crowd out connection and GATT events: they are
	 * dropped once the queue is half full (tags advertise again 250 ms later). */
	if ((e->type == BEV_ADVERT &&
	     k_msgq_num_used_get(&bev_q) >= CONFIG_CTAG_BRIDGE_EVENT_QUEUE / 2) ||
	    k_msgq_put(&bev_q, e, K_NO_WAIT) != 0) {
		atomic_inc(&bev_dropped);
		return;
	}
	(void)k_work_submit_to_queue(&bwq, &bev_work);
}

size_t bridge_counters(struct ctag_cbor_counter *items, size_t max)
{
	const struct dlv_counters *d = &br.dlv.c;
	size_t n = 0u;

	BRIDGE_COUNTER("events_dropped", (uint32_t)atomic_get(&bev_dropped));
	BRIDGE_COUNTER("layouts_accepted", d->layouts_accepted);
	BRIDGE_COUNTER("duplicates", d->duplicates);
	BRIDGE_COUNTER("superseded", d->superseded);
	BRIDGE_COUNTER("results", d->results);
	BRIDGE_COUNTER("result_resends", d->result_resends);
	BRIDGE_COUNTER("results_unacked", d->results_unacked);
	BRIDGE_COUNTER("results_repeated", d->results_repeated);
	BRIDGE_COUNTER("storage_errors", d->storage_errors);
	BRIDGE_COUNTER("unauth_final", d->unauth_final);
	BRIDGE_COUNTER("queue_depth", dlv_queue_depth(&br.dlv));
	BRIDGE_COUNTER("assigned", dlv_assigned_count(&br.dlv));
	BRIDGE_COUNTER("flash_errors", br.flash.errors);
	BRIDGE_COUNTER("cache_hits", br.flash.hits);
	BRIDGE_COUNTER("cache_misses", br.flash.misses);
	n += central_counters(&items[n], max - n);
	n += mesh_counters(&items[n], max - n);
	return n;
}

/* ---- DELIVERY_RESULT re-sends (docs/protocol.md 3.4) ---- */

static void tick_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	bridge_dlv_tick();
}

static K_WORK_DELAYABLE_DEFINE(tick_work, tick_fn);

void bridge_dlv_tick(void)
{
	uint32_t next = dlv_tick(&br.dlv, k_uptime_get_32());

	if (next == UINT32_MAX) {
		(void)k_work_cancel_delayable(&tick_work);
	} else {
		(void)k_work_reschedule_for_queue(&bwq, &tick_work, K_MSEC(next));
	}
}

static void dlv_send(void *ctx, uint8_t op, const uint8_t *params, size_t len)
{
	ARG_UNUSED(ctx);
	mesh_send(op, params, len, GATEWAY_ADDR);
	if (op == CTAG_MESH_OP_DELIVERY_RESULT) {
		/* Keeps an earlier deadline; tick_fn then computes the exact one. */
		(void)k_work_schedule_for_queue(&bwq, &tick_work, K_MSEC(CTAG_MESH_RESULT_RETRY_MS));
	}
}

/* ---- Boot (on the work queue; the main thread then serves the maintenance port) ---- */

static void init_fn(struct k_work *work)
{
	const struct device *ext = DEVICE_DT_GET(DT_CHOSEN(cremind_bridge_flash));
	struct dlv_env env = {
		.send = dlv_send,
		.save = persist_save,
		.flash = &br.flash,
		.fonts = &br.fonts,
		.sha = &bridge_sha,
	};
	uint8_t id[CTAG_FONTPACK_ID_LEN];
	int err;

	ARG_UNUSED(work);
	LOG_INF("Cremind Tag bridge %s, board %d", BRIDGE_FW, CONFIG_CTAG_BRIDGE_BOARD_ID);
	br.boot_id = sys_rand32_get();
	err = bflash_init(&br.flash, ext, CONFIG_CTAG_BRIDGE_WORKING_SPACE);
	br.flash_ok = err == 0;
	if (br.flash_ok) {
		LOG_INF("external flash %u B: slots of %u B, directory at 0x%x, %u pending slots",
			br.flash.geom.flash_size, br.flash.geom.slot_size, br.flash.geom.dir_off,
			br.flash.geom.pending_slots);
	} else {
		LOG_ERR("external flash unusable (%d): no font pack, no layouts", err);
	}
	err = ctag_crypto_init();
	if (err != 0) {
		LOG_ERR("PSA crypto init failed (%d)", err);
	}
	fontstore_init(&br.fonts, &br.flash, br.flash_ok);
	if (fontstore_active_id(&br.fonts, id)) {
		LOG_INF("font pack %02x%02x%02x%02x%02x%02x%02x%02x in slot %u", id[0], id[1], id[2],
			id[3], id[4], id[5], id[6], id[7], br.fonts.active.slot);
	} else if (br.fonts.has_record) {
		LOG_WRN("font pack in slot %u failed validation (%u)", br.fonts.active.slot,
			br.fonts.last_open_status);
	}
	env.flash_ok = br.flash_ok;
	dlv_init(&br.dlv, &env);
	sched_init(&br.sched, &central_sched_ops, NULL);
	for (uint8_t i = 0; i < ARRAY_SIZE(br.sess); i++) {
		tsess_init(&br.sess[i], &central_tsess_io, central_link_ctx(i), &br.dlv, &br.fonts,
			   &bridge_sha);
	}

	err = settings_subsys_init();
	if (err != 0) {
		LOG_ERR("settings init failed (%d)", err);
	}
	err = bt_enable(NULL);
	if (err != 0) {
		LOG_ERR("bt_enable failed (%d)", err);
		return;
	}
	err = mesh_init();
	if (err != 0) {
		LOG_ERR("mesh init failed (%d)", err);
		return;
	}
	/* Mesh state (keys, addresses, IV index) and the bridge's records. */
	(void)settings_load();
	dlv_start(&br.dlv, k_uptime_get_32());
	LOG_INF("%u tags assigned, %u jobs restored", dlv_assigned_count(&br.dlv),
		dlv_queue_depth(&br.dlv));
	err = central_init();
	if (err != 0) {
		LOG_ERR("central init failed (%d)", err);
	}
	mesh_start();
	/* Anything Bluetooth posted during the boot is handled now. */
	(void)k_work_submit_to_queue(&bwq, &bev_work);
	mesh_rx_drain();
	err = maint_port_start();
	if (err != 0) {
		LOG_ERR("maintenance port unavailable (%d)", err);
	}
}

static K_WORK_DEFINE(init_work, init_fn);

int main(void)
{
	struct k_work_queue_config cfg = {.name = "bridge"};

	k_work_queue_start(&bwq, bwq_stack, K_THREAD_STACK_SIZEOF(bwq_stack),
			   CONFIG_CTAG_BRIDGE_WQ_PRIORITY, &cfg);
	(void)k_work_submit_to_queue(&bwq, &init_work);
	/* This thread (and its stack) becomes the maintenance port's. */
	maint_port_run();
	return 0;
}
