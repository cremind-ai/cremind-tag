"""Rasterise font faces into 1-bpp strikes with FreeType (docs/fontpack.md §1).

Every glyph id of a face is loaded with ``FT_LOAD_RENDER | FT_LOAD_TARGET_MONO``
plus the flags of its hinting mode, at ``FT_Set_Pixel_Sizes(face, 0, size)``.
Glyph metrics follow the pack format: ``bearing_x = bitmap_left``,
``bearing_y = bitmap_top``, ``advance = round(advance.x / 64)`` (half up);
strike metrics are the ceilings of ``size->metrics`` ascender, −descender and
height. Icons are drawn into exact ``size × size`` cells.

Results are deterministic for a given FreeType version, which builds pin
(``render.freetype`` in the manifest).
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import struct
from dataclasses import dataclass, field
from pathlib import Path

import freetype
import freetype.raw as ft

from cremind_tag.fontpack.format import EMPTY_GLYPH, GlyphBitmap

LOAD_BASE = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO
LOAD_FLAGS = {
    "native": LOAD_BASE,
    "autohint": LOAD_BASE | freetype.FT_LOAD_FORCE_AUTOHINT,
    "none": LOAD_BASE | freetype.FT_LOAD_NO_HINTING,
}
# Pack format limits (u8 width/height, i8 bearings).
MAX_DIM = 0xFF
BEARING_MIN, BEARING_MAX = -128, 127

_ENTRY = struct.Struct("<BBbbH")
_configured = False


def freetype_version() -> str:
    return ".".join(str(v) for v in freetype.version())


def freetype_py_version() -> str:
    return importlib.metadata.version("freetype-py")


def _configure_library() -> None:
    """Pin FreeType module properties that change rasterisation, whatever the build defaults."""
    global _configured
    if _configured:
        return
    lib = freetype.get_handle()
    for module, prop, value in ((b"truetype", b"interpreter-version", 40),
                                (b"cff", b"hinting-engine", 1),            # FT_HINTING_ADOBE
                                (b"cff", b"no-stem-darkening", 1),
                                (b"autofitter", b"no-stem-darkening", 1)):
        error = ft.FT_Property_Set(lib, module, prop, ctypes.byref(ctypes.c_uint(value)))
        if error:
            raise RuntimeError(f"FT_Property_Set({module.decode()}, {prop.decode()}) failed: {error}")
    _configured = True


def open_face(path: Path, variations: tuple[tuple[str, float], ...] = ()) -> freetype.Face:
    """Open ``path`` and apply design-space variation coordinates (e.g. ``wght`` 400)."""
    _configure_library()
    face = freetype.Face(str(path))
    if variations:
        if not face.has_multiple_masters:
            raise ValueError(f"{path.name} is not a variable font")
        info = face.get_variation_info()
        axes = {a.tag: a for a in info.axes}
        wanted = dict(variations)
        if unknown := set(wanted) - set(axes):
            raise ValueError(f"{path.name}: no variation axes {sorted(unknown)}")
        face.set_var_design_coords(tuple(wanted.get(a.tag, a.default) for a in info.axes))
    return face


# --------------------------------------------------------------------------- font facts


@dataclass(frozen=True)
class FontFacts:
    """What the manifest records about a font file, read from the file itself."""

    num_glyphs: int
    hinting: str
    """``cff`` (CFF/CFF2 outlines), ``truetype-bytecode`` (fpgm/prep present) or ``none``."""
    names: dict[int, str]
    """Windows-English ``name`` records 0 (copyright), 1, 5 (version), 7 (trademark), 16."""
    tables: tuple[str, ...]


def font_facts(path: Path) -> FontFacts:
    data = path.read_bytes()
    count = struct.unpack_from(">H", data, 4)[0]
    tables = tuple(sorted(data[12 + 16 * i:16 + 16 * i].decode("latin-1") for i in range(count)))
    if "CFF " in tables or "CFF2" in tables:
        hinting = "cff"
    elif "fpgm" in tables or "prep" in tables:
        hinting = "truetype-bytecode"
    else:
        hinting = "none"
    face = freetype.Face(str(path))
    names: dict[int, str] = {}
    for i in range(face.sfnt_name_count):
        rec = face.get_sfnt_name(i)
        if rec.platform_id == 3 and rec.encoding_id in (1, 10) and rec.language_id == 0x409 \
                and rec.name_id in (0, 1, 5, 7, 16):
            names[rec.name_id] = rec.string.decode("utf-16-be")
    return FontFacts(face.num_glyphs, hinting, names, tables)


# --------------------------------------------------------------------------- bitmaps


def _crop(width: int, height: int, bits: bytes, left: int, top: int, new_w: int, new_h: int) -> bytes:
    """Sub-rectangle of a 1-bpp MSB-first bitmap (slow path, rare)."""
    src_rb = (width + 7) >> 3
    dst_rb = (new_w + 7) >> 3
    out = bytearray(dst_rb * new_h)
    for y in range(new_h):
        row = bits[(top + y) * src_rb:(top + y + 1) * src_rb]
        for x in range(new_w):
            sx = left + x
            if row[sx >> 3] & (0x80 >> (sx & 7)):
                out[y * dst_rb + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out)


@dataclass
class StrikeStats:
    glyphs: int = 0
    empty: int = 0
    """Glyphs without ink, stored as empty entries (advance kept)."""
    clipped: list[int] = field(default_factory=list)
    """Glyph ids cropped to the format's 255 px / ±127 px limits."""
    dropped: list[int] = field(default_factory=list)
    """Glyph ids whose ink lies wholly outside what a glyph entry can address (stored empty)."""
    errors: list[int] = field(default_factory=list)
    """Glyph ids FreeType failed to load (stored empty)."""


