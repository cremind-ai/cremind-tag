/*
 * The bridge's records in Zephyr settings (NVS on the internal flash storage
 * partition, beside the mesh state): assignments with K_epoch ("ctag/a/<i>"),
 * per-tag history ("ctag/h/<i>") and the result_seq reservation ("ctag/rs").
 */
#include <errno.h>

#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>

#include "bridge.h"

static int ctag_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	uint8_t buf[64];
	ssize_t n;

	if (name == NULL || len > sizeof(buf)) {
		return -EINVAL;
	}
	n = read_cb(cb_arg, buf, len);
	if (n < 0) {
		return (int)n;
	}
	dlv_restore(&br.dlv, name, buf, (size_t)n);
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ctag, "ctag", NULL, ctag_set, NULL, NULL);

int persist_save(void *ctx, const char *name, const void *data, size_t len)
{
	char key[16];

	ARG_UNUSED(ctx);
	(void)snprintk(key, sizeof(key), "ctag/%s", name);
	return len > 0u ? settings_save_one(key, data, len) : settings_delete(key);
}
