"""Generated bindings and fixtures are up to date, and the generated codecs behave."""

from __future__ import annotations

from types import ModuleType

import pytest

from cremind_tag.protocol import ids, msgs


def test_codegen_outputs_are_current(codegen: ModuleType) -> None:
    stale = codegen.stale_outputs(codegen.generate())
    assert not stale, f"run tools/codegen.py: {[p.name for p in stale]}"


def test_fixtures_are_current(gen_fixtures: ModuleType) -> None:
    stale = [name for name, content in gen_fixtures.build_all().items()
             if gen_fixtures.read_fixture(name) != content]
    assert not stale, f"run tools/gen_fixtures.py: {stale}"


def test_generator_is_deterministic(gen_fixtures: ModuleType) -> None:
    assert gen_fixtures.build_all() == gen_fixtures.build_all()


def test_spec_hash_matches_generated_modules(codegen: ModuleType) -> None:
    import hashlib

    assert ids.SPEC_SHA256 == hashlib.sha256(codegen.spec_text().encode()).hexdigest()


def test_spec_consistency_check_rejects_bad_limits(codegen: ModuleType) -> None:
    spec = codegen.load_spec(codegen.spec_text())
    spec["constants"]["TAG_PLANE_DATA_MAX"]["value"] = 190
    with pytest.raises(codegen.SpecError, match="TAG_PLANE_DATA_MAX"):
        codegen.check_spec(spec, codegen.collect_messages(spec))


def test_yaml_on_key_is_not_a_boolean(codegen: ModuleType) -> None:
    spec = codegen.load_spec(codegen.spec_text())
    assert all("on" in model for model in spec["mesh"]["models"].values())


def test_mesh_vendor_opcode_matches_zephyr() -> None:
    # BT_MESH_MODEL_OP_3(b0, cid) = ((b0 << 16) | 0xC00000) | cid, sent as [0xC0|b0, cid LE].
    assert ids.mesh_vendor_opcode(ids.MeshOp.LAYOUT_BEGIN) == 0xC1FFFF
    assert ids.mesh_opcode_bytes(ids.MeshOp.TAG_SEEN) == bytes([0xD9, 0xFF, 0xFF])


def test_gatt_uuids() -> None:
    assert str(ids.GATT_SERVICE_UUID) == "6a7c0001-4c1e-4b9f-9d2a-43524d544147"
    assert str(ids.GATT_CHR_UUIDS[ids.GattChr.STATUS]) == "6a7c0005-4c1e-4b9f-9d2a-43524d544147"


def test_message_lengths() -> None:
    assert msgs.SerialHeader.LEN == ids.SERIAL_HEADER_LEN
    assert msgs.LayoutHeader.LEN == 12
    assert msgs.Enrollment.LEN == 48
    assert msgs.MeshLayoutChunk.MAX_LEN == 3 + ids.LAYOUT_CHUNK_DATA_MAX
    assert msgs.RecPlaneData.MAX_LEN == ids.TAG_RECORD_PAYLOAD_MAX
    assert msgs.LayoutGlyph.LEN == 4


def test_fixed_message_round_trip_and_little_endian() -> None:
    msg = msgs.MeshTagSeen(0x01020304, -2, 0xABCD, 7)
    data = msg.pack()
    assert data == bytes.fromhex("04030201" "fe" "cdab" "07")
    assert msgs.MeshTagSeen.unpack(data) == msg


def test_fixed_message_length_errors() -> None:
    data = msgs.CtrlAuth(bytes(16)).pack()
    with pytest.raises(msgs.TruncatedError):
        msgs.CtrlAuth.unpack(data[:-1])
    with pytest.raises(msgs.OversizeError):
        msgs.CtrlAuth.unpack(data + b"\x00")
    with pytest.raises(msgs.MessageError):
        msgs.CtrlAuth(bytes(15)).pack()
    with pytest.raises(msgs.MessageError):
        msgs.MeshIdentify(256).pack()


def test_trailing_bytes_field() -> None:
    msg = msgs.RecPlaneData(1, 189, bytes(range(10)))
    assert msgs.RecPlaneData.unpack(msg.pack()) == msg
    with pytest.raises(msgs.OversizeError):
        msgs.RecPlaneData(0, 0, bytes(ids.TAG_PLANE_DATA_MAX + 1)).pack()
    with pytest.raises(msgs.OversizeError):
        msgs.RecPlaneData.unpack(bytes(3 + ids.TAG_PLANE_DATA_MAX + 1))
    assert msgs.RecPlaneData.unpack(b"\x00\x00\x00").data == b""


def test_variable_part_messages_decode_fixed_part_only() -> None:
    fixed = msgs.LayoutCmdQr(1, 2, 3, 0, 1, 4)
    assert msgs.LayoutCmdQr.unpack(fixed.pack() + b"abcd") == fixed
