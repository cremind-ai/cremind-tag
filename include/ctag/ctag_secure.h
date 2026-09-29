/*
 * Protocol v2 secure layer (docs/connect-setup.md 2-5, docs/firmware-libs.md
 * "ctag_secure"): device identity, the key schedule, canonical grants and the
 * device-side grant rules, the ownership record, secure-message framing,
 * tunnel fragments and the responder-side secure endpoint that every v2
 * device runs behind IDENTIFY / IDENT, SECURE_OPEN and SECURE_DATA (or a
 * tunnel). Mirrors Cremind's app/tags/runtime/secure/{identity,grants,noise,messages,device}.py;
 * protocol/fixtures/v2_secure.json pins the behaviour.
 *
 * Primitives come from the vendored HACL* (lib/third_party/hacl) and the
 * Noise_IK_25519_ChaChaPoly_SHA256 handshake and transport from the vendored
 * Noise* (lib/third_party/noise_ik). Noise* allocates: its KaRaMeL allocator
 * is a bounded heap owned by this library (CONFIG_CTAG_SECURE_HEAP_SIZE); an
 * allocation that fails aborts only the Noise* call in progress (the heap is
 * reset and every Noise object dropped), never the device.
 *
 * Single-threaded: one thread uses the library at a time.
 */
#ifndef CTAG_SECURE_H_
#define CTAG_SECURE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_ids.h>
#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CTAG_SECURE_KEY_LEN    32u /* X25519 / Ed25519 keys, root, mk, k_setup */
#define CTAG_SECURE_HASH_LEN   32u /* SHA-256, the handshake hash h */
#define CTAG_SECURE_TAG_LEN    16u /* ChaCha20-Poly1305 tag */
#define CTAG_SECURE_MSG1_LEN   96u /* Noise IK message 1, empty payload */
#define CTAG_SECURE_MSG2_LEN   48u /* Noise IK message 2, empty payload */
#define CTAG_SECURE_HEADER_LEN 4u  /* type u8 | flags u8 | request_id u16le */
/* A sealed secure message is its plaintext plus this. */
#define CTAG_SECURE_OVERHEAD   CTAG_SECURE_TAG_LEN

/* ---- Primitives (HACL*) ---- */

void ctag_secure_sha256(const uint8_t *data, size_t len, uint8_t out[CTAG_SECURE_HASH_LEN]);
void ctag_secure_hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *data, size_t len,
			     uint8_t out[CTAG_SECURE_HASH_LEN]);
/* RFC 5869 HKDF-SHA256 with 1 <= len <= 32 and info_len <= 63. 0 or -EINVAL. */
int ctag_secure_hkdf(const uint8_t *ikm, size_t ikm_len, const uint8_t *salt, size_t salt_len,
		     const uint8_t *info, size_t info_len, uint8_t *out, size_t len);
void ctag_secure_x25519_public(const uint8_t priv[32], uint8_t pub[32]);
/* Ed25519 over "cremind-tag/v2/grant" | grant (grants.signed_message). */
bool ctag_secure_grant_sig_ok(const uint8_t authority_pub[32], const uint8_t *grant, size_t len,
			      const uint8_t sig[CTAG_GRANT_SIG_LEN]);
/* Test and factory helpers (the devices never sign). */
void ctag_secure_ed25519_public(const uint8_t sk[32], uint8_t pub[32]);
void ctag_secure_grant_sign(const uint8_t sk[32], const uint8_t *grant, size_t len,
			    uint8_t sig[CTAG_GRANT_SIG_LEN]);

/* Constant time in len. */
bool ctag_secure_equal(const void *a, const void *b, size_t len);
/* Zeroes that the compiler keeps. */
void ctag_secure_wipe(void *p, size_t len);

/* ---- Identity and key schedule (identity.py; connect-setup.md 2.1, 3.3-3.6) ---- */

