/*
 * Gateway interop build (native_sim): the real gateway core, gateway loop and
 * UART glue (apps/gateway/src) on a native PTY UART, with the mesh replaced by
 * a small simulated network. The companion's GatewayClient drives it through
 * the PTY (interop.py).
 *
 * The simulated network (sysworkq context; it only posts gw_evt events, as
 * the Bluetooth callbacks do on hardware):
 * - bridges 0x0002 and 0x0003, provisioned and configured, plus one
 *   unprovisioned device that beacons every second;
 * - every mesh send travels through a "radio" queue (10 ms); segmented sends
 *   report their end; a full queue refuses a send (-ENOBUFS);
 * - LAYOUT_BEGIN/CHUNK/COMMIT go through the library's bridge-side assembler
 *   (ctag_layout_asm: chunk sizes, digest), a repeated commit of an accepted
 *   transfer answers DUPLICATE, and an accepted layout produces DELIVERY_STAGE
 *   TRANSFERRING and REFRESHING and a DELIVERY_RESULT OK re-sent every
 *   500 ms until RESULT_ACK;
 * - tag_id 0xDEAD0001: chunk 1 of its first transfer is lost once
 *   (LAYOUT_STATUS INCOMPLETE, the gateway resends it);
 * - tag_id 0xDEAD0002: the LAYOUT_STATUS OK of its first transfer is lost
 *   and its result held for 11 s (the gateway re-commits after 10 s and the
 *   bridge answers DUPLICATE, protocol 10);
 * - every DELIVERY_RESULT reports the tag's stored epoch = the delivery's
 *   epoch; tag_id 0xDEAD0003's result is the tag's stored ACK (flags bit0);
 * - ASSIGN_SET/DEL, CAPS_GET, HEALTH_GET, TAG_CMD and the configuration
 *   client steps are answered; PB-ADV provisioning of the unprovisioned
 *   device assigns 0x0004;
 * - REBOOT re-initialises the core with a new boot_id (the PTY stays open, as
 *   a UART does); the CDB (nodes) survives.
 *
 * Protocol v2 (v2.conf, CONFIG_CTAG_GW_SECURE; interop_v2.py drives it):
 * - the gateway's identity key is fixed, its ownership record and generation
 *   floor live in RAM across simulated reboots; RELEASE empties the network
 *   (the next boot has no nodes);
 * - the unprovisioned device is a v2 bridge: its UUID is its device_id, it
 *   has a setup secret, and PB-ADV succeeds only with the static OOB derived
 *   from that secret (the capabilities offer static OOB, GW_EVT_PROV_AUTH;
 *   any other value: the link closes without the node, SECURITY_CONFIG);
 * - bridges answer CAPS_GET with CAPS_STATUS and CAPS2_STATUS, and DISCOVER
 *   with DISCOVERED, twice, for a v2 tag in setup mode (tag_id 0x13572468);
 * - bridge 0x0002 runs a real secure endpoint (lib/secure, role BRIDGE,
 *   factory secret "SIM-BRIDGE") behind TUNNEL_OPEN tag_id 0: its ident2
 *   goes up first, then kind|body messages (Noise handshake, transport,
 *   close) in TUNNEL_UP fragments; a tunnel to a tag is closed NOT_FOUND,
 *   any other (bridge 0x0003, a second one) BUSY.
 *   The system work queue and the gateway loop are both cooperative threads,
 *   so the two users of lib/secure never interleave inside a call.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/printk.h>

#include <psa/crypto.h>

#include <ctag/ctag_layout.h>
#ifdef CONFIG_CTAG_GW_SECURE
#include <ctag/ctag_cbor.h>
#include <ctag/ctag_secure.h>
#endif

#include "gw_app.h"

#define N_NODES     3
#define RADIO_MS    10
#define CFG_OP      0x80u /* radio items for configuration-client steps */
#define DROP_TAG_ID 0xDEAD0001u
#define LOST_OK_TAG_ID 0xDEAD0002u
#define STORED_ACK_TAG_ID 0xDEAD0003u

static struct gw_core core;

/* ---- SHA-256 through PSA (gateway digest and the assembler's check) ---- */

static psa_hash_operation_t sha_op;

static int sha_init(void *ctx)
{
	ARG_UNUSED(ctx);
	sha_op = psa_hash_operation_init();
	return psa_hash_setup(&sha_op, PSA_ALG_SHA_256) == PSA_SUCCESS ? 0 : -EIO;
}

static int sha_update(void *ctx, const uint8_t *data, size_t len)
{
	ARG_UNUSED(ctx);
	return psa_hash_update(&sha_op, data, len) == PSA_SUCCESS ? 0 : -EIO;
}

static int sha_finish(void *ctx, uint8_t digest[32])
{
	size_t n;

	ARG_UNUSED(ctx);
	return psa_hash_finish(&sha_op, digest, 32, &n) == PSA_SUCCESS ? 0 : -EIO;
}

