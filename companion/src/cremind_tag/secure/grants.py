"""Grants: the server-signed authorisation of one ownership change (connect-setup.md 3.1).

::

    grant = canonical CBOR {0: 2, 1: op, 2: device_id, 3: role, 4: authority_pub, 5: owner,
                            6: controller, 7: gen_from, 8: gen_to, 9: challenge}
    sig   = Ed25519(authority_sk, "cremind-tag/v2/grant" | grant)

:func:`check_grant` applies the device-side rules in order; the simulator's
devices use it and the firmware must refuse exactly the same inputs (the v2
fixtures carry one case per rule).
"""

from __future__ import annotations

from dataclasses import dataclass

import cbor2

from cremind_tag.protocol.ids import (
    CHALLENGE_LEN,
    DEVICE_ID_LEN,
    GRANT_MAX,
    GRANT_SIG_LEN,
    OWNER_LEN,
    V2_GRANT,
    GrantOp,
    NodeRole,
    OwnerState,
    Status,
)

from . import identity

GRANT_VERSION = 2
_KEYS = tuple(range(10))
_FIRST_OWNERSHIP = {NodeRole.GATEWAY: GrantOp.CLAIM, NodeRole.BRIDGE: GrantOp.PAIR, NodeRole.TAG: GrantOp.PAIR}
_OWNED_OPS = {
    NodeRole.GATEWAY: {GrantOp.RECOVER, GrantOp.RELEASE},
    NodeRole.BRIDGE: {GrantOp.REKEY, GrantOp.RELEASE, GrantOp.MAINT},
    NodeRole.TAG: {GrantOp.REKEY, GrantOp.RELEASE},
}


class GrantError(ValueError):
    """A grant is malformed (not a canonical v2 grant)."""


@dataclass(frozen=True, slots=True)
class Grant:
    op: GrantOp
    device_id: bytes
    role: NodeRole
    authority_pub: bytes
    owner: bytes
    controller: bytes
    gen_from: int
    gen_to: int
    challenge: bytes

    def encode(self) -> bytes:
        for name, value, size in (("device_id", self.device_id, DEVICE_ID_LEN),
                                  ("authority_pub", self.authority_pub, 32), ("owner", self.owner, OWNER_LEN),
                                  ("controller", self.controller, 32), ("challenge", self.challenge, CHALLENGE_LEN)):
            if len(value) != size:
                raise GrantError(f"{name} must be {size} bytes")
        for name, value in (("gen_from", self.gen_from), ("gen_to", self.gen_to)):
            if not 0 <= value <= 0xFFFFFFFF:
                raise GrantError(f"{name} must be a u32")
        return cbor2.dumps({
            0: GRANT_VERSION, 1: int(self.op), 2: bytes(self.device_id), 3: int(self.role),
            4: bytes(self.authority_pub), 5: bytes(self.owner), 6: bytes(self.controller),
            7: self.gen_from, 8: self.gen_to, 9: bytes(self.challenge),
        }, canonical=True)

    @classmethod
    def decode(cls, raw: bytes) -> Grant:
        if not raw or len(raw) > GRANT_MAX:
            raise GrantError("grant size out of bounds")
        try:
            value = cbor2.loads(raw)
        except Exception as exc:  # noqa: BLE001 - any decoder failure is a malformed grant
            raise GrantError(f"not CBOR: {exc}") from None
        if not isinstance(value, dict) or tuple(sorted(value)) != _KEYS:
            raise GrantError("a grant has exactly the keys 0..9")
        if value[0] != GRANT_VERSION:
            raise GrantError("unsupported grant version")
        ints = (1, 3, 7, 8)
        blobs = {2: DEVICE_ID_LEN, 4: 32, 5: OWNER_LEN, 6: 32, 9: CHALLENGE_LEN}
        if any(not isinstance(value[k], int) or isinstance(value[k], bool) for k in ints):
            raise GrantError("integer field has another type")
        if any(not isinstance(value[k], bytes) or len(value[k]) != n for k, n in blobs.items()):
            raise GrantError("byte-string field has another type or length")
        try:
            op, role = GrantOp(value[1]), NodeRole(value[3])
        except ValueError:
            raise GrantError("unknown op or role") from None
        grant = cls(op, value[2], role, value[4], value[5], value[6], value[7], value[8], value[9])
        if grant.encode() != bytes(raw):
            raise GrantError("the grant is not in canonical form")
        return grant


def signed_message(grant_bytes: bytes) -> bytes:
    return V2_GRANT + bytes(grant_bytes)


def sign(grant_bytes: bytes, authority_sk: bytes) -> bytes:
    return identity.ed25519_sign(authority_sk, signed_message(grant_bytes))


@dataclass(slots=True)
class DeviceOwnership:
    """What a device has pinned (its ownership record, connect-setup.md 4.1)."""

    role: NodeRole
    device_id: bytes
    state: OwnerState = OwnerState.UNOWNED
    gen: int = 0
    authority_pub: bytes = b""
    owner: bytes = b""
    controller: bytes = b""


def check_grant(own: DeviceOwnership, grant_bytes: bytes, sig: bytes, *, challenge: bytes | None,
                session_controller: bytes, expected_ops: set[GrantOp] | frozenset[GrantOp],
                setup_proof_ok: bool | None = None) -> tuple[Status, Grant | None]:
    """Apply the device rules (connect-setup.md 3.1) in order.

    ``challenge`` is the device's current challenge (``None`` = none drawn).
    ``expected_ops`` are the ops the calling message carries (``CLAIM`` for the
    CLAIM message, ``REKEY`` for REKEY, …). ``setup_proof_ok`` is the result of
    the first-pairing setup proof for bridges and tags (checked by the caller,
    which knows the handshake hash). Returns the status and the parsed grant.
    """
    try:
        grant = Grant.decode(grant_bytes)
    except GrantError:
        return Status.GRANT_INVALID, None
    if len(sig) != GRANT_SIG_LEN:
        return Status.GRANT_INVALID, None
    if grant.device_id != own.device_id or grant.role != own.role or grant.op not in expected_ops:
        return Status.GRANT_INVALID, grant
    if challenge is None or not identity.equal(grant.challenge, challenge):
        return Status.GRANT_INVALID, grant
    if grant.gen_from != own.gen or grant.gen_to != grant.gen_from + 1:
        return Status.STALE_GENERATION, grant
    if not identity.equal(grant.controller, session_controller):
        return Status.GRANT_INVALID, grant
    if not identity.ed25519_verify(grant.authority_pub, sig, signed_message(grant_bytes)):
        return Status.GRANT_INVALID, grant
    if own.state == OwnerState.OWNED:
        if grant.op not in _OWNED_OPS[own.role]:
            return Status.NOT_OWNER, grant
        if not identity.equal(grant.authority_pub, own.authority_pub) or not identity.equal(grant.owner, own.owner):
            return Status.NOT_OWNER, grant
    else:
        if grant.op != _FIRST_OWNERSHIP[own.role]:
            return Status.NOT_OWNER, grant
        if own.role != NodeRole.GATEWAY and not setup_proof_ok:
            return Status.PROOF_FAILED, grant
    return Status.OK, grant
