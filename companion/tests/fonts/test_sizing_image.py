"""Flash sizing rule and external-flash images (docs/fontpack.md §3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.fontpack.format import Face, FontPack, GlyphBitmap, StrikeSpec, build_pack
from cremind_tag.fonts.image import (
    QSPI_XIP_BASE,
    SECTOR,
    flash_image,
    intel_hex,
    parse_slot_dir_record,
    slot_dir_record,
    write_image,
)
from cremind_tag.fonts.sizing import (
    MIB,
    density_label,
    erase_aligned,
    parse_size,
    required_flash,
    size_pack,
    slot_size,
    smallest_density,
)
from cremind_tag.protocol.crc import crc32


def test_capacity_rule_arithmetic() -> None:
    assert erase_aligned(0) == 0 and erase_aligned(1) == 65536 and erase_aligned(65536) == 65536
    assert erase_aligned(65537) == 131072
    assert required_flash(34_478_342) == 2 * 34_537_472 + 16 * MIB == 85_852_160
    assert smallest_density(85_852_160) == 128 * MIB and smallest_density(16 * MIB) == 16 * MIB
    assert smallest_density(257 * MIB) is None
    assert slot_size(128 * MIB) == 56 * MIB and slot_size(8 * MIB, MIB) == 3_670_016 and slot_size(8 * MIB) == 0
    s = size_pack(34_478_342)
    assert (s.flash_size, s.fits, s.slot_size, s.four_byte_addressing, s.development_only) == (
        128 * MIB, True, 56 * MIB, True, False)
    assert s.headroom == 56 * MIB - 34_478_342
    dev = size_pack(470_296, flash_size=8 * MIB, working_space=MIB)
    assert dev.development_only and dev.fits and dev.slot_size == 3_670_016
    assert not size_pack(470_296, flash_size=8 * MIB).fits  # 8 MiB can never meet the 16 MiB working space
    assert density_label(128 * MIB) == "1 Gbit (128 MiB)" and density_label(8 * MIB) == "64 Mbit (8 MiB)"
    assert parse_size("8MiB") == 8 * MIB and parse_size("512KiB") == 512 * 1024 and parse_size("0x100") == 256


def test_slot_directory_record() -> None:
    rec = slot_dir_record(7, 1, bytes(range(8)), 1234, bytes(range(32)))
    assert len(rec) == 64 and rec[:4] == b"CTSL" and int.from_bytes(rec[60:], "little") == crc32(rec[:60])
    assert parse_slot_dir_record(rec) == {"version": 1, "seq": 7, "slot": 1, "pack_id": bytes(range(8)),
                                          "size": 1234, "content_hash": bytes(range(32))}
    assert parse_slot_dir_record(b"\xff" * 64) is None
    assert parse_slot_dir_record(rec[:59] + bytes([rec[59] ^ 1]) + rec[60:]) is None


def _parse_hex(text: str) -> dict[int, int]:
    mem: dict[int, int] = {}
    upper = 0
    for line in text.splitlines():
        raw = bytes.fromhex(line[1:])
        assert sum(raw) & 0xFF == 0
        n, addr, kind = raw[0], int.from_bytes(raw[1:3], "big"), raw[3]
        if kind == 4:
            upper = int.from_bytes(raw[4:6], "big") << 16
        elif kind == 0:
            for i, b in enumerate(raw[4:4 + n]):
                mem[upper + addr + i] = b
    return mem


def test_flash_image(built: Any, tmp_path: Path) -> None:
    data = built.pack_path.read_bytes()
    img = flash_image(data, 8 * MIB, MIB)
    assert (img.slot_size, img.dir_offset, len(img.data)) == (3_670_016, 7_340_032, 7_340_032 + 2 * SECTOR)
    assert img.data[: len(data)] == data and set(img.data[len(data): img.slot_size]) == {0xFF}
    record = parse_slot_dir_record(img.data[img.dir_offset:])
    pack = FontPack(data)
    assert record == {"version": 1, "seq": 1, "slot": 0, "pack_id": pack.pack_id, "size": len(data),
                      "content_hash": pack.content_hash}
    assert set(img.data[img.dir_offset + 64:]) == {0xFF}  # rest of sector A and all of sector B erased
    paths = write_image(img, tmp_path / "image")
    assert paths["bin"].read_bytes() == img.data
    mem = _parse_hex(paths["hex"].read_text(encoding="ascii"))
    pack_end = -(-len(data) // SECTOR) * SECTOR
    assert sorted(mem) == [QSPI_XIP_BASE + i for i in range(pack_end)] + [
        QSPI_XIP_BASE + img.dir_offset + i for i in range(2 * SECTOR)]
    assert bytes(mem[QSPI_XIP_BASE + i] for i in range(len(data))) == data
    meta = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert meta["pack_id"] == pack.pack_id.hex() and meta["dir_offset"] == img.dir_offset
    with pytest.raises(ValueError, match="no pack slots"):
        flash_image(data, 8 * MIB)
    big = build_pack([Face(0, 0, "big", "Zyyy", 12)], [StrikeSpec(0, 16, 16, 0, 16, tuple(
        GlyphBitmap(255, 255, 0, 0, 255, bytes([2 * i]) * (32 * 255)) for i in range(12)))])
    assert len(big) > 65536
    with pytest.raises(ValueError, match="does not fit"):
        flash_image(big, 1 * MIB, 1 * MIB - 2 * 65536)


def test_intel_hex_crosses_64k_boundaries() -> None:
    mem = _parse_hex(intel_hex([(0xFFF0, bytes(range(64)))], 0))
    assert [mem[0xFFF0 + i] for i in range(64)] == list(range(64))
