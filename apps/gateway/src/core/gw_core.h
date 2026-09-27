/*
 * Gateway core (docs/gateway-firmware.md): the serial protocol server of
 * docs/protocol.md 1 and 10, the provisioner's node bookkeeping of 2 and the
 * layout delivery engine of 3.2-3.4, as one single-threaded state machine.
 *
 * Nothing here calls Zephyr's Bluetooth or kernel APIs: time is passed in
 * (milliseconds, monotonic), bytes and mesh events are fed in, and the
 * platform (struct gw_backend) sends mesh messages, runs configuration-client
 * steps, persists records and writes serial bytes. The firmware drives it from
 * one thread (gw_thread.c); the native_sim tests drive it directly with a
 * mocked backend and a fake clock.
 *
 * Behaviour mirrors the companion's simulator (companion/src/cremind_tag/sim/
 * gateway.py, device.py); docs/gateway-firmware.md lists where real hardware
 * forced a difference.
 */
#ifndef GW_CORE_H_
#define GW_CORE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_cbor.h>
#include <ctag/ctag_frame.h>
#include <ctag/proto_msgs.h>

#include "gw_ring.h"

#define GW_ADDR               0x0001u /* the gateway's primary element (2) */
#define GW_UNSEG_MAX          11u     /* access PDU bytes sent unsegmented (4-byte TransMIC) */
#define GW_SEND_RETRIES       3u      /* 3.2 rule 1 */
#define GW_INCOMPLETE_ROUNDS  3u      /* 3.2 rule 3 */
#define GW_COMMIT_RESENDS     3u      /* 3.2 rule 3 */
#define GW_REPLY_ATTEMPTS     4u      /* ASSIGN_SET/DEL sent at most 1 + 3 times */
#define GW_CFG_ATTEMPTS       3u      /* each configuration step */
#define GW_RETRY_MS           100     /* the stack had no buffer: try again */
#define GW_LANE_WATCHDOG_MS   30000   /* a segmented send whose end never came */
#define GW_REBOOT_GRACE_MS    2000    /* REBOOT: reset even if the answer was dropped */
#define GW_IDENTIFY_S         5u
#define GW_UUID_LEN           16u
#define GW_FW_STR             12u     /* "255.255.255" */
#define GW_SCAN_SEEN          16u
#define GW_TAG_SEEN_SLOTS     32u
#define GW_NODES              CTAG_MAX_BRIDGES

/* ---- Platform ---- */

/* Configuration-client steps (CONFIGURE_NODE, then REMOVE_NODE's reset). */
enum gw_cfg_step {
	GW_CFG_APP_KEY_ADD = 0, /* segmented: completion via gw_core_send_end() */
	GW_CFG_BIND_LAYOUT,     /* LAYOUT_SRV bound to app key 0 */
	GW_CFG_BIND_MGMT,       /* MGMT_SRV bound to app key 0 */
	GW_CFG_RELAY,           /* arg: relay on/off (retransmit 2 x 20 ms) */
	GW_CFG_TTL,             /* arg: default TTL */
	GW_CFG_NET_TX,          /* network transmit 3 x 20 ms */
	GW_CFG_RESET,           /* node reset (REMOVE_NODE) */
	GW_CFG_STEPS,
};

struct gw_assign {
	uint16_t bridge;
	uint32_t tag_id;
	uint32_t epoch;
};

