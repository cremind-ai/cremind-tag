"""Active cards -> what a screen shows (docs/layout.md "Screen model").

The card JSON is the connector's (docs/connector-api.md "Job shape"). This
module decides, per card, the icon, the plain-text title and body, the
language, the time stamp, a progress fraction and a QR link, and the display
order. Nothing here draws.

Policy:

- ``resolved`` and ``clear`` cards are instructions, never shown;
- the icon is ``card.icon`` when it names a built-in icon, else the kind's
  default;
- the body is shown only when the profile enables excerpts
  (``show_excerpts``) and the kind is in `BODY_KINDS`; a ``pinned_note`` body
  is text the owner wrote for this tag and is always shown;
- the title is red on two-plane panels for ``needs_input`` cards and
  ``error`` severity;
- progress needs real counts: integers ``done >= 0`` and ``total > 0``;
- a QR link must be a short, token-free ``https`` URL (`qr_link`).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from cremind_tag.compose.api import ActiveCard, ScreenSettings
from cremind_tag.compose.timefmt import parse_timestamp
from cremind_tag.layout.plaintext import plain_text
from cremind_tag.layout.unicode import normalize_language
from cremind_tag.protocol.ids import LAYOUT_QR_MAX_TEXT, Icon

HIDDEN_KINDS = frozenset({"resolved", "clear"})
BODY_KINDS = frozenset({"excerpt", "needs_input", "task_outcome", "notification", "health", "indexing_problem",
                        "calendar", "automation", "usage", "tag_diagnostics", "progress"})
ALWAYS_BODY_KINDS = frozenset({"pinned_note"})
RED_KINDS = frozenset({"needs_input"})
RED_SEVERITIES = frozenset({"error"})

KIND_ICONS: dict[str, Icon] = {
    "notification": Icon.NOTIFICATIONS, "task_outcome": Icon.TASK, "needs_input": Icon.HELP, "excerpt": Icon.CHAT,
    "progress": Icon.SYNC, "health": Icon.WARNING, "indexing_problem": Icon.FOLDER, "calendar": Icon.EVENT,
    "automation": Icon.SCHEDULE, "usage": Icon.BAR_CHART, "pinned_note": Icon.PUSH_PIN,
    "tag_diagnostics": Icon.BATTERY_LOW,
}
SEVERITY_ICONS: dict[str, Icon] = {"error": Icon.ERROR, "warning": Icon.WARNING, "success": Icon.CHECK_CIRCLE}

TITLE_MAX = 400
BODY_MAX = 1200
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{24,}")
_UUID = re.compile(r"(?<![A-Za-z0-9_\-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                   r"(?![A-Za-z0-9_\-])")
_SECRET_WORDS = ("token", "secret", "passw", "apikey", "api_key", "api-key", "access_key", "signature", "jwt",
                 "bearer")


@dataclass(frozen=True)
class CardView:
    """One displayable card, normalised."""

    delivery_id: int
    kind: str
    priority: int
    created_at: datetime
    title: str
    body: str | None
    language: str
    icon: int
    red: bool
    ts: datetime
    progress: tuple[int, int] | None
    link: bytes | None


def icon_for(card: dict, kind: str) -> int:
    name = card.get("icon")
    if isinstance(name, str) and name.upper() in Icon.__members__:
        return int(Icon[name.upper()])
    if kind in KIND_ICONS:
        return int(KIND_ICONS[kind])
    severity = card.get("severity")
    return int(SEVERITY_ICONS.get(severity, Icon.INFO)) if isinstance(severity, str) else int(Icon.INFO)


def progress_of(card: dict) -> tuple[int, int] | None:
    p = card.get("progress")
    if not isinstance(p, dict):
        return None
    done, total = p.get("done"), p.get("total")
    if type(done) is not int or type(total) is not int or total <= 0 or done < 0:
        return None
    return min(done, total), total


def qr_link(link: object) -> bytes | None:
    """``link`` when it is a short, token-free https URL a QR may carry; else None.

    Rules: printable ASCII only, at most ``LAYOUT_QR_MAX_TEXT`` bytes, scheme
    ``https`` with a host (and a valid port, if any), no user info, no query
    (not even an empty ``?``), no ``=``, ``&`` or ``;`` anywhere (parameters in
    paths or fragments), no path or fragment run that looks like a token (24+
    characters of ``[A-Za-z0-9_-]`` once UUID record ids are set aside) and
    none of the usual credential words.
    """
    if not isinstance(link, str) or not 1 <= len(link) <= LAYOUT_QR_MAX_TEXT:
        return None
    if any(not 0x21 <= ord(c) <= 0x7E for c in link):
        return None
    try:
        u = urllib.parse.urlsplit(link)
        _ = u.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if u.scheme.lower() != "https" or not u.hostname or u.username is not None or u.password is not None:
        return None
    if "?" in link or "@" in u.netloc or u.query or any(ch in link for ch in "=&;"):
        return None
    lowered = link.lower()
    if any(word in lowered for word in _SECRET_WORDS):
        return None
    rest = _UUID.sub("/", u.path + "#" + u.fragment)  # record ids (UUIDs) are identifiers, not tokens
    if _TOKENISH.search(rest):
        return None
    return link.encode("ascii")


def card_view(active: ActiveCard, settings: ScreenSettings) -> CardView | None:
    """The displayable view of ``active``, or None for instruction kinds (resolved, clear)."""
    card = active.card if isinstance(active.card, dict) else {}
    kind = active.kind or str(card.get("kind") or "")
    if kind in HIDDEN_KINDS:
        return None
    title = plain_text(str(card.get("title") or "")[:TITLE_MAX])
    if not title:
        title = kind.replace("_", " ").capitalize() or "Update"
    body = None
    raw_body = card.get("body")
    if isinstance(raw_body, str) and raw_body.strip() and (
            kind in ALWAYS_BODY_KINDS or (settings.show_excerpts and kind in BODY_KINDS)):
        body = plain_text(raw_body[:BODY_MAX], keep_newlines=True) or None
    language = normalize_language(card.get("lang")) or normalize_language(settings.language)
    severity = card.get("severity")
    red = kind in RED_KINDS or (isinstance(severity, str) and severity in RED_SEVERITIES)
    ts = parse_timestamp(card.get("ts")) or parse_timestamp(active.created_at) or active.created_at
    link = qr_link(card.get("link")) if settings.qr_links else None
    return CardView(active.delivery_id, kind, int(active.priority), active.created_at, title, body, language,
                    icon_for(card, kind), red, ts, progress_of(card) if kind == "progress" else None, link)


def _sort_key(view: CardView) -> tuple[int, float, int]:
    created = parse_timestamp(view.created_at) or view.ts
    return (-view.priority, -created.timestamp(), -view.delivery_id)


def ordered_views(cards: list[ActiveCard], settings: ScreenSettings) -> list[CardView]:
    """Displayable cards, highest priority first, then newest (then highest delivery id)."""
    views = [v for v in (card_view(c, settings) for c in cards) if v is not None]
    return sorted(views, key=_sort_key)
