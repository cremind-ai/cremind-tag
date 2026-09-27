"""Positioned glyphs -> GLYPHS commands (docs/protocol.md §4.2, §4.4).

A GLYPHS command draws a run from one strike ``(face, size_px)`` in one colour:
the pen starts at ``(origin_x, origin_y)`` and each entry moves it by
``(dx, dy)`` (i8) **then** draws. Consecutive glyphs with the same face, size
and colour share a command while every delta fits an i8 and the run has fewer
than 255 glyphs; otherwise a new command starts at that glyph's origin.

Lines of one text block are emitted alternately left-to-right and
right-to-left ("serpentine"): the pen then steps one line down with a small
``dx`` instead of jumping back across the box, so a multi-line paragraph in one
face usually needs a single command. Glyphs of one colour never depend on
drawing order, so this changes no pixel.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import groupby

from cremind_tag.protocol.layout import Glyph, Glyphs

from .engine import PositionedGlyph

I8_MIN, I8_MAX = -128, 127
MAX_RUN = 255


def serpentine(glyphs: Sequence[PositionedGlyph]) -> list[PositionedGlyph]:
    """Every other line of ``glyphs`` (grouped by ``line``) reversed."""
    out: list[PositionedGlyph] = []
    for n, (_line, group) in enumerate(groupby(glyphs, key=lambda g: g.line)):
        items = list(group)
        out.extend(reversed(items) if n % 2 else items)
    return out


def glyph_commands(glyphs: Iterable[PositionedGlyph], color: int, *, reorder: bool = True) -> list[Glyphs]:
    """GLYPHS commands for absolute-positioned ``glyphs`` in ``color`` (see the module docstring)."""
    ordered = serpentine(list(glyphs)) if reorder else list(glyphs)
    commands: list[Glyphs] = []
    run: list[Glyph] = []
    key: tuple[int, int] | None = None
    origin = (0, 0)
    prev = (0, 0)

    def flush() -> None:
        if run and key is not None:
            commands.append(Glyphs(key[0], key[1], color, origin[0], origin[1], tuple(run)))

    for g in ordered:
        k = (g.face_id, g.size_px)
        dx, dy = g.x - prev[0], g.y - prev[1]
        if k == key and len(run) < MAX_RUN and I8_MIN <= dx <= I8_MAX and I8_MIN <= dy <= I8_MAX:
            run.append(Glyph(g.glyph_id, dx, dy))
        else:
            flush()
            key = k
            origin = (g.x, g.y)
            run = [Glyph(g.glyph_id, 0, 0)]
        prev = (g.x, g.y)
    flush()
    return commands


def command_glyphs(cmd: Glyphs) -> list[tuple[int, int, int]]:
    """(glyph id, x, y) of every entry of a GLYPHS command (the pen walk of §4.4)."""
    x, y = cmd.origin_x, cmd.origin_y
    out = []
    for g in cmd.glyphs:
        x += g.dx
        y += g.dy
        out.append((g.glyph_id, x, y))
    return out
