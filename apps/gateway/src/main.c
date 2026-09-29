/*
 * Cremind Tag gateway (docs/gateway-firmware.md): the companion's serial link
 * (USB CDC ACM on the nRF52840, the UART on the nRF52832) to a Bluetooth Mesh
 * provisioner that delivers layouts to the bridges.
 *
 * main() starts the serial port (bytes are buffered while the mesh starts),
 * brings up Bluetooth, the mesh and the network from settings, draws a fresh
 * boot_id, loads the CDB into the core and then becomes the gateway loop
 * (gw_thread.c), which never returns.
 *
 * Protocol v2 (CONFIG_CTAG_GW_SECURE, docs/connect-setup.md): before the mesh
 * starts, a board button held through power-up for 10 s is the physical
 * factory reset; the identity key is generated with the hardware RNG at
 * first boot and kept in settings; the ownership record and the generation
 * floor are loaded; an unowned gateway never keeps a network (a released or
 * reset one, or a v1 network found at the first v2 boot, is wiped).
 *
 * Tags on the own radio (CONFIG_CTAG_GW_RADIO, docs/protocol.md 11): once the
 * mesh runs, central.c listens to its scan for tag advertisements and serves
 * the core's link operations.
 */
#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/util.h>

#include <app_version.h>
#include <psa/crypto.h>

#include "gw_app.h"
#include "gw_mesh.h"

LOG_MODULE_REGISTER(gateway, LOG_LEVEL_INF);

#define GW_UART DEVICE_DT_GET(DT_CHOSEN(cremind_gateway_uart))

#if defined(APP_BUILD_VERSION)
#define GW_BUILD STRINGIFY(APP_BUILD_VERSION)
#else
#define GW_BUILD "unknown"
#endif

static struct gw_core core;

static size_t be_write(void *ctx, const uint8_t *data, size_t len)
{
	ARG_UNUSED(ctx);
	return uart_io_write(data, len);
}

static void be_reboot(void *ctx)
{
	ARG_UNUSED(ctx);
	/* 10: the REBOOT answer is out of the ring; give the UART / USB FIFO a
	 * moment, then reset (USB re-enumerates, a new boot_id follows). */
	uart_io_flush(K_MSEC(200));
	k_sleep(K_MSEC(50));
	sys_reboot(SYS_REBOOT_COLD);
}

static size_t be_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	size_t n = uart_io_counters(items, max);

	ARG_UNUSED(ctx);
	n += gw_mesh_counters(&items[n], max - n);
#ifdef CONFIG_CTAG_GW_RADIO
	n += gw_central_counters(&items[n], max - n);
#endif
	return n;
}

#ifdef CONFIG_CTAG_GW_SECURE
static int be_random(void *ctx, uint8_t *buf, size_t len)
{
	ARG_UNUSED(ctx);
	return sys_csrand_get(buf, len);
}
#endif

static const struct gw_backend backend = {
	.write = be_write,
	.reboot = be_reboot,
	.mesh_send = gw_mesh_send,
	.mesh_cfg = gw_mesh_cfg,
	.provision = gw_mesh_provision,
	.node_configured = gw_mesh_node_configured,
	.node_delete = gw_mesh_node_delete,
	.store_name = gw_store_name,
	.store_assignments = gw_store_assignments,
	.sha256 = gw_sha256,
	.counters = be_counters,
#ifdef CONFIG_CTAG_GW_SECURE
	.store_owner = gw_store_save_owner,
	.random = be_random,
	.release = gw_mesh_wipe,
#endif
#ifdef CONFIG_CTAG_GW_RADIO
	.radio_listen = gw_central_listen,
	.radio_suspend = gw_central_suspend,
	.radio_resume = gw_central_resume,
	.link_connect = gw_central_connect,
	.link_disconnect = gw_central_disconnect,
	.link_setup = gw_central_setup,
	.link_write = gw_central_write,
#endif
};

