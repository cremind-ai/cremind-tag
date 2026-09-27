"""Companion side of the serial protocol to the gateway (docs/protocol.md §1).

- :class:`GatewayClient` — typed requests, retained-event pipeline with the
  ACK-after-handler durability contract (see ``client`` module docstring).
- ``events`` — typed event dataclasses; ``results`` — typed responses.
- ``link`` — framing, credits, request matching, resync; shared with
  :mod:`cremind_tag.bridge_maint`.
- ``transport`` — pyserial URL in a reader thread.
"""

from .client import EventHandler, EventSubscription, EventWaiter, GatewayClient, any_of, matches
from .errors import (
    FrameTooLargeError,
    GatewayDisconnected,
    GatewayError,
    GatewayTimeout,
    ProtocolError,
    StatusError,
)
from .events import (
    RETAINED_EVENTS,
    AssignResult,
    BridgeInfoEvent,
    GatewayEvent,
    LogEvent,
    NodeConfigured,
    NodeRemoved,
    Provisioned,
    ResultEvent,
    RetainedEvent,
    SessionStarted,
    StageEvent,
    TagSeen,
    UnknownEvent,
    UnprovBeacon,
)
from .opid import OpIdGenerator, new_op_id
from .results import Ack, BridgeInfo, Caps, DeviceInfo, HelloInfo, NodeInfo, Timing

__all__ = [
    "RETAINED_EVENTS", "Ack", "AssignResult", "BridgeInfo", "BridgeInfoEvent", "Caps", "DeviceInfo", "EventHandler",
    "EventSubscription", "EventWaiter", "FrameTooLargeError", "GatewayClient", "GatewayDisconnected", "GatewayError",
    "GatewayEvent", "GatewayTimeout", "HelloInfo", "LogEvent", "NodeConfigured", "NodeInfo", "NodeRemoved",
    "OpIdGenerator", "ProtocolError", "Provisioned", "ResultEvent", "RetainedEvent", "SessionStarted", "StageEvent",
    "StatusError", "TagSeen", "Timing", "UnknownEvent", "UnprovBeacon", "any_of", "matches", "new_op_id",
]
