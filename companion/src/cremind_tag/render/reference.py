"""Normative layout renderer (docs/protocol.md §4.4); C must match it bit for bit.

Commands paint a logical ``W x H`` canvas in order (last writer wins), clipped
to the canvas and to the logical region that maps onto the native rows being
produced. ``render_strip`` renders native rows ``[y0, y0 + rows)`` of one plane
the way a bridge does; ``render_frame`` renders every row of every plane, and
by construction a strip equals the same rows of the frame.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from ..fontpack.format import GlyphBitmap
from ..protocol.ids import BRIDGE_STRIP_ROWS, Color, Status
from ..protocol.layout import (
    ICON_FACE,
    Clear,
    Glyphs,
    Icon,
    Layout,
    LayoutError,
    Line,
    Progress,
    Qr,
    Rect,
    check_panel,
    check_strikes,
    decode_layout,
    qr_code,
)
from ..protocol.msgs import TagCaps


class GlyphSource(Protocol):
    """Where strikes come from (a ``fontpack.format.FontPack`` on a bridge)."""

    def has_strike(self, face: int, size_px: int) -> bool: ...

    def glyph(self, face: int, size_px: int, glyph_id: int) -> GlyphBitmap | None: ...


@dataclass(frozen=True, slots=True)
class Panel:
    """Native panel geometry and plane encoding (tag CAPS)."""

    width: int
    height: int
    planes: int
    plane_flags: int

    @classmethod
    def from_caps(cls, caps: TagCaps) -> Panel:
        return cls(caps.width, caps.height, caps.planes, caps.plane_flags)

    @property
    def row_bytes(self) -> int:
        return (self.width + 7) // 8

    @property
    def plane_len(self) -> int:
        return self.row_bytes * self.height


@dataclass(frozen=True, slots=True)
class Frame:
    planes: tuple[bytes, ...]
    digest: bytes


def frame_digest(planes: tuple[bytes, ...] | list[bytes]) -> bytes:
    """SHA-256 over plane 0 then plane 1 (when present)."""
    return hashlib.sha256(b"".join(planes)).digest()


class _Canvas:
    """Logical pixels (one colour byte each) with a half-open clip rectangle."""

    def __init__(self, width: int, height: int, background: int, clip: tuple[int, int, int, int]) -> None:
        self.width = width
        self.pixels = bytearray([background]) * (width * height)
        x0, y0, x1, y1 = clip
        self.clip = (max(x0, 0), max(y0, 0), min(x1, width), min(y1, height))

    def fill(self, x0: int, y0: int, x1: int, y1: int, color: int) -> None:
        cx0, cy0, cx1, cy1 = self.clip
        x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
        if x0 >= x1 or y0 >= y1:
            return
        run = bytes([color]) * (x1 - x0)
        for y in range(y0, y1):
            start = y * self.width + x0
            self.pixels[start : start + len(run)] = run

    def blit(self, glyph: GlyphBitmap, left: int, top: int, color: int) -> None:
        """Paint the glyph's 1 bits with its top-left at (left, top)."""
        row_bytes = (glyph.width + 7) // 8
        for row in range(glyph.height):
            bits = glyph.bitmap[row * row_bytes : (row + 1) * row_bytes]
            for col in range(glyph.width):
                if bits[col >> 3] & (0x80 >> (col & 7)):
                    self.fill(left + col, top + row, left + col + 1, top + row + 1, color)


def _paint_rect(canvas: _Canvas, x: int, y: int, w: int, h: int, border: int, color: int) -> None:
    if w == 0 or h == 0:
        return
    if border == 0:
        canvas.fill(x, y, x + w, y + h, color)
        return
    # The four bands whose union is lx < x+b || lx >= x+w-b || ly < y+b || ly >= y+h-b.
    canvas.fill(x, y, x + w, min(y + border, y + h), color)
    canvas.fill(x, max(y + h - border, y), x + w, y + h, color)
    canvas.fill(x, y, min(x + border, x + w), y + h, color)
    canvas.fill(max(x + w - border, x), y, x + w, y + h, color)