/* SHA-256("cremind-tag/v2/device-id" | role | ik_pub)[0:16] */
void ctag_secure_device_id(uint8_t role, const uint8_t ik_pub[32],
			   uint8_t out[CTAG_DEVICE_ID_LEN]);
/* u32le(device_id[0:4]), ^ 0x5A5A5A5A when 0 or 0xFFFFFFFF (a v2 tag's tag_id) */
uint32_t ctag_secure_short_id(const uint8_t device_id[CTAG_DEVICE_ID_LEN]);
/* SHA-256(authority_pub)[0:16] */
void ctag_secure_authority_id(const uint8_t authority_pub[32], uint8_t out[16]);
void ctag_secure_k_setup(const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			 const uint8_t device_id[CTAG_DEVICE_ID_LEN], uint8_t out[32]);
void ctag_secure_static_oob(const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			    const uint8_t device_id[CTAG_DEVICE_ID_LEN],
			    uint8_t out[CTAG_STATIC_OOB_LEN]);
void ctag_secure_k_epoch(const uint8_t root[CTAG_OP_KEY_LEN], uint32_t tag_id, uint32_t epoch,
			 uint8_t out[CTAG_TAG_KEY_LEN]);
void ctag_secure_proof_s(const uint8_t k_setup[32], const uint8_t h[32], const uint8_t *grant,
			 size_t grant_len, uint8_t out[CTAG_PROOF_LEN]);
void ctag_secure_proof_d(const uint8_t k_setup[32], const uint8_t h[32],
			 const uint8_t proof_s[CTAG_PROOF_LEN], uint8_t out[CTAG_PROOF_LEN]);
void ctag_secure_root_proof(const uint8_t root[CTAG_OP_KEY_LEN], const uint8_t h[32],
			    uint8_t out[CTAG_PROOF_LEN]);
void ctag_secure_maint_proof(const uint8_t mk[CTAG_OP_KEY_LEN], const uint8_t h[32],
			     uint8_t out[CTAG_PROOF_LEN]);
/* (0x20 | role) | short_id u32le | secret (codes.py SetupPayload.pack) */
void ctag_secure_setup_payload(uint8_t role, uint32_t short_id,
			       const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			       uint8_t out[CTAG_SETUP_PAYLOAD_LEN]);

/* ---- Grants (grants.py; connect-setup.md 3.1) ---- */

#define CTAG_GRANT_VERSION 2u

struct ctag_grant {
	uint8_t op;   /* enum ctag_grant_op */
	uint8_t role; /* enum ctag_node_role */
	uint32_t gen_from;
	uint32_t gen_to;
	uint8_t device_id[CTAG_DEVICE_ID_LEN];
	uint8_t authority_pub[CTAG_AUTHORITY_KEY_LEN];
	uint8_t owner[CTAG_OWNER_LEN];
	uint8_t controller[CTAG_IDENTITY_KEY_LEN];
	uint8_t challenge[CTAG_CHALLENGE_LEN];
};

/* Canonical CBOR {0: 2, 1: op, ..., 9: challenge}; returns the length or -EINVAL/-EMSGSIZE. */
int ctag_grant_encode(const struct ctag_grant *g, uint8_t *buf, size_t size);
/* Strict decode (Grant.decode): exactly the canonical encoding of a v2 grant
 * with a known op and role, 1..GRANT_MAX bytes. 0 or -EBADMSG. */
int ctag_grant_decode(const uint8_t *raw, size_t len, struct ctag_grant *g);

/* What the device has pinned and what the carrying message allows (check_grant). */
struct ctag_grant_ctx {
	uint8_t role;
	uint8_t state; /* enum ctag_owner_state */
	uint32_t gen;
	const uint8_t *device_id;     /* 16 */
	const uint8_t *authority_pub; /* 32, when owned */
	const uint8_t *owner;         /* 16, when owned */
	const uint8_t *challenge;     /* 16, NULL = none drawn */
	const uint8_t *session_controller; /* 32: the Noise initiator's static key */
	uint8_t ops;                  /* bit (1 << op) per op the message carries */
	int8_t setup_proof_ok;        /* -1 not given, 0 false, 1 true */
};