static const struct ctag_sha256_ops sha_ops = {sha_init, sha_update, sha_finish, NULL};

static int be_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32])
{
	size_t n;

	ARG_UNUSED(ctx);
	return psa_hash_compute(PSA_ALG_SHA_256, data, len, out, 32, &n) == PSA_SUCCESS ? 0 : -EIO;
}

/* ---- The simulated network ---- */

#define N_PEND 16

/* A delivery the bridge works on: its stages, then its result, re-sent every
 * 500 ms until RESULT_ACK (MESH_RESULT_RETRIES sends at most). */
struct pend {
	bool used;
	bool stages;
	uint8_t sends;
	uint8_t next_stage;
	int64_t due;
	struct ctag_mesh_delivery_result res;
};

struct node {
	uint16_t addr; /* 0: unprovisioned */
	bool configured;
	uint8_t uuid[16];
	uint16_t result_seq;
	uint16_t accepted_xfer;
	bool accepted;
	bool dropped;
	bool lost_ok;
	/* Tags assigned on this bridge (CAPS_STATUS.assigned counts them). */
	uint32_t tags[8];
	uint8_t n_tags;
	struct ctag_layout_asm as;
	uint8_t buf[CTAG_LAYOUT_HARD_MAX];
	struct pend pend[N_PEND];
};

static struct node nodes[N_NODES];
static void tick_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(tick_work, tick_fn);

#ifdef CONFIG_CTAG_GW_SECURE
#define SIM_TAG_ID 0x13572468u
/* The v2 test fixtures (interop_v2.py uses the same bytes). */
static const uint8_t gw_ik[32] = {0x0A, 0x1B, 0x2C, 0x3D, 0x4E, 0x5F, 0x60, 0x71, 0x82, 0x93, 0xA4,
				  0xB5, 0xC6, 0xD7, 0xE8, 0xF9, 0x01, 0x12, 0x23, 0x34, 0x45, 0x56,
				  0x67, 0x78, 0x89, 0x9A, 0xAB, 0xBC, 0xCD, 0xDE, 0xEF, 0xF0};
static const uint8_t bridge_ik[32] = {0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB,
				      0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6,
				      0xC7, 0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF, 0xD0};
static const uint8_t bridge_secret[CTAG_SETUP_SECRET_LEN] = {0x53, 0x49, 0x4D, 0x2D, 0x42,
							     0x52, 0x49, 0x44, 0x47, 0x45};
static const uint8_t newbie_ik[32] = {0x77, 0x6E, 0x65, 0x77, 0x62, 0x69, 0x65, 0x2D, 0x69, 0x6B, 0x00,
				      0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B,
				      0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15};
static const uint8_t newbie_secret[CTAG_SETUP_SECRET_LEN] = {0x4E, 0x45, 0x57, 0x42, 0x49,
							     0x45, 0x2D, 0x53, 0x45, 0x43};

/* The gateway's persistent v2 records (settings on hardware). */
static uint8_t own_rec[CTAG_OWNER_RECORD_LEN];
static size_t own_len;
static uint32_t own_floor;
static bool network_wiped;

/* Bridge 0x0002's secure endpoint and its one tunnel. */
static struct ctag_secure_keys br_keys;
static struct ctag_secure_ep br_ep;
static struct ctag_owner_record br_rec_store;
static struct ctag_tunnel_rx br_rx;
static uint8_t br_rxbuf[CTAG_TUNNEL_MSG_MAX];
static uint16_t br_tunnel;
static uint8_t newbie_device_id[16];
#endif

struct radio_item {
	uint16_t dst;
	uint8_t op;
	uint8_t len;
	uint32_t tag;
	uint8_t data[CTAG_MESH_MAX_VENDOR_PARAMS];
};

K_MSGQ_DEFINE(radio_q, sizeof(struct radio_item), 32, 4);
static void radio_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(radio_work, radio_fn);

static struct node *node_at(uint16_t addr)
{
	for (int i = 0; i < N_NODES; i++) {
		if (addr != 0u && nodes[i].addr == addr) {
			return &nodes[i];
		}
	}
	return NULL;
}

static void post_mesh(uint16_t src, uint8_t op, const uint8_t *p, size_t len)
{
	struct gw_evt e = {.type = GW_EVT_MESH_RX, .op = op, .addr = src, .len = (uint8_t)len};

	memcpy(e.data, p, len);
	gw_post(&e);
}

static void post_end(uint32_t tag, int err)
{
	struct gw_evt e = {.type = GW_EVT_SEND_END, .tag = tag, .err = err};

	gw_post(&e);
}

static void post_cfg(uint16_t addr, uint8_t step, uint8_t status, uint8_t value)
{
	struct gw_evt e = {.type = GW_EVT_CFG_STATUS, .op = step, .addr = addr};

	e.data[0] = status;
	e.data[1] = value;
	gw_post(&e);
}

