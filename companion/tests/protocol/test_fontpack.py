"""Font pack format (docs/fontpack.md) against fontpack_test.ctfp and fontpack.json."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.fontpack.format import (
    Face,
    FontPack,
    FontPackError,
    GlyphBitmap,
    StrikeSpec,
    bitmap_size,
    build_pack,
)
from cremind_tag.protocol.crc import crc32
from cremind_tag.protocol.ids import FONTPACK_EMPTY_BITMAP, Status


def test_header_and_tables(fixture: Any, pack: FontPack, fixtures_dir: Path) -> None:
    fx = fixture("fontpack.json")
    data = (fixtures_dir / fx["file"]).read_bytes()
    h = fx["header"]
    assert (pack.total_size, pack.pack_id.hex(), pack.content_hash.hex(), pack.manifest_id.hex()) == (
        h["total_size"], h["pack_id"], h["content_hash"], h["manifest_id"])
    assert len(data) == h["total_size"] and hashlib.sha256(data[128:]).hexdigest() == h["content_hash"]
    assert crc32(data[:124]) == h["header_crc32"]
    assert [f.face_id for f in pack.faces] == [f["face_id"] for f in fx["faces"]]
    assert [(f.name, f.scripts, f.flags, f.glyph_count) for f in pack.faces] == [
        (f["name"], f["scripts"], f["flags"], f["glyph_count"]) for f in fx["faces"]]
    for strike, expected in zip(pack.strikes, fx["strikes"], strict=True):
        assert {k: getattr(strike, k) for k in expected} == expected
    offsets = [h["face_table_offset"], h["strike_table_offset"], h["string_table_offset"],
               fx["strikes"][0]["index_off"], h["bitmap_area_offset"]]
    assert offsets == sorted(offsets) and offsets[0] == 128  # contiguous layout in documented order


def test_glyph_samples(fixture: Any, pack: FontPack) -> None:
    for g in fixture("fontpack.json")["glyphs"]:
        glyph = pack.glyph(g["face"], g["size_px"], g["glyph_id"])
        assert glyph is not None
        assert (glyph.width, glyph.height, glyph.bearing_x, glyph.bearing_y, glyph.advance, glyph.bitmap.hex()) == (
            g["width"], g["height"], g["bearing_x"], g["bearing_y"], g["advance"], g["bitmap"])
        assert (g["bitmap_off"] == FONTPACK_EMPTY_BITMAP) == (glyph.width == 0 or glyph.height == 0)


def test_deduplication(fixture: Any, pack: FontPack) -> None:
    fx = fixture("fontpack.json")
    samples = {(g["face"], g["size_px"], g["glyph_id"]): g["bitmap_off"] for g in fx["glyphs"]}
    assert samples[(1, 16, 11)] == samples[(1, 16, 12)]
    assert samples[(1, 16, 19)] == samples[(1, 24, 19)]
    assert fx["dedup"]["bitmap_area_size"] < fx["dedup"]["undeduplicated_size"]
    assert pack.glyph(1, 16, 20) is None and pack.glyph(5, 16, 0) is None


def test_corruptions(fixture: Any, fixtures_dir: Path) -> None:
    fx = fixture("fontpack.json")
    data = (fixtures_dir / fx["file"]).read_bytes()
    for case in fx["corruptions"]:
        bad = bytearray(data)
        bad[case["offset"]] ^= 0x01
        for verify, key in ((False, "boot_status"), (True, "install_status")):
            if case[key] == "OK":
                FontPack(bytes(bad), verify_content=verify)
            else:
                with pytest.raises(FontPackError) as info:
                    FontPack(bytes(bad), verify_content=verify)
                assert info.value.status == Status[case[key]]


def test_writer_is_order_independent_and_reader_accepts_slot_tail() -> None:
    glyph = GlyphBitmap(3, 2, 0, 2, 4, bytes([0b10100000, 0b01000000]))
    faces = [Face(1, 0, "B", "Latn", 2), Face(0, 1, "A", "Zsym", 1)]
    strikes = [StrikeSpec(1, 16, 12, 4, 19, (glyph, glyph)), StrikeSpec(0, 16, 16, 0, 16, (glyph,))]
    data = build_pack(faces, strikes)
    assert data == build_pack(faces[::-1], strikes[::-1])
    assert FontPack(data + b"\xff" * 100).total_size == len(data)
    assert len(data) == 128 + 2 * 16 + 2 * 24 + len(b"A\0Zsym\0B\0Latn\0") + 3 * 12 + 2


@pytest.mark.parametrize("glyph", [
    GlyphBitmap(3, 2, 0, 0, 0, bytes(1)),  # wrong bitmap size
    GlyphBitmap(3, 1, 0, 0, 0, bytes([0b00010000])),  # ink in a padding bit
    GlyphBitmap(1, 1, 200, 0, 0, bytes([0x80])),  # bearing out of i8
])
def test_writer_rejects_bad_glyphs(glyph: GlyphBitmap) -> None:
    with pytest.raises(FontPackError):
        build_pack([Face(0, 0, "A", "Zsym", 1)], [StrikeSpec(0, 16, 16, 0, 16, (glyph,))])


def test_reader_rejects_structural_errors(pack: FontPack) -> None:
    data = bytearray(pack.data)
    with pytest.raises(FontPackError):
        FontPack(bytes(data[:100]))
    bad = bytearray(data)
    bad[0] ^= 0xFF
    with pytest.raises(FontPackError):
        FontPack(bytes(bad))
    swapped = bytearray(data)  # swap the two face records: unsorted table, CRC fixed up
    face_off = int.from_bytes(data[60:64], "little")
    swapped[face_off : face_off + 32] = data[face_off + 16 : face_off + 32] + data[face_off : face_off + 16]
    swapped[124:128] = crc32(bytes(swapped[:124])).to_bytes(4, "little")
    with pytest.raises(FontPackError, match="sorted"):
        FontPack(bytes(swapped), verify_content=False)


def test_bitmap_size() -> None:
    assert (bitmap_size(0, 5), bitmap_size(8, 2), bitmap_size(9, 2)) == (0, 2, 4)
