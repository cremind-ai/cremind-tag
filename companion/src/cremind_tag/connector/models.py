"""Typed views of the connector API's JSON (docs/connector-api.md).

Parsing is strict about what the companion relies on (ids, sequence numbers,
epochs, timestamps) and lenient about everything else, so a newer Cremind that
adds fields never breaks an older companion. A malformed object raises
:class:`MalformedResponse`.

Timestamps on the wire are ISO 8601 UTC strings (``2026-09-27T10:00:00Z``);
:func:`parse_time` also accepts epoch milliseconds, which Cremind's REST API
(not the connector) uses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

TAG_ID_HEX_LEN = 8


class MalformedResponse(ValueError):
    """Cremind answered JSON the companion cannot use."""


def parse_time(value: Any, *, where: str = "timestamp") -> dt.datetime:
    """ISO 8601 (``Z`` allowed) or epoch milliseconds -> an aware UTC datetime."""
    if isinstance(value, bool) or value is None:
        raise MalformedResponse(f"{where}: missing")
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(float(value) / 1000.0, tz=dt.UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            raise MalformedResponse(f"{where}: not an ISO 8601 timestamp: {value!r}") from None
        return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
    raise MalformedResponse(f"{where}: not a timestamp: {value!r}")


def iso(moment: dt.datetime) -> str:
    """``2026-09-27T10:00:00.123Z`` (what the companion sends)."""
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_now() -> str:
    return iso(dt.datetime.now(dt.UTC))


def parse_tag_hw_id(value: Any) -> int:
    """Cremind's ``tag_id`` (8 hex digits) -> the u32 tag id."""
    if not isinstance(value, str) or len(value) != TAG_ID_HEX_LEN:
        raise MalformedResponse(f"tag_id: expected 8 hex digits, got {value!r}")
    try:
        tag_id = int(value, 16)
    except ValueError:
        raise MalformedResponse(f"tag_id: expected 8 hex digits, got {value!r}") from None
    if not 1 <= tag_id <= 0xFFFFFFFE:
        raise MalformedResponse(f"tag_id: out of range: {value!r}")
    return tag_id


def tag_hw_id(tag_id: int) -> str:
    """The u32 tag id as Cremind names it (8 upper-case hex digits)."""
    return f"{tag_id:08X}"