#ifdef CONFIG_CTAG_GW_SECURE
/* ---- v2: the bridge's secure endpoint behind a tunnel ---- */

/* One kind|body message up the tunnel in TUNNEL_UP fragments. */
static void tunnel_up_msg(uint16_t src, uint16_t tunnel, const uint8_t *msg, size_t len)
{
	size_t off = 0u;
	uint8_t seq, flags;
	int n;

	while ((n = ctag_tunnel_frag(len, off, &seq, &flags)) > 0) {
		struct ctag_mesh_tunnel_up m = {.tunnel = tunnel, .seq = seq, .flags = flags,
						.data = &msg[off], .data_len = (size_t)n};
		uint8_t p[CTAG_MESH_TUNNEL_UP_MAX_LEN];
		int k = ctag_mesh_tunnel_up_pack(&m, p, sizeof(p));

		if (k > 0) {
			post_mesh(src, CTAG_MESH_OP_TUNNEL_UP, p, (size_t)k);
		}
		off += (size_t)n;
	}
}

static void tunnel_close_up(uint16_t src, uint16_t tunnel, uint8_t status)
{
	struct ctag_mesh_tunnel_up m = {.tunnel = tunnel, .flags = CTAG_TUNNEL_FRAG_CLOSE,
					.data = &status, .data_len = 1u};
	uint8_t p[CTAG_MESH_TUNNEL_UP_MAX_LEN];
	int k = ctag_mesh_tunnel_up_pack(&m, p, sizeof(p));

	if (k > 0) {
		post_mesh(src, CTAG_MESH_OP_TUNNEL_UP, p, (size_t)k);
	}
}

static int br_persist(void *ctx, const struct ctag_owner_record *r)
{
	ARG_UNUSED(ctx);
	br_rec_store = *r;
	return 0;
}

static int sim_random(void *ctx, uint8_t *buf, size_t len)
{
	ARG_UNUSED(ctx);
	return sys_csrand_get(buf, len);
}

/* A secure message to the bridge endpoint: decoded with the serial CBOR
 * rules, handled, answered sealed. */
static void br_request(uint16_t src, const uint8_t *pt, size_t len)
{
	struct ctag_secure_hdr h, rh;
	struct ctag_cbor_field f[5] = {
		{.key = CTAG_CBOR_KEY_GRANT},  {.key = CTAG_CBOR_KEY_SIG},
		{.key = CTAG_CBOR_KEY_PROOF},  {.key = CTAG_CBOR_KEY_OP_KEY},
		{.key = CTAG_CBOR_KEY_RELEASE_STAGE},
	};
	struct ctag_secure_req req = {0};
	struct ctag_secure_answer ans = {.status = CTAG_STATUS_INVALID};
	static uint8_t out[1u + CTAG_SECURE_HEADER_LEN + 160u + CTAG_SECURE_TAG_LEN];
	int n;

	if (ctag_secure_hdr_unpack(&h, pt, len) != 0) {
		return;
	}
	if (ctag_cbor_decode(&pt[CTAG_SECURE_HEADER_LEN], len - CTAG_SECURE_HEADER_LEN, f, 5u) == 0) {
		if (f[0].present) {
			req.grant = f[0].v.str.ptr;
			req.grant_len = f[0].v.str.len;
		}
		if (f[1].present) {
			req.sig = f[1].v.str.ptr;
			req.sig_len = f[1].v.str.len;
		}
		if (f[2].present) {
			req.proof = f[2].v.str.ptr;
			req.proof_len = f[2].v.str.len;
		}
		if (f[3].present) {
			req.op_key = f[3].v.str.ptr;
			req.op_key_len = f[3].v.str.len;
		}
		if (f[4].present) {
			req.has_release_stage = true;
			req.release_stage = (uint32_t)f[4].v.u;
		}
		ctag_secure_handle(&br_ep, h.type, &req, &ans);
	}
	out[0] = CTAG_PAIR_KIND_TRANSPORT;
	rh = (struct ctag_secure_hdr){h.type, CTAG_SERIAL_FLAG_RESPONSE, h.request_id};
	ctag_secure_hdr_pack(&rh, &out[1]);
	n = ctag_secure_answer_encode(&ans, &out[1u + CTAG_SECURE_HEADER_LEN],
				      sizeof(out) - 1u - CTAG_SECURE_HEADER_LEN - CTAG_SECURE_TAG_LEN);
	if (n >= 0) {
		n = ctag_secure_seal(&br_ep, &out[1], CTAG_SECURE_HEADER_LEN + (size_t)n, &out[1],
				     sizeof(out) - 1u);
	}
	if (n > 0) {
		tunnel_up_msg(src, br_tunnel, out, 1u + (size_t)n);
	}
}