def _fit(width: int, height: int, bx: int, by: int, bits: bytes, gid: int,
         stats: StrikeStats) -> tuple[int, int, int, int, bytes]:
    """Crop a glyph to the pack format limits; drop it when nothing addressable remains."""
    if bx > BEARING_MAX or by < BEARING_MIN:
        stats.dropped.append(gid)
        return 0, 0, 0, 0, b""
    left = max(0, BEARING_MIN - bx)
    top = max(0, by - BEARING_MAX)
    new_w = min(width - left, MAX_DIM)
    new_h = min(height - top, MAX_DIM)
    if new_w <= 0 or new_h <= 0:
        stats.dropped.append(gid)
        return 0, 0, 0, 0, b""
    stats.clipped.append(gid)
    return new_w, new_h, bx + left, by - top, _crop(width, height, bits, left, top, new_w, new_h)


def _slot_bitmap(slot: ft.FT_GlyphSlotRec) -> tuple[int, int, bytes]:
    """The rendered mono bitmap as tightly packed rows with zero padding bits."""
    bm = slot.bitmap
    width, rows, pitch = bm.width, bm.rows, bm.pitch
    if width == 0 or rows == 0:
        return 0, 0, b""
    row_bytes = (width + 7) >> 3
    step = abs(pitch)
    raw = ctypes.string_at(bm.buffer, step * rows)
    if pitch < 0:  # bottom-up flow; FreeType renders top-down, kept for completeness
        raw = b"".join(raw[(rows - 1 - r) * step:(rows - r) * step] for r in range(rows))
    if step != row_bytes:
        raw = b"".join(raw[r * step:r * step + row_bytes] for r in range(rows))
    if width & 7:
        mask = (0xFF00 >> (width & 7)) & 0xFF
        last = row_bytes - 1
        if any(raw[r * row_bytes + last] & ~mask & 0xFF for r in range(rows)):
            buf = bytearray(raw)
            for r in range(rows):
                buf[r * row_bytes + last] &= mask
            raw = bytes(buf)
    return width, rows, raw


