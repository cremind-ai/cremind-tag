/* Mocked backend and host for the gateway core tests. */
#include <errno.h>
#include <string.h>

#include <psa/crypto.h>

#include "common.h"

struct gw_core core;
int64_t now_ms;
struct mock mock;

static uint8_t capture[256 * 1024];
static size_t cap_len, cap_read;
static struct ctag_serial_rx host_rx;
static uint8_t host_rxbuf[CTAG_SERIAL_MAX_FRAME];
static uint16_t next_rid;
static size_t send_cursor;

struct frame rx_frames[64];
size_t rx_count;

#ifdef CONFIG_CTAG_GW_SECURE
/* Keys of the tests: any 32 bytes are an X25519 or Ed25519 private key. */
const uint8_t test_auth_sk[32] = {0xA1, 0x07, 0x7E, 0x5A, 0x11, 0x42, 0x99, 0x03, 0x08,
				  0x1C, 0x2D, 0x3E, 0x4F, 0x50, 0x61, 0x72, 0x83, 0x94,
				  0xA5, 0xB6, 0xC7, 0xD8, 0xE9, 0xFA, 0x0B, 0x1C, 0x2D,
				  0x3E, 0x4F, 0x50, 0x61, 0x72};
const uint8_t test_owner[16] = {0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47,
				0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F};
const uint8_t test_worker[32] = {0x31, 0x48, 0x0A, 0x12, 0xF5, 0x5D, 0x51, 0x7B, 0xB4, 0x40, 0xF4,
				 0xAF, 0xDD, 0x90, 0xE4, 0x33, 0x9B, 0xA5, 0xDE, 0x8A, 0x05, 0xD8,
				 0x21, 0xEE, 0x97, 0x28, 0xFB, 0xAD, 0xB4, 0x42, 0xF1, 0xE2};
const uint8_t test_other[32] = {0xBD, 0x15, 0xA9, 0x69, 0x80, 0x0D, 0xCC, 0xF2, 0xFA, 0x75, 0xA5,
				0x41, 0x89, 0x1C, 0xF2, 0x48, 0x41, 0xC2, 0x47, 0xEB, 0x17, 0xE1,
				0x3E, 0x75, 0x5A, 0xE6, 0xF5, 0x35, 0xFF, 0x4D, 0x3C, 0x23};
static const uint8_t test_ik[32] = {0xD6, 0x62, 0xC9, 0x6F, 0x56, 0x19, 0x1D, 0x4B, 0x0F, 0x77, 0xB4,
				    0x8B, 0x0D, 0xA1, 0x0C, 0xDC, 0x02, 0x7A, 0x1B, 0xD7, 0x8D, 0x78,
				    0x54, 0xF0, 0x2F, 0xFD, 0x58, 0x14, 0x23, 0xBE, 0x5B, 0x10};

/* The host side of the session (the worker). */
static struct {
	bool open;
	bool pending; /* message 1 sent, message 2 awaited */
	const uint8_t *priv;
	uint8_t pub[32];
	uint8_t h[32];
	struct ctag_noise nz;
} hs;

static uint32_t rng_counter;

static int test_rng(void *ctx, uint8_t *buf, size_t len)
{
	(void)ctx;
	while (len > 0u) {
		uint8_t seed[8] = {'g', 'w', 't', 's'};
		uint8_t d[32];
		size_t n = len < sizeof(d) ? len : sizeof(d);

		ctag_put_le32(&seed[4], rng_counter++);
		ctag_secure_sha256(seed, sizeof(seed), d);
		memcpy(buf, d, n);
		buf += n;
		len -= n;
	}
	return 0;
}
#endif

/* ---- Backend ---- */

static size_t be_write(void *ctx, const uint8_t *data, size_t len)
{
	(void)ctx;
	if (len > mock.tx_room) {
		len = mock.tx_room;
	}
	zassert_true(cap_len + len <= sizeof(capture), "capture full");
	memcpy(&capture[cap_len], data, len);
	cap_len += len;
	return len;
}

static void be_reboot(void *ctx)
{
	(void)ctx;
	mock.reboots++;
}

static int next_rc(void)
{
	int rc = 0;

	if (mock.send_rc_n > 0u) {
		rc = mock.send_rc[0];
		memmove(&mock.send_rc[0], &mock.send_rc[1], (mock.send_rc_n - 1u) * sizeof(int));
		mock.send_rc_n--;
	}
	return rc;
}

