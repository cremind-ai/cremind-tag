"""Enrollment blob (§9) and the tag transaction decisions (§5.6, §6)."""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.protocol import enrollment, tag_txn
from cremind_tag.protocol.ids import Board, Status


def test_enrollment_fixture(fixture: Any) -> None:
    fx = fixture("enrollment.json")
    f = fx["fields"]
    blob = enrollment.pack_blob(f["tag_id"], bytes.fromhex(f["secret"]), f["board"], f["panel"], f["flags"])
    assert blob.hex() == fx["blob"] and len(blob) == 48
    parsed = enrollment.unpack_blob(blob)
    assert (parsed.magic, parsed.crc32, parsed.tag_id) == (f["magic"], f["crc32"], f["tag_id"])
    assert blob[:4] == b"CTAG"
    assert enrollment.enrollment_hex(blob, Board(f["board"])).splitlines() == fx["intel_hex"]
    assert fx["uicr_customer_addr"] == {"nrf51": 0x10001080, "nrf52": 0x10001080}
    for case in fx["invalid"]:
        with pytest.raises(enrollment.EnrollmentError) as info:
            enrollment.unpack_blob(bytes.fromhex(case["blob"]))
        assert info.value.status is Status.SECURITY_CONFIG == Status[case["status_name"]]


def _parse_hex(text: str) -> dict[int, int]:
    """Minimal I32HEX reader: checks every checksum, returns address -> byte."""
    memory: dict[int, int] = {}
    upper = 0
    lines = text.splitlines()
    assert lines[-1] == ":00000001FF"
    for line in lines[:-1]:
        raw = bytes.fromhex(line[1:])
        assert line[0] == ":" and sum(raw) & 0xFF == 0 and raw[0] == len(raw) - 5
        address, kind, data = int.from_bytes(raw[1:3], "big"), raw[3], raw[4:-1]
        if kind == 0x04:
            upper = int.from_bytes(data, "big") << 16
        else:
            assert kind == 0x00
            memory.update({upper + address + i: b for i, b in enumerate(data)})
    return memory


def test_intel_hex_crosses_64k_boundary() -> None:
    text = enrollment.intel_hex(bytes(range(20)), 0x1000FFF8)
    assert text.count(":02000004") == 2  # a new extended linear address at 0x10010000
    assert _parse_hex(text) == {0x1000FFF8 + i: i for i in range(20)}


def test_enrollment_hex_places_blob_at_uicr_customer(fixture: Any) -> None:
    fx = fixture("enrollment.json")
    memory = _parse_hex("\n".join(fx["intel_hex"]) + "\n")
    assert bytes(memory[0x10001080 + i] for i in range(48)).hex() == fx["blob"]
    assert len(memory) == 48


def test_every_tag_board_has_a_uicr_address() -> None:
    tag_boards = [b for b in Board if b >= 16]
    assert all(enrollment.BOARD_SOC[b] in enrollment.UICR_CUSTOMER_ADDR for b in tag_boards)


def _record(r: dict[str, Any] | None) -> tag_txn.DisplayRecord | None:
    if r is None:
        return None
    return tag_txn.DisplayRecord(r["tag_id"], r["epoch"], r["revision"], r["update_id"], bytes.fromhex(r["digest"]),
                                 Status[r["status_name"]], tag_txn.StoredState[r["state"]])


def test_frame_begin_table(fixture: Any) -> None:
    fx = fixture("tag_txn.json")
    panel = fx["panel"]
    for case in fx["frame_begin"]:
        frame = case["frame"]
        decision = tag_txn.frame_begin_decision(
            _record(case["stored"]), frame["epoch"], frame["revision"], bytes.fromhex(frame["digest"]),
            frame["planes"], frame["plane_len"], panel["planes"], panel["plane_len"])
        expect = case["expect"]
        assert decision.accept == expect["accept"], case["name"]
        assert decision.duplicate == expect["duplicate"], case["name"]
        assert (decision.status.name if decision.status is not None else None) == expect["status_name"], case["name"]


def test_boot_rule(fixture: Any) -> None:
    for case in fixture("tag_txn.json")["boot"]:
        result = tag_txn.boot_recover(_record(case["stored"]))
        expect = case["expect"]
        assert result.record == _record(expect["record"]), case["name"]
        assert (result.persist, result.unknown_pending) == (expect["persist"], expect["challenge_flag_bit0"])