struct gw_backend {
	void *ctx;
	/* Serial output: returns how many bytes were accepted (the rest is
	 * offered again after gw_core_poll()). */
	size_t (*write)(void *ctx, const uint8_t *data, size_t len);
	/* The REBOOT answer went out (or its grace time passed): reset. */
	void (*reboot)(void *ctx);
	/* Vendor message CTAG_MESH_OP_<op> from the matching client model to
	 * dst (app key 0). tag != 0: report the stack's end callback through
	 * gw_core_send_end(tag, err). Returns 0, -EBUSY/-ENOBUFS/-EAGAIN (no
	 * buffer: offered again later) or another -errno (failed). */
	int (*mesh_send)(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
			 uint32_t tag);
	/* Configuration-client step to addr (device key). The reply arrives
	 * through gw_core_cfg_status(). tag as for mesh_send (APP_KEY_ADD). */
	int (*mesh_cfg)(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag);
	/* PB-ADV provisioning of uuid, address from the CDB allocator. */
	int (*provision)(void *ctx, const uint8_t uuid[GW_UUID_LEN]);
	/* CDB: set BT_MESH_CDB_NODE_CONFIGURED and store; delete and store. */
	void (*node_configured)(void *ctx, uint16_t addr);
	void (*node_delete)(void *ctx, uint16_t addr);
	/* The gateway's own records (settings). */
	void (*store_name)(void *ctx, uint16_t addr, const char *name, size_t len);
	void (*store_assignments)(void *ctx, const struct gw_assign *a, size_t n);
	/* SHA-256 (LAYOUT_BEGIN.digest is its first 16 bytes). */
	int (*sha256)(void *ctx, const uint8_t *data, size_t len, uint8_t out[32]);
	/* Extra counters for INFO / GET_COUNTERS; returns how many were filled. */
	size_t (*counters)(void *ctx, struct ctag_cbor_counter *items, size_t max);
};

struct gw_info {
	const char *fw;
	const char *build;
	uint8_t board;
	uint32_t boot_id;
};

/* ---- State ---- */

struct gw_resp { /* an answer waiting for a host credit */
	uint16_t rid;
	uint8_t type;
	uint8_t status;
	bool duplicate; /* detail = DUPLICATE */
	bool reboot;    /* REBOOT: reset once this answer is written */
	const char *text;
};

struct gw_retained {
	uint32_t seq;
	uint8_t type;
	uint8_t len;
	uint8_t payload[CONFIG_CTAG_GW_EVENT_MAX];
};

struct gw_idem {
	uint64_t op_id;
	const char *text;
	uint8_t status;
	bool used;
};

struct gw_serial {
	struct ctag_serial_rx rx;
	bool hello_done;
	uint16_t send_credits; /* frames the gateway may still send */
	int32_t host_budget;   /* frames the host may still send (our view) */
	uint16_t owed;         /* credits to return to the host */
	/* HELLO answer pending (exempt from credits) */
	bool hello_pending;
	uint16_t hello_rid;
	uint8_t hello_status;
	/* responses */
	struct gw_resp resp[CONFIG_CTAG_GW_SERIAL_CREDITS];
	uint8_t resp_head, resp_count;
	/* retained events */
	struct gw_retained ret[CTAG_SERIAL_EVENT_RETAIN];
	uint8_t ret_head, ret_count;
	uint32_t seq;          /* last retained seq of this boot */
	uint32_t next_retained;
	/* best-effort events: records [type][payload] */
	struct gw_ring evq;
	/* idempotency (1.4) */
	struct gw_idem idem[CTAG_SERIAL_IDEMPOTENCY_SLOTS];
	uint8_t idem_next;
	/* transmit: s->tx is COBS-encoded straight into the driver (no copy) */
	size_t tx_len;   /* decoded frame being sent (0 = none) */
	size_t cobs_start, cobs_n, cobs_off;
	uint8_t cobs_phase;
	bool reboot_after_frame;
	int64_t reboot_at; /* 0 = none */
	uint8_t rxbuf[CTAG_SERIAL_MAX_FRAME] __attribute__((aligned(4)));
	uint8_t tx[CONFIG_CTAG_GW_TX_FRAME] __attribute__((aligned(4)));
	uint8_t evq_buf[CONFIG_CTAG_GW_EVQ_BYTES] __attribute__((aligned(4)));
};

/* Segmented-send lane (3.2 rule 1): one outstanding segmented send. */
enum gw_requester {
	GW_REQ_DELIVERY = 0,
	GW_REQ_CFG = 1,
	GW_REQ_OP0 = 2, /* + mesh op slot */
	GW_REQ_COUNT = GW_REQ_OP0 + CONFIG_CTAG_GW_MESH_OPS,
};