/* docs/protocol.md 11.3: nothing reaches the mesh stack while it is suspended. */
static void assert_mesh_running(void)
{
#ifdef CONFIG_CTAG_GW_RADIO
	zassert_false(mock.suspended, "a mesh operation while the mesh is suspended");
#endif
}

static int be_send(void *ctx, uint16_t dst, uint8_t op, const uint8_t *params, size_t len,
		   uint32_t tag)
{
	struct sent *s;
	int rc = next_rc();

	(void)ctx;
	assert_mesh_running();
	zassert_true(mock.n_sent < ARRAY_SIZE(mock.sent), "send log full");
	zassert_true(len <= CTAG_MESH_MAX_VENDOR_PARAMS, "params too long");
	if (rc != 0) {
		return rc; /* refused: not recorded */
	}
	s = &mock.sent[mock.n_sent++];
	s->dst = dst;
	s->op = op;
	s->len = (uint8_t)len;
	s->tag = tag;
	if (len > 0u) {
		memcpy(s->data, params, len);
	}
	return 0;
}

static int be_cfg(void *ctx, uint16_t addr, uint8_t step, uint8_t arg, uint32_t tag)
{
	(void)ctx;
	assert_mesh_running();
	zassert_true(mock.n_cfg < ARRAY_SIZE(mock.cfg), "cfg log full");
	if (mock.cfg_rc != 0) {
		return mock.cfg_rc;
	}
	mock.cfg[mock.n_cfg++] = (struct cfg_call){addr, step, arg, tag};
	return 0;
}

static int be_provision(void *ctx, const uint8_t uuid[16], const uint8_t *static_oob)
{
	size_t i = mock.n_provision % 8;

	(void)ctx;
	assert_mesh_running();
	if (mock.provision_rc != 0) {
		return mock.provision_rc;
	}
	memcpy(mock.provisioned[i], uuid, 16);
	mock.provision_oob_set[i] = static_oob != NULL;
	if (static_oob != NULL) {
		memcpy(mock.provision_oob[i], static_oob, 32);
	}
	mock.n_provision++;
	return 0;
}

static void be_configured(void *ctx, uint16_t addr)
{
	(void)ctx;
	mock.configured[mock.n_configured++ % 8] = addr;
}

static void be_delete(void *ctx, uint16_t addr)
{
	(void)ctx;
	mock.deleted[mock.n_deleted++ % 8] = addr;
}

static void be_name(void *ctx, uint16_t addr, const char *name, size_t len)
{
	(void)ctx;
	(void)addr;
	memcpy(mock.stored_name, name, len);
	mock.stored_name_len = len;
}

static void be_assign(void *ctx, const struct gw_assign *a, size_t n)
{
	(void)ctx;
	mock.assign_stores++;
	memcpy(mock.stored_assign, a, n * sizeof(*a));
	mock.stored_assign_n = n;
}

static int be_sha256(void *ctx, const uint8_t *data, size_t len, uint8_t out[32])
{
	size_t olen;

	(void)ctx;
	return psa_hash_compute(PSA_ALG_SHA_256, data, len, out, 32, &olen) == PSA_SUCCESS ? 0 : -EIO;
}

static size_t be_counters(void *ctx, struct ctag_cbor_counter *items, size_t max)
{
	(void)ctx;
	if (max == 0u) {
		return 0u;
	}
	items[0] = (struct ctag_cbor_counter)CTAG_CBOR_COUNTER("uart_rx_overflow", 0u);
	return 1u;
}

#ifdef CONFIG_CTAG_GW_SECURE
static int be_store_owner(void *ctx, const uint8_t rec[CTAG_OWNER_RECORD_LEN], uint32_t gen)
{
	(void)ctx;
	if (mock.owner_fail) {
		return -EIO;
	}
	if (gen > mock.stored_floor) {
		mock.stored_floor = gen;
	}
	memcpy(mock.stored_owner, rec, CTAG_OWNER_RECORD_LEN);
	mock.owner_writes++;
	return 0;
}

static int be_random(void *ctx, uint8_t *buf, size_t len)
{
	return mock.random_fail ? -EIO : test_rng(ctx, buf, len);
}

static void be_release(void *ctx)
{
	(void)ctx;
	mock.releases++;
}
#endif

#ifdef CONFIG_CTAG_GW_RADIO
static void be_listen(void *ctx, bool on)
{
	(void)ctx;
	zassert_true(on != mock.listening, "radio_listen() only on a change");
	mock.listening = on;
	mock.listen_calls++;
}

