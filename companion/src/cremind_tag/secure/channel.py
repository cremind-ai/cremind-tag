"""The worker (initiator) side of a v2 secure session (connect-setup.md 3.2-3.6).

:class:`SecureChannel` runs Noise IK toward one device, then seals and opens
secure messages. The helpers build the proofs the v2 messages carry, so a
caller never handles ``h`` or ``k_setup`` itself.
"""

from __future__ import annotations

from typing import Any

from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import SerialFlag, SerialMsg

from . import identity, noise
from .messages import SecureMessage


class SecureChannelError(Exception):
    """The session failed (handshake, decryption, a bad proof)."""


class SecureChannel:
    def __init__(self, controller_priv: bytes, device_ik: bytes, device_id: bytes, link: int,
                 *, ephemeral: noise.EphemeralSource | None = None):
        kwargs = {"ephemeral": ephemeral} if ephemeral else {}
        self._init = noise.Initiator(controller_priv, device_ik, noise.prologue(link, device_id), **kwargs)
        self.device_id = bytes(device_id)
        self.device_ik = bytes(device_ik)
        self.controller_pub = identity.x25519_public(controller_priv)
        self._session: noise.Session | None = None
        self._next_request = 1

    # ---- handshake ----

    def message1(self) -> bytes:
        return self._init.write_message1(b"")

    def finish(self, message2: bytes) -> None:
        try:
            _payload, self._session = self._init.read_message2(message2)
        except noise.NoiseError as exc:
            raise SecureChannelError(f"handshake failed: {exc}") from None

    @property
    def open(self) -> bool:
        return self._session is not None

    @property
    def handshake_hash(self) -> bytes:
        if self._session is None:
            raise SecureChannelError("no session")
        return self._session.handshake_hash

    # ---- transport ----

    def next_request_id(self) -> int:
        rid = self._next_request
        self._next_request = rid % 0xFFFF + 1
        return rid

    def seal(self, msg: SecureMessage) -> bytes:
        if self._session is None:
            raise SecureChannelError("no session")
        return self._session.encrypt(msg.pack())

    def seal_request(self, mtype: SerialMsg | int, fields: dict[str, Any], request_id: int | None = None
                     ) -> tuple[int, bytes]:
        rid = self.next_request_id() if request_id is None else request_id
        payload = cbor_msgs.encode_request(SerialMsg(mtype), fields) if fields else b""
        return rid, self.seal(SecureMessage(int(mtype), 0, rid, payload))

    def unseal(self, ciphertext: bytes) -> SecureMessage:
        if self._session is None:
            raise SecureChannelError("no session")
        try:
            return SecureMessage.unpack(self._session.decrypt(ciphertext))
        except noise.NoiseError as exc:
            self._session = None
            raise SecureChannelError(f"transport failed: {exc}") from None

    @staticmethod
    def decode(msg: SecureMessage) -> dict[str, Any]:
        mtype = SerialMsg(msg.type)
        if msg.flags & SerialFlag.RESPONSE:
            return cbor_msgs.decode_response(mtype, msg.payload)
        if msg.flags & SerialFlag.EVENT:
            return cbor_msgs.decode_event(mtype, msg.payload)
        return cbor_msgs.decode_request(mtype, msg.payload)

    # ---- proofs ----

    def setup_proof(self, setup_secret: bytes, grant: bytes) -> tuple[bytes, bytes]:
        """``(proof_s, k_setup)`` for PAIR; keep ``k_setup`` to check ``proof_d``."""
        k_set = identity.k_setup(setup_secret, self.device_id)
        return identity.proof_s(k_set, self.handshake_hash, grant), k_set

    def check_device_proof(self, k_set: bytes, proof_s: bytes, proof_d: bytes) -> bool:
        return identity.equal(identity.proof_d(k_set, self.handshake_hash, proof_s), proof_d)

    def root_proof(self, root: bytes) -> bytes:
        return identity.root_proof(root, self.handshake_hash)

    def maint_proof(self, mk: bytes) -> bytes:
        return identity.maint_proof(mk, self.handshake_hash)
