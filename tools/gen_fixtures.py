#!/usr/bin/env python3
"""Regenerate protocol/fixtures/ from the Python reference implementation.

Usage::

    uv run --project companion python tools/gen_fixtures.py          # write
    uv run --project companion python tools/gen_fixtures.py --check  # exit 1 if stale

Every fixture is a deterministic function of this script and the reference
modules (fixed keys, nonces and inputs; no clocks, no randomness). The
companion test-suite rebuilds them in memory and compares them with the
committed files. Integers in JSON stay below 2**53 so that any JSON parser
reads them exactly; byte strings are lower-case hex.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "protocol" / "fixtures"
if str(ROOT / "companion" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "companion" / "src"))

from cremind_tag.fontpack.format import (  # noqa: E402
    FACE_FLAG_ICON,
    Face,
    FontPack,
    GlyphBitmap,
    StrikeSpec,
    build_pack,
)
from cremind_tag.protocol import cbor_msgs, cobs, enrollment, session, tag_txn  # noqa: E402
from cremind_tag.protocol.crc import crc32  # noqa: E402
from cremind_tag.protocol.fragments import MAX_MESSAGE, Fragmenter, FragmentError, Reassembler  # noqa: E402
from cremind_tag.protocol.ids import (  # noqa: E402
    BRIDGE_STRIP_ROWS,
    SERIAL_MAX_FRAME,
    Board,
    Color,
    CtrlMsg,
    DeliveryStage,
    GattChr,
    MeshOp,
    NodeRole,
    PlainMsg,
    RecordDir,
    RecordType,
    SerialFlag,
    SerialMsg,
    Status,
    mesh_opcode_bytes,
    mesh_vendor_opcode,
)
from cremind_tag.protocol.ids import (
    Panel as PanelId,
)
from cremind_tag.protocol.layout import (  # noqa: E402
    Clear,
    Glyph,
    Glyphs,
    Icon,
    Layout,
    LayoutError,
    Line,
    Progress,
    Qr,
    Rect,
    check_panel,
    decode_layout,
    encode_layout,
    layout_digest,
    qr_code,
    validate_layout,
)
from cremind_tag.protocol.msgs import (  # noqa: E402
    MESH_MESSAGES,
    CtrlAuth,
    CtrlAuthOk,
    CtrlChallenge,
    CtrlError,
    CtrlHello,
    LayoutHeader,
    MessageError,
    OversizeError,
    PlainCredit,
    RecFrameBegin,
    RecFrameEnd,
    RecPlaneData,
    RecProgress,
    RecResult,
    TagCaps,
)
from cremind_tag.protocol.serial_frame import Frame, SerialFrameError, decode_frame, encode_frame  # noqa: E402
from cremind_tag.render.reference import Panel, render_frame  # noqa: E402

FONTPACK_NAME = "fontpack_test.ctfp"


def _hex(data: bytes | bytearray) -> str:
    return bytes(data).hex()


def _json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def fields_to_json(fields: Any) -> Any:
    """Named serial/mesh fields -> JSON (bytes become hex strings)."""
    if isinstance(fields, dict):
        return {k: fields_to_json(v) for k, v in fields.items()}
    if isinstance(fields, list | tuple):
        return [fields_to_json(v) for v in fields]
    if isinstance(fields, bytes | bytearray):
        return _hex(fields)
    return fields


def cbor_fields_from_json(fields: dict[str, Any]) -> dict[str, Any]:
    """Inverse of fields_to_json for serial payloads, using the key kinds."""
    out: dict[str, Any] = {}
    for name, value in fields.items():
        kind = cbor_msgs.KEYS[name].kind
        if kind is cbor_msgs.Kind.BSTR:
            value = bytes.fromhex(value)
        elif kind is cbor_msgs.Kind.MAP:
            value = cbor_fields_from_json(value)
        elif kind is cbor_msgs.Kind.MAPS:
            value = [cbor_fields_from_json(v) for v in value]
        out[name] = value
    return out


def _status(status: Status | int | None) -> dict[str, Any]:
    if status is None:
        return {"status": None, "status_name": None}
    return {"status": int(status), "status_name": Status(status).name}


# ---------------------------------------------------------------------------
# Synthetic font pack
# ---------------------------------------------------------------------------


def _bitmap(width: int, height: int, ink: Callable[[int, int], bool]) -> bytes:
    row_bytes = (width + 7) // 8
    out = bytearray()
    for y in range(height):
        row = 0
        for x in range(width):
            if ink(x, y):
                row |= 1 << (row_bytes * 8 - 1 - x)
        out += row.to_bytes(row_bytes, "big")
    return bytes(out)


ICON_PATTERNS: dict[int, Callable[[int, int, int], bool]] = {
    1: lambda x, y, s: x in (0, s - 1) or y in (0, s - 1),
    2: lambda x, y, s: (2 * x - s + 1) ** 2 + (2 * y - s + 1) ** 2 <= s * s,
    3: lambda x, y, s: x == y or x == s - 1 - y,
    4: lambda x, y, s: abs(2 * x - (s - 1)) <= y,
    5: lambda x, y, s: (x // 2 + y // 2) % 2 == 0,
    6: lambda x, y, s: x in (0, s - 1) or y in (0, s - 1),  # same bitmap as icon 1 (de-dup)
}
ICON_GLYPH_COUNT = 7
TEXT_GLYPH_COUNT = 20


def _icon_glyph(icon: int, size: int) -> GlyphBitmap:
    if icon not in ICON_PATTERNS:
        return GlyphBitmap(0, 0, 0, 0, 0)
    pattern = ICON_PATTERNS[icon]
    return GlyphBitmap(size, size, 0, 0, size, _bitmap(size, size, lambda x, y: pattern(x, y, size)))


def _text_glyph(gid: int, size: int) -> GlyphBitmap:
    if gid == 0:  # .notdef box
        w, h = size // 2, size * 3 // 4
        return GlyphBitmap(w, h, 1, h, w + 2, _bitmap(w, h, lambda x, y: x in (0, w - 1) or y in (0, h - 1)))
    if gid == 1:  # space: empty, advance only
        return GlyphBitmap(0, 0, 0, 0, size // 3)
    if gid == 12:  # identical to glyph 11 (de-dup inside a strike)
        return _text_glyph(11, size)
    if gid == 18:  # ink-free but non-empty: stored, draws nothing
        return GlyphBitmap(5, 6, 0, 6, 6, bytes(6))
    if gid == 19:  # same 8x8 diamond in every strike (de-dup across strikes)
        return GlyphBitmap(8, 8, 0, 8, 9, _bitmap(8, 8, lambda x, y: abs(2 * x - 7) + abs(2 * y - 7) <= 8))
    w = 3 + (gid * 5) % (size // 2)
    h = size // 2 + gid % (size // 2 - 2)
    ink = _bitmap(w, h, lambda x, y: (x + 2 * y + gid) % 5 < 2 or x == 0)
    return GlyphBitmap(w, h, gid % 5 - 2, h - gid % 4, w + 1, ink)


def build_test_fontpack() -> bytes:
    faces = [
        Face(1, 0, "Test Sans 1.000", "Latn,Grek", TEXT_GLYPH_COUNT),
        Face(0, FACE_FLAG_ICON, "Test Icons 1.000", "Zsym", ICON_GLYPH_COUNT),
    ]
    strikes = []
    for size in (24, 16):  # unsorted on purpose: the writer orders strikes
        strikes.append(StrikeSpec(0, size, size, 0, size,
                                  tuple(_icon_glyph(i, size) for i in range(ICON_GLYPH_COUNT))))
        strikes.append(StrikeSpec(1, size, size * 4 // 5, size // 5 + 1, size * 6 // 5,
                                  tuple(_text_glyph(g, size) for g in range(TEXT_GLYPH_COUNT))))
    manifest_id = hashlib.sha256(b"cremind-tag synthetic test font pack v1").digest()[:8]
    return build_pack(faces, strikes, manifest_id)


def fontpack_fixture(data: bytes) -> dict[str, Any]:
    pack = FontPack(data)
    header = {
        "magic": int.from_bytes(data[0:4], "little"),
        "version": int.from_bytes(data[4:6], "little"),
        "header_size": int.from_bytes(data[6:8], "little"),
        "flags": pack.flags,
        "total_size": pack.total_size,
        "pack_id": _hex(pack.pack_id),
        "content_hash": _hex(pack.content_hash),
        "face_count": len(pack.faces),
        "strike_count": len(pack.strikes),
        "face_table_offset": int.from_bytes(data[60:64], "little"),
        "strike_table_offset": int.from_bytes(data[64:68], "little"),
        "string_table_offset": int.from_bytes(data[68:72], "little"),
        "string_table_size": int.from_bytes(data[72:76], "little"),
        "bitmap_area_offset": int.from_bytes(data[76:80], "little"),
        "bitmap_area_size": int.from_bytes(data[80:84], "little"),
        "manifest_id": _hex(pack.manifest_id),
        "header_crc32": int.from_bytes(data[124:128], "little"),
    }
    samples = []
    for face, size, gid in [(0, 16, 0), (0, 16, 1), (0, 16, 6), (0, 24, 4), (1, 16, 0), (1, 16, 1),
                            (1, 16, 7), (1, 16, 11), (1, 16, 12), (1, 16, 18), (1, 16, 19), (1, 24, 19),
                            (1, 24, 13)]:
        strike = pack.strike(face, size)
        assert strike is not None
        entry = data[strike.index_off + gid * 12 : strike.index_off + gid * 12 + 12]
        glyph = pack.glyph(face, size, gid)
        assert glyph is not None
        samples.append({
            "face": face, "size_px": size, "glyph_id": gid,
            "bitmap_off": int.from_bytes(entry[0:4], "little"),
            "width": glyph.width, "height": glyph.height, "bearing_x": glyph.bearing_x,
            "bearing_y": glyph.bearing_y, "advance": glyph.advance, "bitmap": _hex(glyph.bitmap),
        })
    distinct = {g.bitmap for s in pack.strikes for gid in range(s.glyph_count)
                if (g := pack.glyph(s.face_id, s.size_px, gid)) is not None and g.bitmap}
    stored_total = sum(len(g.bitmap) for s in pack.strikes for gid in range(s.glyph_count)
                       if (g := pack.glyph(s.face_id, s.size_px, gid)) is not None and g.bitmap)

    def corrupt(offset: int) -> bytes:
        return data[:offset] + bytes([data[offset] ^ 0x01]) + data[offset + 1 :]

    first_index = pack.strikes[0].index_off
    return {
        "description": "Expected parse of fontpack_test.ctfp (docs/fontpack.md). Synthetic bitmaps; "
                       "icon 6 duplicates icon 1, text glyph 12 duplicates 11, glyph 19 is shared by both "
                       "text strikes, glyph 1 is empty, glyph 18 is stored without ink.",
        "file": FONTPACK_NAME,
        "header": header,
        "faces": [{"face_id": f.face_id, "flags": f.flags, "name": f.name, "scripts": f.scripts,
                   "glyph_count": f.glyph_count} for f in pack.faces],
        "strikes": [{"face_id": s.face_id, "size_px": s.size_px, "flags": s.flags, "glyph_count": s.glyph_count,
                     "index_off": s.index_off, "ascent": s.ascent, "descent": s.descent,
                     "line_height": s.line_height, "index_crc32": s.index_crc32} for s in pack.strikes],
        "glyphs": samples,
        "dedup": {"distinct_bitmaps": len(distinct), "bitmap_area_size": header["bitmap_area_size"],
                  "undeduplicated_size": stored_total},
        "corruptions": [
            {"name": "header byte flipped", "offset": 20, "boot_status": "CRC_ERROR", "install_status": "CRC_ERROR"},
            {"name": "glyph index byte flipped", "offset": first_index + 5, "boot_status": "CRC_ERROR",
             "install_status": "CRC_ERROR"},
            {"name": "bitmap byte flipped", "offset": pack.total_size - 1, "boot_status": "OK",
             "install_status": "DIGEST_MISMATCH"},
        ],
        "_corrupt_check": [_check_corruption(corrupt(c)) for c in (20, first_index + 5, pack.total_size - 1)],
    }


def _check_corruption(data: bytes) -> list[str]:
    results = []
    for verify in (False, True):
        try:
            FontPack(data, verify_content=verify)
            results.append("OK")
        except ValueError as exc:
            results.append(exc.status.name)  # type: ignore[attr-defined]
    return results


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


def text_run(pack: FontPack, face: int, size: int, color: int, x: int, y: int, gids: list[int]) -> Glyphs:
    """A shaped run: each glyph advances the pen by the previous glyph's advance."""
    entries: list[Glyph] = []
    previous = 0
    for gid in gids:
        entries.append(Glyph(gid, previous if entries else 0, 0))
        glyph = pack.glyph(face, size, gid)
        previous = glyph.advance if glyph is not None else 0
    return Glyphs(face, size, color, x, y, tuple(entries))