static int be_suspend(void *ctx)
{
	(void)ctx;
	mock.suspends++;
	if (mock.suspend_rc == 0 || mock.suspend_rc == -EALREADY) {
		mock.suspended = true;
	}
	return mock.suspend_rc;
}

static int be_resume(void *ctx)
{
	(void)ctx;
	mock.resumes++;
	if (mock.resume_rc == 0 || mock.resume_rc == -EALREADY) {
		mock.suspended = false;
	}
	return mock.resume_rc;
}

static int be_connect(void *ctx, uint8_t link, const struct sched_peer *peer, uint32_t timeout_ms)
{
	(void)ctx;
	zassert_true(mock.suspended, "5.2: connecting only while the mesh is suspended");
	zassert_true(link < CONFIG_CTAG_GW_TAG_LINKS);
	mock.connects++;
	mock.connect_link = link;
	mock.connect_peer = *peer;
	mock.connect_timeout = timeout_ms;
	return mock.connect_rc;
}

static int be_disconnect(void *ctx, uint8_t link)
{
	(void)ctx;
	zassert_true(link < CONFIG_CTAG_GW_TAG_LINKS);
	mock.disconnects++;
	mock.disconnect_link = link;
	return 0;
}

static int be_setup(void *ctx, uint8_t link, uint8_t mode)
{
	(void)ctx;
	zassert_false(mock.suspended, "5.2 step 8: GATT only after the resume");
	mock.setups++;
	mock.setup_link = link;
	mock.setup_mode = mode;
	return mock.setup_rc;
}

static int be_link_write(void *ctx, uint8_t link, uint8_t chr, const uint8_t *value, size_t len)
{
	int rc = 0;

	(void)ctx;
	if (mock.write_rc_n > 0u) {
		rc = mock.write_rc[0];
		memmove(&mock.write_rc[0], &mock.write_rc[1], (mock.write_rc_n - 1u) * sizeof(int));
		mock.write_rc_n--;
	}
	if (rc != 0) {
		return rc; /* refused: not recorded */
	}
	zassert_true(mock.n_writes < ARRAY_SIZE(mock.writes), "write log full");
	zassert_true(len >= 2u && len <= CTAG_ATT_VALUE_MAX, "fragment of %u bytes", (unsigned)len);
	mock.writes[mock.n_writes].link = link;
	mock.writes[mock.n_writes].chr = chr;
	mock.writes[mock.n_writes].len = (uint8_t)len;
	memcpy(mock.writes[mock.n_writes].data, value, len);
	mock.n_writes++;
	return 0;
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
	.store_name = be_name,
	.store_assignments = be_assign,
	.sha256 = be_sha256,
	.counters = be_counters,
#ifdef CONFIG_CTAG_GW_SECURE
	.store_owner = be_store_owner,
	.random = be_random,
	.release = be_release,
#endif
#ifdef CONFIG_CTAG_GW_RADIO
	.radio_listen = be_listen,
	.radio_suspend = be_suspend,
	.radio_resume = be_resume,
	.link_connect = be_connect,
	.link_disconnect = be_disconnect,
	.link_setup = be_setup,
	.link_write = be_link_write,
#endif
};

/* ---- Core ---- */

static const uint8_t uuid_a[16] = {0xA0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};
static const uint8_t uuid_b[16] = {0xB0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};

static void core_init_common(void)
{
	static const struct gw_info info = {
		.fw = "0.1.0", .build = "test", .board = 1, .boot_id = BOOT_ID};

	zassert_equal(psa_crypto_init(), PSA_SUCCESS);
#ifdef CONFIG_CTAG_GW_SECURE
	/* The previous test's gateway session and device object, and the host's
	 * initiator, go back to the secure heap: nothing may be left over. */
	ctag_secure_close(&core.v2.ep);
	ctag_noise_free(&core.v2.ep.noise);
	ctag_noise_free(&hs.nz);
	{
		struct ctag_secure_heap_stats st;

		ctag_secure_heap_stats(&st);
		zassert_equal(st.used, 0u, "secure heap: %u bytes leaked", (unsigned int)st.used);
	}
#endif
	memset(&mock, 0, sizeof(mock));
	mock.tx_room = SIZE_MAX;
	cap_len = cap_read = 0u;
	ctag_serial_rx_init(&host_rx, host_rxbuf, sizeof(host_rxbuf));
	rx_count = 0u;
	next_rid = 0u;
	send_cursor = 0u;
	now_ms = 1000;
	gw_core_init(&core, &backend, &info, now_ms);
#ifdef CONFIG_CTAG_GW_SECURE
	rng_counter = 0u;
	ctag_secure_rng_set(test_rng, NULL);
	ctag_secure_heap_fail_after(-1);
	host_forget();
	host_use_worker(test_worker);
#endif
}