static void br_message(struct node *n, const uint8_t *msg, size_t len)
{
	if (len == 0u) {
		return;
	}
	switch (msg[0]) {
	case CTAG_PAIR_KIND_HANDSHAKE: {
		uint8_t out[1u + CTAG_SECURE_MSG2_LEN] = {CTAG_PAIR_KIND_HANDSHAKE};

		if (ctag_secure_open(&br_ep, CTAG_LINK_TUNNEL, &msg[1], len - 1u, &out[1]) == 0) {
			tunnel_up_msg(n->addr, br_tunnel, out, sizeof(out));
		} else {
			tunnel_close_up(n->addr, br_tunnel, CTAG_STATUS_AUTH_FAILED);
			br_tunnel = 0u;
		}
		break;
	}
	case CTAG_PAIR_KIND_TRANSPORT: {
		const uint8_t *pt;
		size_t pt_len;

		if (ctag_secure_unseal(&br_ep, &msg[1], len - 1u, &pt, &pt_len) == 0) {
			br_request(n->addr, pt, pt_len);
			ctag_secure_unseal_done(&br_ep);
		} else {
			tunnel_close_up(n->addr, br_tunnel, CTAG_STATUS_AUTH_FAILED);
			br_tunnel = 0u;
		}
		break;
	}
	default:
		ctag_secure_close(&br_ep);
		break;
	}
}

static void bridge_rx_v2(struct node *n, const struct radio_item *m)
{
	switch (m->op) {
	case CTAG_MESH_OP_DISCOVER: {
		struct ctag_mesh_discover d;
		struct ctag_mesh_discovered r = {.tag_id = SIM_TAG_ID, .rssi = -70,
						 .flags = CTAG_ADV_FLAG_SETUP};
		uint8_t p[CTAG_MESH_DISCOVERED_LEN];

		if (ctag_mesh_discover_unpack(&d, m->data, m->len) != 0 || d.duration_s == 0u ||
		    (d.tag_id != 0u && d.tag_id != SIM_TAG_ID)) {
			break;
		}
		(void)ctag_mesh_discovered_pack(&r, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_DISCOVERED, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_DISCOVERED, p, sizeof(p)); /* rate-limited */
		break;
	}
	case CTAG_MESH_OP_TUNNEL_OPEN: {
		struct ctag_mesh_tunnel_open o;
		struct ctag_ident2 id;
		uint8_t raw[CTAG_IDENT2_LEN];

		if (ctag_mesh_tunnel_open_unpack(&o, m->data, m->len) != 0) {
			break;
		}
		if (n->addr != 0x0002u || o.tag_id != 0u || br_tunnel != 0u ||
		    ctag_secure_ident2(&br_ep, &id) != 0) {
			tunnel_close_up(n->addr, o.tunnel,
					o.tag_id != 0u ? CTAG_STATUS_NOT_FOUND : CTAG_STATUS_BUSY);
			break;
		}
		br_tunnel = o.tunnel;
		ctag_tunnel_rx_init(&br_rx, br_rxbuf, sizeof(br_rxbuf));
		(void)ctag_ident2_pack(&id, raw, sizeof(raw));
		tunnel_up_msg(n->addr, br_tunnel, raw, sizeof(raw)); /* the first message: ident2 */
		break;
	}
	case CTAG_MESH_OP_TUNNEL_DATA: {
		struct ctag_mesh_tunnel_data d;
		size_t len;

		if (ctag_mesh_tunnel_data_unpack(&d, m->data, m->len) != 0 || d.tunnel != br_tunnel ||
		    br_tunnel == 0u) {
			break;
		}
		if (ctag_tunnel_rx_feed(&br_rx, d.seq, d.flags, d.data, d.data_len, &len) == 1) {
			br_message(n, br_rxbuf, len);
		}
		break;
	}
	case CTAG_MESH_OP_TUNNEL_CLOSE: {
		struct ctag_mesh_tunnel_close c;

		if (ctag_mesh_tunnel_close_unpack(&c, m->data, m->len) == 0 && c.tunnel == br_tunnel) {
			br_tunnel = 0u;
			ctag_secure_close(&br_ep);
		}
		break;
	}
	default:
		break;
	}
}
#endif

static void pend_step(struct node *n, struct pend *pd, int64_t now)
{
	if (pd->stages && pd->next_stage <= CTAG_STAGE_REFRESHING) {
		struct ctag_mesh_delivery_stage st = {
			.update_id = pd->res.update_id,
			.tag_id = pd->res.tag_id,
			.revision = pd->res.revision,
			.stage = pd->next_stage++,
		};
		uint8_t p[CTAG_MESH_DELIVERY_STAGE_LEN];

		(void)ctag_mesh_delivery_stage_pack(&st, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_DELIVERY_STAGE, p, sizeof(p));
		pd->due = now + (pd->next_stage > CTAG_STAGE_REFRESHING ? 200 : 50);
		return;
	}
	{
		uint8_t p[CTAG_MESH_DELIVERY_RESULT_LEN];

		(void)ctag_mesh_delivery_result_pack(&pd->res, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_DELIVERY_RESULT, p, sizeof(p));
	}
	pd->due = now + 500;
	if (++pd->sends >= CTAG_MESH_RESULT_RETRIES) {
		pd->used = false;
	}
}

