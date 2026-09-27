/*
 * Bridge <-> tag session (docs/protocol.md 5.4-5.5): the handshake for both
 * roles and authenticated records, over ctag_crypto.h. Messages are the
 * reassembled CTRL messages including their type byte.
 *
 * th = SHA-256(CAPS | HELLO | CHALLENGE): both roles pass the CAPS
 * characteristic value, the bridge the exact bytes it read, the tag the bytes
 * it serves, so a relay that alters the geometry or the plane flags breaks
 * the handshake (AUTH_FAILED) instead of changing what the tag displays.
 */
#ifndef CTAG_SESSION_H_
#define CTAG_SESSION_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CTAG_SESSION_HELLO_LEN     (1u + CTAG_CTRL_HELLO_LEN)     /* 26 */
#define CTAG_SESSION_CHALLENGE_LEN (1u + CTAG_CTRL_CHALLENGE_LEN) /* 30 */
#define CTAG_SESSION_AUTH_LEN      (1u + CTAG_CTRL_AUTH_LEN)      /* 17 */
#define CTAG_SESSION_AUTH_OK_LEN   (1u + CTAG_CTRL_AUTH_OK_LEN)   /* 17 */
#define CTAG_SESSION_ERROR_LEN     (1u + CTAG_CTRL_ERROR_LEN)     /* 6: status, stored_epoch */

#define CTAG_RECORD_HEADER_LEN 5u /* type u8, counter u32 */
#define CTAG_RECORD_OVERHEAD   (CTAG_RECORD_HEADER_LEN + CTAG_TAG_RECORD_MIC_LEN)

/* One record direction: key, next counter, nonce direction byte. */
struct ctag_record_dir {
	uint8_t key[16];
	uint32_t counter;
	uint8_t dir; /* enum ctag_rec_dir */
};

enum ctag_session_state {
	CTAG_SESSION_IDLE = 0,
	CTAG_SESSION_HELLO_SENT, /* bridge */
	CTAG_SESSION_CHALLENGED, /* tag: CHALLENGE sent; bridge: AUTH sent */
	CTAG_SESSION_ESTABLISHED,
	CTAG_SESSION_FAILED,
};

struct ctag_session {
	uint8_t state;
	uint32_t tag_id;
	uint32_t epoch; /* the tag persists it after AUTH when above stored_epoch */
	uint8_t k_epoch[CTAG_TAG_KEY_LEN];
	uint8_t mac_b[CTAG_TAG_MAC_LEN];
	union {
		uint8_t hello[CTAG_SESSION_HELLO_LEN]; /* bridge, until CHALLENGE */
		uint8_t th[32];                        /* transcript hash */
	} t;
	struct ctag_record_dir tx;
	struct ctag_record_dir rx;
};

/* K_epoch = HKDF-SHA256(tag_secret, "cremind-tag/v1/epoch", "K_epoch" | tag_id | epoch, 16). */
int ctag_session_k_epoch(const uint8_t secret[CTAG_TAG_SECRET_LEN], uint32_t tag_id, uint32_t epoch,
			 uint8_t k_epoch[CTAG_TAG_KEY_LEN]);

/* Constant-time comparison. */
bool ctag_session_equal(const uint8_t *a, const uint8_t *b, size_t len);

/* ERROR{status, stored_epoch}, the tag's plaintext refusal (epochs are not
 * secret). Writes CTAG_SESSION_ERROR_LEN bytes and returns that length. */
size_t ctag_session_error_pack(uint8_t out[CTAG_SESSION_ERROR_LEN], uint8_t status,
			       uint32_t stored_epoch);

/* A well-formed ERROR with a non-OK status: true with *status and
 * *stored_epoch set (either pointer may be NULL). */
bool ctag_session_error_unpack(const uint8_t *msg, size_t len, uint8_t *status,
			       uint32_t *stored_epoch);

/*
 * Bridge: start a session and write HELLO into out. nonce_b must be fresh
 * (ctag_crypto_random()). Returns 0.
 */
int ctag_session_bridge_hello(struct ctag_session *s, uint32_t tag_id, uint32_t epoch,
			      const uint8_t k_epoch[CTAG_TAG_KEY_LEN],
			      const uint8_t nonce_b[CTAG_TAG_NONCE_LEN],
			      uint8_t out[CTAG_SESSION_HELLO_LEN]);

/*
 * Bridge: CHALLENGE (or ERROR) received; caps is the CAPS value read from this
 * tag (caps_len > 0). Fills *ch and writes AUTH into out. Returns OK, the
 * tag's ERROR status (*ch is then zero but for the ERROR's stored_epoch),
 * INVALID (unexpected or malformed), VERSION_MISMATCH or INTERNAL.
 */
uint8_t ctag_session_bridge_challenge(struct ctag_session *s, const uint8_t *caps, size_t caps_len,
				      const uint8_t *msg, size_t len, struct ctag_ctrl_challenge *ch,
				      uint8_t out[CTAG_SESSION_AUTH_LEN]);

/*
 * Bridge: AUTH_OK (or ERROR) received; verifies mac_t and derives the record
 * keys. Returns OK, the tag's ERROR status, INVALID, AUTH_FAILED or INTERNAL.
 */
uint8_t ctag_session_bridge_auth_ok(struct ctag_session *s, const uint8_t *msg, size_t len);

/*
 * Tag: HELLO received. caps is the CAPS value the tag serves (caps_len > 0);
 * ch carries the CHALLENGE fields (stored_epoch, displayed_rev, last_status,
 * battery_mv, flags, fresh nonce_t); proto is set here. Checks, in order:
 * message (INVALID), tag_id (NOT_FOUND), proto (VERSION_MISMATCH), epoch >=
 * stored_epoch (STALE_EPOCH). Writes CHALLENGE, or ERROR{status,
 * ch->stored_epoch} on failure, into out (CTAG_SESSION_CHALLENGE_LEN bytes)
 * and sets *out_len. Returns the status.
 */
uint8_t ctag_session_tag_hello(struct ctag_session *s, uint32_t tag_id,
			       const uint8_t secret[CTAG_TAG_SECRET_LEN], const uint8_t *caps,
			       size_t caps_len, const struct ctag_ctrl_challenge *ch,
			       const uint8_t *msg, size_t len, uint8_t *out, size_t *out_len);

/*
 * Tag: AUTH received. Writes AUTH_OK, or ERROR{AUTH_FAILED, stored_epoch} for
 * any failure, into out (CTAG_SESSION_AUTH_OK_LEN bytes). On OK the session is
 * established and s->epoch is authenticated.
 */
uint8_t ctag_session_tag_auth(struct ctag_session *s, uint32_t stored_epoch, const uint8_t *msg,
			      size_t len, uint8_t *out, size_t *out_len);

/*
 * Seal len <= TAG_RECORD_PAYLOAD_MAX plaintext bytes as a record of type:
 * out receives len + CTAG_RECORD_OVERHEAD bytes (pt must not overlap out).
 * Returns the record length, -EMSGSIZE or -EIO.
 */
int ctag_record_seal(struct ctag_record_dir *d, uint8_t type, const uint8_t *pt, size_t len,
		     uint8_t *out, size_t size);

/*
 * Authenticate and decrypt a record: returns the plaintext length with *type
 * set, or -EBADMSG (AUTH_FAILED: length, counter or MIC); -EMSGSIZE when pt is
 * too small. The counter advances only on success.
 */
int ctag_record_open(struct ctag_record_dir *d, const uint8_t *rec, size_t len, uint8_t *type,
		     uint8_t *pt, size_t size);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_SESSION_H_ */
