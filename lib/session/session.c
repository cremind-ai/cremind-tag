/*
 * Tag session handshake and records (docs/protocol.md 5.4-5.5); mirrors
 * companion protocol/session.py. HKDF is built from HMAC (firmware-notes 5:
 * never the PSA key-derivation API); every output here is at most 32 bytes,
 * so HKDF-Expand needs a single block.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_crypto.h>
#include <ctag/ctag_session.h>

#define LABEL_LEN   CTAG_CRYPTO_MAC_LABEL_BRIDGE_LEN
#define MAC_MSG_MAX (LABEL_LEN + 32u + CTAG_TAG_MAC_LEN)
#define INFO_MAX    CTAG_CRYPTO_HKDF_INFO_SESSION_LEN

static void wipe(void *p, size_t len)
{
	volatile uint8_t *v = p;

	while (len-- > 0u) {
		*v++ = 0u;
	}
}

bool ctag_session_equal(const uint8_t *a, const uint8_t *b, size_t len)
{
	uint8_t diff = 0u;

	while (len-- > 0u) {
		diff |= (uint8_t)(*a++ ^ *b++);
	}
	return diff == 0u;
}

/* HKDF-SHA256 with L <= 32: PRK = HMAC(salt, IKM); OKM = HMAC(PRK, info | 0x01). */
static int hkdf(const uint8_t *salt, size_t salt_len, const uint8_t *ikm, size_t ikm_len,
		const uint8_t *info, size_t info_len, uint8_t okm[32])
{
	uint8_t prk[32];
	uint8_t msg[INFO_MAX + 1u];
	int err = ctag_crypto_hmac_sha256(salt, salt_len, ikm, ikm_len, prk);

	if (err == 0) {
		memcpy(msg, info, info_len);
		msg[info_len] = 1u;
		err = ctag_crypto_hmac_sha256(prk, sizeof(prk), msg, info_len + 1u, okm);
	}
	wipe(prk, sizeof(prk));
	return err;
}

int ctag_session_k_epoch(const uint8_t secret[CTAG_TAG_SECRET_LEN], uint32_t tag_id, uint32_t epoch,
			 uint8_t k_epoch[CTAG_TAG_KEY_LEN])
{
	uint8_t info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN + 8u];
	uint8_t okm[32];
	int err;

	memcpy(info, CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX, CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN);
	ctag_put_le32(&info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN], tag_id);
	ctag_put_le32(&info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN + 4u], epoch);
	err = hkdf((const uint8_t *)CTAG_CRYPTO_HKDF_SALT_EPOCH, CTAG_CRYPTO_HKDF_SALT_EPOCH_LEN,
		   secret, CTAG_TAG_SECRET_LEN, info, sizeof(info), okm);
	memcpy(k_epoch, okm, CTAG_TAG_KEY_LEN);
	wipe(okm, sizeof(okm));
	return err;
}

/* th = SHA-256(CAPS | HELLO | CHALLENGE), stored over the union holding HELLO. */
static int transcript(struct ctag_session *s, const uint8_t *caps, size_t caps_len,
		      const uint8_t *hello, const uint8_t *challenge)
{
	ctag_sha256_ctx ctx;
	uint8_t th[32];

	if (caps == NULL || caps_len == 0u || ctag_crypto_sha256_init(&ctx) != 0) {
		return -EIO;
	}
	if (ctag_crypto_sha256_update(&ctx, caps, caps_len) != 0 ||
	    ctag_crypto_sha256_update(&ctx, hello, CTAG_SESSION_HELLO_LEN) != 0 ||
	    ctag_crypto_sha256_update(&ctx, challenge, CTAG_SESSION_CHALLENGE_LEN) != 0 ||
	    ctag_crypto_sha256_finish(&ctx, th) != 0) {
		ctag_crypto_sha256_abort(&ctx);
		return -EIO;
	}
	memcpy(s->t.th, th, sizeof(th));
	return 0;
}

/* HMAC-SHA256(K_epoch, label | th [| mac_b])[0:16]. */
static int handshake_mac(const struct ctag_session *s, char label, const uint8_t *mac_b,
			 uint8_t out[CTAG_TAG_MAC_LEN])
{
	uint8_t msg[MAC_MSG_MAX];
	uint8_t mac[32];
	size_t len = LABEL_LEN + sizeof(s->t.th);
	int err;

	msg[0] = (uint8_t)label;
	memcpy(&msg[LABEL_LEN], s->t.th, sizeof(s->t.th));
	if (mac_b != NULL) {
		memcpy(&msg[len], mac_b, CTAG_TAG_MAC_LEN);
		len += CTAG_TAG_MAC_LEN;
	}
	err = ctag_crypto_hmac_sha256(s->k_epoch, sizeof(s->k_epoch), msg, len, mac);
	memcpy(out, mac, CTAG_TAG_MAC_LEN);
	wipe(mac, sizeof(mac));
	return err;
}

