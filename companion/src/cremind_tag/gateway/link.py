"""Host side of the serial protocol (docs/protocol.md §1.1–§1.3), shared by the
gateway client and the bridge maintenance-port client.

What this layer does:

- **HELLO** opens every session. It is exempt from credit accounting (it resets
  the link): the request's ``credits`` byte grants the device
  ``host_window - SERIAL_DEFAULT_CREDITS`` frames on top of the default window,
  and the host's own window becomes the response's ``caps.credits`` (default
  ``SERIAL_DEFAULT_CREDITS``) plus that response's grant byte. Grants carried by
  frames that arrive before the HELLO response belong to the previous session and
  are ignored.
- **Credits** (§1.3): every other frame costs one device credit; the ``credits``
  byte of every received frame is added back. The host owes the device one grant
  per frame it received and piggybacks what it owes on its next request; when it
  owes half a window and has nothing to send it sends a PING carrying the grant.
  Ordinary requests leave one credit in reserve for that PING, so the two sides
  can never both sit at zero credits.
- **Requests** get a fresh non-zero ``request_id`` per transmission and are
  matched by it. Each attempt waits ``request_timeout`` (2 s, §1.3); a timeout
  triggers a **resync** (a new HELLO, which also repairs credit accounting after
  a frame was lost) and every unanswered request is sent again *with the same
  payload* — so the same ``op_id`` (§1.4) — up to ``attempts`` times.
- **Link loss**: pending requests fail with ``GatewayDisconnected``; with
  ``reconnect=True`` the link reopens the port with back-off and HELLOs again.

Events are handed to ``on_event`` as raw frames; retained-event semantics live in
:class:`~cremind_tag.gateway.client.GatewayClient`.

**Protocol v2 secure sessions** (docs/connect-setup.md §3.2, §5), with
``secure=SecureOptions(...)``: after every HELLO the link sends the plaintext
``IDENTIFY`` (checking the device is the pinned one, when one is pinned), opens
a Noise IK session with ``SECURE_OPEN``, and from then on every request goes
out as a ``SECURE_DATA`` frame carrying the sealed ``type | flags |
request_id | CBOR`` message; every sealed response and event is opened and
handled exactly like a plaintext frame, so the client above never notices.
The outer frame of a sealed message has ``request_id = 0`` and ``flags = 0``;
the inner header is authoritative (§5). The session is installed while the
``SECURE_OPEN`` answer is handled, before the sealed retained events the
device sends right after it. A frame that does not open — or the device's
plaintext refusal (a ``SECURE_DATA`` *response* ``{status}``) — ends the
session: the link resynchronises (HELLO, IDENTIFY, SECURE_OPEN) and re-sends
what was pending with the same payloads, so the same ``op_id``\\ s.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..protocol import cbor_msgs
from ..protocol.ids import (
    PROTO_VERSION,
    SECURE_PROTO_VERSION,
    SERIAL_CRC_LEN,
    SERIAL_DEFAULT_CREDITS,
    SERIAL_HEADER_LEN,
    SERIAL_MAX_FRAME,
    Link,
    SerialFlag,
    SerialMsg,
    Status,
)
from ..protocol.serial_frame import Frame
from ..secure.channel import SecureChannel, SecureChannelError
from ..secure.messages import SecureFrameError, SecureMessage
from .errors import FrameTooLargeError, GatewayDisconnected, GatewayError, GatewayTimeout, ProtocolError, StatusError
from .results import HelloInfo, IdentifyInfo, to_status
from .transport import SerialTransport, TransportClosed

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 2.0  # §1.3
DEFAULT_ATTEMPTS = 3
HOST_WINDOW = 64  # frames the device may send before the host's next grant

TransportFactory = Callable[[str], SerialTransport]
EventSink = Callable[[Frame, int | None], None]
SessionSink = Callable[[HelloInfo, "HelloInfo | None"], None]
# Sealing adds the inner header (4), the Poly1305 tag (16) and the CBOR wrapper.
SECURE_OVERHEAD = 32


class WrongDeviceError(ProtocolError):
    """The device on this port is not the one this link is pinned to."""


@dataclass(frozen=True)
class SecureOptions:
    """A v2 secure session toward the device (docs/connect-setup.md §3.2).

    ``controller_priv`` is the worker's X25519 key (the Noise initiator's
    static key). ``expect_device_id`` / ``expect_ik`` pin the device: a
    different one on the port is refused (:class:`WrongDeviceError`) before
    any secret is sent. ``role`` is the role the device must report.
    ``on_identify`` sees every IDENTIFY answer (its fresh challenge included).
    """

    controller_priv: bytes
    expect_device_id: bytes | None = None
    expect_ik: bytes | None = None
    role: int | None = None
    on_identify: Callable[[IdentifyInfo], None] | None = None


@dataclass(eq=False)
class _Request:
    msg: SerialMsg
    payload: bytes
    timeout: float
    max_attempts: int
    order: int
    future: asyncio.Future[Frame]
    internal: bool = False  # a grant-carrying PING: may use the reserved credit
    attempts: int = 0
    sent_generation: int = -1
    sent: asyncio.Event = field(default_factory=asyncio.Event)
    request_ids: list[int] = field(default_factory=list)


class SerialLink:
    """One logical connection to a serial device (see the module docstring)."""

    def __init__(
        self,
        url: str,
        *,
        name: str = "cremind-tag",
        label: str = "device",
        request_timeout: float = REQUEST_TIMEOUT_S,
        attempts: int = DEFAULT_ATTEMPTS,
        hello_timeout: float | None = None,
        reconnect: bool = False,
        reconnect_delays: tuple[float, float] = (0.25, 5.0),
        host_window: int = HOST_WINDOW,
        transport_factory: TransportFactory | None = None,
        on_event: EventSink | None = None,
        on_session: SessionSink | None = None,
        secure: SecureOptions | None = None,
    ) -> None:
        if not SERIAL_DEFAULT_CREDITS <= host_window <= SERIAL_DEFAULT_CREDITS + 255:
            raise ValueError("host_window out of range")
        self.url = url
        self.name = name
        self.label = label
        self.request_timeout = request_timeout
        self.attempts = attempts
        self.hello_timeout = hello_timeout if hello_timeout is not None else request_timeout
        self.reconnect = reconnect
        self.reconnect_delays = reconnect_delays
        self.host_window = host_window
        self._factory = transport_factory or (lambda u: SerialTransport(u))
        self._on_event = on_event
        self._on_session = on_session

        self._transport: SerialTransport | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._ready = asyncio.Event()
        self._closed = False
        self._opened = False  # reconnect only after a first successful open()
        self._credits = 0
        self._owed = 0
        self._reserve = 1
        self._max_frame = SERIAL_MAX_FRAME
        self._credit_changed = asyncio.Event()
        self._outbox: deque[_Request] = deque()
        self._outbox_changed = asyncio.Event()
        self._by_rid: dict[int, _Request] = {}
        self._pending: dict[int, _Request] = {}
        self._order = itertools.count()
        self._next_rid = 0
        self._generation = 0
        self._resync_lock = asyncio.Lock()
        self._hello_rid: int | None = None
        self._hello_future: asyncio.Future[HelloInfo] | None = None
        self._awaiting_hello = False
        self._grant_ping_outstanding = False
        self._session: HelloInfo | None = None
        self._secure = secure
        self._channel: SecureChannel | None = None
        self._identify: IdentifyInfo | None = None
        # One plaintext setup request at a time during the handshake (IDENTIFY, SECURE_OPEN).
        self._aux_rid: int | None = None
        self._aux_msg: SerialMsg | None = None
        self._aux_future: asyncio.Future[Frame] | None = None
        self._aux_on_answer: Callable[[Frame], None] | None = None
        self.stats: dict[str, int] = {"resyncs": 0, "timeouts": 0, "reconnects": 0, "unmatched": 0,
                                      "grant_pings": 0, "frames_rx": 0, "frames_tx": 0, "secure_failures": 0}

    # -- properties ------------------------------------------------------------

    @property
    def session(self) -> HelloInfo | None:
        """The latest HELLO answer (``None`` before the first)."""
        return self._session

    @property
    def boot_id(self) -> int | None:
        return self._session.boot_id if self._session else None

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    @property
    def credits(self) -> int:
        return self._credits

    @property
    def owed(self) -> int:
        return self._owed

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def max_frame(self) -> int:
        return self._max_frame

    @property
    def identify(self) -> IdentifyInfo | None:
        """The device's latest IDENTIFY answer (secure links only)."""
        return self._identify

    @property
    def secure(self) -> bool:
        return self._secure is not None

    @property
    def handshake_hash(self) -> bytes | None:
        """The current Noise session's handshake hash (proofs bind to it)."""
        return self._channel.handshake_hash if self._channel is not None and self._channel.open else None

    @property
    def channel(self) -> SecureChannel | None:
        return self._channel

    def transport_stats(self) -> dict[str, int]:
        return self._transport.stats() if self._transport is not None else {}

    # -- lifecycle ---------------------------------------------------------------

    async def open(self) -> HelloInfo:
        """Open the port and HELLO; raises ``GatewayError`` when the device does not answer."""
        if self._closed:
            raise GatewayDisconnected("link closed")
        await self._connect()
        self._opened = True
        assert self._session is not None
        return self._session

    async def _connect(self) -> None:
        transport = self._factory(self.url)
        await transport.open()
        self._transport = transport
        self._reader_task = asyncio.create_task(self._reader(transport), name=f"{self.label} reader")
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender(), name=f"{self.label} sender")
        try:
            async with self._resync_lock:
                await self._resync_locked("connect")
        except BaseException:
            if self._transport is transport:
                self._transport = None
            await transport.close()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready.clear()
        tasks = [t for t in (self._reconnect_task, self._sender_task, self._reader_task, *self._background) if t]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._fail_all(GatewayDisconnected("link closed"))
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # -- receiving ---------------------------------------------------------------

    async def _reader(self, transport: SerialTransport) -> None:
        try:
            while True:
                frame = await transport.recv()
                self._on_frame(frame)
        except TransportClosed as exc:
            if transport is self._transport:
                self._connection_lost(exc)

    def _on_frame(self, frame: Frame) -> None:
        self.stats["frames_rx"] += 1
        if frame.type == SerialMsg.SECURE_DATA and self._secure is not None:
            inner = self._unseal(frame)
            if inner is None:
                self._account(frame)
                self._maybe_grant()
                return
            frame = inner
        elif (frame.flags & SerialFlag.RESPONSE and self._aux_future is not None
              and frame.request_id == self._aux_rid and frame.type == self._aux_msg):
            self._account(frame)
            if not self._aux_future.done():
                if self._aux_on_answer is not None:
                    try:
                        self._aux_on_answer(frame)  # e.g. install the session before the next frame
                    except Exception:
                        log.exception("%s: %s answer handler failed", self.label, frame.type)
                self._aux_future.set_result(frame)
            return
        if frame.flags & SerialFlag.RESPONSE:
            if frame.type == SerialMsg.HELLO:  # exempt from credits; a late answer to an old HELLO is ignored
                future = self._hello_future
                if future is not None and frame.request_id == self._hello_rid and not future.done():
                    # Installed right here, before the reader handles the next frame: the retained
                    # events the device re-sends after its HELLO answer belong to the new session.
                    try:
                        future.set_result(self._install_session(frame))
                    except GatewayError as exc:
                        future.set_exception(exc)
                else:
                    self.stats["unmatched"] += 1
                return
            self._account(frame)
            req = self._by_rid.get(frame.request_id)
            if req is not None and req.msg == frame.type and not req.future.done():
                req.future.set_result(frame)
            else:
                self.stats["unmatched"] += 1
        elif frame.flags & SerialFlag.EVENT:
            self._account(frame)
            if self._on_event is not None:
                try:
                    self._on_event(frame, self.boot_id)
                except Exception:
                    log.exception("%s: event sink failed", self.label)
        else:
            self.stats["unmatched"] += 1
        self._maybe_grant()

    def _unseal(self, frame: Frame) -> Frame | None:
        """The inner frame of a ``SECURE_DATA`` frame, or ``None`` after ending
        a session that failed (a plaintext refusal, a frame that does not open)."""
        try:
            fields = cbor_msgs.decode_map(frame.payload)
        except cbor_msgs.CborError:
            fields = {}
        data = fields.get("data")
        channel = self._channel
        refused = bool(frame.flags & SerialFlag.RESPONSE)  # the device answers a failed session in plaintext
        if refused or not isinstance(data, bytes) or channel is None or not channel.open:
            status = fields.get("status")
            log.info("%s: secure session refused (%s); resynchronising", self.label,
                     Status(status).name if isinstance(status, int) and status in Status._value2member_map_ else status)
            self.stats["secure_failures"] += 1
            self._channel = None
            self._schedule_resync("secure session refused")
            return None
        try:
            msg = channel.unseal(data)
        except (SecureChannelError, SecureFrameError) as exc:  # bad ciphertext, or a malformed inner header
            log.warning("%s: a sealed frame did not open (%s); resynchronising", self.label, exc)
            self.stats["secure_failures"] += 1
            self._channel = None
            self._schedule_resync("secure frame failed")
            return None
        try:
            mtype = SerialMsg(msg.type)
        except ValueError:
            mtype = msg.type  # type: ignore[assignment]  # unknown inner type: counted as unmatched below
        return Frame(mtype, msg.request_id, msg.payload, msg.flags, frame.credits, frame.version)

    def _seal(self, msg: SerialMsg, rid: int, payload: bytes, grant: int) -> Frame:
        channel = self._channel
        if self._secure is None or msg == SerialMsg.HELLO:
            return Frame(msg, rid, payload, 0, grant)
        if channel is None or not channel.open:
            raise GatewayDisconnected(f"{self.label}: no secure session")
        sealed = channel.seal(SecureMessage(int(msg), 0, rid, payload))
        return Frame(SerialMsg.SECURE_DATA, 0, cbor_msgs.encode_map({"data": sealed}), 0, grant)

    def _account(self, frame: Frame) -> None:
        if self._awaiting_hello:
            return  # a frame of the previous session: its grant no longer applies
        self._credits += frame.credits
        self._owed += 1  # the frame's receive buffer is free again as soon as it is dispatched
        if frame.credits:
            self._credit_changed.set()

    def _maybe_grant(self) -> None:
        if (not self._ready.is_set() or self._grant_ping_outstanding or self._owed < max(1, self.host_window // 2)
                or any(not r.internal for r in self._outbox)):
            return
        self._grant_ping_outstanding = True
        self.stats["grant_pings"] += 1
        req = self._new_request(SerialMsg.PING, b"", self.request_timeout, 1, internal=True)

        async def run() -> None:
            try:
                await self._roundtrip(req)
            except GatewayError:
                pass
            finally:
                self._grant_ping_outstanding = False

        self._spawn(run(), f"{self.label} grant ping")

    # -- sending -----------------------------------------------------------------

    def _alloc_rid(self) -> int:
        for _ in range(0xFFFF):
            self._next_rid = self._next_rid % 0xFFFF + 1
            if self._next_rid not in self._by_rid and self._next_rid != self._hello_rid:
                return self._next_rid
        raise GatewayError("no free request id")

    async def _sender(self) -> None:
        while True:
            await self._ready.wait()
            if not self._outbox:
                self._outbox_changed.clear()
                await self._outbox_changed.wait()
                continue
            req = self._outbox[0]
            if req.future.done():
                self._outbox.popleft()
                continue
            need = 1 if req.internal else 1 + self._reserve
            if self._credits < need:
                self._credit_changed.clear()
                try:
                    await asyncio.wait_for(self._credit_changed.wait(), self.request_timeout)
                except TimeoutError:
                    log.warning("%s: no credit granted for %.1f s; resynchronising", self.label, self.request_timeout)
                    self._schedule_resync("credit stall")
                continue
            transport = self._transport
            if not self._ready.is_set() or transport is None:
                continue
            self._outbox.popleft()
            rid = self._alloc_rid()
            grant = min(self._owed, 255)
            try:
                # Sealed here, at send time: nonces must follow the wire order.
                out = self._seal(req.msg, rid, req.payload, grant)
            except GatewayError:
                self._outbox.appendleft(req)
                self._schedule_resync("no secure session")
                continue
            self._owed -= grant
            self._credits -= 1
            req.request_ids.append(rid)
            self._by_rid[rid] = req
            req.attempts += 1
            req.sent_generation = self._generation
            try:
                await transport.send(out)
            except TransportClosed as exc:
                if transport is self._transport:
                    self._connection_lost(exc)
                continue
            self.stats["frames_tx"] += 1
            req.sent.set()

    def _new_request(self, msg: SerialMsg, payload: bytes, timeout: float, attempts: int,
                     internal: bool = False) -> _Request:
        size = SERIAL_HEADER_LEN + len(payload) + SERIAL_CRC_LEN + (SECURE_OVERHEAD if self._secure else 0)
        if size > self._max_frame:
            raise FrameTooLargeError(f"{msg.name} frame of {size} bytes exceeds max_frame {self._max_frame}")
        loop = asyncio.get_running_loop()
        req = _Request(msg, payload, timeout, max(1, attempts), next(self._order), loop.create_future(), internal)
        self._pending[req.order] = req
        self._outbox.append(req)
        self._outbox_changed.set()
        return req

    async def request_frame(self, msg: SerialMsg, payload: bytes = b"", *, timeout: float | None = None,
                            attempts: int | None = None) -> Frame:
        """Send one request and return its response frame (retrying per the module rules)."""
        if self._closed:
            raise GatewayDisconnected("link closed")
        req = self._new_request(msg, payload, timeout or self.request_timeout,
                                self.attempts if attempts is None else attempts)
        return await self._roundtrip(req)

    async def _roundtrip(self, req: _Request) -> Frame:
        loop = asyncio.get_running_loop()
        send_deadline = loop.time() + req.timeout * req.max_attempts
        try:
            while True:
                await self._wait_sent(req, send_deadline - loop.time())
                generation = req.sent_generation
                try:
                    return await asyncio.wait_for(asyncio.shield(req.future), req.timeout)
                except TimeoutError:
                    if req.future.done():
                        return req.future.result()
                    self.stats["timeouts"] += 1
                    if req.attempts >= req.max_attempts:
                        raise GatewayTimeout(f"{req.msg.name}: no response after {req.attempts} attempt(s)") from None
                    log.info("%s: %s timed out (attempt %d/%d); resynchronising", self.label, req.msg.name,
                             req.attempts, req.max_attempts)
                    await self._resync_after(generation, f"{req.msg.name} timeout")
                    send_deadline = loop.time() + req.timeout * (req.max_attempts - req.attempts + 1)
        finally:
            self._pending.pop(req.order, None)
            for rid in req.request_ids:
                if self._by_rid.get(rid) is req:
                    del self._by_rid[rid]
            with contextlib.suppress(ValueError):
                self._outbox.remove(req)
            if not req.future.done():
                req.future.cancel()
            elif not req.future.cancelled():
                req.future.exception()  # mark retrieved

    async def _wait_sent(self, req: _Request, budget: float) -> None:
        if req.sent.is_set():
            return
        waiter = asyncio.ensure_future(req.sent.wait())
        try:
            pending: set[asyncio.Future[Any]] = {waiter, req.future}
            await asyncio.wait(pending, timeout=max(0.0, budget), return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if req.future.done():
            req.future.result()  # raises the failure (e.g. disconnected)
            return
        if not req.sent.is_set():
            if not self._ready.is_set():
                raise GatewayDisconnected(f"{self.label} {self.url} is not connected")
            raise GatewayTimeout(f"{req.msg.name}: could not be sent (no credit)")

    async def request(self, msg: SerialMsg, fields: Mapping[str, Any] | None = None, *,
                      timeout: float | None = None, attempts: int | None = None) -> dict[str, Any]:
        """Encode, send, and decode the response fields (``status`` always present)."""
        payload = cbor_msgs.encode_request(msg, dict(fields or {}))
        frame = await self.request_frame(msg, payload, timeout=timeout, attempts=attempts)
        try:
            return cbor_msgs.decode_response(msg, frame.payload)
        except cbor_msgs.CborError as exc:
            raise ProtocolError(f"{msg.name} response: {exc}") from None

    # -- resynchronisation ---------------------------------------------------------

    def _schedule_resync(self, reason: str) -> None:
        if self._closed or self._transport is None:
            return
        self._ready.clear()

        async def run() -> None:
            with contextlib.suppress(GatewayError):
                await self.resync(reason)

        self._spawn(run(), f"{self.label} resync")

    async def resync(self, reason: str = "requested") -> HelloInfo:
        """Send HELLO again; unanswered requests are re-sent afterwards."""
        async with self._resync_lock:
            return await self._resync_locked(reason)

    async def _resync_after(self, generation: int, reason: str) -> None:
        async with self._resync_lock:
            if self._generation != generation:
                return  # someone resynced since this request was sent; it was requeued then
            with contextlib.suppress(GatewayError):
                await self._resync_locked(reason)

    async def _resync_locked(self, reason: str) -> HelloInfo:
        if self._transport is None:
            raise GatewayDisconnected(f"{self.label} {self.url} is not connected")
        self._ready.clear()
        self._channel = None  # HELLO ends any secure session on the device too
        try:
            hello = await self._handshake()
            if self._secure is not None:
                await self._secure_open()
                self._ready.set()
                self._outbox_changed.set()
                self._credit_changed.set()
        except GatewayError as exc:
            self._connection_lost(exc)
            raise
        log.debug("%s: session %d (%s), boot_id %08x", self.label, self._generation, reason, hello.boot_id)
        return hello

    async def _handshake(self) -> HelloInfo:
        transport = self._transport
        if transport is None:
            raise GatewayDisconnected("not connected")
        payload = cbor_msgs.encode_request(SerialMsg.HELLO, {"proto": PROTO_VERSION, "name": self.name})
        grant = min(255, self.host_window - SERIAL_DEFAULT_CREDITS)
        loop = asyncio.get_running_loop()
        self._awaiting_hello = True
        try:
            for attempt in range(1, self.attempts + 1):
                rid = self._alloc_rid()
                future: asyncio.Future[HelloInfo] = loop.create_future()
                self._hello_rid, self._hello_future = rid, future
                try:
                    await transport.send(Frame(SerialMsg.HELLO, rid, payload, 0, grant))
                    return await asyncio.wait_for(future, self.hello_timeout)
                except TimeoutError:
                    log.info("%s: HELLO attempt %d timed out", self.label, attempt)
                finally:
                    self._hello_rid, self._hello_future = None, None
        finally:
            self._awaiting_hello = False
        raise GatewayTimeout(f"{self.label} {self.url}: no HELLO response")

    async def _aux_request(self, msg: SerialMsg, fields: dict[str, Any], *,
                           on_answer: Callable[[Frame], None] | None = None,
                           attempts: int | None = None) -> dict[str, Any]:
        """A plaintext request of the secure handshake, sent while the link is
        not ready (so outside the outbox). Costs one device credit.
        ``on_answer`` runs inside the frame handler, before the next frame."""
        transport = self._transport
        if transport is None:
            raise GatewayDisconnected("not connected")
        payload = cbor_msgs.encode_request(msg, fields)
        loop = asyncio.get_running_loop()
        for attempt in range(1, (attempts or self.attempts) + 1):
            if self._credits < 1:
                raise GatewayTimeout(f"{msg.name}: no credit during the secure handshake")
            rid = self._alloc_rid()
            future: asyncio.Future[Frame] = loop.create_future()
            self._aux_rid, self._aux_msg, self._aux_future = rid, msg, future
            self._aux_on_answer = on_answer
            grant = min(self._owed, 255)
            self._owed -= grant
            self._credits -= 1
            try:
                await transport.send(Frame(msg, rid, payload, 0, grant))
                frame = await asyncio.wait_for(future, self.request_timeout)
            except TimeoutError:
                log.info("%s: %s attempt %d timed out", self.label, msg.name, attempt)
                continue
            except TransportClosed as exc:
                raise GatewayDisconnected(str(exc)) from None
            finally:
                self._aux_rid, self._aux_msg, self._aux_future = None, None, None
                self._aux_on_answer = None
            try:
                return cbor_msgs.decode_response(msg, frame.payload)
            except cbor_msgs.CborError as exc:
                raise ProtocolError(f"{msg.name} response: {exc}") from None
        raise GatewayTimeout(f"{self.label} {self.url}: no {msg.name} response")

    async def _secure_open(self) -> None:
        """IDENTIFY, pin check, SECURE_OPEN (docs/connect-setup.md §5)."""
        opts = self._secure
        assert opts is not None
        fields = await self._aux_request(SerialMsg.IDENTIFY, {})
        status = to_status(fields["status"])
        if status == Status.UNSUPPORTED:
            raise ProtocolError(f"{self.label} {self.url} runs protocol v1 firmware (no IDENTIFY)")
        if status != Status.OK:
            raise StatusError(SerialMsg.IDENTIFY, status, fields.get("detail"), fields.get("text"))
        try:
            ident = IdentifyInfo.from_fields(fields)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(f"IDENTIFY response: {exc}") from None
        if ident.proto < SECURE_PROTO_VERSION:
            raise ProtocolError(f"{self.label} speaks secure protocol {ident.proto}")
        if opts.role is not None and ident.role != opts.role:
            raise WrongDeviceError(f"{self.label} {self.url}: the device reports role {ident.role}")
        if ((opts.expect_device_id is not None and ident.device_id != opts.expect_device_id)
                or (opts.expect_ik is not None and ident.ik != opts.expect_ik)):
            raise WrongDeviceError(f"{self.label} {self.url}: another device is on this port")
        self._identify = ident
        if opts.on_identify is not None:
            try:
                opts.on_identify(ident)
            except Exception:
                log.exception("%s: identify callback failed", self.label)
        channel = SecureChannel(opts.controller_priv, ident.ik, ident.device_id, Link.SERIAL)
        failure: list[str] = []

        def install(frame: Frame) -> None:
            # Right here, before the reader handles the next frame: the device re-sends its
            # retained events sealed in the new session immediately after this answer.
            try:
                fields = cbor_msgs.decode_response(SerialMsg.SECURE_OPEN, frame.payload)
            except cbor_msgs.CborError as exc:
                failure.append(str(exc))
                return
            if to_status(fields["status"]) == Status.OK and isinstance(fields.get("data"), bytes):
                try:
                    channel.finish(fields["data"])
                except SecureChannelError as exc:
                    failure.append(str(exc))
                    return
                self._channel = channel

        # One attempt: a repeated message 1 would open a second session (a new ephemeral key each time).
        answer = await self._aux_request(SerialMsg.SECURE_OPEN, {"data": channel.message1()}, on_answer=install,
                                         attempts=1)
        status = to_status(answer["status"])
        if status != Status.OK or not isinstance(answer.get("data"), bytes):
            raise StatusError(SerialMsg.SECURE_OPEN, status, answer.get("detail"), answer.get("text"))
        if failure or self._channel is not channel:
            self._channel = None
            raise ProtocolError(f"secure session: {failure[0] if failure else 'not established'}")

    def _install_session(self, frame: Frame) -> HelloInfo:
        """Accept a HELLO answer and start the new session (runs inside the frame handler)."""
        hello = self._accept_hello(frame)
        self._awaiting_hello = False
        previous = self._session
        self._session = hello
        self._generation += 1
        self.stats["resyncs"] += 1
        unanswered = sorted((r for r in self._pending.values() if not r.future.done()), key=lambda r: r.order)
        for req in unanswered:
            req.sent.clear()
        self._outbox = deque(unanswered)
        if self._secure is None:
            # A secure link becomes ready only once its session is open (_secure_open).
            self._ready.set()
            self._outbox_changed.set()
            self._credit_changed.set()
        if self._on_session is not None:
            try:
                self._on_session(hello, previous)
            except Exception:
                log.exception("%s: session callback failed", self.label)
        return hello

    def _accept_hello(self, frame: Frame) -> HelloInfo:
        try:
            fields = cbor_msgs.decode_response(SerialMsg.HELLO, frame.payload)
        except cbor_msgs.CborError as exc:
            raise ProtocolError(f"HELLO response: {exc}") from None
        status = to_status(fields["status"])
        if status != Status.OK:
            raise StatusError(SerialMsg.HELLO, status, fields.get("detail"), fields.get("text"))
        hello = HelloInfo.from_fields(fields)
        if hello.proto != PROTO_VERSION:
            raise ProtocolError(f"device speaks protocol {hello.proto}, this companion {PROTO_VERSION}")
        initial = hello.caps.credits if hello.caps.credits is not None else SERIAL_DEFAULT_CREDITS
        self._credits = initial + frame.credits
        self._reserve = 1 if initial >= 2 else 0
        self._owed = 0
        self._max_frame = min(hello.caps.max_frame or SERIAL_MAX_FRAME, SERIAL_MAX_FRAME)
        return hello

    # -- link loss ------------------------------------------------------------------

    def _fail_all(self, exc: BaseException) -> None:
        for req in list(self._pending.values()):
            if not req.future.done():
                req.future.set_exception(exc)
        self._outbox.clear()
        if self._hello_future is not None and not self._hello_future.done():
            self._hello_future.set_exception(exc)

    def _connection_lost(self, exc: BaseException) -> None:
        transport, self._transport = self._transport, None
        self._ready.clear()
        if transport is None:
            return
        log.warning("%s: link to %s lost: %s", self.label, self.url, exc)
        self._spawn(transport.close(), f"{self.label} close")
        self._fail_all(GatewayDisconnected(f"{self.label} {self.url}: {exc}"))
        if (self.reconnect and self._opened and not self._closed
                and (self._reconnect_task is None or self._reconnect_task.done())):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name=f"{self.label} reconnect")

    async def _reconnect_loop(self) -> None:
        delay, max_delay = self.reconnect_delays
        while not self._closed:
            await asyncio.sleep(delay)
            try:
                await self._connect()
            except GatewayError as exc:
                log.info("%s: reconnect to %s failed: %s", self.label, self.url, exc)
                delay = min(delay * 2, max_delay)
                continue
            self.stats["reconnects"] += 1
            log.info("%s: reconnected to %s", self.label, self.url)
            return

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until a session is established (after a reconnect, for example)."""
        await asyncio.wait_for(self._ready.wait(), timeout)