static void tick_fn(struct k_work *w)
{
	int64_t now = k_uptime_get();

	ARG_UNUSED(w);
	for (int i = 0; i < N_NODES; i++) {
		for (int j = 0; j < N_PEND; j++) {
			struct pend *pd = &nodes[i].pend[j];

			if (pd->used && nodes[i].addr != 0u && now >= pd->due) {
				pend_step(&nodes[i], pd, now);
			}
		}
	}
	k_work_reschedule(&tick_work, K_MSEC(20));
}

static void result_later(struct node *n, uint64_t update_id, uint32_t tag_id, uint32_t epoch,
			 uint32_t revision, const uint8_t *digest, bool stages)
{
	struct pend *pd = NULL;

	for (int j = 0; j < N_PEND && pd == NULL; j++) {
		if (!n->pend[j].used) {
			pd = &n->pend[j];
		}
	}
	if (pd == NULL) {
		printk("gateway interop: bridge 0x%04x has too many pending results\n", n->addr);
		return;
	}
	*pd = (struct pend){.used = true, .stages = stages, .next_stage = CTAG_STAGE_TRANSFERRING,
			    .due = k_uptime_get() + (tag_id == LOST_OK_TAG_ID ? 11000 : 50)};
	pd->res = (struct ctag_mesh_delivery_result){
		.result_seq = ++n->result_seq,
		.update_id = update_id,
		.tag_id = tag_id,
		.epoch = epoch,
		.revision = revision,
		.status = CTAG_STATUS_OK,
		.battery_mv = 2950u,
		.wake_ms = 120u,
		.suspend_ms = 40u,
		.transfer_ms = 900u,
		.refresh_ms = 300u,
		.stored_epoch = epoch,
		.flags = tag_id == STORED_ACK_TAG_ID ? CTAG_RESULT_FLAG_DUPLICATE : 0u,
	};
	if (digest != NULL) {
		memcpy(pd->res.digest, digest, sizeof(pd->res.digest));
	}
}

static void layout_commit(struct node *n, uint16_t xfer_id)
{
	struct ctag_mesh_layout_status st = {.xfer_id = xfer_id};
	uint8_t p[CTAG_MESH_LAYOUT_STATUS_LEN];

	if (n->accepted && n->accepted_xfer == xfer_id) {
		st.status = CTAG_STATUS_DUPLICATE; /* its OK was lost (protocol 10) */
	} else {
		st.status = ctag_layout_asm_commit(&n->as, xfer_id, &st.missing, &sha_ops);
		if (st.status == CTAG_STATUS_OK) {
			uint8_t digest[32];

			n->accepted = true;
			n->accepted_xfer = xfer_id;
			(void)be_sha256(NULL, n->buf, n->as.begin.total_len, digest);
			result_later(n, n->as.begin.update_id, n->as.begin.tag_id, n->as.begin.epoch,
				     n->as.begin.revision, digest, true);
			if (n->as.begin.tag_id == LOST_OK_TAG_ID && !n->lost_ok) {
				n->lost_ok = true;
				return; /* this LAYOUT_STATUS OK never reaches the gateway */
			}
		}
	}
	(void)ctag_mesh_layout_status_pack(&st, p, sizeof(p));
	post_mesh(n->addr, CTAG_MESH_OP_LAYOUT_STATUS, p, sizeof(p));
}

