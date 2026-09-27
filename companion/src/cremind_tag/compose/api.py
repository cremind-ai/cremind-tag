"""Screen composition contract: a tag's active cards -> one logical screen.

Implemented by `cremind_tag.compose.screen.compose_screen`; called by the daemon
(`cremind_tag.daemon`) every time a tag's active card set or clock line changes.
See docs/connector-api.md "Screen model" and docs/layout.md. Also in
`compose.screen`: ``compose_identify(panel, fonts, tag_id=None)`` and
``compose_blank(panel)``; previews in `compose.preview` (``preview_png``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cremind_tag.fonts.fontset import FontSet


@dataclass(frozen=True)
class TagPanel:
    """What layout needs to know about a tag's display (from enrollment/sync)."""

    tag_id: int
    width: int
    """Native panel width in pixels."""
    height: int
    planes: int
    """1 = black/white, 2 = black/white/red."""
    plane_flags: int
    rotation: int = 0
    """0..3 quarter turns clockwise, logical -> native (docs/protocol.md §4.4)."""
    name: str = ""


@dataclass(frozen=True)
class ActiveCard:
    """One card the tag should currently show (a delivery job's `card`, see docs/connector-api.md)."""

    delivery_id: int
    kind: str
    priority: int
    created_at: datetime
    card: dict[str, Any]


@dataclass(frozen=True)
class ScreenSettings:
    """Profile display settings delivered by `sync` (docs/connector-api.md)."""

    show_excerpts: bool = False
    qr_links: bool = False
    timezone: str = "UTC"
    language: str = "en"


@dataclass(frozen=True)
class ComposedScreen:
    layout: bytes
    """Encoded logical screen (docs/protocol.md §4), already validated."""
    delivery_ids: tuple[int, ...]
    """Deliveries whose cards this screen shows (all become `displayed` together), in display order
    (headline first). Cards only counted in the footer are NOT included (docs/layout.md "Delivery ids")."""
    pending_count: int
    """Active cards not shown for lack of space ("N more updates waiting for this tag")."""
    unsupported_chars: tuple[str, ...] = field(default=())
    """Characters no face in the pack covers (reported in previews and diagnostics)."""
    pending_delivery_ids: tuple[int, ...] = field(default=())
    """The deliveries counted in the footer (``len == pending_count``); they stay undisplayed."""


class Composer(Protocol):
    def __call__(self, panel: TagPanel, cards: list[ActiveCard], fonts: FontSet,
                 settings: ScreenSettings, now: datetime) -> ComposedScreen: ...
