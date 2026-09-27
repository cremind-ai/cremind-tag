"""FreeType rasterisation of synthetic fonts: metrics, bitmaps, format limits, icon cells."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from cremind_tag.fontpack.format import GlyphBitmap
from cremind_tag.fonts.rasterize import (
    StrikeTask,
    font_facts,
    icon_cell,
    open_face,
    render_strike,
    render_task,
)


def _font(tmp_path: Path, data: bytes, name: str = "t.ttf") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _ink(g: GlyphBitmap) -> set[tuple[int, int]]:
    rb = (g.width + 7) // 8
    return {(x, y) for y in range(g.height) for x in range(g.width) if g.bitmap[y * rb + x // 8] & (0x80 >> (x % 8))}


@pytest.fixture()
def latin(tmp_path: Path, synthetic: ModuleType) -> Path:
    return _font(tmp_path, synthetic.latin_font({1: "Test Sans", 5: "Version 1.000", 0: "c"}))


def test_font_facts(latin: Path) -> None:
    facts = font_facts(latin)
    assert facts.num_glyphs == 5 and facts.hinting == "none" and facts.names[5] == "Version 1.000"
    assert "glyf" in facts.tables and "fpgm" not in facts.tables


@pytest.mark.parametrize("size, px", [(16, 30), (24, 20), (32, 15)])
def test_exact_bitmaps_and_metrics(latin: Path, size: int, px: int) -> None:
    (ascent, descent, line_height), glyphs, stats = render_strike(open_face(latin), size, "none")
    assert (ascent, descent, line_height) == (-(-420 // px), -(-120 // px), -(-540 // px))
    bar = glyphs[2]
    assert (bar.width, bar.height, bar.bearing_x, bar.bearing_y, bar.advance) == (
        180 // px, 300 // px, 60 // px, 300 // px, 300 // px)
    assert _ink(bar) == {(x, y) for x in range(bar.width) for y in range(bar.height)}
    assert glyphs[3] == bar  # identical outlines, identical bitmaps
    assert glyphs[1].empty and glyphs[1].advance == (150 * 64 // px + 32) // 64  # space: empty, advance kept
    assert len(glyphs) == stats.glyphs == 5 and stats.errors == [] and stats.empty == 1


def test_wide_glyph_is_cropped_to_the_format_limit(latin: Path) -> None:
    _metrics, glyphs, stats = render_strike(open_face(latin), 16, "none")
    wide = glyphs[4]
    assert stats.clipped == [4] and stats.dropped == []
    assert (wide.width, wide.height, wide.bearing_x, wide.advance) == (255, 2, 0, 300)
    assert len(_ink(wide)) == 255 * 2


@pytest.mark.parametrize("mode", ["native", "autohint", "none"])
def test_every_hinting_mode_yields_valid_glyphs(latin: Path, mode: str) -> None:
    _metrics, glyphs, _stats = render_strike(open_face(latin), 24, mode)
    for g in glyphs:
        assert len(g.bitmap) == g.height * ((g.width + 7) // 8)
        pad = -g.width % 8
        if pad and not g.empty:
            rb = (g.width + 7) // 8
            assert all(g.bitmap[r * rb + rb - 1] & ((1 << pad) - 1) == 0 for r in range(g.height))


def test_render_task_round_trips_glyphs(latin: Path) -> None:
    result = render_task(StrikeTask(3, str(latin), 16, "none"))
    _metrics, glyphs, _stats = render_strike(open_face(latin), 16, "none")
    assert result.glyphs() == tuple(glyphs) and (result.face_id, result.size) == (3, 16)


def test_unknown_variation_axis_is_rejected(latin: Path) -> None:
    with pytest.raises(ValueError, match="not a variable font"):
        open_face(latin, (("wght", 400.0),))


@pytest.mark.parametrize("size", [16, 24, 32, 48])
def test_icon_cells_are_exact_and_centred(tmp_path: Path, synthetic: ModuleType, size: int) -> None:
    path = _font(tmp_path, synthetic.icon_font({1: "Test Icons"}), "icons.ttf")
    face = open_face(path)
    cell = icon_cell(face, 1, size, "none")  # icon 1: square (40,40)-(440,440) in a 480-unit em
    assert (cell.width, cell.height, cell.bearing_x, cell.bearing_y, cell.advance) == (size, size, 0, 0, size)
    ink = _ink(cell)
    xs = sorted({x for x, _ in ink})
    ys = sorted({y for _, y in ink})
    left, right = xs[0], size - 1 - xs[-1]
    top, bottom = ys[0], size - 1 - ys[-1]
    assert abs(left - right) <= 1 and abs(top - bottom) <= 1
    if size in (24, 48):  # 20 and 10 units per pixel: edges land exactly
        pad = 40 * size // 480
        assert (left, top) == (pad, pad) and len(ink) == (size - 2 * pad) ** 2


def test_icon_task_puts_icon_ids_at_glyph_ids(tmp_path: Path, synthetic: ModuleType) -> None:
    path = _font(tmp_path, synthetic.icon_font({1: "Test Icons"}), "icons.ttf")
    cps = tuple(synthetic.ICON_BASE + i for i in range(3))
    result = render_task(StrikeTask(0, str(path), 24, "none", icon_codepoints=cps))
    glyphs = result.glyphs()
    assert len(glyphs) == 4 and glyphs[0].empty
    assert (result.ascent, result.descent, result.line_height) == (24, 0, 24)
    heights = [len({y for _, y in _ink(g)}) for g in glyphs[1:]]
    assert heights == sorted(heights, reverse=True)  # icon i is 10*i units shorter
    with pytest.raises(ValueError, match="not in"):
        render_task(StrikeTask(0, str(path), 24, "none", icon_codepoints=(0xF000,)))