/* k_b2t | k_t2b = HKDF(K_epoch, th, "cremind-tag/v1/session", 32); then forget K_epoch. */
static int establish(struct ctag_session *s, bool bridge)
{
	uint8_t okm[32];
	int err = hkdf(s->t.th, sizeof(s->t.th), s->k_epoch, sizeof(s->k_epoch),
		       (const uint8_t *)CTAG_CRYPTO_HKDF_INFO_SESSION,
		       CTAG_CRYPTO_HKDF_INFO_SESSION_LEN, okm);
	struct ctag_record_dir *b2t = bridge ? &s->tx : &s->rx;
	struct ctag_record_dir *t2b = bridge ? &s->rx : &s->tx;

	memcpy(b2t->key, okm, 16u);
	memcpy(t2b->key, &okm[16], 16u);
	b2t->dir = CTAG_REC_DIR_B2T;
	t2b->dir = CTAG_REC_DIR_T2B;
	b2t->counter = 0u;
	t2b->counter = 0u;
	wipe(okm, sizeof(okm));
	wipe(s->k_epoch, sizeof(s->k_epoch));
	s->state = err == 0 ? CTAG_SESSION_ESTABLISHED : CTAG_SESSION_FAILED;
	return err;
}

size_t ctag_session_error_pack(uint8_t out[CTAG_SESSION_ERROR_LEN], uint8_t status,
			       uint32_t stored_epoch)
{
	out[0] = CTAG_CTRL_ERROR;
	out[1] = status;
	ctag_put_le32(&out[2], stored_epoch);
	return CTAG_SESSION_ERROR_LEN;
}

bool ctag_session_error_unpack(const uint8_t *msg, size_t len, uint8_t *status,
			       uint32_t *stored_epoch)
{
	if (len != CTAG_SESSION_ERROR_LEN || msg[0] != CTAG_CTRL_ERROR || msg[1] == CTAG_STATUS_OK) {
		return false;
	}
	if (status != NULL) {
		*status = msg[1];
	}
	if (stored_epoch != NULL) {
		*stored_epoch = ctag_get_le32(&msg[2]);
	}
	return true;
}

/* A tag ERROR carries a non-OK status; anything else unexpected is INVALID. */
static uint8_t tag_error(const uint8_t *msg, size_t len, uint32_t *stored_epoch)
{
	uint8_t st;

	return ctag_session_error_unpack(msg, len, &st, stored_epoch) ? st : CTAG_STATUS_INVALID;
}

int ctag_session_bridge_hello(struct ctag_session *s, uint32_t tag_id, uint32_t epoch,
			      const uint8_t k_epoch[CTAG_TAG_KEY_LEN],
			      const uint8_t nonce_b[CTAG_TAG_NONCE_LEN],
			      uint8_t out[CTAG_SESSION_HELLO_LEN])
{
	struct ctag_ctrl_hello h = {.proto = CTAG_PROTO_VERSION, .tag_id = tag_id, .epoch = epoch};

	memset(s, 0, sizeof(*s));
	s->tag_id = tag_id;
	s->epoch = epoch;
	memcpy(s->k_epoch, k_epoch, CTAG_TAG_KEY_LEN);
	memcpy(h.nonce_b, nonce_b, CTAG_TAG_NONCE_LEN);
	out[0] = CTAG_CTRL_HELLO;
	(void)ctag_ctrl_hello_pack(&h, &out[1], CTAG_CTRL_HELLO_LEN);
	memcpy(s->t.hello, out, CTAG_SESSION_HELLO_LEN);
	s->state = CTAG_SESSION_HELLO_SENT;
	return 0;
}

