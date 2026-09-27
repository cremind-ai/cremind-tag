"""Paragraph layout with the real pack: shaping, bidi, breaking, reshaping, fallback, rounding."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from typing import Any

import pytest
import uharfbuzz as hb

from cremind_tag.layout import FontContext, layout_text, measure_text
from cremind_tag.layout import engine as engine_module

pytestmark = pytest.mark.fonts


def lay(fonts: Any, text: str, **kw: Any) -> Any:
    kw.setdefault("width", 2000)
    kw.setdefault("size_px", 24)
    return layout_text(text, fonts, **kw)


def per_cluster(block: Any) -> dict[int, frozenset[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    for g in block.glyphs:
        out[g.cluster].add(g.glyph_id)
    return {k: frozenset(v) for k, v in out.items()}


def x_of(block: Any, index: int) -> int:
    xs = [g.x for g in block.glyphs if g.cluster == index]
    assert xs, f"no glyph for code point {index} ({block.text[index]!r})"
    return min(xs)


# --------------------------------------------------------------------------- Arabic, Hebrew, bidi


def test_arabic_joining_uses_contextual_forms(fonts: Any) -> None:
    isolated = per_cluster(lay(fonts, "ب"))[0]
    three = per_cluster(lay(fonts, "ببب"))
    initial, medial, final = three[0], three[1], three[2]
    assert len({isolated, initial, medial, final}) == 4
    assert all(g.face_id == 5 for g in lay(fonts, "ببب").glyphs)


def test_lam_alef_ligature(fonts: Any) -> None:
    lam_alef = per_cluster(lay(fonts, "لا"))
    lam_initial = per_cluster(lay(fonts, "لب"))[0]
    alef_final = per_cluster(lay(fonts, "با"))[1]
    # The mandatory lam-alef ligature replaces the plain joining forms of both letters.
    assert lam_alef[0] != lam_initial and lam_alef[1] != alef_final
    assert lam_alef[0] | lam_alef[1] != lam_initial | alef_final


def test_rtl_paragraph_reorders_latin_and_digits(fonts: Any) -> None:
    text = "مرحبا ABC 123"
    block = lay(fonts, text, width=400)
    assert block.lines[0].rtl
    xs = [x_of(block, text.index(c)) for c in "ABC123"]
    assert xs == sorted(xs), "Latin and digits keep their left-to-right order"
    arabic = [x_of(block, i) for i in range(5)]
    assert arabic == sorted(arabic, reverse=True), "Arabic runs right to left"
    assert max(xs) < min(arabic), "in an RTL paragraph the trailing LTR run sits left of the Arabic"
    assert block.lines[0].x + block.lines[0].width == 400  # start alignment = right


def test_arabic_digits_inside_arabic(fonts: Any) -> None:
    text = "العدد 2026 كبير"
    block = lay(fonts, text)
    digits = [x_of(block, i) for i in range(6, 10)]
    assert digits == sorted(digits) and len(set(digits)) == 4  # "2026" still reads left to right
    assert x_of(block, 0) > x_of(block, 6) > x_of(block, text.index("ك"))


def test_hebrew_runs_right_to_left(fonts: Any) -> None:
    text = "שלום עולם"
    block = lay(fonts, text)
    letters = [i for i, c in enumerate(text) if c != " "]
    xs = [x_of(block, i) for i in letters]
    assert xs == sorted(xs, reverse=True)
    assert {g.face_id for g in block.glyphs} == {47}


def test_mixed_directions_on_one_line(fonts: Any) -> None:
    text = "Hello שלום world"
    block = lay(fonts, text)
    assert not block.lines[0].rtl and len(block.lines) == 1
    assert x_of(block, 0) < x_of(block, text.index("ם")) < x_of(block, text.index("ש")) < x_of(block, text.index("w"))


# --------------------------------------------------------------------------- line breaking


def test_thai_breaks_at_dictionary_word_boundaries(fonts: Any) -> None:
    text = "สวัสดีครับผมชื่อสมชายวันนี้อากาศดีมาก"
    words = ["สวัสดี", "ครับ", "ผม", "ชื่อ", "สมชาย", "วัน", "นี้", "อากาศ", "ดี", "มาก"]
    assert "".join(words) == text
    boundaries = {0}
    for w in words:
        boundaries.add(max(boundaries) + len(w))
    width = measure_text("สวัสดีครับ", fonts, size_px=24) + 2
    block = lay(fonts, text, width=width, language="th")
    assert len(block.lines) >= 3
    for line in block.lines:
        assert line.start in boundaries and line.end in boundaries, (line, block.text[line.start:line.end])
        assert line.width <= width
    assert "".join(block.text[ln.start:ln.end] for ln in block.lines) == text


def test_cjk_breaks_and_kinsoku(fonts: Any) -> None:
    text = "今日は良い天気ですね。東京タワーへ行きましょう！"
    block = lay(fonts, text, width=5 * 24 + 4, language="ja")
    assert len(block.lines) >= 4
    for line in block.lines[1:]:
        assert block.text[line.start] not in "。、！ーょゃゅっ", block.text[line.start:line.end]
    assert {g.face_id for g in block.glyphs} == {169}


@pytest.mark.parametrize(("language", "face"), [
    ("zh-Hans", 166), ("zh-CN", 166), ("zh", 166), ("zh-TW", 167), ("zh-Hant", 167), ("zh-HK", 168),
    ("zh-Hant-HK", 168), ("yue", 168), ("ja", 169), ("ko", 170), ("en", 166), ("", 166),
])
def test_han_regional_face_by_language(fonts: Any, language: str, face: int) -> None:
    assert lay(fonts, "直骨", language=language).faces == (face,)


def test_han_language_inferred_from_kana_and_hangul(fonts: Any) -> None:
    assert lay(fonts, "直す", language="en").faces == (169,)
    assert lay(fonts, "直 한국", language="en").faces == (170,)


def test_regional_glyphs_differ(fonts: Any) -> None:
    ctx = FontContext.for_fontset(fonts)
    sc = lay(fonts, "骨", language="zh-Hans").glyphs[0]
    jp = lay(fonts, "骨", language="ja").glyphs[0]
    assert ctx.glyph(sc.face_id, 24, sc.glyph_id).bitmap != ctx.glyph(jp.face_id, 24, jp.glyph_id).bitmap


def test_wrapping_reshapes_each_line(fonts: Any) -> None:
    """A joining word broken across lines takes final/initial forms at the break (line-boundary reshaping)."""
    shapes = per_cluster(lay(fonts, "ببب"))
    initial, medial, final = shapes[0], shapes[1], shapes[2]
    block = lay(fonts, "ب" * 30, width=120)
    assert len(block.lines) >= 2
    glyphs = per_cluster(block)
    for line in block.lines:
        assert glyphs[line.start] == initial
        assert glyphs[line.end - 1] == final
    assert glyphs[block.lines[0].end - 2] == medial
    # Unbroken, the same letter is medial: the break changed its shape.
    assert per_cluster(lay(fonts, "ب" * 30))[block.lines[0].end - 1] == medial


def test_trailing_spaces_hang_and_lines_fit(fonts: Any) -> None:
    assert lay(fonts, "abc    ").lines[0].width == lay(fonts, "abc").lines[0].width
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore"
    for width in (60, 120, 200, 333):
        block = lay(fonts, text, width=width, size_px=16)
        for line in block.lines:
            assert line.width <= width or line.end - line.start == 1
            assert not block.text[line.start].isspace()
            assert not block.text[line.end - 1].isspace()
        for a, b in zip(block.lines, block.lines[1:], strict=False):
            assert b.top >= a.bottom


def test_long_word_breaks_between_clusters(fonts: Any) -> None:
    block = lay(fonts, "Supercalifragilisticexpialidocious", width=100)
    assert len(block.lines) > 1 and all(ln.width <= 100 for ln in block.lines)


# --------------------------------------------------------------------------- marks and complex scripts


def test_vietnamese_stacked_marks_and_normalisation(fonts: Any) -> None:
    nfc = "Tiếng Việt: Người ta học mãi, Ở Ữ ặ"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    a, b = lay(fonts, nfc, language="vi"), lay(fonts, nfd, language="vi")
    assert a.glyphs == b.glyphs and a.lines == b.lines and a.notdef == 0

    ctx = FontContext.for_fontset(fonts)
    block = lay(fonts, "x̂́")  # no precomposed glyph: HarfBuzz positions both marks
    assert len(block.glyphs) == 3 and {g.cluster for g in block.glyphs} == {0}
    tops = []
    for g in block.glyphs:
        ink = ctx.ink(g.face_id, 24, g.glyph_id)
        tops.append((g.y + ink[1], g.y + ink[3]))
    (base_top, _), (circ_top, circ_bottom), (acute_top, acute_bottom) = tops
    assert circ_bottom <= base_top and acute_bottom <= circ_top, tops


@pytest.mark.parametrize(("text", "face", "glyphs"), [
    ("क्ष", 29, 1),       # Devanagari KSSA conjunct
    ("स्त्र", 29, 2),      # three-consonant conjunct
    ("ক্ষ", 12, 1),       # Bengali KSSA
    ("ஸ்ரீ", 145, 1),      # Tamil SHRII ligature
    ("க்ஷ", 145, 1),      # Tamil KSSA
])
def test_indic_conjuncts(fonts: Any, text: str, face: int, glyphs: int) -> None:
    block = lay(fonts, text)
    assert block.faces == (face,) and len(block.glyphs) == glyphs < len(text) and block.notdef == 0


def test_indic_reordering(fonts: Any) -> None:
    ka = lay(fonts, "क").glyphs[0].glyph_id
    ki = sorted(lay(fonts, "कि").glyphs, key=lambda g: g.x)
    assert ki[0].glyph_id != ka and ki[1].glyph_id == ka  # the i-matra is drawn before the consonant
    tamil_ka = lay(fonts, "க").glyphs[0].glyph_id
    ko = sorted(lay(fonts, "கொ").glyphs, key=lambda g: g.x)
    assert len(ko) == 3 and ko[1].glyph_id == tamil_ka  # two-part vowel around the consonant


def test_emoji_presentation_selects_the_emoji_face(fonts: Any) -> None:
    assert lay(fonts, "😀").faces == (171,)
    assert lay(fonts, "👍🏽").faces == (171,) and len(lay(fonts, "👍🏽").glyphs) == 1
    assert 171 not in lay(fonts, "☺").faces
    assert lay(fonts, "☺️").faces == (171,)
    mixed = lay(fonts, "Done 🎉 ok")
    assert set(mixed.faces) == {1, 171} and mixed.notdef == 0


def test_unsupported_characters_are_reported(fonts: Any) -> None:
    block = lay(fonts, "A\U00020000B\U0010FFFD")  # CJK Extension B and a private-use character
    assert block.unsupported == ("\U00020000", "\U0010FFFD") and block.notdef == 2
    assert block.unsupported_clusters == ("\U00020000", "\U0010FFFD")
    assert lay(fonts, "Hello").unsupported == ()
    # Each mark exists somewhere, but no face maps the whole cluster: recorded, drawn with the base's face.
    partial = lay(fonts, "a⃝᪰")
    assert partial.unsupported == () and partial.unsupported_clusters == ("a⃝᪰",)
    # Controls never reach HarfBuzz (a tab is a space).
    assert lay(fonts, "a\tb\x00c\x85").notdef == 0 and lay(fonts, "a\tb").text == "a b"


# --------------------------------------------------------------------------- ellipsis, alignment, rounding


def test_ellipsis_ltr_and_rtl(fonts: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog again and again and again"
    block = lay(fonts, text, width=200, max_lines=2)
    assert block.truncated and len(block.lines) == 2 and block.lines[1].ellipsis
    last = [g for g in block.glyphs if g.line == 1]
    assert max(last, key=lambda g: g.x).cluster == -1  # the ellipsis ends the LTR line
    assert all(ln.width <= 200 for ln in block.lines)

    arabic = "مرحبا بالعالم هذا نص عربي طويل للاختبار مع الكثير من الكلمات"
    block = lay(fonts, arabic, width=200, max_lines=2)
    last = [g for g in block.glyphs if g.line == 1]
    assert block.lines[1].ellipsis and min(last, key=lambda g: g.x).cluster == -1  # at the left end in RTL
    assert not lay(fonts, "short", width=200, max_lines=1).truncated


def test_max_lines_across_paragraphs(fonts: Any) -> None:
    block = lay(fonts, "first\nsecond\nthird", width=300, max_lines=2)
    assert len(block.lines) == 2 and block.lines[1].ellipsis and block.truncated
    plain = lay(fonts, "first\nsecond\nthird", width=300)
    assert len(plain.lines) == 3 and not plain.truncated
    cut = lay(fonts, "one two three four five six seven", width=80, max_lines=1, ellipsis=False)
    assert cut.truncated and not cut.lines[0].ellipsis


def test_alignment_respects_direction(fonts: Any) -> None:
    for text, rtl in (("abc", False), ("אבג", True)):
        start = lay(fonts, text, width=200, align="start").lines[0]
        end = lay(fonts, text, width=200, align="end").lines[0]
        center = lay(fonts, text, width=200, align="center").lines[0]
        right, left = (start, end) if rtl else (end, start)
        assert left.x == 0 and right.x + right.width == 200
        assert abs(center.x - (200 - center.x - center.width)) <= 1
        assert lay(fonts, text, width=200, align="left").lines[0].x == 0


def test_positions_accumulate_in_26_6(fonts: Any) -> None:
    ctx = FontContext.for_fontset(fonts)
    buf = hb.Buffer()
    buf.add_str("i")
    buf.guess_segment_properties()
    hb.shape(ctx.hb_font(1, 24), buf, {})
    advance = buf.glyph_positions[0].x_advance
    assert advance % 64, "the test needs a fractional advance"
    block = lay(fonts, "i" * 60, width=5000)
    assert [g.x for g in block.glyphs] == [(k * advance + 32) >> 6 for k in range(60)]


def test_line_boxes_grow_to_the_ink(fonts: Any) -> None:
    base = lay(fonts, "Hxg", size_px=24).lines[0]
    strike = fonts.strike(1, 24)
    assert (base.baseline - base.top, base.bottom - base.baseline) == (strike.ascent, strike.descent)
    tall = lay(fonts, "ཀྵྐྵྨྱ", size_px=24).lines[0]  # a deep Tibetan stack
    assert tall.height > base.height
    block = lay(fonts, "ဣ္ဇ ကြွ ပြော ဉ္ဇ " * 3, width=150, size_px=24)
    for a, b in zip(block.lines, block.lines[1:], strict=False):
        assert b.top >= a.bottom


def test_layout_is_deterministic(fonts: Any) -> None:
    text = "Tiếng Việt, مرحبا 123, שלום, สวัสดีครับ, नमस्ते, 你好 😀 — wrapped over lines"
    first = lay(fonts, text, width=150, size_px=16, max_lines=4)
    engine_module._cache.clear()
    second = lay(fonts, text, width=150, size_px=16, max_lines=4)
    assert first is not second and first == second
    assert first.commands(3, 4, 1) == second.commands(3, 4, 1)


def test_dev_pack_has_no_32px_and_no_cjk(dev_fonts: Any) -> None:
    ctx = FontContext.for_fontset(dev_fonts)
    assert ctx.text_sizes() == (16, 24)
    block = layout_text("Hello مرحبا שלום สวัสดี नमस्ते", dev_fonts, width=400, size_px=24)
    assert block.unsupported == () and block.notdef == 0 and set(block.faces) == {1, 5, 29, 47, 151}
    cjk = layout_text("日本 😀", dev_fonts, width=400, size_px=16)
    assert cjk.unsupported == ("日", "本", "😀")
