"""ICU-backed Unicode algorithms: graphemes, line breaks, bidi, scripts, emoji."""

from __future__ import annotations

import pytest

from cremind_tag.layout.unicode import (
    ParagraphBidi,
    Utf16Map,
    emoji_presentation,
    graphemes,
    line_breaks,
    paragraph_level,
    script_extensions,
    script_of,
    visual_order,
)


def test_graphemes_never_split_clusters_and_map_utf16() -> None:
    text = "á👍🏽🇻🇳👨‍👩‍👧x"
    bounds = graphemes(text)
    clusters = [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    assert clusters == ["á", "👍🏽", "🇻🇳", "👨‍👩‍👧", "x"]
    u16 = Utf16Map.of("a😀b")
    assert u16.to16 == (0, 1, 3, 4) and u16.to_cp(3) == 2 and not u16.is_bmp


def test_thai_dictionary_breaks() -> None:
    # สวัสดี | ครับ | ผม | ชื่อ | สมชาย — no spaces, ICU's dictionary finds the words.
    assert [b for b, _ in line_breaks("สวัสดีครับผมชื่อสมชาย", "th")] == [6, 10, 12, 16, 21]
    # Locale-independent: the dictionary applies to Thai text under any hint.
    assert line_breaks("สวัสดีครับผมชื่อสมชาย", "en") == line_breaks("สวัสดีครับผมชื่อสมชาย", "th")
    assert [b for b, _ in line_breaks("ສະບາຍດີ ພາສາລາວ", "")][-1] == len("ສະບາຍດີ ພາສາລາວ")


def test_cjk_strict_breaks_and_hard_breaks() -> None:
    text = "東京タワーへ行きましょう"
    breaks = [b for b, _ in line_breaks(text, "ja")]
    assert text.index("ー") not in breaks  # never before the prolonged sound mark
    assert text.index("ょ") not in breaks  # nor before small kana
    assert 1 in breaks  # between ideographs
    assert line_breaks("a\nb", "en")[0] == (2, True)
    assert [b for b, _ in line_breaks("well-known words", "en")] == [5, 11, 16]


def test_paragraph_direction() -> None:
    assert paragraph_level("Hello", "auto", "en") == 0
    assert paragraph_level("שלום world", "auto", "en") == 1
    assert paragraph_level("Google Drive: تم الحفظ", "auto", "ar") == 1  # RTL hint + RTL content
    assert paragraph_level("Google Drive: تم الحفظ", "auto", "en") == 0  # first strong
    assert paragraph_level("Error 404", "auto", "ar") == 0  # no RTL content: first strong wins
    assert paragraph_level("123 !", "auto", "he") == 1 and paragraph_level("123 !", "auto", "en") == 0
    assert paragraph_level("abc", "rtl", "en") == 1 and paragraph_level("אבג", "ltr", "he") == 0


def test_bidi_levels_and_l2() -> None:
    bidi = ParagraphBidi("abc אבג 123 def", 0)
    assert bidi.levels == [0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 0, 0, 0, 0]
    assert bidi.line_levels(0, 8)[-1] == 0  # L1: trailing whitespace at the paragraph level
    assert visual_order([0, 1, 1, 2, 1, 0]) == [0, 4, 3, 2, 1, 5]
    assert visual_order([1, 1, 1]) == [2, 1, 0]
    assert visual_order([2, 2, 1]) == [2, 0, 1]


def test_scripts_and_emoji_properties() -> None:
    assert script_of(ord("ก")) == "Thai" and script_of(ord("!")) == "Zyyy" and script_of(0x0301) == "Zinh"
    assert "Deva" in script_extensions(0x0964)
    assert emoji_presentation([0x1F600]) is True
    assert emoji_presentation([0x263A]) is False  # ☺ defaults to text
    assert emoji_presentation([0x263A, 0xFE0F]) is True
    assert emoji_presentation([0x1F600, 0xFE0E]) is False
    assert emoji_presentation([ord("1"), 0xFE0F, 0x20E3]) is True
    assert emoji_presentation([0x1F1FB, 0x1F1F3]) is True
    assert emoji_presentation([ord("a")]) is None


@pytest.mark.parametrize("text", ["", "a", "😀😀"])
def test_small_inputs(text: str) -> None:
    assert graphemes(text)[-1] == len(text)
