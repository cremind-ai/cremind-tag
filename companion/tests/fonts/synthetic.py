"""Tiny TrueType fonts and a self-contained font "repository" for offline tests.

`make_ttf` writes a valid, uninstructed TrueType font whose glyphs are unions
of axis-aligned rectangles (font units), so rasterised bitmaps are predictable:
with ``upem = 480`` every multiple of 30/20/15/10 units lands on a pixel edge
at 16/24/32/48 px. `make_repo` lays out a manifest, icon map, licences, lock
inputs and a pre-filled cache, so `lock` never touches the network.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from cremind_tag.protocol.ids import Icon

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 in font units


@dataclass
class Glyph:
    advance: int
    rects: list[Rect] = field(default_factory=list)


def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def _glyf(glyph: Glyph) -> bytes:
    if not glyph.rects:
        return b""
    xs = [c for r in glyph.rects for c in (r[0], r[2])]
    ys = [c for r in glyph.rects for c in (r[1], r[3])]
    points = []
    ends = []
    for x0, y0, x1, y1 in glyph.rects:  # clockwise outer contours
        points += [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
        ends.append(len(points) - 1)
    out = struct.pack(">hhhhh", len(glyph.rects), min(xs), min(ys), max(xs), max(ys))
    out += struct.pack(f">{len(ends)}H", *ends) + struct.pack(">H", 0)
    out += bytes([0x01] * len(points))  # on-curve, 16-bit deltas
    px = py = 0
    xd, yd = b"", b""
    for x, y in points:
        xd += struct.pack(">h", x - px)
        yd += struct.pack(">h", y - py)
        px, py = x, y
    out += xd + yd
    return out + b"\0" * (-len(out) % 4)


def _cmap(cmap: dict[int, int]) -> bytes:
    bmp = sorted(cp for cp in cmap if cp <= 0xFFFF)
    segs = [(cp, cp, (cmap[cp] - cp) & 0xFFFF) for cp in bmp] + [(0xFFFF, 0xFFFF, 1)]
    n = len(segs)
    search = 2 * (1 << (n.bit_length() - 1))
    f4 = struct.pack(">HHHHHHH", 4, 16 + 8 * n, 0, 2 * n, search, (search // 2).bit_length() - 1, 2 * n - search)
    f4 += struct.pack(f">{n}H", *(s[1] for s in segs)) + b"\0\0"
    f4 += struct.pack(f">{n}H", *(s[0] for s in segs))
    f4 += struct.pack(f">{n}H", *(s[2] for s in segs)) + struct.pack(f">{n}H", *([0] * n))
    groups = sorted(cmap.items())
    f12 = struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups))
    f12 += b"".join(struct.pack(">III", cp, cp, gid) for cp, gid in groups)
    header = struct.pack(">HH", 0, 2) + struct.pack(">HHI", 3, 1, 20) + struct.pack(">HHI", 3, 10, 20 + len(f4))
    return header + f4 + f12


def _name(names: dict[int, str]) -> bytes:
    records, strings = b"", b""
    for name_id in sorted(names):
        s = names[name_id].encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x409, name_id, len(s), len(strings))
        strings += s
    return struct.pack(">HHH", 0, len(names), 6 + len(records)) + records + strings


def make_ttf(glyphs: list[Glyph], cmap: dict[int, int], *, upem: int = 480, ascender: int = 420,
             descender: int = -120, names: dict[int, str] | None = None) -> bytes:
    """A minimal TrueType font (glyph 0 = .notdef as given)."""
    names = names or {1: "Test Sans", 2: "Regular", 5: "Version 1.000"}
    glyf = b""
    loca = [0]
    for g in glyphs:
        glyf += _glyf(g)
        loca.append(len(glyf))
    xs = [c for g in glyphs for r in g.rects for c in (r[0], r[2])] or [0]
    ys = [c for g in glyphs for r in g.rects for c in (r[1], r[3])] or [0]
    max_pts = max((4 * len(g.rects) for g in glyphs), default=0)
    max_ctr = max((len(g.rects) for g in glyphs), default=0)
    tables = {
        b"head": struct.pack(">IIIIHHqqhhhhHHhhh", 0x00010000, 0x00010000, 0, 0x5F0F3CF5, 0x0003, upem, 0, 0,
                             min(xs), min(ys), max(xs), max(ys), 0, 8, 2, 1, 0),
        b"hhea": struct.pack(">Ihhh" + "H" + "hhh" + "hhh" + "hhhh" + "h" + "H", 0x00010000, ascender, descender, 0,
                             max(g.advance for g in glyphs), 0, 0, max(xs), 1, 0, 0, 0, 0, 0, 0, 0, len(glyphs)),
        b"maxp": struct.pack(">IHHHHHHHHHHHHHH", 0x00010000, len(glyphs), max_pts, max_ctr, 0, 0, 2, 0, 0, 0, 0,
                             0, 0, 0, 0),
        b"hmtx": b"".join(struct.pack(">Hh", g.advance, min((r[0] for r in g.rects), default=0)) for g in glyphs),
        b"cmap": _cmap(cmap),
        b"loca": struct.pack(f">{len(loca)}I", *loca),
        b"glyf": glyf,
        b"name": _name(names),
        b"post": struct.pack(">IIhhIIIII", 0x00030000, 0, -50, 30, 0, 0, 0, 0, 0),
    }
    tags = sorted(tables)
    n = len(tags)
    search = 16 * (1 << (n.bit_length() - 1))
    out = struct.pack(">IHHHH", 0x00010000, n, search, (search // 16).bit_length() - 1, 16 * n - search)
    offset = 12 + 16 * n
    directory, body = b"", b""
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(">4sIII", tag, _checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    font = bytearray(out + directory + body)
    head_off = 12 + 16 * n + sum(len(tables[t]) + (-len(tables[t]) % 4) for t in tags[: tags.index(b"head")])
    struct.pack_into(">I", font, head_off + 8, (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --------------------------------------------------------------------------- fonts used by the tests

BOX = Glyph(300, [(30, 0, 270, 30), (30, 360, 270, 390), (30, 30, 60, 360), (240, 30, 270, 360)])  # .notdef frame
BAR = Glyph(300, [(60, 0, 240, 300)])  # solid 180x300-unit block: 6x10 px at 16 px
SPACE = Glyph(150)
HAN_A = Glyph(480, [(30, -30, 450, 0), (30, 390, 450, 420), (225, -30, 255, 420)])
HAN_B = Glyph(480, [(30, 180, 450, 210), (225, -30, 255, 420)])
HAN_C = Glyph(480, [(60, 60, 420, 360)])


def latin_font(names: dict[int, str]) -> bytes:
    """Glyphs: 0 .notdef, 1 space, 2 'A' (bar), 3 'B' (same bar), 4 wide bar."""
    wide = Glyph(9000, [(0, 0, 8400, 60)])  # 280 px at 16 px: wider than a glyph entry allows
    return make_ttf([BOX, SPACE, BAR, BAR, wide], {0x20: 1, 0x41: 2, 0x42: 3, 0x2014: 4}, names=names)


def han_font(names: dict[int, str], extra: bool) -> bytes:
    """A CJK-like regional face; two regions share glyphs 1-2, only one has glyph 3."""
    glyphs = [BOX, HAN_A, HAN_B] + ([HAN_C] if extra else [])
    cmap = {0x4E00: 1, 0x4E01: 2} | ({0x4E02: 3, 0x20000: 3} if extra else {})
    return make_ttf(glyphs, cmap, names=names)


ICON_BASE = 0xE000


def icon_font(names: dict[int, str]) -> bytes:
    """Material-like icon font: upem 480, em square = cell, icon i at U+E000+i."""
    glyphs = [Glyph(480)] + [Glyph(480, [(40, 40, 440, 440 - 10 * i)]) for i in range(len(Icon))]
    cmap = {ICON_BASE + i: i + 1 for i in range(len(Icon))}
    return make_ttf(glyphs, cmap, ascender=480, descender=0, names=names)


# --------------------------------------------------------------------------- repository

SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
SCRIPTS_TXT = """# Scripts-17.0.0.txt
0020          ; Common # Zs       SPACE
2014          ; Common # Pd       EM DASH
0041..0042    ; Latin # L&   [2] LATIN CAPITAL LETTER A..LATIN CAPITAL LETTER B
0043          ; Latin # L&       LATIN CAPITAL LETTER C
4E00..4E02    ; Han # Lo   [3] CJK UNIFIED IDEOGRAPH-4E00..CJK UNIFIED IDEOGRAPH-4E02
20000         ; Han # Lo       CJK UNIFIED IDEOGRAPH-20000
0E01          ; Thai # Lo       THAI CHARACTER KO KAI
"""
SCRIPT_EXTENSIONS_TXT = """# ScriptExtensions-17.0.0.txt
3001          ; Hani Hira # Po       IDEOGRAPHIC COMMA
"""
ALIASES_TXT = """# PropertyValueAliases-17.0.0.txt
sc ; Hani                             ; Han                              ; Hanb ; Jpan ; Kore
sc ; Hira                             ; Hiragana
sc ; Latn                             ; Latin
sc ; Thai                             ; Thai
sc ; Zyyy                             ; Common
sc ; Zinh                             ; Inherited                        ; Qaai
"""


@dataclass
class Repo:
    root: Path
    manifest: Path
    cache: Path


def _names(family: str, version: str, copyright_: str, trademark: str | None) -> dict[int, str]:
    names = {0: copyright_, 1: family, 2: "Regular", 5: f"Version {version}"}
    if trademark:
        names[7] = trademark
    return names


def make_repo(root: Path) -> Repo:
    """Manifest + icon map + cache for 1 icon face, 1 Latin face, 2 Han regional faces."""
    from cremind_tag.fonts.rasterize import font_facts

    fonts_dir = root / "fonts"
    cache = fonts_dir / "cache"
    specs = [
        # key, family, role, scripts, languages, file, copyright, trademark, data
        ("test-icons", "Test Icons", "icons", ["Zsym"], [], "icons/TestIcons-Regular.ttf", "Copyright Test Icons",
         None, icon_font),
        ("test-sans", "Test Sans", "primary", ["Latn"], [], "sans/TestSans-Regular.ttf", "Copyright Test Sans",
         "Test is a trademark", latin_font),
        ("test-han-sc", "Test Han SC", "cjk-region", ["Hans", "Hani"], ["zh-Hans", "zh"], "han/TestHanSC-Regular.ttf",
         "Copyright Test Han", None, lambda names: han_font(names, extra=True)),
        ("test-han-jp", "Test Han JP", "cjk-region", ["Jpan", "Hani"], ["ja"], "han/TestHanJP-Regular.ttf",
         "Copyright Test Han", None, lambda names: han_font(names, extra=False)),
    ]
    faces = []
    for face_id, (key, family, role, scripts, languages, path, copyright_, trademark, maker) in enumerate(specs):
        data = maker(_names(family, "1.000", copyright_, trademark))
        target = cache / "test" / Path(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry = {
            "face_id": face_id, "key": key, "family": family, "role": role, "scripts": scripts,
            "source": "test", "path": path, "size": len(data), "git_blob_sha1": git_blob_sha1(data),
            "version": "1.000", "name_version": "Version 1.000", "num_glyphs": font_facts(target).num_glyphs,
            "hinting": "none", "copyright": copyright_, "trademark": trademark,
            "license": "Apache-2.0" if role == "icons" else "OFL-1.1",
        }
        if languages:
            entry["languages"] = languages
        faces.append(entry)
    codepoints = "".join(f"{icon.name.lower()}_m {ICON_BASE + i:x}\n" for i, icon in enumerate(Icon))
    (cache / "test" / "TestIcons.codepoints").write_text(codepoints, encoding="utf-8", newline="\n")
    faces[0]["icons"] = {"map": "icons.yaml", "codepoints": {
        "path": "icons/TestIcons.codepoints", "size": len(codepoints.encode()),
        "git_blob_sha1": git_blob_sha1(codepoints.encode())}}
    licenses = []
    for lic in ("OFL-1.1", "Apache-2.0"):
        text = f"{lic} licence text for tests\n".encode()
        (cache / "test" / f"{lic}.txt").write_bytes(text)
        licenses.append({"id": lic, "file": f"LICENSES/{lic}.txt", "source": "test", "path": f"{lic}.txt",
                         "size": len(text), "git_blob_sha1": git_blob_sha1(text)})
    unicode_dir = cache / "unicode" / "17.0.0"
    unicode_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (("Scripts.txt", SCRIPTS_TXT), ("ScriptExtensions.txt", SCRIPT_EXTENSIONS_TXT),
                       ("PropertyValueAliases.txt", ALIASES_TXT)):
        (unicode_dir / name).write_text(text, encoding="utf-8", newline="\n")
    manifest = {
        "schema": "cremind-tag/fonts-manifest@1",
        "pack_name": "Test Glyph Pack",
        "sources": {"test": {"repo": "https://example.invalid/test", "ref": "v1", "commit": SOURCE_COMMIT,
                             "url_template": "https://example.invalid/{commit}/{path}"}},
        "licenses": licenses,
        "unicode": {"version": "17.0.0", "url_template": "https://example.invalid/ucd/{version}/{path}",
                    "files": ["Scripts.txt", "ScriptExtensions.txt", "PropertyValueAliases.txt"]},
        "render": {"freetype": "0.0.0", "freetype_py": "0.0.0",
                   "hinting": {"truetype-bytecode": "native", "none": "none", "cff": "autohint"}, "icons": "none"},
        "profiles": {"full": {"faces": "all", "text_sizes": [16, 24, 32], "icon_sizes": [16, 24, 32, 48]},
                     "dev": {"faces": ["test-icons", "test-sans"], "text_sizes": [16], "icon_sizes": [16, 24],
                             "flash_size": 8388608, "working_space": 1048576}},
        "faces": faces,
    }
    fonts_dir.mkdir(parents=True, exist_ok=True)
    path = fonts_dir / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    icons = {"face": "test-icons", "icons": [
        {"id": icon.value, "name": icon.name.lower(), "material": f"{icon.name.lower()}_m", "codepoint": ICON_BASE + i}
        for i, icon in enumerate(Icon)]}
    (fonts_dir / "icons.yaml").write_text(yaml.safe_dump(icons, sort_keys=False), encoding="utf-8")
    return Repo(root, path, cache)