uint8_t ctag_session_bridge_challenge(struct ctag_session *s, const uint8_t *caps, size_t caps_len,
				      const uint8_t *msg, size_t len, struct ctag_ctrl_challenge *ch,
				      uint8_t out[CTAG_SESSION_AUTH_LEN])
{
	uint8_t hello[CTAG_SESSION_HELLO_LEN];
	uint8_t st = CTAG_STATUS_INVALID;

	memset(ch, 0, sizeof(*ch));
	if (s->state != CTAG_SESSION_HELLO_SENT) {
		goto fail;
	}
	if (len != CTAG_SESSION_CHALLENGE_LEN || msg[0] != CTAG_CTRL_CHALLENGE) {
		st = tag_error(msg, len, &ch->stored_epoch);
		goto fail;
	}
	(void)ctag_ctrl_challenge_unpack(ch, &msg[1], CTAG_CTRL_CHALLENGE_LEN);
	if (ch->proto != CTAG_PROTO_VERSION) {
		st = CTAG_STATUS_VERSION_MISMATCH;
		goto fail;
	}
	memcpy(hello, s->t.hello, sizeof(hello));
	st = CTAG_STATUS_INTERNAL;
	if (transcript(s, caps, caps_len, hello, msg) != 0 ||
	    handshake_mac(s, CTAG_CRYPTO_MAC_LABEL_BRIDGE[0], NULL, s->mac_b) != 0) {
		goto fail;
	}
	out[0] = CTAG_CTRL_AUTH;
	memcpy(&out[1], s->mac_b, CTAG_TAG_MAC_LEN);
	s->state = CTAG_SESSION_CHALLENGED;
	return CTAG_STATUS_OK;

fail:
	wipe(s, sizeof(*s));
	s->state = CTAG_SESSION_FAILED;
	return st;
}

uint8_t ctag_session_bridge_auth_ok(struct ctag_session *s, const uint8_t *msg, size_t len)
{
	uint8_t mac_t[CTAG_TAG_MAC_LEN];
	uint8_t st = CTAG_STATUS_INVALID;

	if (s->state != CTAG_SESSION_CHALLENGED) {
		goto fail;
	}
	if (len != CTAG_SESSION_AUTH_OK_LEN || msg[0] != CTAG_CTRL_AUTH_OK) {
		st = tag_error(msg, len, NULL);
		goto fail;
	}
	st = CTAG_STATUS_INTERNAL;
	if (handshake_mac(s, CTAG_CRYPTO_MAC_LABEL_TAG[0], s->mac_b, mac_t) != 0) {
		goto fail;
	}
	st = CTAG_STATUS_AUTH_FAILED;
	if (!ctag_session_equal(mac_t, &msg[1], CTAG_TAG_MAC_LEN)) {
		goto fail;
	}
	if (establish(s, true) != 0) {
		st = CTAG_STATUS_INTERNAL;
		goto fail;
	}
	return CTAG_STATUS_OK;

fail:
	wipe(s, sizeof(*s));
	s->state = CTAG_SESSION_FAILED;
	return st;
}

static uint8_t tag_fail(struct ctag_session *s, uint8_t st, uint32_t stored_epoch, uint8_t *out,
			size_t *out_len)
{
	wipe(s, sizeof(*s));
	s->state = CTAG_SESSION_FAILED;
	*out_len = ctag_session_error_pack(out, st, stored_epoch);
	return st;
}

uint8_t ctag_session_tag_hello(struct ctag_session *s, uint32_t tag_id,
			       const uint8_t secret[CTAG_TAG_SECRET_LEN], const uint8_t *caps,
			       size_t caps_len, const struct ctag_ctrl_challenge *ch,
			       const uint8_t *msg, size_t len, uint8_t *out, size_t *out_len)
{
	uint32_t stored = ch->stored_epoch;
	struct ctag_ctrl_hello h;

	memset(s, 0, sizeof(*s));
	if (len != CTAG_SESSION_HELLO_LEN || msg[0] != CTAG_CTRL_HELLO) {
		return tag_fail(s, CTAG_STATUS_INVALID, stored, out, out_len);
	}
	(void)ctag_ctrl_hello_unpack(&h, &msg[1], CTAG_CTRL_HELLO_LEN);
	if (h.tag_id != tag_id) {
		return tag_fail(s, CTAG_STATUS_NOT_FOUND, stored, out, out_len);
	}
	if (h.proto != CTAG_PROTO_VERSION) {
		return tag_fail(s, CTAG_STATUS_VERSION_MISMATCH, stored, out, out_len);
	}
	if (h.epoch < stored) {
		return tag_fail(s, CTAG_STATUS_STALE_EPOCH, stored, out, out_len);
	}
	s->tag_id = tag_id;
	s->epoch = h.epoch;
	out[0] = CTAG_CTRL_CHALLENGE;
	(void)ctag_ctrl_challenge_pack(ch, &out[1], CTAG_CTRL_CHALLENGE_LEN);
	out[1] = CTAG_PROTO_VERSION; /* proto is the first CHALLENGE field */
	if (ctag_session_k_epoch(secret, tag_id, h.epoch, s->k_epoch) != 0 ||
	    transcript(s, caps, caps_len, msg, out) != 0) {
		return tag_fail(s, CTAG_STATUS_INTERNAL, stored, out, out_len);
	}
	s->state = CTAG_SESSION_CHALLENGED;
	*out_len = CTAG_SESSION_CHALLENGE_LEN;
	return CTAG_STATUS_OK;
}