#define CTAG_GRANT_OP_BIT(op) ((uint8_t)(1u << (op)))

/*
 * The device rules of connect-setup.md 3.1 in the order of check_grant():
 * returns CTAG_STATUS_OK, GRANT_INVALID, STALE_GENERATION, NOT_OWNER or
 * PROOF_FAILED. *g (optional) receives the decoded grant; *decoded tells
 * whether it was (check_grant's grant is not None).
 */
uint8_t ctag_grant_check(const struct ctag_grant_ctx *c, const uint8_t *grant, size_t len,
			 const uint8_t *sig, size_t sig_len, struct ctag_grant *g, bool *decoded);

/* ---- Ownership record (connect-setup.md 4.1) ---- */

/*
 * One layout for every role (the fields a role does not use stay zero):
 *   version u8 (1) | state u8 | flags u8 | reserved u8 | gen u32le |
 *   authority_pub[32] | owner[16] | controller[32] | op_key[32] |
 *   override_secret[10] | pending_override[10] | pending_controller[32] |
 *   crc32 u32le over all previous bytes
 * flags: bit0 locked, bit1 op_key, bit2 override_secret, bit3
 * pending_override, bit4 pending_controller present.
 */
#define CTAG_OWNER_RECORD_VERSION 1u
#define CTAG_OWNER_RECORD_LEN     176u

struct ctag_owner_record {
	uint8_t state; /* enum ctag_owner_state */
	bool locked;   /* released bridge: pairing refused until recommissioned */
	bool has_op_key;
	bool has_override;
	bool has_pending_override;
	bool has_pending_controller;
	uint32_t gen;
	uint8_t authority_pub[CTAG_AUTHORITY_KEY_LEN];
	uint8_t owner[CTAG_OWNER_LEN];
	uint8_t controller[CTAG_IDENTITY_KEY_LEN];
	uint8_t op_key[CTAG_OP_KEY_LEN];                     /* tag root / bridge mk */
	uint8_t override_secret[CTAG_SETUP_SECRET_LEN];      /* RELEASED: the only secret PAIR accepts */
	uint8_t pending_override[CTAG_SETUP_SECRET_LEN];     /* tag RELEASE stage 0 */
	uint8_t pending_controller[CTAG_IDENTITY_KEY_LEN];
};

void ctag_owner_record_encode(const struct ctag_owner_record *r,
			      uint8_t out[CTAG_OWNER_RECORD_LEN]);
/* 0, or -EBADMSG for a wrong length, version, CRC, state or flag bit. */
int ctag_owner_record_decode(struct ctag_owner_record *r, const uint8_t *in, size_t len);
/*
 * The boot rule: a stored record (NULL / len 0 = none) that does not decode is
 * UNOWNED with the generation floor; a good one keeps max(gen, floor), so
 * corruption can never rewind a generation. Returns true if the record was good.
 */
bool ctag_owner_record_load(struct ctag_owner_record *r, const uint8_t *in, size_t len,
			    uint32_t gen_floor);

/* ---- Secure messages and tunnel fragments (messages.py; connect-setup.md 3.2, 6) ---- */

struct ctag_secure_hdr {
	uint8_t type;  /* enum ctag_serial_msg */
	uint8_t flags; /* enum ctag_serial_flag */
	uint16_t request_id;
};

void ctag_secure_hdr_pack(const struct ctag_secure_hdr *h, uint8_t out[CTAG_SECURE_HEADER_LEN]);
/* 0, or -EBADMSG when shorter than the header. */
int ctag_secure_hdr_unpack(struct ctag_secure_hdr *h, const uint8_t *in, size_t len);

