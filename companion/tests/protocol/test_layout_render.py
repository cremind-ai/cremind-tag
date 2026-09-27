"""Layout validation (§4.3), QR (§4.4) and the normative renderer (§4.4)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from cremind_tag.protocol.ids import BRIDGE_STRIP_ROWS, Color, Status
from cremind_tag.protocol.layout import (
    Clear,
    Glyph,
    Glyphs,
    Layout,
    LayoutError,
    Line,
    Progress,
    Rect,
    check_panel,
    decode_layout,
    encode_layout,
    layout_digest,
    qr_code,
    validate_layout,
)
from cremind_tag.render.reference import Panel, frame_digest, render_frame, render_strip

W, B, R = Color.WHITE, Color.BLACK, Color.RED


class _NoGlyphs:
    def has_strike(self, face: int, size_px: int) -> bool:
        return False

    def glyph(self, face: int, size_px: int, glyph_id: int) -> None:
        return None


def test_valid_layouts(fixture: Any, pack: Any) -> None:
    for v in fixture("layouts.json")["valid"]:
        data = bytes.fromhex(v["hex"])
        assert validate_layout(data, pack.has_strike) is Status.OK
        layout = decode_layout(data)
        assert encode_layout(layout) == data
        assert layout_digest(data).hex() == v["digest"]
        assert len(layout.commands) == len(v["layout"]["commands"])
        assert [c["op"] for c in v["layout"]["commands"]] == [
            type(c).__name__.removeprefix("LayoutCmd").upper() for c in layout.commands]
    ops = {c["op"] for v in fixture("layouts.json")["valid"] for c in v["layout"]["commands"]}
    assert ops == {"CLEAR", "GLYPHS", "ICON", "LINE", "RECT", "PROGRESS", "QR"}


def test_invalid_layouts(fixture: Any, pack: Any) -> None:
    cases = fixture("layouts.json")["invalid"]
    assert len(cases) >= 12
    for case in cases:
        assert validate_layout(bytes.fromhex(case["hex"]), pack.has_strike) == Status[case["status_name"]], case["name"]


def test_panel_checks(fixture: Any) -> None:
    fx = fixture("layouts.json")
    by_name = {v["name"]: bytes.fromhex(v["hex"]) for v in fx["valid"]}
    for case in fx["panel_checks"]:
        layout = decode_layout(by_name[case["layout"]])
        if case["status_name"] == "OK":
            check_panel(layout, case["native_width"], case["native_height"])
        else:
            with pytest.raises(LayoutError):
                check_panel(layout, case["native_width"], case["native_height"])


def test_structural_errors_precede_strike_errors(pack: Any) -> None:
    layout = Layout(8, 8, 0, W, (Glyphs(9, 16, B, 0, 0, (Glyph(1, 0, 0),)), Clear(5)))
    assert validate_layout(encode_layout(layout, validate=False), pack.has_strike) is Status.INVALID


def test_qr_fixture(fixture: Any) -> None:
    vectors = fixture("qr.json")["vectors"]
    assert len({v["text"] for v in vectors}) >= 5
    for v in vectors:
        symbol = qr_code(v["text"].encode(), v["ecc"])
        assert (symbol.get_version(), symbol.get_mask(), symbol.get_size()) == (v["version"], v["mask"], v["size"])
        assert symbol.get_error_correction_level().ordinal == v["ecc_used"] >= v["ecc"]
        size = v["size"]
        for y, row in enumerate(v["rows"]):
            bits = bin(int(row, 16))[2:].zfill(len(row) * 4)
            assert [c == "1" for c in bits[:size]] == [symbol.get_module(x, y) for x in range(size)]


def test_every_valid_qr_text_fits_version_10() -> None:
    assert qr_code(b"~" * 96, 3).get_version() <= 10


def _frame(layout: Layout, panel: Panel) -> list[str]:
    """Plane 0 as rows of '#' (ink) and '.' (white) for a 1-plane, 1-is-white panel."""
    plane = render_frame(encode_layout(layout), panel, _NoGlyphs()).planes[0]
    rows = []
    for y in range(panel.height):
        row = plane[y * panel.row_bytes : (y + 1) * panel.row_bytes]
        rows.append("".join("." if row[x >> 3] & (0x80 >> (x & 7)) else "#" for x in range(panel.width)))
    return rows


def test_line_and_brush_by_hand() -> None:
    panel = Panel(6, 4, 1, 0x01)
    assert _frame(Layout(6, 4, 0, W, (Line(0, 0, 5, 2, 1, B),)), panel) == [
        "##....", "..##..", "....##", "......"]
    # width 2: o = 0, square [px, px+2); width 3: o = 1, centred
    assert _frame(Layout(6, 4, 0, W, (Line(1, 1, 1, 1, 2, B),)), panel) == ["......", ".##...", ".##...", "......"]
    assert _frame(Layout(6, 4, 0, W, (Line(1, 1, 1, 1, 3, B),)), panel) == ["###...", "###...", "###...", "......"]


def test_rect_and_progress_by_hand() -> None:
    panel = Panel(8, 6, 1, 0x01)
    assert _frame(Layout(8, 6, 0, W, (Rect(1, 1, 5, 4, 1, B),)), panel) == [
        "........", ".#####..", ".#...#..", ".#...#..", ".#####..", "........"]
    # f = floor((8 - 4) * 1 / 2) = 2 columns from x + 2
    assert _frame(Layout(8, 6, 0, W, (Progress(0, 0, 8, 6, 1, 2, B),)), panel) == [
        "########", "#......#", "#.##...#", "#.##...#", "#......#", "########"]


def test_rotation_maps_the_logical_origin() -> None:
    panel = Panel(5, 3, 1, 0x01)
    dot = (Rect(0, 0, 1, 1, 0, B),)
    assert _frame(Layout(5, 3, 0, W, dot), panel)[0] == "#...."
    assert _frame(Layout(3, 5, 1, W, dot), panel)[0] == "....#"
    assert _frame(Layout(5, 3, 2, W, dot), panel)[2] == "....#"
    assert _frame(Layout(3, 5, 3, W, dot), panel)[2] == "#...."


def test_plane_encoding_and_padding() -> None:
    layout = encode_layout(Layout(3, 1, 0, W, (Rect(0, 0, 1, 1, 0, B), Rect(1, 0, 1, 1, 0, R))))
    one_plane = render_frame(layout, Panel(3, 1, 1, 0x01), _NoGlyphs())
    assert one_plane.planes == (bytes([0b00111111]),)  # red -> black, padding white (1)
    inverted = render_frame(layout, Panel(3, 1, 1, 0x00), _NoGlyphs())
    assert inverted.planes == (bytes([0b11000000]),)
    two = render_frame(layout, Panel(3, 1, 2, 0x03), _NoGlyphs())
    assert two.planes == (bytes([0b01111111]), bytes([0b01000000]))  # red takes plane 0 white
    assert two.digest == hashlib.sha256(two.planes[0] + two.planes[1]).digest() == frame_digest(two.planes)


def test_render_fixture(fixture: Any, pack: Any) -> None:
    fx = fixture("render.json")
    assert len(fx["scenarios"]) >= 10
    for s in fx["scenarios"]:
        frame = render_frame(bytes.fromhex(s["layout_hex"]), Panel(**s["panel"]), pack)
        assert [hashlib.sha256(p).hexdigest() for p in frame.planes] == s["planes_sha256"], s["name"]
        assert frame.digest.hex() == s["frame_digest"]
        if "planes_hex" in s:
            assert [p.hex() for p in frame.planes] == s["planes_hex"]


def test_strips_equal_frame_rows(fixture: Any, pack: Any) -> None:
    for s in fixture("render.json")["scenarios"]:
        panel = Panel(**s["panel"])
        layout = bytes.fromhex(s["layout_hex"])
        frame = render_frame(layout, panel, pack)
        for plane in range(panel.planes):
            strips = b"".join(render_strip(layout, panel, pack, plane, y0)
                              for y0 in range(0, panel.height, BRIDGE_STRIP_ROWS))
            assert strips == frame.planes[plane], (s["name"], plane)


def test_render_rejects_panel_mismatch_and_missing_strikes(pack: Any) -> None:
    layout = encode_layout(Layout(10, 20, 0, W, ()))
    with pytest.raises(LayoutError):
        render_frame(layout, Panel(20, 10, 1, 1), pack)
    glyphs = encode_layout(Layout(10, 20, 0, W, (Glyphs(3, 16, B, 0, 0, (Glyph(1, 0, 0),)),)))
    with pytest.raises(LayoutError) as info:
        render_frame(glyphs, Panel(10, 20, 1, 1), pack)
    assert info.value.status is Status.FONTPACK_MISMATCH