struct gw_lane {
	uint8_t fifo[GW_REQ_COUNT];
	uint8_t head, count;
	int16_t active;   /* requester, -1 = idle */
	uint8_t attempts; /* failed ends of the current message */
	uint32_t tag;     /* tag of the send in flight (0 = waiting to be issued) */
	uint32_t tag_seq;
	int64_t retry_at;    /* re-issue after a buffer shortage (0 = none) */
	int64_t watchdog_at; /* no end callback by then: failed */
};

struct gw_unseg {
	uint16_t dst;
	uint8_t op;
	uint8_t len;
	uint8_t params[8];
};

struct gw_unseg_q {
	struct gw_unseg q[CONFIG_CTAG_GW_UNSEG_QUEUE];
	uint8_t head, count;
	int64_t retry_at;
};

enum gw_dstate { GW_D_FREE = 0, GW_D_QUEUED, GW_D_ACTIVE };

struct gw_delivery {
	uint8_t state;
	uint16_t bridge;
	uint16_t len;
	uint32_t order;
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint64_t update_id;
	uint64_t op_id;
	uint8_t fontpack_id[CTAG_FONTPACK_ID_LEN];
	uint8_t *layout; /* in the layout arena */
};

enum gw_xphase { GW_X_IDLE = 0, GW_X_BEGIN, GW_X_CHUNKS, GW_X_COMMIT };

struct gw_xfer { /* the one active transfer */
	int8_t slot;
	uint8_t phase;
	uint8_t chunk_count;
	uint8_t cur;      /* chunk being sent */
	uint8_t round;    /* INCOMPLETE rounds used */
	uint8_t commits;  /* LAYOUT_COMMITs sent */
	uint16_t xfer_id;
	uint32_t pending; /* chunks still to send this round */
	int64_t started;
	int64_t deadline; /* LAYOUT_STATUS wait */
	uint8_t digest[CTAG_LAYOUT_DIGEST_LEN];
};

struct gw_at_bridge {
	bool used;
	uint16_t bridge;
	uint32_t order;
	uint32_t tag_id;
	uint32_t epoch;
	uint32_t revision;
	uint32_t mesh_ms;
	uint64_t update_id;
};

struct gw_node {
	bool used;
	bool configured;
	bool caps_valid;
	bool health_valid;
	uint16_t addr;
	uint8_t elements;
	uint8_t name_len;
	uint8_t uuid[GW_UUID_LEN];
	char name[CONFIG_CTAG_GW_NAME_MAX];
	int64_t last_seen;
	struct ctag_mesh_caps_status caps;
	struct ctag_mesh_health_status health;
};

struct gw_prov {
	bool active;
	bool link_open;
	bool added;
	uint8_t elements;
	uint8_t name_len;
	uint16_t addr;
	uint64_t op_id;
	int64_t deadline;
	uint8_t uuid[GW_UUID_LEN];
	char name[CONFIG_CTAG_GW_NAME_MAX];
};

enum gw_cfg_kind { GW_CFGOP_NONE = 0, GW_CFGOP_CONFIGURE, GW_CFGOP_REMOVE };

struct gw_cfgop {
	uint8_t kind;
	uint8_t step;
	uint8_t attempts;
	uint8_t ttl;
	bool relay;
	bool waiting;   /* reply awaited until deadline */
	bool lane_busy; /* AppKey Add handed to the lane, end not yet reported */
	uint16_t addr;
	uint64_t op_id;
	int64_t deadline;
	int64_t retry_at;
};

enum gw_op_kind { GW_OP_NONE = 0, GW_OP_ASSIGN, GW_OP_UNASSIGN, GW_OP_TAG_CMD };

struct gw_op {
	uint8_t kind;
	uint8_t cmd;
	uint8_t sends;  /* requests sent (ASSIGN_SET/DEL) */
	bool waiting;   /* ASSIGN_STATUS awaited until deadline */
	bool lane_busy; /* in the segmented lane: the slot is not reused until its end */
	uint16_t bridge;
	uint32_t tag_id;
	uint32_t epoch;
	uint64_t op_id;
	int64_t deadline;
	uint8_t key[CTAG_TAG_KEY_LEN];
};

