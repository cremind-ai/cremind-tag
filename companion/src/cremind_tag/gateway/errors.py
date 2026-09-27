"""Errors raised by the serial clients (gateway and bridge maintenance port)."""

from __future__ import annotations

from ..protocol.ids import SerialMsg, Status


class GatewayError(Exception):
    """Base class for serial-link failures."""


class GatewayTimeout(GatewayError):
    """No response after every attempt (docs/protocol.md §1.3: 2 s per attempt)."""


class GatewayDisconnected(GatewayError):
    """The link is down (port closed, device gone or rebooting)."""


class ProtocolError(GatewayError):
    """The device answered something this client cannot accept."""


class FrameTooLargeError(GatewayError):
    """A request does not fit the device's ``max_frame`` (docs/protocol.md §1.1)."""


class StatusError(GatewayError):
    """A request that must succeed answered a non-OK status."""

    def __init__(self, msg: SerialMsg, status: Status | int, detail: int | None = None,
                 text: str | None = None) -> None:
        name = status.name if isinstance(status, Status) else str(status)
        message = f"{msg.name} answered {name}"
        if text:
            message += f": {text}"
        super().__init__(message)
        self.msg = msg
        self.status = status
        self.detail = detail
        self.text = text
