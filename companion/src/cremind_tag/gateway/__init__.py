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
    Discovered,
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
    TunnelEvent,
    UnknownEvent,
    UnprovBeacon,
)
from .link import SecureOptions, WrongDeviceError
from .opid import OpIdGenerator, new_op_id
from .results import Ack, BridgeInfo, Caps, DeviceInfo, HelloInfo, IdentifyInfo, NodeInfo, Timing

__all__ = [
    "RETAINED_EVENTS", "Ack", "AssignResult", "BridgeInfo", "BridgeInfoEvent", "Caps", "DeviceInfo", "Discovered",
    "EventHandler", "EventSubscription", "EventWaiter", "FrameTooLargeError", "GatewayClient", "GatewayDisconnected",
    "GatewayError", "GatewayEvent", "GatewayTimeout", "HelloInfo", "IdentifyInfo", "LogEvent", "NodeConfigured",
    "NodeInfo", "NodeRemoved", "OpIdGenerator", "ProtocolError", "Provisioned", "ResultEvent", "RetainedEvent",
    "SecureOptions", "SessionStarted", "StageEvent", "StatusError", "TagSeen", "Timing", "TunnelEvent",
    "UnknownEvent", "UnprovBeacon", "WrongDeviceError", "any_of", "matches", "new_op_id",
]
