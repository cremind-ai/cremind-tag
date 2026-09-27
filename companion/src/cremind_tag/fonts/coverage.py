"""What a font pack can draw: per-script coverage and unsupported characters.

Script membership comes from the Unicode Character Database files pinned in the
manifest (``Scripts.txt``, ``ScriptExtensions.txt``, ``PropertyValueAliases.txt``
at ``unicode.version``, downloaded by ``cremind-tag fonts lock``); face coverage
from each face's ``cmap``. A script's coverage counts every code point
``Scripts.txt`` assigns to it; code points whose Script_Extensions include it
are reported separately.

`check_text` lists the characters of a text that no face maps — previews and
diagnostics report them. Controls, format characters (except the visible
prepended concatenation marks), separators and variation selectors need no
glyph and are never reported.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import freetype

from cremind_tag.fonts.fontset import FaceInfo, FontSet

_NO_GLYPH_GC = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
# Prepended_Concatenation_Mark: format characters that are drawn.
_VISIBLE_CF = frozenset({*range(0x0600, 0x0606), 0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2, 0x110BD, 0x110CD})
_LINE = re.compile(r"^([0-9A-F]{4,6})(?:\.\.([0-9A-F]{4,6}))?\s*;\s*([^#]+)(?:#\s*(\S\S))?")
_HAN_DEFAULT_LANGUAGE = "zh-Hans"


def needs_glyph(ch: str) -> bool:
    """False for characters a renderer never draws (controls, most format characters, variation selectors)."""
    cp = ord(ch)
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or 0x180B <= cp <= 0x180F or cp == 0x034F:
        return False
    return cp in _VISIBLE_CF or unicodedata.category(ch) not in _NO_GLYPH_GC


# --------------------------------------------------------------------------- Unicode data


@dataclass(frozen=True)
class UnicodeScripts:
    version: str
    names: dict[str, str]
    """ISO 15924 code -> Unicode long name (``Latn`` -> ``Latin``)."""
    scripts: dict[str, frozenset[int]]
    """ISO code -> code points whose Script is that code (Zyyy = Common, Zinh = Inherited)."""
    extensions: dict[str, frozenset[int]]
    """ISO code -> code points whose Script_Extensions list it but whose Script differs."""
    spans: tuple[tuple[int, int, str], ...]
    """Sorted (first, last, ISO code) ranges from Scripts.txt."""

    def script_of(self, cp: int) -> str:
        """ISO 15924 Script of ``cp`` (``Zzzz`` when unassigned)."""
        i = bisect_right(self.spans, (cp, 0x10FFFF, "~")) - 1
        if i >= 0 and self.spans[i][0] <= cp <= self.spans[i][1]:
            return self.spans[i][2]
        return "Zzzz"


def _parse(path: Path) -> list[tuple[int, int, str, str | None]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m:
            out.append((int(m[1], 16), int(m[2] or m[1], 16), m[3].strip(), m[4]))
    return out


def load_unicode(directory: Path, version: str) -> UnicodeScripts:
    """Parse the pinned UCD files from ``directory`` (the cache's ``unicode/<version>``)."""
    for name in ("Scripts.txt", "ScriptExtensions.txt", "PropertyValueAliases.txt"):
        path = directory / name
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing; run `cremind-tag fonts fetch`")
        head = path.read_text(encoding="utf-8").split("\n", 1)[0]
        if version not in head:
            raise ValueError(f"{path} is not Unicode {version} ({head!r})")
    names: dict[str, str] = {}
    short_to_code: dict[str, str] = {}
    for line in (directory / "PropertyValueAliases.txt").read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("#")[0].split(";")]
        if len(parts) >= 3 and parts[0] == "sc":
            names[parts[1]] = parts[2]
            short_to_code[parts[2]] = parts[1]
            short_to_code[parts[1]] = parts[1]
    scripts: dict[str, set[int]] = {}
    of: dict[int, str] = {}
    spans = []
    for lo, hi, long_name, _gc in _parse(directory / "Scripts.txt"):
        code = short_to_code[long_name]
        spans.append((lo, hi, code))
        scripts.setdefault(code, set()).update(range(lo, hi + 1))
        for cp in range(lo, hi + 1):
            of[cp] = code
    extensions: dict[str, set[int]] = {}
    for lo, hi, values, _gc in _parse(directory / "ScriptExtensions.txt"):
        for short in values.split():
            code = short_to_code[short]
            for cp in range(lo, hi + 1):
                if of.get(cp) != code:
                    extensions.setdefault(code, set()).add(cp)
    return UnicodeScripts(version, names, {k: frozenset(v) for k, v in scripts.items()},
                          {k: frozenset(v) for k, v in extensions.items()}, tuple(sorted(spans)))