static void add_nodes(void)
{
	gw_core_add_node(&core, BRIDGE_A, uuid_a, 1u, true);
	gw_core_add_node(&core, BRIDGE_B, uuid_b, 1u, false);
	gw_core_set_name(&core, BRIDGE_A, "hall", 4u);
}

void core_reset(void)
{
	core_init_common();
#ifdef CONFIG_CTAG_GW_SECURE
	{
		/* Owned by the test authority at generation 1, the test worker pinned. */
		struct ctag_owner_record r = {.state = CTAG_OWNER_OWNED, .gen = 1u};
		uint8_t raw[CTAG_OWNER_RECORD_LEN];

		ctag_secure_ed25519_public(test_auth_sk, r.authority_pub);
		memcpy(r.owner, test_owner, sizeof(r.owner));
		ctag_secure_x25519_public(test_worker, r.controller);
		ctag_owner_record_encode(&r, raw);
		gw_core_secure_init(&core, test_ik, raw, sizeof(raw), 1u);
		mock.stored_floor = 1u;
	}
#endif
	add_nodes();
}

#ifdef CONFIG_CTAG_GW_SECURE
void core_reset_unowned(void)
{
	core_init_common();
	gw_core_secure_init(&core, test_ik, NULL, 0u, 0u);
	add_nodes();
}

void core_reset_record(const uint8_t *rec, size_t len, uint32_t floor)
{
	core_init_common();
	gw_core_secure_init(&core, test_ik, rec, len, floor);
	mock.stored_floor = floor;
	add_nodes();
}

void host_use_worker(const uint8_t priv[32])
{
	hs.priv = priv;
	ctag_secure_x25519_public(priv, hs.pub);
}

void host_forget(void)
{
	hs.open = false;
	hs.pending = false;
}

bool host_sealed(void)
{
	return hs.open;
}

static uint8_t open_session(uint8_t credits, int32_t fail_after)
{
	uint8_t prologue[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN];
	uint8_t msg1[CTAG_SECURE_MSG1_LEN];
	size_t plen = ctag_noise_prologue(CTAG_LINK_SERIAL, core.v2.keys.device_id, prologue);
	struct ctag_cbor_field f;
	uint16_t rid;
	const struct frame *r;

	host_forget();
	zassert_ok(ctag_noise_connect(&hs.nz, hs.priv, core.v2.keys.ik_pub, prologue, plen, msg1));
	f = GW_F_BSTR(CTAG_CBOR_KEY_DATA, msg1, sizeof(msg1));
	hs.pending = true;
	/* Only the gateway's allocations count from here (the heap is shared). */
	ctag_secure_heap_fail_after(fail_after);
	rid = host_send_plain(CTAG_SERIAL_MSG_SECURE_OPEN, &f, 1u, credits);
	ctag_secure_heap_fail_after(-1);
	host_read(); /* finishes the handshake when message 2 arrives */
	r = response(rid);
	zassert_not_null(r, "no SECURE_OPEN answer");
	hs.pending = false;
	return (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
}

uint8_t host_open(void)
{
	return open_session(60u, -1);
}

uint8_t host_open_credits(uint8_t credits)
{
	return open_session(credits, -1);
}

uint8_t host_open_fail_after(int32_t n)
{
	return open_session(60u, n);
}
#endif

void core_session(void)
{
	struct ctag_cbor_field f[2] = {
		GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_PROTO_VERSION),
		GW_F_TSTR(CTAG_CBOR_KEY_NAME, "test", 4u),
	};
	uint16_t rid;
	const struct frame *r;

#ifdef CONFIG_CTAG_GW_SECURE
	host_forget(); /* HELLO drops the session */
#endif
	rid = host_send(CTAG_SERIAL_MSG_HELLO, f, 2u, 60u);
	host_read();
	r = response(rid);
	zassert_not_null(r, "no HELLO answer");
	zassert_equal(field_u(r, CTAG_CBOR_KEY_STATUS), CTAG_STATUS_OK);
#ifdef CONFIG_CTAG_GW_SECURE
	zassert_equal(host_open(), CTAG_STATUS_OK, "SECURE_OPEN failed");
	zassert_true(hs.open);
#endif
}

