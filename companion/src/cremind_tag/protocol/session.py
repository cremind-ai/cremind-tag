"""Bridge <-> tag session cryptography (docs/protocol.md §5.4–§5.5).

Key schedule::

    K_epoch = HKDF-SHA256(IKM = tag_secret, salt = "cremind-tag/v1/epoch",
                          info = "K_epoch" | tag_id u32 LE | epoch u32 LE, L = 16)
    th      = SHA-256(HELLO | CHALLENGE)          (reassembled, type byte included)
    mac_b   = HMAC-SHA256(K_epoch, "B" | th)[0:16]
    mac_t   = HMAC-SHA256(K_epoch, "T" | th | mac_b)[0:16]
    k_b2t | k_t2b = HKDF-SHA256(IKM = K_epoch, salt = th, info = "cremind-tag/v1/session", L = 32)

Records: ``type u8 | counter u32 LE | AES-128-CCM(ciphertext) | mic[8]`` with
AAD ``type | counter`` and the 13-byte nonce ``dir | counter u32 LE | 8 x 0x00``.
Counters start at 0 per session and direction and increase by exactly one.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .ids import (
    CRYPTO_HKDF_INFO_EPOCH_PREFIX,
    CRYPTO_HKDF_INFO_SESSION,
    CRYPTO_HKDF_SALT_EPOCH,
    CRYPTO_MAC_LABEL_BRIDGE,
    CRYPTO_MAC_LABEL_TAG,
    TAG_KEY_LEN,
    TAG_MAC_LEN,
    TAG_RECORD_MIC_LEN,
    TAG_RECORD_PAYLOAD_MAX,
    TAG_RECORD_WIRE_MAX,
    TAG_SECRET_LEN,
    RecordDir,
    Status,
)

RECORD_HEADER_LEN = 5
NONCE_LEN = 13
_HEADER = struct.Struct("<BI")


class AuthError(ValueError):
    """MAC, MIC or counter check failed; the session aborts with AUTH_FAILED."""

    status = Status.AUTH_FAILED


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def derive_k_epoch(tag_secret: bytes, tag_id: int, epoch: int) -> bytes:
    """Per-(tag, epoch) authorization key sent to the assigned bridge."""
    if len(tag_secret) != TAG_SECRET_LEN:
        raise ValueError(f"tag secret must be {TAG_SECRET_LEN} bytes")
    info = CRYPTO_HKDF_INFO_EPOCH_PREFIX + struct.pack("<II", tag_id, epoch)
    return _hkdf(tag_secret, CRYPTO_HKDF_SALT_EPOCH, info, TAG_KEY_LEN)


def transcript_hash(hello: bytes, challenge: bytes) -> bytes:
    """th over the reassembled HELLO and CHALLENGE messages (type bytes included)."""
    return hashlib.sha256(hello + challenge).digest()


def mac_b(k_epoch: bytes, th: bytes) -> bytes:
    return hmac.new(k_epoch, CRYPTO_MAC_LABEL_BRIDGE + th, hashlib.sha256).digest()[:TAG_MAC_LEN]


def mac_t(k_epoch: bytes, th: bytes, bridge_mac: bytes) -> bytes:
    return hmac.new(k_epoch, CRYPTO_MAC_LABEL_TAG + th + bridge_mac, hashlib.sha256).digest()[:TAG_MAC_LEN]


def constant_time_equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def verify_mac_b(k_epoch: bytes, th: bytes, received: bytes) -> None:
    if not constant_time_equal(mac_b(k_epoch, th), received):
        raise AuthError("mac_b mismatch")


def verify_mac_t(k_epoch: bytes, th: bytes, bridge_mac: bytes, received: bytes) -> None:
    if not constant_time_equal(mac_t(k_epoch, th, bridge_mac), received):
        raise AuthError("mac_t mismatch")


def session_keys(k_epoch: bytes, th: bytes) -> tuple[bytes, bytes]:
    """Return (k_b2t, k_t2b)."""
    okm = _hkdf(k_epoch, th, CRYPTO_HKDF_INFO_SESSION, 32)
    return okm[:16], okm[16:]


def record_nonce(direction: RecordDir, counter: int) -> bytes:
    return struct.pack("<BI", direction, counter) + bytes(NONCE_LEN - 5)


def seal_record(key: bytes, direction: RecordDir, record_type: int, counter: int, plaintext: bytes) -> bytes:
    if len(plaintext) > TAG_RECORD_PAYLOAD_MAX:
        raise ValueError(f"plaintext of {len(plaintext)} bytes exceeds {TAG_RECORD_PAYLOAD_MAX}")
    header = _HEADER.pack(record_type, counter)
    sealed = AESCCM(key, tag_length=TAG_RECORD_MIC_LEN).encrypt(record_nonce(direction, counter), plaintext, header)
    return header + sealed


def open_record(key: bytes, direction: RecordDir, record: bytes, expected_counter: int) -> tuple[int, bytes]:
    """Authenticate and decrypt a record; return (type, plaintext)."""
    if not RECORD_HEADER_LEN + TAG_RECORD_MIC_LEN <= len(record) <= TAG_RECORD_WIRE_MAX:
        raise AuthError(f"record of {len(record)} bytes")
    record_type, counter = _HEADER.unpack_from(record)
    if counter != expected_counter:
        raise AuthError(f"counter {counter}, expected {expected_counter}")
    header = record[:RECORD_HEADER_LEN]
    try:
        plaintext = AESCCM(key, tag_length=TAG_RECORD_MIC_LEN).decrypt(
            record_nonce(direction, counter), record[RECORD_HEADER_LEN:], header)
    except InvalidTag:
        raise AuthError("record MIC mismatch") from None
    return record_type, plaintext


class RecordSender:
    """Seals records for one direction, numbering them from 0."""

    def __init__(self, key: bytes, direction: RecordDir) -> None:
        self.key = key
        self.direction = direction
        self.counter = 0

    def seal(self, record_type: int, plaintext: bytes) -> bytes:
        record = seal_record(self.key, self.direction, record_type, self.counter, plaintext)
        self.counter += 1
        return record


class RecordReceiver:
    """Opens records for one direction, requiring counters 0, 1, 2, ..."""

    def __init__(self, key: bytes, direction: RecordDir) -> None:
        self.key = key
        self.direction = direction
        self.counter = 0

    def open(self, record: bytes) -> tuple[int, bytes]:
        opened = open_record(self.key, self.direction, record, self.counter)
        self.counter += 1
        return opened