def _int(obj: Mapping[str, Any], key: str, *, default: int | None = None, minimum: int | None = 0) -> int:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        if value is None and default is not None:
            return default
        raise MalformedResponse(f"{key}: expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise MalformedResponse(f"{key}: {value} is below {minimum}")
    return value


def _opt_str(obj: Mapping[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedResponse(f"{key}: expected a string, got {value!r}")
    return value


def _str(obj: Mapping[str, Any], key: str) -> str:
    value = _opt_str(obj, key)
    if value is None:
        raise MalformedResponse(f"{key}: missing")
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MalformedResponse(f"{where}: expected an object")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedResponse(f"{where}: expected a list")
    return value


@dataclass(frozen=True, slots=True)
class Job:
    """One delivery job (connector-api.md "Job shape")."""

    delivery_id: int
    seq: int
    tag_id: int
    epoch: int
    kind: str
    priority: int
    replace_key: str | None
    resolves: str | None
    created_at: dt.datetime
    expires_at: dt.datetime
    stage: str
    card: dict[str, Any]

    @property
    def tag_hw_id(self) -> str:
        return tag_hw_id(self.tag_id)

    @classmethod
    def from_json(cls, raw: Any) -> Job:
        obj = _mapping(raw, "job")
        card = obj.get("card")
        if card is None:
            card = {}
        if not isinstance(card, dict):
            raise MalformedResponse("job.card: expected an object")
        kind = _str(obj, "kind")
        return cls(
            delivery_id=_int(obj, "delivery_id", minimum=1),
            seq=_int(obj, "seq"),
            tag_id=parse_tag_hw_id(obj.get("tag_id")),
            epoch=_int(obj, "epoch"),
            kind=kind,
            priority=_int(obj, "priority", default=0, minimum=None),
            replace_key=_opt_str(obj, "replace_key"),
            resolves=_opt_str(obj, "resolves"),
            created_at=parse_time(obj.get("created_at"), where="job.created_at"),
            expires_at=parse_time(obj.get("expires_at"), where="job.expires_at"),
            stage=_opt_str(obj, "stage") or "queued",
            card=dict(card),
        )


@dataclass(frozen=True, slots=True)
class TagInfo:
    """A tag the credential's profile owns on this companion (``sync.tags``)."""

    tag_id: int
    name: str
    epoch: int
    bridge_hw_id: str | None
    width: int | None
    height: int | None
    planes: int | None
    rotation: int
    desired_revision: int
    displayed_revision: int
    clear_required: bool

    @classmethod
    def from_json(cls, raw: Any) -> TagInfo:
        obj = _mapping(raw, "tag")

        def opt_int(key: str) -> int | None:
            value = obj.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return cls(
            tag_id=parse_tag_hw_id(obj.get("tag_id")),
            name=str(obj.get("name") or ""),
            epoch=_int(obj, "epoch", default=0),
            bridge_hw_id=_opt_str(obj, "bridge_hw_id"),
            width=opt_int("width"),
            height=opt_int("height"),
            planes=opt_int("planes"),
            rotation=(opt_int("rotation") or 0) % 4,
            desired_revision=_int(obj, "desired_revision", default=0),
            displayed_revision=_int(obj, "displayed_revision", default=0),
            clear_required=bool(obj.get("clear_required")),
        )


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    """The profile's display settings (``sync.settings``)."""

    enabled: bool = True
    layout: str = "status"
    show_excerpts: bool = False
    qr_links: bool = False
    progress_cadence_s: float = 300.0
    timezone: str = "UTC"
    language: str = "en"

    @classmethod
    def from_json(cls, raw: Any) -> ProfileSettings:
        obj = raw if isinstance(raw, Mapping) else {}
        cadence = obj.get("progress_cadence_s")
        return cls(
            enabled=bool(obj.get("enabled", True)),
            layout=str(obj.get("layout") or "status"),
            show_excerpts=bool(obj.get("show_excerpts")),
            qr_links=bool(obj.get("qr_links")),
            progress_cadence_s=float(cadence) if isinstance(cadence, int | float) and not isinstance(cadence, bool)
            and cadence >= 0 else 300.0,
            timezone=str(obj.get("timezone") or "UTC"),
            language=str(obj.get("language") or "en"),
        )

    def as_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "layout": self.layout, "show_excerpts": self.show_excerpts,
                "qr_links": self.qr_links, "progress_cadence_s": self.progress_cadence_s,
                "timezone": self.timezone, "language": self.language}


@dataclass(frozen=True, slots=True)
class SyncResult:
    """``POST sync``: the explicit resynchronisation."""

    profile: str
    companion_id: str
    stream_id: str
    cursor_valid: bool
    oldest_seq: int
    head_seq: int
    outstanding: tuple[Job, ...]
    tags: tuple[TagInfo, ...]
    settings: ProfileSettings

    @classmethod
    def from_json(cls, raw: Any) -> SyncResult:
        obj = _mapping(raw, "sync")
        return cls(
            profile=_str(obj, "profile"),
            companion_id=_str(obj, "companion_id"),
            stream_id=_str(obj, "stream_id"),
            cursor_valid=bool(obj.get("cursor_valid")),
            oldest_seq=_int(obj, "oldest_seq", default=0),
            head_seq=_int(obj, "head_seq", default=0),
            outstanding=tuple(Job.from_json(j) for j in _list(obj.get("outstanding"), "outstanding")),
            tags=tuple(TagInfo.from_json(t) for t in _list(obj.get("tags"), "tags")),
            settings=ProfileSettings.from_json(obj.get("settings")),
        )


@dataclass(frozen=True, slots=True)
class EventsPage:
    """``GET events``: one page of the profile's journal of jobs."""

    stream_id: str
    jobs: tuple[Job, ...]
    next_after: int
    head_seq: int

    @classmethod
    def from_json(cls, raw: Any) -> EventsPage:
        obj = _mapping(raw, "events")
        return cls(
            stream_id=_str(obj, "stream_id"),
            jobs=tuple(Job.from_json(j) for j in _list(obj.get("jobs"), "jobs")),
            next_after=_int(obj, "next_after"),
            head_seq=_int(obj, "head_seq", default=0),
        )


@dataclass(frozen=True, slots=True)
class WhoAmI:
    credential_id: str
    kind: str
    companion_id: str
    profile: str | None
    api_version: int
    server_time: str | None

    @classmethod
    def from_json(cls, raw: Any) -> WhoAmI:
        obj = _mapping(raw, "whoami")
        return cls(credential_id=_str(obj, "credential_id"), kind=_str(obj, "kind"),
                   companion_id=_str(obj, "companion_id"), profile=_opt_str(obj, "profile"),
                   api_version=_int(obj, "api_version", default=1), server_time=_opt_str(obj, "server_time"))


@dataclass(frozen=True, slots=True)
class Command:
    """A hardware operation queued by an admin action (``GET commands``)."""

    id: str
    kind: str
    args: dict[str, Any]
    status: str
    created_at: dt.datetime | None
    expires_at: dt.datetime | None

    @classmethod
    def from_json(cls, raw: Any) -> Command:
        obj = _mapping(raw, "command")
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            raise MalformedResponse("command.args: expected an object")

        def when(key: str) -> dt.datetime | None:
            return parse_time(obj[key], where=f"command.{key}") if obj.get(key) is not None else None

        return cls(id=_str(obj, "id"), kind=_str(obj, "kind"), args=dict(args),
                   status=_opt_str(obj, "status") or "queued", created_at=when("created_at"),
                   expires_at=when("expires_at"))


@dataclass(frozen=True, slots=True)
class Assignment:
    """One tag's assignment as Cremind sees it (``POST inventory`` response)."""

    tag_id: int
    owner_profile: str | None
    bridge_hw_id: str | None
    epoch: int
    rotation: int

    @classmethod
    def from_json(cls, raw: Any) -> Assignment:
        obj = _mapping(raw, "assignment")
        return cls(tag_id=parse_tag_hw_id(obj.get("tag_id")), owner_profile=_opt_str(obj, "owner_profile"),
                   bridge_hw_id=_opt_str(obj, "bridge_hw_id"), epoch=_int(obj, "epoch", default=0),
                   rotation=_int(obj, "rotation", default=0) % 4)


@dataclass(frozen=True, slots=True)
class InventoryResult:
    devices: tuple[dict[str, Any], ...]
    assignments: tuple[Assignment, ...]

    @classmethod
    def from_json(cls, raw: Any) -> InventoryResult:
        obj = _mapping(raw, "inventory")
        return cls(devices=tuple(d for d in _list(obj.get("devices"), "devices") if isinstance(d, dict)),
                   assignments=tuple(Assignment.from_json(a) for a in _list(obj.get("assignments"), "assignments")))


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    server_time: str | None
    commands_pending: int

    @classmethod
    def from_json(cls, raw: Any) -> HeartbeatResult:
        obj = _mapping(raw, "heartbeat")
        return cls(server_time=_opt_str(obj, "server_time"),
                   commands_pending=_int(obj, "commands_pending", default=0))


@dataclass(frozen=True, slots=True)
class Receipt:
    """One delivery receipt (``POST receipts``)."""

    delivery_id: int
    stage: str
    at: str
    tag_id: str
    epoch: int
    outcome: str | None = None
    status_code: int | None = None
    revision: int | None = None
    digest: str | None = None
    timing: dict[str, int] | None = None
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"delivery_id": self.delivery_id, "stage": self.stage, "outcome": self.outcome,
                               "at": self.at, "tag_id": self.tag_id, "epoch": self.epoch}
        for key in ("status_code", "revision", "digest", "timing", "detail"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        out.update(self.extra)
        return out


__all__ = [
    "Assignment", "Command", "EventsPage", "HeartbeatResult", "InventoryResult", "Job", "MalformedResponse",
    "ProfileSettings", "Receipt", "SyncResult", "TagInfo", "WhoAmI", "iso", "iso_now", "parse_tag_hw_id",
    "parse_time", "tag_hw_id",
]
