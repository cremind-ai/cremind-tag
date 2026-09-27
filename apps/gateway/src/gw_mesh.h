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
int gw_mesh_provision(void *ctx, const uint8_t uuid[16]);
void gw_mesh_node_configured(void *ctx, uint16_t addr);
void gw_mesh_node_delete(void *ctx, uint16_t addr);
int gw_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32]);
size_t gw_mesh_counters(struct ctag_cbor_counter *items, size_t max);

/* store.c: bridge names and assignments under the settings key "ctag/gw". */
void gw_store_name(void *ctx, uint16_t addr, const char *name, size_t len);
void gw_store_assignments(void *ctx, const struct gw_assign *a, size_t n);
/* Hand what settings_load() found to the core (after its nodes were added). */
void gw_store_apply(struct gw_core *g);

#endif /* GW_MESH_H_ */
