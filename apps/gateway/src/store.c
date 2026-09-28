/*
 * The gateway's own persistent records (the mesh stack persists the CDB,
 * keys, IV index and sequence number itself): bridge names under
 * "ctag/gw/n/<addr>" and the assignment table under "ctag/gw/a" (10-byte
 * little-endian records: bridge u16, tag_id u32, epoch u32).
 *
 * Protocol v2 (docs/connect-setup.md 2.1, 4.1): the identity key under
 * "ctag/gw/id" (32 bytes, generated on the device at first boot, never
 * exported), the ownership record under "ctag/gw/own" (lib/secure's
 * 176-byte record, CRC-protected) and the generation floor under
 * "ctag/gw/genf" (u32le), written before the record so that a record lost to
 * corruption can never rewind the generation. Each settings write is atomic
 * (NVS).
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

#ifdef CONFIG_CTAG_GW_SECURE
static uint8_t identity[32];
static bool identity_ok;
static uint8_t owner_rec[CTAG_OWNER_RECORD_LEN];
static size_t owner_len;
static uint32_t gen_floor;
#endif

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
#ifdef CONFIG_CTAG_GW_SECURE
	if (settings_name_steq(key, "id", &next) && next == NULL) {
		identity_ok = len == sizeof(identity) &&
			      read_cb(cb_arg, identity, sizeof(identity)) == (ssize_t)sizeof(identity);
		return 0;
	}
	if (settings_name_steq(key, "own", &next) && next == NULL) {
		ssize_t n = len <= sizeof(owner_rec) ? read_cb(cb_arg, owner_rec, sizeof(owner_rec)) : -1;

		/* A record of another size is kept as unreadable: UNOWNED + floor. */
		owner_len = n > 0 ? (size_t)n : 0u;
		return 0;
	}
	if (settings_name_steq(key, "genf", &next) && next == NULL) {
		uint8_t raw[4];

		if (len == sizeof(raw) && read_cb(cb_arg, raw, sizeof(raw)) == (ssize_t)sizeof(raw)) {
			gen_floor = sys_get_le32(raw);
		}
		return 0;
	}
#endif
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

#ifdef CONFIG_CTAG_GW_SECURE

bool gw_store_identity(uint8_t ik[32])
{
	if (!identity_ok) {
		return false;
	}
	memcpy(ik, identity, sizeof(identity));
	memset(identity, 0, sizeof(identity)); /* the core keeps the one copy */
	identity_ok = false;
	return true;
}

int gw_store_save_identity(const uint8_t ik[32])
{
	return settings_save_one("ctag/gw/id", ik, 32u);
}

void gw_store_owner(const uint8_t **rec, size_t *len, uint32_t *floor)
{
	*rec = owner_len > 0u ? owner_rec : NULL;
	*len = owner_len;
	*floor = gen_floor;
}

int gw_store_save_owner(void *ctx, const uint8_t rec[CTAG_OWNER_RECORD_LEN], uint32_t gen)
{
	int err = 0;

	ARG_UNUSED(ctx);
	if (gen > gen_floor) {
		uint8_t raw[4];

		sys_put_le32(gen, raw);
		err = settings_save_one("ctag/gw/genf", raw, sizeof(raw));
		if (err != 0) {
			return err; /* nothing changed: the old record and floor stand */
		}
		gen_floor = gen;
	}
	err = settings_save_one("ctag/gw/own", rec, CTAG_OWNER_RECORD_LEN);
	memset(owner_rec, 0, sizeof(owner_rec)); /* the boot copy is stale now */
	owner_len = 0u;
	return err;
}

#endif
