/* Identity and the v2 key schedule (identity.py; docs/connect-setup.md 2.1, 3.3-3.6). */
#include <string.h>

#include <ctag/ctag_secure.h>

#include "secure_int.h"

bool ctag_secure_equal(const void *a, const void *b, size_t len)
{
	const volatile uint8_t *x = a;
	const volatile uint8_t *y = b;
	uint8_t acc = 0u;

	for (size_t i = 0u; i < len; i++) {
		acc |= (uint8_t)(x[i] ^ y[i]);
	}
	return acc == 0u;
}

void ctag_secure_device_id(uint8_t role, const uint8_t ik_pub[32], uint8_t out[CTAG_DEVICE_ID_LEN])
{
	uint8_t msg[CTAG_V2_DEVICE_ID_LEN + 1u + CTAG_IDENTITY_KEY_LEN];
	uint8_t digest[CTAG_SECURE_HASH_LEN];

	memcpy(msg, CTAG_V2_DEVICE_ID, CTAG_V2_DEVICE_ID_LEN);
	msg[CTAG_V2_DEVICE_ID_LEN] = role;
	memcpy(&msg[CTAG_V2_DEVICE_ID_LEN + 1u], ik_pub, CTAG_IDENTITY_KEY_LEN);
	ctag_secure_sha256(msg, sizeof(msg), digest);
	memcpy(out, digest, CTAG_DEVICE_ID_LEN);
}

uint32_t ctag_secure_short_id(const uint8_t device_id[CTAG_DEVICE_ID_LEN])
{
	uint32_t v = ctag_get_le32(device_id);

	if (v == 0u || v == 0xFFFFFFFFu) {
		v ^= 0x5A5A5A5Au;
	}
	return v;
}

void ctag_secure_authority_id(const uint8_t authority_pub[32], uint8_t out[16])
{
	uint8_t digest[CTAG_SECURE_HASH_LEN];

	ctag_secure_sha256(authority_pub, CTAG_AUTHORITY_KEY_LEN, digest);
	memcpy(out, digest, 16u);
}

void ctag_secure_k_setup(const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			 const uint8_t device_id[CTAG_DEVICE_ID_LEN], uint8_t out[32])
{
	(void)ctag_secure_hkdf(secret, CTAG_SETUP_SECRET_LEN, (const uint8_t *)CTAG_V2_SETUP_SALT,
			       CTAG_V2_SETUP_SALT_LEN, device_id, CTAG_DEVICE_ID_LEN, out, 32u);
}

void ctag_secure_static_oob(const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			    const uint8_t device_id[CTAG_DEVICE_ID_LEN],
			    uint8_t out[CTAG_STATIC_OOB_LEN])
{
	(void)ctag_secure_hkdf(secret, CTAG_SETUP_SECRET_LEN, (const uint8_t *)CTAG_V2_MESH_OOB_SALT,
			       CTAG_V2_MESH_OOB_SALT_LEN, device_id, CTAG_DEVICE_ID_LEN, out,
			       CTAG_STATIC_OOB_LEN);
}

void ctag_secure_k_epoch(const uint8_t root[CTAG_OP_KEY_LEN], uint32_t tag_id, uint32_t epoch,
			 uint8_t out[CTAG_TAG_KEY_LEN])
{
	uint8_t info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN + 8u];

	memcpy(info, CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX, CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN);
	ctag_put_le32(&info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN], tag_id);
	ctag_put_le32(&info[CTAG_CRYPTO_HKDF_INFO_EPOCH_PREFIX_LEN + 4u], epoch);
	(void)ctag_secure_hkdf(root, CTAG_OP_KEY_LEN, (const uint8_t *)CTAG_V2_EPOCH_SALT,
			       CTAG_V2_EPOCH_SALT_LEN, info, sizeof(info), out, CTAG_TAG_KEY_LEN);
}

