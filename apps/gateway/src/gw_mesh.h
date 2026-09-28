/* Firmware backend of the gateway core: mesh (mesh.c) and settings (store.c). */
#ifndef GW_MESH_H_
#define GW_MESH_H_

#include <stddef.h>
#include <stdint.h>

#include "gw_core.h"

/* mesh.c */
int gw_mesh_start(void); /* bt_enable, mesh, settings, CDB, self-provisioning */
void gw_mesh_load_nodes(struct gw_core *g);
int gw_mesh_send(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
		 uint32_t tag);
int gw_mesh_cfg(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag);
int gw_mesh_provision(void *ctx, const uint8_t uuid[16], const uint8_t *static_oob);
void gw_mesh_node_configured(void *ctx, uint16_t addr);
void gw_mesh_node_delete(void *ctx, uint16_t addr);
int gw_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32]);
size_t gw_mesh_counters(struct ctag_cbor_counter *items, size_t max);
#ifdef CONFIG_CTAG_GW_SECURE
/* Wipe the network: names, assignments, the CDB and the node state (v2). */
void gw_mesh_wipe(void *ctx);
#endif

/* store.c: bridge names and assignments under the settings key "ctag/gw". */
void gw_store_name(void *ctx, uint16_t addr, const char *name, size_t len);
void gw_store_assignments(void *ctx, const struct gw_assign *a, size_t n);
/* Hand what settings_load() found to the core (after its nodes were added). */
void gw_store_apply(struct gw_core *g);
#ifdef CONFIG_CTAG_GW_SECURE
/*
 * v2 records (connect-setup.md 2.1, 4.1): the identity key "ctag/gw/id"
 * (generated at first boot, never exported), the ownership record
 * "ctag/gw/own" and the generation floor "ctag/gw/genf".
 */
bool gw_store_identity(uint8_t ik[32]);
int gw_store_save_identity(const uint8_t ik[32]);
/* The stored record (NULL, 0 = none) and floor, as settings_load() found them. */
void gw_store_owner(const uint8_t **rec, size_t *len, uint32_t *floor);
/* Backend store_owner: raise the floor to gen, then write the record. */
int gw_store_save_owner(void *ctx, const uint8_t rec[CTAG_OWNER_RECORD_LEN], uint32_t gen);
#endif

#endif /* GW_MESH_H_ */
