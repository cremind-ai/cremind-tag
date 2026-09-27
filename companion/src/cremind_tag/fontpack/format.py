"""Font pack binary format, version 1 (docs/fontpack.md §2).

``build_pack`` writes a pack from already-rasterised strikes: faces sorted by
id, strikes by (face, size), sections contiguous in the documented order,
identical bitmaps stored once in order of first use (strikes in table order,
glyph ids ascending), so the output is a pure function of its input.
``FontPack`` parses and validates a pack and serves glyphs to the renderer.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from ..protocol.crc import crc32
from ..protocol.ids import (
    FONTPACK_EMPTY_BITMAP,
    FONTPACK_FACE_RECORD_SIZE,
    FONTPACK_GLYPH_ENTRY_SIZE,
    FONTPACK_HEADER_SIZE,
    FONTPACK_ID_LEN,
    FONTPACK_MAGIC,
    FONTPACK_STRIKE_RECORD_SIZE,
    FONTPACK_VERSION,
    Status,
)

FACE_FLAG_ICON = 0x0001
FACE_FLAG_CJK = 0x0002
FACE_FLAG_RTL = 0x0004
MANIFEST_ID_LEN = 8
HEADER_CRC_OFFSET = 124

_HEADER = struct.Struct("<IHHII8s32sHHIIIIII8s32sI")
_FACE = struct.Struct("<HHIII")
_STRIKE = struct.Struct("<HBBIIhhHHI")
_GLYPH = struct.Struct("<IBBbbHH")
assert _HEADER.size == FONTPACK_HEADER_SIZE and _FACE.size == FONTPACK_FACE_RECORD_SIZE
assert _STRIKE.size == FONTPACK_STRIKE_RECORD_SIZE and _GLYPH.size == FONTPACK_GLYPH_ENTRY_SIZE


class FontPackError(ValueError):
    """Invalid font pack; ``status`` is the spec status a bridge would report."""

    def __init__(self, message: str, status: Status = Status.INVALID) -> None:
        super().__init__(message)
        self.status = status


def bitmap_size(width: int, height: int) -> int:
    """Bytes of a 1-bpp bitmap: ``height`` rows of ceil(width / 8) bytes."""
    return height * ((width + 7) // 8)


@dataclass(frozen=True, slots=True)
class GlyphBitmap:
    """One glyph of a strike; ``bitmap`` is MSB-first rows, 1 = ink."""

    width: int
    height: int
    bearing_x: int
    bearing_y: int
    advance: int
    bitmap: bytes = b""

    @property
    def empty(self) -> bool:
        return self.width == 0 or self.height == 0


EMPTY_GLYPH = GlyphBitmap(0, 0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class Face:
    face_id: int
    flags: int
    name: str
    scripts: str  # comma-separated ISO 15924 codes
    glyph_count: int


@dataclass(frozen=True, slots=True)
class StrikeSpec:
    """Writer input: every glyph of one (face, size) strike, by glyph id."""

    face_id: int
    size_px: int
    ascent: int
    descent: int
    line_height: int
    glyphs: tuple[GlyphBitmap, ...]


@dataclass(frozen=True, slots=True)
class Strike:
    """Reader view of a strike record."""

    face_id: int
    size_px: int
    flags: int
    glyph_count: int
    index_off: int
    ascent: int
    descent: int
    line_height: int
    index_crc32: int


def _check_glyph(glyph: GlyphBitmap, where: str) -> None:
    if not (0 <= glyph.width <= 0xFF and 0 <= glyph.height <= 0xFF):
        raise FontPackError(f"{where}: size {glyph.width}x{glyph.height}")
    if not (-128 <= glyph.bearing_x <= 127 and -128 <= glyph.bearing_y <= 127 and 0 <= glyph.advance <= 0xFFFF):
        raise FontPackError(f"{where}: metrics out of range")
    if len(glyph.bitmap) != bitmap_size(glyph.width, glyph.height):
        raise FontPackError(f"{where}: bitmap is {len(glyph.bitmap)} bytes")
    pad = -glyph.width % 8
    if pad and not glyph.empty:
        row_bytes = (glyph.width + 7) // 8
        mask = (1 << pad) - 1
        if any(glyph.bitmap[r * row_bytes + row_bytes - 1] & mask for r in range(glyph.height)):
            raise FontPackError(f"{where}: row padding bits must be 0")


def build_pack(faces: Iterable[Face], strikes: Iterable[StrikeSpec], manifest_id: bytes = bytes(MANIFEST_ID_LEN)) -> bytes:
    """Serialise a pack (docs/fontpack.md §2)."""
    face_list = sorted(faces, key=lambda f: f.face_id)
    strike_list = sorted(strikes, key=lambda s: (s.face_id, s.size_px))
    if len(manifest_id) != MANIFEST_ID_LEN:
        raise FontPackError("manifest id must be 8 bytes")
    face_ids = [f.face_id for f in face_list]
    if len(set(face_ids)) != len(face_ids):
        raise FontPackError("duplicate face id")
    keys = [(s.face_id, s.size_px) for s in strike_list]
    if len(set(keys)) != len(keys):
        raise FontPackError("duplicate strike")
    for strike in strike_list:
        if strike.face_id not in face_ids:
            raise FontPackError(f"strike ({strike.face_id}, {strike.size_px}) has no face")
        for gid, glyph in enumerate(strike.glyphs):
            _check_glyph(glyph, f"strike ({strike.face_id}, {strike.size_px}) glyph {gid}")

    strings = bytearray()
    face_table = bytearray()
    for face in face_list:
        name_off = len(strings)
        strings += face.name.encode() + b"\0"
        scripts_off = len(strings)
        strings += face.scripts.encode() + b"\0"
        face_table += _FACE.pack(face.face_id, face.flags, name_off, scripts_off, face.glyph_count)

    face_off = FONTPACK_HEADER_SIZE
    strike_off = face_off + len(face_table)
    string_off = strike_off + len(strike_list) * FONTPACK_STRIKE_RECORD_SIZE
    index_off = string_off + len(strings)
    bitmap_off = index_off + sum(len(s.glyphs) for s in strike_list) * FONTPACK_GLYPH_ENTRY_SIZE

    bitmaps = bytearray()
    stored: dict[bytes, int] = {}
    strike_table = bytearray()
    indexes = bytearray()
    for strike in strike_list:
        index = bytearray()
        for glyph in strike.glyphs:
            if glyph.empty:
                offset = FONTPACK_EMPTY_BITMAP
            else:
                offset = stored.setdefault(glyph.bitmap, len(bitmaps))
                if offset == len(bitmaps):
                    bitmaps += glyph.bitmap
            index += _GLYPH.pack(offset, glyph.width, glyph.height, glyph.bearing_x, glyph.bearing_y,
                                 glyph.advance, 0)
        strike_table += _STRIKE.pack(strike.face_id, strike.size_px, 0, len(strike.glyphs),
                                     index_off + len(indexes), strike.ascent, strike.descent,
                                     strike.line_height, 0, crc32(bytes(index)))
        indexes += index

    body = bytes(face_table + strike_table + strings + indexes + bitmaps)
    total = FONTPACK_HEADER_SIZE + len(body)
    content_hash = hashlib.sha256(body).digest()
    header = _HEADER.pack(FONTPACK_MAGIC, FONTPACK_VERSION, FONTPACK_HEADER_SIZE, 0, total,
                          content_hash[:FONTPACK_ID_LEN], content_hash, len(face_list), len(strike_list),
                          face_off, strike_off, string_off, len(strings), bitmap_off, len(bitmaps),
                          manifest_id, bytes(32), 0)
    header = header[:HEADER_CRC_OFFSET] + crc32(header[:HEADER_CRC_OFFSET]).to_bytes(4, "little")
    return header + body


class FontPack:
    """A validated font pack (docs/fontpack.md §2, "Validation").

    ``verify_content`` adds the install-time check of ``content_hash`` (and of
    the pack id derived from it); boot-time validation skips it. Buffers longer
    than ``total`` (a flash slot) are accepted; the tail is ignored.
    """

    def __init__(self, data: bytes, *, verify_content: bool = True) -> None:
        if len(data) < FONTPACK_HEADER_SIZE:
            raise FontPackError("shorter than the header")
        (magic, version, header_size, self.flags, total, self.pack_id, self.content_hash, face_count,
         strike_count, face_off, strike_off, string_off, string_size, bitmap_off, bitmap_size_,
         self.manifest_id, _reserved, header_crc) = _HEADER.unpack_from(data)
        if magic != FONTPACK_MAGIC:
            raise FontPackError("bad magic")
        if version != FONTPACK_VERSION or header_size != FONTPACK_HEADER_SIZE:
            raise FontPackError(f"unsupported version {version} / header size {header_size}", Status.UNSUPPORTED)
        if header_crc != crc32(data[:HEADER_CRC_OFFSET]):
            raise FontPackError("header CRC mismatch", Status.CRC_ERROR)
        if not FONTPACK_HEADER_SIZE <= total <= len(data):
            raise FontPackError(f"total size {total} outside the buffer")
        self.data = bytes(data[:total])
        self.total_size = total

        def inside(offset: int, size: int, what: str) -> None:
            if offset < FONTPACK_HEADER_SIZE or offset + size > total:
                raise FontPackError(f"{what} outside the pack")

        inside(face_off, face_count * FONTPACK_FACE_RECORD_SIZE, "face table")
        inside(strike_off, strike_count * FONTPACK_STRIKE_RECORD_SIZE, "strike table")
        inside(string_off, string_size, "string table")
        inside(bitmap_off, bitmap_size_, "bitmap area")
        self._bitmap_off = bitmap_off
        strings = self.data[string_off : string_off + string_size]

        def string(offset: int) -> str:
            end = strings.find(b"\0", offset)
            if offset >= len(strings) or end < 0:
                raise FontPackError("string outside the string table")
            try:
                return strings[offset:end].decode("utf-8")
            except UnicodeDecodeError:
                raise FontPackError("string is not UTF-8") from None

        self.faces: list[Face] = []
        for i in range(face_count):
            face_id, flags, name_off, scripts_off, glyph_count = _FACE.unpack_from(
                self.data, face_off + i * FONTPACK_FACE_RECORD_SIZE)
            if self.faces and face_id <= self.faces[-1].face_id:
                raise FontPackError("face table not sorted by face id")
            self.faces.append(Face(face_id, flags, string(name_off), string(scripts_off), glyph_count))
        face_ids = {f.face_id for f in self.faces}

        self.strikes: list[Strike] = []
        for i in range(strike_count):
            (face_id, size_px, flags, glyph_count, index_off, ascent, descent, line_height, _reserved,
             index_crc) = _STRIKE.unpack_from(self.data, strike_off + i * FONTPACK_STRIKE_RECORD_SIZE)
            strike = Strike(face_id, size_px, flags, glyph_count, index_off, ascent, descent, line_height, index_crc)
            if self.strikes and (strike.face_id, strike.size_px) <= (self.strikes[-1].face_id, self.strikes[-1].size_px):
                raise FontPackError("strike table not sorted by (face, size)")
            if strike.face_id not in face_ids:
                raise FontPackError(f"strike ({strike.face_id}, {strike.size_px}) has no face")
            index_len = strike.glyph_count * FONTPACK_GLYPH_ENTRY_SIZE
            inside(strike.index_off, index_len, "glyph index")
            index = self.data[strike.index_off : strike.index_off + index_len]
            if crc32(index) != strike.index_crc32:
                raise FontPackError(f"strike ({strike.face_id}, {strike.size_px}) index CRC mismatch", Status.CRC_ERROR)
            for offset, width, height, *_ in _GLYPH.iter_unpack(index):
                if offset != FONTPACK_EMPTY_BITMAP and offset + bitmap_size(width, height) > bitmap_size_:
                    raise FontPackError("glyph bitmap outside the bitmap area")
            self.strikes.append(strike)
        self._by_key = {(s.face_id, s.size_px): s for s in self.strikes}

        if verify_content:
            digest = hashlib.sha256(self.data[FONTPACK_HEADER_SIZE:]).digest()
            if digest != self.content_hash or self.pack_id != digest[:FONTPACK_ID_LEN]:
                raise FontPackError("content hash mismatch", Status.DIGEST_MISMATCH)

    def strike(self, face: int, size_px: int) -> Strike | None:
        return self._by_key.get((face, size_px))

    def has_strike(self, face: int, size_px: int) -> bool:
        return (face, size_px) in self._by_key

    def glyph(self, face: int, size_px: int, glyph_id: int) -> GlyphBitmap | None:
        """The glyph, or None when the strike is missing or the id out of range."""
        strike = self._by_key.get((face, size_px))
        if strike is None or not 0 <= glyph_id < strike.glyph_count:
            return None
        offset, width, height, bearing_x, bearing_y, advance, _ = _GLYPH.unpack_from(
            self.data, strike.index_off + glyph_id * FONTPACK_GLYPH_ENTRY_SIZE)
        if offset == FONTPACK_EMPTY_BITMAP:
            return GlyphBitmap(width, height, bearing_x, bearing_y, advance)
        start = self._bitmap_off + offset
        return GlyphBitmap(width, height, bearing_x, bearing_y, advance,
                           self.data[start : start + bitmap_size(width, height)])