#ifdef CONFIG_CTAG_GW_SECURE
/* The identity key: from settings, or generated now (first boot). */
static void load_identity(uint8_t ik[32])
{
	if (gw_store_identity(ik)) {
		return;
	}
	while (sys_csrand_get(ik, 32u) != 0) {
		/* The entropy driver not ready yet: an identity is never weakened. */
		k_sleep(K_MSEC(10));
	}
	if (gw_store_save_identity(ik) != 0) {
		LOG_ERR("identity key not stored");
	}
	LOG_INF("new identity key");
}

/*
 * The v2 part of start-up; true = the gateway must reboot (a factory reset
 * or a stale network was wiped, and the next boot creates a new network).
 */
static bool secure_boot(bool factory_reset)
{
	uint8_t ik[32];
	const uint8_t *rec;
	size_t rec_len;
	uint32_t floor;
	struct ctag_secure_ep *ep = &core.v2.ep;

	load_identity(ik);
	gw_store_owner(&rec, &rec_len, &floor);
	gw_core_secure_init(&core, ik, rec, rec_len, floor);
	ctag_secure_wipe(ik, sizeof(ik));
	if (factory_reset) {
		/* 4.3: ownership, mesh and assignments go; identity and generation stay. */
		struct ctag_owner_record r = {.state = CTAG_OWNER_UNOWNED, .gen = ep->rec.gen};
		uint8_t raw[CTAG_OWNER_RECORD_LEN];

		ctag_owner_record_encode(&r, raw);
		(void)gw_store_save_owner(NULL, raw, r.gen);
		gw_mesh_wipe(NULL);
		LOG_WRN("factory reset (generation %u kept)", r.gen);
		return true;
	}
	if (ep->rec.state != CTAG_OWNER_OWNED &&
	    (gw_node_count(&core) > 0u || core.assign_count > 0u)) {
		/* A network belongs to an owner: an unowned gateway never serves one. */
		gw_mesh_wipe(NULL);
		LOG_WRN("unowned gateway: stale network wiped");
		return true;
	}
	return false;
}
#endif

int main(void)
{
	struct gw_info info = {
		.fw = APP_VERSION_STRING,
		.build = GW_BUILD,
		.board = CONFIG_CTAG_GW_BOARD_ID,
	};
	int err;
#ifdef CONFIG_CTAG_GW_SECURE
	/* Before anything else: the button held through power-up (10 s). */
	bool factory_reset = gw_factory_reset_held();
#endif

	err = uart_io_init(GW_UART);
	if (err != 0) {
		LOG_ERR("serial port not ready: %d", err);
	}
	/* The mesh initialises PSA too; the layout digest needs it even if the
	 * mesh does not come up. */
	(void)psa_crypto_init();
	err = gw_mesh_start();
	if (err != 0) {
		/* Keep serving the host: INFO shows mesh_init, requests to bridges fail. */
		LOG_ERR("mesh unavailable: %d", err);
	}
#ifdef CONFIG_CTAG_GW_RADIO
	if (err == 0) {
		/* Tags on the own radio are heard through the mesh's scan: once it runs. */
		gw_central_init();
	}
#endif
	if (sys_csrand_get(&info.boot_id, sizeof(info.boot_id)) != 0) {
		info.boot_id = sys_rand32_get();
	}
	gw_core_init(&core, &backend, &info, k_uptime_get());
	gw_mesh_load_nodes(&core);
	gw_store_apply(&core);
#ifdef CONFIG_CTAG_GW_SECURE
	if (secure_boot(factory_reset)) {
		k_sleep(K_MSEC(1000)); /* the LED stays solid for a moment */
		sys_reboot(SYS_REBOOT_COLD);
	}
#endif
	LOG_INF("gateway %s (%s) boot_id %08x, %u bridges", info.fw, info.build, info.boot_id,
		gw_node_count(&core));
	gw_run(&core); /* the gateway loop keeps main (and its stack) */
}
