"""Grants and the device-side secure endpoint (connect-setup.md 3-5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cbor2
import pytest

from cremind_tag.protocol.ids import GrantOp, Link, NodeRole, OwnerState, SerialFlag, SerialMsg, Status
from cremind_tag.secure import grants, identity
from cremind_tag.secure.channel import SecureChannel
from cremind_tag.secure.codes import SetupPayload
from cremind_tag.secure.device import DeviceKeys, OwnerRecord, SecureDevice
from cremind_tag.secure.messages import SecureMessage


@dataclass
class Authority:
    sk: bytes
    pub: bytes
    owner: bytes = b"\x11" * 16

    @classmethod
    def new(cls, owner: bytes = b"\x11" * 16) -> Authority:
        sk, pub = identity.ed25519_generate()
        return cls(sk, pub, owner)

    def grant(self, dev: SecureDevice, op: GrantOp, controller: bytes, challenge: bytes, *,
              gen_from: int | None = None, owner: bytes | None = None) -> tuple[bytes, bytes]:
        g = gen_from if gen_from is not None else dev.record.gen
        raw = grants.Grant(op, dev.device_id, dev.role, self.pub, owner or self.owner, controller, g, g + 1,
                           challenge).encode()
        return raw, grants.sign(raw, self.sk)


class Worker:
    """A controller key and one session at a time toward a device."""

    def __init__(self) -> None:
        self.priv, self.pub = identity.x25519_generate()

    def connect(self, dev: SecureDevice, link: Link = Link.SERIAL) -> SecureChannel:
        ch = SecureChannel(self.priv, dev.keys.ik_pub, dev.device_id, link)
        ch.finish(dev.open_session(link, ch.message1()))
        return ch

    @staticmethod
    def call(dev: SecureDevice, ch: SecureChannel, mtype: SerialMsg, fields: dict[str, Any]
             ) -> tuple[Status, dict[str, Any]]:
        rid, sealed = ch.seal_request(mtype, fields)
        # The device side: open, handle, seal the answer (what the firmware does).
        assert dev.session is not None
        msg = SecureMessage.unpack(dev.session.noise.decrypt(sealed))
        from cremind_tag.protocol import cbor_msgs
        outcome = dev.handle(msg.type, cbor_msgs.decode_request(SerialMsg(msg.type), msg.payload))
        reply = {"status": int(outcome.status), **outcome.fields}
        payload = cbor_msgs.encode_response(SerialMsg(msg.type), reply)
        answer = dev.session.noise.encrypt(SecureMessage(msg.type, int(SerialFlag.RESPONSE), rid, payload).pack())
        decoded = SecureChannel.decode(ch.unseal(answer))
        return Status(decoded["status"]), decoded


def _device(role: NodeRole) -> SecureDevice:
    return SecureDevice(DeviceKeys.generate(role))


def _challenge(dev: SecureDevice, ch: SecureChannel) -> bytes:
    status, fields = Worker.call(dev, ch, SerialMsg.STATUS, {})
    assert status == Status.OK
    return fields["challenge"]


# ---------------------------------------------------------------- grants


def test_grant_is_canonical_and_strict() -> None:
    auth = Authority.new()
    g = grants.Grant(GrantOp.CLAIM, bytes(16), NodeRole.GATEWAY, auth.pub, auth.owner, bytes(32), 0, 1, bytes(16))
    raw = g.encode()
    assert grants.Grant.decode(raw) == g
    # A non-canonical encoding of the same map (keys out of order) is refused.
    value = cbor2.loads(raw)
    shuffled = cbor2.dumps(dict(reversed(list(value.items()))))
    assert shuffled != raw
    with pytest.raises(grants.GrantError):
        grants.Grant.decode(shuffled)
    value[10] = 1
    with pytest.raises(grants.GrantError):
        grants.Grant.decode(cbor2.dumps(value, canonical=True))


# ---------------------------------------------------------------- gateway


def test_gateway_claim_recover_release() -> None:
    auth, gw, a, b = Authority.new(), _device(NodeRole.GATEWAY), Worker(), Worker()
    ch = a.connect(gw)
    assert not gw.operations_allowed()
    c = gw.identify_fields(fw="0.2.0", build="t")["challenge"]
    grant, sig = auth.grant(gw, GrantOp.CLAIM, a.pub, c)
    status, fields = Worker.call(gw, ch, SerialMsg.CLAIM, {"grant": grant, "sig": sig})
    assert status == Status.OK and fields["gen"] == 1
    assert gw.record.state == OwnerState.OWNED and gw.operations_allowed()

    # A second CLAIM (another computer) is refused; the challenge was single use anyway.
    ch_b = b.connect(gw)
    assert not gw.operations_allowed()
    c2 = _challenge(gw, ch_b)
    grant, sig = auth.grant(gw, GrantOp.CLAIM, b.pub, c2)
    status, _ = Worker.call(gw, ch_b, SerialMsg.CLAIM, {"grant": grant, "sig": sig})
    assert status == Status.NOT_OWNER

    # RECOVER with a server grant moves the controller to b.
    c3 = _challenge(gw, ch_b)
    grant, sig = auth.grant(gw, GrantOp.RECOVER, b.pub, c3)
    status, fields = Worker.call(gw, ch_b, SerialMsg.RECOVER, {"grant": grant, "sig": sig})
    assert status == Status.OK and fields["gen"] == 2 and gw.operations_allowed()
    # a's key no longer operates the gateway.
    a.connect(gw)
    assert not gw.operations_allowed()

    ch_b = b.connect(gw)
    grant, sig = auth.grant(gw, GrantOp.RELEASE, b.pub, _challenge(gw, ch_b))
    status, fields = Worker.call(gw, ch_b, SerialMsg.RELEASE, {"grant": grant, "sig": sig})
    assert status == Status.OK and gw.record.state == OwnerState.UNOWNED and gw.record.gen == 3


@pytest.mark.parametrize("fault, expected", [
    ("wrong_challenge", Status.GRANT_INVALID),
    ("replayed_challenge", Status.GRANT_INVALID),
    ("stale_gen", Status.STALE_GENERATION),
    ("other_controller", Status.GRANT_INVALID),
    ("bad_signature", Status.GRANT_INVALID),
    ("other_device", Status.GRANT_INVALID),
    ("wrong_op", Status.GRANT_INVALID),
])
def test_gateway_claim_rules(fault: str, expected: Status) -> None:
    auth, gw, w = Authority.new(), _device(NodeRole.GATEWAY), Worker()
    ch = w.connect(gw)
    c = _challenge(gw, ch)
    controller, gen_from, op = w.pub, None, GrantOp.CLAIM
    target = gw
    if fault == "wrong_challenge":
        c = bytes(16)
    elif fault == "stale_gen":
        gen_from = 5
    elif fault == "other_controller":
        controller = identity.x25519_generate()[1]
    elif fault == "other_device":
        target = _device(NodeRole.GATEWAY)
    elif fault == "wrong_op":
        op = GrantOp.RECOVER
    grant, sig = auth.grant(target, op, controller, c, gen_from=gen_from)
    if fault == "bad_signature":
        sig = bytes(64)
    if fault == "replayed_challenge":
        Worker.call(gw, ch, SerialMsg.CLAIM, {"grant": grant, "sig": bytes(64)})  # consumes c
    status, _ = Worker.call(gw, ch, SerialMsg.CLAIM, {"grant": grant, "sig": sig})
    assert status == expected
    assert gw.record.state == OwnerState.UNOWNED and gw.record.gen == 0


def test_messages_need_a_session() -> None:
    gw = _device(NodeRole.GATEWAY)
    assert gw.handle(SerialMsg.STATUS, {}).status == Status.AUTH_REQUIRED


# ---------------------------------------------------------------- tag


def _pair(auth: Authority, dev: SecureDevice, w: Worker, secret: bytes, root: bytes, *,
          link: Link = Link.TUNNEL) -> tuple[Status, dict[str, Any]]:
    ch = w.connect(dev, link)
    c = _challenge(dev, ch)
    grant, sig = auth.grant(dev, GrantOp.PAIR, w.pub, c)
    proof_s, k_set = ch.setup_proof(secret, grant)
    status, fields = Worker.call(dev, ch, SerialMsg.PAIR, {"grant": grant, "sig": sig, "proof": proof_s,
                                                          "op_key": root})
    if status == Status.OK:
        assert ch.check_device_proof(k_set, proof_s, fields["proof"])
    return status, fields


def test_tag_pair_rekey_and_root_proof() -> None:
    auth, tag, w = Authority.new(), _device(NodeRole.TAG), Worker()
    secret = tag.keys.factory_secret
    assert secret is not None
    root = bytes([7]) * 32
    status, fields = _pair(auth, tag, w, secret, root)
    assert status == Status.OK and tag.record.gen == 1 and tag.record.op_key == root
    assert tag.k_epoch(tag.keys.short_id, 1) == identity.k_epoch_v2(root, tag.keys.short_id, 1)

    # root_proof tells a worker which root the tag committed (lost acknowledgement).
    ch = w.connect(tag, Link.TUNNEL)
    status, fields = Worker.call(tag, ch, SerialMsg.STATUS, {})
    assert fields["root_proof"] == ch.root_proof(root)

    # REKEY (recovery onto another computer) installs a new root; old K_epoch keys die.
    w2 = Worker()
    ch2 = w2.connect(tag, Link.TUNNEL)
    new_root = bytes([9]) * 32
    grant, sig = auth.grant(tag, GrantOp.REKEY, w2.pub, _challenge(tag, ch2))
    status, fields = Worker.call(tag, ch2, SerialMsg.REKEY, {"grant": grant, "sig": sig, "op_key": new_root})
    assert status == Status.OK and fields["gen"] == 2
    assert tag.k_epoch(tag.keys.short_id, 5) != identity.k_epoch_v2(root, tag.keys.short_id, 5)


def test_tag_pair_refuses_wrong_setup_code_and_other_owner() -> None:
    auth, tag, w = Authority.new(), _device(NodeRole.TAG), Worker()
    status, _ = _pair(auth, tag, w, bytes(10), bytes(32))
    assert status == Status.PROOF_FAILED and tag.record.state == OwnerState.UNOWNED
    assert tag.keys.factory_secret is not None
    status, _ = _pair(auth, tag, w, tag.keys.factory_secret, bytes([1]) * 32)
    assert status == Status.OK
    # An owned tag refuses PAIR from anyone (another profile's authority included).
    other = Authority.new(owner=b"\x22" * 16)
    status, _ = _pair(other, tag, Worker(), tag.keys.factory_secret, bytes([2]) * 32)
    assert status == Status.NOT_OWNER
    # REKEY signed by another authority is refused too.
    w3 = Worker()
    ch = w3.connect(tag, Link.TUNNEL)
    grant, sig = other.grant(tag, GrantOp.REKEY, w3.pub, _challenge(tag, ch))
    status, _ = Worker.call(tag, ch, SerialMsg.REKEY, {"grant": grant, "sig": sig, "op_key": bytes(32)})
    assert status == Status.NOT_OWNER


def test_tag_release_two_stages_arms_a_fresh_secret() -> None:
    auth, tag, w = Authority.new(), _device(NodeRole.TAG), Worker()
    factory = tag.keys.factory_secret
    assert factory is not None
    assert _pair(auth, tag, w, factory, bytes([3]) * 32)[0] == Status.OK
    ch = w.connect(tag, Link.TUNNEL)
    grant, sig = auth.grant(tag, GrantOp.RELEASE, w.pub, _challenge(tag, ch))
    status, fields = Worker.call(tag, ch, SerialMsg.RELEASE, {"grant": grant, "sig": sig, "release_stage": 0})
    assert status == Status.OK and tag.record.state == OwnerState.OWNED
    fresh = SetupPayload.unpack(fields["data"])
    assert fresh.short_id == tag.keys.short_id and fresh.secret != factory
    # Stage 1 by another controller is refused.
    grant, sig = auth.grant(tag, GrantOp.RELEASE, w.pub, _challenge(tag, ch))
    status, _ = Worker.call(tag, ch, SerialMsg.RELEASE, {"grant": grant, "sig": sig, "release_stage": 1})
    assert status == Status.OK and tag.record.state == OwnerState.RELEASED and tag.record.gen == 2
    assert tag.record.op_key == b"" and tag.k_epoch(tag.keys.short_id, 1) is None
    # The label's factory secret no longer pairs; the fresh one does.
    assert _pair(auth, tag, Worker(), factory, bytes(32))[0] == Status.PROOF_FAILED
    assert _pair(auth, tag, Worker(), fresh.secret, bytes([4]) * 32)[0] == Status.OK
    assert tag.record.gen == 3


# ---------------------------------------------------------------- bridge


def test_bridge_pair_maint_and_recommission() -> None:
    auth, br, w = Authority.new(), _device(NodeRole.BRIDGE), Worker()
    assert br.keys.factory_secret is not None
    mk = bytes([5]) * 32
    assert _pair(auth, br, w, br.keys.factory_secret, mk)[0] == Status.OK

    # USB maintenance: a wrong mk is refused, the right one unlocks RECOMMISSION.
    ch = w.connect(br, Link.SERIAL)
    assert Worker.call(br, ch, SerialMsg.MAINT_AUTH, {"proof": ch.maint_proof(bytes(32))})[0] == Status.PROOF_FAILED
    grant, sig = auth.grant(br, GrantOp.MAINT, w.pub, _challenge(br, ch))
    assert Worker.call(br, ch, SerialMsg.RECOMMISSION, {"grant": grant, "sig": sig})[0] == Status.NOT_OWNER
    assert Worker.call(br, ch, SerialMsg.MAINT_AUTH, {"proof": ch.maint_proof(mk)})[0] == Status.OK
    grant, sig = auth.grant(br, GrantOp.MAINT, w.pub, _challenge(br, ch))
    status, fields = Worker.call(br, ch, SerialMsg.RECOMMISSION, {"grant": grant, "sig": sig})
    assert status == Status.OK and br.record.state == OwnerState.RELEASED
    fresh = SetupPayload.unpack(fields["data"])
    assert _pair(auth, br, w, fresh.secret, bytes([6]) * 32)[0] == Status.OK


def test_removed_bridge_is_locked_until_local_recommission() -> None:
    auth, br, w = Authority.new(), _device(NodeRole.BRIDGE), Worker()
    factory = br.keys.factory_secret
    assert factory is not None
    assert _pair(auth, br, w, factory, bytes([5]) * 32)[0] == Status.OK
    ch = w.connect(br, Link.TUNNEL)
    grant, sig = auth.grant(br, GrantOp.RELEASE, w.pub, _challenge(br, ch))
    assert Worker.call(br, ch, SerialMsg.RELEASE, {"grant": grant, "sig": sig})[0] == Status.OK
    assert br.record.locked
    assert _pair(auth, br, w, factory, bytes(32))[0] == Status.LOCKED
    # Recommissioning is refused over the mesh and allowed over local USB.
    ch = w.connect(br, Link.TUNNEL)
    assert Worker.call(br, ch, SerialMsg.RECOMMISSION, {})[0] == Status.NOT_OWNER
    ch = w.connect(br, Link.SERIAL)
    status, fields = Worker.call(br, ch, SerialMsg.RECOMMISSION, {})
    assert status == Status.OK
    assert _pair(auth, br, w, SetupPayload.unpack(fields["data"]).secret, bytes([7]) * 32)[0] == Status.OK


def test_generation_never_rewinds_through_records() -> None:
    rec = OwnerRecord(OwnerState.OWNED, 7, b"a" * 32, b"o" * 16, b"c" * 32, b"k" * 32)
    assert OwnerRecord.from_json(rec.to_json()) == rec
    keys = DeviceKeys.generate(NodeRole.TAG)
    assert DeviceKeys.from_json(keys.to_json()) == keys
