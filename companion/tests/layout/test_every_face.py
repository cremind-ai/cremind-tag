"""Acceptance: "cover every installed script with automated samples".

For every text face of the full pack, a sample is generated from the face's
cmap and the pinned ``Scripts.txt`` (letters of its primary script that the
layout engine draws with that face), laid out and rendered through the
reference renderer: the face must be used, no character may be unsupported,
HarfBuzz must return no .notdef and the rendering must have ink. The explicit
script tests live in test_engine.py; the hand-written multilingual set is
checked here too.
"""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.compose.preview import render_image
from cremind_tag.compose.samples import MULTILINGUAL, default_unicode, face_samples
from cremind_tag.layout import layout_text
from cremind_tag.protocol.ids import Color
from cremind_tag.protocol.layout import Layout

pytestmark = pytest.mark.fonts


def _ink(fonts: Any, block: Any) -> int:
    layout = Layout(block.box_width + 8, block.height + 8, 0, Color.WHITE, tuple(block.commands(4, 4, Color.BLACK)))
    img = render_image(layout, fonts)
    assert img.mode == "1"
    return img.histogram()[0]  # black pixels


@pytest.fixture(scope="module")
def unicode_data() -> Any:
    try:
        return default_unicode()
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(f"pinned Unicode data missing: {exc}")


def test_every_text_face_draws_its_own_sample(fonts: Any, unicode_data: Any) -> None:
    samples = face_samples(fonts, unicode_data, size_px=24)
    text_faces = [f for f in fonts.faces if f.path is not None]
    assert len(samples) == len(text_faces) == 170
    problems = []
    for sample in samples:
        if not sample.text:
            problems.append((sample.face_id, sample.key, "no character drawn by this face"))
            continue
        block = layout_text(sample.text, fonts, width=760, size_px=24, language=sample.language)
        if sample.face_id not in block.faces:
            problems.append((sample.face_id, sample.key, f"drawn with {block.faces}"))
        if block.unsupported or block.notdef:
            problems.append((sample.face_id, sample.key, block.unsupported, block.notdef))
        if _ink(fonts, block) == 0:
            problems.append((sample.face_id, sample.key, "no ink"))
    assert problems == []


@pytest.mark.parametrize(("label", "language", "text"), MULTILINGUAL, ids=[m[0] for m in MULTILINGUAL])
def test_multilingual_samples(fonts: Any, label: str, language: str, text: str) -> None:
    block = layout_text(text, fonts, width=380, size_px=24, language=language)
    assert block.unsupported == () and block.notdef == 0, label
    assert all(line.width <= 380 for line in block.lines)
    assert _ink(fonts, block) > 0
