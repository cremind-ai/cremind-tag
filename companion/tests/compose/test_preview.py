"""Previews: reference-renderer PNGs, true 1-bit / 3-colour, logical orientation, 64 KiB limit."""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from PIL import Image

from cremind_tag.compose.api import ActiveCard, ScreenSettings, TagPanel
from cremind_tag.compose.preview import MAX_PREVIEW_BYTES, PreviewTooLarge, preview_png, render_image
from cremind_tag.compose.samples import example_cards
from cremind_tag.compose.screen import compose_screen
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.layout.fonts import FontContext
from cremind_tag.render.reference import Panel, render_frame

pytestmark = pytest.mark.fonts

BW = TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 0, "Desk")
BWR_PORTRAIT = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 1, "Desk")
BW_INVERTED_FLAGS = TagPanel(0x1A2B3C4D, 400, 300, 2, 0, 3, "Desk")


def _png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def test_bw_preview_is_true_one_bit(fonts: FontSet, now: datetime) -> None:
    screen = compose_screen(BW, example_cards(now), fonts, ScreenSettings(), now)
    img = _png(preview_png(screen, BW, fonts))
    assert img.mode == "1" and img.size == (400, 300)
    frame = render_frame(screen.layout, Panel(400, 300, 1, 1), FontContext.for_fontset(fonts).pack)
    ones = sum(bin(b).count("1") for b in frame.planes[0])  # plane_flags bit0: 1 = white
    assert img.histogram()[255] == ones and img.histogram()[0] == 400 * 300 - ones


def test_bwr_preview_shows_red_in_logical_orientation(fonts: FontSet, now: datetime) -> None:
    screen = compose_screen(BWR_PORTRAIT, example_cards(now), fonts, ScreenSettings(), now)
    img = _png(preview_png(screen, BWR_PORTRAIT, fonts))
    assert img.mode == "P" and img.size == (300, 400)  # rotated back to what a reader sees
    counts = img.histogram()[:3]
    assert counts[0] > counts[1] > 0 and counts[2] > 0  # white, black, red (the needs-input title)
    assert img.getpalette()[6:9] == [220, 0, 0]
    native = render_image(screen.layout, fonts, panel=BWR_PORTRAIT, orientation="native")
    assert native.size == (400, 300) and native.transpose(Image.Transpose.ROTATE_90).tobytes() == img.tobytes()


def test_plane_flag_polarity_does_not_change_the_picture(fonts: FontSet, now: datetime) -> None:
    cards = example_cards(now)
    normal = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 3, "Desk")
    a = compose_screen(normal, cards, fonts, ScreenSettings(), now)
    b = compose_screen(BW_INVERTED_FLAGS, cards, fonts, ScreenSettings(), now)
    assert a.layout == b.layout  # plane polarity is a panel property, not a layout one
    img_a = render_image(a.layout, fonts, panel=normal)
    img_b = render_image(b.layout, fonts, panel=BW_INVERTED_FLAGS)
    assert img_a.tobytes() == img_b.tobytes()


def test_worst_case_previews_stay_under_64_kib(fonts: FontSet, now: datetime) -> None:
    text = ("il1| 😀 ".join(["Tiếng Việt", "مرحبا", "שלום", "สวัสดี", "नमस्ते", "你好"]) * 30)[:400]
    cards = [ActiveCard(i, "excerpt", 50, now, {"kind": "excerpt", "title": text, "body": text * 3,
                                                  "link": "https://x.example/#/a/c/1"}) for i in range(1, 12)]
    for panel in (BW, BWR_PORTRAIT):
        screen = compose_screen(panel, cards, fonts, ScreenSettings(True, True), now)
        for scale in (1, 2, 4):
            data = preview_png(screen, panel, fonts, scale=scale)
            assert len(data) <= MAX_PREVIEW_BYTES
    with pytest.raises(PreviewTooLarge):
        preview_png(screen, BWR_PORTRAIT, fonts, limit=100)
