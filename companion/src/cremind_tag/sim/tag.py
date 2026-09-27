"""Simulated battery e-paper tag (docs/protocol.md §5, §6, §9).

The tag wakes every ``TAG_WAKE_PERIOD_MS ± TAG_WAKE_JITTER_MS`` (uniform) and
advertises for ``TAG_ADV_WINDOW_MS``. A bridge that connects gets the real
session: CAPS read, the plaintext handshake on CTRL (``protocol.session`` key
schedule and MACs), then AES-CCM records on DATA/STATUS with credits, all
fragmented at ``ATT_VALUE_MAX`` by ``protocol.fragments``.

Display transaction (§6) on a persisted record (``TagNvs``, the tag's NVS):
``FRAME_BEGIN`` is decided by ``protocol.tag_txn.frame_begin_decision``; plane
bytes are staged and hashed incrementally; at ``FRAME_END`` the frame must be
complete and match the announced digest, then ``REFRESH_INTENT`` is persisted,
``PROGRESS{REFRESHING}`` sent, the refresh runs (panel-specific duration), the
``DISPLAYED`` record is persisted and only then ``RESULT`` is sent. Boot applies
``tag_txn.boot_recover`` (``DISPLAY_STATE_UNKNOWN``).

Fault injection (:class:`TagFaults`): power loss between ``REFRESH_INTENT`` and
``DISPLAYED`` (the panel may or may not have changed), a disconnect after N
``PLANE_DATA`` records, forced MAC failures, refresh timeouts. Three consecutive
authentication failures make the tag skip its next wake window.

Battery: a simple linear drain per wake, session and refresh; low battery below
2.4 V. Not modelled: RF, the panel controller's SPI traffic, real power figures.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from ..enroll.hardware import PANEL_PROFILES
from ..protocol import session as crypto
from ..protocol.enrollment import pack_blob
from ..protocol.fragments import Fragmenter, FragmentError, Reassembler
from ..protocol.ids import (
    PROTO_VERSION,
    TAG_ADV_INTERVAL_MS,
    TAG_ADV_WINDOW_MS,
    TAG_CTRL_MSG_MAX,
    TAG_RECORD_PAYLOAD_MAX,
    TAG_RECORD_WIRE_MAX,
    TAG_SECRET_LEN,
    TAG_SESSION_TIMEOUT_MS,
    TAG_WAKE_JITTER_MS,
    TAG_WAKE_PERIOD_MS,
    Board,
    CtrlMsg,
    DeliveryStage,
    GattChr,
    Panel,
    PlainMsg,
    RecordDir,
    RecordType,
    Status,
    TagCommand,
)
from ..protocol.msgs import (
    CtrlAuth,
    CtrlAuthOk,
    CtrlChallenge,
    CtrlError,
    CtrlHello,
    MessageError,
    PlainCredit,
    RecCmd,
    RecFrameBegin,
    RecPlaneData,
    RecProgress,
    RecResult,
    TagCaps,
)
from ..protocol.tag_txn import DisplayRecord, StoredState, boot_recover, frame_begin_decision
from .core import SimClock, TaskSet, rng_stream
from .radio import (
    ADV_FLAG_LOW_BATTERY,
    ADV_FLAG_RESULT_PENDING,
    ADV_FLAG_UNKNOWN_STATE,
    Advert,
    Air,
    GattLink,
    LinkLost,
)

log = logging.getLogger(__name__)

REFRESH_MS: dict[int, int] = {Panel.UC8176_420_BW: 4000, Panel.UC8176_420_BWR: 15000, Panel.NONE: 300}
LOW_BATTERY_MV = 2400
AUTH_FAILURES_BEFORE_SKIP = 3
_COST_WAKE_MV = 0.002
_COST_SESSION_MV = 0.05
_COST_REFRESH_MV = {1: 0.5, 2: 1.5}


@dataclass
class TagSpec:
    """Enrollment data and hardware of one simulated tag."""

    tag_id: int
    secret: bytes
    board: int = Board.NRF52DK_TAG
    panel: int = Panel.UC8176_420_BW
    width: int = 0  # 0 = from the panel profile
    height: int = 0
    planes: int = 0
    plane_flags: int = -1
    fw: tuple[int, int, int] = (0, 1, 0)
    credits: int = 2  # two TAG_RECORD_BUF buffers (§5.5)
    battery_mv: float = 3000.0
    refresh_ms: int = 0  # 0 = panel default
    sleep_supported: bool = False

    def __post_init__(self) -> None:
        if len(self.secret) != TAG_SECRET_LEN:
            raise ValueError("tag secret must be 32 bytes")
        profile = PANEL_PROFILES.get(Panel(self.panel)) if self.panel in Panel else None
        if profile is None and (not self.width or not self.height or not self.planes or self.plane_flags < 0):
            raise ValueError(f"panel {self.panel} has no profile: give width, height, planes and plane_flags")
        if profile is not None:
            self.width = self.width or profile.width
            self.height = self.height or profile.height
            self.planes = self.planes or profile.planes
            self.plane_flags = profile.plane_flags if self.plane_flags < 0 else self.plane_flags
        self.refresh_ms = self.refresh_ms or REFRESH_MS.get(self.panel, 4000)

    @classmethod
    def generate(cls, seed: int, index: int, **overrides: Any) -> TagSpec:
        """A reproducible tag: id and secret drawn from ``(seed, index)``."""
        rng = rng_stream(seed, "tag-spec", index)
        return cls(tag_id=rng.randint(1, 0xFFFFFFFE), secret=rng.randbytes(TAG_SECRET_LEN), **overrides)

    @property
    def plane_len(self) -> int:
        return (self.width + 7) // 8 * self.height

    def enrollment_blob(self) -> bytes:
        return pack_blob(self.tag_id, self.secret, self.board, self.panel)

    def caps(self) -> TagCaps:
        return TagCaps(PROTO_VERSION, self.tag_id, self.board, self.panel, self.width, self.height, self.planes,
                       self.plane_flags, *self.fw, TAG_RECORD_PAYLOAD_MAX, self.credits)

    def white_planes(self) -> tuple[bytes, ...]:
        plane0 = b"\xff" if self.plane_flags & 1 else b"\x00"  # plane 0: white value (padding too)
        plane1 = b"\x00" if self.plane_flags & 2 else b"\xff"  # plane 1: not-red value
        return tuple((plane0 if p == 0 else plane1) * self.plane_len for p in range(self.planes))


@dataclass
class TagFaults:
    """One-shot fault counters; each firing decrements its counter."""

    power_loss: int = 0  # next N refreshes lose power after REFRESH_INTENT is persisted
    disconnect_after_records: int | None = None  # drop the link after this many PLANE_DATA records (once)
    auth_fail: int = 0  # next N AUTH checks fail as if mac_b were wrong
    refresh_timeout: int = 0  # next N refreshes: BUSY never releases


@dataclass
class TagNvs:
    """What the tag persists (one NVS record + the accepted epoch)."""

    record: DisplayRecord | None = None
    stored_epoch: int = 0

    def to_json(self) -> dict[str, Any]:
        r = self.record
        return {"stored_epoch": self.stored_epoch, "record": None if r is None else {
            "tag_id": r.tag_id, "epoch": r.epoch, "revision": r.revision, "update_id": r.update_id,
            "digest": r.digest.hex(), "status": int(r.status), "state": int(r.state)}}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TagNvs:
        r = data.get("record")
        record = None if r is None else DisplayRecord(r["tag_id"], r["epoch"], r["revision"], r["update_id"],
                                                      bytes.fromhex(r["digest"]), Status(r["status"]),
                                                      StoredState(r["state"]))
        return cls(record, int(data.get("stored_epoch", 0)))


class _EndSession(Exception):
    """The tag ends the session (after sending ERROR, or on a protocol violation)."""


class _PowerLoss(Exception):
    """Injected power failure: the tag reboots."""


@dataclass
class _Frame:
    epoch: int
    revision: int
    update_id: int
    digest: bytes
    planes: int
    plane_len: int
    staged: list[bytearray] = field(default_factory=list)
    received: list[int] = field(default_factory=list)
    hasher: Any = field(default_factory=hashlib.sha256)
    records: int = 0


class SimTag:
    """One simulated tag (see the module docstring)."""

    def __init__(self, spec: TagSpec, clock: SimClock, air: Air, rng: random.Random, *,
                 nvs: TagNvs | None = None, faults: TagFaults | None = None) -> None:
        self.spec = spec
        self.tag_id = spec.tag_id
        self.clock = clock
        self.air = air
        self.rng = rng
        self.nvs = nvs or TagNvs()
        self.faults = faults or TagFaults()
        self.battery_mv = spec.battery_mv
        self.stats: Counter[str] = Counter()
        self.panel_planes: tuple[bytes, ...] = spec.white_planes()
        self.result_pending = False
        self.out_of_range = False  # test/demo hook: the tag keeps waking but no bridge hears it
        self._advertising = False
        self._link: GattLink | None = None
        self._connected = asyncio.Event()
        self._session_task: asyncio.Task[None] | None = None
        self._auth_failures = 0
        self._skip_windows = 0
        self._tasks = TaskSet(f"tag {spec.tag_id:08X}")
        self._boot()

    # -- state ---------------------------------------------------------------------

    @property
    def displayed_digest(self) -> bytes:
        return hashlib.sha256(b"".join(self.panel_planes)).digest()

    @property
    def unknown_pending(self) -> bool:
        r = self.nvs.record
        return r is not None and r.state == StoredState.REFRESH_INTENT

    def _boot(self) -> None:
        recovery = boot_recover(self.nvs.record)
        if recovery.persist:
            self.nvs.record = recovery.record
            self.stats["recovered_unknown"] += 1

    def advert(self) -> Advert:
        flags = 0
        if self.result_pending:
            flags |= ADV_FLAG_RESULT_PENDING
        if self.battery_mv < LOW_BATTERY_MV:
            flags |= ADV_FLAG_LOW_BATTERY
        if self.unknown_pending:
            flags |= ADV_FLAG_UNKNOWN_STATE
        revision = self.nvs.record.revision if self.nvs.record else 0
        return Advert(self.tag_id, flags, revision & 0xFFFF)

    def _drain(self, mv: float) -> None:
        self.battery_mv = max(0.0, self.battery_mv - mv)

    # -- peripheral interface ------------------------------------------------------

    def connectable(self) -> bool:
        return self._advertising and self._link is None and not self.out_of_range

    def accept(self, link: GattLink) -> None:
        self._link = link
        self._advertising = False
        self._connected.set()
        self._session_task = self._tasks.spawn(self._session(link), "session")

    def read_characteristic(self, chr: GattChr) -> bytes:
        if chr == GattChr.CAPS:
            return self.spec.caps().pack()
        raise ValueError(f"characteristic {chr!r} is not readable")

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.air.tags[self.tag_id] = self
        self._tasks.spawn(self._run(), "wake loop")

    async def stop(self) -> None:
        self.air.tags.pop(self.tag_id, None)
        if self._link is not None:
            self._link.disconnect("tag stopped")
        await self._tasks.cancel_all()

    async def _run(self) -> None:
        await self.clock.sleep_ms(self.rng.uniform(0, TAG_WAKE_PERIOD_MS))  # random phase
        while True:
            started = self.clock.now_ms()
            self.stats["wakes"] += 1
            self._drain(_COST_WAKE_MV)
            if self._skip_windows:
                self._skip_windows -= 1
                self.stats["skipped_windows"] += 1
            else:
                await self._advertise_window()
            period = TAG_WAKE_PERIOD_MS + self.rng.uniform(-TAG_WAKE_JITTER_MS, TAG_WAKE_JITTER_MS)
            await self.clock.sleep_ms(max(0.0, started + period - self.clock.now_ms()))

    async def _advertise_window(self) -> None:
        self._connected.clear()
        self._advertising = True
        end = self.clock.now_ms() + TAG_ADV_WINDOW_MS
        try:
            while self._link is None:
                remaining = end - self.clock.now_ms()
                if remaining <= 0:
                    break
                self.stats["adverts"] += 1
                if not self.out_of_range:
                    self.air.advertise(self.tag_id, self.advert().to_bytes())
                try:
                    await self.clock.wait_for(self._connected.wait(), min(TAG_ADV_INTERVAL_MS, remaining))
                except TimeoutError:
                    pass
        finally:
            self._advertising = False
        if self._session_task is not None:
            await asyncio.shield(self._session_task)
            self._session_task = None

    # -- session ---------------------------------------------------------------------

    async def _session(self, link: GattLink) -> None:
        self.stats["sessions"] += 1
        self._drain(_COST_SESSION_MV)
        session = _TagSession(self, link)
        try:
            await session.run()
        except (LinkLost, _EndSession):
            pass
        except TimeoutError:
            self.stats["session_timeouts"] += 1
        except _PowerLoss:
            self.stats["power_losses"] += 1
            link.disconnect("tag power loss")
            self._link = None
            self.result_pending = False  # RAM is gone
            self._boot()  # reboot: REFRESH_INTENT -> DISPLAY_STATE_UNKNOWN (§6)
        finally:
            if session.frame is not None:
                self.stats["frames_aborted"] += 1  # abort_frame(): the panel never refreshes
            link.disconnect("tag ended the session")
            self._link = None


class _TagSession:
    """Peripheral side of one connection (§5.3–§5.6)."""

    def __init__(self, tag: SimTag, link: GattLink) -> None:
        self.tag = tag
        self.link = link
        self.ctrl_rx = Reassembler(TAG_CTRL_MSG_MAX)
        self.data_rx = Reassembler(TAG_RECORD_WIRE_MAX)
        self.ctrl_tx = Fragmenter(TAG_CTRL_MSG_MAX)
        self.status_tx = Fragmenter(TAG_RECORD_WIRE_MAX)
        self.hello: CtrlHello | None = None
        self.hello_msg = b""
        self.challenge_msg = b""
        self.receiver: crypto.RecordReceiver | None = None
        self.sender: crypto.RecordSender | None = None
        self.granted = 0  # DATA credits the bridge still holds
        self.frame: _Frame | None = None

    @property
    def epoch(self) -> int:
        assert self.hello is not None
        return self.hello.epoch

    async def run(self) -> None:
        clock = self.tag.clock
        while True:
            try:
                chr_, value = await self.link.peripheral_recv(clock, TAG_SESSION_TIMEOUT_MS)
            except TimeoutError:
                self.tag.stats["session_timeouts"] += 1
                raise _EndSession from None
            try:
                if chr_ == GattChr.CTRL:
                    message = self.ctrl_rx.feed(value)
                    if message is not None:
                        await self.on_ctrl(message)
                elif chr_ == GattChr.DATA:
                    record = self.data_rx.feed(value)
                    if record is not None:
                        await self.on_record(record)
                else:
                    raise FragmentError(f"write to {chr_!r}")
            except FragmentError:
                self.tag.stats["fragment_errors"] += 1
                raise _EndSession from None

    # -- sending -------------------------------------------------------------------

    def send_ctrl(self, message: bytes) -> None:
        for value in self.ctrl_tx.split(message):
            self.link.notify(GattChr.CTRL, value)

    def send_status(self, message: bytes) -> None:
        for value in self.status_tx.split(message):
            self.link.notify(GattChr.STATUS, value)

    def grant(self, credits: int) -> None:
        self.granted += credits
        self.send_status(bytes([PlainMsg.CREDIT]) + PlainCredit(credits).pack())

    def send_record(self, record_type: RecordType, plaintext: bytes) -> None:
        assert self.sender is not None
        self.send_status(self.sender.seal(record_type, plaintext))

    def send_result(self, update_id: int, epoch: int, revision: int, status: Status, digest: bytes = bytes(32),
                    refresh_ms: int = 0, flags: int = 0) -> None:
        tag = self.tag
        self.send_record(RecordType.RESULT, RecResult(update_id, epoch, revision, status, digest[:8],
                                                      int(tag.battery_mv), min(refresh_ms, 0xFFFF), flags).pack())
        tag.stats["results"] += 1
        tag.stats[f"result_{status.name}"] += 1
        tag.result_pending = False

    def error(self, status: Status) -> None:
        self.send_ctrl(bytes([CtrlMsg.ERROR]) + CtrlError(status).pack())
        self.tag.stats[f"error_{status.name}"] += 1
        raise _EndSession

    # -- handshake -----------------------------------------------------------------

    async def on_ctrl(self, message: bytes) -> None:
        tag, nvs = self.tag, self.tag.nvs
        kind, body = message[0], message[1:]
        try:
            if kind == CtrlMsg.HELLO and self.hello is None:
                hello = CtrlHello.unpack(body)
                if hello.proto != PROTO_VERSION:
                    self.error(Status.VERSION_MISMATCH)
                if hello.tag_id != tag.tag_id:
                    self.error(Status.NOT_FOUND)
                if hello.epoch < nvs.stored_epoch:
                    tag.stats["stale_epoch_refused"] += 1
                    self.error(Status.STALE_EPOCH)
                self.hello, self.hello_msg = hello, message
                record = nvs.record
                flags = (1 if tag.unknown_pending else 0) | (2 if tag.battery_mv < LOW_BATTERY_MV else 0)
                challenge = CtrlChallenge(PROTO_VERSION, tag.rng.randbytes(16), nvs.stored_epoch,
                                          record.revision if record else 0, record.status if record else Status.OK,
                                          int(tag.battery_mv), flags)
                self.challenge_msg = bytes([CtrlMsg.CHALLENGE]) + challenge.pack()
                self.send_ctrl(self.challenge_msg)
            elif kind == CtrlMsg.AUTH and self.hello is not None and self.receiver is None:
                auth = CtrlAuth.unpack(body)
                k_epoch = crypto.derive_k_epoch(tag.spec.secret, tag.tag_id, self.epoch)
                th = crypto.transcript_hash(self.hello_msg, self.challenge_msg)
                ok = crypto.constant_time_equal(crypto.mac_b(k_epoch, th), auth.mac_b)
                if tag.faults.auth_fail > 0:
                    tag.faults.auth_fail -= 1
                    ok = False
                if not ok:
                    tag._auth_failures += 1
                    tag.stats["auth_failures"] += 1
                    if tag._auth_failures >= AUTH_FAILURES_BEFORE_SKIP:
                        tag._auth_failures = 0
                        tag._skip_windows += 1  # anti-brute-force pacing (§5.4)
                    self.error(Status.AUTH_FAILED)
                tag._auth_failures = 0
                if self.epoch > nvs.stored_epoch:
                    nvs.stored_epoch = self.epoch  # persisted only after AUTH verified (§5.4)
                self.send_ctrl(bytes([CtrlMsg.AUTH_OK]) + CtrlAuthOk(crypto.mac_t(k_epoch, th, auth.mac_b)).pack())
                k_b2t, k_t2b = crypto.session_keys(k_epoch, th)
                self.receiver = crypto.RecordReceiver(k_b2t, RecordDir.B2T)
                self.sender = crypto.RecordSender(k_t2b, RecordDir.T2B)
                self.grant(tag.spec.credits)
            else:
                self.error(Status.INVALID)
        except MessageError:
            self.error(Status.INVALID)

    # -- records ---------------------------------------------------------------------

    async def on_record(self, record: bytes) -> None:
        if self.receiver is None:
            self.error(Status.INVALID)
        assert self.receiver is not None
        try:
            kind, plaintext = self.receiver.open(record)
        except crypto.AuthError:
            self.tag.stats["record_auth_failures"] += 1
            self.error(Status.AUTH_FAILED)
        if self.granted <= 0:
            self.tag.stats["credit_violations"] += 1
            self.error(Status.INVALID)
        self.granted -= 1
        try:
            if kind == RecordType.FRAME_BEGIN:
                self.frame_begin(RecFrameBegin.unpack(plaintext))
            elif kind == RecordType.PLANE_DATA:
                self.plane_data(RecPlaneData.unpack(plaintext))
            elif kind == RecordType.FRAME_END:
                await self.frame_end()
            elif kind == RecordType.FRAME_ABORT:
                self.frame = None
            elif kind == RecordType.CMD:
                await self.command(RecCmd.unpack(plaintext))
            else:
                self.error(Status.INVALID)
        except MessageError:
            self.error(Status.INVALID)
        self.grant(1)  # this record's buffer is free again

    def frame_begin(self, fb: RecFrameBegin) -> None:
        tag, spec = self.tag, self.tag.spec
        self.frame = None
        decision = frame_begin_decision(tag.nvs.record, self.epoch, fb.revision, fb.digest, fb.planes, fb.plane_len,
                                        spec.planes, spec.plane_len)
        if decision.duplicate:
            stored = tag.nvs.record
            assert stored is not None
            tag.stats["duplicates"] += 1
            self.send_result(stored.update_id, stored.epoch, stored.revision, Status.OK, stored.digest, flags=1)
            return
        if not decision.accept:
            assert decision.status is not None
            self.send_result(fb.update_id, self.epoch, fb.revision, decision.status)
            return
        self.frame = _Frame(self.epoch, fb.revision, fb.update_id, fb.digest, fb.planes, fb.plane_len,
                            [bytearray() for _ in range(fb.planes)], [0] * fb.planes)

    def _reject_frame(self, status: Status) -> None:
        frame = self.frame
        assert frame is not None
        self.frame = None
        self.tag.stats["frames_aborted"] += 1
        self.send_result(frame.update_id, frame.epoch, frame.revision, status)

    def plane_data(self, pd: RecPlaneData) -> None:
        frame = self.frame
        if frame is None:
            self.tag.stats["stray_plane_data"] += 1
            return
        current = next((p for p in range(frame.planes) if frame.received[p] < frame.plane_len), frame.planes)
        if (pd.plane != current or pd.offset != frame.received[pd.plane]
                or pd.offset + len(pd.data) > frame.plane_len or not pd.data):
            self._reject_frame(Status.INVALID)
            return
        frame.staged[pd.plane] += pd.data  # write_plane_chunk(): controller RAM, no refresh
        frame.received[pd.plane] += len(pd.data)
        frame.hasher.update(pd.data)
        frame.records += 1
        faults = self.tag.faults
        if faults.disconnect_after_records is not None and frame.records >= faults.disconnect_after_records:
            faults.disconnect_after_records = None
            self.tag.stats["fault_disconnects"] += 1
            self.link.disconnect("fault: tag dropped the link mid-transfer")
            raise LinkLost("fault: disconnect mid-transfer")

    async def frame_end(self) -> None:
        frame = self.frame
        if frame is None:
            return
        if any(r != frame.plane_len for r in frame.received):
            self._reject_frame(Status.INCOMPLETE)
            return
        if frame.hasher.digest() != frame.digest:
            self._reject_frame(Status.DIGEST_MISMATCH)
            return
        planes = tuple(bytes(p) for p in frame.staged)
        self.frame = None  # validated: from here on the §6 transaction owns the frame
        await self.transaction(frame.epoch, frame.revision, frame.update_id, frame.digest, planes)

    async def command(self, cmd: RecCmd) -> None:
        tag = self.tag
        if cmd.cmd == TagCommand.CLEAR:
            tag.stats["clears"] += 1
            planes = tag.spec.white_planes()
            await self.transaction(self.epoch, 0, cmd.update_id, hashlib.sha256(b"".join(planes)).digest(), planes)
        elif cmd.cmd == TagCommand.SLEEP and tag.spec.sleep_supported:
            self.send_result(cmd.update_id, self.epoch, 0, Status.OK)
        else:
            self.send_result(cmd.update_id, self.epoch, 0, Status.UNSUPPORTED)

    async def transaction(self, epoch: int, revision: int, update_id: int, digest: bytes,
                          planes: tuple[bytes, ...]) -> None:
        """§6: REFRESH_INTENT persisted -> refresh -> DISPLAYED persisted -> RESULT."""
        tag = self.tag
        intent = DisplayRecord(tag.tag_id, epoch, revision, update_id, digest, Status.OK, StoredState.REFRESH_INTENT)
        tag.nvs.record = intent
        self.send_record(RecordType.PROGRESS, RecProgress(DeliveryStage.REFRESHING).pack())
        tag.result_pending = True
        refresh_ms = tag.spec.refresh_ms * tag.rng.uniform(0.95, 1.05)
        if tag.faults.power_loss > 0:
            tag.faults.power_loss -= 1
            await tag.clock.sleep_ms(refresh_ms * tag.rng.uniform(0.2, 0.8))
            if tag.rng.random() < 0.5:
                tag.panel_planes = planes  # the refresh may have completed before the reset
            raise _PowerLoss
        if tag.faults.refresh_timeout > 0:
            tag.faults.refresh_timeout -= 1
            await tag.clock.sleep_ms(refresh_ms * 3)
            tag.nvs.record = replace(intent, status=Status.REFRESH_TIMEOUT)
            self.send_result(update_id, epoch, revision, Status.REFRESH_TIMEOUT)
            return
        started = tag.clock.now_ms()
        await tag.clock.sleep_ms(refresh_ms)  # commit_refresh() + wait_refresh_complete()
        tag.panel_planes = planes
        tag.stats["refreshes"] += 1
        tag._drain(_COST_REFRESH_MV.get(tag.spec.planes, 1.0))
        tag.nvs.record = replace(intent, state=StoredState.DISPLAYED)
        self.send_result(update_id, epoch, revision, Status.OK, digest, int(tag.clock.now_ms() - started))
