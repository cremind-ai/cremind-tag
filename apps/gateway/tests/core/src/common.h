/* Shared fixtures of the gateway core tests: a mocked backend and a host. */
#ifndef GW_TEST_COMMON_H_
#define GW_TEST_COMMON_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/ztest.h>

#include <ctag/ctag_cbor.h>
#include <ctag/ctag_frame.h>
#include <ctag/proto_msgs.h>

#include "gw_core.h"

extern struct gw_core core;
extern int64_t now_ms;

#define BOOT_ID  0x12345678u
#define BRIDGE_A 0x0002u
#define BRIDGE_B 0x0003u

/* ---- Mocked backend ---- */

struct sent {
	uint16_t dst;
	uint8_t op;
	uint8_t len;
	uint32_t tag;
	uint8_t data[CTAG_MESH_MAX_VENDOR_PARAMS];
};

struct cfg_call {
	uint16_t addr;
	uint8_t step;
	uint8_t arg;
	uint32_t tag;
};

#ifdef CONFIG_CTAG_GW_RADIO
struct radio_write { /* a fragment the core handed to link_write() */
	uint8_t link;
	uint8_t chr;
	uint8_t len;
	uint8_t data[CTAG_ATT_VALUE_MAX];
};
#endif

struct mock {
	struct sent sent[512];
	size_t n_sent;
	struct cfg_call cfg[64];
	size_t n_cfg;
	int send_rc[16]; /* results of the next sends (then 0) */
	size_t send_rc_n;
	int cfg_rc;
	int provision_rc;
	uint8_t provisioned[8][16];
	size_t n_provision;
	uint16_t configured[8];
	size_t n_configured;
	uint16_t deleted[8];
	size_t n_deleted;
	size_t assign_stores;
	struct gw_assign stored_assign[32];
	size_t stored_assign_n;
	char stored_name[64];
	size_t stored_name_len;
	size_t reboots;
	size_t tx_room; /* bytes the "UART" accepts per write (SIZE_MAX = all) */
	/* v2 */
	uint8_t provision_oob[8][32];
	bool provision_oob_set[8];
	uint8_t stored_owner[176];
	size_t owner_writes;
	uint32_t stored_floor;
	bool owner_fail;
	bool random_fail; /* the backend's RNG (challenges) fails */
	size_t releases;
#ifdef CONFIG_CTAG_GW_RADIO
	/* the own radio (every mesh send asserts the mesh is not suspended) */
	bool listening;       /* the last radio_listen() */
	size_t listen_calls;
	bool suspended;       /* the mesh: between a successful suspend and resume */
	int suspend_rc;       /* bt_mesh_suspend() / bt_mesh_resume() results */
	int resume_rc;
	size_t suspends, resumes;
	int connect_rc;
	size_t connects;
	uint8_t connect_link;
	struct sched_peer connect_peer;
	uint32_t connect_timeout;
	size_t disconnects;
	uint8_t disconnect_link;
	int setup_rc;
	size_t setups;
	uint8_t setup_link, setup_mode;
	int write_rc[8]; /* results of the next link writes (then 0) */
	size_t write_rc_n;
	struct radio_write writes[256];
	size_t n_writes;
#endif
};

extern struct mock mock;

/* Fresh core with no session: BRIDGE_A (configured) and BRIDGE_B (not).
 * v2: owned by the test authority, the test worker's key pinned. */
void core_reset(void);
/* A HELLO'd session with a large host window; v2: then SECURE_OPEN as the
 * current worker (requests and answers are sealed from here on). */
void core_session(void);

#ifdef CONFIG_CTAG_GW_SECURE
#include <ctag/ctag_secure.h>

/* The test authority, owner and worker keys. */
extern const uint8_t test_auth_sk[32];
extern const uint8_t test_owner[16];
extern const uint8_t test_worker[32]; /* the pinned controller's private key */
extern const uint8_t test_other[32];  /* another computer's controller key */