void advance(int64_t ms)
{
	now_ms += ms;
	gw_core_poll(&core, now_ms);
}

size_t sent_count(uint8_t op)
{
	size_t n = 0u;

	for (size_t i = 0; i < mock.n_sent; i++) {
		n += mock.sent[i].op == op ? 1u : 0u;
	}
	return n;
}

const struct sent *last_sent(uint8_t op)
{
	for (size_t i = mock.n_sent; i > 0u; i--) {
		if (mock.sent[i - 1u].op == op) {
			return &mock.sent[i - 1u];
		}
	}
	return NULL;
}

const struct sent *sent_at(size_t i)
{
	return i < mock.n_sent ? &mock.sent[i] : NULL;
}

void end_tag(uint32_t tag, int err)
{
	gw_core_send_end(&core, tag, err, now_ms);
}

void end_last(int err)
{
	for (size_t i = mock.n_sent; i > 0u; i--) {
		if (mock.sent[i - 1u].tag != 0u) {
			end_tag(mock.sent[i - 1u].tag, err);
			return;
		}
	}
	zassert_unreachable("no tagged send");
}

/* ---- Host ---- */

static void send_frame(struct ctag_serial_header *h, uint8_t *frame, size_t size, int len)
{
	static uint8_t wire[CTAG_SERIAL_MAX_ENCODED];

	len = ctag_serial_frame_build(frame, size, h, NULL, (size_t)len);
	zassert_true(len > 0);
	len = ctag_serial_wire_encode(frame, (size_t)len, wire, sizeof(wire));
	zassert_true(len > 0);
	gw_core_rx(&core, wire, (size_t)len, now_ms);
}

uint16_t host_send_plain(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits)
{
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	struct ctag_serial_header h = {
		.version = CTAG_PROTO_VERSION, .type = type, .credits = credits};
	int len = n > 0u ? ctag_cbor_encode(f, n, &frame[CTAG_SERIAL_HEADER_LEN],
					    sizeof(frame) - CTAG_SERIAL_MIN_FRAME)
			 : 0;

	zassert_true(len >= 0, "encode failed: %d", len);
	next_rid = (uint16_t)(next_rid % 0xFFFFu + 1u);
	h.request_id = next_rid;
	send_frame(&h, frame, sizeof(frame), len);
	return h.request_id;
}

#ifdef CONFIG_CTAG_GW_SECURE
int host_seal(uint8_t type, uint8_t flags, uint16_t rid, const struct ctag_cbor_field *f, size_t n,
	      uint8_t *out, size_t size)
{
	static uint8_t inner[CTAG_SERIAL_MAX_FRAME];
	struct ctag_secure_hdr ih = {type, flags, rid};
	int len = n > 0u ? ctag_cbor_encode(f, n, &inner[CTAG_SECURE_HEADER_LEN],
					    sizeof(inner) - CTAG_SECURE_HEADER_LEN)
			 : 0;

	zassert_true(len >= 0, "encode failed: %d", len);
	ctag_secure_hdr_pack(&ih, inner);
	return ctag_noise_seal(&hs.nz, inner, CTAG_SECURE_HEADER_LEN + (size_t)len, out, size);
}

uint16_t host_send_secure_raw(const uint8_t *data, size_t len, uint8_t credits)
{
	static uint8_t frame[CTAG_SERIAL_MAX_FRAME];
	struct ctag_serial_header h = {.version = CTAG_PROTO_VERSION,
				       .type = CTAG_SERIAL_MSG_SECURE_DATA,
				       .credits = credits};
	struct ctag_cbor_field f = GW_F_BSTR(CTAG_CBOR_KEY_DATA, data, len);
	int n = ctag_cbor_encode(&f, 1u, &frame[CTAG_SERIAL_HEADER_LEN],
				 sizeof(frame) - CTAG_SERIAL_MIN_FRAME);

	zassert_true(n > 0);
	send_frame(&h, frame, sizeof(frame), n);
	return 0u;
}

static uint16_t host_send_sealed(uint8_t type, const struct ctag_cbor_field *f, size_t n,
				 uint8_t credits)
{
	static uint8_t ct[CTAG_SERIAL_MAX_FRAME];
	uint16_t rid;
	int len;

	next_rid = (uint16_t)(next_rid % 0xFFFFu + 1u);
	rid = next_rid;
	len = host_seal(type, 0u, rid, f, n, ct, sizeof(ct));
	zassert_true(len > 0, "seal failed: %d", len);
	(void)host_send_secure_raw(ct, (size_t)len, credits);
	return rid;
}
#endif