#define CTAG_TUNNEL_FRAG_START 0x01u
#define CTAG_TUNNEL_FRAG_END   0x02u
#define CTAG_TUNNEL_FRAG_CLOSE 0x80u /* TUNNEL_UP: data = status u8 */

/*
 * The fragment of a len-byte tunnel message that starts at byte off
 * (messages.fragments: TUNNEL_DATA_MAX bytes each, seq from 0, START on the
 * first, END on the last): returns its length with seq and flags filled, 0
 * when off is past the end, -EINVAL for an empty or overlong message or an
 * off between fragments.
 */
int ctag_tunnel_frag(size_t len, size_t off, uint8_t *seq, uint8_t *flags);

/* Reassembler (messages.Reassembler): in-order fragments, a gap drops the message. */
struct ctag_tunnel_rx {
	uint8_t *buf;
	uint16_t limit;
	uint16_t len;
	uint8_t next;
	bool active;
};

void ctag_tunnel_rx_init(struct ctag_tunnel_rx *rx, uint8_t *buf, uint16_t limit);
/* 1 = a message of *msg_len bytes is complete in buf, 0 = more to come,
 * -EBADMSG = a gap or an overlong message (the partial message is dropped). */
int ctag_tunnel_rx_feed(struct ctag_tunnel_rx *rx, uint8_t seq, uint8_t flags,
			const uint8_t *data, size_t len, size_t *msg_len);

/* ---- Noise_IK_25519_ChaChaPoly_SHA256 (Noise*) ---- */

/* One handshake and its transport keys; zero-initialise before first use. */
struct ctag_noise {
	void *dv;          /* Noise_IK_device_t * (keeps the static key and prologue) */
	void *sn;          /* Noise_IK_session_t * */
	uint8_t *pt;       /* the last unsealed plaintext, in the secure heap */
	size_t pt_len;
	uint32_t pid;      /* the session's peer */
	uint32_t epoch;    /* heap epoch the pointers belong to */
	uint8_t prologue[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN];
	uint8_t prologue_len;
};

/* prologue = "cremind-tag/v2" | link u8 | device_id; returns its length (31). */
size_t ctag_noise_prologue(uint8_t link, const uint8_t device_id[CTAG_DEVICE_ID_LEN],
			   uint8_t out[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN]);

/*
 * Responder: read message 1, answer message 2 (both with an empty payload).
 * rs = the initiator's static key, h = the handshake hash. Any previous
 * session is dropped first. 0; -EBADMSG handshake refused; -ENOMEM heap;
 * -EIO no randomness.
 */
int ctag_noise_accept(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t *prologue,
		      size_t prologue_len, const uint8_t *msg1, size_t len,
		      uint8_t msg2[CTAG_SECURE_MSG2_LEN], uint8_t rs[32], uint8_t h[32]);
/* Initiator (tests and tools): message 1 toward rs, then read message 2. */
int ctag_noise_connect(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t rs[32],
		       const uint8_t *prologue, size_t prologue_len,
		       uint8_t msg1[CTAG_SECURE_MSG1_LEN]);
int ctag_noise_finish(struct ctag_noise *nz, const uint8_t *msg2, size_t len, uint8_t h[32]);
bool ctag_noise_open(const struct ctag_noise *nz);
/*
 * Transport. seal: out receives len + 16 bytes (out may be pt); returns that
 * length, -ENOTCONN, -EMSGSIZE or -ENOMEM. unseal: *pt points into the secure
 * heap until ctag_noise_unseal_done(); -EBADMSG closes the session.
 */
int ctag_noise_seal(struct ctag_noise *nz, const uint8_t *pt, size_t len, uint8_t *out,
		    size_t size);
int ctag_noise_unseal(struct ctag_noise *nz, const uint8_t *ct, size_t len, const uint8_t **pt,
		      size_t *pt_len);
void ctag_noise_unseal_done(struct ctag_noise *nz);
/* Drop the session (the device object with its prologue stays). */
void ctag_noise_close(struct ctag_noise *nz);
/* Drop everything. */
void ctag_noise_free(struct ctag_noise *nz);

