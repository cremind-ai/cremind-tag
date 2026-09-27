/*
 * The gateway's own persistent records (the mesh stack persists the CDB,
 * keys, IV index and sequence number itself): bridge names under
 * "ctag/gw/n/<addr>" and the assignment table under "ctag/gw/a" (10-byte
 * little-endian records: bridge u16, tag_id u32, epoch u32).
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/byteorder.h>

#include "gw_mesh.h"

#define ASSIGN_REC 10u

static struct {
	uint16_t addr;
	uint8_t len;
	char name[CONFIG_CTAG_GW_NAME_MAX];
} names[GW_NODES];
static size_t names_n;
static struct gw_assign assigns[CONFIG_CTAG_GW_ASSIGN_MAX];
static size_t assigns_n;

static int store_set(const char *key, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	const char *next;

	if (settings_name_steq(key, "a", &next) && next == NULL) {
		uint8_t rec[ASSIGN_REC];

		assigns_n = 0u;
		while (len >= ASSIGN_REC && assigns_n < ARRAY_SIZE(assigns)) {
			if (read_cb(cb_arg, rec, sizeof(rec)) != (ssize_t)sizeof(rec)) {
				break;
			}
			assigns[assigns_n].bridge = sys_get_le16(&rec[0]);
			assigns[assigns_n].tag_id = sys_get_le32(&rec[2]);
			assigns[assigns_n].epoch = sys_get_le32(&rec[6]);
			assigns_n++;
			len -= ASSIGN_REC;
		}
		return 0;
	}
	if (settings_name_steq(key, "n", &next) && next != NULL && names_n < ARRAY_SIZE(names)) {
		unsigned long addr = strtoul(next, NULL, 16);
		ssize_t n = read_cb(cb_arg, names[names_n].name, sizeof(names[names_n].name));

		if (n > 0 && addr != 0u) {
			names[names_n].addr = (uint16_t)addr;
			names[names_n].len = (uint8_t)n;
			names_n++;
		}
		return 0;
	}
	return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(ctag_gw, "ctag/gw", NULL, store_set, NULL, NULL);

void gw_store_name(void *ctx, uint16_t addr, const char *name, size_t len)
{
	char key[20];

	ARG_UNUSED(ctx);
	(void)snprintf(key, sizeof(key), "ctag/gw/n/%04x", addr);
	if (len == 0u) {
		(void)settings_delete(key);
	} else {
		(void)settings_save_one(key, name, len);
	}
}

void gw_store_assignments(void *ctx, const struct gw_assign *a, size_t n)
{
	static uint8_t buf[CONFIG_CTAG_GW_ASSIGN_MAX * ASSIGN_REC];

	ARG_UNUSED(ctx);
	for (size_t i = 0; i < n && i < CONFIG_CTAG_GW_ASSIGN_MAX; i++) {
		sys_put_le16(a[i].bridge, &buf[i * ASSIGN_REC]);
		sys_put_le32(a[i].tag_id, &buf[i * ASSIGN_REC + 2u]);
		sys_put_le32(a[i].epoch, &buf[i * ASSIGN_REC + 6u]);
	}
	if (n == 0u) {
		(void)settings_delete("ctag/gw/a");
	} else {
		(void)settings_save_one("ctag/gw/a", buf, n * ASSIGN_REC);
	}
}

void gw_store_apply(struct gw_core *g)
{
	for (size_t i = 0; i < names_n; i++) {
		gw_core_set_name(g, names[i].addr, names[i].name, names[i].len);
	}
	gw_core_set_assignments(g, assigns, assigns_n);
}