/* HMAC(key, label | a | b)[0:16] with the parts concatenated on the stack. */
static void proof(const uint8_t *key, size_t key_len, const char *label, size_t label_len,
		  const uint8_t *a, size_t a_len, const uint8_t *b, size_t b_len,
		  uint8_t out[CTAG_PROOF_LEN])
{
	uint8_t msg[CTAG_V2_ROOT_PROOF_LEN + 2u * CTAG_SECURE_HASH_LEN];
	uint8_t mac[CTAG_SECURE_HASH_LEN];
	size_t n = 0u;

	memcpy(&msg[n], label, label_len);
	n += label_len;
	memcpy(&msg[n], a, a_len);
	n += a_len;
	if (b_len > 0u) {
		memcpy(&msg[n], b, b_len);
		n += b_len;
	}
	ctag_secure_hmac_sha256(key, key_len, msg, n, mac);
	memcpy(out, mac, CTAG_PROOF_LEN);
	ctag_secure_wipe(mac, sizeof(mac));
}

void ctag_secure_proof_s(const uint8_t k_setup[32], const uint8_t h[32], const uint8_t *grant,
			 size_t grant_len, uint8_t out[CTAG_PROOF_LEN])
{
	uint8_t gh[CTAG_SECURE_HASH_LEN];

	ctag_secure_sha256(grant, grant_len, gh);
	proof(k_setup, 32u, CTAG_V2_SETUP_LABEL_S, CTAG_V2_SETUP_LABEL_S_LEN, h, CTAG_SECURE_HASH_LEN,
	      gh, sizeof(gh), out);
}

void ctag_secure_proof_d(const uint8_t k_setup[32], const uint8_t h[32],
			 const uint8_t proof_s[CTAG_PROOF_LEN], uint8_t out[CTAG_PROOF_LEN])
{
	proof(k_setup, 32u, CTAG_V2_SETUP_LABEL_D, CTAG_V2_SETUP_LABEL_D_LEN, h, CTAG_SECURE_HASH_LEN,
	      proof_s, CTAG_PROOF_LEN, out);
}

void ctag_secure_root_proof(const uint8_t root[CTAG_OP_KEY_LEN], const uint8_t h[32],
			    uint8_t out[CTAG_PROOF_LEN])
{
	proof(root, CTAG_OP_KEY_LEN, CTAG_V2_ROOT_PROOF, CTAG_V2_ROOT_PROOF_LEN, h,
	      CTAG_SECURE_HASH_LEN, NULL, 0u, out);
}

void ctag_secure_maint_proof(const uint8_t mk[CTAG_OP_KEY_LEN], const uint8_t h[32],
			     uint8_t out[CTAG_PROOF_LEN])
{
	proof(mk, CTAG_OP_KEY_LEN, CTAG_V2_MAINT_PROOF, CTAG_V2_MAINT_PROOF_LEN, h,
	      CTAG_SECURE_HASH_LEN, NULL, 0u, out);
}

void ctag_secure_setup_payload(uint8_t role, uint32_t short_id,
			       const uint8_t secret[CTAG_SETUP_SECRET_LEN],
			       uint8_t out[CTAG_SETUP_PAYLOAD_LEN])
{
	out[0] = (uint8_t)(0x20u | role);
	ctag_put_le32(&out[1], short_id);
	memcpy(&out[5], secret, CTAG_SETUP_SECRET_LEN);
}

void ctag_secure_keys_init(struct ctag_secure_keys *k, uint8_t role, const uint8_t ik_priv[32],
			   const uint8_t *factory_secret, uint8_t board, uint8_t fw_major,
			   uint8_t fw_minor, uint8_t fw_patch)
{
	memset(k, 0, sizeof(*k));
	k->role = role;
	k->board = board;
	k->fw[0] = fw_major;
	k->fw[1] = fw_minor;
	k->fw[2] = fw_patch;
	memcpy(k->ik_priv, ik_priv, sizeof(k->ik_priv));
	ctag_secure_x25519_public(k->ik_priv, k->ik_pub);
	ctag_secure_device_id(role, k->ik_pub, k->device_id);
	k->short_id = ctag_secure_short_id(k->device_id);
	if (factory_secret != NULL) {
		k->has_factory_secret = true;
		memcpy(k->factory_secret, factory_secret, sizeof(k->factory_secret));
	}
}