struct gw_tag_seen {
	uint16_t bridge;
	uint32_t tag_id;
	int64_t last;
};

/* A bridge's caps map: flags, proto, board, flash_size, max_tags, assigned_count. */
#define GW_CAPS_FIELDS 6u

/* Encoder workspace for LIST_NODES, GET_INVENTORY and EVT_BRIDGE_INFO (one
 * encoding at a time; kept off the thread stack). */
struct gw_scratch {
	struct ctag_cbor_field item[GW_NODES][12];
	struct ctag_cbor_field caps[GW_NODES][GW_CAPS_FIELDS];
	struct ctag_cbor_counter health[GW_NODES][8];
	struct ctag_cbor_map items[GW_NODES];
	struct ctag_cbor_field as_f[CONFIG_CTAG_GW_ASSIGN_MAX][2];
	struct ctag_cbor_map as_maps[CONFIG_CTAG_GW_ASSIGN_MAX];
	char fw[GW_NODES][GW_FW_STR];
};

struct gw_counters {
	uint32_t frames_rx, frames_tx, unexpected_frames, overruns, credit_violations;
	uint32_t unsupported, invalid, internal_errors, hellos;
	uint32_t events_dropped, events_discarded, duplicate_ops, busy;
	uint32_t deliveries_accepted, results, duplicate_results, repeated_results, stale_status;
	uint32_t mesh_send_retries, mesh_send_failures, mesh_busy, chunks_resent, commit_resends;
	uint32_t unexpected_mesh, unseg_dropped, provisions, tag_seen_limited, beacons;
	uint32_t reboots;
};

struct gw_core {
	const struct gw_backend *be;
	struct gw_info info;
	int64_t boot_ms;
	int64_t now;
	struct gw_counters c;
	struct gw_serial s;
	struct gw_lane lane;
	struct gw_unseg_q unseg;
	/* deliveries: records here, layouts in the arena */
	struct gw_delivery d[CONFIG_CTAG_GW_DELIVERY_QUEUE + 1];
	struct gw_ring arena;
	uint8_t arena_buf[CONFIG_CTAG_GW_LAYOUT_ARENA] __attribute__((aligned(4)));
	struct gw_xfer x;
	uint32_t d_order;
	uint16_t xfer_id;
	struct gw_at_bridge ab[CONFIG_CTAG_GW_AT_BRIDGE];
	uint32_t seen_results[CONFIG_CTAG_GW_RESULT_DEDUP]; /* bridge << 16 | result_seq, +1 */
	uint16_t seen_next;
	uint64_t reported[CONFIG_CTAG_GW_REPORTED]; /* update_ids with an EVT_RESULT */
	uint16_t reported_next, reported_count;
	/* network */
	struct gw_node nodes[GW_NODES];
	struct gw_assign assign[CONFIG_CTAG_GW_ASSIGN_MAX];
	uint8_t assign_count;
	struct gw_prov prov;
	struct gw_cfgop cfg;
	struct gw_op ops[CONFIG_CTAG_GW_MESH_OPS];
	/* SCAN_UNPROV */
	int64_t scan_until;
	uint8_t scan_filter[GW_UUID_LEN];
	uint8_t scan_filter_len;
	uint8_t scan_seen_count;
	uint8_t scan_seen[GW_SCAN_SEEN][GW_UUID_LEN];
	struct gw_tag_seen tag_seen[GW_TAG_SEEN_SLOTS];
	struct gw_scratch scratch;
};

/* ---- API (one thread) ---- */

void gw_core_init(struct gw_core *g, const struct gw_backend *be, const struct gw_info *info,
		  int64_t now);

/* Boot: nodes found in the CDB, then the stored names and assignments. */
void gw_core_add_node(struct gw_core *g, uint16_t addr, const uint8_t uuid[GW_UUID_LEN],
		      uint8_t elements, bool configured);
