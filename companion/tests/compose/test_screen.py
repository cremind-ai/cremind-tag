"""Screen composition with the real pack: model, panels, limits, degradation, determinism, speed."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from cremind_tag.compose.api import ActiveCard, ComposedScreen, ScreenSettings, TagPanel
from cremind_tag.compose.samples import example_cards
from cremind_tag.compose.screen import MAX_BYTES, PLANS, compose_blank, compose_identify, compose_screen
from cremind_tag.layout import command_glyphs
from cremind_tag.layout import engine as engine_module
from cremind_tag.protocol.ids import (
    LAYOUT_HARD_MAX,
    LAYOUT_MAX_COMMANDS,
    LAYOUT_MAX_GLYPHS,
    LAYOUT_SERIAL_MAX,
    Color,
)
from cremind_tag.protocol.layout import (
    Glyphs,
    Icon,
    Progress,
    Qr,
    check_panel,
    check_strikes,
    decode_layout,
)

pytestmark = pytest.mark.fonts

LANDSCAPE_BW = TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 0, "Desk")
LANDSCAPE_BWR = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "Desk")
PORTRAIT_BWR = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 1, "Kitchen")
PANELS = [
    LANDSCAPE_BW, LANDSCAPE_BWR, PORTRAIT_BWR,
    TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 2, "Upside down"),
    TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 3, "Portrait 3"),
    TagPanel(0x0BADCAFE, 296, 128, 1, 1, 0, "Small"),
    TagPanel(0x0BADCAFE, 250, 122, 2, 3, 1, "Tiny portrait"),
]


def check(screen: ComposedScreen, panel: TagPanel, fonts: Any) -> Any:
    assert len(screen.layout) <= MAX_BYTES == min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX) == 4000
    layout = decode_layout(screen.layout)
    check_strikes(layout, fonts.has_strike)
    check_panel(layout, panel.width, panel.height)
    assert len(layout.commands) <= LAYOUT_MAX_COMMANDS
    assert sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs)) <= LAYOUT_MAX_GLYPHS
    for cmd in layout.commands:
        if isinstance(cmd, Glyphs):
            for _gid, x, y in command_glyphs(cmd):
                assert -64 <= x < layout.width + 64 and -64 <= y < layout.height + 64
    assert screen.pending_count == len(screen.pending_delivery_ids)
    assert not set(screen.delivery_ids) & set(screen.pending_delivery_ids)
    return layout


def card(did: int, kind: str, title: str, prio: int, minutes: int, now: datetime, **extra: Any) -> ActiveCard:
    ts = now - timedelta(minutes=minutes)
    return ActiveCard(did, kind, prio, ts, {"v": 1, "kind": kind, "title": title, "lang": extra.pop("lang", "en"),
                                            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra})


# --------------------------------------------------------------------------- model


@pytest.mark.parametrize("panel", PANELS, ids=lambda p: f"{p.width}x{p.height}-r{p.rotation}-p{p.planes}")
def test_screens_validate_on_every_panel(fonts: Any, now: datetime, panel: TagPanel) -> None:
    for settings in (ScreenSettings(), ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "vi"),
                     ScreenSettings(True, True, "Asia/Riyadh", "ar")):
        screen = compose_screen(panel, example_cards(now), fonts, settings, now)
        layout = check(screen, panel, fonts)
        expect = (panel.width, panel.height) if panel.rotation % 2 == 0 else (panel.height, panel.width)
        assert (layout.width, layout.height, layout.rotation) == (*expect, panel.rotation)
        assert screen.delivery_ids[0] == 501  # highest priority first


def test_delivery_ids_shown_and_pending(fonts: Any, now: datetime) -> None:
    cards = example_cards(now)
    screen = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(), now)
    # needs_input 90, health 75, then priority-50 cards newest first; four shown, the rest counted.
    assert screen.delivery_ids == (501, 504, 503, 507)
    assert screen.pending_delivery_ids == (505, 506, 502) and screen.pending_count == 3
    assert set(screen.delivery_ids) | set(screen.pending_delivery_ids) == {c.delivery_id for c in cards}
    resolved = [*cards, card(900, "resolved", "Answered", 90, 0, now)]
    assert compose_screen(LANDSCAPE_BW, resolved, fonts, ScreenSettings(), now).delivery_ids == screen.delivery_ids


def test_single_card_and_empty_screen(fonts: Any, now: datetime) -> None:
    one = compose_screen(LANDSCAPE_BW, example_cards(now)[:1], fonts, ScreenSettings(), now)
    assert one.delivery_ids == (501,) and one.pending_count == 0
    empty = compose_screen(LANDSCAPE_BW, [], fonts, ScreenSettings(), now)
    layout = check(empty, LANDSCAPE_BW, fonts)
    assert empty.delivery_ids == () and any(isinstance(c, Icon) for c in layout.commands)


def test_red_only_on_two_plane_panels(fonts: Any, now: datetime) -> None:
    cards = example_cards(now)
    bwr = decode_layout(compose_screen(LANDSCAPE_BWR, cards, fonts, ScreenSettings(), now).layout)
    bw = decode_layout(compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(), now).layout)
    assert bwr.flags & 1 and any(getattr(c, "color", 0) == Color.RED for c in bwr.commands)
    assert not bw.flags & 1 and all(getattr(c, "color", 0) != Color.RED for c in bw.commands)
    calm = [card(1, "notification", "All good", 40, 1, now)]
    calm_layout = decode_layout(compose_screen(LANDSCAPE_BWR, calm, fonts, ScreenSettings(), now).layout)
    assert not calm_layout.flags & 1


def _glyph_total(screen: ComposedScreen) -> int:
    return sum(len(c.glyphs) for c in decode_layout(screen.layout).commands if isinstance(c, Glyphs))


def test_body_only_with_excerpts(fonts: Any, now: datetime) -> None:
    cards = [card(1, "excerpt", "Weekly summary", 50, 1, now, body="Shipped the companion and fixed bugs.")]
    off = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(show_excerpts=False), now)
    on = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(show_excerpts=True), now)
    assert _glyph_total(on) > _glyph_total(off) + 25


def test_qr_only_for_valid_links_when_enabled(fonts: Any, now: datetime) -> None:
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    cards = [card(1, "needs_input", "Approve?", 90, 1, now, link=link)]

    def qrs(settings: ScreenSettings, cs: list[ActiveCard]) -> list[Qr]:
        return [c for c in decode_layout(compose_screen(LANDSCAPE_BW, cs, fonts, settings, now).layout).commands
                if isinstance(c, Qr)]

    found = qrs(ScreenSettings(qr_links=True), cards)
    assert len(found) == 1 and found[0].text == link.encode()
    assert qrs(ScreenSettings(qr_links=False), cards) == []
    bad = [card(1, "needs_input", "Approve?", 90, 1, now, link=link + "?token=abc")]
    assert qrs(ScreenSettings(qr_links=True), bad) == []


def test_progress_bar_needs_real_counts(fonts: Any, now: datetime) -> None:
    def bars(progress: Any) -> list[Progress]:
        cs = [card(1, "progress", "Backup", 30, 1, now, progress=progress)]
        layout = decode_layout(compose_screen(LANDSCAPE_BW, cs, fonts, ScreenSettings(), now).layout)
        return [c for c in layout.commands if isinstance(c, Progress)]

    (bar,) = bars({"done": 7, "total": 12})
    assert (bar.value, bar.max) == (7, 12) and bar.w > 100
    (big,) = bars({"done": 50_000, "total": 200_000})
    assert big.max <= 0xFFFF and abs(big.value / big.max - 0.25) < 0.01
    assert bars(None) == [] and bars({"done": 1, "total": 0}) == []


def test_identify_and_blank(fonts: Any) -> None:
    for panel in (LANDSCAPE_BW, PORTRAIT_BWR, TagPanel(0x1A2B3C4D, 296, 128, 1, 1, 0, "")):
        ident = compose_identify(panel, fonts)
        layout = check(ident, panel, fonts)
        assert ident.delivery_ids == () and any(isinstance(c, Glyphs) and c.size_px == 32 for c in layout.commands)
        blank = compose_blank(panel)
        layout = decode_layout(blank.layout)
        assert layout.commands == () and layout.background == Color.WHITE
        check_panel(layout, panel.width, panel.height)
    long_name = TagPanel(1, 400, 300, 1, 1, 0, "Phòng họp " * 40)
    check(compose_identify(long_name, fonts, "CAFE0001"), long_name, fonts)


def test_dev_pack_without_32px(dev_fonts: Any, now: datetime) -> None:
    cards = [c for c in example_cards(now) if c.delivery_id in (501, 506)]
    for panel in (LANDSCAPE_BWR, PORTRAIT_BWR):
        screen = compose_screen(panel, cards, dev_fonts, ScreenSettings(True, True), now)
        layout = check(screen, panel, dev_fonts)
        assert all(c.size_px != 32 for c in layout.commands if isinstance(c, Glyphs))
        assert screen.delivery_ids == (501, 506)
        assert set(screen.unsupported_chars) >= {"今", "日"}  # shown texts only; the dev pack has no CJK
    check(compose_identify(LANDSCAPE_BW, dev_fonts), LANDSCAPE_BW, dev_fonts)


# --------------------------------------------------------------------------- worst cases


LONG = {
    "latin": "Approve the deployment of release candidate twelve to the production cluster tonight? ",
    "vietnamese": "Người dùng đã yêu cầu phê duyệt việc triển khai phiên bản mới lên máy chủ sản xuất. ",
    "arabic": "هل توافق على نشر الإصدار الجديد على خوادم الإنتاج الليلة؟ ",
    "hebrew": "האם לאשר את הפריסה של הגרסה החדשה לשרתי הייצור הלילה? ",
    "thai": "คุณต้องการอนุมัติการติดตั้งเวอร์ชันใหม่บนเซิร์ฟเวอร์คืนนี้หรือไม่ ",
    "devanagari": "क्या आप आज रात उत्पादन सर्वर पर नए संस्करण की तैनाती को स्वीकृति देते हैं? ",
    "tamil": "இன்றிரவு புதிய பதிப்பை உற்பத்தி சேவையகங்களில் நிறுவ ஒப்புதல் தருகிறீர்களா? ",
    "chinese": "您是否批准今晚将新版本部署到生产服务器？请尽快回复。",
    "japanese": "今夜、新しいバージョンを本番サーバーにデプロイすることを承認しますか？",
    "korean": "오늘 밤 새 버전을 프로덕션 서버에 배포하는 것을 승인하시겠습니까? ",
    "emoji": "🎉👍🏽❤️😀🚀🔥✅📦🧪🛠️ ",
    "narrow": "il1.,:;|!ij'" * 3,
    "alternating": "aب1אกक" * 4,
}


LARGE = TagPanel(0x1A2B3C4D, 800, 480, 2, 3, 0, "Wall")


@pytest.mark.parametrize("script", sorted(LONG))
@pytest.mark.parametrize("panel", [LANDSCAPE_BWR, PORTRAIT_BWR, PANELS[5], LARGE],
                         ids=["landscape", "portrait", "small", "large"])
def test_worst_case_screens_stay_within_limits(fonts: Any, now: datetime, script: str, panel: TagPanel) -> None:
    text = (LONG[script] * 40)[:400]
    body = (LONG[script] * 60)[:1200]
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    cards = [card(i, "excerpt" if i % 2 else "needs_input", text, 50 + (i % 5), i, now, body=body, link=link,
                  severity="error")
             for i in range(1, 21)]
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "ar" if script == "arabic" else "en")
    screen = compose_screen(panel, cards, fonts, settings, now)
    check(screen, panel, fonts)
    assert len(screen.delivery_ids) >= 1 and len(screen.delivery_ids) + screen.pending_count == 20
    assert screen.unsupported_chars == ()


@pytest.mark.parametrize(("limit", "value"), [("LAYOUT_MAX_GLYPHS", 150), ("MAX_BYTES", 700),
                                              ("LAYOUT_MAX_COMMANDS", 14)])
def test_degradation_ladder_under_tight_limits(fonts: Any, now: datetime, monkeypatch: pytest.MonkeyPatch,
                                               limit: str, value: int) -> None:
    from cremind_tag.compose import screen as screen_module

    cards = example_cards(now)
    settings = ScreenSettings(True, True)
    full = compose_screen(LANDSCAPE_BW, cards, fonts, settings, now)
    monkeypatch.setattr(screen_module, limit, value)
    tight = compose_screen(LANDSCAPE_BW, cards, fonts, settings, now)
    layout = check(tight, LANDSCAPE_BW, fonts)
    glyphs = sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs))
    used = {"LAYOUT_MAX_GLYPHS": glyphs, "MAX_BYTES": len(tight.layout), "LAYOUT_MAX_COMMANDS": len(layout.commands)}
    assert used[limit] <= value
    assert len(tight.delivery_ids) < len(full.delivery_ids)  # list rows went first
    assert tight.delivery_ids == full.delivery_ids[:len(tight.delivery_ids)]
    assert tight.pending_count == len(cards) - len(tight.delivery_ids)
    assert compose_screen(LANDSCAPE_BW, cards, fonts, settings, now) == tight  # deterministic


def test_plans_degrade_deterministically(fonts: Any, now: datetime) -> None:
    text = ("aب1אกक" * 80)[:400]  # a strike change on every character: one GLYPHS command per glyph
    cards = [card(i, "excerpt", text, 50, i, now, body=text * 3) for i in range(1, 6)]
    a = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(True), now)
    engine_module._cache.clear()
    b = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(True), now)
    assert a == b
    layout = check(a, LANDSCAPE_BW, fonts)
    assert len(layout.commands) <= LAYOUT_MAX_COMMANDS
    assert PLANS[0].list_rows == 3 and PLANS[-1].list_rows == 0


def test_same_input_same_bytes(fonts: Any, now: datetime) -> None:
    cards = example_cards(now)
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "vi")
    first = compose_screen(PORTRAIT_BWR, cards, fonts, settings, now)
    engine_module._cache.clear()
    assert compose_screen(PORTRAIT_BWR, list(reversed(cards)), fonts, settings, now) == first
    later = compose_screen(PORTRAIT_BWR, cards, fonts, settings, now + timedelta(minutes=1))
    assert later.layout != first.layout  # the clock line changed


def test_typical_screen_under_200ms(fonts: Any, now: datetime) -> None:
    cards = example_cards(now)
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "en")
    compose_screen(LANDSCAPE_BWR, cards, fonts, settings, now)  # warm-up: pack, cmaps, HarfBuzz faces
    timings = []
    for minute in range(1, 6):
        engine_module._cache.clear()  # no layout cache: every text is shaped again
        started = time.perf_counter()
        compose_screen(LANDSCAPE_BWR, cards, fonts, settings, now + timedelta(minutes=minute))
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.2, timings
