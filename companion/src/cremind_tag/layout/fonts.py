"""Per-`FontSet` state layout needs: HarfBuzz fonts, cmap coverage, face choice, pack glyphs.

`FontContext.for_fontset(fonts)` is cached per FontSet (weakly), so the
daemon's first composition pays for loading and every later one reuses:

- HarfBuzz faces built from the **exact cached file bytes** the pack was
  rasterised from (glyph ids match the pack 1:1), with the face's variation
  coordinates applied, one ``hb.Font`` per (face, size) scaled to 26.6 at the
  strike's pixel size;
- cmap coverage (`cremind_tag.fonts.coverage.CmapIndex`);
- the font pack itself (`FontPack`) for ink extents and to skip ink-less glyphs.

Face choice for one grapheme cluster (docs/layout.md "Font selection"):

1. emoji presentation -> the emoji face when it maps the cluster;
2. the faces declaring the cluster's resolved script, best first by language
   (`candidate_faces`: longest BCP-47 match, then primary, CJK region,
   supplement), the first that maps every character that needs a glyph;
3. the previous cluster's face, when it maps the cluster (keeps punctuation and
   digits in the run's font);
4. every face that maps the cluster, by language match, role (primary, CJK
   region, supplement, optional, emoji last unless asked) and face id;
5. a face mapping the base character only; else the previous / base face, whose
   ``.notdef`` then shows the gap and the characters are reported unsupported.

Only faces with a strike at the requested size take part.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Sequence
from functools import lru_cache

import uharfbuzz as hb

from cremind_tag.fontpack.format import FontPack, GlyphBitmap
from cremind_tag.fonts.coverage import CmapIndex, _lang_match, candidate_faces, needs_glyph
from cremind_tag.fonts.fontset import FaceInfo, FontSet
from cremind_tag.protocol.ids import FONT_SIZES

_ROLE_RANK = {"primary": 0, "cjk-region": 1, "supplement": 2, "optional": 3, "emoji": 4}
_HAN_FAMILY = frozenset({"Hani", "Hira", "Kana", "Hang", "Bopo", "Hrkt"})
_contexts: weakref.WeakKeyDictionary[FontSet, FontContext] = weakref.WeakKeyDictionary()
_contexts_lock = threading.Lock()


@lru_cache(maxsize=8)
def _load_pack(path: str, pack_id: bytes) -> FontPack:
    with open(path, "rb") as fh:
        pack = FontPack(fh.read(), verify_content=False)
    if pack.pack_id != pack_id:
        raise ValueError(f"{path} changed on disk: pack id {pack.pack_id.hex()}, the FontSet has {pack_id.hex()}")
    return pack


class FontContext:
    """Shaping and fallback state for one `FontSet` (see the module docstring)."""

    def __init__(self, fonts: FontSet) -> None:
        self.fonts = fonts
        self.text_faces: dict[int, FaceInfo] = {f.face_id: f for f in fonts.faces if f.path is not None}
        if not self.text_faces:
            raise ValueError("the font set has no text faces")
        self.index = CmapIndex.for_fontset(fonts)
        self.emoji_face = next((f.face_id for f in fonts.faces if f.role == "emoji"), None)
        self._hb_faces: dict[int, hb.Face] = {}
        self._hb_fonts: dict[tuple[int, int], hb.Font] = {}
        self._covering: dict[int, tuple[int, ...]] = {}
        self._candidates: dict[tuple[str, str, int], tuple[int, ...]] = {}
        self._fallback: dict[tuple[tuple[int, ...], str, int, bool], tuple[int, ...]] = {}
        self._ink: dict[tuple[int, int, int], tuple[int, int, int, int] | None] = {}
        self._lock = threading.RLock()

    @classmethod
    def for_fontset(cls, fonts: FontSet) -> FontContext:
        with _contexts_lock:
            ctx = _contexts.get(fonts)
            if ctx is None:
                ctx = _contexts[fonts] = cls(fonts)
            return ctx

    # ------------------------------------------------------------------ pack and strikes

    @property
    def pack(self) -> FontPack:
        return _load_pack(str(self.fonts.pack_path), self.fonts.pack_id)

    def has_strike(self, face_id: int, size_px: int) -> bool:
        return self.fonts.has_strike(face_id, size_px)

    def base_face(self, size_px: int) -> int:
        """The face whose metrics set the minimum line box (Noto Sans, face 1, when present)."""
        if 1 in self.text_faces and self.has_strike(1, size_px):
            return 1
        for fid in sorted(self.text_faces):
            if self.has_strike(fid, size_px):
                return fid
        raise ValueError(f"no text face has a {size_px} px strike")

    def text_sizes(self) -> tuple[int, ...]:
        """FONT_SIZES the base face has strikes for (the dev pack has no 32 px)."""
        base = 1 if 1 in self.text_faces else min(self.text_faces)
        return tuple(s for s in FONT_SIZES if self.has_strike(base, s))

    def glyph(self, face_id: int, size_px: int, glyph_id: int) -> GlyphBitmap | None:
        return self.pack.glyph(face_id, size_px, glyph_id)

    def ink(self, face_id: int, size_px: int, glyph_id: int) -> tuple[int, int, int, int] | None:
        """(left, top, right, bottom) of the glyph's bitmap relative to its origin, or None without ink."""
        key = (face_id, size_px, glyph_id)
        box = self._ink.get(key, False)
        if box is False:
            g = self.pack.glyph(face_id, size_px, glyph_id)
            if g is None or g.empty or not g.bitmap or not any(g.bitmap):
                box = None
            else:
                box = (g.bearing_x, -g.bearing_y, g.bearing_x + g.width, -g.bearing_y + g.height)
            self._ink[key] = box
        return box  # type: ignore[return-value]

    # ------------------------------------------------------------------ HarfBuzz

    def hb_font(self, face_id: int, size_px: int) -> hb.Font:
        key = (face_id, size_px)
        font = self._hb_fonts.get(key)
        if font is None:
            with self._lock:
                face = self._hb_faces.get(face_id)
                if face is None:
                    info = self.text_faces[face_id]
                    assert info.path is not None
                    face = self._hb_faces[face_id] = hb.Face(hb.Blob(info.path.read_bytes()))
                font = hb.Font(face)
                font.scale = (size_px * 64, size_px * 64)
                font.ppem = (size_px, size_px)
                variations = self.text_faces[face_id].variations
                if variations:
                    font.set_variations(dict(variations))
                self._hb_fonts[key] = font
        return font

    # ------------------------------------------------------------------ coverage and choice

    def maps(self, face_id: int, cps: Sequence[int]) -> bool:
        cmap = self.index.cmaps.get(face_id)
        return cmap is not None and all(cp in cmap for cp in cps)

    def covering(self, cp: int) -> tuple[int, ...]:
        faces = self._covering.get(cp)
        if faces is None:
            faces = self._covering[cp] = self.index.faces_for(cp)
        return faces

    def covers(self, cp: int) -> bool:
        return self.index.covers(cp)

    def candidates(self, script: str, language: str, size_px: int) -> tuple[int, ...]:
        key = (script, language, size_px)
        out = self._candidates.get(key)
        if out is None:
            out = tuple(f.face_id for f in candidate_faces(self.fonts, script, language)
                        if self.has_strike(f.face_id, size_px))
            self._candidates[key] = out
        return out

    def _ranked(self, faces: tuple[int, ...], language: str, size_px: int, emoji: bool) -> tuple[int, ...]:
        key = (faces, language, size_px, emoji)
        out = self._fallback.get(key)
        if out is None:
            def rank(fid: int) -> tuple[int, int, int, int]:
                info = self.text_faces[fid]
                role = _ROLE_RANK.get(info.role, 5)
                if emoji and info.role == "emoji":
                    role = -1
                return (-_lang_match(language, info.languages) if language else 0, 0 if fid == 1 else 1, role, fid)
            out = tuple(sorted((f for f in faces if f in self.text_faces and self.has_strike(f, size_px)), key=rank))
            self._fallback[key] = out
        return out

    def choose(self, cluster: Sequence[int], script: str, language: str, size_px: int, previous: int | None,
               emoji: bool | None) -> tuple[int, bool]:
        """(face id, fully mapped) for one grapheme cluster (rules in the module docstring)."""
        need = [cp for cp in cluster if needs_glyph(chr(cp))]
        if not need:
            fid = previous if previous is not None else self.base_face(size_px)
            return fid, True
        if emoji and self.emoji_face is not None and self.has_strike(self.emoji_face, size_px) \
                and self.maps(self.emoji_face, need):
            return self.emoji_face, True
        if script not in ("Zyyy", "Zinh", "Zzzz"):
            for fid in self.candidates(script, language, size_px):
                if self.maps(fid, need):
                    return fid, True
        if previous is not None and self.maps(previous, need) and not (previous == self.emoji_face and not emoji):
            return previous, True
        common = set(self.covering(need[0]))
        for cp in need[1:]:
            common &= set(self.covering(cp))
        ranked = self._ranked(tuple(sorted(common)), language, size_px, bool(emoji))
        if emoji is False or emoji is None:
            text_first = [f for f in ranked if f != self.emoji_face]
            ranked = tuple(text_first + [f for f in ranked if f == self.emoji_face])
        if ranked:
            return ranked[0], True
        base_faces = self._ranked(self.covering(need[0]), language, size_px, bool(emoji))
        if base_faces:
            return base_faces[0], False
        return (previous if previous is not None else self.base_face(size_px)), False


def han_language(language: str, scripts: set[str]) -> str:
    """Language used to pick CJK faces: the hint, or ``ja``/``ko`` inferred from kana/hangul in the text."""
    primary = (language or "").split("-", 1)[0].lower()
    if primary in ("zh", "ja", "ko", "yue"):
        return language
    if scripts & {"Hira", "Kana", "Hrkt"}:
        return "ja"
    if "Hang" in scripts:
        return "ko"
    return language


def is_han_family(script: str) -> bool:
    return script in _HAN_FAMILY
