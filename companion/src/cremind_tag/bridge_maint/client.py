"""Client for a bridge's maintenance port (docs/protocol.md §1.6, docs/fontpack.md §4).

Same framing and credit rules as the gateway link (:mod:`cremind_tag.gateway.link`);
the port answers ``HELLO``, ``PING``, ``INFO``, ``REBOOT``, the ``FONT_*``
messages and ``FLASH_TEST`` — never mesh or delivery requests.

Font installation::

    FONT_ABORT                                      (clears a half-finished install; harmless otherwise)
    FONT_BEGIN{size, digest, fontpack_id} -> {slot, flash_size}
    FONT_DATA{offset, data} x n                     (strictly sequential, pipelined up to ``window``)
    FONT_COMMIT{} -> {fontpack_id}                  (SHA-256 of the slot = digest, format check, slot flip)

``digest`` is the SHA-256 of the whole pack image as written to the slot.
Resume safety: a data frame is never retried on its own (a retry would repeat an
offset the bridge may already have written); any failure aborts the install with
``FONT_ABORT`` — the active pack is untouched until ``FONT_COMMIT`` flips the
directory — and running the install again starts cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..fontpack.format import FontPack
from ..gateway.errors import GatewayError, StatusError
from ..gateway.link import DEFAULT_ATTEMPTS, REQUEST_TIMEOUT_S, SerialLink, TransportFactory
from ..gateway.opid import OpIdGenerator
from ..gateway.results import Ack, DeviceInfo, FlashTestResult, FontStatus, HelloInfo, to_status
from ..protocol.ids import SERIAL_CRC_LEN, SERIAL_HEADER_LEN, SerialMsg, Status

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
FONT_DATA_OVERHEAD = 24  # CBOR map + keys + offset + byte-string header, rounded up
DEFAULT_CHUNK = 2048


class FontInstallError(GatewayError):
    """The bridge refused a step of the installation (the active pack is unchanged)."""

    def __init__(self, step: str, status: Status | int, text: str | None = None) -> None:
        name = status.name if isinstance(status, Status) else str(status)
        super().__init__(f"{step} answered {name}" + (f": {text}" if text else ""))
        self.step = step
        self.status = status


@dataclass(frozen=True, slots=True)
class FontInstallResult:
    fontpack_id: bytes
    size: int
    slot: int | None
    flash_size: int | None
    skipped: bool = False  # the pack was already active


class BridgeMaintClient:
    """Connection to one bridge maintenance port."""

    def __init__(self, url: str, *, name: str = "cremind-tag", request_timeout: float = REQUEST_TIMEOUT_S,
                 attempts: int = DEFAULT_ATTEMPTS, transport_factory: TransportFactory | None = None,
                 op_ids: OpIdGenerator | None = None) -> None:
        self.url = url
        self._op_ids = op_ids or OpIdGenerator()
        self.link = SerialLink(url, name=name, label="bridge", request_timeout=request_timeout, attempts=attempts,
                               reconnect=False, transport_factory=transport_factory)

    async def connect(self) -> HelloInfo:
        return await self.link.open()

    async def close(self) -> None:
        await self.link.close()

    async def __aenter__(self) -> BridgeMaintClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def hello_info(self) -> HelloInfo | None:
        return self.link.session

    async def _checked(self, msg: SerialMsg, fields: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        response = await self.link.request(msg, fields or {}, **kw)
        status = to_status(response["status"])
        if status != Status.OK:
            raise StatusError(msg, status, response.get("detail"), response.get("text"))
        return response

    async def ping(self) -> int:
        return int((await self._checked(SerialMsg.PING)).get("uptime_s", 0))

    async def info(self) -> DeviceInfo:
        return DeviceInfo.from_fields(await self._checked(SerialMsg.INFO))

    async def reboot(self) -> Ack:
        op = self._op_ids.next()
        return Ack.from_fields(SerialMsg.REBOOT, await self.link.request(SerialMsg.REBOOT, {"op_id": op}), op)

    async def font_status(self) -> FontStatus:
        """The active pack (``fontpack_id`` absent when the bridge has none)."""
        response = await self.link.request(SerialMsg.FONT_STATUS, {})
        status = to_status(response["status"])
        if status not in (Status.OK, Status.NOT_FOUND):
            raise StatusError(SerialMsg.FONT_STATUS, status, response.get("detail"), response.get("text"))
        return FontStatus.from_fields(response)

    async def font_abort(self) -> Ack:
        return Ack.from_fields(SerialMsg.FONT_ABORT, await self.link.request(SerialMsg.FONT_ABORT, {}))

    def _chunk_size(self, requested: int | None) -> int:
        limit = self.link.max_frame - SERIAL_HEADER_LEN - SERIAL_CRC_LEN - FONT_DATA_OVERHEAD
        size = min(requested or DEFAULT_CHUNK, limit)
        if size < 64:
            raise GatewayError(f"max_frame {self.link.max_frame} leaves no room for font data")
        return size

    async def font_install(self, pack: bytes, *, progress: ProgressCallback | None = None, force: bool = False,
                           chunk_size: int | None = None, window: int = 2, begin_timeout: float = 30.0,
                           commit_timeout: float = 180.0) -> FontInstallResult:
        """Install ``pack`` into the inactive slot and activate it (see the module docstring)."""
        parsed = FontPack(pack)  # refuse a corrupt pack before touching the bridge
        pack_id = parsed.pack_id
        if not force:
            current = await self.font_status()
            if current.fontpack_id == pack_id:
                return FontInstallResult(pack_id, len(pack), current.slot, current.flash_size, skipped=True)
        await self.font_abort()
        begin = await self.link.request(
            SerialMsg.FONT_BEGIN, {"size": len(pack), "digest": hashlib.sha256(pack).digest(), "fontpack_id": pack_id},
            timeout=begin_timeout)
        status = to_status(begin["status"])
        if status != Status.OK:
            raise FontInstallError("FONT_BEGIN", status, begin.get("text"))
        try:
            await self._send_data(pack, self._chunk_size(chunk_size), max(1, window), progress)
            commit = await self.link.request(SerialMsg.FONT_COMMIT, {}, timeout=commit_timeout, attempts=1)
            status = to_status(commit["status"])
            if status != Status.OK:
                raise FontInstallError("FONT_COMMIT", status, commit.get("text"))
            if commit.get("fontpack_id") != pack_id:
                raise FontInstallError("FONT_COMMIT", Status.DIGEST_MISMATCH, "bridge reports another pack id")
        except BaseException:
            with contextlib.suppress(GatewayError, asyncio.CancelledError):
                await self.font_abort()
            raise
        log.info("bridge %s: font pack %s active in slot %s", self.url, pack_id.hex(), begin.get("slot"))
        return FontInstallResult(pack_id, len(pack), begin.get("slot"), begin.get("flash_size"))

    async def _send_data(self, pack: bytes, chunk: int, window: int, progress: ProgressCallback | None) -> None:
        total = len(pack)
        offsets = list(range(0, total, chunk))
        in_flight: list[tuple[int, asyncio.Task[dict[str, Any]]]] = []
        sent = 0

        async def settle(offset: int, task: asyncio.Task[dict[str, Any]]) -> None:
            nonlocal sent
            response = await task
            status = to_status(response["status"])
            if status != Status.OK:
                raise FontInstallError(f"FONT_DATA @{offset}", status, response.get("text"))
            sent = min(offset + chunk, total)
            if progress is not None:
                progress(sent, total)

        try:
            for offset in offsets:
                if len(in_flight) >= window:
                    await settle(*in_flight.pop(0))
                task = asyncio.create_task(self.link.request(
                    SerialMsg.FONT_DATA, {"offset": offset, "data": pack[offset:offset + chunk]}, attempts=1))
                in_flight.append((offset, task))
            while in_flight:
                await settle(*in_flight.pop(0))
        finally:
            for _, task in in_flight:
                task.cancel()
            for _, task in in_flight:
                with contextlib.suppress(BaseException):
                    await task

    async def flash_test(self, *, op_id: int | None = None, timeout: float = 120.0) -> FlashTestResult:
        """Erase/write/read-back at the qualification offsets (docs/fontpack.md §3)."""
        op = self._op_ids.next() if op_id is None else op_id
        response = await self.link.request(SerialMsg.FLASH_TEST, {"op_id": op}, timeout=timeout)
        return FlashTestResult.from_fields(response)
