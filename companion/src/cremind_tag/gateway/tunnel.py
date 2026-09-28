"""The worker's end of a v2 mesh tunnel (docs/connect-setup.md §5.2, §6, §7.2).

A tunnel runs from the gateway through one bridge to that bridge's own secure
endpoint (``tag_id`` 0) or on to a tag's ``PAIR`` characteristic. The first
message up a tunnel is the endpoint's ``ident2``; after that every message is
``kind | body`` (:class:`~cremind_tag.protocol.ids.PairKind`): the Noise IK
handshake, then sealed secure messages, and a ``CLOSE {status}``::

    async with await Tunnel.open(gateway, bridge=addr, tag_id=0, duration_s=60) as tunnel:
        ident = await tunnel.wait_open()
        await tunnel.handshake(controller_priv)
        status = await tunnel.call(SerialMsg.STATUS)

A tunnel carries one session; a message that does not open ends it (the
caller opens a new tunnel). Leaving the ``async with`` sends ``CLOSE`` (best
effort) and ``TUNNEL_CLOSE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..protocol.ids import Link, PairKind, SerialFlag, SerialMsg, Status, TunnelState
from ..protocol.msgs import Ident2
from ..secure.channel import SecureChannel, SecureChannelError
from ..secure.messages import SecureFrameError, pair_message, parse_pair_message
from .client import EventSubscription, GatewayClient
from .errors import GatewayError, StatusError
from .events import TunnelEvent
from .results import to_status

log = logging.getLogger(__name__)

EVENT_QUEUE = 256


class TunnelError(GatewayError):
    """The tunnel or the session over it ended (``status`` says why when the endpoint or gateway told)."""

    def __init__(self, message: str, status: Status | int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Tunnel:
    """One open tunnel (see the module docstring)."""

    def __init__(self, gateway: GatewayClient, tunnel: int, bridge: int, tag_id: int,
                 events: EventSubscription) -> None:
        self.gateway = gateway
        self.tunnel = tunnel
        self.bridge = bridge
        self.tag_id = tag_id
        self._events = events
        self.ident: Ident2 | None = None
        self.channel: SecureChannel | None = None
        self.closed_status: Status | int | None = None

    @classmethod
    async def open(cls, gateway: GatewayClient, *, bridge: int, tag_id: int, duration_s: int,
                   op_id: int | None = None) -> Tunnel:
        events = gateway.subscribe(types=TunnelEvent, maxsize=EVENT_QUEUE)  # before the OPEN can arrive
        try:
            tunnel = await gateway.tunnel_open(bridge=bridge, tag_id=tag_id, duration_s=duration_s, op_id=op_id)
        except BaseException:
            events.close()
            raise
        return cls(gateway, tunnel, bridge, tag_id, events)

    async def __aenter__(self) -> Tunnel:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- events ---------------------------------------------------------------------------

    async def _event(self, timeout: float) -> TunnelEvent:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            event = await self._events.get(timeout=remaining)
            if isinstance(event, TunnelEvent) and event.tunnel == self.tunnel:
                if event.state == TunnelState.CLOSED:
                    self.closed_status = event.status
                    self.channel = None
                    raise TunnelError(f"tunnel {self.tunnel} closed: {_name(event.status)}", event.status)
                return event

    async def wait_open(self, timeout: float = 60.0) -> Ident2:
        """The endpoint's ``ident2`` (a sleeping tag answers when it next wakes)."""
        try:
            event = await self._event(timeout)
        except TimeoutError:
            raise TunnelError(f"tunnel {self.tunnel}: the endpoint did not answer", Status.TIMEOUT) from None
        if event.state != TunnelState.OPEN or event.data is None:
            raise TunnelError(f"tunnel {self.tunnel}: expected OPEN, got state {event.state}", Status.INVALID)
        try:
            self.ident = Ident2.unpack(event.data)
        except ValueError as exc:
            raise TunnelError(f"tunnel {self.tunnel}: malformed ident2 ({exc})", Status.INVALID) from None
        return self.ident

    async def recv(self, timeout: float = 15.0) -> bytes:
        try:
            event = await self._event(timeout)
        except TimeoutError:
            raise TunnelError(f"tunnel {self.tunnel}: no answer", Status.TIMEOUT) from None
        if event.state != TunnelState.DATA or event.data is None:
            raise TunnelError(f"tunnel {self.tunnel}: unexpected state {event.state}", Status.INVALID)
        return event.data

    async def send(self, message: bytes, *, busy_s: float = 10.0) -> None:
        """``TUNNEL_SEND``. The gateway takes one message per tunnel at a time and answers ``BUSY`` until the
        previous one is through the mesh, so ``BUSY`` is retried for ``busy_s``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + busy_s
        delay = 0.02
        while True:
            try:
                await self.gateway.tunnel_send(self.tunnel, message)
                return
            except StatusError as exc:
                if exc.status != Status.BUSY or loop.time() >= deadline:
                    raise TunnelError(f"tunnel {self.tunnel}: TUNNEL_SEND answered {_name(exc.status)}",
                                      exc.status) from None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.5)

    # -- the secure session over it -----------------------------------------------------------

    async def handshake(self, controller_priv: bytes, *, timeout: float = 15.0) -> SecureChannel:
        """Noise IK toward the endpoint that answered (its ``ik`` and ``device_id`` from ``ident2``)."""
        if self.ident is None:
            raise TunnelError(f"tunnel {self.tunnel}: not open yet")
        channel = SecureChannel(controller_priv, self.ident.ik, self.ident.device_id, Link.TUNNEL)
        await self.send(pair_message(PairKind.HANDSHAKE, channel.message1()))
        kind, body = self._parse(await self.recv(timeout))
        if kind == PairKind.CLOSE:
            raise TunnelError(f"tunnel {self.tunnel}: handshake refused ({_name(_close_status(body))})",
                              _close_status(body))
        if kind != PairKind.HANDSHAKE:
            raise TunnelError(f"tunnel {self.tunnel}: expected the handshake answer", Status.INVALID)
        try:
            channel.finish(body)
        except SecureChannelError as exc:
            raise TunnelError(f"tunnel {self.tunnel}: {exc}", Status.AUTH_FAILED) from None
        self.channel = channel
        return channel

    async def call(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                   timeout: float = 20.0) -> dict[str, Any]:
        """A sealed request to the endpoint; returns its decoded answer (``status`` unchecked)."""
        channel = self.channel
        if channel is None or not channel.open:
            raise TunnelError(f"tunnel {self.tunnel}: no session", Status.AUTH_REQUIRED)
        rid, sealed = channel.seal_request(msg, fields or {})
        await self.send(pair_message(PairKind.TRANSPORT, sealed))
        kind, body = self._parse(await self.recv(timeout))
        if kind == PairKind.CLOSE:
            self.channel = None
            raise TunnelError(f"tunnel {self.tunnel}: {msg.name} ended the session "
                              f"({_name(_close_status(body))})", _close_status(body))
        if kind != PairKind.TRANSPORT:
            raise TunnelError(f"tunnel {self.tunnel}: unexpected message kind {kind}", Status.INVALID)
        try:
            answer = channel.unseal(body)
        except SecureChannelError as exc:
            self.channel = None
            raise TunnelError(f"tunnel {self.tunnel}: {exc}", Status.AUTH_FAILED) from None
        if answer.request_id != rid or not answer.flags & SerialFlag.RESPONSE or answer.type != int(msg):
            self.channel = None
            raise TunnelError(f"tunnel {self.tunnel}: the answer does not match {msg.name}", Status.INVALID)
        return SecureChannel.decode(answer)

    async def checked(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                      timeout: float = 20.0) -> dict[str, Any]:
        answer = await self.call(msg, fields, timeout=timeout)
        status = to_status(answer["status"])
        if status != Status.OK:
            raise TunnelError(f"{msg.name} answered {_name(status)}", status)
        return answer

    @staticmethod
    def _parse(raw: bytes) -> tuple[PairKind, bytes]:
        try:
            return parse_pair_message(raw)
        except (SecureFrameError, ValueError) as exc:
            raise TunnelError(f"malformed tunnel message: {exc}", Status.INVALID) from None

    async def close(self, *, say_goodbye: bool = True) -> None:
        """``CLOSE`` to the endpoint (when a session is open) and ``TUNNEL_CLOSE`` — both best effort."""
        try:
            if self.closed_status is None:
                if say_goodbye and self.channel is not None:
                    with contextlib.suppress(GatewayError):
                        await self.send(pair_message(PairKind.CLOSE, bytes([Status.OK])))
                with contextlib.suppress(GatewayError):
                    await self.gateway.tunnel_close(self.tunnel)
        finally:
            self.channel = None
            self._events.close()


def _close_status(body: bytes) -> Status | int:
    return to_status(body[0]) if body else Status.INVALID


def _name(status: Status | int | None) -> str:
    if isinstance(status, Status):
        return status.name
    return str(status)


__all__ = ["Tunnel", "TunnelError"]
