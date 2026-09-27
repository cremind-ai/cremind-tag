"""The companion's card validator mirrors Cremind's sanitiser and refuses, never rewrites."""

from __future__ import annotations

import pytest

from cremind_tag.daemon.validator import card_problem, text_problem

REFUSED = [
    ("Your verification code is 482913", "one-time code"),
    ("482 913 is your login code", "one-time code"),
    ("OTP: 1234", "one-time code"),
    ("Mã OTP 556677", "one-time code"),
    ("Authorization: Bearer abcdEFGH12345678", "credential"),
    ("key sk-proj-abcdefghijklmnop1234", "credential"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz123456", "credential"),
    ("use tagc_abcdefghijklmnopqrstuvwxyz.secretpart", "credential"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk", "credential"),
    ("password=hunter22", "credential"),
    ("api_key: 'zzz'", "credential"),
    ("digest 0123456789abcdef0123456789abcdef", "credential-like opaque run"),
    ("blob QWxhZGRpbjpvcGVuIHNlc2FtZQ9xYz1234", "credential-like opaque run"),
    ("\x1b[31mred\x1b[0m", "tool or terminal output"),
    ("```\nls\n```", "tool or terminal output"),
    ("Traceback (most recent call last):\n  File x", "tool or terminal output"),
    ("$ rm -rf build", "tool or terminal output"),
    ("root@host:~# reboot", "tool or terminal output"),
    ("PS C:\\Users\\me> dir", "tool or terminal output"),
    ("C:\\work> make", "tool or terminal output"),
    (">>> import os", "tool or terminal output"),
    ("2026-09-27 10:00:00 INFO a\n2026-09-27 10:00:01 ERROR b\n2026-09-27 10:00:02 WARN c",
     "tool or terminal output"),
]

ALLOWED = [
    "Approve deployment?",
    "Reply ready: Weekly plan",
    "Build 1234 finished",  # a number without a code word
    "Your verification code is ••••",  # Cremind's own masking
    "password=[redacted]",
    "Bearer [redacted]",
    "Meeting at 10:30 in room 4",
    "Visit https://cremind.example.org/#/alice/c/3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "Straße, 東京, مرحبا — all fine",
    "One log line 2026-09-27 10:00:00 INFO only",
    "internationalization_and_localization_everywhere",
]


@pytest.mark.parametrize(("text", "reason"), REFUSED)
def test_refused(text: str, reason: str) -> None:
    assert text_problem(text) == reason


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text: str) -> None:
    assert text_problem(text) is None


def test_card_fields() -> None:
    assert card_problem({"title": "Fine", "body": "PIN 4321 for the door"}) == "one-time code in body"
    assert card_problem({"title": 5}) == "title is not text"
    assert card_problem({"title": "Fine", "body": None, "link": "https://x/?token=abc"}) is None  # links: compose
    assert card_problem(None) is None


@pytest.mark.parametrize(("body", "reason"), [
    ("Your code:\n482913", "one-time code"),
    ("Your code is &#52;&#56;&#50;&#57;&#49;&#51;", "one-time code"),
    ("Login code: **482**913", "one-time code"),
    ("code:" + " " * 30 + "482913", "one-time code"),
    ("Authorization: Bearer&nbsp;abcdEFGH12345678", "credential"),
    ("<b>PIN</b> <i>4321</i>", "one-time code"),
    ("key: hunter22", "credential"),
])
def test_what_the_tag_would_show_is_checked(body: str, reason: str) -> None:
    """Review regression: the raw text passed while its plain-text rendering showed a code or token."""
    assert card_problem({"title": "Fine", "body": body}) == f"{reason} in body"
    assert card_problem({"title": body}) == f"{reason} in title"


def test_rendered_forms() -> None:
    from cremind_tag.daemon.validator import displayed_forms

    assert displayed_forms("plain") == ["plain"]
    assert "Your code is 482913" in displayed_forms("Your code is &#52;&#56;&#50;&#57;&#49;&#51;")


def test_time_zones_are_normalised_for_the_composer() -> None:
    from cremind_tag.daemon.screens import iana_timezone

    assert iana_timezone("Asia/Ho_Chi_Minh") == "Asia/Ho_Chi_Minh"
    assert iana_timezone("SE Asia Standard Time") == "Asia/Bangkok"  # a Windows id
    assert iana_timezone("+07:00") == "GMT+07:00"
    assert iana_timezone("UTC-5") == "GMT-05:00"
    assert iana_timezone("") == iana_timezone(None) == "UTC"
    assert iana_timezone("Mars/Olympus") == "UTC"
