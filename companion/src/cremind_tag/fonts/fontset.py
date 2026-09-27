"""The fonts the companion shapes with, matched to the font pack a bridge draws with.

A `FontSet` ties three things together so text layout and bridge rendering agree
glyph for glyph:

- the pinned font files (``fonts/manifest.lock.json`` + the local cache), which
  HarfBuzz shapes with and FreeType rasterised,
- the font pack built from exactly those files (face ids, strikes, metrics),
- the face metadata layout needs to choose a face (scripts, language hints,
  direction, role).

Contract between `cremind_tag.fonts` (builds packs, owns `FontSet.load`) and
`cremind_tag.layout` / `cremind_tag.compose` (consume it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FaceRole = Literal["primary", "supplement", "cjk-region", "optional", "emoji", "icons"]


@dataclass(frozen=True)
class FaceInfo:
    face_id: int
    """Face id inside the font pack; 0 is the icon face."""
    key: str
    """Manifest id, e.g. ``noto-sans-arabic``."""
    family: str
    path: Path | None
    """Local font file used for shaping — the same bytes that were rasterised. None for the icon face."""
    scripts: tuple[str, ...]
    """ISO 15924 codes the face is chosen for."""
    role: FaceRole
    languages: tuple[str, ...] = ()
    """BCP-47 prefixes that prefer this face (CJK regions: ``zh-Hans``, ``zh-Hant``, ``zh-HK``, ``ja``, ``ko``)."""
    rtl: bool = False
    variations: tuple[tuple[str, float], ...] = ()
    """Variation-axis coordinates the pack was rasterised at (Noto Emoji: ``(("wght", 400.0),)``);
    shape with the same (HarfBuzz ``font.set_variations``)."""


@dataclass(frozen=True)
class StrikeMetrics:
    face_id: int
    size_px: int
    ascent: int
    descent: int
    line_height: int
    glyph_count: int


class FontSet:
    """Faces + strikes of one font pack. Construct with `FontSet.load`."""

    def __init__(self, pack_path: Path, pack_id: bytes, faces: tuple[FaceInfo, ...],
                 strikes: dict[tuple[int, int], StrikeMetrics]) -> None:
        self.pack_path = pack_path
        self.pack_id = pack_id
        self.faces = faces
        self._by_id = {f.face_id: f for f in faces}
        self._strikes = strikes

    def face(self, face_id: int) -> FaceInfo:
        return self._by_id[face_id]

    def strike(self, face_id: int, size_px: int) -> StrikeMetrics:
        return self._strikes[(face_id, size_px)]

    def has_strike(self, face_id: int, size_px: int) -> bool:
        return (face_id, size_px) in self._strikes

    @classmethod
    def load(cls, pack_path: Path, cache_dir: Path | None = None) -> FontSet:
        """Read a built pack and resolve each face to its cached font file.

        Implemented by `cremind_tag.fonts` (it knows the manifest and cache layout).
        """
        from cremind_tag.fonts.build import load_fontset

        return load_fontset(pack_path, cache_dir)
