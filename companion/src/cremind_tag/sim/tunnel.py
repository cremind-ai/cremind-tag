"""A v2 bridge's end of a mesh tunnel (docs/connect-setup.md 6, 7.2; docs/simulator.md "Protocol v2").

A tunnel with ``tag_id = 0`` ends at the bridge's own secure endpoint (a
:class:`~cremind_tag.sim.v2.PairEndpoint` over the bridge's ``SecureDevice``); any
other ``tag_id`` is relayed to that tag's ``PAIR`` characteristic. For a relay
the bridge connects at the tag's next advertisement with the §5.2 initiation
rules (its own mesh sends first, the suspend rate limit, a bounded connection
attempt with the mesh suspended; see ``SimBridge._relay_attempt``), reads
``IDENT`` and relays ``PAIR`` messages both ways (fragmented like ``CTRL``, at most
``PAIR_MSG_MAX`` bytes each).

The first message up is the endpoint's ``ident2``; every later one is a
``kind | body`` message. Messages down arrive as ``TUNNEL_DATA`` fragments, and
go up as ``TUNNEL_UP`` fragments, one message after the other. The bridge closes
the tunnel (``TUNNEL_UP`` with the ``CLOSE`` bit, data = status) after
``timeout_s`` without progress (``TIMEOUT``), when the tag drops the link
(``DISCONNECTED``), when the worker's ``PairKind.CLOSE`` ended the session
(``OK``), after a bridge ``RELEASE`` answer (``OK``, then the bridge leaves the
mesh), or when a message cannot be relayed (``INVALID``, ``UNSUPPORTED`` for a tag
without ``IDENT``). A ``TUNNEL_CLOSE`` from the gateway ends it silently.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..protocol.fragments import Fragmenter, FragmentError
from ..protocol.fragments import Reassembler as GattReassembler
from ..protocol.ids import PAIR_MSG_MAX, GattChr, PairKind, Status
from ..protocol.msgs import MeshTunnelData, MeshTunnelUp
from ..secure.messages import FRAG_CLOSE, Reassembler, SecureFrameError, fragments
from .radio import GattLink, LinkLost
from .v2 import PairEndpoint

if TYPE_CHECKING:
    from .bridge import SimBridge


@dataclass
class _Close:
    status: Status | None  # None: end without telling the gateway
    then: Callable[[], None] | None = None


class BridgeTunnel:
    """One tunnel at a bridge (see the module docstring); a bridge holds at most one."""

    def __init__(self, bridge: SimBridge, tunnel: int, tag_id: int, timeout_s: int) -> None:
        self.bridge = bridge
        self.tunnel = tunnel
        self.tag_id = tag_id
        self.timeout_ms = max(1, timeout_s) * 1000.0
        self.rx = Reassembler()
        self.last_ms = bridge.clock.now_ms()
        self.closed = False
        self.close_status: Status | None = None
        self.link: GattLink | None = None
        self.relaying = False  # a connection attempt or relay to the tag is running
        self.worker_closed = False  # the worker sent PairKind.CLOSE down
        self.endpoint: PairEndpoint | None = None
        if tag_id == 0:
            assert bridge.sessions is not None
            self.endpoint = PairEndpoint(bridge.sessions, bridge._tunnel_handler)
        self._up: asyncio.Queue[bytes | _Close] = asyncio.Queue()
        self._down: asyncio.Queue[bytes] = asyncio.Queue()

    @property
    def own(self) -> bool:
        return self.tag_id == 0

    def start(self) -> None:
        tasks = self.bridge._tasks
        tasks.spawn(self._sender(), f"tunnel {self.tunnel} up")
        tasks.spawn(self._watchdog(), f"tunnel {self.tunnel} idle")
        if self.endpoint is not None:
            self._up.put_nowait(self.endpoint.ident())

    def touch(self) -> None:
        self.last_ms = self.bridge.clock.now_ms()

    # -- down (gateway -> endpoint) ---------------------------------------------------------

    def on_data(self, msg: MeshTunnelData) -> None:
        if self.closed:
            return
        self.touch()
        try:
            message = self.rx.feed(msg.seq, msg.flags, msg.data)
        except SecureFrameError:
            self.bridge.counters["tunnel_gaps"] += 1  # the message is lost; the session above it fails
            return
        if message is None:
            return
        self.bridge.counters["tunnel_messages_down"] += 1
        if self.endpoint is None:
            self._down.put_nowait(message)
            return
        result = self.endpoint.receive(message)
        if result.record_changed:
            self.bridge._changed()  # persist, then acknowledge
        for reply in result.replies:
            self._up.put_nowait(reply)
        if result.closed:
            self.close(Status.OK)
        elif result.outcome is not None and result.outcome.released:
            # The RELEASE answer goes up first; then the tunnel closes and the bridge leaves the mesh, locked.
            self.close(Status.OK, then=self.bridge._left_mesh)

    # -- up (endpoint -> gateway) -------------------------------------------------------------

    async def _sender(self) -> None:
        bridge = self.bridge
        while True:
            item = await self._up.get()
            if isinstance(item, _Close):
                if item.status is not None:
                    await bridge._mesh_send(MeshTunnelUp(self.tunnel, 0, FRAG_CLOSE, bytes([int(item.status)])))
                if item.then is not None:
                    item.then()
                return
            for seq, flags, part in fragments(item):
                if not await bridge._mesh_send(MeshTunnelUp(self.tunnel, seq, flags, part)):
                    bridge.counters["tunnel_up_failed"] += 1
                    self.close(Status.TIMEOUT, notify=False)  # the gateway is unreachable: nothing to tell
                    return
            bridge.counters["tunnel_messages_up"] += 1

    async def _watchdog(self) -> None:
        clock = self.bridge.clock
        while not self.closed:
            remaining = self.last_ms + self.timeout_ms - clock.now_ms()
            if remaining <= 0:
                self.close(Status.TIMEOUT)
                return
            await clock.sleep_ms(remaining)

    # -- closing ------------------------------------------------------------------------------

    def close(self, status: Status, *, notify: bool = True, then: Callable[[], None] | None = None) -> None:
        """End the tunnel; with ``notify`` its ``TUNNEL_UP`` CLOSE follows the messages already queued."""
        if self.closed:
            return
        self.closed = True
        self.close_status = status
        bridge = self.bridge
        bridge.counters[f"tunnel_closed_{status.name.lower()}"] += 1
        if bridge.tunnel is self:
            bridge.tunnel = None
        if self.endpoint is not None:
            self.endpoint.drop()
        if self.link is not None:
            self.link.disconnect("bridge closed the tunnel")
        self._up.put_nowait(_Close(status if notify else None, then))

    # -- relay to a tag's PAIR characteristic --------------------------------------------------

    def wants(self, tag_id: int) -> bool:
        """An advertisement of ``tag_id`` should start a connection attempt for this tunnel."""
        return (not self.own and not self.closed and tag_id == self.tag_id and self.link is None
                and not self.relaying)

    async def relay(self, link: GattLink) -> None:
        """The tag accepted the connection: ``IDENT`` up, then ``PAIR`` both ways until the tunnel or the link
        ends."""
        bridge = self.bridge
        self.link = link
        down: asyncio.Task[None] | None = None
        try:
            if self.closed:
                return
            try:
                ident = await link.read(GattChr.IDENT)
            except ValueError:  # no IDENT characteristic: not a v2 tag
                self.close(Status.UNSUPPORTED)
                return
            self.touch()
            self._up.put_nowait(ident)
            bridge.counters["tunnel_relays"] += 1
            down = bridge._tasks.spawn(self._pump_down(link), f"tunnel {self.tunnel} down")
            rx = GattReassembler(PAIR_MSG_MAX)
            while not self.closed:
                chr_, value = await link.central_recv(bridge.clock, self.timeout_ms)
                if chr_ != GattChr.PAIR:
                    raise FragmentError(f"unexpected value on {chr_!r} during a pairing session")
                message = rx.feed(value)
                if message is not None:
                    self.touch()
                    self._up.put_nowait(message)
        except LinkLost:
            self.close(Status.OK if self.worker_closed else Status.DISCONNECTED)
        except TimeoutError:
            self.close(Status.TIMEOUT)
        except FragmentError:
            self.close(Status.INVALID)
        finally:
            if down is not None:
                down.cancel()
            link.disconnect("bridge ended the pairing relay")
            self.link = None

    async def _pump_down(self, link: GattLink) -> None:
        tx = Fragmenter(PAIR_MSG_MAX)
        with contextlib.suppress(LinkLost):
            while True:
                message = await self._down.get()
                if message[:1] == bytes([PairKind.CLOSE]):
                    self.worker_closed = True
                try:
                    values = tx.split(message)
                except FragmentError:  # longer than PAIR_MSG_MAX: a tag cannot take it
                    self.close(Status.INVALID)
                    return
                for value in values:
                    await link.write(GattChr.PAIR, value)
