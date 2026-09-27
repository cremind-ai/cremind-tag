"""Device side of the serial protocol (docs/protocol.md §1) over TCP, for simulated
gateways and bridge maintenance ports.

A TCP connection stands for an opened serial port; a new connection replaces the
previous one (the port was reopened). The device answers nothing before a
``HELLO`` on the connection. Rules modelled:

- Framing and the §1.1 checks (COBS, CRC, length, version) with their counters;
  requests of unknown type answer ``UNSUPPORTED``; malformed CBOR ``INVALID``.
- Credits (§1.3): the device has ``rx_buffers`` receive buffers (reported as
  ``caps.credits``); a request that arrives when all are busy is dropped and
  counted in ``overruns``; ``credit_violations`` counts frames the host sent
  without credit. Each processed request frees its buffer and the grant rides on
  the next frame to the host. The device sends only while it holds host credits.
- HELLO is exempt and resets the session: unsent responses and non-retained
  events are discarded, the device may send ``SERIAL_DEFAULT_CREDITS`` + the
  HELLO's grant byte, and every retained event is re-sent after the HELLO answer.
- Retained events (``seq`` from 1 per boot) stay in a ring of
  ``SERIAL_EVENT_RETAIN`` until ``EVENT_ACK``; overflow drops the oldest
  (``events_dropped``). Non-retained events that cannot be sent are dropped
  (``events_discarded``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..protocol import cbor_msgs
from ..protocol.ids import SERIAL_DEFAULT_CREDITS, SERIAL_EVENT_RETAIN, SerialFlag, SerialMsg, Status
from ..protocol.serial_frame import Frame, FrameReader, frame_to_wire
from .core import TaskSet

log = logging.getLogger(__name__)


@dataclass
class Reply:
    """A handler's answer; ``after`` runs once the response has been written (e.g. reboot)."""

    fields: dict[str, Any]
    after: Callable[[], Awaitable[None]] | None = None


RequestHandler = Callable[[SerialMsg, dict[str, Any]], Awaitable[Reply | dict[str, Any]]]


@dataclass
class _Out:
    type: int
    request_id: int
    payload: bytes
    flags: int
    sent: asyncio.Future[None] | None = None


@dataclass
class _Conn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    frames: FrameReader = field(default_factory=FrameReader)
    hello_done: bool = False
    send_credits: int = 0  # frames the device may still send to the host
    host_budget: int = 0  # frames the host may still send (device's view)
    owed: int = 0  # grants to give back to the host
    rx: deque[Frame] = field(default_factory=deque)
    responses: deque[_Out] = field(default_factory=deque)
    events: deque[_Out] = field(default_factory=deque)
    next_retained: int = 1  # next retained seq to (re)send in this session
    rx_event: asyncio.Event = field(default_factory=asyncio.Event)
    tx_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    closed: bool = False