def valid_layouts(pack: FontPack) -> dict[str, Layout]:
    W, B, R = Color.WHITE, Color.BLACK, Color.RED
    return {
        "status_card": Layout(400, 300, 0, W, (
            Icon(2, 24, B, 12, 12),
            text_run(pack, 1, 24, B, 44, 32, [2, 3, 4, 5, 1, 6, 7, 8, 9, 10]),
            text_run(pack, 1, 16, B, 12, 64, [11, 12, 13, 1, 14, 15, 16, 17, 19]),
            Line(12, 76, 388, 76, 1, B),
            Progress(12, 260, 376, 20, 37, 100, B),
        )),
        "every_command": Layout(296, 128, 1, W, (
            Clear(W),
            Glyphs(1, 16, B, 4, 20, (Glyph(2, 0, 0), Glyph(3, 9, 0), Glyph(4, 9, -2))),
            Glyphs(1, 24, R, 4, 60, (Glyph(5, 0, 0), Glyph(19, 12, 3))),
            Icon(4, 16, R, 270, 4),
            Line(0, 127, 295, 0, 2, B),
            Rect(150, 10, 60, 40, 3, B),
            Progress(150, 60, 100, 12, 50, 100, B),
            Qr(220, 70, 1, 1, B, b"https://cremind.io/t/1A2B"),
        ), flags=1),
        "multi_run_glyphs": Layout(128, 64, 0, W, tuple(
            text_run(pack, 1, 16 if i % 2 else 24, B, 2, 18 + 22 * i, list(range(2, 12)))
            for i in range(3)
        )),
        "qr_only": Layout(64, 64, 0, W, (Qr(4, 4, 2, 3, B, b"HTTPS://CREMIND.IO/T/42"),)),
        "empty": Layout(1, 1, 0, B, ()),
        # §4.3 render cost, at the limits: LAYOUT_MAX_QR QR commands; LINE endpoints on the edges of the box
        # [-W, 2W) x [-H, 2H); exactly LAYOUT_MAX_LINE_STEPS steps (2 x 6144 + 4096).
        "four_qr": Layout(64, 64, 0, W, tuple(Qr(2 + 16 * (i % 2), 2 + 16 * (i // 2), 1, 0, B, b"https://a.b/%d" % i)
                                              for i in range(4))),
        "line_box_edges": Layout(16, 16, 0, W, (Line(-16, -16, 31, 31, 1, B), Line(31, -16, -16, 31, 2, B))),
        "line_steps_limit": Layout(2048, 16, 0, W, (Line(-2048, 0, 4095, 0, 1, B), Line(4095, 15, -2048, 15, 1, B),
                                                    Line(0, 8, 4095, 8, 1, B))),
    }


def _layout_json(layout: Layout) -> dict[str, Any]:
    commands = []
    for cmd in layout.commands:
        if isinstance(cmd, Glyphs):
            commands.append({"op": "GLYPHS", "face": cmd.face, "size_px": cmd.size_px, "color": cmd.color,
                             "origin_x": cmd.origin_x, "origin_y": cmd.origin_y,
                             "glyphs": [[g.glyph_id, g.dx, g.dy] for g in cmd.glyphs]})
        elif isinstance(cmd, Qr):
            commands.append({"op": "QR", "x": cmd.x, "y": cmd.y, "module_px": cmd.module_px, "ecc": cmd.ecc,
                             "color": cmd.color, "text": cmd.text.decode("ascii")})
        else:
            name = type(cmd).__name__.removeprefix("LayoutCmd").upper()
            commands.append({"op": name, **{f.name: getattr(cmd, f.name) for f in dataclasses.fields(cmd)}})
    return {"width": layout.width, "height": layout.height, "rotation": layout.rotation,
            "background": layout.background, "flags": layout.flags, "commands": commands}


def invalid_layouts(pack: FontPack) -> list[tuple[str, bytes]]:
    base = encode_layout(Layout(16, 16, 0, Color.WHITE, (Clear(Color.BLACK),)))

    def header(**changes: int) -> bytes:
        h = LayoutHeader.unpack(base[:12])
        return dataclasses.replace(h, **changes).pack()

    def one(cmd: bytes) -> bytes:
        return header(cmd_count=1) + cmd

    def raw(layout: Layout) -> bytes:
        return encode_layout(layout, validate=False)

    def lay(*cmds: Any) -> Layout:
        return Layout(16, 16, 0, Color.WHITE, tuple(cmds))

    glyph_run = tuple(Glyph(2, 1, 0) for _ in range(171))
    return [
        ("empty input", b""),
        ("truncated header", base[:11]),
        ("bad magic", b"LC" + base[2:]),
        ("version 2", header(version=2) + base[12:]),
        ("width 0", header(width=0) + base[12:]),
        ("height 2049", header(height=2049) + base[12:]),
        ("rotation 4", header(rotation=4) + base[12:]),
        ("background 3", header(background=3) + base[12:]),
        ("cmd_count 257", header(cmd_count=257) + bytes([1, 1]) * 257),
        ("cmd_count exceeds commands", header(cmd_count=2) + base[12:]),
        ("trailing byte", base + b"\x00"),
        ("unknown op 0x08", one(b"\x08\x00")),
        ("unknown op 0x00", one(b"\x00")),
        ("CLEAR truncated", one(b"\x01")),
        ("CLEAR colour 3", one(b"\x01\x03")),
        ("LINE width 0", raw(lay(Line(0, 0, 5, 5, 0, 1)))),
        ("LINE width 9", raw(lay(Line(0, 0, 5, 5, 9, 1)))),
        ("LINE x0 -W-1", raw(lay(Line(-17, 0, 5, 5, 1, 1)))),
        ("LINE y0 -H-1", raw(lay(Line(0, -17, 5, 5, 1, 1)))),
        ("LINE x1 2W", raw(lay(Line(0, 0, 32, 5, 1, 1)))),
        ("LINE y1 2H", raw(lay(Line(0, 0, 5, 32, 1, 1)))),
        ("LINE extreme endpoints (-32768..32767)", raw(lay(Line(-32768, 5, 32767, 5, 1, 1)))),
        ("LINE steps 16385", raw(Layout(2048, 16, 0, Color.WHITE, (
            Line(-2048, 0, 4095, 0, 1, 1), Line(4095, 15, -2048, 15, 1, 1), Line(0, 8, 4095, 8, 1, 1),
            Line(3, 3, 3, 3, 1, 1))))),
        ("RECT colour 7", raw(lay(Rect(0, 0, 5, 5, 0, 7)))),
        ("GLYPHS entries truncated", raw(lay(Glyphs(1, 16, 1, 0, 10, (Glyph(2, 0, 0), Glyph(3, 5, 0)))))[:-2]),
        ("GLYPHS total 513", raw(lay(*(Glyphs(1, 16, 1, 0, 10, glyph_run) for _ in range(3))))),
        ("QR module_px 0", raw(lay(Qr(0, 0, 0, 0, 1, b"https://a.b")))),
        ("QR module_px 9", raw(lay(Qr(0, 0, 9, 0, 1, b"https://a.b")))),
        ("QR ecc 4", raw(lay(Qr(0, 0, 1, 4, 1, b"https://a.b")))),
        ("QR len 0", raw(lay(Qr(0, 0, 1, 0, 1, b"")))),
        ("QR len 97", raw(lay(Qr(0, 0, 1, 0, 1, b"https://cremind.io/" + b"x" * 78)))),
        ("QR space in text", raw(lay(Qr(0, 0, 1, 0, 1, b"https://a.b/c d")))),
        ("QR text truncated", raw(lay(Qr(0, 0, 1, 0, 1, b"https://a.b")))[:-1]),
        ("5 QR commands", raw(lay(*(Qr(0, 0, 1, 0, 1, b"https://a.b") for _ in range(5))))),
        ("5th QR before the 513th glyph", raw(lay(*(Qr(0, 0, 1, 0, 1, b"https://a.b") for _ in range(5)),
                                               *(Glyphs(1, 16, 1, 0, 10, glyph_run) for _ in range(3))))),
        ("513th glyph before the 5th QR", raw(lay(*(Glyphs(1, 16, 1, 0, 10, glyph_run) for _ in range(3)),
                                               *(Qr(0, 0, 1, 0, 1, b"https://a.b") for _ in range(5))))),
        ("size 4097", raw(Layout(16, 16, 0, Color.WHITE, (Qr(0, 0, 1, 0, 1, b"x" * 96),) * 39))[:4097]),
        ("LINE endpoint before a missing strike", raw(lay(Icon(1, 48, 1, 0, 0), Line(0, 0, 40, 0, 1, 1)))),
        ("GLYPHS strike (7, 16) missing", raw(lay(Glyphs(7, 16, 1, 0, 10, (Glyph(2, 0, 0),))))),
        ("GLYPHS strike (1, 32) missing", raw(lay(Glyphs(1, 32, 1, 0, 10, (Glyph(2, 0, 0),))))),
        ("ICON strike 48 missing", raw(lay(Icon(1, 48, 1, 0, 0)))),
        ("colour error after missing strike", raw(lay(Icon(1, 48, 1, 0, 0), Clear(9)))),
    ]


def layouts_fixture(pack: FontPack) -> dict[str, Any]:
    valid: list[dict[str, Any]] = []
    for name, layout in valid_layouts(pack).items():
        data = encode_layout(layout)
        assert validate_layout(data, pack.has_strike) is Status.OK
        valid.append({"name": name, "hex": _hex(data), "digest": _hex(layout_digest(data)),
                      "layout": _layout_json(layout)})
    invalid = []
    for name, data in invalid_layouts(pack):
        invalid.append({"name": name, "hex": _hex(data), **_status(validate_layout(data, pack.has_strike))})
    panel_checks = []
    for name, (width, height) in [("status_card", (400, 300)), ("status_card", (300, 400)),
                                  ("every_command", (128, 296)), ("every_command", (296, 128))]:
        layout = decode_layout(bytes.fromhex(next(v["hex"] for v in valid if v["name"] == name)))
        try:
            check_panel(layout, width, height)
            status = Status.OK
        except LayoutError as exc:
            status = exc.status
        panel_checks.append({"layout": name, "native_width": width, "native_height": height, **_status(status)})
    return {
        "description": "Layouts (docs/protocol.md §4.1-4.3). Every case is validated against the strikes of "
                       f"{FONTPACK_NAME}; the status is the first failing check in the §4.3 order.",
        "fontpack": FONTPACK_NAME,
        "valid": valid,
        "invalid": invalid,
        "panel_checks": panel_checks,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scenarios(pack: FontPack) -> list[tuple[str, str, Panel, Layout]]:
    W, B, R = Color.WHITE, Color.BLACK, Color.RED
    layouts = valid_layouts(pack)
    bwr = Layout(400, 300, 0, W, (
        Rect(0, 0, 400, 40, 0, R),
        text_run(pack, 1, 24, W, 10, 30, [2, 3, 4, 5, 6]),
        Icon(2, 24, B, 360, 8),
        text_run(pack, 1, 16, B, 10, 80, list(range(0, 20))),
        Icon(5, 16, R, 10, 100),
        Progress(10, 270, 380, 16, 1, 3, R),
    ))
    asym = (
        Line(0, 0, 20, 5, 1, B), Rect(2, 30, 6, 4, 1, B), Icon(3, 16, B, 4, 8),
        text_run(pack, 1, 16, B, 1, 34, [7, 8]),
    )
    edge = Layout(40, 24, 0, W, (
        Line(-10, -10, 50, 30, 3, B),
        Rect(-3, -3, 10, 10, 2, B),
        Rect(35, 18, 20, 20, 0, B),
        Icon(1, 16, B, -8, 12),
        Glyphs(1, 24, B, 30, 30, (Glyph(13, 0, 0),)),
        Qr(28, -10, 1, 0, B, b"https://a.b"),
        # Lines that leave the canvas across every edge, their endpoints inside the §4.3 box [-W, 2W) x [-H, 2H).
        Line(-40, 5, 79, 5, 1, B),
        Line(20, -24, 20, 47, 2, B),
        Line(-40, -24, 79, 47, 1, B),
        Line(79, -24, -40, 47, 3, B),
    ))
    thick = Layout(64, 48, 0, W, tuple(
        Line(32, 24, 32 + dx, 24 + dy, width, B)
        for width, (dx, dy) in zip(range(1, 9), [(30, 3), (5, 22), (-7, 20), (-28, 9), (-30, -4), (-6, -22),
                                                (8, -21), (29, -10)], strict=True)
    ) + (Line(3, 3, 3, 3, 4, B), Line(60, 2, 60, 45, 2, B)))
    progress = Layout(64, 48, 0, W, (
        Progress(1, 1, 30, 8, 0, 0, B),
        Progress(33, 1, 30, 8, 500, 100, B),
        Progress(1, 11, 4, 8, 1, 2, B),
        Progress(7, 11, 5, 5, 1, 1, B),
        Progress(14, 11, 20, 4, 3, 4, B),
        Progress(36, 11, 27, 9, 2, 3, B),
        Progress(1, 22, 62, 10, 65535, 65535, B),
        Progress(1, 34, 62, 12, 1, 65535, B),
        Progress(0, 47, 0, 5, 1, 1, B),
    ))
    rects = Layout(48, 32, 0, W, (
        Rect(1, 1, 10, 10, 5, B), Rect(13, 1, 10, 7, 255, B), Rect(25, 1, 0, 9, 1, B), Rect(25, 12, 9, 0, 0, B),
        Rect(1, 13, 20, 15, 1, B), Rect(24, 14, 22, 16, 4, B), Rect(26, 16, 3, 3, 0, W),
    ))
    qr = Layout(160, 100, 0, W, (
        Qr(2, 2, 1, 0, B, b"https://cremind.io"),
        Qr(30, 2, 2, 3, B, b"HTTPS://CREMIND.IO/T/42"),
        Qr(2, 40, 1, 0, B, b"1234567890"),
        Qr(80, 2, 1, 1, R, b"https://cremind.io/t/1A2B3C4D/e3/r18?view=card&lang=vi&tz=Asia/Ho_Chi_Minh"),
    ))
    bearings = Layout(96, 40, 0, W, (
        text_run(pack, 1, 16, B, 2, 14, [0, 1, 2, 3, 4, 17, 18, 5]),
        Glyphs(1, 24, B, 2, 34, (Glyph(6, 0, 0), Glyph(250, 10, 0), Glyph(9, 8, -3), Glyph(19, 14, 6),
                                 Glyph(14, -4, -12), Glyph(1, 20, 0), Glyph(16, 3, 2))),
        Glyphs(0, 16, R, 70, 30, (Glyph(2, 0, 0),)),
    ))
    last_writer = Layout(32, 16, 0, R, (
        Rect(0, 0, 16, 16, 0, B), Clear(W), Rect(4, 4, 8, 8, 0, R), Line(0, 15, 31, 0, 1, B),
    ))
    small = (37, 21)
    return [
        ("rot0_bw_status_card", "rotation 0, 1 plane, plane_flags bit0 = 1 (1 = white)",
         Panel(400, 300, 1, 0x01), layouts["status_card"]),
        ("rot1_every_command", "rotation 1 on a 128x296 panel, 2 planes, every command",
         Panel(128, 296, 2, 0x03), layouts["every_command"]),
        ("rot1_asym", "rotation 1, odd native width 37 (row padding)",
         Panel(*small, 1, 0x01), Layout(small[1], small[0], 1, W, asym)),
        ("rot2_asym", "rotation 2, odd native width", Panel(*small, 1, 0x01), Layout(*small, 2, W, asym)),
        ("rot3_asym", "rotation 3, odd native width", Panel(*small, 1, 0x01),
         Layout(small[1], small[0], 3, W, asym)),
        ("rot0_asym", "rotation 0 reference for the three above", Panel(*small, 1, 0x01),
         Layout(*small, 0, W, asym)),
        ("bwr_flags_3", "2 planes, plane0 1 = white, plane1 1 = red", Panel(400, 300, 2, 0x03), bwr),
        ("bwr_flags_0", "2 planes, inverted polarity: plane0 1 = black, plane1 0 = red",
         Panel(400, 300, 2, 0x00), bwr),
        ("red_on_1_plane", "RED renders as BLACK on a 1-plane panel", Panel(400, 300, 1, 0x01), bwr),
        ("bw_flags_0", "1 plane with plane0 1 = black", Panel(37, 21, 1, 0x00), Layout(*small, 0, W, asym)),
        ("clipping", "commands partly or fully outside the canvas", Panel(40, 24, 1, 0x01), edge),
        ("thick_lines", "LINE widths 1..8 across octants, points, verticals", Panel(64, 48, 1, 0x01), thick),
        ("progress_edges", "PROGRESS max 0, value > max, tiny boxes, full scale", Panel(64, 48, 1, 0x01),
         progress),
        ("rect_borders", "RECT border >= half size, w/h 0, filled", Panel(48, 32, 1, 0x01), rects),
        ("qr", "QR in byte, alphanumeric and numeric modes, boosted ECC, red", Panel(160, 100, 2, 0x03), qr),
        ("glyph_bearings", "negative bearings, descenders, empty/ink-free/out-of-range glyphs, dx/dy",
         Panel(96, 40, 2, 0x03), bearings),
        ("last_writer_wins", "red background, CLEAR mid-stream, overlaps", Panel(32, 16, 2, 0x02), last_writer),
    ]


def render_fixture(pack: FontPack) -> dict[str, Any]:
    scenarios = []
    for name, description, panel, layout in render_scenarios(pack):
        data = encode_layout(layout)
        frame = render_frame(data, panel, pack)
        entry: dict[str, Any] = {
            "name": name, "description": description,
            "panel": {"width": panel.width, "height": panel.height, "planes": panel.planes,
                      "plane_flags": panel.plane_flags},
            "layout_hex": _hex(data),
            "plane_len": panel.plane_len,
            "planes_sha256": [hashlib.sha256(p).hexdigest() for p in frame.planes],
            "frame_digest": _hex(frame.digest),
        }
        if panel.plane_len <= 512:
            entry["planes_hex"] = [_hex(p) for p in frame.planes]
        scenarios.append(entry)
    return {
        "description": "Normative rendering (docs/protocol.md §4.4) with glyphs from " + FONTPACK_NAME,
        "fontpack": FONTPACK_NAME,
        "strip_rows": BRIDGE_STRIP_ROWS,
        "strip_note": "Rendering native rows [y0, y0 + strip_rows) of one plane must give exactly those rows "
                      "of the full plane (row_bytes = ceil(width / 8)); the last strip may be shorter.",
        "scenarios": scenarios,
    }


# ---------------------------------------------------------------------------
# QR
# ---------------------------------------------------------------------------

QR_TEXTS = [
    b"https://cremind.io",
    b"HTTPS://CREMIND.IO/T/1A2B",
    b"1234567890",
    b"https://example.com/a?b=c&d=e#f",
    b"https://cremind.io/t/" + b"0123456789abcdef" * 4 + b"?v=" + b"z" * 12,
]


def qr_fixture() -> dict[str, Any]:
    vectors = []
    for text in QR_TEXTS:
        for ecc in range(4):
            symbol = qr_code(text, ecc)
            size = symbol.get_size()
            rows = []
            for y in range(size):
                bits = "".join("1" if symbol.get_module(x, y) else "0" for x in range(size))
                bits += "0" * (-size % 8)
                rows.append(int(bits, 2).to_bytes(len(bits) // 8, "big").hex())
            vectors.append({"text": text.decode("ascii"), "ecc": ecc, "version": symbol.get_version(),
                            "ecc_used": symbol.get_error_correction_level().ordinal, "mask": symbol.get_mask(),
                            "size": size, "rows": rows})
    return {
        "description": "Nayuki QR-Code-generator v1.8.0 encodeText with minVersion 1, maxVersion 10, auto mask, "
                       "boostEcl true (docs/protocol.md §4.4). rows: MSB-first, 1 = dark, padded to bytes.",
        "vectors": vectors,
    }


# ---------------------------------------------------------------------------
# COBS, CRC, serial frames
# ---------------------------------------------------------------------------


def cobs_fixture() -> dict[str, Any]:
    nonzero = lambda n: bytes(i % 255 + 1 for i in range(n))  # noqa: E731
    mixed = bytes((i * 7 + 3) % 256 for i in range(SERIAL_MAX_FRAME))
    cases = [
        ("empty", b""),
        ("single zero", b"\x00"),
        ("two zeros", b"\x00\x00"),
        ("mixed short", bytes.fromhex("11220033")),
        ("trailing zeros", bytes.fromhex("11000000")),
        ("253 non-zero", nonzero(253)),
        ("254 non-zero", nonzero(254)),
        ("255 non-zero", nonzero(255)),
        ("254 non-zero then zero", nonzero(254) + b"\x00"),
        ("zero then 254 non-zero", b"\x00" + nonzero(254)),
        ("508 non-zero", nonzero(508)),
        ("max frame mixed", mixed),
        ("max frame non-zero (worst case)", nonzero(SERIAL_MAX_FRAME)),
    ]
    vectors = [{"name": n, "decoded": _hex(d), "encoded": _hex(cobs.encode(d))} for n, d in cases]
    frame_a, frame_b = cobs.encode(b"\x01\x02\x00\x03"), cobs.encode(nonzero(300))
    oversize = cobs.encode(nonzero(SERIAL_MAX_FRAME + 1))
    streams = [
        ("two frames with extra delimiters", b"\x00" + frame_a + b"\x00\x00" + frame_b + b"\x00"),
        ("garbage then frame", bytes.fromhex("1122") + b"\x00" + frame_a + b"\x00"),
        ("oversize frame discarded then frame", oversize + b"\x00" + frame_a + b"\x00"),
        ("frame without final delimiter", frame_a + b"\x00" + frame_b),
    ]
    stream_cases = []
    for name, data in streams:
        decoder = cobs.StreamDecoder()
        frames = decoder.feed(data)
        stream_cases.append({"name": name, "input": _hex(data), "frames": [_hex(f) for f in frames],
                             "errors": decoder.errors, "oversize": decoder.oversize})
    return {
        "description": "COBS (docs/protocol.md §1.1): no trailing 0x01 after a final 0xFF block. encoded "
                       "excludes the 0x00 delimiter. stream: input fed to a decoder with max decoded frame "
                       f"{SERIAL_MAX_FRAME}.",
        "vectors": vectors,
        "decode_only": [{"name": "trailing 0x01 after a final 0xFF block", "encoded": _hex(cobs.encode(nonzero(254)) + b"\x01"),
                         "decoded": _hex(nonzero(254))}],
        "decode_errors": [{"name": "zero byte", "encoded": "0200"}, {"name": "truncated block", "encoded": "051122"},
                          {"name": "no code byte", "encoded": ""}],
        "stream": stream_cases,
    }


def crc_fixture() -> dict[str, Any]:
    inputs = [("check", b"123456789"), ("empty", b""), ("zero byte", b"\x00"), ("ff x4", b"\xff" * 4),
              ("0..255", bytes(range(256))), ("enrollment body", _enrollment_blob()[:44])]
    return {
        "description": "CRC-32/IEEE: poly 0x04C11DB7 reflected, init 0xFFFFFFFF, xorout 0xFFFFFFFF. "
                       "le is the 4-byte little-endian trailer.",
        "vectors": [{"name": n, "data": _hex(d), "crc32": crc32(d), "le": _hex(crc32(d).to_bytes(4, "little"))}
                    for n, d in inputs],
    }


def serial_fixture(pack: FontPack) -> dict[str, Any]:
    layout = encode_layout(valid_layouts(pack)["status_card"])
    E, R = SerialFlag.EVENT, SerialFlag.RESPONSE
    frames: list[tuple[str, str, int, int, int, int, dict[str, Any]]] = [
        ("hello request", "request", SerialMsg.HELLO, 1, 0, 4, {"proto": 1, "name": "companion"}),
        ("hello response", "response", SerialMsg.HELLO, 1, R, 4, {
            "status": 0, "proto": 1, "fw": "0.1.0", "build": "g1a2b3c4", "boot_id": 0x5EED1234,
            "caps": {"max_frame": SERIAL_MAX_FRAME, "credits": 4, "max_bridges": 5, "max_tags": 20,
                     "role": int(NodeRole.GATEWAY)}}),
        ("ping request (empty payload)", "request", SerialMsg.PING, 2, 0, 0, {}),
        ("ping response with credit grant", "response", SerialMsg.PING, 2, R, 3, {"status": 0, "uptime_s": 86400}),
        ("deliver layout request", "request", SerialMsg.DELIVER_LAYOUT, 0xFFFF, 0, 0, {
            "op_id": 0x1F2E3D4C5B6A79, "bridge": 2, "tag_id": 0x1A2B3C4D, "epoch": 3, "revision": 18,
            "update_id": 501, "fontpack_id": pack.pack_id, "layout": layout}),
        ("deliver layout accepted", "response", SerialMsg.DELIVER_LAYOUT, 0xFFFF, R, 1,
         {"status": int(Status.ACCEPTED)}),
        ("duplicate op response", "response", SerialMsg.ASSIGN_TAG, 9, R, 0,
         {"status": int(Status.ACCEPTED), "detail": int(Status.DUPLICATE)}),
        ("result event", "event", SerialMsg.EVT_RESULT, 0, E, 0, {
            "seq": 7, "update_id": 501, "bridge": 2, "tag_id": 0x1A2B3C4D, "epoch": 3, "revision": 18,
            "status": 0, "digest": bytes.fromhex("0011223344556677"), "battery_mv": 2950,
            "timing": {"wake_ms": 12000, "mesh_ms": 800, "transfer_ms": 4100, "refresh_ms": 3900,
                       "suspend_ms": 640}, "flags": 1, "stored_epoch": 3}),
        ("result event, escalated stale epoch", "event", SerialMsg.EVT_RESULT, 0, E, 0, {
            "seq": 8, "update_id": 502, "bridge": 3, "tag_id": 0x1A2B3C4D, "epoch": 3, "revision": 19,
            "status": int(Status.STALE_EPOCH), "digest": bytes(8), "battery_mv": 0,
            "timing": {"wake_ms": 0, "mesh_ms": 900, "transfer_ms": 0, "refresh_ms": 0, "suspend_ms": 0},
            "flags": 2, "stored_epoch": 0xFFFFFFFF}),
        ("counters response", "response", SerialMsg.GET_COUNTERS, 10, R, 0, {
            "status": 0, "counters": {"crc_errors": 0, "overruns": 2, "events_dropped": 0, "len_errors": 1}}),
        ("event ack", "request", SerialMsg.EVENT_ACK, 11, 0, 0, {"seq": 7}),
        ("log event", "event", SerialMsg.EVT_LOG, 0, E, 0, {"text": "mesh resume ok"}),
        ("unknown type answered", "response", 0x7F, 12, R, 0, {"status": int(Status.UNSUPPORTED)}),
    ]
    out = []
    for name, kind, msg, request_id, flags, credits, fields in frames:
        if msg in SerialMsg:
            payload = {"request": cbor_msgs.encode_request, "response": cbor_msgs.encode_response,
                       "event": cbor_msgs.encode_event}[kind](SerialMsg(msg), fields)
        else:
            payload = cbor_msgs.encode_map(fields)
        frame = Frame(int(msg), request_id, payload, int(flags), credits)
        decoded = encode_frame(frame)
        out.append({"name": name, "kind": kind, "type": int(msg), "request_id": request_id, "flags": int(flags),
                    "credits": credits, "fields": fields_to_json(fields), "payload": _hex(payload),
                    "decoded": _hex(decoded), "wire": _hex(cobs.encode(decoded) + b"\x00")})
    good = encode_frame(Frame(SerialMsg.PING, 3, b""))
    bad_len = bytearray(encode_frame(Frame(SerialMsg.PING, 3, b"\xa0")))
    bad_len[4] = 2
    bad_len[-4:] = crc32(bytes(bad_len[:-4])).to_bytes(4, "little")
    bad_version = bytearray(good)
    bad_version[0] = 2
    bad_version[-4:] = crc32(bytes(bad_version[:-4])).to_bytes(4, "little")
    flipped = bytearray(good)
    flipped[1] ^= 0x40
    invalid = []
    for name, data in [("too short", good[:11]), ("crc mismatch", bytes(flipped)),
                       ("length mismatch", bytes(bad_len)), ("version 2", bytes(bad_version)),
                       ("crc checked before version", bytes(bad_version[:-1]) + b"\x00")]:
        try:
            decode_frame(data)
            error = "none"
        except SerialFrameError as exc:
            error = {"FrameLengthError": "len", "FrameCrcError": "crc", "FrameVersionError": "version"}[type(exc).__name__]
        invalid.append({"name": name, "decoded": _hex(data), "error": error})
    return {
        "description": "Serial frames (docs/protocol.md §1): decoded = header | payload | crc32, wire = COBS + 0x00. "
                       "Payloads are canonical CBOR (integer keys ascending). error: which counter a receiver "
                       "increments (len_errors, crc_errors, version_errors).",
        "frames": out,
        "invalid": invalid,
    }


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------


def mesh_fixture() -> dict[str, Any]:
    fpid = bytes.fromhex("a1b2c3d4e5f60718")
    samples: dict[MeshOp, dict[str, Any]] = {
        MeshOp.LAYOUT_BEGIN: dict(xfer_id=0x1234, tag_id=0x1A2B3C4D, epoch=3, revision=18,
                                  update_id=0x1F2E3D4C5B6A79, fontpack_id=fpid, total_len=1234, chunk_count=9,
                                  digest=bytes(range(0xF0, 0x100))),
        MeshOp.LAYOUT_CHUNK: dict(xfer_id=0x1234, index=8, data=bytes(range(34))),
        MeshOp.LAYOUT_COMMIT: dict(xfer_id=0x1234),
        MeshOp.LAYOUT_CANCEL: dict(update_id=501),
        MeshOp.LAYOUT_STATUS: dict(xfer_id=0x1234, status=int(Status.INCOMPLETE), missing=0x08000005),
        MeshOp.DELIVERY_STAGE: dict(update_id=501, tag_id=0x1A2B3C4D, revision=18,
                                    stage=int(DeliveryStage.TRANSFERRING)),
        MeshOp.DELIVERY_RESULT: dict(result_seq=77, update_id=501, tag_id=0x1A2B3C4D, epoch=3, revision=18,
                                     status=0, digest=bytes.fromhex("0011223344556677"), battery_mv=2950,
                                     wake_ms=12000, suspend_ms=640, transfer_ms=4100, refresh_ms=3900,
                                     stored_epoch=0x00010003, flags=0x01),
        MeshOp.RESULT_ACK: dict(result_seq=77),
        MeshOp.CAPS_GET: {},
        MeshOp.CAPS_STATUS: dict(proto=1, fw_major=0, fw_minor=1, fw_patch=0, board=int(Board.NRF52840_BRIDGE),
                                 fontpack_id=fpid, flash_mib=64, max_tags=20, assigned=3, flags=0x01),
        MeshOp.HEALTH_GET: {},
        MeshOp.HEALTH_STATUS: dict(uptime_s=86400, sessions_ok=120, sessions_fail=3, suspend_count=130,
                                   suspend_max_ms=980, resume_fail=0, queue_depth=2, last_status=0),
        MeshOp.ASSIGN_SET: dict(tag_id=0x1A2B3C4D, epoch=3, key=bytes(range(16)), flags=1),
        MeshOp.ASSIGN_DEL: dict(tag_id=0x1A2B3C4D, epoch=2),
        MeshOp.ASSIGN_STATUS: dict(tag_id=0x1A2B3C4D, epoch=3, status=int(Status.STALE_EPOCH)),
        MeshOp.TAG_CMD: dict(update_id=0x1F2E3D4C5B6A79, tag_id=0x1A2B3C4D, epoch=3, cmd=1),
        MeshOp.IDENTIFY: dict(seconds=10),
        MeshOp.TAG_SEEN: dict(tag_id=0x1A2B3C4D, rssi=-61, battery_mv=2950, flags=0x05),
        MeshOp.TUNNEL_OPEN: dict(tunnel=0x0102, tag_id=0x1A2B3C4D, timeout_s=30),
        MeshOp.TUNNEL_DATA: dict(tunnel=0x0102, seq=0, flags=0x01, data=bytes(range(40))),
        MeshOp.TUNNEL_CLOSE: dict(tunnel=0x0102, status=int(Status.TIMEOUT)),
        MeshOp.DISCOVER: dict(duration_s=60, tag_id=0),
        MeshOp.DISCOVERED: dict(tag_id=0x1A2B3C4D, rssi=-70, flags=0x08),
        MeshOp.CAPS2_STATUS: dict(device_id=bytes(range(16)), gen=3, owner_state=1),
        MeshOp.TUNNEL_UP: dict(tunnel=0x0102, seq=1, flags=0x02, data=bytes(range(20))),
    }
    vectors = []
    for op, cls in MESH_MESSAGES.items():
        params = cls(**samples[op]).pack()
        vectors.append({"name": op.name, "op": int(op), "opcode": mesh_vendor_opcode(op),
                        "opcode_hex": _hex(mesh_opcode_bytes(op)), "fields": fields_to_json(samples[op]),
                        "params": _hex(params), "access": _hex(mesh_opcode_bytes(op) + params)})
    errors = []
    begin = MESH_MESSAGES[MeshOp.LAYOUT_BEGIN](**samples[MeshOp.LAYOUT_BEGIN]).pack()
    for name, op, data in [("LAYOUT_BEGIN one byte short", MeshOp.LAYOUT_BEGIN, begin[:-1]),
                           ("LAYOUT_BEGIN one byte long", MeshOp.LAYOUT_BEGIN, begin + b"\x00"),
                           ("LAYOUT_CHUNK without index", MeshOp.LAYOUT_CHUNK, b"\x34\x12"),
                           ("LAYOUT_CHUNK 151 data bytes", MeshOp.LAYOUT_CHUNK, b"\x34\x12\x00" + bytes(151)),
                           ("CAPS_GET with a parameter", MeshOp.CAPS_GET, b"\x00")]:
        try:
            MESH_MESSAGES[op].unpack(data)
            error = "none"
        except OversizeError:
            error = "EMSGSIZE"
        except MessageError:
            error = "EINVAL"
        errors.append({"name": name, "op": int(op), "params": _hex(data), "error": error})
    return {
        "description": "Mesh vendor messages (docs/protocol.md §2-3). access = opcode (0xC0|op, company id LE) "
                       "| params. opcode is the Zephyr BT_MESH_MODEL_OP_3(op, MESH_COMPANY_ID) value. errors: "
                       "decoder result (EINVAL short, EMSGSIZE long).",
        "vectors": vectors,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# GATT fragments and session
# ---------------------------------------------------------------------------


def fragments_fixture() -> dict[str, Any]:
    pattern = lambda n, seed: bytes((seed + 13 * i) & 0xFF for i in range(n))  # noqa: E731
    cases = [
        ("CTRL 1 byte", GattChr.CTRL, 0, bytes([CtrlMsg.ERROR])),
        ("CTRL HELLO 26 bytes", GattChr.CTRL, 0, bytes([CtrlMsg.HELLO]) + pattern(25, 1)),
        ("CTRL exactly one fragment (19)", GattChr.CTRL, 5, pattern(19, 2)),
        ("CTRL 20 bytes", GattChr.CTRL, 5, pattern(20, 3)),
        ("CTRL 38 bytes", GattChr.CTRL, 5, pattern(38, 4)),
        ("CTRL maximum 64 bytes", GattChr.CTRL, 5, pattern(64, 5)),
        ("DATA maximum record 205 bytes, seq wraps", GattChr.DATA, 60, pattern(205, 6)),
        ("STATUS CREDIT", GattChr.STATUS, 63, bytes([PlainMsg.CREDIT]) + PlainCredit(4).pack()),
    ]
    vectors: list[dict[str, Any]] = []
    for name, chr_, seq, message in cases:
        fragmenter = Fragmenter(MAX_MESSAGE[chr_])
        fragmenter.seq = seq
        fragments = fragmenter.split(message)
        vectors.append({"name": name, "characteristic": chr_.name, "max_message": MAX_MESSAGE[chr_],
                        "seq_start": seq, "message": _hex(message), "fragments": [_hex(f) for f in fragments],
                        "seq_next": fragmenter.seq})
    f = [bytes.fromhex(x) for x in vectors[3]["fragments"]]  # CTRL 20 bytes: seq 5, 6
    errors = [
        ("sequence gap", 64, [bytes([0x80 | 0]) + b"\x01", bytes([0x40 | 2]) + b"\x02"]),
        ("START in the middle", 64, [bytes([0x80 | 0]) + b"\x01", bytes([0x80 | 1]) + b"\x02"]),
        ("continuation without START", 64, [bytes([0x40 | 0]) + b"\x01"]),
        ("empty fragment", 64, [bytes([0xC0 | 0])]),
        ("CTRL message of 65 bytes", 64, Fragmenter(65).split(pattern(65, 7))),
        ("first fragment with seq 5 after connect", 64, f[:1]),
    ]
    error_cases = []
    for name, max_message, fragments in errors:
        reassembler = Reassembler(max_message)
        failed_at = None
        for i, fragment in enumerate(fragments):
            try:
                reassembler.feed(fragment)
            except FragmentError:
                failed_at = i
                break
        assert failed_at is not None, name
        error_cases.append({"name": name, "max_message": max_message, "fragments": [_hex(x) for x in fragments],
                            "error_at": failed_at, **_status(Status.INVALID)})
    return {
        "description": "GATT fragmentation (docs/protocol.md §5.3), 19-byte fragment payload (ATT MTU 23). "
                       "Receivers start at seq 0; error_at is the index of the fragment that aborts.",
        "fragment_payload": 19,
        "vectors": vectors,
        "errors": error_cases,
    }


SECRET = bytes(range(0x40, 0x60))
TAG_ID = 0x1A2B3C4D
EPOCH = 3


def _enrollment_blob() -> bytes:
    return enrollment.pack_blob(TAG_ID, SECRET, Board.LAOWU_BWR_NRF51802, PanelId.UC8176_420_BWR)


def session_caps(plane_flags: int = 0x01) -> bytes:
    """The fixture tag's CAPS value: a Laowu 4.2" black/white tag, 400 x 300, one plane."""
    return TagCaps(1, TAG_ID, Board.LAOWU_BW_NRF51822, PanelId.UC8176_420_BW, 400, 300, 1, plane_flags,
                   0, 1, 0, 192, 2).pack()


def session_fixture(pack: FontPack) -> dict[str, Any]:
    nonce_b = bytes(range(0xA0, 0xB0))
    nonce_t = bytes(range(0xB0, 0xC0))
    caps = session_caps()
    hello = bytes([CtrlMsg.HELLO]) + CtrlHello(1, TAG_ID, EPOCH, nonce_b).pack()
    challenge = bytes([CtrlMsg.CHALLENGE]) + CtrlChallenge(1, nonce_t, 2, 17, 0, 2950, 0).pack()
    k_epoch = session.derive_k_epoch(SECRET, TAG_ID, EPOCH)
    th = session.transcript_hash(caps, hello, challenge)
    mac_b = session.mac_b(k_epoch, th)
    mac_t = session.mac_t(k_epoch, th, mac_b)
    k_b2t, k_t2b = session.session_keys(k_epoch, th)
    scenario = next(s for s in render_scenarios(pack) if s[0] == "rot0_bw_status_card")
    frame = render_frame(encode_layout(scenario[3]), scenario[2], pack)
    plane = frame.planes[0]
    b2t = [
        (RecordType.FRAME_BEGIN, RecFrameBegin(18, 501, frame.digest, 1, len(plane)).pack()),
        (RecordType.PLANE_DATA, RecPlaneData(0, 0, plane[:189]).pack()),
        (RecordType.PLANE_DATA, RecPlaneData(0, 189, plane[189:200]).pack()),
        (RecordType.FRAME_END, RecFrameEnd().pack()),
    ]
    t2b = [
        (RecordType.PROGRESS, RecProgress(DeliveryStage.REFRESHING).pack()),
        (RecordType.RESULT, RecResult(501, EPOCH, 18, 0, frame.digest[:8], 2950, 3900, 0).pack()),
        (RecordType.RESULT, RecResult(501, EPOCH, 18, 0, frame.digest[:8], 2940, 0, 1).pack()),
    ]
    records = []
    sealed: dict[RecordDir, list[bytes]] = {RecordDir.B2T: [], RecordDir.T2B: []}
    for record_dir, key, items in [(RecordDir.B2T, k_b2t, b2t), (RecordDir.T2B, k_t2b, t2b)]:
        sender = session.RecordSender(key, record_dir)
        for rtype, plaintext in items:
            counter = sender.counter
            record = sender.seal(rtype, plaintext)
            sealed[record_dir].append(record)
            records.append({"direction": record_dir.name, "counter": counter, "type": int(rtype),
                            "type_name": rtype.name, "plaintext": _hex(plaintext), "record": _hex(record)})

    def flip(data: bytes, index: int) -> bytes:
        return data[:index] + bytes([data[index] ^ 0x01]) + data[index + 1 :]

    first, last = sealed[RecordDir.B2T][0], sealed[RecordDir.B2T][-1]
    replay_counter = session.seal_record(k_b2t, RecordDir.B2T, RecordType.FRAME_END, 5, b"")
    tampered = [
        ("ciphertext bit flipped", "B2T", 0, flip(first, 7)),
        ("mic bit flipped", "B2T", 0, flip(first, len(first) - 1)),
        ("type byte changed (AAD)", "B2T", 0, bytes([RecordType.FRAME_ABORT]) + first[1:]),
        ("replayed record", "B2T", 4, first),
        ("counter skipped", "B2T", 4, replay_counter),
        ("counter field edited (nonce and AAD change)", "B2T", 4, last[:1] + (4).to_bytes(4, "little") + last[5:]),
        ("wrong direction key", "T2B", 0, first),
        ("truncated below header + mic", "B2T", 0, first[:12]),
    ]
    tamper_cases = []
    for name, direction, expected_counter, record in tampered:
        key = k_b2t if direction == "B2T" else k_t2b
        try:
            session.open_record(key, RecordDir[direction], record, expected_counter)
            outcome = "OK"
        except session.AuthError:
            outcome = "AUTH_FAILED"
        tamper_cases.append({"name": name, "direction": direction, "expected_counter": expected_counter,
                             "record": _hex(record), "status_name": outcome})
    try:
        session.verify_mac_b(k_epoch, th, flip(mac_b, 0))
        bad_mac = "OK"
    except session.AuthError:
        bad_mac = "AUTH_FAILED"
    # An active relay flips plane_flags bit0 in the CAPS value the bridge reads: the bridge's transcript
    # differs from the tag's, so the tag refuses the bridge's AUTH (§5.4) and nothing is ever rendered
    # with the altered polarity.
    relayed_caps = session_caps(0x00)
    relayed_th = session.transcript_hash(relayed_caps, hello, challenge)
    relayed_mac_b = session.mac_b(k_epoch, relayed_th)
    try:
        session.verify_mac_b(k_epoch, th, relayed_mac_b)
        relayed = "OK"
    except session.AuthError:
        relayed = "AUTH_FAILED"
    stale_error = bytes([CtrlMsg.ERROR]) + CtrlError(Status.STALE_EPOCH, EPOCH + 1).pack()
    return {
        "description": "Handshake and records (docs/protocol.md §5.4-5.5). HELLO/CHALLENGE/AUTH/AUTH_OK/ERROR are the "
                       "reassembled CTRL messages including their type byte; caps is the CAPS characteristic value "
                       "bound into th = SHA-256(caps | hello | challenge).",
        "tag_secret": _hex(SECRET),
        "tag_id": TAG_ID,
        "epoch": EPOCH,
        "nonce_b": _hex(nonce_b),
        "nonce_t": _hex(nonce_t),
        "caps": _hex(caps),
        "hello": _hex(hello),
        "challenge": _hex(challenge),
        "k_epoch": _hex(k_epoch),
        "k_epoch_next": {"epoch": EPOCH + 1, "k_epoch": _hex(session.derive_k_epoch(SECRET, TAG_ID, EPOCH + 1))},
        "th": _hex(th),
        "mac_b": _hex(mac_b),
        "mac_t": _hex(mac_t),
        "auth": _hex(bytes([CtrlMsg.AUTH]) + CtrlAuth(mac_b).pack()),
        "auth_ok": _hex(bytes([CtrlMsg.AUTH_OK]) + CtrlAuthOk(mac_t).pack()),
        "k_b2t": _hex(k_b2t),
        "k_t2b": _hex(k_t2b),
        "nonce_example": {"direction": "T2B", "counter": 1, "nonce": _hex(session.record_nonce(RecordDir.T2B, 1))},
        "records": records,
        "tampered": tamper_cases,
        "bad_mac_b": {"mac_b": _hex(flip(mac_b, 0)), "status_name": bad_mac},
        "caps_relayed": {"description": "CAPS as an active relay rewrote it (plane_flags bit0 flipped): the bridge's "
                                        "th and AUTH; the tag answers ERROR{AUTH_FAILED}",
                         "caps": _hex(relayed_caps), "th": _hex(relayed_th),
                         "auth": _hex(bytes([CtrlMsg.AUTH]) + CtrlAuth(relayed_mac_b).pack()),
                         "status_name": relayed},
        "stale_epoch": {"description": "The fixture HELLO (epoch 3) to the tag with stored epoch 4: ERROR carries the "
                                       "tag's stored epoch",
                        "stored_epoch": EPOCH + 1, "error": _hex(stale_error), **_status(Status.STALE_EPOCH)},
    }


# ---------------------------------------------------------------------------
# Enrollment and tag transaction
# ---------------------------------------------------------------------------


def enrollment_fixture() -> dict[str, Any]:
    blob = _enrollment_blob()
    bad_crc = blob[:-1] + bytes([blob[-1] ^ 0xFF])
    body = bytearray(blob)
    body[4] = 2
    body[44:] = crc32(bytes(body[:44])).to_bytes(4, "little")
    cases = [("crc flipped", bad_crc), ("magic wrong", b"XTAG" + blob[4:]), ("version 2", bytes(body)),
             ("truncated", blob[:47])]
    invalid = []
    for name, data in cases:
        try:
            enrollment.unpack_blob(data)
            status = Status.OK
        except enrollment.EnrollmentError as exc:
            status = exc.status
        invalid.append({"name": name, "blob": _hex(data), **_status(status)})
    return {
        "description": "Enrollment blob (docs/protocol.md §9) written to UICR.CUSTOMER[0..11].",
        "fields": {"magic": int.from_bytes(blob[:4], "little"), "version": 1,
                   "board": int(Board.LAOWU_BWR_NRF51802), "panel": int(PanelId.UC8176_420_BWR), "flags": 0,
                   "tag_id": TAG_ID, "secret": _hex(SECRET), "crc32": int.from_bytes(blob[44:], "little")},
        "blob": _hex(blob),
        "uicr_customer_addr": {family: addr for family, addr in enrollment.UICR_CUSTOMER_ADDR.items()},
        "intel_hex": enrollment.enrollment_hex(blob, Board.LAOWU_BWR_NRF51802).splitlines(),
        "invalid": invalid,
    }


def _record_json(record: tag_txn.DisplayRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"tag_id": record.tag_id, "epoch": record.epoch, "revision": record.revision,
            "update_id": record.update_id, "digest": _hex(record.digest), "status_name": Status(record.status).name,
            "state": record.state.name}


def tag_txn_fixture() -> dict[str, Any]:
    d1, d2 = hashlib.sha256(b"frame 17").digest(), hashlib.sha256(b"frame 17 bis").digest()
    State = tag_txn.StoredState

    def rec(epoch: int, revision: int, digest: bytes, state: tag_txn.StoredState,
            status: Status = Status.OK) -> tag_txn.DisplayRecord:
        return tag_txn.DisplayRecord(TAG_ID, epoch, revision, 500, digest, status, state)

    shown = rec(3, 17, d1, State.DISPLAYED)
    unknown = rec(3, 17, d1, State.REFRESH_INTENT, Status.DISPLAY_STATE_UNKNOWN)
    panel = (1, 15000)
    cases = [
        ("no stored record", None, (3, 1, d1, 1, 15000)),
        ("older revision", shown, (3, 16, d2, 1, 15000)),
        ("older epoch, higher revision", shown, (2, 99, d2, 1, 15000)),
        ("same revision and digest, displayed", shown, (3, 17, d1, 1, 15000)),
        ("same revision, other digest, displayed", shown, (3, 17, d2, 1, 15000)),
        ("same revision, other digest, refresh intent", unknown, (3, 17, d2, 1, 15000)),
        ("same revision and digest, display state unknown", unknown, (3, 17, d1, 1, 15000)),
        ("display state unknown, wrong plane_len", unknown, (3, 17, d1, 1, 15001)),
        ("newer revision", shown, (3, 18, d2, 1, 15000)),
        ("newer epoch, lower revision", shown, (4, 1, d2, 1, 15000)),
        ("newer revision, wrong plane_len", shown, (3, 18, d2, 1, 14999)),
        ("newer revision, wrong planes", shown, (3, 18, d2, 2, 15000)),
        ("older revision and wrong planes", shown, (3, 10, d2, 2, 15000)),
        ("duplicate wins over wrong planes", shown, (3, 17, d1, 2, 15000)),
    ]
    frame_begin = []
    for name, stored, (epoch, revision, digest, planes, plane_len) in cases:
        decision = tag_txn.frame_begin_decision(stored, epoch, revision, digest, planes, plane_len, *panel)
        frame_begin.append({
            "name": name, "stored": _record_json(stored),
            "frame": {"epoch": epoch, "revision": revision, "digest": _hex(digest), "planes": planes,
                      "plane_len": plane_len},
            "expect": {"accept": decision.accept, "duplicate": decision.duplicate,
                       "status_name": decision.status.name if decision.status is not None else None},
        })
    boot = []
    for name, stored in [("no record", None), ("displayed", shown),
                         ("refresh intent", rec(3, 18, d2, State.REFRESH_INTENT)),
                         ("already unknown", unknown),
                         ("refresh intent after refresh timeout",
                          rec(3, 18, d2, State.REFRESH_INTENT, Status.REFRESH_TIMEOUT))]:
        result = tag_txn.boot_recover(stored)
        boot.append({"name": name, "stored": _record_json(stored), "expect": {
            "record": _record_json(result.record), "persist": result.persist,
            "challenge_flag_bit0": result.unknown_pending}})
    return {
        "description": "Tag decisions: the FRAME_BEGIN table of docs/protocol.md §5.6 (first matching row) and "
                       "the boot rule of §6. The panel has planes = 1, plane_len = 15000.",
        "panel": {"planes": panel[0], "plane_len": panel[1]},
        "frame_begin": frame_begin,
        "boot": boot,
    }


# ---------------------------------------------------------------------------
# Protocol v2 (docs/connect-setup.md)
# ---------------------------------------------------------------------------


def v2_fixture() -> dict[str, Any]:
    from cremind_tag.protocol.ids import GrantOp, Link, OwnerState
    from cremind_tag.protocol.msgs import Ident2
    from cremind_tag.secure import grants, identity, noise
    from cremind_tag.secure.codes import SetupPayload, format_code
    from cremind_tag.secure.messages import SecureMessage

    def key(seed: int) -> bytes:
        return hashlib.sha256(b"cremind-tag/v2/fixture" + bytes([seed])).digest()

    # --- setup codes
    codes = []
    for role, short, secret in [(NodeRole.TAG, 0x1A2B3C4D, bytes(range(10))),
                                (NodeRole.BRIDGE, 0x00000001, bytes(range(0xF0, 0xFA))),
                                (NodeRole.TAG, 0xFFFFFFFE, bytes(10))]:
        p = SetupPayload(role, short, secret)
        codes.append({"role": int(role), "short_id": short, "secret": _hex(secret), "payload": _hex(p.pack()),
                      "code": p.code(), "qr": p.qr_text()})

    # --- identities
    ids = []
    for seed, role in [(1, NodeRole.GATEWAY), (2, NodeRole.BRIDGE), (3, NodeRole.TAG)]:
        priv = key(seed)
        pub = identity.x25519_public(priv)
        dev = identity.device_id(role, pub)
        ids.append({"role": int(role), "ik_priv": _hex(priv), "ik_pub": _hex(pub), "device_id": _hex(dev),
                    "short_id": identity.short_id(dev)})

    # --- key schedule
    tag_dev = bytes.fromhex(ids[2]["device_id"])
    secret = bytes(range(10))
    root = key(10)
    h = key(11)
    k_set = identity.k_setup(secret, tag_dev)
    schedule = {
        "setup_secret": _hex(secret), "device_id": _hex(tag_dev), "k_setup": _hex(k_set),
        "static_oob": _hex(identity.static_oob(secret, tag_dev)), "root": _hex(root), "tag_id": 0x1A2B3C4D,
        "epoch": 3, "k_epoch": _hex(identity.k_epoch_v2(root, 0x1A2B3C4D, 3)), "h": _hex(h),
        "root_proof": _hex(identity.root_proof(root, h)), "maint_proof": _hex(identity.maint_proof(root, h)),
    }

    # --- grants and the device rules (connect-setup.md 3.1), in check order
    auth_sk = key(20)
    auth_pub = identity.ed25519_public(auth_sk)
    owner = bytes(range(0x40, 0x50))
    controller = identity.x25519_public(key(21))
    challenge = bytes(range(0x80, 0x90))
    gw_dev = bytes.fromhex(ids[0]["device_id"])

    def g(op: GrantOp = GrantOp.CLAIM, *, dev: bytes = gw_dev, role: NodeRole = NodeRole.GATEWAY,
          apub: bytes = auth_pub, own: bytes = owner, ctl: bytes = controller, gen: int = 0,
          gen_to: int | None = None, chal: bytes = challenge) -> bytes:
        return grants.Grant(op, dev, role, apub, own, ctl, gen, gen + 1 if gen_to is None else gen_to, chal).encode()

    claim = g()
    sig = grants.sign(claim, auth_sk)
    other_sk = key(22)
    other_pub = identity.ed25519_public(other_sk)
    owned_by_us = {"state": int(OwnerState.OWNED), "gen": 1, "authority_pub": _hex(auth_pub), "owner": _hex(owner),
                   "controller": _hex(controller)}
    unowned = {"state": int(OwnerState.UNOWNED), "gen": 0, "authority_pub": "", "owner": "", "controller": ""}
    recover_other = g(GrantOp.RECOVER, gen=1, apub=other_pub)
    cases = [
        ("claim ok", unowned, claim, sig, [GrantOp.CLAIM], None, Status.OK),
        ("not canonical", unowned, claim[:-1], sig, [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("short signature", unowned, claim, sig[:-1], [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("other device", unowned, g(dev=bytes(16)), None, [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("op not carried by this message", unowned, g(GrantOp.RECOVER), None, [GrantOp.CLAIM], None,
         Status.GRANT_INVALID),
        ("wrong challenge", unowned, g(chal=bytes(16)), None, [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("stale generation", unowned, g(gen=4), None, [GrantOp.CLAIM], None, Status.STALE_GENERATION),
        ("generation jump", unowned, g(gen_to=2), None, [GrantOp.CLAIM], None, Status.STALE_GENERATION),
        ("other controller", unowned, g(ctl=bytes(32)), None, [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("bad signature", unowned, claim, bytes(64), [GrantOp.CLAIM], None, Status.GRANT_INVALID),
        ("owned: recover ok", owned_by_us, g(GrantOp.RECOVER, gen=1), None, [GrantOp.RECOVER], None, Status.OK),
        ("owned: claim again", owned_by_us, g(GrantOp.CLAIM, gen=1), None, [GrantOp.CLAIM], None, Status.NOT_OWNER),
        ("owned: other authority", owned_by_us, recover_other, grants.sign(recover_other, other_sk),
         [GrantOp.RECOVER], None, Status.NOT_OWNER),
        ("owned: other owner", owned_by_us, g(GrantOp.RECOVER, gen=1, own=bytes(16)), None, [GrantOp.RECOVER],
         None, Status.NOT_OWNER),
        ("tag pair ok", {**unowned}, g(GrantOp.PAIR, dev=tag_dev, role=NodeRole.TAG), None, [GrantOp.PAIR], True,
         Status.OK),
        ("tag pair bad setup proof", {**unowned}, g(GrantOp.PAIR, dev=tag_dev, role=NodeRole.TAG), None,
         [GrantOp.PAIR], False, Status.PROOF_FAILED),
    ]
    grant_cases = []
    for name, own_state, raw, signature, ops, setup_ok, expected in cases:
        signature = grants.sign(raw, auth_sk) if signature is None else signature
        dev = tag_dev if "tag" in name else gw_dev
        role = NodeRole.TAG if "tag" in name else NodeRole.GATEWAY
        own = grants.DeviceOwnership(role, dev, OwnerState(own_state["state"]), own_state["gen"],
                                     bytes.fromhex(own_state["authority_pub"]), bytes.fromhex(own_state["owner"]),
                                     bytes.fromhex(own_state["controller"]))
        status, _ = grants.check_grant(own, raw, signature, challenge=challenge, session_controller=controller,
                                       expected_ops=set(ops), setup_proof_ok=setup_ok)
        assert status == expected, (name, status)
        grant_cases.append({"name": name, "role": int(role), "device_id": _hex(dev), "ownership": own_state,
                            "grant": _hex(raw), "sig": _hex(signature), "ops": [int(o) for o in ops],
                            "challenge": _hex(challenge), "session_controller": _hex(controller),
                            "setup_proof_ok": setup_ok, "expect": int(expected)})

    # --- a byte-exact secure conversation: worker -> tag PAIR through a tunnel
    tag_priv = bytes.fromhex(ids[2]["ik_priv"])
    tag_pub = bytes.fromhex(ids[2]["ik_pub"])
    ctl_priv = key(21)
    ident = Ident2(2, int(NodeRole.TAG), tag_dev, tag_pub, int(OwnerState.UNOWNED), 0, bytes(16), challenge,
                   int(Board.HEMA_NRF52811), 0, 2, 0)
    prologue = noise.prologue(Link.TUNNEL, tag_dev)
    init = noise.Initiator(ctl_priv, tag_pub, prologue, ephemeral=lambda: key(30))
    resp = noise.Responder(tag_priv, prologue, ephemeral=lambda: key(31))
    msg1 = init.write_message1()
    resp.read_message1(msg1)
    msg2, r_sess = resp.write_message2()
    _, i_sess = init.read_message2(msg2)
    hh = i_sess.handshake_hash
    pair_grant = g(GrantOp.PAIR, dev=tag_dev, role=NodeRole.TAG, ctl=controller)
    pair_sig = grants.sign(pair_grant, auth_sk)
    k_tag = identity.k_setup(secret, tag_dev)
    p_s = identity.proof_s(k_tag, hh, pair_grant)
    request = SecureMessage(int(SerialMsg.PAIR), 0, 1, cbor_msgs.encode_request(SerialMsg.PAIR, {
        "grant": pair_grant, "sig": pair_sig, "proof": p_s, "op_key": root})).pack()
    answer = SecureMessage(int(SerialMsg.PAIR), int(SerialFlag.RESPONSE), 1, cbor_msgs.encode_response(
        SerialMsg.PAIR, {"status": 0, "gen": 1, "proof": identity.proof_d(k_tag, hh, p_s)})).pack()
    sealed_request = i_sess.encrypt(request)
    assert r_sess.decrypt(sealed_request) == request
    sealed_answer = r_sess.encrypt(answer)
    assert i_sess.decrypt(sealed_answer) == answer
    conversation = {
        "ident": _hex(ident.pack()), "controller_priv": _hex(ctl_priv), "controller_pub": _hex(controller),
        "tag_ik_priv": _hex(tag_priv), "prologue": _hex(prologue), "init_ephemeral": _hex(key(30)),
        "resp_ephemeral": _hex(key(31)), "msg1": _hex(msg1), "msg2": _hex(msg2), "handshake_hash": _hex(hh),
        "setup_secret": _hex(secret), "grant": _hex(pair_grant), "sig": _hex(pair_sig), "proof_s": _hex(p_s),
        "request_plain": _hex(request), "request_sealed": _hex(sealed_request),
        "answer_plain": _hex(answer), "answer_sealed": _hex(sealed_answer),
    }
    return {
        "description": "Protocol v2 (docs/connect-setup.md): setup codes, identities, the key schedule, grant "
                       "rules in check order (expect = status code), and one byte-exact secure conversation "
                       "(Noise IK through a tunnel, then PAIR). Keys are SHA-256('cremind-tag/v2/fixture' | seed).",
        "authority_pub": _hex(auth_pub),
        "setup_codes": codes,
        "identities": ids,
        "key_schedule": schedule,
        "grant_cases": grant_cases,
        "conversation": conversation,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_all() -> dict[str, bytes]:
    """Every fixture file name -> exact content."""
    data = build_test_fontpack()
    pack = FontPack(data)
    fontpack: dict[str, Any] = fontpack_fixture(data)
    checks = fontpack.pop("_corrupt_check")
    for corruption, (boot, install) in zip(fontpack["corruptions"], checks, strict=True):
        assert (corruption["boot_status"], corruption["install_status"]) == (boot, install), corruption
    return {
        FONTPACK_NAME: data,
        "fontpack.json": _json(fontpack),
        "cobs.json": _json(cobs_fixture()),
        "crc32.json": _json(crc_fixture()),
        "serial_frames.json": _json(serial_fixture(pack)),
        "mesh_msgs.json": _json(mesh_fixture()),
        "layouts.json": _json(layouts_fixture(pack)),
        "render.json": _json(render_fixture(pack)),
        "qr.json": _json(qr_fixture()),
        "fragments.json": _json(fragments_fixture()),
        "session.json": _json(session_fixture(pack)),
        "enrollment.json": _json(enrollment_fixture()),
        "tag_txn.json": _json(tag_txn_fixture()),
        "v2_secure.json": _json(v2_fixture()),
    }


def read_fixture(name: str) -> bytes | None:
    """Committed content, with CRLF normalised for text files (git autocrlf)."""
    path = FIXTURES / name
    if not path.exists():
        return None
    data = path.read_bytes()
    return data if name.endswith(".ctfp") else data.replace(b"\r\n", b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a fixture is stale")
    args = parser.parse_args(argv)
    fixtures = build_all()
    if args.check:
        stale = [name for name, content in fixtures.items() if read_fixture(name) != content]
        for name in stale:
            print(f"stale: protocol/fixtures/{name}", file=sys.stderr)
        return 1 if stale else 0
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, content in fixtures.items():
        (FIXTURES / name).write_bytes(content)
        print(f"wrote protocol/fixtures/{name} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
