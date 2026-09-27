"""Typed gateway events (docs/protocol.md §1.2, spec ``serial.message_types`` d2h).

*Retained* events (``EVT_PROVISIONED``, ``EVT_NODE_CONFIGURED``,
``EVT_NODE_REMOVED``, ``EVT_ASSIGN_RESULT``, ``EVT_RESULT``) carry ``seq``; the
gateway keeps them until ``EVENT_ACK`` and re-sends them after every ``HELLO``.
The other events are best effort. Every event records the ``boot_id`` of the
gateway session it arrived in, so ``(boot_id, seq)`` identifies a retained event.

:class:`SessionStarted` is synthesised by :class:`~cremind_tag.gateway.client.GatewayClient`
after every successful ``HELLO`` (first connection, reconnection, resync) and
travels through the same ordered handler pipeline as retained events;
``boot_changed`` tells that in-flight gateway state was lost (§1.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..protocol import cbor_msgs
from ..protocol.ids import RESULT_FLAG_DUPLICATE, RESULT_FLAG_ESCALATED, DeliveryStage, SerialMsg, Status
from .results import Assignment, BridgeInfo, Caps, HelloInfo, Timing, to_status

RETAINED_EVENTS = frozenset({SerialMsg.EVT_PROVISIONED, SerialMsg.EVT_NODE_CONFIGURED, SerialMsg.EVT_NODE_REMOVED,
                             SerialMsg.EVT_ASSIGN_RESULT, SerialMsg.EVT_RESULT})


@dataclass(frozen=True, slots=True, kw_only=True)
class GatewayEvent:
    """Base of every event; ``raw`` holds the decoded CBOR fields."""

    TYPE: ClassVar[SerialMsg | None] = None
    boot_id: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def retained(self) -> bool:
        return False

    @property
    def seq(self) -> int | None:
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class RetainedEvent(GatewayEvent):
    """An event the gateway keeps until ``EVENT_ACK`` (cumulative by ``seq``)."""

    event_seq: int

    @property
    def retained(self) -> bool:
        return True

    @property
    def seq(self) -> int:
        return self.event_seq


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEvent(GatewayEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_LOG
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UnprovBeacon(GatewayEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_UNPROV_BEACON
    uuid: bytes
    rssi: int
    oob: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Provisioned(RetainedEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_PROVISIONED
    op_id: int
    uuid: bytes
    addr: int
    elements: int
    status: Status | int


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeConfigured(RetainedEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_NODE_CONFIGURED
    op_id: int
    addr: int
    status: Status | int


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeRemoved(RetainedEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_NODE_REMOVED
    op_id: int
    addr: int
    status: Status | int


@dataclass(frozen=True, slots=True, kw_only=True)
class AssignResult(RetainedEvent):
    """Outcome of ``ASSIGN_TAG`` / ``UNASSIGN_TAG`` (the bridge's ``ASSIGN_STATUS``)."""

    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_ASSIGN_RESULT
    op_id: int
    bridge: int
    tag_id: int
    epoch: int
    status: Status | int


@dataclass(frozen=True, slots=True, kw_only=True)
class StageEvent(GatewayEvent):
    """Best-effort progress of a delivery (``BRIDGE_RECEIVED``, ``TRANSFERRING``, ``REFRESHING``)."""

    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_STAGE
    update_id: int
    tag_id: int
    revision: int
    stage: DeliveryStage | int


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultEvent(RetainedEvent):
    """The final result of a delivery or tag command (exactly one per ``update_id``, §1.5).

    For ``TAG_COMMAND`` the ``update_id`` is the command's ``op_id``. ``digest`` is
    the first 8 bytes of the frame digest (zeros when nothing was displayed).
    ``flags`` and ``stored_epoch`` are the bridge's ``DELIVERY_RESULT`` report
    (§3.4): bit0 ``RESULT_FLAG_DUPLICATE`` (the tag answered with its stored
    ACK), bit1 ``RESULT_FLAG_ESCALATED`` (an unauthenticated status repeated in 3
    sessions); ``stored_epoch`` is the tag's stored epoch, 0 when no tag session
    produced the result (every result the gateway makes itself).
    """

    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_RESULT
    update_id: int
    bridge: int
    tag_id: int
    epoch: int
    revision: int
    status: Status | int
    digest: bytes
    battery_mv: int
    timing: Timing
    flags: int = 0
    stored_epoch: int = 0

    @property
    def displayed(self) -> bool:
        return self.status == Status.OK

    @property
    def duplicate(self) -> bool:
        """The tag answered from its stored ACK: the revision was already displayed, nothing was redrawn."""
        return bool(self.flags & RESULT_FLAG_DUPLICATE)

    @property
    def escalated(self) -> bool:
        """An unauthenticated tag status that ended the job after 3 consecutive sessions (§10)."""
        return bool(self.flags & RESULT_FLAG_ESCALATED)


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeInfoEvent(GatewayEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_BRIDGE_INFO
    info: BridgeInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class TagSeen(GatewayEvent):
    TYPE: ClassVar[SerialMsg] = SerialMsg.EVT_TAG_SEEN
    bridge: int
    tag_id: int
    rssi: int
    battery_mv: int
    flags: int


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownEvent(GatewayEvent):
    """An event type this build does not know (forward compatibility)."""

    type_code: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionStarted(GatewayEvent):
    """Synthesised after every successful HELLO; never sent by the device, never ACKed."""

    hello: HelloInfo
    previous_boot_id: int | None

    @property
    def boot_changed(self) -> bool:
        """The gateway rebooted since the previous session: its in-flight state is gone."""
        return self.previous_boot_id is not None and self.previous_boot_id != self.hello.boot_id


def parse_event(type_code: int, payload: bytes, boot_id: int | None) -> GatewayEvent:
    """Decode an ``EVENT`` frame; raises ``cbor_msgs.CborError`` on a malformed payload."""
    try:
        msg = SerialMsg(type_code)
    except ValueError:
        return UnknownEvent(boot_id=boot_id, raw=cbor_msgs.decode_map(payload), type_code=type_code)
    if msg not in cbor_msgs.EVENTS:
        return UnknownEvent(boot_id=boot_id, raw=cbor_msgs.decode_map(payload), type_code=type_code)
    f = cbor_msgs.decode_event(msg, payload)
    common: dict[str, Any] = {"boot_id": boot_id, "raw": f}
    match msg:
        case SerialMsg.EVT_LOG:
            return LogEvent(**common, text=f["text"])
        case SerialMsg.EVT_UNPROV_BEACON:
            return UnprovBeacon(**common, uuid=f["uuid"], rssi=f["rssi"], oob=f["oob"])
        case SerialMsg.EVT_PROVISIONED:
            return Provisioned(**common, event_seq=f["seq"], op_id=f["op_id"], uuid=f["uuid"], addr=f["addr"],
                               elements=f["elements"], status=to_status(f["status"]))
        case SerialMsg.EVT_NODE_CONFIGURED:
            return NodeConfigured(**common, event_seq=f["seq"], op_id=f["op_id"], addr=f["addr"],
                                  status=to_status(f["status"]))
        case SerialMsg.EVT_NODE_REMOVED:
            return NodeRemoved(**common, event_seq=f["seq"], op_id=f["op_id"], addr=f["addr"],
                               status=to_status(f["status"]))
        case SerialMsg.EVT_ASSIGN_RESULT:
            return AssignResult(**common, event_seq=f["seq"], op_id=f["op_id"], bridge=f["bridge"], tag_id=f["tag_id"],
                                epoch=f["epoch"], status=to_status(f["status"]))
        case SerialMsg.EVT_STAGE:
            stage: DeliveryStage | int = f["stage"]
            try:
                stage = DeliveryStage(stage)
            except ValueError:
                pass
            return StageEvent(**common, update_id=f["update_id"], tag_id=f["tag_id"], revision=f["revision"],
                              stage=stage)
        case SerialMsg.EVT_RESULT:
            return ResultEvent(**common, event_seq=f["seq"], update_id=f["update_id"], bridge=f["bridge"],
                               tag_id=f["tag_id"], epoch=f["epoch"], revision=f["revision"],
                               status=to_status(f["status"]), digest=f["digest"], battery_mv=f["battery_mv"],
                               timing=Timing.from_map(f["timing"]), flags=f["flags"],
                               stored_epoch=f["stored_epoch"])
        case SerialMsg.EVT_BRIDGE_INFO:
            info = BridgeInfo(f["addr"], f["fw"], f["fontpack_id"], Caps.from_map(f["caps"]),
                              tuple(Assignment(a.get("tag_id", 0), a.get("epoch", 0)) for a in f["assigned"]),
                              dict(f["counters"]), raw=f)
            return BridgeInfoEvent(**common, info=info)
        case SerialMsg.EVT_TAG_SEEN:
            return TagSeen(**common, bridge=f["bridge"], tag_id=f["tag_id"], rssi=f["rssi"],
                           battery_mv=f["battery_mv"], flags=f["flags"])
    return UnknownEvent(boot_id=boot_id, raw=f, type_code=type_code)  # pragma: no cover - EVENTS covers all