uint16_t host_send(uint8_t type, const struct ctag_cbor_field *f, size_t n, uint8_t credits)
{
#ifdef CONFIG_CTAG_GW_SECURE
	if (hs.open && type != CTAG_SERIAL_MSG_HELLO && type != CTAG_SERIAL_MSG_IDENTIFY &&
	    type != CTAG_SERIAL_MSG_SECURE_OPEN && type != CTAG_SERIAL_MSG_SECURE_DATA) {
		return host_send_sealed(type, f, n, credits);
	}
#endif
	return host_send_plain(type, f, n, credits);
}

void host_bytes(const uint8_t *data, size_t len)
{
	gw_core_rx(&core, data, len, now_ms);
}

size_t capture_peek(const uint8_t **p)
{
	*p = &capture[cap_read];
	return cap_len - cap_read;
}

#ifdef CONFIG_CTAG_GW_SECURE
/* A frame the gateway sent inside the session: replace it by the secure
 * message it carries (inner type, flags, request id; the outer credits). */
static void unseal_frame(struct frame *fr)
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_DATA};
	struct ctag_secure_hdr ih;
	const uint8_t *pt;
	size_t pt_len;

	zassert_ok(ctag_cbor_decode(fr->payload, fr->len, &f, 1u));
	zassert_true(f.present, "SECURE_DATA without data");
	zassert_ok(ctag_noise_unseal(&hs.nz, f.v.str.ptr, f.v.str.len, &pt, &pt_len),
		   "a sealed frame did not open");
	zassert_ok(ctag_secure_hdr_unpack(&ih, pt, pt_len));
	fr->h.type = ih.type;
	fr->h.flags = ih.flags;
	fr->h.request_id = ih.request_id;
	fr->len = (uint16_t)(pt_len - CTAG_SECURE_HEADER_LEN);
	fr->h.length = fr->len;
	memcpy(fr->payload, &pt[CTAG_SECURE_HEADER_LEN], fr->len);
	ctag_noise_unseal_done(&hs.nz);
}

/* The SECURE_OPEN answer: finish the host's handshake with message 2. */
static void finish_open(const struct frame *fr)
{
	struct ctag_cbor_field f[2] = {{.key = CTAG_CBOR_KEY_STATUS}, {.key = CTAG_CBOR_KEY_DATA}};

	zassert_ok(ctag_cbor_decode(fr->payload, fr->len, f, 2u));
	hs.pending = false;
	if (f[0].present && f[0].v.u == CTAG_STATUS_OK && f[1].present) {
		zassert_ok(ctag_noise_finish(&hs.nz, f[1].v.str.ptr, f[1].v.str.len, hs.h));
		hs.open = true;
	}
}
#endif

size_t host_read(void)
{
	struct ctag_serial_header h;
	size_t got = 0u;

	rx_count = 0u;
	for (; cap_read < cap_len; cap_read++) {
		if (ctag_serial_rx_put(&host_rx, capture[cap_read], &h)) {
			struct frame *fr = &rx_frames[rx_count];

			zassert_true(rx_count < ARRAY_SIZE(rx_frames), "too many frames");
			fr->h = h;
			fr->len = h.length;
			zassert_true(h.length <= sizeof(fr->payload));
			memcpy(fr->payload, &host_rxbuf[CTAG_SERIAL_HEADER_LEN], h.length);
#ifdef CONFIG_CTAG_GW_SECURE
			if (h.type == CTAG_SERIAL_MSG_SECURE_OPEN &&
			    (h.flags & CTAG_SERIAL_FLAG_RESPONSE) != 0u && hs.pending) {
				finish_open(fr);
			} else if (h.type == CTAG_SERIAL_MSG_SECURE_DATA && h.flags == 0u) {
				zassert_true(hs.open, "a sealed frame without a host session");
				zassert_equal(h.request_id, 0u, "outer request_id of SECURE_DATA");
				unseal_frame(fr);
			}
#endif
			rx_count++;
			got++;
		}
	}
	zassert_equal(host_rx.crc_errors + host_rx.len_errors + host_rx.version_errors, 0u,
		      "gateway sent a malformed frame");
	return got;
}

const struct frame *response(uint16_t rid)
{
	for (size_t i = 0; i < rx_count; i++) {
		if ((rx_frames[i].h.flags & CTAG_SERIAL_FLAG_RESPONSE) &&
		    rx_frames[i].h.request_id == rid) {
			return &rx_frames[i];
		}
	}
	return NULL;
}