# --------------------------------------------------------------------------- cmaps


@lru_cache(maxsize=512)
def _cmap(path: str, size: int, mtime_ns: int) -> frozenset[int]:
    return frozenset(cp for cp, gid in freetype.Face(path).get_chars() if gid)


def face_cmap(path: Path) -> frozenset[int]:
    """Code points a font file maps to a glyph (memoised per file state)."""
    st = Path(path).stat()
    return _cmap(str(path), st.st_size, st.st_mtime_ns)


class CmapIndex:
    """Code point -> text faces that map it, for the faces of one pack."""

    def __init__(self, faces: Iterable[tuple[int, Path]]) -> None:
        self.cmaps: dict[int, frozenset[int]] = {face_id: face_cmap(path) for face_id, path in faces}
        self._all: frozenset[int] = frozenset().union(*self.cmaps.values()) if self.cmaps else frozenset()

    @classmethod
    def for_fontset(cls, fonts: FontSet) -> CmapIndex:
        return _index_for(tuple((f.face_id, f.path) for f in fonts.faces if f.path is not None))

    @property
    def code_points(self) -> frozenset[int]:
        """Every code point some face maps."""
        return self._all

    def covers(self, cp: int) -> bool:
        return cp in self._all

    def faces_for(self, cp: int) -> tuple[int, ...]:
        return tuple(fid for fid, cmap in self.cmaps.items() if cp in cmap)

    def check_text(self, text: str) -> tuple[str, ...]:
        """Characters of ``text`` that need a glyph and that no face maps, in first-seen order."""
        seen: dict[str, None] = {}
        for ch in text:
            if ch not in seen and needs_glyph(ch) and ord(ch) not in self._all:
                seen[ch] = None
        return tuple(seen)


@lru_cache(maxsize=8)
def _index_for(faces: tuple[tuple[int, Path], ...]) -> CmapIndex:
    return CmapIndex(faces)


@lru_cache(maxsize=1)
def _default_index() -> CmapIndex:
    """Every text face of the manifest's full profile, from the verified cache."""
    from cremind_tag.fonts.build import plan_build
    from cremind_tag.fonts.fetch import verify_cached
    from cremind_tag.fonts.manifest import default_cache_dir, load_lock, load_manifest

    manifest = load_manifest()
    plan = plan_build(manifest, "full")
    text = [f for f in plan.faces if not f.is_icons]
    paths = verify_cached(load_lock(manifest.lock_path), default_cache_dir(), [f.key for f in text])
    return _index_for(tuple((f.face_id, paths[f.key]) for f in text))


def check_text(text: str, fonts: FontSet | CmapIndex | None = None) -> tuple[str, ...]:
    """Characters of ``text`` no face covers (``fonts`` defaults to the full profile's faces)."""
    if isinstance(fonts, FontSet):
        index = CmapIndex.for_fontset(fonts)
    elif isinstance(fonts, CmapIndex):
        index = fonts
    else:
        index = _default_index()
    return index.check_text(text)