void gw_core_set_name(struct gw_core *g, uint16_t addr, const char *name, size_t len);
void gw_core_set_assignments(struct gw_core *g, const struct gw_assign *a, size_t n);
/* Boot done: ask every configured bridge for CAPS and HEALTH. */
void gw_core_start(struct gw_core *g, int64_t now);

/* Serial bytes from the host. */
void gw_core_rx(struct gw_core *g, const uint8_t *data, size_t len, int64_t now);

/* Mesh events. */
void gw_core_mesh_rx(struct gw_core *g, uint16_t src, uint8_t op, const uint8_t *params,
		     size_t len, int64_t now);
void gw_core_send_end(struct gw_core *g, uint32_t tag, int err, int64_t now);
void gw_core_cfg_status(struct gw_core *g, uint16_t addr, uint8_t step, uint8_t status,
			uint8_t value, int64_t now);
void gw_core_beacon(struct gw_core *g, const uint8_t uuid[GW_UUID_LEN], uint16_t oob, int8_t rssi,
		    int64_t now);
void gw_core_prov_link_open(struct gw_core *g, int64_t now);
void gw_core_prov_added(struct gw_core *g, const uint8_t uuid[GW_UUID_LEN], uint16_t addr,
			uint8_t elements, int64_t now);
void gw_core_prov_closed(struct gw_core *g, int64_t now);

/* Timers and the transmit pump; call after any of the above and whenever
 * the serial driver has room again. */
void gw_core_poll(struct gw_core *g, int64_t now);
/* Earliest absolute time gw_core_poll() must run again (INT64_MAX = none). */
int64_t gw_core_next_deadline(const struct gw_core *g);

/* Counters for INFO / GET_COUNTERS (core + backend); returns the count. */
size_t gw_core_counters(const struct gw_core *g, struct ctag_cbor_counter *items, size_t max);

/* ---- Internal interfaces between the core's modules ---- */

/* gw_serial.c */
void gw_serial_init(struct gw_core *g);
void gw_serial_pump(struct gw_core *g);
bool gw_emit(struct gw_core *g, uint8_t type, const struct ctag_cbor_field *f, size_t n,
	     bool retained);
uint32_t gw_retained_count(const struct gw_core *g);
int64_t gw_serial_deadline(const struct gw_core *g);
void gw_serial_timers(struct gw_core *g);

/* gw_lane.c */
void gw_lane_init(struct gw_core *g);
void gw_lane_request(struct gw_core *g, uint8_t requester);
void gw_lane_withdraw(struct gw_core *g, uint8_t requester);
/* Withdraw a requester still waiting for the lane; false = its send is in
 * flight (the owner hears its end later). */
bool gw_lane_cancel(struct gw_core *g, uint8_t requester);
void gw_lane_end(struct gw_core *g, uint32_t tag, int err);
/* An owner's lane_go handed its message to the stack with this result. */
void gw_lane_issued(struct gw_core *g, int err);
void gw_lane_timers(struct gw_core *g);
int64_t gw_lane_deadline(const struct gw_core *g);
bool gw_unseg_send(struct gw_core *g, uint16_t dst, uint8_t op, const uint8_t *params, size_t len);
bool gw_is_retryable(int err);

/* gw_delivery.c */
struct gw_req_result {
	uint8_t status;
	const char *text;
};
struct gw_req_result gw_deliver(struct gw_core *g, uint64_t op_id, uint16_t bridge,
				uint32_t tag_id, uint32_t epoch, uint32_t revision,
				uint64_t update_id, const uint8_t *fontpack_id, const uint8_t *layout,
				size_t len);
