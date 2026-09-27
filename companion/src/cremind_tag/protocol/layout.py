"""Logical screen ("layout") codec and validator (docs/protocol.md §4.1–§4.3).

``decode_layout`` performs the structural checks of §4.3 in their normative
order and raises ``LayoutError`` carrying the spec status of the first failure;
``check_strikes`` is the font-pack step that runs only on a structurally valid
layout, and ``check_panel`` the panel-geometry rule of §4.4. Commands without
a variable part are the generated fixed-layout classes; ``Glyphs`` and ``Qr``
add their glyph run and text.
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from ..third_party.qrcodegen import DataTooLongError, QrCode, QrSegment
from .ids import (
    LAYOUT_DIGEST_LEN,
    LAYOUT_HARD_MAX,
    LAYOUT_MAGIC,
    LAYOUT_MAX_COMMANDS,
    LAYOUT_MAX_GLYPHS,
    LAYOUT_QR_MAX_TEXT,
    PROTO_VERSION,
    Color,
    LayoutCmd,
    Status,
)
from .msgs import (
    LAYOUT_COMMANDS,
    LayoutCmdClear,
    LayoutCmdGlyphs,
    LayoutCmdIcon,
    LayoutCmdLine,
    LayoutCmdProgress,
    LayoutCmdQr,
    LayoutCmdRect,
    LayoutGlyph,
    LayoutHeader,
)

MAX_SIDE = 2048
QR_MIN_VERSION = 1
QR_MAX_VERSION = 10
ICON_FACE = 0

Clear = LayoutCmdClear
Icon = LayoutCmdIcon
Line = LayoutCmdLine
Rect = LayoutCmdRect
Progress = LayoutCmdProgress
Glyph = LayoutGlyph

_QR_ECC = (QrCode.Ecc.LOW, QrCode.Ecc.MEDIUM, QrCode.Ecc.QUARTILE, QrCode.Ecc.HIGH)


class LayoutError(ValueError):
    """The layout fails validation with ``status``."""

    def __init__(self, status: Status, message: str) -> None:
        super().__init__(f"{status.name}: {message}")
        self.status = status


@dataclass(frozen=True, slots=True)
class Glyphs:
    """GLYPHS: a run from strike (face, size_px); each glyph offsets the pen."""

    face: int
    size_px: int
    color: int
    origin_x: int
    origin_y: int
    glyphs: tuple[Glyph, ...]


@dataclass(frozen=True, slots=True)
class Qr:
    """QR: ``text`` (printable ASCII) encoded per §4.4."""

    x: int
    y: int
    module_px: int
    ecc: int
    color: int
    text: bytes


type Command = Clear | Glyphs | Icon | Line | Rect | Progress | Qr

_OPS: dict[type, LayoutCmd] = {
    Clear: LayoutCmd.CLEAR, Glyphs: LayoutCmd.GLYPHS, Icon: LayoutCmd.ICON, Line: LayoutCmd.LINE,
    Rect: LayoutCmd.RECT, Progress: LayoutCmd.PROGRESS, Qr: LayoutCmd.QR,
}


@dataclass(frozen=True, slots=True)
class Layout:
    width: int
    height: int
    rotation: int
    background: int
    commands: tuple[Command, ...]
    flags: int = 0
    version: int = PROTO_VERSION


@functools.lru_cache(maxsize=64)
def qr_code(text: bytes, ecc: int) -> QrCode:
    """The QR symbol of §4.4 (Nayuki encodeText, versions 1..10, auto mask, boosted ECC)."""
    segments = QrSegment.make_segments(text.decode("ascii"))
    try:
        return QrCode.encode_segments(segments, _QR_ECC[ecc], QR_MIN_VERSION, QR_MAX_VERSION, -1, True)
    except DataTooLongError:
        raise LayoutError(Status.INVALID, "QR text does not fit version 10") from None


def layout_digest(data: bytes) -> bytes:
    """LAYOUT_BEGIN.digest: SHA-256 of the layout bytes, first 16 bytes."""
    return hashlib.sha256(data).digest()[:LAYOUT_DIGEST_LEN]


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encode_command(cmd: Command) -> bytes:
    op = bytes([_OPS[type(cmd)]])
    if isinstance(cmd, Glyphs):
        run = LayoutCmdGlyphs(cmd.face, cmd.size_px, cmd.color, cmd.origin_x, cmd.origin_y, len(cmd.glyphs))
        return op + run.pack() + b"".join(g.pack() for g in cmd.glyphs)
    if isinstance(cmd, Qr):
        return op + LayoutCmdQr(cmd.x, cmd.y, cmd.module_px, cmd.ecc, cmd.color, len(cmd.text)).pack() + cmd.text
    return op + cmd.pack()


def encode_layout(layout: Layout, *, validate: bool = True) -> bytes:
    """Serialise ``layout``; by default also run the structural checks."""
    header = LayoutHeader(LAYOUT_MAGIC, layout.version, layout.flags, layout.width, layout.height,
                          layout.rotation, layout.background, len(layout.commands))
    data = header.pack() + b"".join(_encode_command(c) for c in layout.commands)
    if validate:
        decode_layout(data)
    return data


# ---------------------------------------------------------------------------
# Decoding and validation
# ---------------------------------------------------------------------------


def _invalid(message: str) -> LayoutError:
    return LayoutError(Status.INVALID, message)


def _check_color(color: int, what: str) -> None:
    if color not in Color:
        raise _invalid(f"{what}: colour {color}")


def _check_command(cmd: Command) -> None:
    """Field bounds of §4.3, in field order."""
    name = type(cmd).__name__
    if isinstance(cmd, Line) and not 1 <= cmd.width <= 8:
        raise _invalid(f"LINE width {cmd.width}")
    if isinstance(cmd, Qr):
        if not 1 <= cmd.module_px <= 8:
            raise _invalid(f"QR module_px {cmd.module_px}")
        if not 0 <= cmd.ecc <= 3:
            raise _invalid(f"QR ecc {cmd.ecc}")
    _check_color(cmd.color, name)
    if isinstance(cmd, Qr):
        if not 1 <= len(cmd.text) <= LAYOUT_QR_MAX_TEXT:
            raise _invalid(f"QR len {len(cmd.text)}")
        if any(not 0x21 <= b <= 0x7E for b in cmd.text):
            raise _invalid("QR text outside 0x21..0x7E")
        qr_code(cmd.text, cmd.ecc)


def _read_command(data: bytes, pos: int) -> tuple[Command, int]:
    op = data[pos]
    pos += 1
    if op not in LayoutCmd:
        raise LayoutError(Status.UNSUPPORTED, f"unknown command op {op:#04x}")
    cls = LAYOUT_COMMANDS[LayoutCmd(op)]
    if len(data) - pos < cls.LEN:
        raise _invalid(f"{LayoutCmd(op).name} truncated")
    fixed = cls.unpack(data[pos : pos + cls.LEN])
    pos += cls.LEN
    if isinstance(fixed, LayoutCmdGlyphs):
        end = pos + fixed.count * Glyph.LEN
        if end > len(data):
            raise _invalid("GLYPHS entries truncated")
        glyphs = tuple(Glyph.unpack(data[p : p + Glyph.LEN]) for p in range(pos, end, Glyph.LEN))
        return Glyphs(fixed.face, fixed.size_px, fixed.color, fixed.origin_x, fixed.origin_y, glyphs), end
    if isinstance(fixed, LayoutCmdQr):
        end = pos + fixed.len
        if end > len(data):
            raise _invalid("QR text truncated")
        return Qr(fixed.x, fixed.y, fixed.module_px, fixed.ecc, fixed.color, bytes(data[pos:end])), end
    return cast(Command, fixed), pos


def decode_layout(data: bytes) -> Layout:
    """Parse ``data`` running the structural checks of §4.3 (steps 1-4)."""
    if len(data) > LAYOUT_HARD_MAX:
        raise LayoutError(Status.TOO_LARGE, f"{len(data)} bytes > {LAYOUT_HARD_MAX}")
    if len(data) < LayoutHeader.LEN:
        raise _invalid("truncated header")
    h = LayoutHeader.unpack(data[: LayoutHeader.LEN])
    if h.magic != LAYOUT_MAGIC:
        raise _invalid(f"magic {h.magic:#06x}")
    if h.version != PROTO_VERSION:
        raise LayoutError(Status.UNSUPPORTED, f"version {h.version}")
    if not 1 <= h.width <= MAX_SIDE or not 1 <= h.height <= MAX_SIDE:
        raise _invalid(f"size {h.width}x{h.height}")
    if h.rotation > 3:
        raise _invalid(f"rotation {h.rotation}")
    _check_color(h.background, "background")
    if h.cmd_count > LAYOUT_MAX_COMMANDS:
        raise LayoutError(Status.TOO_LARGE, f"{h.cmd_count} commands > {LAYOUT_MAX_COMMANDS}")
    pos = LayoutHeader.LEN
    glyph_total = 0
    commands: list[Command] = []
    for index in range(h.cmd_count):
        if pos >= len(data):
            raise _invalid(f"command {index} missing")
        cmd, pos = _read_command(data, pos)
        _check_command(cmd)
        if isinstance(cmd, Glyphs):
            glyph_total += len(cmd.glyphs)
            if glyph_total > LAYOUT_MAX_GLYPHS:
                raise LayoutError(Status.TOO_LARGE, f"more than {LAYOUT_MAX_GLYPHS} glyphs")
        commands.append(cmd)
    if pos != len(data):
        raise _invalid(f"{len(data) - pos} bytes after the last command")
    return Layout(h.width, h.height, h.rotation, h.background, tuple(commands), h.flags, h.version)


def check_strikes(layout: Layout, has_strike: Callable[[int, int], bool]) -> None:
    """§4.3 step 5: every referenced strike exists in the active font pack."""
    for cmd in layout.commands:
        if isinstance(cmd, Glyphs) and not has_strike(cmd.face, cmd.size_px):
            raise LayoutError(Status.FONTPACK_MISMATCH, f"no strike ({cmd.face}, {cmd.size_px})")
        if isinstance(cmd, Icon) and not has_strike(ICON_FACE, cmd.size_px):
            raise LayoutError(Status.FONTPACK_MISMATCH, f"no icon strike {cmd.size_px}")


def check_panel(layout: Layout, native_width: int, native_height: int) -> None:
    """§4.4 Rotation: the logical size must match the panel for the rotation."""
    want = (native_width, native_height) if layout.rotation in (0, 2) else (native_height, native_width)
    if (layout.width, layout.height) != want:
        raise _invalid(f"{layout.width}x{layout.height} rotation {layout.rotation} "
                       f"does not fit a {native_width}x{native_height} panel")


def validate_layout(data: bytes, has_strike: Callable[[int, int], bool] | None = None) -> Status:
    """Status of the first failing §4.3 check, or OK."""
    try:
        layout = decode_layout(data)
        if has_strike is not None:
            check_strikes(layout, has_strike)
    except LayoutError as exc:
        return exc.status
    return Status.OK
