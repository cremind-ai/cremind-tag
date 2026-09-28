"""Noise_IK_25519_ChaChaPoly_SHA256 (Noise spec rev. 34), both roles.

Only the IK pattern this protocol uses::

    <- s
    ...
    -> e, es, s, ss
    <- e, ee, se

Checked against the cacophony test vector (tests/secure/test_noise.py). The
firmware runs the generated Noise* code; ``protocol/fixtures/v2_noise.json``
carries deterministic handshakes both must reproduce byte for byte.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cremind_tag.protocol.ids import V2_NOISE_PROTOCOL, V2_PROLOGUE

from . import identity

DHLEN = 32
HASHLEN = 32
TAGLEN = 16
MAX_MESSAGE = 65535
_MAX_NONCE = 2 ** 64 - 1


class NoiseError(Exception):
    """Handshake or transport failure (bad MAC, wrong state, bad length)."""


def prologue(link: int, dev_id: bytes) -> bytes:
    """``"cremind-tag/v2" | link u8 | device_id`` (connect-setup.md 3.2)."""
    return V2_PROLOGUE + bytes([int(link)]) + bytes(dev_id)


def _hkdf(ck: bytes, ikm: bytes, outputs: int) -> tuple[bytes, ...]:
    temp = hmac.new(ck, ikm, hashlib.sha256).digest()
    out1 = hmac.new(temp, b"\x01", hashlib.sha256).digest()
    out2 = hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
    if outputs == 2:
        return out1, out2
    out3 = hmac.new(temp, out2 + b"\x03", hashlib.sha256).digest()
    return out1, out2, out3


class CipherState:
    def __init__(self, key: bytes | None = None):
        self.k = key
        self.n = 0

    def has_key(self) -> bool:
        return self.k is not None

    @staticmethod
    def _nonce(n: int) -> bytes:
        return b"\x00\x00\x00\x00" + n.to_bytes(8, "little")

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self.k is None:
            return bytes(plaintext)
        if self.n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        out = ChaCha20Poly1305(self.k).encrypt(self._nonce(self.n), bytes(plaintext), bytes(ad))
        self.n += 1
        return out

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self.k is None:
            return bytes(ciphertext)
        if self.n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        try:
            out = ChaCha20Poly1305(self.k).decrypt(self._nonce(self.n), bytes(ciphertext), bytes(ad))
        except Exception:  # noqa: BLE001 - InvalidTag and friends
            raise NoiseError("decryption failed") from None
        self.n += 1
        return out


class SymmetricState:
    def __init__(self, protocol_name: bytes = V2_NOISE_PROTOCOL):
        if len(protocol_name) <= HASHLEN:
            self.h = protocol_name + b"\x00" * (HASHLEN - len(protocol_name))
        else:
            self.h = hashlib.sha256(protocol_name).digest()
        self.ck = self.h
        self.cs = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp = _hkdf(self.ck, ikm, 2)
        self.cs = CipherState(temp[:32])

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + bytes(data)).digest()

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cs.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cs.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1[:32]), CipherState(k2[:32])


class Session:
    """A finished handshake: ``send``/``recv`` transport ciphers and the handshake hash."""

    def __init__(self, send: CipherState, recv: CipherState, h: bytes, remote_static: bytes):
        self._send = send
        self._recv = recv
        self.handshake_hash = h
        self.remote_static = remote_static

    def encrypt(self, plaintext: bytes) -> bytes:
        if len(plaintext) + TAGLEN > MAX_MESSAGE:
            raise NoiseError("message too long")
        return self._send.encrypt_with_ad(b"", plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) < TAGLEN:
            raise NoiseError("message too short")
        return self._recv.decrypt_with_ad(b"", ciphertext)


EphemeralSource = Callable[[], bytes]


def _random_ephemeral() -> bytes:
    return identity.x25519_generate()[0]


class Initiator:
    """IK initiator: knows the responder's static key before the first message."""

    def __init__(self, static_priv: bytes, remote_static: bytes, prologue_bytes: bytes,
                 *, ephemeral: EphemeralSource = _random_ephemeral):
        self.s = static_priv
        self.s_pub = identity.x25519_public(static_priv)
        self.rs = bytes(remote_static)
        self.ss = SymmetricState()
        self.ss.mix_hash(prologue_bytes)
        self.ss.mix_hash(self.rs)
        self._ephemeral = ephemeral
        self.e: bytes | None = None
        self._done = False

    def write_message1(self, payload: bytes = b"") -> bytes:
        if self.e is not None:
            raise NoiseError("message 1 already written")
        self.e = self._ephemeral()
        e_pub = identity.x25519_public(self.e)
        self.ss.mix_hash(e_pub)
        self.ss.mix_key(_dh(self.e, self.rs))
        enc_s = self.ss.encrypt_and_hash(self.s_pub)
        self.ss.mix_key(_dh(self.s, self.rs))
        return e_pub + enc_s + self.ss.encrypt_and_hash(payload)

    def read_message2(self, message: bytes) -> tuple[bytes, Session]:
        if self.e is None or self._done:
            raise NoiseError("unexpected message 2")
        if len(message) < DHLEN + TAGLEN:
            raise NoiseError("message 2 too short")
        re = bytes(message[:DHLEN])
        self.ss.mix_hash(re)
        self.ss.mix_key(_dh(self.e, re))
        self.ss.mix_key(_dh(self.s, re))
        payload = self.ss.decrypt_and_hash(message[DHLEN:])
        c1, c2 = self.ss.split()
        self._done = True
        return payload, Session(c1, c2, self.ss.h, self.rs)


class Responder:
    """IK responder: a device with its identity key."""

    def __init__(self, static_priv: bytes, prologue_bytes: bytes, *, ephemeral: EphemeralSource = _random_ephemeral):
        self.s = static_priv
        self.ss = SymmetricState()
        self.ss.mix_hash(prologue_bytes)
        self.ss.mix_hash(identity.x25519_public(static_priv))
        self._ephemeral = ephemeral
        self.re: bytes | None = None
        self.rs: bytes | None = None

    def read_message1(self, message: bytes) -> bytes:
        if self.re is not None:
            raise NoiseError("message 1 already read")
        if len(message) < DHLEN + DHLEN + TAGLEN + TAGLEN:
            raise NoiseError("message 1 too short")
        self.re = bytes(message[:DHLEN])
        self.ss.mix_hash(self.re)
        self.ss.mix_key(_dh(self.s, self.re))
        self.rs = self.ss.decrypt_and_hash(message[DHLEN:DHLEN + DHLEN + TAGLEN])
        self.ss.mix_key(_dh(self.s, self.rs))
        return self.ss.decrypt_and_hash(message[DHLEN + DHLEN + TAGLEN:])

    def write_message2(self, payload: bytes = b"") -> tuple[bytes, Session]:
        if self.re is None or self.rs is None:
            raise NoiseError("message 1 not read")
        e = self._ephemeral()
        e_pub = identity.x25519_public(e)
        self.ss.mix_hash(e_pub)
        self.ss.mix_key(_dh(e, self.re))
        self.ss.mix_key(_dh(e, self.rs))
        out = e_pub + self.ss.encrypt_and_hash(payload)
        c1, c2 = self.ss.split()
        return out, Session(c2, c1, self.ss.h, self.rs)


def _dh(priv: bytes, pub: bytes) -> bytes:
    try:
        return identity.x25519(priv, pub)
    except Exception:  # noqa: BLE001 - low-order point / bad length
        raise NoiseError("invalid public key") from None
