"""Protocol v2 building blocks of the simulator without a scenario: adverts, specs, labels, per-link sessions,
the state file, configuration errors and the ``sim run --protocol 2`` helpers."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import GrantOp, Link, NodeRole, OwnerState, PairKind, SerialFlag, SerialMsg, Status
from cremind_tag.protocol.msgs import Ident2
from cremind_tag.secure import grants, identity
from cremind_tag.secure.channel import SecureChannel
from cremind_tag.secure.codes import parse_code
from cremind_tag.secure.messages import SecureMessage, pair_message, parse_pair_message
from cremind_tag.sim import Assign, BridgeSpec, SimConfig, Simulator, SimulatorThread, TagSpec
from cremind_tag.sim.harness import make_config
from cremind_tag.sim.radio import ADV_FLAG_SETUP, ADV_VERSION_V2, Advert
from cremind_tag.sim.v2 import LinkSessions, PairEndpoint, generate_keys, new_secure_device


def test_v2_advert_is_invisible_to_a_v1_bridge() -> None:
    advert = Advert(0x1A2B3C4D, ADV_FLAG_SETUP, 7, ADV_VERSION_V2)
    data = advert.to_bytes()
    assert Advert.parse(data) is None  # a v1 bridge only knows ver 1
    assert Advert.parse(data, (1, 2)) == advert
    v1 = Advert(0x1A2B3C4D, 0, 7)
    assert Advert.parse(v1.to_bytes(), (1, 2)) == v1 and v1.version == 1


def test_v2_tag_specs() -> None:
    spec = TagSpec.generate(5, 0, protocol=2)
    assert spec == TagSpec.generate(5, 0, protocol=2) and spec != TagSpec.generate(5, 1, protocol=2)
    assert spec.keys is not None and spec.tag_id == spec.keys.short_id and spec.secret == b""
    assert spec.fw == spec.keys.fw and spec.keys.factory_secret is not None
    with pytest.raises(ValueError, match="short_id"):
        TagSpec(spec.tag_id ^ 1, b"", protocol=2, keys=spec.keys)
    with pytest.raises(ValueError, match="DeviceKeys"):
        TagSpec(spec.tag_id, b"", protocol=2)
    assert TagSpec.generate(5, 0) == TagSpec.generate(5, 0, protocol=1)  # v1 unchanged


def test_setup_codes_are_the_labels() -> None:
    cfg = SimConfig(seed=3, protocol=2, bridges=[BridgeSpec(provisioned=False), BridgeSpec(labelled=False)],
                    tags=[TagSpec.generate(3, 0, protocol=2)])
    sim = Simulator(cfg)
    codes = sim.setup_codes()
    assert [c["role"] for c in codes] == ["bridge", "bridge", "tag"]
    bridge_label = parse_code(codes[0]["code"], role=NodeRole.BRIDGE)
    assert bridge_label.short_id == identity.short_id(sim.bridge(0).uuid)
    assert codes[0]["device_id"] == sim.bridge(0).uuid.hex() and codes[0]["qr"].startswith("CTAG:")
    assert codes[1]["code"] is None  # waits for FACTORY_SETUP
    tag_label = parse_code(codes[2]["qr"], role=NodeRole.TAG)
    assert tag_label.short_id == cfg.tags[0].tag_id and tag_label.secret == cfg.tags[0].keys.factory_secret
    assert sim.gateway_identity() == {"device_id": sim.gateway.secure.device_id.hex(), "owner_state": 0, "gen": 0}
    # Reproducible from the seed.
    assert Simulator(cfg).setup_codes() == codes


def test_a_bridge_keeps_one_session_per_link() -> None:
    """The USB port and the tunnel endpoint each have their own session; the challenge is the device's."""
    keys = generate_keys(1, NodeRole.BRIDGE, 0, board=3)
    device = new_secure_device(keys, 1, "b")
    sessions = LinkSessions(device)
    endpoint = PairEndpoint(sessions)
    worker_priv, _ = identity.x25519_generate()
    tunnel = SecureChannel(worker_priv, keys.ik_pub, keys.device_id, Link.TUNNEL)
    kind, body = parse_pair_message(endpoint.receive(pair_message(PairKind.HANDSHAKE, tunnel.message1())).replies[0])
    assert kind == PairKind.HANDSHAKE
    tunnel.finish(body)
    usb = SecureChannel(worker_priv, keys.ik_pub, keys.device_id, Link.SERIAL)
    with sessions.use(Link.SERIAL) as dev:
        usb.finish(dev.open_session(Link.SERIAL, usb.message1()))
    # Both sessions work, in any order.
    _, sealed = tunnel.seal_request(SerialMsg.STATUS, {})
    result = endpoint.receive(pair_message(PairKind.TRANSPORT, sealed))
    status = SecureChannel.decode(tunnel.unseal(parse_pair_message(result.replies[0])[1]))
    assert status["status"] == Status.OK and result.request == SerialMsg.STATUS
    with sessions.use(Link.SERIAL) as dev:
        assert dev.session is not None
        _, sealed = usb.seal_request(SerialMsg.STATUS, {})
        message = SecureMessage.unpack(dev.session.noise.decrypt(sealed))
        assert message.type == SerialMsg.STATUS and device.challenge == status["challenge"]
    assert device.session is None  # swapped out after use
    # A garbled transport message ends the tunnel session only.
    assert endpoint.receive(pair_message(PairKind.TRANSPORT, bytes(20))).replies == [
        pair_message(PairKind.CLOSE, bytes([Status.AUTH_REQUIRED]))]
    assert not sessions.has_session(Link.TUNNEL) and sessions.has_session(Link.SERIAL)
    assert endpoint.receive(b"").replies == [pair_message(PairKind.CLOSE, bytes([Status.INVALID]))]
    assert endpoint.receive(pair_message(PairKind.CLOSE, bytes([0]))).closed
    ident = Ident2.unpack(endpoint.ident())
    assert (ident.proto, ident.role, ident.device_id) == (2, NodeRole.BRIDGE, keys.device_id)


