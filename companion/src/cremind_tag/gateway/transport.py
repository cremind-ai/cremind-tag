"""Serial transport: a pyserial URL read by a thread, frames delivered to asyncio.

``serial.serial_for_url`` opens both real ports (``COM7``, ``/dev/ttyACM0``) and
``socket://host:port`` (the simulator). A daemon thread reads bytes, runs the
COBS stream decoder and the frame checks of docs/protocol.md §1.1
(:class:`~cremind_tag.protocol.serial_frame.FrameReader`, which counts what it
drops) and hands each frame to the event loop with ``call_soon_threadsafe``.
Writes run on a single-thread executor, so they keep their order and never
block the loop. :meth:`SerialTransport.close` stops the thread and closes the
port; a device that disappears ends the frame stream with
:class:`TransportClosed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import serial

from ..protocol.serial_frame import Frame, FrameReader, frame_to_wire
from .errors import GatewayDisconnected

log = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 115200
OPEN_TIMEOUT_S = 5.0


class TransportClosed(GatewayDisconnected):
    """The port closed or failed; no more frames will arrive."""


class _Closed:
    __slots__ = ("error",)

    def __init__(self, error: BaseException | None) -> None:
        self.error = error


class SerialTransport:
    """One open serial link (see the module docstring)."""

    def __init__(self, url: str, *, baudrate: int = DEFAULT_BAUDRATE, poll_interval: float = 0.05) -> None:
        self.url = url
        self.baudrate = baudrate
        self.poll_interval = poll_interval
        self.reader = FrameReader()
        self._serial: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Frame | _Closed] = asyncio.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serial-write")
        self._closed = False

    @property
    def is_open(self) -> bool:
        return self._serial is not None and not self._closed

    def stats(self) -> dict[str, int]:
        """Frames the receiver dropped (§1.1 counters, host side)."""
        r = self.reader
        return {"crc_errors": r.crc_errors, "len_errors": r.len_errors, "version_errors": r.version_errors,
                "cobs_errors": r.cobs_errors, "oversize": r.oversize}

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()

        def _open() -> Any:
            return serial.serial_for_url(self.url, baudrate=self.baudrate, timeout=self.poll_interval,
                                         write_timeout=OPEN_TIMEOUT_S, exclusive=True)

        try:
            self._serial = await asyncio.wait_for(self._loop.run_in_executor(self._writer, _open), OPEN_TIMEOUT_S)
        except (serial.SerialException, OSError, ValueError, TimeoutError) as exc:
            raise TransportClosed(f"cannot open {self.url}: {exc}") from exc
        self._thread = threading.Thread(target=self._read_loop, name=f"serial-read {self.url}", daemon=True)
        self._thread.start()
        log.debug("transport: opened %s", self.url)

    def _post(self, item: Frame | _Closed) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop shutting down
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def _read_loop(self) -> None:
        port = self._serial
        error: BaseException | None = None
        try:
            while not self._stop.is_set():
                data = port.read(1)  # blocks up to poll_interval
                if not data:
                    continue
                waiting = port.in_waiting
                if waiting:
                    data += port.read(waiting)
                for frame in self.reader.feed(data):
                    self._post(frame)
        except Exception as exc:  # port unplugged, socket closed, ...
            if not self._stop.is_set():
                error = exc
        finally:
            self._post(_Closed(error))

    async def recv(self) -> Frame:
        """The next valid frame; raises :class:`TransportClosed` once the port is gone."""
        item = await self._queue.get()
        if isinstance(item, _Closed):
            self._queue.put_nowait(item)  # every later recv() sees the closure too
            reason = f": {item.error}" if item.error else ""
            raise TransportClosed(f"{self.url} closed{reason}")
        return item

    async def send(self, frame: Frame) -> None:
        await self.send_bytes(frame_to_wire(frame))

    async def send_bytes(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportClosed(f"{self.url} is not open")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._writer, self._serial.write, data)
        except (serial.SerialException, OSError) as exc:
            raise TransportClosed(f"write to {self.url} failed: {exc}") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        port = self._serial
        if port is not None:
            with contextlib.suppress(Exception):
                port.cancel_read()  # real ports: wake the blocked read (socket:// has no cancel)
            with contextlib.suppress(Exception):
                await asyncio.get_running_loop().run_in_executor(self._writer, port.close)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2.0)
        self._writer.shutdown(wait=False)
        self._post(_Closed(None))
        log.debug("transport: closed %s", self.url)
