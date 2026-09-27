/* Shared fixtures of the bridge core tests. */
#ifndef BRIDGE_TEST_COMMON_H_
#define BRIDGE_TEST_COMMON_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/ztest.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_layout.h>
#include <ctag/proto_msgs.h>

#include "bflash.h"
#include "delivery.h"
#include "fontstore.h"

#define EXT_FLASH_DEV DEVICE_DT_GET(DT_CHOSEN(cremind_bridge_flash))
#define MIB           (1024u * 1024u)

extern const struct ctag_sha256_ops test_sha;

/* The simulated part: erased in place (fast), raw access for corruption. */
void flash_wipe(void);
uint8_t *flash_mem(void);
/* Power-cut injection: the next writes stop (-EIO) after `bytes` more bytes;
 * a negative value disables it. */
void flash_cut_after(int32_t bytes);

void sha256(const void *data, size_t len, uint8_t out[32]);

/* ---- Persistence fake (settings) ---- */
void kv_clear(void);
int kv_save(void *ctx, const char *name, const void *data, size_t len);
void kv_replay(struct dlv *d);
/* Called on every save before it is stored (NULL: none). */
extern void (*save_probe)(const char *name);
size_t kv_count(void);
bool kv_get(const char *name, void *data, size_t *len);

/* ---- Outbound mesh messages ---- */
struct sent_msg {
	uint8_t op;
	uint8_t len;
	uint8_t data[CTAG_MESH_MAX_VENDOR_PARAMS];
};

extern struct sent_msg sent_log[128];
extern size_t sent_n;
void sent_clear(void);
void record_send(void *ctx, uint8_t op, const uint8_t *params, size_t len);
/* Called on every outbound message before it is logged (NULL: none). */
extern void (*send_probe)(uint8_t op, const uint8_t *params, size_t len);
size_t sent_count(uint8_t op);
/* The last DELIVERY_RESULT for update_id; false when none. */
bool last_result(uint64_t update_id, struct ctag_mesh_delivery_result *r);
size_t results_for(uint64_t update_id);
/* 10: every DELIVERY_RESULT sent for an update_id is the same message with
 * the same result_seq, and different update_ids never share a result_seq. */
void assert_one_seq_per_update(void);

/* ---- A booted bridge core (flash, fonts, delivery) ---- */
struct bridge_env {
	struct bflash flash;
	struct fontstore fonts;
	struct dlv dlv;
	bool flash_ok;
};

extern struct bridge_env benv;
extern uint8_t scratch[CTAG_LAYOUT_HARD_MAX];

/* Boot from the current flash and persisted records (a reset keeps both). */
void env_boot(uint32_t now);
/* Fresh device: erase the flash, forget settings, boot. */
void env_fresh(uint32_t now);
/* Install a pack over the store's FONT_* path in chunks of `chunk` bytes. */
uint8_t install_pack(const uint8_t *pack, size_t len, size_t chunk);
void install_fixture_pack(void);
extern const uint8_t *fixture_pack;
extern size_t fixture_pack_len;
extern const uint8_t *fixture_pack_id;

/* ---- Layout delivery over LAYOUT_BEGIN / CHUNK / COMMIT ---- */
struct xfer {
	uint16_t xfer_id;
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint64_t update_id;
	const uint8_t *fontpack_id; /* NULL: the fixture pack */
};

void layout_digest(const uint8_t *layout, size_t len, uint8_t digest[CTAG_LAYOUT_DIGEST_LEN]);
/* Begin + every chunk (skip: bitmap of chunks not sent), no commit. */
void transfer(struct dlv *d, const struct xfer *x, const uint8_t *layout, size_t len,
	      uint32_t skip);
/* transfer() + commit. */
uint8_t deliver(struct dlv *d, const struct xfer *x, const uint8_t *layout, size_t len,
		uint32_t skip, uint32_t now, uint32_t *missing);
void assign(struct dlv *d, uint32_t tag_id, uint32_t epoch, const uint8_t key[16]);

/* A valid fixture layout (status_card) and a 4096-byte one built in code. */
extern const uint8_t *small_layout;
extern size_t small_layout_len;
size_t big_layout(uint8_t *out); /* LAYOUT_HARD_MAX bytes, valid for the fixture pack */

#endif /* BRIDGE_TEST_COMMON_H_ */
