"""The device side of a v2 secure endpoint (connect-setup.md 4-5).

:class:`SecureDevice` is what a gateway, a bridge and a tag run behind
``IDENTIFY`` / ``IDENT``, ``SECURE_OPEN`` and ``SECURE_DATA`` (or a tunnel):
ownership record, single-use challenges, the Noise responder, and the v2
secure messages (``STATUS``, ``CLAIM``, ``RECOVER``, ``RELEASE``, ``PAIR``,
``REKEY``, ``MAINT_AUTH``, ``RECOMMISSION``). The simulator's devices run it;
the firmware implements the same rules (the v2 fixtures pin them).

Persistence is the caller's: ``on_persist(record)`` is called with the new
record *before* any answer that depends on it is produced, mirroring the
firmware's "persist, then acknowledge".
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from cremind_tag.protocol.ids import (
    CHALLENGE_LEN,
    OP_KEY_LEN,
    SECURE_MAX_FAILURES,
    SECURE_PROTO_VERSION,
    SETUP_SECRET_LEN,
    GrantOp,
    Link,
    NodeRole,
    OwnerState,
    SerialMsg,
    Status,
)
from cremind_tag.protocol.msgs import Ident2

from . import identity, noise
from .codes import SetupPayload
from .grants import DeviceOwnership, check_grant

Rng = Callable[[int], bytes]


@dataclass(frozen=True, slots=True)
class DeviceKeys:
    """Factory state that never changes: role, identity key, label secret."""

    role: NodeRole
    ik_priv: bytes
    factory_secret: bytes | None = None  # bridges and tags
    board: int = 0
    fw: tuple[int, int, int] = (0, 2, 0)

    @property
    def ik_pub(self) -> bytes:
        return identity.x25519_public(self.ik_priv)

    @property
    def device_id(self) -> bytes:
        return identity.device_id(self.role, self.ik_pub)

    @property
    def short_id(self) -> int:
        return identity.short_id(self.device_id)

    def setup_payload(self) -> SetupPayload | None:
        if self.factory_secret is None:
            return None
        return SetupPayload(self.role, self.short_id, self.factory_secret)

    @classmethod
    def generate(cls, role: NodeRole, *, board: int = 0, rng: Rng = os.urandom) -> DeviceKeys:
        priv = identity.x25519_generate()[0] if rng is os.urandom else _clamp(rng(32))
        secret = rng(SETUP_SECRET_LEN) if role in (NodeRole.BRIDGE, NodeRole.TAG) else None
        return cls(role, priv, secret, board)

    def to_json(self) -> dict[str, Any]:
        return {"role": int(self.role), "ik_priv": self.ik_priv.hex(),
                "factory_secret": self.factory_secret.hex() if self.factory_secret else None,
                "board": self.board, "fw": list(self.fw)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeviceKeys:
        secret = data.get("factory_secret")
        return cls(NodeRole(data["role"]), bytes.fromhex(data["ik_priv"]),
                   bytes.fromhex(secret) if secret else None, int(data.get("board", 0)),
                   tuple(data.get("fw", (0, 2, 0))))


def _clamp(raw: bytes) -> bytes:
    # A deterministic rng in tests; X25519 clamps on use, any 32 bytes are a key.
    return bytes(raw)


@dataclass(slots=True)
class OwnerRecord:
    """The atomic ownership record (connect-setup.md 4.1)."""

    state: OwnerState = OwnerState.UNOWNED
    gen: int = 0
    authority_pub: bytes = b""
    owner: bytes = b""
    controller: bytes = b""
    op_key: bytes = b""                    # tag root / bridge mk
    override_secret: bytes | None = None   # RELEASED: the only secret PAIR accepts
    pending_override: bytes | None = None  # tag RELEASE stage 0
    pending_controller: bytes = b""
    locked: bool = False                   # released bridge: provisioning refused until recommissioned

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["state"] = int(self.state)
        for key, value in list(out.items()):
            if isinstance(value, bytes):
                out[key] = value.hex()
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OwnerRecord:
        def b(key: str) -> bytes:
            return bytes.fromhex(data.get(key) or "")

        def ob(key: str) -> bytes | None:
            value = data.get(key)
            return bytes.fromhex(value) if value else None

        return cls(OwnerState(int(data.get("state", 0))), int(data.get("gen", 0)), b("authority_pub"),
                   b("owner"), b("controller"), b("op_key"), ob("override_secret"),
                   ob("pending_override"), b("pending_controller"), bool(data.get("locked", False)))


@dataclass(slots=True)
class _Session:
    noise: noise.Session
    controller: bytes
    link: int
    maint_ok: bool = False


@dataclass(slots=True)
class Outcome:
    """What a secure message did, for the device model around the engine."""

    status: Status
    fields: dict[str, Any] = field(default_factory=dict)
    released: bool = False        # gateway: wipe mesh + assignments; bridge: leave the mesh
    recommissioned: bool = False  # bridge: leave the mesh, fresh secret armed
    rekeyed: bool = False         # tag: every K_epoch of the old root is dead


class SecureDevice:
    def __init__(self, keys: DeviceKeys, record: OwnerRecord | None = None, *,
                 rng: Rng = os.urandom, on_persist: Callable[[OwnerRecord], None] | None = None,
                 ephemeral: noise.EphemeralSource | None = None):
        self.keys = keys
        self.record = record or OwnerRecord()
        self._rng = rng
        self._persist = on_persist or (lambda _record: None)
        self._ephemeral = ephemeral
        self.challenge: bytes | None = None
        self.session: _Session | None = None
        self.failures = 0

    # ---- identity ----

    @property
    def role(self) -> NodeRole:
        return self.keys.role

    @property
    def device_id(self) -> bytes:
        return self.keys.device_id

    def _authority_id(self) -> bytes:
        if self.record.state == OwnerState.OWNED and self.record.authority_pub:
            return identity.authority_id(self.record.authority_pub)
        return b"\x00" * 16

    def draw_challenge(self) -> bytes:
        self.challenge = self._rng(CHALLENGE_LEN)
        return self.challenge

    def ident2(self) -> Ident2:
        """The IDENT value (tags, tunnel OPEN data); draws a fresh challenge."""
        major, minor, patch = self.keys.fw
        return Ident2(SECURE_PROTO_VERSION, int(self.role), self.device_id, self.keys.ik_pub,
                      int(self.record.state), self.record.gen, self._authority_id(),
                      self.draw_challenge(), self.keys.board, major, minor, patch)

    def identify_fields(self, *, fw: str, build: str) -> dict[str, Any]:
        """The IDENTIFY answer (serial); draws a fresh challenge."""
        out: dict[str, Any] = {
            "status": int(Status.OK), "proto": SECURE_PROTO_VERSION, "role": int(self.role),
            "device_id": self.device_id, "ik": self.keys.ik_pub, "fw": fw, "build": build,
            "board": self.keys.board, "owner_state": int(self.record.state), "gen": self.record.gen,
            "challenge": self.draw_challenge(),
        }
        if self.record.state == OwnerState.OWNED:
            out["authority_id"] = self._authority_id()
        return out

    # ---- sessions ----

    def open_session(self, link: int, message1: bytes) -> bytes:
        """SECURE_OPEN / PAIR handshake: answer Noise message 2 (raises NoiseError)."""
        self.session = None
        kwargs = {"ephemeral": self._ephemeral} if self._ephemeral else {}
        responder = noise.Responder(self.keys.ik_priv, noise.prologue(link, self.device_id), **kwargs)
        responder.read_message1(message1)
        message2, sess = responder.write_message2(b"")
        self.session = _Session(sess, bytes(responder.rs or b""), link)
        return message2

    def close_session(self) -> None:
        self.session = None

    def controller_match(self) -> bool:
        return (self.session is not None and self.record.state == OwnerState.OWNED
                and identity.equal(self.session.controller, self.record.controller))

    def operations_allowed(self) -> bool:
        """Gateway: may this session use the v1 catalogue and the v2 operational messages?"""
        return self.controller_match()

    # ---- secure messages ----

    def handle(self, mtype: int, fields: dict[str, Any]) -> Outcome:
        if self.session is None:
            return Outcome(Status.AUTH_REQUIRED)
        handler = {
            SerialMsg.STATUS: self._status, SerialMsg.CLAIM: self._claim, SerialMsg.RECOVER: self._recover,
            SerialMsg.RELEASE: self._release, SerialMsg.PAIR: self._pair, SerialMsg.REKEY: self._rekey,
            SerialMsg.MAINT_AUTH: self._maint_auth, SerialMsg.RECOMMISSION: self._recommission,
        }.get(mtype)  # type: ignore[call-overload]  # IntEnum keys hash like their ints
        if handler is None:
            return Outcome(Status.UNSUPPORTED)
        return handler(fields)

    def _ownership(self) -> DeviceOwnership:
        r = self.record
        return DeviceOwnership(self.role, self.device_id, r.state, r.gen, r.authority_pub, r.owner, r.controller)

    def _check(self, fields: dict[str, Any], ops: set[GrantOp], *, setup_ok: bool | None = None):
        grant_bytes, sig = fields.get("grant"), fields.get("sig")
        if not isinstance(grant_bytes, bytes) or not isinstance(sig, bytes):
            return Status.INVALID, None
        assert self.session is not None
        status, grant = check_grant(self._ownership(), grant_bytes, sig, challenge=self.challenge,
                                    session_controller=self.session.controller, expected_ops=ops,
                                    setup_proof_ok=setup_ok)
        self.challenge = None  # single use, whatever the outcome
        return status, grant

    def _commit(self, record: OwnerRecord) -> None:
        self._persist(record)
        self.record = record

    def _status(self, _fields: dict[str, Any]) -> Outcome:
        r = self.record
        out: dict[str, Any] = {"owner_state": int(r.state), "gen": r.gen,
                               "controller_match": self.controller_match(), "challenge": self.draw_challenge()}
        if r.state == OwnerState.OWNED:
            out["authority_id"] = self._authority_id()
            if self.controller_match():  # the owner (a profile id) only to the pinned controller
                out["owner"] = r.owner
            if self.role == NodeRole.TAG and len(r.op_key) == OP_KEY_LEN and self.session is not None:
                out["root_proof"] = identity.root_proof(r.op_key, self.session.noise.handshake_hash)
        return Outcome(Status.OK, out)

    def _owned_record(self, grant, *, op_key: bytes = b"") -> OwnerRecord:
        return OwnerRecord(OwnerState.OWNED, grant.gen_to, grant.authority_pub, grant.owner, grant.controller,
                           op_key)

    def _claim(self, fields: dict[str, Any]) -> Outcome:
        if self.role != NodeRole.GATEWAY:
            return Outcome(Status.UNSUPPORTED)
        status, grant = self._check(fields, {GrantOp.CLAIM})
        if status != Status.OK:
            return Outcome(status)
        self._commit(self._owned_record(grant))
        return Outcome(Status.OK, {"gen": self.record.gen})

    def _recover(self, fields: dict[str, Any]) -> Outcome:
        if self.role != NodeRole.GATEWAY:
            return Outcome(Status.UNSUPPORTED)
        if self.record.state != OwnerState.OWNED:
            return Outcome(Status.NOT_OWNER)
        status, grant = self._check(fields, {GrantOp.RECOVER})
        if status != Status.OK:
            return Outcome(status)
        self._commit(replace(self.record, gen=grant.gen_to, controller=grant.controller))
        return Outcome(Status.OK, {"gen": self.record.gen})

    def _setup_secret(self) -> bytes | None:
        r = self.record
        if r.state == OwnerState.RELEASED:
            return r.override_secret
        if r.state == OwnerState.UNOWNED:
            return self.keys.factory_secret
        return None

    def _pair(self, fields: dict[str, Any]) -> Outcome:
        if self.role == NodeRole.GATEWAY:
            return Outcome(Status.UNSUPPORTED)
        if self.record.state == OwnerState.OWNED:
            self.challenge = None
            return Outcome(Status.NOT_OWNER)
        secret = self._setup_secret()
        op_key, proof = fields.get("op_key"), fields.get("proof")
        if secret is None or self.record.locked:
            self.challenge = None
            return Outcome(Status.LOCKED)
        if not isinstance(op_key, bytes) or len(op_key) != OP_KEY_LEN or not isinstance(proof, bytes):
            self.challenge = None
            return Outcome(Status.INVALID)
        assert self.session is not None
        h = self.session.noise.handshake_hash
        k_set = identity.k_setup(secret, self.device_id)
        grant_bytes = fields.get("grant") if isinstance(fields.get("grant"), bytes) else b""
        expected = identity.proof_s(k_set, h, grant_bytes)
        setup_ok = identity.equal(expected, proof)
        status, grant = self._check(fields, {GrantOp.PAIR}, setup_ok=setup_ok)
        if status == Status.PROOF_FAILED:
            self.failures += 1
            return Outcome(Status.PROOF_FAILED)
        if status != Status.OK:
            return Outcome(status)
        self.failures = 0
        self._commit(self._owned_record(grant, op_key=bytes(op_key)))
        return Outcome(Status.OK, {"gen": self.record.gen, "proof": identity.proof_d(k_set, h, proof)})

    def _rekey(self, fields: dict[str, Any]) -> Outcome:
        if self.role == NodeRole.GATEWAY:
            return Outcome(Status.UNSUPPORTED)
        if self.record.state != OwnerState.OWNED:
            self.challenge = None
            return Outcome(Status.NOT_OWNER)
        op_key = fields.get("op_key")
        if not isinstance(op_key, bytes) or len(op_key) != OP_KEY_LEN:
            self.challenge = None
            return Outcome(Status.INVALID)
        status, grant = self._check(fields, {GrantOp.REKEY})
        if status != Status.OK:
            return Outcome(status)
        # A rekey abandons a tag release in progress: its stage 1 can never follow.
        self._commit(replace(self.record, gen=grant.gen_to, controller=grant.controller, op_key=bytes(op_key),
                             pending_override=None, pending_controller=b""))
        return Outcome(Status.OK, {"gen": self.record.gen}, rekeyed=self.role == NodeRole.TAG)

    def _release(self, fields: dict[str, Any]) -> Outcome:
        if self.record.state != OwnerState.OWNED:
            self.challenge = None
            return Outcome(Status.NOT_OWNER)
        stage = fields.get("release_stage", 1)
        if self.role == NodeRole.TAG and stage == 0:
            status, grant = self._check(fields, {GrantOp.RELEASE})
            if status != Status.OK:
                return Outcome(status)
            fresh = self._rng(SETUP_SECRET_LEN)
            self._commit(replace(self.record, pending_override=fresh, pending_controller=grant.controller))
            payload = SetupPayload(self.role, self.keys.short_id, fresh).pack()
            return Outcome(Status.OK, {"gen": self.record.gen, "data": payload})
        if stage not in (0, 1):
            self.challenge = None
            return Outcome(Status.INVALID)
        status, grant = self._check(fields, {GrantOp.RELEASE})
        if status != Status.OK:
            return Outcome(status)
        if self.role == NodeRole.TAG:
            r = self.record
            if r.pending_override is None or not identity.equal(r.pending_controller, grant.controller):
                return Outcome(Status.INVALID)  # stage 0 first, by the same controller
            self._commit(OwnerRecord(OwnerState.RELEASED, grant.gen_to, override_secret=r.pending_override))
            return Outcome(Status.OK, {"gen": self.record.gen, "data": b""}, released=True)
        if self.role == NodeRole.BRIDGE:
            # Removed bridges stay locked until recommissioned over local USB.
            self._commit(OwnerRecord(OwnerState.RELEASED, grant.gen_to, locked=True))
        else:
            self._commit(OwnerRecord(OwnerState.UNOWNED, grant.gen_to))
        return Outcome(Status.OK, {"gen": self.record.gen, "data": b""}, released=True)

    def _maint_auth(self, fields: dict[str, Any]) -> Outcome:
        if self.role != NodeRole.BRIDGE:
            return Outcome(Status.UNSUPPORTED)
        assert self.session is not None
        if self.session.link != Link.SERIAL:
            return Outcome(Status.NOT_OWNER)  # the maintenance port only, never over the mesh
        proof = fields.get("proof")
        if self.record.state != OwnerState.OWNED or len(self.record.op_key) != OP_KEY_LEN:
            return Outcome(Status.NOT_OWNER)
        if not isinstance(proof, bytes) or not identity.equal(
                proof, identity.maint_proof(self.record.op_key, self.session.noise.handshake_hash)):
            self.failures += 1
            return Outcome(Status.PROOF_FAILED)
        self.failures = 0
        self.session.maint_ok = True
        return Outcome(Status.OK)

    def _recommission(self, fields: dict[str, Any]) -> Outcome:
        """Bridge over local USB: owned (MAINT_AUTH + MAINT grant) or released/locked (physical presence);
        an unowned bridge keeps its label (INVALID)."""
        if self.role != NodeRole.BRIDGE:
            return Outcome(Status.UNSUPPORTED)
        assert self.session is not None
        if self.session.link != Link.SERIAL:
            return Outcome(Status.NOT_OWNER)  # never over the mesh
        r = self.record
        if r.state == OwnerState.UNOWNED:
            self.challenge = None
            return Outcome(Status.INVALID)  # nothing to recommission: the label's secret stays valid
        fresh = self._rng(SETUP_SECRET_LEN)
        if r.state == OwnerState.OWNED:
            if not self.session.maint_ok:
                return Outcome(Status.NOT_OWNER)
            status, grant = self._check(fields, {GrantOp.MAINT})
            if status != Status.OK:
                return Outcome(status)
            gen = grant.gen_to
        else:
            self.challenge = None
            gen = r.gen
        self._commit(OwnerRecord(OwnerState.RELEASED, gen, override_secret=fresh))
        payload = SetupPayload(self.role, self.keys.short_id, fresh).pack()
        return Outcome(Status.OK, {"gen": gen, "data": payload}, recommissioned=True)

    # ---- tag helpers ----

    def k_epoch(self, tag_id: int, epoch: int) -> bytes | None:
        """The key a v2 tag authenticates bridges with (None when it has no root)."""
        if self.record.state != OwnerState.OWNED or len(self.record.op_key) != OP_KEY_LEN:
            return None
        return identity.k_epoch_v2(self.record.op_key, tag_id, epoch)

    def pairing_paused(self) -> bool:
        return self.failures >= SECURE_MAX_FAILURES