static void bridge_rx(struct node *n, const struct radio_item *m)
{
	uint8_t p[CTAG_MESH_CAPS_STATUS_LEN];

	switch (m->op) {
	case CTAG_MESH_OP_LAYOUT_BEGIN: {
		struct ctag_mesh_layout_begin b;

		if (ctag_mesh_layout_begin_unpack(&b, m->data, m->len) == 0) {
			ctag_layout_asm_begin(&n->as, &b);
			n->accepted = false;
		}
		break;
	}
	case CTAG_MESH_OP_LAYOUT_CHUNK: {
		struct ctag_mesh_layout_chunk c;

		if (ctag_mesh_layout_chunk_unpack(&c, m->data, m->len) != 0) {
			break;
		}
		if (n->as.begin.tag_id == DROP_TAG_ID && c.index == 1u && !n->dropped) {
			n->dropped = true; /* lost at the access layer, segments were acked */
			break;
		}
		(void)ctag_layout_asm_chunk(&n->as, &c);
		break;
	}
	case CTAG_MESH_OP_LAYOUT_COMMIT:
		layout_commit(n, ctag_get_le16(m->data));
		break;
	case CTAG_MESH_OP_RESULT_ACK:
		for (int j = 0; j < N_PEND; j++) {
			if (n->pend[j].used && n->pend[j].sends > 0u &&
			    n->pend[j].res.result_seq == ctag_get_le16(m->data)) {
				n->pend[j].used = false;
			}
		}
		break;
	case CTAG_MESH_OP_ASSIGN_SET:
	case CTAG_MESH_OP_ASSIGN_DEL: {
		struct ctag_mesh_assign_status st = {.tag_id = ctag_get_le32(&m->data[0]),
						     .epoch = ctag_get_le32(&m->data[4]),
						     .status = CTAG_STATUS_OK};
		int k = 0;

		while (k < n->n_tags && n->tags[k] != st.tag_id) {
			k++;
		}
		if (m->op == CTAG_MESH_OP_ASSIGN_SET && k == n->n_tags &&
		    n->n_tags < ARRAY_SIZE(n->tags)) {
			n->tags[n->n_tags++] = st.tag_id;
		} else if (m->op == CTAG_MESH_OP_ASSIGN_DEL && k < n->n_tags) {
			n->tags[k] = n->tags[--n->n_tags];
		}
		(void)ctag_mesh_assign_status_pack(&st, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_ASSIGN_STATUS, p, CTAG_MESH_ASSIGN_STATUS_LEN);
		break;
	}
	case CTAG_MESH_OP_CAPS_GET: {
		struct ctag_mesh_caps_status c = {.proto = 1u, .fw_major = 0u, .fw_minor = 1u,
						  .fw_patch = 0u, .board = CTAG_BOARD_NRF52840_BRIDGE,
						  .fontpack_id = {0xF0, 1, 2, 3, 4, 5, 6, 7},
						  .flash_mib = 8u, .max_tags = 20u,
						  .assigned = n->n_tags, .flags = 1u};

		(void)ctag_mesh_caps_status_pack(&c, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_CAPS_STATUS, p, CTAG_MESH_CAPS_STATUS_LEN);
#ifdef CONFIG_CTAG_GW_SECURE
		{
			/* A v2 bridge adds its identity and ownership (connect-setup.md 6). */
			struct ctag_mesh_caps2_status c2 = {.gen = br_ep.rec.gen,
							    .owner_state = br_ep.rec.state};
			uint8_t p2[CTAG_MESH_CAPS2_STATUS_LEN];

			memcpy(c2.device_id, n->addr == 0x0002u ? br_keys.device_id : n->uuid, 16);
			(void)ctag_mesh_caps2_status_pack(&c2, p2, sizeof(p2));
			post_mesh(n->addr, CTAG_MESH_OP_CAPS2_STATUS, p2, sizeof(p2));
		}
#endif
		break;
	}
	case CTAG_MESH_OP_HEALTH_GET: {
		struct ctag_mesh_health_status h = {.uptime_s = (uint32_t)(k_uptime_get() / 1000),
						    .sessions_ok = 3u};

		(void)ctag_mesh_health_status_pack(&h, p, sizeof(p));
		post_mesh(n->addr, CTAG_MESH_OP_HEALTH_STATUS, p, CTAG_MESH_HEALTH_STATUS_LEN);
		break;
	}
	case CTAG_MESH_OP_TAG_CMD: {
		struct ctag_mesh_tag_cmd c;

		if (ctag_mesh_tag_cmd_unpack(&c, m->data, m->len) == 0) {
			result_later(n, c.update_id, c.tag_id, c.epoch, 0u, NULL, false);
		}
		break;
	}
	default: /* IDENTIFY, LAYOUT_CANCEL; v2: DISCOVER, TUNNEL_* */
#ifdef CONFIG_CTAG_GW_SECURE
		bridge_rx_v2(n, m);
#endif
		break;
	}
}

static void cfg_rx(struct node *n, const struct radio_item *m)
{
	uint8_t step = (uint8_t)(m->op & ~CFG_OP);
	uint8_t value = step == GW_CFG_RELAY || step == GW_CFG_TTL ? m->data[0] : 0u;

	post_cfg(n->addr, step, 0u, value);
	if (step == GW_CFG_NET_TX) {
		n->configured = true;
	} else if (step == GW_CFG_RESET) {
		n->addr = 0u; /* unprovisioned again: it beacons */
		n->configured = false;
	}
}

static void radio_fn(struct k_work *w)
{
	struct radio_item m;

	ARG_UNUSED(w);
	while (k_msgq_get(&radio_q, &m, K_NO_WAIT) == 0) {
		struct node *n = node_at(m.dst);

		if (n == NULL) {
			if (m.tag != 0u) {
				post_end(m.tag, -ETIMEDOUT); /* nobody acknowledged the segments */
			}
			continue;
		}
		if (m.tag != 0u) {
			post_end(m.tag, 0);
		}
		if ((m.op & CFG_OP) != 0u) {
			cfg_rx(n, &m);
		} else {
			bridge_rx(n, &m);
		}
	}
}