def _paint_line(canvas: _Canvas, cmd: Line) -> None:
    x0, y0, x1, y1 = cmd.x0, cmd.y0, cmd.x1, cmd.y1
    o = (cmd.width - 1) // 2
    dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
    dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        canvas.fill(x0 - o, y0 - o, x0 - o + cmd.width, y0 - o + cmd.width, cmd.color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _paint(canvas: _Canvas, layout: Layout, glyphs: GlyphSource) -> None:
    for cmd in layout.commands:
        if isinstance(cmd, Clear):
            canvas.fill(0, 0, layout.width, layout.height, cmd.color)
        elif isinstance(cmd, Glyphs):
            pen_x, pen_y = cmd.origin_x, cmd.origin_y
            for entry in cmd.glyphs:
                pen_x += entry.dx
                pen_y += entry.dy
                glyph = glyphs.glyph(cmd.face, cmd.size_px, entry.glyph_id)
                if glyph is not None and glyph.bitmap:
                    canvas.blit(glyph, pen_x + glyph.bearing_x, pen_y - glyph.bearing_y, cmd.color)
        elif isinstance(cmd, Icon):
            glyph = glyphs.glyph(ICON_FACE, cmd.size_px, cmd.icon)
            if glyph is not None and glyph.bitmap:
                canvas.blit(glyph, cmd.x, cmd.y, cmd.color)
        elif isinstance(cmd, Line):
            _paint_line(canvas, cmd)
        elif isinstance(cmd, Rect):
            _paint_rect(canvas, cmd.x, cmd.y, cmd.w, cmd.h, cmd.border, cmd.color)
        elif isinstance(cmd, Progress):
            _paint_rect(canvas, cmd.x, cmd.y, cmd.w, cmd.h, 1, cmd.color)
            if cmd.w > 4 and cmd.h > 4:
                fill = (cmd.w - 4) * min(cmd.value, cmd.max) // cmd.max if cmd.max else 0
                canvas.fill(cmd.x + 2, cmd.y + 2, cmd.x + 2 + fill, cmd.y + cmd.h - 2, cmd.color)
        elif isinstance(cmd, Qr):
            symbol = qr_code(cmd.text, cmd.ecc)
            m = cmd.module_px
            for my in range(symbol.get_size()):
                for mx in range(symbol.get_size()):
                    if symbol.get_module(mx, my):
                        canvas.fill(cmd.x + mx * m, cmd.y + my * m, cmd.x + (mx + 1) * m,
                                    cmd.y + (my + 1) * m, cmd.color)


def _strip_clip(layout: Layout, panel: Panel, y0: int, y1: int) -> tuple[int, int, int, int]:
    """Logical rectangle that maps onto native rows [y0, y1) (§4.4 Rotation)."""
    w, h, hn = layout.width, layout.height, panel.height
    return {
        0: (0, y0, w, y1),
        1: (y0, 0, y1, h),
        2: (0, hn - y1, w, hn - y0),
        3: (hn - y1, 0, hn - y0, h),
    }[layout.rotation]


def _native_row(canvas: _Canvas, layout: Layout, panel: Panel, ny: int) -> bytes:
    """Colours of native row ``ny``, nx = 0 .. Wn-1."""
    px, w, hn = canvas.pixels, layout.width, panel.height
    if layout.rotation == 0:  # nx = lx, ny = ly
        return bytes(px[ny * w : (ny + 1) * w])
    if layout.rotation == 1:  # nx = Wn-1-ly, ny = lx
        return bytes(px[ny::w][::-1])
    if layout.rotation == 2:  # nx = Wn-1-lx, ny = Hn-1-ly
        ly = hn - 1 - ny
        return bytes(px[ly * w : (ly + 1) * w][::-1])
    return bytes(px[hn - 1 - ny :: w])  # nx = ly, ny = Hn-1-lx


def _plane_bits(panel: Panel, plane: int) -> tuple[bytes, str]:
    """Translation table colour -> '0'/'1' and the padding bit for ``plane``."""
    white0 = panel.plane_flags & 1
    red1 = (panel.plane_flags >> 1) & 1
    if plane == 0:
        # planes=1: red renders as black; planes=2: red takes plane 0's white value.
        red = white0 if panel.planes == 2 else 1 - white0
        bits = {Color.WHITE: white0, Color.BLACK: 1 - white0, Color.RED: red}
        pad = white0
    else:
        bits = {Color.WHITE: 1 - red1, Color.BLACK: 1 - red1, Color.RED: red1}
        pad = 1 - red1
    table = bytearray(range(256))
    for color, bit in bits.items():
        table[color] = ord("1") if bit else ord("0")
    return bytes(table), "1" if pad else "0"


def _prepare(layout: bytes | Layout, panel: Panel, glyphs: GlyphSource) -> Layout:
    if isinstance(layout, bytes | bytearray):
        layout = decode_layout(bytes(layout))
    check_strikes(layout, glyphs.has_strike)
    check_panel(layout, panel.width, panel.height)
    if panel.planes not in (1, 2):
        raise LayoutError(Status.INVALID, f"panel with {panel.planes} planes")
    return layout


def _rows(canvas: _Canvas, layout: Layout, panel: Panel, plane: int, y0: int, y1: int) -> bytes:
    table, pad = _plane_bits(panel, plane)
    padding = pad * (panel.row_bytes * 8 - panel.width)
    out = bytearray()
    for ny in range(y0, y1):
        bits = _native_row(canvas, layout, panel, ny).translate(table).decode("ascii") + padding
        out += int(bits, 2).to_bytes(panel.row_bytes, "big")
    return bytes(out)


def render_strip(layout: bytes | Layout, panel: Panel, glyphs: GlyphSource, plane: int, y0: int,
                 rows: int = BRIDGE_STRIP_ROWS) -> bytes:
    """Native rows [y0, min(y0 + rows, Hn)) of ``plane``, as a bridge renders them."""
    layout = _prepare(layout, panel, glyphs)
    if not 0 <= plane < panel.planes or not 0 <= y0 < panel.height or rows < 1:
        raise ValueError("strip outside the frame")
    y1 = min(y0 + rows, panel.height)
    canvas = _Canvas(layout.width, layout.height, layout.background, _strip_clip(layout, panel, y0, y1))
    _paint(canvas, layout, glyphs)
    return _rows(canvas, layout, panel, plane, y0, y1)


def render_frame(layout: bytes | Layout, panel: Panel, glyphs: GlyphSource) -> Frame:
    """Every plane of the frame and its digest."""
    layout = _prepare(layout, panel, glyphs)
    canvas = _Canvas(layout.width, layout.height, layout.background, (0, 0, layout.width, layout.height))
    _paint(canvas, layout, glyphs)
    planes = tuple(_rows(canvas, layout, panel, p, 0, panel.height) for p in range(panel.planes))
    return Frame(planes, frame_digest(planes))
