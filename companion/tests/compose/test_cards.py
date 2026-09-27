"""Card normalisation policy: visibility, icons, bodies, colour, progress, QR links, order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cremind_tag.compose.api import ActiveCard, ScreenSettings
from cremind_tag.compose.cards import card_view, ordered_views, qr_link
from cremind_tag.compose.timefmt import card_stamp, format_time, parse_timestamp
from cremind_tag.protocol.ids import LAYOUT_QR_MAX_TEXT, Icon

NOW = datetime(2026, 9, 27, 7, 5, tzinfo=UTC)


def active(did: int = 1, kind: str = "notification", prio: int = 40, minutes: int = 0, **card: object) -> ActiveCard:
    body = {"v": 1, "kind": kind, "title": "Title", "lang": "en", **card}
    return ActiveCard(did, kind, prio, NOW - timedelta(minutes=minutes), body)


@pytest.mark.parametrize("link", [
    "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c",
    "https://x.example/events",
    "https://x.example:8443/#/bob/channels",
])
def test_qr_accepts_short_token_free_https(link: str) -> None:
    assert qr_link(link) == link.encode()


@pytest.mark.parametrize("link", [
    "http://x.example/a",                                  # not https
    "https://x.example/a?next=1",                          # query
    "https://x.example/a?",                                # empty query
    "https://user:pw@x.example/a",                         # user info
    "https://x.example/#/a?token=1",                       # parameters in the fragment
    "https://x.example/a;jsessionid=1",                    # path parameters
    "https://x.example/reset/k3JH8sd9f7G6h5J4k3L2m1N0p9Q8",  # token-looking path segment
    "https://x.example/api_key/abc",                       # credential word
    "https://x.example/" + "a" * LAYOUT_QR_MAX_TEXT,       # too long
    "https://x.example/ä",                                 # not printable ASCII
    "https://x.example/a b",
    "https://x.example:99999/",                            # bad port
    "", None, 42,
])
def test_qr_rejects_everything_else(link: object) -> None:
    assert qr_link(link) is None


def test_hidden_kinds_icons_titles() -> None:
    settings = ScreenSettings()
    assert card_view(active(kind="resolved"), settings) is None
    assert card_view(active(kind="clear"), settings) is None
    assert card_view(active(icon="approval"), settings).icon == Icon.APPROVAL
    assert card_view(active(kind="calendar", icon="nope"), settings).icon == Icon.EVENT
    assert card_view(active(kind="weird", severity="error"), settings).icon == Icon.ERROR
    assert card_view(active(title="**Hello** <b>there</b>\n\nfriend"), settings).title == "Hello there friend"
    assert card_view(active(kind="task_outcome", title=""), settings).title == "Task outcome"


def test_body_policy() -> None:
    off, on = ScreenSettings(show_excerpts=False), ScreenSettings(show_excerpts=True)
    excerpt = active(kind="excerpt", body="Some *excerpt*\ntext")
    assert card_view(excerpt, off).body is None
    assert card_view(excerpt, on).body == "Some excerpt\ntext"
    assert card_view(active(kind="needs_input", body="context"), off).body is None
    assert card_view(active(kind="needs_input", body="context"), on).body == "context"
    assert card_view(active(kind="future_kind", body="x"), on).body is None
    assert card_view(active(kind="pinned_note", body="note"), off).body == "note"


def test_red_progress_and_link() -> None:
    settings = ScreenSettings(qr_links=True)
    assert card_view(active(kind="needs_input"), settings).red
    assert card_view(active(severity="error"), settings).red
    assert not card_view(active(severity="warning"), settings).red
    assert card_view(active(kind="progress", progress={"done": 3, "total": 10}), settings).progress == (3, 10)
    assert card_view(active(kind="progress", progress={"done": 12, "total": 10}), settings).progress == (10, 10)
    for bad in (None, {"done": 1}, {"done": 1, "total": 0}, {"done": -1, "total": 5}, {"done": "1", "total": 5},
                {"done": True, "total": 5}):
        assert card_view(active(kind="progress", progress=bad), settings).progress is None
    assert card_view(active(kind="notification", progress={"done": 3, "total": 10}), settings).progress is None
    link = "https://x.example/events"
    assert card_view(active(link=link), settings).link == link.encode()
    assert card_view(active(link=link), ScreenSettings(qr_links=False)).link is None


def test_order_priority_then_newest() -> None:
    cards = [active(1, prio=40, minutes=5), active(2, prio=90, minutes=60), active(3, prio=40, minutes=1),
             active(4, prio=40, minutes=1), active(5, kind="resolved", prio=100)]
    assert [v.delivery_id for v in ordered_views(cards, ScreenSettings())] == [2, 4, 3, 1]


def test_time_formatting() -> None:
    assert format_time(NOW, "en", "Asia/Ho_Chi_Minh") == "2:05 PM"
    assert format_time(NOW, "vi", "Asia/Ho_Chi_Minh") == "14:05"
    assert format_time(NOW, "de", "Unknown/Zone") == "07:05"  # unknown zone -> UTC
    assert card_stamp(NOW - timedelta(hours=1), NOW, "en", "UTC") == "6:05 AM"
    assert card_stamp(NOW - timedelta(days=1), NOW, "en", "UTC") == "Sep 26"
    assert card_stamp(NOW - timedelta(days=1), NOW, "en", "UTC", long=True) == "Sep 26, 7:05 AM"
    # 23:05 UTC on Sep 26 is 06:05 on Sep 27 in Ho Chi Minh City: the same local day as NOW (14:05).
    assert card_stamp(NOW - timedelta(hours=8), NOW, "vi", "Asia/Ho_Chi_Minh") == "6:05"
    assert parse_timestamp("2026-09-27T10:00:00Z") == datetime(2026, 9, 27, 10, tzinfo=UTC)
    assert parse_timestamp("nonsense") is None and parse_timestamp(None) is None
