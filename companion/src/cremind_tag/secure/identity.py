"""Device identity and the v2 key schedule (connect-setup.md 2.1, 3.3-3.6).

::

    device_id   = SHA-256("cremind-tag/v2/device-id" | role u8 | ik_pub)[0:16]
    short_id    = u32le(device_id[0:4]), ^ 0x5A5A5A5A when 0 or 0xFFFFFFFF (= a v2 tag's tag_id)
    k_setup     = HKDF(setup_secret, salt "cremind-tag/v2/setup", info device_id, 32)
    static_oob  = HKDF(setup_secret, salt "cremind-tag/v2/mesh-oob", info device_id, 32)
    K_epoch v2  = HKDF(root, salt "cremind-tag/v2/epoch", "K_epoch" | tag_id | epoch, 16)
    proof_s     = HMAC(k_setup, "S" | h | SHA-256(grant))[0:16]
    proof_d     = HMAC(k_setup, "D" | h | proof_s)[0:16]
    root_proof  = HMAC(root, "cremind-tag/v2/root-proof" | h)[0:16]
    maint_proof = HMAC(mk, "cremind-tag/v2/maint" | h)[0:16]
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cremind_tag.protocol.ids import (
    CRYPTO_HKDF_INFO_EPOCH_PREFIX,
    DEVICE_ID_LEN,
    PROOF_LEN,
    TAG_KEY_LEN,
    V2_DEVICE_ID,
    V2_EPOCH_SALT,
    V2_MAINT_PROOF,
    V2_MESH_OOB_SALT,
    V2_ROOT_PROOF,
    V2_SETUP_LABEL_D,
    V2_SETUP_LABEL_S,
    V2_SETUP_SALT,
)

_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_NO_ENC = serialization.NoEncryption()


def device_id(role: int, ik_pub: bytes) -> bytes:
    if len(ik_pub) != 32:
        raise ValueError("an identity key is 32 bytes")
    return hashlib.sha256(V2_DEVICE_ID + bytes([int(role)]) + ik_pub).digest()[:DEVICE_ID_LEN]


def short_id(dev_id: bytes) -> int:
    value = int.from_bytes(dev_id[:4], "little")
    if value in (0, 0xFFFFFFFF):
        value ^= 0x5A5A5A5A
    return value


def device_id_text(dev_id: bytes) -> str:
    """The canonical text form (lower-case hex, 32 characters)."""
    return bytes(dev_id).hex()


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def hmac256(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


# ---- X25519 identity / controller keys ----

def x25519_generate() -> tuple[bytes, bytes]:
    """(private, public) raw 32-byte keys from the OS CSPRNG."""
    priv = X25519PrivateKey.generate()
    return (priv.private_bytes(_RAW, _RAW_PRIV, _NO_ENC), priv.public_key().public_bytes(_RAW, _RAW_PUB))


def x25519_public(priv: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(_RAW, _RAW_PUB)


def x25519(priv: bytes, pub: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))


# ---- Ed25519 authority / installation keys ----

def ed25519_generate() -> tuple[bytes, bytes]:
    priv = Ed25519PrivateKey.generate()
    return (priv.private_bytes(_RAW, _RAW_PRIV, _NO_ENC), priv.public_key().public_bytes(_RAW, _RAW_PUB))


def ed25519_public(priv: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(_RAW, _RAW_PUB)


def ed25519_sign(priv: bytes, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(priv).sign(message)


def ed25519_verify(pub: bytes, sig: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature, bad key length/encoding
        return False


def authority_id(authority_pub: bytes) -> bytes:
    return hashlib.sha256(authority_pub).digest()[:16]


# ---- key schedule ----

def k_setup(setup_secret: bytes, dev_id: bytes) -> bytes:
    return hkdf(setup_secret, V2_SETUP_SALT, dev_id, 32)


def static_oob(setup_secret: bytes, dev_id: bytes) -> bytes:
    return hkdf(setup_secret, V2_MESH_OOB_SALT, dev_id, 32)


def k_epoch_v2(root: bytes, tag_id: int, epoch: int) -> bytes:
    if len(root) != 32:
        raise ValueError("an operational root is 32 bytes")
    info = CRYPTO_HKDF_INFO_EPOCH_PREFIX + tag_id.to_bytes(4, "little") + epoch.to_bytes(4, "little")
    return hkdf(root, V2_EPOCH_SALT, info, TAG_KEY_LEN)


def proof_s(k_set: bytes, h: bytes, grant: bytes) -> bytes:
    return hmac256(k_set, V2_SETUP_LABEL_S, h, hashlib.sha256(grant).digest())[:PROOF_LEN]


def proof_d(k_set: bytes, h: bytes, p_s: bytes) -> bytes:
    return hmac256(k_set, V2_SETUP_LABEL_D, h, p_s)[:PROOF_LEN]


def root_proof(root: bytes, h: bytes) -> bytes:
    return hmac256(root, V2_ROOT_PROOF, h)[:PROOF_LEN]


def maint_proof(mk: bytes, h: bytes) -> bytes:
    return hmac256(mk, V2_MAINT_PROOF, h)[:PROOF_LEN]


def equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(bytes(a), bytes(b))


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
