/*
 * Crypto primitives used by the tag session (docs/protocol.md 5.4-5.5) and by
 * the apps (plane/frame digests). One backend is linked: lib/session/
 * crypto_psa.c (CONFIG_CTAG_CRYPTO_PSA) uses the PSA Crypto API with volatile
 * keys that are destroyed after every operation. Other backends provide
 * <ctag_crypto_backend.h> defining ctag_sha256_ctx.
 */
#ifndef CTAG_CRYPTO_H_
#define CTAG_CRYPTO_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef CONFIG_CTAG_CRYPTO_PSA
#include <psa/crypto.h>
typedef psa_hash_operation_t ctag_sha256_ctx;
#else
#include <ctag_crypto_backend.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define CTAG_SHA256_LEN    32u
#define CTAG_CCM_NONCE_LEN 13u

/* Once at boot (psa_crypto_init()). All functions return 0 on success. */
int ctag_crypto_init(void);

int ctag_crypto_sha256_init(ctag_sha256_ctx *ctx);
int ctag_crypto_sha256_update(ctag_sha256_ctx *ctx, const uint8_t *data, size_t len);
/* Finishes and releases ctx; on any error call ctag_crypto_sha256_abort(). */
int ctag_crypto_sha256_finish(ctag_sha256_ctx *ctx, uint8_t digest[CTAG_SHA256_LEN]);
void ctag_crypto_sha256_abort(ctag_sha256_ctx *ctx);

int ctag_crypto_hmac_sha256(const uint8_t *key, size_t key_len, const uint8_t *msg, size_t msg_len,
			    uint8_t mac[CTAG_SHA256_LEN]);

/*
 * AES-128-CCM with an 8-byte tag. Encrypt: out receives len + 8 bytes.
 * Decrypt: len includes the tag, out receives len - 8 bytes, -EBADMSG when
 * the tag does not verify. One function keeps the tag's flash small.
 */
int ctag_crypto_ccm8(bool encrypt, const uint8_t key[16], const uint8_t nonce[CTAG_CCM_NONCE_LEN],
		     const uint8_t *aad, size_t aad_len, const uint8_t *in, size_t len,
		     uint8_t *out);

/* Cryptographically secure random bytes (nonces). */
int ctag_crypto_random(uint8_t *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* CTAG_CRYPTO_H_ */
