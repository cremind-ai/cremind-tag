"""Typed views of serial responses (docs/protocol.md §1, spec ``serial.message_types``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..protocol.ids import NodeRole, SerialMsg, Status
from .errors import StatusError


def as_status(value: int | None) -> Status | int | None:
    """``Status`` when the code is known, else the raw integer (forward compatibility)."""
    if value is None:
        return None
    try:
        return Status(value)
    except ValueError:
        return value


def to_status(value: int) -> Status | int:
    """Like :func:`as_status` for a value that is always present (never test it for truth: OK is 0)."""
    try:
        return Status(value)
    except ValueError:
        return value


def status_name(value: Status | int | None) -> str:
    if value is None:
        return "-"
    return value.name if isinstance(value, Status) else str(value)


@dataclass(frozen=True, slots=True)
class Ack:
    """A request's immediate answer. Asynchronous work answers ``ACCEPTED`` (§1.2).

    ``duplicate`` is set when the device recognised the ``op_id`` and returned the
    remembered status without doing new work (§1.4).
    """

    msg: SerialMsg
    status: Status | int
    detail: Status | int | None = None
    text: str | None = None
    op_id: int | None = None
    fields: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def duplicate(self) -> bool:
        return self.detail == Status.DUPLICATE or self.status == Status.DUPLICATE

    @property
    def ok(self) -> bool:
        """The device accepted or completed the request (a duplicate of an accepted one counts)."""
        return self.status in (Status.OK, Status.ACCEPTED) or self.status == Status.DUPLICATE

    @property
    def busy(self) -> bool:
        return self.status == Status.BUSY

    def raise_for_status(self) -> Ack:
        if not self.ok:
            raise StatusError(self.msg, self.status, self.detail if isinstance(self.detail, int) else None, self.text)
        return self

    @classmethod
    def from_fields(cls, msg: SerialMsg, fields: Mapping[str, Any], op_id: int | None = None) -> Ack:
        return cls(msg, to_status(fields["status"]), as_status(fields.get("detail")), fields.get("text"),
                   op_id, dict(fields))


@dataclass(frozen=True, slots=True)
class Caps:
    """``caps`` map of HELLO/INFO responses."""

    max_frame: int | None = None
    credits: int | None = None
    max_bridges: int | None = None
    max_tags: int | None = None
    role: NodeRole | int | None = None
    board: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_map(cls, caps: Mapping[str, Any] | None) -> Caps:
        caps = caps or {}
        role = caps.get("role")
        if isinstance(role, int):
            try:
                role = NodeRole(role)
            except ValueError:
                pass
        return cls(caps.get("max_frame"), caps.get("credits"), caps.get("max_bridges"), caps.get("max_tags"),
                   role, caps.get("board"), dict(caps))


@dataclass(frozen=True, slots=True)
class HelloInfo:
    proto: int
    fw: str
    build: str
    boot_id: int
    caps: Caps

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> HelloInfo:
        return cls(fields.get("proto", 0), fields.get("fw", ""), fields.get("build", ""), fields.get("boot_id", 0),
                   Caps.from_map(fields.get("caps")))


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    fw: str
    build: str
    boot_id: int
    caps: Caps
    counters: Mapping[str, int]

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> DeviceInfo:
        return cls(fields.get("fw", ""), fields.get("build", ""), fields.get("boot_id", 0),
                   Caps.from_map(fields.get("caps")), dict(fields.get("counters", {})))


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """One ``LIST_NODES`` entry (the gateway's CDB)."""

    addr: int
    uuid: bytes
    elements: int
    configured: bool
    name: str
    last_seen_s: int | None

    @classmethod
    def from_map(cls, node: Mapping[str, Any]) -> NodeInfo:
        return cls(node.get("addr", 0), node.get("uuid", b""), node.get("elements", 1), bool(node.get("configured")),
                   node.get("name", ""), node.get("last_seen_s"))


@dataclass(frozen=True, slots=True)
class Assignment:
    tag_id: int
    epoch: int


@dataclass(frozen=True, slots=True)
class BridgeInfo:
    """``GET_INVENTORY`` item / ``EVT_BRIDGE_INFO`` body."""

    addr: int
    fw: str | None
    fontpack_id: bytes | None
    caps: Caps
    assigned: tuple[Assignment, ...]
    counters: Mapping[str, int]
    uuid: bytes | None = None
    name: str | None = None
    flash_size: int | None = None
    board: int | None = None
    configured: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_map(cls, item: Mapping[str, Any]) -> BridgeInfo:
        assigned = tuple(Assignment(a.get("tag_id", 0), a.get("epoch", 0)) for a in item.get("assigned", []))
        return cls(item.get("addr", 0), item.get("fw"), item.get("fontpack_id"), Caps.from_map(item.get("caps")),
                   assigned, dict(item.get("counters", {})), item.get("uuid"), item.get("name"),
                   item.get("flash_size"), item.get("board"), item.get("configured"), dict(item))


@dataclass(frozen=True, slots=True)
class Timing:
    """``timing`` of ``EVT_RESULT`` (ms): wake = layout validated -> tag connected."""

    wake_ms: int = 0
    mesh_ms: int = 0
    transfer_ms: int = 0
    refresh_ms: int = 0
    suspend_ms: int = 0

    @classmethod
    def from_map(cls, timing: Mapping[str, Any] | None) -> Timing:
        timing = timing or {}
        return cls(timing.get("wake_ms", 0), timing.get("mesh_ms", 0), timing.get("transfer_ms", 0),
                   timing.get("refresh_ms", 0), timing.get("suspend_ms", 0))

    def as_dict(self) -> dict[str, int]:
        return {"wake_ms": self.wake_ms, "mesh_ms": self.mesh_ms, "transfer_ms": self.transfer_ms,
                "refresh_ms": self.refresh_ms, "suspend_ms": self.suspend_ms}


@dataclass(frozen=True, slots=True)
class FontStatus:
    """``FONT_STATUS`` on a bridge maintenance port: the active pack."""

    fontpack_id: bytes | None
    slot: int | None
    size: int | None
    flash_size: int | None

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> FontStatus:
        return cls(fields.get("fontpack_id"), fields.get("slot"), fields.get("size"), fields.get("flash_size"))


@dataclass(frozen=True, slots=True)
class FlashTestItem:
    offset: int
    status: Status | int


@dataclass(frozen=True, slots=True)
class FlashTestResult:
    status: Status | int
    flash_size: int | None
    items: tuple[FlashTestItem, ...]

    @property
    def ok(self) -> bool:
        return self.status == Status.OK and all(i.status == Status.OK for i in self.items)

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> FlashTestResult:
        items = tuple(FlashTestItem(i.get("offset", 0), to_status(i.get("status", 0)))
                      for i in fields.get("items", []))
        return cls(to_status(fields["status"]), fields.get("flash_size"), items)