def test_state_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    cfg = SimConfig(seed=9, protocol=2, state_file=path, bridges=[BridgeSpec(provisioned=False, labelled=False)],
                    tags=[TagSpec.generate(9, 0, protocol=2)])
    sim = Simulator(cfg)
    tag = next(iter(sim.tags.values()))
    auth_sk, auth_pub = identity.ed25519_generate()
    ctl_priv, ctl_pub = identity.x25519_generate()
    # Pair the tag directly on its endpoint (no network): a record to keep.
    endpoint = tag.pairing
    channel = SecureChannel(ctl_priv, tag.secure.keys.ik_pub, tag.secure.device_id, Link.TUNNEL)
    channel.finish(parse_pair_message(endpoint.receive(pair_message(PairKind.HANDSHAKE,
                                                                    channel.message1())).replies[0])[1])
    challenge = tag.secure.draw_challenge()
    raw = grants.Grant(GrantOp.PAIR, tag.secure.device_id, NodeRole.TAG, auth_pub, b"\x01" * 16, ctl_pub, 0, 1,
                       challenge).encode()
    proof, _ = channel.setup_proof(tag.secure.keys.factory_secret, raw)
    root = os.urandom(32)
    _, sealed = channel.seal_request(SerialMsg.PAIR, {"grant": raw, "sig": grants.sign(raw, auth_sk), "proof": proof,
                                                     "op_key": root})
    result = endpoint.receive(pair_message(PairKind.TRANSPORT, sealed))
    answer = SecureChannel.decode(channel.unseal(parse_pair_message(result.replies[0])[1]))
    assert answer["status"] == Status.OK and result.record_changed
    bridge = sim.bridge(0).secure
    bridge.keys = replace(bridge.keys, factory_secret=b"\x07" * 10)  # what FACTORY_SETUP stores
    sim.save_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tags"][f"{tag.tag_id:08X}"]["v2"]["owner"]["state"] == OwnerState.OWNED
    assert {c["role"] for c in data["setup_codes"]} == {"bridge", "tag"}
    again = Simulator(cfg)
    again.load_state(path)
    restored = again.tag(tag.tag_id)
    assert restored.owned and restored.secure.record.op_key == root and restored.secure.record.gen == 1
    assert again.bridge(0).secure.keys.factory_secret == b"\x07" * 10
    # A state file of other hardware (another seed) is not adopted by a bridge or tag.
    other = Simulator(SimConfig(seed=10, protocol=2, bridges=[BridgeSpec(provisioned=False)],
                                tags=[TagSpec.generate(10, 0, protocol=2)]))
    other.load_state(path)
    assert other.bridge(0).secure.keys.factory_secret != b"\x07" * 10
    assert not next(iter(other.tags.values())).owned


def test_v2_configuration_rules() -> None:
    cfg = make_config(fontpack=None, tags=2, bridges=2, protocol=2, seed=4)
    assert cfg.protocol == 2 and cfg.assignments == [] and all(not b.provisioned for b in cfg.bridges)
    assert all(t.protocol == 2 for t in cfg.tags)
    sim = Simulator(cfg)
    assert sim.gateway.v2 and all(b.v2 for b in sim.bridges) and all(t.secure for t in sim.tags.values())
    # A v2 tag cannot be pre-assigned (it is paired first).
    bad = SimConfig(seed=4, protocol=2, bridges=[BridgeSpec()], tags=cfg.tags[:1],
                    assignments=[Assign(cfg.tags[0].tag_id, 0)])
    with pytest.raises(ValueError, match="v2 tag"):
        Simulator(bad)._setup_network()
    # A mixed world: a v1 bridge in a v2 world.
    mixed = Simulator(SimConfig(seed=4, protocol=2, bridges=[BridgeSpec(protocol=1)]))
    assert mixed.gateway.v2 and not mixed.bridge(0).v2


def test_sim_run_helpers_for_protocol_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    from cremind_tag.cli.sim import _describe, _register

    monkeypatch.setenv("CREMIND_TAG_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CREMIND_TAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CREMIND_TAG_SECRETS_BACKEND", "file")
    cfg = make_config(fontpack=None, tags=1, bridges=1, protocol=2, seed=6)
    cfg.state_file = tmp_path / "sim.json"
    with SimulatorThread(cfg) as thread:
        thread.call(_register)
        thread.call(_describe)
        codes = thread.call(lambda sim: sim.setup_codes())
    out = capsys.readouterr()
    assert "Setup codes" in out.out and "nothing was added" in out.err
    for entry in codes:
        assert entry["code"] in out.out
    # --register did not write a secret or an inventory row for v2 hardware.
    assert not (tmp_path / "data" / "secrets.json").exists()
    assert not (tmp_path / "data" / "companion.sqlite3").exists()
    saved = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    assert [c["code"] for c in saved["setup_codes"]] == [c["code"] for c in codes]


def test_sealed_answers_follow_the_serial_catalogue() -> None:
    """The inner header of a sealed answer: the request's type and request_id, the RESPONSE flag."""
    message = SecureMessage(SerialMsg.STATUS, int(SerialFlag.RESPONSE), 7,
                            cbor_msgs.encode_response(SerialMsg.STATUS, {"status": 0, "gen": 1}))
    decoded = SecureChannel.decode(SecureMessage.unpack(message.pack()))
    assert decoded == {"status": 0, "gen": 1}
