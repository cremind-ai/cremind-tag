/*
 * Primitives from the vendored HACL* (lib/third_party/hacl): SHA-256,
 * HMAC-SHA256, HKDF built from two HMACs (every output is <= 32 bytes, RFC
 * 5869), X25519 public keys and Ed25519 grant signatures. HACL* never
 * allocates; secrets on this file's stack are wiped before returning.
 */
#include <errno.h>
#include <string.h>

#include "ctag_krml.h"

#include "Hacl_Curve25519_51.h"
#include "Hacl_Ed25519.h"
#include "Hacl_HMAC.h"
#include "Hacl_Hash_SHA2.h"

#include <ctag/ctag_secure.h>

#include "secure_int.h"

#define HKDF_INFO_MAX 63u

void ctag_secure_sha256(const uint8_t *data, size_t len, uint8_t out[CTAG_SECURE_HASH_LEN])
{
	Hacl_Hash_SHA2_hash_256(ctag_mut(data), (uint32_t)len, out);
}

void ctag_secure_hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *data, size_t len,
			     uint8_t out[CTAG_SECURE_HASH_LEN])
{
	Hacl_HMAC_compute_sha2_256(out, ctag_mut(key), (uint32_t)key_len, ctag_mut(data),
				   (uint32_t)len);
}

int ctag_secure_hkdf(const uint8_t *ikm, size_t ikm_len, const uint8_t *salt, size_t salt_len,
		     const uint8_t *info, size_t info_len, uint8_t *out, size_t len)
{
	uint8_t prk[CTAG_SECURE_HASH_LEN];
	uint8_t t[CTAG_SECURE_HASH_LEN];
	uint8_t msg[HKDF_INFO_MAX + 1u];

	if (len == 0u || len > CTAG_SECURE_HASH_LEN || info_len > HKDF_INFO_MAX) {
		return -EINVAL;
	}
	/* Extract: PRK = HMAC(salt, IKM); expand: T(1) = HMAC(PRK, info | 0x01). */
	ctag_secure_hmac_sha256(salt, salt_len, ikm, ikm_len, prk);
	if (info_len > 0u) {
		memcpy(msg, info, info_len);
	}
	msg[info_len] = 0x01u;
	ctag_secure_hmac_sha256(prk, sizeof(prk), msg, info_len + 1u, t);
	memcpy(out, t, len);
	ctag_secure_wipe(prk, sizeof(prk));
	ctag_secure_wipe(t, sizeof(t));
	ctag_secure_wipe(msg, sizeof(msg));
	return 0;
}

void ctag_secure_x25519_public(const uint8_t priv[32], uint8_t pub[32])
{
	Hacl_Curve25519_51_secret_to_public(pub, ctag_mut(priv));
}

bool ctag_secure_grant_sig_ok(const uint8_t authority_pub[32], const uint8_t *grant, size_t len,
			      const uint8_t sig[CTAG_GRANT_SIG_LEN])
{
	uint8_t msg[CTAG_V2_GRANT_LEN + CTAG_GRANT_MAX];

	if (len > CTAG_GRANT_MAX) {
		return false;
	}
	memcpy(msg, CTAG_V2_GRANT, CTAG_V2_GRANT_LEN);
	memcpy(&msg[CTAG_V2_GRANT_LEN], grant, len);
	return Hacl_Ed25519_verify(ctag_mut(authority_pub), (uint32_t)(CTAG_V2_GRANT_LEN + len), msg,
				   ctag_mut(sig));
}

void ctag_secure_ed25519_public(const uint8_t sk[32], uint8_t pub[32])
{
	Hacl_Ed25519_secret_to_public(pub, ctag_mut(sk));
}

void ctag_secure_grant_sign(const uint8_t sk[32], const uint8_t *grant, size_t len,
			    uint8_t sig[CTAG_GRANT_SIG_LEN])
{
	uint8_t msg[CTAG_V2_GRANT_LEN + CTAG_GRANT_MAX];

	if (len > CTAG_GRANT_MAX) {
		memset(sig, 0, CTAG_GRANT_SIG_LEN);
		return;
	}
	memcpy(msg, CTAG_V2_GRANT, CTAG_V2_GRANT_LEN);
	memcpy(&msg[CTAG_V2_GRANT_LEN], grant, len);
	Hacl_Ed25519_sign(sig, ctag_mut(sk), (uint32_t)(CTAG_V2_GRANT_LEN + len), msg);
}