def _lang_match(tag: str, prefixes: Sequence[str]) -> int:
    """Length of the longest BCP-47 prefix of ``tag`` in ``prefixes`` (0 = none)."""
    tag = tag.lower()
    best = 0
    for p in prefixes:
        p = p.lower()
        if tag == p or tag.startswith(p + "-"):
            best = max(best, len(p))
    return best


def candidate_faces(fonts: FontSet, script: str, language: str = "") -> tuple[FaceInfo, ...]:
    """Text faces declaring ``script`` (ISO 15924), best first.

    Faces whose ``languages`` match ``language`` by the longest BCP-47 prefix
    win (``zh-Hant-HK`` picks HK over TC); then primary, CJK-region,
    supplement and optional faces in face-id order. Han with no matching
    language prefers the ``zh-Hans`` face (Noto Sans SC). Callers still check
    the chosen face's cmap and fall back to `CmapIndex.faces_for`.
    """
    rank = {"primary": 0, "emoji": 0, "cjk-region": 1, "supplement": 2, "optional": 3}
    faces = [f for f in fonts.faces if f.path is not None and script in f.scripts]
    if not any(_lang_match(language, f.languages) for f in faces) and script == "Hani":
        language = _HAN_DEFAULT_LANGUAGE
    return tuple(sorted(faces, key=lambda f: (-_lang_match(language, f.languages), rank.get(f.role, 4),
                                              f.face_id)))


# --------------------------------------------------------------------------- report


def ranges(cps: Sequence[int]) -> list[tuple[int, int]]:
    """Sorted code points -> inclusive ranges."""
    out: list[tuple[int, int]] = []
    for cp in cps:
        if out and cp == out[-1][1] + 1:
            out[-1] = (out[-1][0], cp)
        else:
            out.append((cp, cp))
    return out


def format_ranges(cps: Iterable[int]) -> list[str]:
    return [f"U+{lo:04X}" if lo == hi else f"U+{lo:04X}..U+{hi:04X}" for lo, hi in ranges(sorted(cps))]


def coverage_report(unicode: UnicodeScripts, faces: Sequence[tuple[int, str, tuple[str, ...], Path]]) -> dict[str, Any]:
    """Per-script and per-face coverage of ``faces`` = [(face_id, key, declared scripts, path)]."""
    index = CmapIndex((fid, path) for fid, _key, _scripts, path in faces)
    covered_all = index.code_points
    scripts: dict[str, Any] = {}
    summary = {"full": [], "partial": [], "none": []}
    for code in sorted(unicode.scripts):
        cps = unicode.scripts[code]
        have = cps & covered_all
        ext = unicode.extensions.get(code, frozenset())
        status = "full" if len(have) == len(cps) else ("none" if not have else "partial")
        if code not in ("Zyyy", "Zinh"):
            summary[status].append(code)
        providers = sorted({fid for fid, cmap in index.cmaps.items() if cmap & cps})
        scripts[code] = {
            "name": unicode.names.get(code, code), "total": len(cps), "covered": len(have), "status": status,
            "faces": providers if code not in ("Zyyy", "Zinh") else len(providers),
            "missing": format_ranges(cps - have),
            "extensions_total": len(ext), "extensions_covered": len(ext & covered_all),
        }
    per_face = {}
    for fid, key, declared, _path in faces:
        cmap = index.cmaps[fid]
        per_face[key] = {"face_id": fid, "cmap": len(cmap),
                         "declared": {s: [len(cmap & unicode.scripts.get(s, frozenset())),
                                          len(unicode.scripts.get(s, frozenset()))] for s in declared
                                      if s in unicode.scripts}}
    return {
        "unicode_version": unicode.version,
        "summary": {"scripts": len(summary["full"]) + len(summary["partial"]) + len(summary["none"]),
                    "full": len(summary["full"]), "partial": len(summary["partial"]), "none": len(summary["none"]),
                    "partial_scripts": summary["partial"], "missing_scripts": summary["none"],
                    "code_points_covered": len(covered_all)},
        "scripts": scripts,
        "faces": per_face,
    }