static int radio_send(uint16_t dst, uint8_t op, const uint8_t *params, size_t len, uint32_t tag)
{
	struct radio_item m = {.dst = dst, .op = op, .len = (uint8_t)len, .tag = tag};

	if (len > sizeof(m.data)) {
		return -EMSGSIZE;
	}
	if (len > 0u) {
		memcpy(m.data, params, len);
	}
	if (k_msgq_put(&radio_q, &m, K_NO_WAIT) != 0) {
		return -ENOBUFS; /* no advertising buffer */
	}
	(void)k_work_schedule(&radio_work, K_MSEC(RADIO_MS));
	return 0;
}

static int be_send(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
		   uint32_t tag)
{
	ARG_UNUSED(ctx);
	return radio_send(dst, op, params, len, tag);
}

static int be_cfg(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag)
{
	ARG_UNUSED(ctx);
	return radio_send(addr, (uint8_t)(CFG_OP | step), &arg, 1u, tag);
}

/* ---- Provisioning ---- */

static struct node *prov_node;
static void prov_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(prov_work, prov_fn);
static uint8_t prov_step;
static bool prov_bad_oob;

static void prov_fn(struct k_work *w)
{
	struct gw_evt e = {0};

	ARG_UNUSED(w);
	if (prov_node == NULL) {
		e.type = GW_EVT_PROV_CLOSED; /* nobody answered the link open */
		gw_post(&e);
		prov_step = 0u;
		return;
	}
	if (prov_step == 0u) {
		e.type = GW_EVT_PROV_OPEN;
		gw_post(&e);
#ifdef CONFIG_CTAG_GW_SECURE
		/* The capabilities offer static OOB (HMAC-SHA256): mesh.c sets the
		 * worker's value and the exchange begins. */
		e.type = GW_EVT_PROV_AUTH;
		gw_post(&e);
#endif
		prov_step = 1u;
		k_work_reschedule(&prov_work, K_MSEC(400));
		return;
	}
	if (prov_bad_oob) {
		/* The confirmation did not match: the device fails the link
		 * (Provisioning Failed), which the stack reports as a closed link. */
		e.type = GW_EVT_PROV_CLOSED;
		gw_post(&e);
		prov_node = NULL;
		prov_step = 0u;
		return;
	}
	prov_node->addr = 0x0004u;
	e.type = GW_EVT_PROV_ADDED;
	e.addr = prov_node->addr;
	e.u16 = 1u;
	memcpy(e.data, prov_node->uuid, 16);
	gw_post(&e);
	e.type = GW_EVT_PROV_CLOSED;
	gw_post(&e);
	prov_node = NULL;
	prov_step = 0u;
}

static int be_provision(void *ctx, const uint8_t uuid[16], const uint8_t *static_oob)
{
	ARG_UNUSED(ctx);
	if (k_work_delayable_is_pending(&prov_work)) {
		return -EBUSY;
	}
	prov_node = NULL;
	for (int i = 0; i < N_NODES; i++) {
		if (nodes[i].addr == 0u && memcmp(nodes[i].uuid, uuid, 16) == 0) {
			prov_node = &nodes[i];
		}
	}
	prov_bad_oob = false;
#ifdef CONFIG_CTAG_GW_SECURE
	{
		/* The v2 bridge authenticates with the static OOB of its label. */
		uint8_t expect[CTAG_STATIC_OOB_LEN];

		ctag_secure_static_oob(newbie_secret, newbie_device_id, expect);
		prov_bad_oob = static_oob == NULL || memcmp(expect, static_oob, sizeof(expect)) != 0;
	}
#else
	ARG_UNUSED(static_oob);
#endif
	prov_step = 0u;
	k_work_reschedule(&prov_work, K_MSEC(prov_node != NULL ? 50 : 300));
	return 0;
}

static void beacon_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(beacon_work, beacon_fn);

static void beacon_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	for (int i = 0; i < N_NODES; i++) {
		if (nodes[i].addr == 0u) {
			struct gw_evt e = {.type = GW_EVT_BEACON, .rssi = -55, .u16 = 0x0020u};

			memcpy(e.data, nodes[i].uuid, 16);
			gw_post(&e);
		}
	}
	k_work_reschedule(&beacon_work, K_MSEC(1000));
}

/* ---- Reboot: a new boot of the core, the CDB survives ---- */

static void boot_core(void);

static void reboot_fn(struct k_work *w)
{
	uint8_t junk[64];

	ARG_UNUSED(w);
	while (uart_io_read(junk, sizeof(junk)) > 0u) {
		/* bytes in flight are lost with the RAM */
	}
	boot_core();
	gw_core_start(&core, k_uptime_get());
	gw_wake();
}

static K_WORK_DELAYABLE_DEFINE(reboot_work, reboot_fn);

