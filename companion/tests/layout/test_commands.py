"""Positioned glyphs -> GLYPHS commands: grouping, i8 delta splitting, run length, validation."""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.layout import PositionedGlyph, command_glyphs, glyph_commands, layout_text
from cremind_tag.protocol.ids import LAYOUT_HARD_MAX, Color, Status
from cremind_tag.protocol.layout import Layout, check_panel, check_strikes, decode_layout, encode_layout


def g(x: int, y: int, gid: int = 5, face: int = 1, size: int = 16, line: int = 0) -> PositionedGlyph:
    return PositionedGlyph(face, size, gid, x, y, 0, line)


def walk(cmds: list[Any]) -> list[tuple[int, int, int, int, int]]:
    return [(c.face, c.size_px, gid, x, y) for c in cmds for gid, x, y in command_glyphs(c)]


def test_consecutive_glyphs_share_a_command() -> None:
    glyphs = [g(10 + 9 * i, 40) for i in range(20)]
    cmds = glyph_commands(glyphs, Color.BLACK)
    assert len(cmds) == 1 and cmds[0].origin_x == 10 and cmds[0].glyphs[0].dx == 0
    assert walk(cmds) == [(1, 16, 5, 10 + 9 * i, 40) for i in range(20)]


@pytest.mark.parametrize(("dx", "dy", "split"), [
    (127, 0, False), (128, 0, True), (-128, 0, False), (-129, 0, True),
    (0, 127, False), (0, 128, True), (0, -128, False), (0, -129, True),
])
def test_i8_delta_overflow_splits(dx: int, dy: int, split: bool) -> None:
    glyphs = [g(100, 200), g(100 + dx, 200 + dy)]
    cmds = glyph_commands(glyphs, Color.BLACK, reorder=False)
    assert len(cmds) == (2 if split else 1)
    assert walk(cmds) == [(1, 16, 5, 100, 200), (1, 16, 5, 100 + dx, 200 + dy)]


def test_runs_split_at_255_glyphs_and_on_strike_change() -> None:
    glyphs = [g(i % 50, 20 + i // 50) for i in range(600)]
    cmds = glyph_commands(glyphs, Color.RED, reorder=False)
    assert [len(c.glyphs) for c in cmds] == [255, 255, 90] and all(c.color == Color.RED for c in cmds)
    mixed = [g(0, 20), g(8, 20, face=5), g(16, 20, face=5), g(24, 20, size=24)]
    runs = [(c.face, c.size_px, len(c.glyphs)) for c in glyph_commands(mixed, 1)]
    assert runs == [(1, 16, 1), (5, 16, 2), (1, 24, 1)]
    assert glyph_commands([], 1) == []


def test_serpentine_joins_lines_into_one_command() -> None:
    lines = [[g(10 + 20 * i, 30 + 24 * n, line=n) for i in range(18)] for n in range(3)]
    flat = [x for line in lines for x in line]
    assert len(glyph_commands(flat, 1, reorder=False)) == 3  # 350 px back to the left margin each line
    cmds = glyph_commands(flat, 1)
    assert len(cmds) == 1
    assert sorted(walk(cmds)) == sorted((1, 16, 5, p.x, p.y) for p in flat)


@pytest.mark.fonts
def test_real_block_commands_validate(fonts: Any) -> None:
    text = ("Tiếng Việt có dấu — مرحبا بالعالم 123 — שלום עולם — สวัสดีครับ — नमस्ते दुनिया — "
            "你好，世界 — こんにちは — 안녕하세요 😀")
    block = layout_text(text, fonts, width=380, size_px=16, language="en")
    cmds = block.commands(10, 5, Color.BLACK)
    layout = Layout(400, 300, 0, Color.WHITE, tuple(cmds))
    data = encode_layout(layout)
    assert len(data) <= LAYOUT_HARD_MAX
    decoded = decode_layout(data)
    check_strikes(decoded, fonts.has_strike)
    check_panel(decoded, 400, 300)
    drawn = sorted(walk(list(decoded.commands)))
    expected = sorted((p.face_id, p.size_px, p.glyph_id, p.x + 10, p.y + 5) for p in block.glyphs)
    assert drawn == expected


@pytest.mark.fonts
def test_missing_strike_is_a_fontpack_mismatch(dev_fonts: Any) -> None:
    from cremind_tag.protocol.layout import validate_layout

    layout = Layout(100, 50, 0, Color.WHITE, tuple(glyph_commands([g(4, 30, size=32)], 1)))
    assert validate_layout(encode_layout(layout), dev_fonts.has_strike) == Status.FONTPACK_MISMATCH