uint8_t ctag_session_tag_auth(struct ctag_session *s, uint32_t stored_epoch, const uint8_t *msg,
			      size_t len, uint8_t *out, size_t *out_len)
{
	uint8_t mac[CTAG_TAG_MAC_LEN];

	/* 5.4: any failure here is AUTH_FAILED (it counts toward wake-window pacing). */
	if (s->state != CTAG_SESSION_CHALLENGED || len != CTAG_SESSION_AUTH_LEN ||
	    msg[0] != CTAG_CTRL_AUTH) {
		return tag_fail(s, CTAG_STATUS_AUTH_FAILED, stored_epoch, out, out_len);
	}
	if (handshake_mac(s, CTAG_CRYPTO_MAC_LABEL_BRIDGE[0], NULL, mac) != 0) {
		return tag_fail(s, CTAG_STATUS_INTERNAL, stored_epoch, out, out_len);
	}
	if (!ctag_session_equal(mac, &msg[1], CTAG_TAG_MAC_LEN)) {
		return tag_fail(s, CTAG_STATUS_AUTH_FAILED, stored_epoch, out, out_len);
	}
	out[0] = CTAG_CTRL_AUTH_OK;
	if (handshake_mac(s, CTAG_CRYPTO_MAC_LABEL_TAG[0], &msg[1], &out[1]) != 0 ||
	    establish(s, false) != 0) {
		return tag_fail(s, CTAG_STATUS_INTERNAL, stored_epoch, out, out_len);
	}
	*out_len = CTAG_SESSION_AUTH_OK_LEN;
	return CTAG_STATUS_OK;
}

static void record_nonce(const struct ctag_record_dir *d, uint32_t counter,
			 uint8_t nonce[CTAG_CCM_NONCE_LEN])
{
	memset(nonce, 0, CTAG_CCM_NONCE_LEN);
	nonce[0] = d->dir;
	ctag_put_le32(&nonce[1], counter);
}

int ctag_record_seal(struct ctag_record_dir *d, uint8_t type, const uint8_t *pt, size_t len,
		     uint8_t *out, size_t size)
{
	uint8_t nonce[CTAG_CCM_NONCE_LEN];

	if (len > CTAG_TAG_RECORD_PAYLOAD_MAX || size < len + CTAG_RECORD_OVERHEAD) {
		return -EMSGSIZE;
	}
	out[0] = type;
	ctag_put_le32(&out[1], d->counter);
	record_nonce(d, d->counter, nonce);
	if (ctag_crypto_ccm8(true, d->key, nonce, out, CTAG_RECORD_HEADER_LEN, pt, len,
			     &out[CTAG_RECORD_HEADER_LEN]) != 0) {
		return -EIO;
	}
	d->counter++;
	return (int)(len + CTAG_RECORD_OVERHEAD);
}

int ctag_record_open(struct ctag_record_dir *d, const uint8_t *rec, size_t len, uint8_t *type,
		     uint8_t *pt, size_t size)
{
	uint8_t nonce[CTAG_CCM_NONCE_LEN];

	if (len < CTAG_RECORD_OVERHEAD || len > CTAG_TAG_RECORD_WIRE_MAX ||
	    ctag_get_le32(&rec[1]) != d->counter) {
		return -EBADMSG;
	}
	if (size < len - CTAG_RECORD_OVERHEAD) {
		return -EMSGSIZE;
	}
	record_nonce(d, d->counter, nonce);
	if (ctag_crypto_ccm8(false, d->key, nonce, rec, CTAG_RECORD_HEADER_LEN,
			     &rec[CTAG_RECORD_HEADER_LEN], len - CTAG_RECORD_HEADER_LEN, pt) != 0) {
		return -EBADMSG;
	}
	*type = rec[0];
	d->counter++;
	return (int)(len - CTAG_RECORD_OVERHEAD);
}