struct gw_req_result gw_cancel(struct gw_core *g, uint64_t update_id);
void gw_delivery_lane_go(struct gw_core *g);
void gw_delivery_lane_done(struct gw_core *g, bool ok);
void gw_delivery_status(struct gw_core *g, uint16_t src, const struct ctag_mesh_layout_status *st);
void gw_delivery_result(struct gw_core *g, uint16_t src, const struct ctag_mesh_delivery_result *r);
void gw_delivery_timers(struct gw_core *g);
int64_t gw_delivery_deadline(const struct gw_core *g);
void gw_delivery_pump(struct gw_core *g);
void gw_result_event(struct gw_core *g, uint64_t update_id, uint16_t bridge, uint32_t tag_id,
		     uint32_t epoch, uint32_t revision, uint8_t status, uint32_t mesh_ms);
uint8_t gw_queue_depth(const struct gw_core *g);

/* gw_nodes.c */
struct gw_node *gw_node_get(struct gw_core *g, uint16_t addr);
uint8_t gw_node_count(const struct gw_core *g);
struct gw_req_result gw_node_usable(struct gw_core *g, uint16_t addr);
struct gw_req_result gw_provision(struct gw_core *g, uint64_t op_id, const uint8_t *uuid,
				  const char *name, size_t name_len);
struct gw_req_result gw_configure(struct gw_core *g, uint64_t op_id, uint16_t addr, bool relay,
				  uint32_t ttl);
struct gw_req_result gw_remove(struct gw_core *g, uint64_t op_id, uint16_t addr);
struct gw_req_result gw_identify(struct gw_core *g, uint16_t addr);
struct gw_req_result gw_mesh_op(struct gw_core *g, uint8_t kind, uint64_t op_id, uint16_t bridge,
				uint32_t tag_id, uint32_t epoch, const uint8_t *key, uint8_t cmd);
void gw_scan(struct gw_core *g, uint32_t duration_s, const uint8_t *filter, size_t filter_len);
void gw_refresh_inventory(struct gw_core *g);
void gw_nodes_mesh_rx(struct gw_core *g, uint16_t src, uint8_t op, const uint8_t *p, size_t len);
void gw_cfg_lane_go(struct gw_core *g);
void gw_cfg_lane_done(struct gw_core *g, bool ok);
void gw_op_lane_go(struct gw_core *g, uint8_t slot);
void gw_op_lane_done(struct gw_core *g, uint8_t slot, bool ok);
void gw_nodes_timers(struct gw_core *g);
int64_t gw_nodes_deadline(const struct gw_core *g);
/* Encoders for LIST_NODES / GET_INVENTORY answers. */
int gw_encode_nodes(struct gw_core *g, uint8_t *buf, size_t size);
int gw_encode_inventory(struct gw_core *g, uint8_t *buf, size_t size);

/* Seconds since boot, and since a timestamp. */
static inline uint32_t gw_uptime_s(const struct gw_core *g)
{
	return (uint32_t)((g->now - g->boot_ms) / 1000);
}

static inline int64_t gw_min_deadline(int64_t a, int64_t b)
{
	return a < b ? a : b;
}

#define GW_NEVER INT64_MAX

/* CBOR field initialisers. */
#define GW_F_UINT(k, val) ((struct ctag_cbor_field){.key = (k), .kind = CTAG_CBOR_UINT, .v.u = (val)})
#define GW_F_INT(k, val)  ((struct ctag_cbor_field){.key = (k), .kind = CTAG_CBOR_INT, .v.i = (val)})
#define GW_F_BOOL(k, val) ((struct ctag_cbor_field){.key = (k), .kind = CTAG_CBOR_BOOL, .v.b = (val)})
#define GW_F_BSTR(k, p, n)                                                                         \
	((struct ctag_cbor_field){.key = (k), .kind = CTAG_CBOR_BSTR, .v.str = {(p), (n)}})
#define GW_F_TSTR(k, p, n)                                                                         \
	((struct ctag_cbor_field){.key = (k),                                                      \
				  .kind = CTAG_CBOR_TSTR,                                          \
				  .v.str = {(const uint8_t *)(p), (n)}})
#define GW_F_MAP(k, f, n)                                                                          \
	((struct ctag_cbor_field){.key = (k), .kind = CTAG_CBOR_MAP, .v.map = {(f), (n)}})

#endif /* GW_CORE_H_ */
