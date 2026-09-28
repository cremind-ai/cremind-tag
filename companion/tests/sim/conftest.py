"""Shared fixtures for simulator tests: font packs from the protocol fixtures, layouts, an async runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cremind_tag.fontpack.format import FontPack, StrikeSpec, build_pack
from cremind_tag.protocol.ids import Color
from cremind_tag.protocol.layout import Glyph, Glyphs, Icon, Layout, Rect, encode_layout

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "protocol" / "fixtures"


def _load(name: str) -> ModuleType:
    """A helper module of this directory (``--import-mode=importlib`` puts none on ``sys.path``)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


v2host = _load("v2host")  # the host-side protocol v2 driver of the test_v2_* modules


@pytest.fixture(scope="session")
def fixture_pack() -> bytes:
    return (FIXTURES / "fontpack_test.ctfp").read_bytes()


@pytest.fixture(scope="session")
def tiny_pack(fixture_pack: bytes) -> bytes:
    """A pack built with ``fontpack.format.build_pack`` from the fixture's 24 px strikes."""
    source = FontPack(fixture_pack)
    strikes = []
    for strike in source.strikes:
        if strike.size_px != 24:
            continue
        glyphs = tuple(source.glyph(strike.face_id, strike.size_px, gid) for gid in range(strike.glyph_count))
        strikes.append(StrikeSpec(strike.face_id, strike.size_px, strike.ascent, strike.descent, strike.line_height,
                                  glyphs))  # type: ignore[arg-type]
    return build_pack(source.faces, strikes, source.manifest_id)


@pytest.fixture(scope="session")
def render_scenarios() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "render.json").read_text(encoding="ascii"))["scenarios"]


def _card(variant: int = 0, glyph_count: int = 6) -> bytes:
    glyphs = tuple(Glyph(2 + (i % 17), 14 if i else 0, 0) for i in range(glyph_count))
    commands = (
        Rect(4 + variant, 4, 392 - 2 * variant, 292, 2, Color.BLACK),
        Icon(2, 24, Color.BLACK, 16, 16),
        Glyphs(1, 24, Color.BLACK, 52, 36, glyphs),
        Rect(16, 250, 200 + 10 * variant, 12, 0, Color.BLACK),
    )
    return encode_layout(Layout(400, 300, 0, Color.WHITE, commands))


@pytest.fixture(scope="session")
def card() -> Any:
    """``card(variant)``: a 400x300 status card on the 24 px strikes; each variant has its own digest."""
    return _card


@pytest.fixture(scope="session")
def long_card() -> bytes:
    """A layout of more than three mesh chunks (several GLYPHS runs)."""
    runs = tuple(Glyphs(1, 24, Color.BLACK, 20, 40 + 30 * row,
                        tuple(Glyph(2 + (i % 17), 12 if i else 0, 0) for i in range(24))) for row in range(6))
    return encode_layout(Layout(400, 300, 0, Color.WHITE, runs))