/* v2: a fresh core that is unowned (factory state). */
void core_reset_unowned(void);
/* v2: a fresh core from these stored bytes and floor (the boot rule). */
void core_reset_record(const uint8_t *rec, size_t len, uint32_t floor);
/* v2: the worker key the next core_session()/host_open() uses. */
void host_use_worker(const uint8_t priv[32]);
/* v2: SECURE_OPEN from the current worker (after HELLO); returns its status. */
uint8_t host_open(void);
/* The same, granting this many credits with the SECURE_OPEN frame. */
uint8_t host_open_credits(uint8_t credits);
/* The same, with every gateway allocation after the first n failing. */
uint8_t host_open_fail_after(int32_t n);
/* v2: the host session is open (host_send seals). */
bool host_sealed(void);
/* v2: forget the host session (the next host_send goes in plaintext). */
void host_forget(void);
/* Send a request in plaintext whatever the session. */
uint16_t host_send_plain(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits);
/* A SECURE_DATA frame with these raw bytes as its data. */
uint16_t host_send_secure_raw(const uint8_t *data, size_t len, uint8_t credits);
/* Seal a secure message of this type/flags/rid with fields; returns the ciphertext length. */
int host_seal(uint8_t type, uint8_t flags, uint16_t rid, const struct ctag_cbor_field *f, size_t n,
	      uint8_t *out, size_t size);
/* The gateway's current challenge through STATUS (sealed). */
void host_challenge(uint8_t out[16]);
/* A grant of op for the gateway toward controller (the worker's public key). */
void host_grant(uint8_t op, const uint8_t controller[32], const uint8_t challenge[16],
		uint32_t gen_from, uint8_t *grant, size_t *len, uint8_t sig[64]);
/* CLAIM/RECOVER/RELEASE with a fresh challenge and a grant for the current worker. */
uint8_t host_grant_request(uint8_t type, uint8_t op, uint32_t gen_from);
#endif
/* Advance the clock and run the core's timers. */
void advance(int64_t ms);

size_t sent_count(uint8_t op);
const struct sent *last_sent(uint8_t op);
const struct sent *sent_at(size_t i);
void end_last(int err);        /* report the end of the last tagged send */
void end_tag(uint32_t tag, int err);

/* ---- Host side ---- */

struct frame {
	struct ctag_serial_header h;
	uint16_t len;
	uint8_t payload[CONFIG_CTAG_GW_TX_FRAME];
};

/* Send a request frame; returns its request id. credits = grant carried. */
uint16_t host_send(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits);
/* Raw bytes to the gateway. */
void host_bytes(const uint8_t *data, size_t len);
/* Bytes the gateway wrote that host_read() has not consumed yet. */
size_t capture_peek(const uint8_t **p);
/* Frames the gateway wrote since the last call (parsed from the capture). */
size_t host_read(void);
extern struct frame rx_frames[64];
extern size_t rx_count;
/* The response to rid among the frames read, or NULL. */
const struct frame *response(uint16_t rid);
/* The i-th event of a type among the frames read, or NULL. */
const struct frame *event(uint8_t type, size_t i);
size_t events_of(uint8_t type);
/* Decode the fields of a frame: keys set by the caller. */
void decode(const struct frame *fr, struct ctag_cbor_field *f, size_t n);
uint64_t field_u(const struct frame *fr, uint8_t key);
bool field_has(const struct frame *fr, uint8_t key);
/* A counter from INFO / GET_COUNTERS. */
uint32_t counter(const char *name);
/* Status of the response to a request: sends it, reads, returns status. */
uint8_t request_status(uint8_t type, const struct ctag_cbor_field *f, size_t n);

/* Convenience requests (op ids chosen by the caller). */
uint16_t send_deliver(uint64_t op_id, uint16_t bridge, uint64_t update_id, const uint8_t *layout,
		      size_t len);
uint16_t send_ack(uint32_t seq);

/* Mesh input helpers. */
void mesh_status(uint16_t src, uint16_t xfer_id, uint8_t status, uint32_t missing);
void mesh_result(uint16_t src, uint16_t result_seq, uint64_t update_id, uint8_t status);
/* A DELIVERY_RESULT with every field given. */
void mesh_result_msg(uint16_t src, const struct ctag_mesh_delivery_result *r);
void mesh_assign_status(uint16_t src, uint32_t tag_id, uint32_t epoch, uint8_t status);

/* Complete the segmented lane's sends until the commit, OK each end; returns
 * the xfer_id of the transfer. */
uint16_t drive_to_commit(void);

#endif /* GW_TEST_COMMON_H_ */