/* Randomness for Noise* ephemerals: the provider (default: sys_csrand_get()
 * under Zephyr, none on the host) and a one-shot test injection. */
typedef int (*ctag_secure_rng_fn)(void *ctx, uint8_t *buf, size_t len);
void ctag_secure_rng_set(ctag_secure_rng_fn fn, void *ctx);
void ctag_secure_test_ephemeral(const uint8_t e[32]);

/* Heap statistics (bytes, including allocator overhead where known). */
struct ctag_secure_heap_stats {
	size_t size;
	size_t used;
	size_t peak;
	uint32_t failures; /* aborted Noise* calls */
};

void ctag_secure_heap_stats(struct ctag_secure_heap_stats *s);
void ctag_secure_heap_reset_peak(void);
/* Tests: make every allocation fail after `after` more succeed (-1 = never). */
void ctag_secure_heap_fail_after(int32_t after);

/* ---- The secure endpoint (device.py SecureDevice) ---- */

/* Factory state that never changes. */
struct ctag_secure_keys {
	uint8_t role;
	uint8_t board;
	uint8_t fw[3];
	bool has_factory_secret;
	uint32_t short_id;
	uint8_t ik_priv[CTAG_SECURE_KEY_LEN];
	uint8_t ik_pub[CTAG_IDENTITY_KEY_LEN];
	uint8_t device_id[CTAG_DEVICE_ID_LEN];
	uint8_t factory_secret[CTAG_SETUP_SECRET_LEN];
};

/* Derive ik_pub, device_id and short_id from the identity key. */
void ctag_secure_keys_init(struct ctag_secure_keys *k, uint8_t role, const uint8_t ik_priv[32],
			   const uint8_t *factory_secret, uint8_t board, uint8_t fw_major,
			   uint8_t fw_minor, uint8_t fw_patch);

struct ctag_secure_ops {
	/* Store the new record before it takes effect (connect-setup.md 4.1:
	 * raise the generation floor, then write the record): 0 = stored. */
	int (*persist)(void *ctx, const struct ctag_owner_record *r);
	/* Cryptographically secure random bytes: 0 = ok. */
	int (*random)(void *ctx, uint8_t *buf, size_t len);
	void *ctx;
};

/* The decoded fields of a secure request (NULL = absent). */
struct ctag_secure_req {
	const uint8_t *grant;
	size_t grant_len;
	const uint8_t *sig;
	size_t sig_len;
	const uint8_t *proof;
	size_t proof_len;
	const uint8_t *op_key;
	size_t op_key_len;
	bool has_release_stage;
	uint32_t release_stage;
};

/* Answer fields present (ctag_secure_answer.fields). */
enum ctag_secure_field {
	CTAG_SECURE_F_GEN = 0x01,
	CTAG_SECURE_F_PROOF = 0x02,
	CTAG_SECURE_F_DATA = 0x04,        /* data (a setup payload, or empty) */
	CTAG_SECURE_F_STATE = 0x08,       /* owner_state, controller_match, challenge */
	CTAG_SECURE_F_OWNED = 0x10,       /* authority_id */
	CTAG_SECURE_F_ROOT_PROOF = 0x20,
	CTAG_SECURE_F_OWNER = 0x40,       /* owner: only to the pinned controller */
};

/* device.py Outcome: the status, the answer's fields and the side effects. */
struct ctag_secure_answer {
	uint8_t status;
	uint8_t fields;
	uint8_t owner_state;
	bool controller_match;
	uint8_t data_len;
	bool released;       /* gateway: wipe mesh + assignments; bridge: leave the mesh */
	bool recommissioned; /* bridge: leave the mesh, fresh secret armed */
	bool rekeyed;        /* tag: every K_epoch of the old root is dead */
	uint32_t gen;
	uint8_t authority_id[16];
	uint8_t owner[CTAG_OWNER_LEN];
	uint8_t challenge[CTAG_CHALLENGE_LEN];
	uint8_t proof[CTAG_PROOF_LEN];
	uint8_t root_proof[CTAG_PROOF_LEN];
	uint8_t data[CTAG_SETUP_PAYLOAD_LEN];
};

