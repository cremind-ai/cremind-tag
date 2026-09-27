/*
 * Cremind Tag gateway (docs/gateway-firmware.md): the companion's serial link
 * (USB CDC ACM on the nRF52840, the UART on the nRF52832) to a Bluetooth Mesh
 * provisioner that delivers layouts to the bridges.
 *
 * main() starts the serial port (bytes are buffered while the mesh starts),
 * brings up Bluetooth, the mesh and the network from settings, draws a fresh
 * boot_id, loads the CDB into the core and then becomes the gateway loop
 * (gw_thread.c), which never returns.
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
	return n + gw_mesh_counters(&items[n], max - n);
}

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
};

int main(void)
{
	struct gw_info info = {
		.fw = APP_VERSION_STRING,
		.build = GW_BUILD,
		.board = CONFIG_CTAG_GW_BOARD_ID,
	};
	int err = uart_io_init(GW_UART);

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
	if (sys_csrand_get(&info.boot_id, sizeof(info.boot_id)) != 0) {
		info.boot_id = sys_rand32_get();
	}
	gw_core_init(&core, &backend, &info, k_uptime_get());
	gw_mesh_load_nodes(&core);
	gw_store_apply(&core);
	LOG_INF("gateway %s (%s) boot_id %08x, %u bridges", info.fw, info.build, info.boot_id,
		gw_node_count(&core));
	gw_run(&core); /* the gateway loop keeps main (and its stack) */
}
