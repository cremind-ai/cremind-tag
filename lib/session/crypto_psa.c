/*
 * PSA Crypto backend of ctag_crypto.h. Fits the lean tag profile of
 * docs/firmware-notes.md 12 (HMAC, SHA-256, CCM, AES-128; no PSA key
 * derivation, no PSA RNG): keys are imported as volatile keys and destroyed
 * after each operation, so at most one key slot is in use. Random bytes come
 * from sys_csrand_get().
 */
#include <errno.h>
#include <string.h>

#include <psa/crypto.h>
#include <zephyr/random/random.h>

#include <ctag/ctag_crypto.h>

#define CCM8 PSA_ALG_AEAD_WITH_SHORTENED_TAG(PSA_ALG_CCM, 8)
#define HMAC PSA_ALG_HMAC(PSA_ALG_SHA_256)

int ctag_crypto_init(void)
{
	return psa_crypto_init() == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_sha256_init(ctag_sha256_ctx *ctx)
{
	/* All-bits-zero is a valid initial operation; no stack temporary. */
	memset(ctx, 0, sizeof(*ctx));
	return psa_hash_setup(ctx, PSA_ALG_SHA_256) == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_sha256_update(ctag_sha256_ctx *ctx, const uint8_t *data, size_t len)
{
	return psa_hash_update(ctx, data, len) == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_sha256_finish(ctag_sha256_ctx *ctx, uint8_t digest[CTAG_SHA256_LEN])
{
	size_t n;

	return psa_hash_finish(ctx, digest, CTAG_SHA256_LEN, &n) == PSA_SUCCESS ? 0 : -EIO;
}

void ctag_crypto_sha256_abort(ctag_sha256_ctx *ctx)
{
	(void)psa_hash_abort(ctx);
}

static int import_key(psa_key_type_t type, psa_algorithm_t alg, psa_key_usage_t usage,
		      const uint8_t *key, size_t len, psa_key_id_t *id)
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_status_t st;

	psa_set_key_type(&attr, type);
	psa_set_key_algorithm(&attr, alg);
	psa_set_key_usage_flags(&attr, usage);
	st = psa_import_key(&attr, key, len, id);
	psa_reset_key_attributes(&attr);
	return st == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t msg_len,
			    uint8_t mac[CTAG_SHA256_LEN])
{
	psa_key_id_t id;
	psa_status_t st;
	size_t n;

	if (import_key(PSA_KEY_TYPE_HMAC, HMAC, PSA_KEY_USAGE_SIGN_MESSAGE, key, key_len, &id) !=
	    0) {
		return -EIO;
	}
	st = psa_mac_compute(id, HMAC, msg, msg_len, mac, CTAG_SHA256_LEN, &n);
	(void)psa_destroy_key(id);
	return st == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_ccm8(bool encrypt, const uint8_t key[16], const uint8_t nonce[CTAG_CCM_NONCE_LEN],
		     const uint8_t *aad, size_t aad_len, const uint8_t *in, size_t len,
		     uint8_t *out)
{
	psa_key_id_t id;
	psa_status_t st;
	size_t n;

	if (!encrypt && len < 8u) {
		return -EBADMSG;
	}
	if (import_key(PSA_KEY_TYPE_AES, CCM8, PSA_KEY_USAGE_ENCRYPT | PSA_KEY_USAGE_DECRYPT, key,
		       16u, &id) != 0) {
		return -EIO;
	}
	if (encrypt) {
		st = psa_aead_encrypt(id, CCM8, nonce, CTAG_CCM_NONCE_LEN, aad, aad_len, in, len,
				      out, len + 8u, &n);
	} else {
		st = psa_aead_decrypt(id, CCM8, nonce, CTAG_CCM_NONCE_LEN, aad, aad_len, in, len,
				      out, len - 8u, &n);
	}
	(void)psa_destroy_key(id);
	if (st == PSA_ERROR_INVALID_SIGNATURE) {
		return -EBADMSG;
	}
	return st == PSA_SUCCESS ? 0 : -EIO;
}

int ctag_crypto_random(uint8_t *buf, size_t len)
{
	return sys_csrand_get(buf, len) == 0 ? 0 : -EIO;
}