def strike_metrics(face: freetype.Face) -> tuple[int, int, int]:
    """(ascent, descent, line_height) = ceilings of the size's 26.6 metrics."""
    m = face._FT_Face.contents.size.contents.metrics
    return -(-m.ascender // 64), -(m.descender // 64), -(-m.height // 64)


def render_strike(face: freetype.Face, size: int, mode: str) -> tuple[tuple[int, int, int], list[GlyphBitmap],
                                                                     StrikeStats]:
    """Every glyph of ``face`` at ``size`` px."""
    flags = LOAD_FLAGS[mode]
    face.set_pixel_sizes(0, size)
    raw_face = face._FT_Face
    slot = raw_face.contents.glyph.contents
    stats = StrikeStats()
    glyphs: list[GlyphBitmap] = []
    for gid in range(face.num_glyphs):
        if ft.FT_Load_Glyph(raw_face, gid, flags):
            stats.errors.append(gid)
            glyphs.append(EMPTY_GLYPH)
            continue
        width, height, bits = _slot_bitmap(slot)
        bx, by = slot.bitmap_left, slot.bitmap_top
        advance = (slot.advance.x + 32) >> 6
        if width and height and (width > MAX_DIM or height > MAX_DIM or not BEARING_MIN <= bx <= BEARING_MAX
                                  or not BEARING_MIN <= by <= BEARING_MAX):
            width, height, bx, by, bits = _fit(width, height, bx, by, bits, gid, stats)
        # FreeType renders an empty outline (e.g. space) as a blank 1x1 bitmap; a glyph
        # without ink draws nothing either way, so it is stored empty.
        if not (width and height) or not bits.strip(b"\0"):
            width = height = 0
            bits = b""
            bx = by = 0
            stats.empty += 1
        glyphs.append(GlyphBitmap(width, height, bx, by, max(0, min(advance, 0xFFFF)), bits))
    stats.glyphs = len(glyphs)
    return strike_metrics(face), glyphs, stats


def icon_cell(face: freetype.Face, glyph_index: int, size: int, mode: str) -> GlyphBitmap:
    """One icon as an exact ``size × size`` cell with zero bearings and advance ``size``.

    The cell is centred on the glyph's design box: horizontally on its advance,
    vertically on the font's ascender–descender span (for Material Icons the em
    square, so the designed padding is kept). Ink outside the cell is clipped.
    """
    face.set_pixel_sizes(0, size)
    raw_face = face._FT_Face
    if ft.FT_Load_Glyph(raw_face, glyph_index, LOAD_FLAGS[mode]):
        raise ValueError(f"icon glyph {glyph_index} failed to load at {size} px")
    slot = raw_face.contents.glyph.contents
    width, height, bits = _slot_bitmap(slot)
    m = raw_face.contents.size.contents.metrics
    cell_left = (slot.advance.x - size * 64 + 64) // 128          # round((advance − size) / 2), in px
    cell_top = (m.ascender + m.descender + size * 64 + 64) // 128  # px above the baseline
    dx = slot.bitmap_left - cell_left
    dy = cell_top - slot.bitmap_top
    row_bytes = (size + 7) >> 3
    out = bytearray(row_bytes * size)
    src_rb = (width + 7) >> 3
    for y in range(height):
        cy = y + dy
        if not 0 <= cy < size:
            continue
        for x in range(width):
            cx = x + dx
            if 0 <= cx < size and bits[y * src_rb + (x >> 3)] & (0x80 >> (x & 7)):
                out[cy * row_bytes + (cx >> 3)] |= 0x80 >> (cx & 7)
    return GlyphBitmap(size, size, 0, 0, size, bytes(out))


# --------------------------------------------------------------------------- worker tasks


@dataclass(frozen=True)
class StrikeTask:
    """One (face, size) strike to render; picklable for process pools."""

    face_id: int
    path: str
    size: int
    mode: str
    variations: tuple[tuple[str, float], ...] = ()
    icon_codepoints: tuple[int, ...] | None = None
    """For the icon face: codepoint per icon id 1..n (glyph 0 stays empty)."""


@dataclass(frozen=True)
class StrikeResult:
    """Compact strike: packed per-glyph entries + concatenated bitmaps."""

    face_id: int
    size: int
    ascent: int
    descent: int
    line_height: int
    entries: bytes
    bitmaps: bytes
    stats: StrikeStats

    def glyphs(self) -> tuple[GlyphBitmap, ...]:
        out = []
        pos = 0
        for width, height, bx, by, advance in _ENTRY.iter_unpack(self.entries):
            n = height * ((width + 7) >> 3)
            out.append(GlyphBitmap(width, height, bx, by, advance, self.bitmaps[pos:pos + n]))
            pos += n
        return tuple(out)


def render_task(task: StrikeTask) -> StrikeResult:
    face = open_face(Path(task.path), task.variations)
    if task.icon_codepoints is None:
        metrics, glyphs, stats = render_strike(face, task.size, task.mode)
    else:
        glyphs = [EMPTY_GLYPH]
        for cp in task.icon_codepoints:
            gi = face.get_char_index(cp)
            if gi == 0:
                raise ValueError(f"icon codepoint {cp:#x} is not in {Path(task.path).name}")
            glyphs.append(icon_cell(face, gi, task.size, task.mode))
        metrics = (task.size, 0, task.size)
        stats = StrikeStats(glyphs=len(glyphs), empty=1)
    entries = b"".join(_ENTRY.pack(g.width, g.height, g.bearing_x, g.bearing_y, g.advance) for g in glyphs)
    return StrikeResult(task.face_id, task.size, *metrics, entries, b"".join(g.bitmap for g in glyphs), stats)