struct ctag_secure_ep {
	const struct ctag_secure_keys *keys;
	struct ctag_secure_ops ops;
	struct ctag_owner_record rec;
	bool has_challenge;
	bool session;
	bool maint_ok;
	uint8_t link;
	uint8_t failures; /* consecutive failed setup / maintenance proofs */
	uint32_t session_serial; /* changes whenever a session opens or closes */
	uint8_t challenge[CTAG_CHALLENGE_LEN];
	uint8_t controller[CTAG_IDENTITY_KEY_LEN]; /* the session's initiator */
	uint8_t h[CTAG_SECURE_HASH_LEN];
	struct ctag_noise noise;
};

void ctag_secure_ep_init(struct ctag_secure_ep *ep, const struct ctag_secure_keys *keys,
			 const struct ctag_owner_record *rec, const struct ctag_secure_ops *ops);
/* A fresh single-use challenge into ep->challenge: 0 or -EIO. */
int ctag_secure_draw_challenge(struct ctag_secure_ep *ep);
/* authority_id of the pinned authority, zeros unless owned. */
void ctag_secure_ep_authority_id(const struct ctag_secure_ep *ep, uint8_t out[16]);
/* The IDENT value (tags, tunnel OPEN data); draws a fresh challenge. 0 or -EIO. */
int ctag_secure_ident2(struct ctag_secure_ep *ep, struct ctag_ident2 *out);

/* SECURE_OPEN / PAIR handshake (replaces any session). As ctag_noise_accept(). */
int ctag_secure_open(struct ctag_secure_ep *ep, uint8_t link, const uint8_t *msg1, size_t len,
		     uint8_t msg2[CTAG_SECURE_MSG2_LEN]);
void ctag_secure_close(struct ctag_secure_ep *ep);
/* The session's controller is the pinned one (and the device is owned). */
bool ctag_secure_controller_match(const struct ctag_secure_ep *ep);
/* Transport over the session (ctag_noise_seal/unseal); a failure closes it. */
int ctag_secure_seal(struct ctag_secure_ep *ep, const uint8_t *pt, size_t len, uint8_t *out,
		     size_t size);
int ctag_secure_unseal(struct ctag_secure_ep *ep, const uint8_t *ct, size_t len,
		       const uint8_t **pt, size_t *pt_len);
void ctag_secure_unseal_done(struct ctag_secure_ep *ep);

/*
 * One secure message (STATUS, CLAIM, RECOVER, RELEASE, PAIR, REKEY,
 * MAINT_AUTH, RECOMMISSION) with the rules of device.py; without a session
 * AUTH_REQUIRED, other types UNSUPPORTED. The record is persisted through
 * ops.persist before the answer depends on it (STORAGE_ERROR if that fails).
 */
void ctag_secure_handle(struct ctag_secure_ep *ep, uint8_t type, const struct ctag_secure_req *req,
			struct ctag_secure_answer *ans);
/* Canonical CBOR of {status, **fields} (cbor_msgs.encode_response); length or -EMSGSIZE. */
int ctag_secure_answer_encode(const struct ctag_secure_answer *ans, uint8_t *buf, size_t size);
/* Three failed proofs in a row: a tag skips its next wake window. */
bool ctag_secure_pairing_paused(const struct ctag_secure_ep *ep);
/* K_epoch v2 from the committed root; -ENOENT without one. */
int ctag_secure_ep_k_epoch(const struct ctag_secure_ep *ep, uint32_t tag_id, uint32_t epoch,
			   uint8_t out[CTAG_TAG_KEY_LEN]);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_SECURE_H_ */