class DeviceEndpoint:
    """One serial device port served over TCP (see the module docstring)."""

    def __init__(self, name: str, handler: RequestHandler, *, supported: Iterable[SerialMsg],
                 rx_buffers: int = SERIAL_DEFAULT_CREDITS, retain: int = SERIAL_EVENT_RETAIN,
                 processing_delay_s: float = 0.0, event_queue: int = 64) -> None:
        self.name = name
        self.handler = handler
        self.supported = frozenset(supported) | {SerialMsg.HELLO, SerialMsg.EVENT_ACK}
        self.rx_buffers = rx_buffers
        self.retain = retain
        self.processing_delay_s = processing_delay_s
        self.event_queue = event_queue
        self.counters: Counter[str] = Counter()
        self.drop_responses = 0  # fault: process the next N requests but lose their responses
        self._retained: deque[tuple[int, _Out]] = deque()
        self._seq = 0
        self._conn: _Conn | None = None
        self._server: asyncio.Server | None = None
        self._tasks = TaskSet(name)
        self.host = "127.0.0.1"
        self.port = 0

    # -- lifecycle ---------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.port}"

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        self._server = await asyncio.start_server(self._accept, host, port)
        sock = self._server.sockets[0]
        self.host, self.port = sock.getsockname()[:2]
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        await self._drop(self._conn)
        await self._tasks.cancel_all()

    def drop_connection(self) -> None:
        """Close the host's connection (USB re-enumeration after a reboot, cable pulled)."""
        conn = self._conn
        if conn is not None:
            self._tasks.spawn(self._drop(conn), "drop")

    def new_boot(self) -> None:
        """Lose the RAM state of the link: retained events, sequence numbers."""
        self._retained.clear()
        self._seq = 0

    async def _drop(self, conn: _Conn | None) -> None:
        if conn is None or conn.closed:
            return
        conn.closed = True
        if self._conn is conn:
            self._conn = None
        for task in conn.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        with contextlib.suppress(Exception):
            conn.writer.close()
            await conn.writer.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._drop(self._conn)
        conn = _Conn(reader, writer)
        self._conn = conn
        self.counters["connections"] += 1
        conn.tasks = [self._tasks.spawn(self._read(conn), "read"), self._tasks.spawn(self._work(conn), "work"),
                      self._tasks.spawn(self._send(conn), "send")]

    # -- events --------------------------------------------------------------------

    @property
    def retained_seqs(self) -> list[int]:
        return [seq for seq, _ in self._retained]

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, msg: SerialMsg, fields: dict[str, Any], *, retained: bool) -> int | None:
        """Queue an event; retained events get the next ``seq`` (returned)."""
        if retained:
            self._seq += 1
            fields = {**fields, "seq": self._seq}
            out = _Out(msg, 0, cbor_msgs.encode_event(msg, fields), SerialFlag.EVENT)
            self._retained.append((self._seq, out))
            if len(self._retained) > self.retain:
                self._retained.popleft()
                self.counters["events_dropped"] += 1
            if self._conn is not None:
                self._conn.tx_event.set()
            return self._seq
        conn = self._conn
        if conn is None or not conn.hello_done or conn.send_credits <= 0 or len(conn.events) >= self.event_queue:
            self.counters["events_discarded"] += 1
            return None
        conn.events.append(_Out(msg, 0, cbor_msgs.encode_event(msg, fields), SerialFlag.EVENT))
        conn.tx_event.set()
        return None

    def _release(self, seq: int) -> None:
        while self._retained and self._retained[0][0] <= seq:
            self._retained.popleft()

    # -- receive -------------------------------------------------------------------

    async def _read(self, conn: _Conn) -> None:
        try:
            while True:
                data = await conn.reader.read(4096)
                if not data:
                    break
                before = (conn.frames.crc_errors, conn.frames.len_errors, conn.frames.version_errors)
                for frame in conn.frames.feed(data):
                    self._receive(conn, frame)
                after = (conn.frames.crc_errors, conn.frames.len_errors, conn.frames.version_errors)
                for key, a, b in zip(("crc_errors", "len_errors", "version_errors"), before, after, strict=True):
                    self.counters[key] += b - a
        except (ConnectionError, OSError):
            pass
        finally:
            self._tasks.spawn(self._drop(conn), "drop")

    def _receive(self, conn: _Conn, frame: Frame) -> None:
        self.counters["frames_rx"] += 1
        if frame.flags & (SerialFlag.RESPONSE | SerialFlag.EVENT):
            self.counters["unexpected_frames"] += 1
            return
        if frame.type == SerialMsg.HELLO:
            conn.rx.append(frame)  # always accepted: it resets the link
            conn.rx_event.set()
            return
        if not conn.hello_done:
            self.counters["overruns"] += 1
            return
        conn.send_credits += frame.credits
        if frame.credits:
            conn.tx_event.set()
        conn.host_budget -= 1
        if conn.host_budget < 0:
            self.counters["credit_violations"] += 1
        if len(conn.rx) >= self.rx_buffers:
            self.counters["overruns"] += 1
            return
        conn.rx.append(frame)
        conn.rx_event.set()

    async def _work(self, conn: _Conn) -> None:
        while True:
            if not conn.rx:
                conn.rx_event.clear()
                await conn.rx_event.wait()
                continue
            frame = conn.rx[0]
            if frame.type == SerialMsg.HELLO:
                conn.rx.popleft()
                await self._hello(conn, frame)
                continue
            if self.processing_delay_s:
                await asyncio.sleep(self.processing_delay_s)
            reply = await self._dispatch(frame)
            conn.rx.popleft()
            conn.owed += 1  # the receive buffer is free again
            if self.drop_responses > 0 and frame.type != SerialMsg.EVENT_ACK:
                self.drop_responses -= 1
                self.counters["responses_dropped"] += 1
                conn.owed -= 1  # lost on the wire together with the grant it carried
                continue
            out = _Out(frame.type, frame.request_id, reply[0], SerialFlag.RESPONSE)
            if reply[1] is not None:
                out.sent = asyncio.get_running_loop().create_future()
                self._tasks.spawn(self._after_sent(out.sent, reply[1]), "after")
            conn.responses.append(out)
            conn.tx_event.set()

    async def _after_sent(self, sent: asyncio.Future[None], action: Callable[[], Awaitable[None]]) -> None:
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(sent, 2.0)
        await asyncio.sleep(0.05)  # the UART/USB finishes sending the answer before the reset
        await action()

    async def _dispatch(self, frame: Frame) -> tuple[bytes, Callable[[], Awaitable[None]] | None]:
        try:
            msg = SerialMsg(frame.type)
        except ValueError:
            msg = None
        if msg is None or msg not in self.supported:
            self.counters["unsupported"] += 1
            return cbor_msgs.encode_map({"status": Status.UNSUPPORTED}), None
        try:
            fields = cbor_msgs.decode_request(msg, frame.payload)
        except cbor_msgs.CborError as exc:
            self.counters["invalid"] += 1
            return cbor_msgs.encode_response(msg, {"status": Status.INVALID, "text": str(exc)[:120]}), None
        if msg == SerialMsg.EVENT_ACK:
            self._release(fields["seq"])
            return cbor_msgs.encode_response(msg, {"status": Status.OK}), None
        try:
            result = await self.handler(msg, fields)
        except Exception as exc:
            log.exception("%s: handler failed for %s", self.name, msg.name)
            self.counters["internal_errors"] += 1
            return cbor_msgs.encode_response(msg, {"status": Status.INTERNAL, "text": str(exc)[:120]}), None
        reply = result if isinstance(result, Reply) else Reply(result)
        return cbor_msgs.encode_response(msg, reply.fields), reply.after

    async def _hello(self, conn: _Conn, frame: Frame) -> None:
        try:
            fields = cbor_msgs.decode_request(SerialMsg.HELLO, frame.payload)
            result = await self.handler(SerialMsg.HELLO, fields)
            reply = result if isinstance(result, Reply) else Reply(result)
        except cbor_msgs.CborError as exc:
            reply = Reply({"status": Status.INVALID, "text": str(exc)[:120]})
        conn.responses.clear()
        conn.events.clear()
        ok = reply.fields.get("status") == Status.OK
        conn.hello_done = ok
        conn.send_credits = SERIAL_DEFAULT_CREDITS + frame.credits
        conn.host_budget = self.rx_buffers
        conn.owed = 0
        conn.next_retained = self._retained[0][0] if self._retained else self._seq + 1
        self.counters["hellos"] += 1
        payload = cbor_msgs.encode_response(SerialMsg.HELLO, reply.fields)
        await self._write(conn, Frame(SerialMsg.HELLO, frame.request_id, payload, SerialFlag.RESPONSE, 0))
        conn.tx_event.set()

    # -- send ------------------------------------------------------------------------

    def _next_out(self, conn: _Conn) -> _Out | None:
        if conn.responses:
            return conn.responses.popleft()
        for seq, out in self._retained:
            if seq >= conn.next_retained:
                conn.next_retained = seq + 1
                return out
        if conn.events:
            return conn.events.popleft()
        return None

    async def _send(self, conn: _Conn) -> None:
        while True:
            conn.tx_event.clear()
            while conn.hello_done and conn.send_credits > 0:
                out = self._next_out(conn)
                if out is None:
                    break
                grant = min(conn.owed, 255)
                conn.owed -= grant
                conn.host_budget += grant
                conn.send_credits -= 1
                await self._write(conn, Frame(out.type, out.request_id, out.payload, out.flags, grant))
                if out.sent is not None and not out.sent.done():
                    out.sent.set_result(None)
            await conn.tx_event.wait()

    async def _write(self, conn: _Conn, frame: Frame) -> None:
        try:
            conn.writer.write(frame_to_wire(frame))
            await conn.writer.drain()
            self.counters["frames_tx"] += 1
        except (ConnectionError, OSError):
            self._tasks.spawn(self._drop(conn), "drop")