const struct frame *event(uint8_t type, size_t idx)
{
	for (size_t i = 0; i < rx_count; i++) {
		if ((rx_frames[i].h.flags & CTAG_SERIAL_FLAG_EVENT) && rx_frames[i].h.type == type) {
			if (idx-- == 0u) {
				return &rx_frames[i];
			}
		}
	}
	return NULL;
}

size_t events_of(uint8_t type)
{
	size_t n = 0u;

	while (event(type, n) != NULL) {
		n++;
	}
	return n;
}

void decode(const struct frame *fr, struct ctag_cbor_field *f, size_t n)
{
	zassert_equal(ctag_cbor_decode(fr->payload, fr->len, f, n), 0, "undecodable payload");
}

uint64_t field_u(const struct frame *fr, uint8_t key)
{
	struct ctag_cbor_field f = {.key = key};

	decode(fr, &f, 1u);
	zassert_true(f.present, "key %u missing", key);
	return f.kind == CTAG_CBOR_INT ? (uint64_t)f.v.i : f.v.u;
}

bool field_has(const struct frame *fr, uint8_t key)
{
	struct ctag_cbor_field f = {.key = key};

	decode(fr, &f, 1u);
	return f.present;
}

uint32_t counter(const char *name)
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_COUNTERS};
	struct ctag_cbor_counter items[96];
	uint16_t rid = host_send(CTAG_SERIAL_MSG_GET_COUNTERS, NULL, 0u, 1u);
	const struct frame *r;
	int n;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no GET_COUNTERS answer");
	decode(r, &f, 1u);
	zassert_true(f.present);
	n = ctag_cbor_counters(&f.v.str, items, ARRAY_SIZE(items));
	zassert_true(n > 0);
	for (int i = 0; i < n; i++) {
		if (items[i].name_len == strlen(name) &&
		    memcmp(items[i].name, name, items[i].name_len) == 0) {
			return items[i].value;
		}
	}
	zassert_unreachable("no counter %s", name);
	return 0u;
}

uint8_t request_status(uint8_t type, const struct ctag_cbor_field *f, size_t n)
{
	uint16_t rid = host_send(type, f, n, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no answer to 0x%02x", type);
	return (uint8_t)field_u(r, CTAG_CBOR_KEY_STATUS);
}

#ifdef CONFIG_CTAG_GW_SECURE
void host_challenge(uint8_t out[16])
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_CHALLENGE};
	uint16_t rid = host_send(CTAG_SERIAL_MSG_STATUS, NULL, 0u, 1u);
	const struct frame *r;

	host_read();
	r = response(rid);
	zassert_not_null(r, "no STATUS answer");
	decode(r, &f, 1u);
	zassert_true(f.present && f.v.str.len == 16u);
	memcpy(out, f.v.str.ptr, 16u);
}

void host_grant(uint8_t op, const uint8_t controller[32], const uint8_t challenge[16],
		uint32_t gen_from, uint8_t *grant, size_t *len, uint8_t sig[64])
{
	struct ctag_grant g = {.op = op, .role = CTAG_NODE_ROLE_GATEWAY, .gen_from = gen_from,
			       .gen_to = gen_from + 1u};
	int n;

	memcpy(g.device_id, core.v2.keys.device_id, sizeof(g.device_id));
	ctag_secure_ed25519_public(test_auth_sk, g.authority_pub);
	memcpy(g.owner, test_owner, sizeof(g.owner));
	memcpy(g.controller, controller, sizeof(g.controller));
	memcpy(g.challenge, challenge, sizeof(g.challenge));
	n = ctag_grant_encode(&g, grant, CTAG_GRANT_MAX);
	zassert_true(n > 0);
	*len = (size_t)n;
	ctag_secure_grant_sign(test_auth_sk, grant, *len, sig);
}

uint8_t host_grant_request(uint8_t type, uint8_t op, uint32_t gen_from)
{
	uint8_t challenge[16], grant[CTAG_GRANT_MAX], sig[64];
	size_t len;

	host_challenge(challenge);
	host_grant(op, hs.pub, challenge, gen_from, grant, &len, sig);
	{
		struct ctag_cbor_field f[2] = {
			GW_F_BSTR(CTAG_CBOR_KEY_GRANT, grant, len),
			GW_F_BSTR(CTAG_CBOR_KEY_SIG, sig, 64u),
		};

		return request_status(type, f, 2u);
	}
}
#endif

