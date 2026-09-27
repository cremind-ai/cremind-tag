"""Async client for the gateway's serial protocol (docs/protocol.md §1).

Usage::

    client = GatewayClient("COM7")                  # or "socket://127.0.0.1:7777" (simulator)
    client.add_event_handler(on_event)              # register BEFORE connect()
    async with client:                              # HELLO; background tasks start
        ack = await client.deliver_layout(bridge=2, tag_id=..., epoch=3, revision=18,
                                          update_id=501, fontpack_id=pack_id, layout=data)
        if ack.busy: ...                            # queue full: retry later, same op_id is fine

Durability contract for retained events
=======================================

``EVT_PROVISIONED``, ``EVT_NODE_CONFIGURED``, ``EVT_NODE_REMOVED``,
``EVT_ASSIGN_RESULT`` and ``EVT_RESULT`` are *retained* by the gateway until the
host acknowledges them with ``EVENT_ACK {seq}`` (cumulative). This client sends
that ACK **only after every registered event handler has returned successfully
for the event** — so a handler that commits the outcome to the database before
returning guarantees that an acknowledged event is never lost:

1. Retained events are handled strictly one at a time, in ``seq`` order, by a
   single pipeline task. Handlers are awaited in registration order.
2. If a handler raises, the event is **not** acknowledged; the same event is
   offered again after a back-off (0.5 s doubling to 30 s) and later events wait
   behind it (a cumulative ACK cannot skip it).
3. The ACK is sent when the pipeline is idle or every 8 events, for the highest
   ``seq`` whose handlers all succeeded.
4. Delivery is **at least once**: after a reconnect or a crash the gateway
   re-sends everything not yet acknowledged, and an ACK can be lost. Within one
   client this deduplicates by ``(boot_id, seq)``; across process restarts the
   handler must be idempotent (key on ``update_id``/``op_id`` or ``(boot_id, seq)``).
5. Events of an earlier gateway boot still reach the handlers but are never
   acknowledged (their ``seq`` means nothing to the new boot).
6. With no handler registered (or ``ack_events=False``) nothing is ever
   acknowledged: a read-only tool never steals events from the daemon. A handler
   registered with ``types=`` only sees those types; a retained event that no
   registered handler subscribes to counts as handled and is acknowledged with
   the others — register a catch-all handler to persist every retained event.
7. Register handlers before :meth:`GatewayClient.connect`: events that arrive
   earlier are not replayed to handlers added later.

Non-retained events (``EVT_STAGE``, ``EVT_LOG``, ``EVT_UNPROV_BEACON``,
``EVT_TAG_SEEN``, ``EVT_BRIDGE_INFO``) go through the same ordered pipeline but a
handler failure is only logged. :class:`~cremind_tag.gateway.events.SessionStarted`
is queued after every HELLO, so handlers see "the gateway rebooted" in order with
the events around it. Subscriptions (:meth:`GatewayClient.subscribe`,
:meth:`GatewayClient.expect`) observe events as they arrive and never gate the
ACK.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..protocol import cbor_msgs
from ..protocol.ids import LAYOUT_HARD_MAX, MESH_DEFAULT_TTL, TAG_KEY_LEN, SerialMsg, Status, TagCommand
from ..protocol.serial_frame import Frame
from .errors import GatewayError, StatusError
from .events import GatewayEvent, SessionStarted, parse_event
from .link import DEFAULT_ATTEMPTS, HOST_WINDOW, REQUEST_TIMEOUT_S, SerialLink, TransportFactory
from .opid import OpIdGenerator
from .results import Ack, BridgeInfo, DeviceInfo, HelloInfo, NodeInfo, to_status

log = logging.getLogger(__name__)

EventHandler = Callable[[GatewayEvent], Awaitable[None]]
EventTypes = type[GatewayEvent] | tuple[type[GatewayEvent], ...] | None

ACK_BATCH = 8
MAX_PIPELINE = 10_000  # non-retained events beyond this are dropped while a handler is stuck


class EventSubscription:
    """Async iterator of events as they arrive (observer; never gates EVENT_ACK)."""

    def __init__(self, client: GatewayClient, types: EventTypes, maxsize: int) -> None:
        self._client = client
        self._types = types
        self._queue: asyncio.Queue[GatewayEvent] = asyncio.Queue(maxsize)
        self.dropped = 0

    def _offer(self, event: GatewayEvent) -> None:
        if self._types is not None and not isinstance(event, self._types):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def get(self, timeout: float | None = None) -> GatewayEvent:
        return await asyncio.wait_for(self._queue.get(), timeout)

    def __aiter__(self) -> EventSubscription:
        return self

    async def __anext__(self) -> GatewayEvent:
        return await self._queue.get()

    def close(self) -> None:
        self._client._observers.discard(self)

    def __enter__(self) -> EventSubscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventWaiter:
    """Resolves with the first event matching a predicate (register before sending the request)."""

    def __init__(self, client: GatewayClient, predicate: Callable[[GatewayEvent], bool]) -> None:
        self._client = client
        self._predicate = predicate
        self._future: asyncio.Future[GatewayEvent] = asyncio.get_running_loop().create_future()

    def _offer(self, event: GatewayEvent) -> None:
        if self._future.done():
            return
        try:
            matched = self._predicate(event)
        except Exception:
            log.exception("event predicate failed")
            return
        if matched:
            self._future.set_result(event)

    async def wait(self, timeout: float | None = None) -> GatewayEvent:
        try:
            return await asyncio.wait_for(asyncio.shield(self._future), timeout)
        finally:
            if self._future.done():
                self.close()

    def close(self) -> None:
        self._client._observers.discard(self)

    def __enter__(self) -> EventWaiter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
        if not self._future.done():
            self._future.cancel()


class GatewayClient:
    """The companion's connection to one gateway (see the module docstring)."""

    def __init__(
        self,
        url: str,
        *,
        name: str = "cremind-tag",
        request_timeout: float = REQUEST_TIMEOUT_S,
        attempts: int = DEFAULT_ATTEMPTS,
        reconnect: bool = True,
        ack_events: bool = True,
        host_window: int = HOST_WINDOW,
        handler_backoff: tuple[float, float] = (0.5, 30.0),
        op_ids: OpIdGenerator | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.url = url
        self.ack_events = ack_events
        self.handler_backoff = handler_backoff
        self._op_ids = op_ids or OpIdGenerator()
        self.link = SerialLink(url, name=name, label="gateway", request_timeout=request_timeout, attempts=attempts,
                               reconnect=reconnect, host_window=host_window, transport_factory=transport_factory,
                               on_event=self._on_event_frame, on_session=self._on_session)
        self._handlers: list[tuple[EventHandler, EventTypes]] = []
        self._observers: set[EventSubscription | EventWaiter] = set()
        self._pipeline: asyncio.Queue[GatewayEvent] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._ack_task: asyncio.Task[None] | None = None
        self._handled: dict[int, int] = {}  # boot_id -> highest seq whose handlers succeeded
        self._enqueued: dict[int, int] = {}  # boot_id -> highest seq queued for the pipeline
        self._acked: dict[int, int] = {}  # boot_id -> highest seq acknowledged
        self._force_ack: set[int] = set()  # boot_ids whose last ACK must be repeated
        self._unacked = 0
        self._last_boot_id: int | None = None
        self.stats: dict[str, int] = {"events": 0, "retained": 0, "duplicates": 0, "acks": 0, "handler_failures": 0,
                                      "bad_events": 0, "dropped": 0}

    # -- lifecycle ---------------------------------------------------------------

    async def connect(self) -> HelloInfo:
        """Open the port and HELLO (raises ``GatewayError`` if the gateway does not answer)."""
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump(), name="gateway event pipeline")
        return await self.link.open()

    async def close(self) -> None:
        await self.link.close()
        for task in (self._pump_task, self._ack_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._pump_task = self._ack_task = None

    async def __aenter__(self) -> GatewayClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def hello_info(self) -> HelloInfo | None:
        return self.link.session

    @property
    def boot_id(self) -> int | None:
        return self.link.boot_id

    @property
    def connected(self) -> bool:
        return self.link.connected

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until a session is up (e.g. after a reconnect); raises ``TimeoutError``."""
        await self.link.wait_connected(timeout)

    def new_op_id(self) -> int:
        return self._op_ids.next()

    # -- events ------------------------------------------------------------------

    def add_event_handler(self, handler: EventHandler, *, types: EventTypes = None) -> Callable[[], None]:
        """Register an ordered, ACK-gating handler (see the durability contract); returns a remover."""
        entry = (handler, types)
        self._handlers.append(entry)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return remove

    def subscribe(self, types: EventTypes = None, maxsize: int = 1024) -> EventSubscription:
        """Observe events as they arrive (drops, and counts, when ``maxsize`` is exceeded)."""
        sub = EventSubscription(self, types, maxsize)
        self._observers.add(sub)
        return sub

    def expect(self, predicate: Callable[[GatewayEvent], bool]) -> EventWaiter:
        """Wait for a matching event: create before sending the request that triggers it."""
        waiter = EventWaiter(self, predicate)
        self._observers.add(waiter)
        return waiter

    async def drain_events(self) -> None:
        """Wait until every queued event went through the handlers (and pending ACKs were sent)."""
        await self._pipeline.join()
        if self._ack_task is not None:
            with contextlib.suppress(Exception):
                await self._ack_task

    def _observe(self, event: GatewayEvent) -> None:
        for observer in list(self._observers):
            observer._offer(event)

    def _on_event_frame(self, frame: Frame, boot_id: int | None) -> None:
        try:
            event = parse_event(frame.type, frame.payload, boot_id)
        except cbor_msgs.CborError as exc:
            self.stats["bad_events"] += 1
            log.error("gateway: malformed event 0x%02x dropped: %s", frame.type, exc)
            return
        self.stats["events"] += 1
        self._observe(event)
        if event.retained:
            self.stats["retained"] += 1
            boot = event.boot_id or 0
            seq = event.seq or 0
            if seq <= self._handled.get(boot, 0):
                self.stats["duplicates"] += 1
                self._force_ack.add(boot)  # re-sent although handled: our ACK was lost, repeat it
                self._schedule_ack()
                return
            if seq <= self._enqueued.get(boot, 0):
                self.stats["duplicates"] += 1
                return
            self._enqueued[boot] = seq
            self._pipeline.put_nowait(event)
        elif self._pipeline.qsize() < MAX_PIPELINE:
            self._pipeline.put_nowait(event)
        else:
            self.stats["dropped"] += 1

    def _on_session(self, hello: HelloInfo, previous: HelloInfo | None) -> None:
        previous_boot = previous.boot_id if previous is not None else self._last_boot_id
        self._last_boot_id = hello.boot_id
        if previous_boot is not None and previous_boot != hello.boot_id:
            log.warning("gateway: boot_id %08x -> %08x: the gateway restarted and lost its in-flight state",
                        previous_boot, hello.boot_id)
        event = SessionStarted(boot_id=hello.boot_id, hello=hello, previous_boot_id=previous_boot)
        self._observe(event)
        self._pipeline.put_nowait(event)

    def _matching_handlers(self, event: GatewayEvent) -> list[EventHandler]:
        return [h for h, types in self._handlers if types is None or isinstance(event, types)]

    async def _pump(self) -> None:
        while True:
            event = await self._pipeline.get()
            try:
                if event.retained:
                    boot, seq = event.boot_id or 0, event.seq or 0
                    if seq > self._handled.get(boot, 0):
                        await self._handle_retained(event)
                        self._handled[boot] = seq
                        self._unacked += 1
                else:
                    for handler in self._matching_handlers(event):
                        try:
                            await handler(event)
                        except Exception:
                            self.stats["handler_failures"] += 1
                            log.exception("gateway: handler failed for %s", type(event).__name__)
                if self._unacked and (self._pipeline.empty() or self._unacked >= ACK_BATCH):
                    self._unacked = 0
                    self._schedule_ack()
            finally:
                self._pipeline.task_done()

    async def _handle_retained(self, event: GatewayEvent) -> None:
        delay, max_delay = self.handler_backoff
        while True:
            try:
                for handler in self._matching_handlers(event):
                    await handler(event)
                return
            except Exception:
                self.stats["handler_failures"] += 1
                log.exception("gateway: handler failed for %s seq %s; not acknowledged, retrying in %.1f s",
                              type(event).__name__, event.seq, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    def _schedule_ack(self) -> None:
        if self._ack_task is None or self._ack_task.done():
            self._ack_task = asyncio.create_task(self._send_acks(), name="gateway event ack")

    async def _send_acks(self) -> None:
        # Loops so that progress made while an ACK was in flight is acknowledged too.
        while self.ack_events and self._handlers and self.link.connected:
            boot = self.link.boot_id
            if boot is None:
                return
            seq = self._handled.get(boot, 0)
            if seq == 0 or (seq <= self._acked.get(boot, 0) and boot not in self._force_ack):
                return
            self._force_ack.discard(boot)
            try:
                ack = await self.event_ack(seq)
            except GatewayError as exc:
                log.info("gateway: EVENT_ACK %d not delivered (%s); the gateway will re-send", seq, exc)
                return
            if ack.status != Status.OK:
                log.warning("gateway: EVENT_ACK %d answered %s", seq, ack.status)
                return
            self.stats["acks"] += 1
            self._acked[boot] = max(seq, self._acked.get(boot, 0))

    # -- requests ----------------------------------------------------------------

    async def _ack(self, msg: SerialMsg, fields: dict[str, Any], op_id: int | None = None,
                   timeout: float | None = None) -> Ack:
        response = await self.link.request(msg, fields, timeout=timeout)
        return Ack.from_fields(msg, response, op_id)

    async def _checked(self, msg: SerialMsg, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.link.request(msg, fields or {})
        status = to_status(response["status"])
        if status != Status.OK:
            raise StatusError(msg, status, response.get("detail"), response.get("text"))
        return response

    def _op(self, op_id: int | None) -> int:
        return self._op_ids.next() if op_id is None else op_id

    async def hello(self) -> HelloInfo:
        """Re-HELLO now (resynchronise credits; the gateway re-sends retained events)."""
        return await self.link.resync("hello")

    async def ping(self) -> int:
        """``PING`` -> the gateway's uptime in seconds."""
        return int((await self._checked(SerialMsg.PING)).get("uptime_s", 0))

    async def info(self) -> DeviceInfo:
        return DeviceInfo.from_fields(await self._checked(SerialMsg.INFO))

    async def get_counters(self) -> dict[str, int]:
        return dict((await self._checked(SerialMsg.GET_COUNTERS)).get("counters", {}))

    async def reboot(self, *, op_id: int | None = None) -> Ack:
        op = self._op(op_id)
        return await self._ack(SerialMsg.REBOOT, {"op_id": op}, op)

    async def event_ack(self, seq: int) -> Ack:
        """Release retained events with ``seq`` <= ``seq`` (normally sent by the pipeline)."""
        return await self._ack(SerialMsg.EVENT_ACK, {"seq": seq})

    async def scan_unprov(self, duration_s: int, uuid_filter: bytes | None = None) -> Ack:
        """Scan for unprovisioned bridges; beacons arrive as ``UnprovBeacon`` events."""
        fields: dict[str, Any] = {"duration_s": duration_s}
        if uuid_filter:
            fields["uuid_filter"] = uuid_filter
        return await self._ack(SerialMsg.SCAN_UNPROV, fields)

    async def provision(self, uuid: bytes, name: str | None = None, *, op_id: int | None = None) -> Ack:
        """Provision the bridge the operator picked; the outcome is a ``Provisioned`` event."""
        op = self._op(op_id)
        fields: dict[str, Any] = {"op_id": op, "uuid": uuid}
        if name:
            fields["name"] = name
        return await self._ack(SerialMsg.PROVISION, fields, op)

    async def configure_node(self, addr: int, *, relay: bool = True, ttl: int = MESH_DEFAULT_TTL,
                             op_id: int | None = None) -> Ack:
        """App key + model binding + relay + TTL; the outcome is a ``NodeConfigured`` event."""
        op = self._op(op_id)
        return await self._ack(SerialMsg.CONFIGURE_NODE, {"op_id": op, "addr": addr, "relay": relay, "ttl": ttl}, op)

    async def remove_node(self, addr: int, *, op_id: int | None = None) -> Ack:
        """Node reset + CDB delete; the outcome is a ``NodeRemoved`` event."""
        op = self._op(op_id)
        return await self._ack(SerialMsg.REMOVE_NODE, {"op_id": op, "addr": addr}, op)

    async def list_nodes(self) -> list[NodeInfo]:
        return [NodeInfo.from_map(n) for n in (await self._checked(SerialMsg.LIST_NODES)).get("nodes", [])]

    async def identify_node(self, addr: int, *, op_id: int | None = None) -> Ack:
        op = self._op(op_id)
        return await self._ack(SerialMsg.IDENTIFY_NODE, {"op_id": op, "addr": addr}, op)

    async def get_inventory(self) -> list[BridgeInfo]:
        """Cached bridge info; fresher data follows as ``BridgeInfoEvent``s."""
        return [BridgeInfo.from_map(i) for i in (await self._checked(SerialMsg.GET_INVENTORY)).get("items", [])]

    async def assign_tag(self, bridge: int, tag_id: int, epoch: int, key: bytes, *, op_id: int | None = None) -> Ack:
        """Give ``K_epoch`` for ``(tag, epoch)`` to a bridge; the outcome is an ``AssignResult`` event."""
        if len(key) != TAG_KEY_LEN:
            raise ValueError(f"K_epoch must be {TAG_KEY_LEN} bytes")
        op = self._op(op_id)
        return await self._ack(SerialMsg.ASSIGN_TAG,
                               {"op_id": op, "bridge": bridge, "tag_id": tag_id, "epoch": epoch, "key": key}, op)

    async def unassign_tag(self, bridge: int, tag_id: int, epoch: int, *, op_id: int | None = None) -> Ack:
        op = self._op(op_id)
        return await self._ack(SerialMsg.UNASSIGN_TAG,
                               {"op_id": op, "bridge": bridge, "tag_id": tag_id, "epoch": epoch}, op)

    async def deliver_layout(self, *, bridge: int, tag_id: int, epoch: int, revision: int, update_id: int,
                             fontpack_id: bytes, layout: bytes, op_id: int | None = None) -> Ack:
        """Queue a layout (§1.5). ``ACCEPTED`` = stage ``GATEWAY_RECEIVED``; ``BUSY`` = retry later.

        The final outcome is exactly one ``ResultEvent`` with this ``update_id``.
        Raises ``FrameTooLargeError`` when the layout plus its envelope does not
        fit a serial frame (a hair below ``LAYOUT_HARD_MAX``).
        """
        if len(layout) > LAYOUT_HARD_MAX:
            raise ValueError(f"layout of {len(layout)} bytes exceeds LAYOUT_HARD_MAX")
        op = self._op(op_id)
        return await self._ack(SerialMsg.DELIVER_LAYOUT, {
            "op_id": op, "bridge": bridge, "tag_id": tag_id, "epoch": epoch, "revision": revision,
            "update_id": update_id, "fontpack_id": fontpack_id, "layout": layout}, op)

    async def cancel_delivery(self, update_id: int, *, op_id: int | None = None) -> Ack:
        """Cancel a queued/pending delivery; a cancelled one ends with ``ResultEvent`` CANCELLED."""
        op = self._op(op_id)
        return await self._ack(SerialMsg.CANCEL_DELIVERY, {"op_id": op, "update_id": update_id}, op)

    async def tag_command(self, *, bridge: int, tag_id: int, epoch: int, cmd: TagCommand | int,
                          op_id: int | None = None) -> Ack:
        """Send a tag command; the outcome is a ``ResultEvent`` whose ``update_id`` is ``op_id``."""
        op = self._op(op_id)
        return await self._ack(SerialMsg.TAG_COMMAND,
                               {"op_id": op, "bridge": bridge, "tag_id": tag_id, "epoch": epoch, "cmd": int(cmd)}, op)


def matches(event_type: type[GatewayEvent], **fields: Any) -> Callable[[GatewayEvent], bool]:
    """Predicate for :meth:`GatewayClient.expect`: type and attribute equality."""

    def predicate(event: GatewayEvent) -> bool:
        return isinstance(event, event_type) and all(getattr(event, k, None) == v for k, v in fields.items())

    return predicate


def any_of(*predicates: Callable[[GatewayEvent], bool]) -> Callable[[GatewayEvent], bool]:
    return lambda event: any(p(event) for p in predicates)


__all__ = ["EventHandler", "EventSubscription", "EventWaiter", "GatewayClient", "any_of", "matches"]