static void be_reboot(void *ctx)
{
	ARG_UNUSED(ctx);
	/* The loop is between steps when this runs (cooperative threads). */
	k_work_reschedule(&reboot_work, K_MSEC(50));
}

static size_t be_write(void *ctx, const uint8_t *data, size_t len)
{
	ARG_UNUSED(ctx);
	return uart_io_write(data, len);
}

static size_t be_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	size_t n = uart_io_counters(items, max);

	ARG_UNUSED(ctx);
	if (n < max) {
		items[n++] = (struct ctag_cbor_counter)CTAG_CBOR_COUNTER("evq_dropped",
									 gw_thread_dropped());
	}
	return n;
}

static void be_configured(void *ctx, uint16_t addr)
{
	ARG_UNUSED(ctx);
	ARG_UNUSED(addr);
}

static void be_delete(void *ctx, uint16_t addr)
{
	ARG_UNUSED(ctx);
	ARG_UNUSED(addr);
}

#ifdef CONFIG_CTAG_GW_SECURE
static int be_store_owner(void *ctx, const uint8_t rec[CTAG_OWNER_RECORD_LEN], uint32_t gen)
{
	ARG_UNUSED(ctx);
	if (gen > own_floor) {
		own_floor = gen;
	}
	memcpy(own_rec, rec, sizeof(own_rec));
	own_len = sizeof(own_rec);
	return 0;
}

static void be_release(void *ctx)
{
	ARG_UNUSED(ctx);
	network_wiped = true; /* the next boot starts without a network */
}
#endif

static const struct gw_backend backend = {
	.write = be_write,
	.reboot = be_reboot,
	.mesh_send = be_send,
	.mesh_cfg = be_cfg,
	.provision = be_provision,
	.node_configured = be_configured,
	.node_delete = be_delete,
	.sha256 = be_sha256,
	.counters = be_counters,
#ifdef CONFIG_CTAG_GW_SECURE
	.store_owner = be_store_owner,
	.random = sim_random,
	.release = be_release,
#endif
};

static void boot_core(void)
{
	struct gw_info info = {.fw = "0.1.0", .build = "interop", .board = CTAG_BOARD_NRF52840DK_GATEWAY};

	info.boot_id = sys_rand32_get();
	gw_core_init(&core, &backend, &info, k_uptime_get());
#ifdef CONFIG_CTAG_GW_SECURE
	gw_core_secure_init(&core, gw_ik, own_len > 0u ? own_rec : NULL, own_len, own_floor);
	if (network_wiped) {
		printk("gateway interop: boot_id %08x, no network\n", info.boot_id);
		return;
	}
#endif
	for (int i = 0; i < N_NODES; i++) {
		if (nodes[i].addr != 0u) {
			gw_core_add_node(&core, nodes[i].addr, nodes[i].uuid, 1u, nodes[i].configured);
		}
	}
	gw_core_set_name(&core, 0x0002u, "hall", 4u);
	printk("gateway interop: boot_id %08x\n", info.boot_id);
}

int main(void)
{
	static const uint8_t uuids[N_NODES][2] = {{0xB0, 0x02}, {0xB0, 0x03}, {0xC0, 0xFF}};
	int err = uart_io_init(DEVICE_DT_GET(DT_CHOSEN(cremind_gateway_uart)));

	if (err != 0 || psa_crypto_init() != PSA_SUCCESS) {
		printk("gateway interop: init failed (%d)\n", err);
		return 0;
	}
	for (int i = 0; i < N_NODES; i++) {
		struct node *n = &nodes[i];

		memset(n->uuid, 0x5A, sizeof(n->uuid));
		n->uuid[0] = uuids[i][0];
		n->uuid[1] = uuids[i][1];
		n->addr = i < 2 ? (uint16_t)(0x0002u + i) : 0u;
		n->configured = i < 2;
		ctag_layout_asm_init(&n->as, n->buf, sizeof(n->buf));
	}
#ifdef CONFIG_CTAG_GW_SECURE
	{
		struct ctag_secure_ops ops = {.persist = br_persist, .random = sim_random};
		uint8_t pub[32];

		ctag_secure_keys_init(&br_keys, CTAG_NODE_ROLE_BRIDGE, bridge_ik, bridge_secret,
				      CTAG_BOARD_NRF52840_BRIDGE, 0u, 2u, 0u);
		ctag_secure_ep_init(&br_ep, &br_keys, NULL, &ops);
		/* The unprovisioned device is a v2 bridge: UUID = device_id. */
		ctag_secure_x25519_public(newbie_ik, pub);
		ctag_secure_device_id(CTAG_NODE_ROLE_BRIDGE, pub, newbie_device_id);
		memcpy(nodes[2].uuid, newbie_device_id, 16);
	}
#endif
	k_work_reschedule(&beacon_work, K_MSEC(500));
	k_work_reschedule(&tick_work, K_MSEC(20));
	boot_core();
	gw_run(&core);
}