uint16_t send_deliver(uint64_t op_id, uint16_t bridge, uint64_t update_id, const uint8_t *layout,
		      size_t len)
{
	static const uint8_t pack[8] = {1, 2, 3, 4, 5, 6, 7, 8};
	struct ctag_cbor_field f[8] = {
		GW_F_UINT(CTAG_CBOR_KEY_OP_ID, op_id),
		GW_F_UINT(CTAG_CBOR_KEY_BRIDGE, bridge),
		GW_F_UINT(CTAG_CBOR_KEY_TAG_ID, 0xCAFE0001u),
		GW_F_UINT(CTAG_CBOR_KEY_EPOCH, 3u),
		GW_F_UINT(CTAG_CBOR_KEY_REVISION, 18u),
		GW_F_UINT(CTAG_CBOR_KEY_UPDATE_ID, update_id),
		GW_F_BSTR(CTAG_CBOR_KEY_FONTPACK_ID, pack, 8u),
		GW_F_BSTR(CTAG_CBOR_KEY_LAYOUT, layout, len),
	};

	return host_send(CTAG_SERIAL_MSG_DELIVER_LAYOUT, f, 8u, 1u);
}

uint16_t send_ack(uint32_t seq)
{
	struct ctag_cbor_field f = GW_F_UINT(CTAG_CBOR_KEY_SEQ, seq);

	return host_send(CTAG_SERIAL_MSG_EVENT_ACK, &f, 1u, 1u);
}

/* ---- Mesh ---- */

void mesh_status(uint16_t src, uint16_t xfer_id, uint8_t status, uint32_t missing)
{
	struct ctag_mesh_layout_status st = {.xfer_id = xfer_id, .status = status, .missing = missing};
	uint8_t p[CTAG_MESH_LAYOUT_STATUS_LEN];

	(void)ctag_mesh_layout_status_pack(&st, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_LAYOUT_STATUS, p, sizeof(p), now_ms);
}

void mesh_result(uint16_t src, uint16_t result_seq, uint64_t update_id, uint8_t status)
{
	struct ctag_mesh_delivery_result r = {
		.result_seq = result_seq,
		.update_id = update_id,
		.tag_id = 0xCAFE0001u,
		.epoch = 3u,
		.revision = 18u,
		.status = status,
		.digest = {9, 8, 7, 6, 5, 4, 3, 2},
		.battery_mv = 2900u,
		.wake_ms = 11u,
		.suspend_ms = 22u,
		.transfer_ms = 33u,
		.refresh_ms = 44u,
		.stored_epoch = 3u,
		.flags = CTAG_RESULT_FLAG_DUPLICATE,
	};
	uint8_t p[CTAG_MESH_DELIVERY_RESULT_LEN];

	(void)ctag_mesh_delivery_result_pack(&r, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_DELIVERY_RESULT, p, sizeof(p), now_ms);
}

void mesh_result_msg(uint16_t src, const struct ctag_mesh_delivery_result *r)
{
	uint8_t p[CTAG_MESH_DELIVERY_RESULT_LEN];

	(void)ctag_mesh_delivery_result_pack(r, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_DELIVERY_RESULT, p, sizeof(p), now_ms);
}

void mesh_assign_status(uint16_t src, uint32_t tag_id, uint32_t epoch, uint8_t status)
{
	struct ctag_mesh_assign_status st = {.tag_id = tag_id, .epoch = epoch, .status = status};
	uint8_t p[CTAG_MESH_ASSIGN_STATUS_LEN];

	(void)ctag_mesh_assign_status_pack(&st, p, sizeof(p));
	gw_core_mesh_rx(&core, src, CTAG_MESH_OP_ASSIGN_STATUS, p, sizeof(p), now_ms);
}

uint16_t drive_to_commit(void)
{
	for (int guard = 0; guard < 400; guard++) {
		bool acted = false;

		while (send_cursor < mock.n_sent) {
			const struct sent *s = &mock.sent[send_cursor++];

			if (s->op == CTAG_MESH_OP_LAYOUT_COMMIT) {
				return ctag_get_le16(s->data);
			}
			if (s->tag != 0u) {
				end_tag(s->tag, 0);
				acted = true;
				break;
			}
		}
		if (!acted) {
			break;
		}
	}
	zassert_unreachable("the transfer never reached LAYOUT_COMMIT");
	return 0u;
}
