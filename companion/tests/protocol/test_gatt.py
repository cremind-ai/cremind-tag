"""GATT fragmentation (§5.3), session crypto (§5.4-5.5) and mesh messages."""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.protocol import session
from cremind_tag.protocol.fragments import MAX_MESSAGE, Fragmenter, FragmentError, Reassembler
from cremind_tag.protocol.ids import CtrlMsg, GattChr, MeshOp, RecordDir, RecordType, Status, mesh_opcode_bytes
from cremind_tag.protocol.msgs import MESH_MESSAGES, CtrlChallenge, CtrlHello, OversizeError, TruncatedError


def test_fragment_vectors(fixture: Any) -> None:
    for v in fixture("fragments.json")["vectors"]:
        fragmenter = Fragmenter(v["max_message"])
        fragmenter.seq = v["seq_start"]
        message = bytes.fromhex(v["message"])
        assert [f.hex() for f in fragmenter.split(message)] == v["fragments"], v["name"]
        assert fragmenter.seq == v["seq_next"]
        reassembler = Reassembler(v["max_message"], seq=v["seq_start"])
        results = [reassembler.feed(bytes.fromhex(f)) for f in v["fragments"]]
        assert results[-1] == message and all(r is None for r in results[:-1])


def test_fragment_errors(fixture: Any) -> None:
    for case in fixture("fragments.json")["errors"]:
        reassembler = Reassembler(case["max_message"])
        fragments = [bytes.fromhex(f) for f in case["fragments"]]
        for fragment in fragments[: case["error_at"]]:
            reassembler.feed(fragment)
        with pytest.raises(FragmentError) as info:
            reassembler.feed(fragments[case["error_at"]])
        assert info.value.status is Status.INVALID


def test_fragmenter_limits() -> None:
    with pytest.raises(FragmentError):
        Fragmenter(MAX_MESSAGE[GattChr.CTRL]).split(bytes(65))
    with pytest.raises(FragmentError):
        Fragmenter(64).split(b"")


def test_reassembler_continues_across_messages() -> None:
    fragmenter, reassembler = Fragmenter(205), Reassembler(205)
    for size in (1, 40, 205, 19, 64) * 20:  # wraps SEQ several times
        message = bytes(range(size))
        out = [reassembler.feed(f) for f in fragmenter.split(message)]
        assert out[-1] == message


def test_handshake_vector(fixture: Any) -> None:
    fx = fixture("session.json")
    secret, nonce_b, nonce_t = (bytes.fromhex(fx[k]) for k in ("tag_secret", "nonce_b", "nonce_t"))
    hello = bytes([CtrlMsg.HELLO]) + CtrlHello(1, fx["tag_id"], fx["epoch"], nonce_b).pack()
    assert hello.hex() == fx["hello"]
    challenge = CtrlChallenge.unpack(bytes.fromhex(fx["challenge"])[1:])
    assert challenge.nonce_t == nonce_t
    k_epoch = session.derive_k_epoch(secret, fx["tag_id"], fx["epoch"])
    assert k_epoch.hex() == fx["k_epoch"]
    assert session.derive_k_epoch(secret, fx["tag_id"], fx["k_epoch_next"]["epoch"]).hex() == fx["k_epoch_next"]["k_epoch"]
    th = session.transcript_hash(hello, bytes.fromhex(fx["challenge"]))
    assert th.hex() == fx["th"]
    mac_b = session.mac_b(k_epoch, th)
    assert mac_b.hex() == fx["mac_b"] == fx["auth"][2:]
    assert session.mac_t(k_epoch, th, mac_b).hex() == fx["mac_t"] == fx["auth_ok"][2:]
    session.verify_mac_b(k_epoch, th, mac_b)
    session.verify_mac_t(k_epoch, th, mac_b, bytes.fromhex(fx["mac_t"]))
    with pytest.raises(session.AuthError):
        session.verify_mac_b(k_epoch, th, bytes.fromhex(fx["bad_mac_b"]["mac_b"]))
    k_b2t, k_t2b = session.session_keys(k_epoch, th)
    assert (k_b2t.hex(), k_t2b.hex()) == (fx["k_b2t"], fx["k_t2b"])
    example = fx["nonce_example"]
    assert session.record_nonce(RecordDir[example["direction"]], example["counter"]).hex() == example["nonce"]


def test_record_vectors(fixture: Any) -> None:
    fx = fixture("session.json")
    keys = {"B2T": bytes.fromhex(fx["k_b2t"]), "T2B": bytes.fromhex(fx["k_t2b"])}
    receivers = {d: session.RecordReceiver(keys[d], RecordDir[d]) for d in keys}
    senders = {d: session.RecordSender(keys[d], RecordDir[d]) for d in keys}
    for r in fx["records"]:
        plaintext = bytes.fromhex(r["plaintext"])
        assert senders[r["direction"]].seal(r["type"], plaintext).hex() == r["record"]
        assert receivers[r["direction"]].open(bytes.fromhex(r["record"])) == (r["type"], plaintext)
    types = {r["type"] for r in fx["records"]}
    assert {RecordType.PLANE_DATA, RecordType.RESULT} <= types


def test_tampered_records_fail(fixture: Any) -> None:
    fx = fixture("session.json")
    for case in fx["tampered"]:
        key = bytes.fromhex(fx["k_b2t" if case["direction"] == "B2T" else "k_t2b"])
        with pytest.raises(session.AuthError) as info:
            session.open_record(key, RecordDir[case["direction"]], bytes.fromhex(case["record"]),
                                case["expected_counter"])
        assert info.value.status is Status.AUTH_FAILED and case["status_name"] == "AUTH_FAILED"


def test_receiver_counter_does_not_advance_on_failure() -> None:
    key = bytes(16)
    record = session.seal_record(key, RecordDir.B2T, RecordType.FRAME_END, 0, b"")
    receiver = session.RecordReceiver(key, RecordDir.B2T)
    with pytest.raises(session.AuthError):
        receiver.open(record[:-1] + bytes([record[-1] ^ 1]))
    assert receiver.open(record) == (RecordType.FRAME_END, b"")


def test_record_plaintext_limit() -> None:
    with pytest.raises(ValueError):
        session.seal_record(bytes(16), RecordDir.B2T, RecordType.PLANE_DATA, 0, bytes(193))


def test_mesh_vectors(fixture: Any) -> None:
    fx = fixture("mesh_msgs.json")
    assert {v["name"] for v in fx["vectors"]} == {op.name for op in MeshOp}
    for v in fx["vectors"]:
        op = MeshOp[v["name"]]
        cls = MESH_MESSAGES[op]
        params = bytes.fromhex(v["params"])
        msg = cls.unpack(params)
        assert msg.pack() == params
        assert (mesh_opcode_bytes(op) + params).hex() == v["access"]
        expected = {k: bytes.fromhex(x) if isinstance(getattr(msg, k), bytes) else x for k, x in v["fields"].items()}
        assert msg == cls(**expected)
    for case in fx["errors"]:
        cls = MESH_MESSAGES[MeshOp(case["op"])]
        with pytest.raises({"EMSGSIZE": OversizeError, "EINVAL": TruncatedError}[case["error"]]):
            cls.unpack(bytes.fromhex(case["params"]))
