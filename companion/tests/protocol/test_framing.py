"""CRC-32, COBS and serial frames against the spec and the fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.protocol import cobs
from cremind_tag.protocol.crc import CHECK_INPUT, CHECK_VALUE, crc32
from cremind_tag.protocol.ids import SERIAL_MAX_ENCODED, SERIAL_MAX_FRAME, SerialFlag, SerialMsg
from cremind_tag.protocol.serial_frame import (
    Frame,
    FrameCrcError,
    FrameLengthError,
    FrameReader,
    FrameVersionError,
    decode_frame,
    encode_frame,
    frame_to_wire,
)


def _crc32_bitwise(data: bytes) -> int:
    """CRC-32/IEEE straight from its parameters (§1.1)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def test_crc_check_value() -> None:
    assert crc32(CHECK_INPUT) == CHECK_VALUE == _crc32_bitwise(CHECK_INPUT)


def test_crc_fixture(fixture: Any) -> None:
    for v in fixture("crc32.json")["vectors"]:
        data = bytes.fromhex(v["data"])
        assert crc32(data) == v["crc32"] == _crc32_bitwise(data)
        assert crc32(data).to_bytes(4, "little").hex() == v["le"]


def test_crc_is_incremental() -> None:
    assert crc32(b"6789", crc32(b"12345")) == CHECK_VALUE


def test_cobs_vectors(fixture: Any) -> None:
    fx = fixture("cobs.json")
    for v in fx["vectors"]:
        decoded, encoded = bytes.fromhex(v["decoded"]), bytes.fromhex(v["encoded"])
        assert cobs.encode(decoded) == encoded, v["name"]
        assert cobs.decode(encoded) == decoded, v["name"]
        assert 0 not in encoded
    for v in fx["decode_only"]:
        assert cobs.decode(bytes.fromhex(v["encoded"])) == bytes.fromhex(v["decoded"])
    for v in fx["decode_errors"]:
        with pytest.raises(cobs.CobsError):
            cobs.decode(bytes.fromhex(v["encoded"]))


def test_cobs_worst_case_fits_max_encoded() -> None:
    worst = cobs.encode(bytes(i % 255 + 1 for i in range(SERIAL_MAX_FRAME)))
    assert len(worst) + 1 <= SERIAL_MAX_ENCODED


@pytest.mark.parametrize("chunk", [1, 7, 4096])
def test_cobs_stream(fixture: Any, chunk: int) -> None:
    for case in fixture("cobs.json")["stream"]:
        data = bytes.fromhex(case["input"])
        decoder = cobs.StreamDecoder()
        frames = []
        for i in range(0, len(data), chunk):
            frames += decoder.feed(data[i : i + chunk])
        assert [f.hex() for f in frames] == case["frames"], case["name"]
        assert (decoder.errors, decoder.oversize) == (case["errors"], case["oversize"]), case["name"]


def test_cobs_stream_accepts_exact_max_frame() -> None:
    for data in (bytes(i % 255 + 1 for i in range(SERIAL_MAX_FRAME)), bytes(SERIAL_MAX_FRAME)):
        assert cobs.StreamDecoder().feed(cobs.encode(data) + b"\x00") == [data]
        oversize = cobs.StreamDecoder()
        assert oversize.feed(cobs.encode(data + b"\x01") + b"\x00") == [] and oversize.oversize == 1


def test_serial_frames_fixture(fixture: Any, gen_fixtures: Any) -> None:
    from cremind_tag.protocol import cbor_msgs

    for f in fixture("serial_frames.json")["frames"]:
        decoded, wire = bytes.fromhex(f["decoded"]), bytes.fromhex(f["wire"])
        frame = decode_frame(decoded)
        assert (frame.type, frame.request_id, frame.flags, frame.credits) == (
            f["type"], f["request_id"], f["flags"], f["credits"])
        assert frame.payload.hex() == f["payload"]
        assert encode_frame(frame) == decoded and frame_to_wire(frame) == wire
        fields = gen_fixtures.cbor_fields_from_json(f["fields"])
        assert cbor_msgs.decode_map(frame.payload) == fields
        assert cbor_msgs.encode_map(fields) == frame.payload
        assert FrameReader().feed(wire) == [frame]


def test_serial_invalid_frames(fixture: Any) -> None:
    errors = {"len": FrameLengthError, "crc": FrameCrcError, "version": FrameVersionError}
    for case in fixture("serial_frames.json")["invalid"]:
        with pytest.raises(errors[case["error"]]):
            decode_frame(bytes.fromhex(case["decoded"]))


def test_frame_reader_counts_and_resyncs() -> None:
    good = Frame(SerialMsg.PING, 5, b"", SerialFlag.RESPONSE)
    bad_crc = bytearray(encode_frame(good))
    bad_crc[-1] ^= 1
    reader = FrameReader()
    stream = cobs.encode(bytes(bad_crc)) + b"\x00" + b"\x05\x01\x02\x00" + frame_to_wire(good)
    assert reader.feed(stream) == [good]
    assert (reader.crc_errors, reader.cobs_errors, reader.len_errors) == (1, 1, 0)


def test_encode_rejects_oversized_payload() -> None:
    with pytest.raises(FrameLengthError):
        encode_frame(Frame(SerialMsg.FONT_DATA, 1, bytes(SERIAL_MAX_FRAME)))
