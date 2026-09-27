"""Simulated bridge (docs/protocol.md §2, §3, §5; docs/fontpack.md §3–§4).

Mesh side (``LAYOUT_SRV`` + ``MGMT_SRV``): one layout being assembled at a time
(a ``LAYOUT_BEGIN`` with a new ``xfer_id`` replaces it); ``LAYOUT_COMMIT``
validates in the order of §3.3 — transfer exists, all chunks present (else
``INCOMPLETE`` + missing bitmap), length, digest, assignment/epoch, revision
(``DUPLICATE`` re-sends the stored result), font pack, layout bounds and
strikes. A validated layout replaces an older pending one for the tag (reported
``SUPERSEDED``). Results go out as ``DELIVERY_RESULT`` with a bridge-local
``result_seq``, re-sent every ``MESH_RESULT_RETRY_MS`` up to
``MESH_RESULT_RETRIES`` times until ``RESULT_ACK``. The assignment table holds
``K_epoch`` per tag (``ASSIGN_SET``/``ASSIGN_DEL``).

Tag side (§5.2): when an advertisement of a tag with pending work is seen, the
scheduler applies the rate limit (``BRIDGE_MAX_SUSPENDS_PER_MIN`` per rolling
minute) and per-tag back-off (``BRIDGE_TAG_BACKOFF_MS`` after a failure), waits
for its own mesh sends, suspends the mesh (injectable failure:
``MESH_SUSPEND_FAILED``), connects within ``BRIDGE_CONN_ATTEMPT_MS``, resumes the
mesh (injectable failure: ``MESH_RESUME_FAILED`` + recovery, reboot after 5 s)
and measures ``suspend_ms``. The session renders the frame with the reference
renderer from the active font pack and the tag's CAPS (so its digest is the one a
real bridge computes), then runs the real handshake and records, at most 4
records per connection event, respecting the tag's credits. After
``FRAME_BEGIN`` the bridge waits for that record's credit (or an immediate
``RESULT``) before streaming plane data.

A job whose ``FRAME_END`` was sent but whose ``RESULT`` never arrived (the link
dropped during the refresh) is resolved on the next session: if the tag's
``CHALLENGE`` reports ``DISPLAY_STATE_UNKNOWN`` for that very ``(epoch,
revision)``, the job ends ``DISPLAY_STATE_UNKNOWN`` (outcome ``UNCERTAIN``);
otherwise ``FRAME_BEGIN`` is sent again and the tag's decision table answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from ..fontpack.format import FontPack
from ..protocol import session as crypto
from ..protocol.fragments import Fragmenter, FragmentError, Reassembler
from ..protocol.ids import (
    BRIDGE_CONN_ATTEMPT_MS,
    BRIDGE_MAX_SUSPENDS_PER_MIN,
    BRIDGE_TAG_BACKOFF_MS,
    LAYOUT_CHUNK_DATA_MAX,
    LAYOUT_DIGEST_LEN,
    LAYOUT_HARD_MAX,
    MAX_TAGS_PER_BRIDGE,
    MESH_RESULT_RETRIES,
    MESH_RESULT_RETRY_MS,
    PROTO_VERSION,
    SERIAL_MAX_FRAME,
    TAG_CTRL_MSG_MAX,
    TAG_PLANE_DATA_MAX,
    TAG_RECORD_WIRE_MAX,
    Board,
    CtrlMsg,
    DeliveryStage,
    GattChr,
    NodeRole,
    PlainMsg,
    RecordDir,
    RecordType,
    SerialMsg,
    Status,
    TagCommand,
)
from ..protocol.layout import LayoutError, check_strikes, decode_layout, layout_digest
from ..protocol.msgs import (
    CtrlAuth,
    CtrlAuthOk,
    CtrlChallenge,
    CtrlError,
    CtrlHello,
    FixedMessage,
    MeshAssignDel,
    MeshAssignSet,
    MeshAssignStatus,
    MeshCapsGet,
    MeshCapsStatus,
    MeshDeliveryResult,
    MeshDeliveryStage,
    MeshHealthGet,
    MeshHealthStatus,
    MeshIdentify,
    MeshLayoutBegin,
    MeshLayoutCancel,
    MeshLayoutChunk,
    MeshLayoutCommit,
    MeshLayoutStatus,
    MeshResultAck,
    MeshTagCmd,
    MeshTagSeen,
    MessageError,
    PlainCredit,
    RecCmd,
    RecFrameBegin,
    RecPlaneData,
    RecProgress,
    RecResult,
    TagCaps,
)
from ..render.reference import Panel, render_frame
from .core import Pacer, SimClock, TaskSet
from .device import DeviceEndpoint, Reply
from .flash import MIB, FlashError, FontStore, SimFlash
from .mesh import GATEWAY_ADDR, MeshNetwork
from .radio import Advert, Air, ConnectFailed, GattLink, LinkLost

log = logging.getLogger(__name__)

FW = (0, 1, 0)
CONN_INTERVAL_MS = 40.0  # 30–50 ms (§5.2 step 8)
RECORDS_PER_EVENT = 4
STEP_TIMEOUT_MS = 5000.0
RESULT_TIMEOUT_MS = 60000.0  # longest panel refresh bound
TAG_SEEN_INTERVAL_MS = 60000.0
RESUME_RETRY_MS = 500.0
RESUME_RECOVERY_MS = 5000.0  # §5.2 step 7: reboot when resume still fails after 5 s
FINAL_TAG_ERRORS = frozenset({Status.AUTH_FAILED, Status.STALE_EPOCH, Status.VERSION_MISMATCH, Status.NOT_FOUND})
U16 = 0xFFFF


@dataclass
class BridgeFaults:
    suspend_fail: float = 0.0  # probability that bt_mesh_suspend() fails
    suspend_fail_next: int = 0  # the next N suspends fail
    resume_fail_next: int = 0  # the next N resumes fail


@dataclass
class Assignment:
    tag_id: int
    epoch: int
    key: bytes  # K_epoch
    flags: int = 0


@dataclass
class StoredResult:
    status: Status
    digest8: bytes
    battery_mv: int
    timing: dict[str, int]


@dataclass
class History:
    """Last accepted revision per (tag, epoch) and its final result (kept in flash)."""

    revision: int
    digest: bytes  # 16-byte layout digest
    result: StoredResult | None = None


@dataclass(eq=False)
class Job:
    kind: str  # "layout" | "cmd"
    update_id: int
    tag_id: int
    epoch: int
    revision: int = 0
    layout: bytes = b""
    layout_digest: bytes = b""
    fontpack_id: bytes = b""
    cmd: int = 0
    validated_ms: float = 0.0
    frame_end_sent: bool = False
    in_session: bool = False


@dataclass
class _Transfer:
    begin: MeshLayoutBegin
    chunks: dict[int, bytes] = field(default_factory=dict)


class _SessionEnd(Exception):
    """The session is over (jobs already finished or left pending)."""


class SimBridge:
    """One simulated bridge (see the module docstring)."""

    def __init__(self, name: str, *, uuid: bytes, clock: SimClock, mesh: MeshNetwork, air: Air, rng: random.Random,
                 flash_size: int = 64 * MIB, board: int = Board.NRF52840_BRIDGE, faults: BridgeFaults | None = None,
                 bad_sectors: tuple[int, ...] = ()) -> None:
        self.name = name
        self.uuid = uuid
        self.clock = clock
        self.mesh = mesh
        self.air = air
        self.rng = rng
        self.board = board
        self.faults = faults or BridgeFaults()
        self.flash = SimFlash(flash_size, bad_sectors)
        self.fonts = FontStore(self.flash)
        self.counters: Counter[str] = Counter()
        self.addr: int | None = None
        self.configured = False
        self.relay = True
        self.ttl = 0
        self.assignments: dict[int, Assignment] = {}
        self.history: dict[tuple[int, int], History] = {}
        self.jobs: dict[int, list[Job]] = {}
        self.last_status: Status = Status.OK
        self.tag_battery: dict[int, int] = {}
        self.boot_id = rng.getrandbits(32)
        self._boot_ms = clock.now_ms()
        self._xfer: _Transfer | None = None
        self._result_seq = 0
        self._result_acks: dict[int, asyncio.Event] = {}
        self._suspended = False
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._sending = 0
        self._suspend_times: deque[float] = deque()
        self._backoff_until: dict[int, float] = {}
        self._tag_seen_at: dict[int, float] = {}
        self._session_task: asyncio.Task[None] | None = None
        self._tasks = TaskSet(f"bridge {name}")
        self._flash_tests: dict[int, dict[str, Any]] = {}
        self.maint = DeviceEndpoint(f"bridge {name} maint", self._maint, supported=(
            SerialMsg.PING, SerialMsg.INFO, SerialMsg.REBOOT, SerialMsg.FONT_BEGIN, SerialMsg.FONT_DATA,
            SerialMsg.FONT_COMMIT, SerialMsg.FONT_STATUS, SerialMsg.FONT_ABORT, SerialMsg.FLASH_TEST))

    # -- identity and lifecycle ------------------------------------------------------

    @property
    def provisioned(self) -> bool:
        return self.addr is not None

    @property
    def fontpack(self) -> FontPack | None:
        return self.fonts.active_pack()

    @property
    def fontpack_id(self) -> bytes | None:
        pack = self.fontpack
        return pack.pack_id if pack is not None else None

    def uptime_s(self) -> int:
        return int((self.clock.now_ms() - self._boot_ms) / 1000)

    def start(self) -> None:
        self.air.scanners.append(self)

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.air.scanners.remove(self)
        await self._tasks.cancel_all()
        await self.maint.stop()

    async def reboot(self) -> None:
        """Lose RAM state; settings (mesh, assignments) and flash (packs, pending layouts) survive."""
        self.counters["reboots"] += 1
        self.maint.drop_connection()
        self.maint.new_boot()
        if self._session_task is not None:
            self._session_task.cancel()
        self._xfer = None
        self.fonts.abort()
        self._result_acks.clear()
        self._suspended = False
        self._resumed.set()
        self.boot_id = self.rng.getrandbits(32)
        self._boot_ms = self.clock.now_ms()
        for jobs in self.jobs.values():
            for job in jobs:
                job.in_session = False

    # -- provisioning (driven by the simulated gateway) -----------------------------------

    def provision(self, addr: int) -> None:
        self.addr = addr
        self.configured = False
        self.mesh.attach(addr, self)

    def configure(self, relay: bool, ttl: int) -> None:
        self.configured = True
        self.relay = relay
        self.ttl = ttl

    def reset_node(self) -> None:
        """Config Node Reset: back to an unprovisioned device; mesh and tag state wiped."""
        if self.addr is not None:
            self.mesh.detach(self.addr)
        self.addr = None
        self.configured = False
        self.assignments.clear()
        self.history.clear()
        self.jobs.clear()
        self._xfer = None

    # -- mesh node interface -----------------------------------------------------------

    @property
    def mesh_suspended(self) -> bool:
        return self._suspended

    async def wait_mesh_resumed(self) -> None:
        await self._resumed.wait()

    def mesh_accepts(self, msg: FixedMessage) -> bool:
        return self.provisioned and self.configured  # vendor models need the bound app key

    async def _mesh_send(self, msg: FixedMessage) -> bool:
        if self.addr is None:
            return False
        self._sending += 1
        try:
            return await self.mesh.send(self.addr, GATEWAY_ADDR, msg)
        finally:
            self._sending -= 1

    def _send_later(self, msg: FixedMessage) -> None:
        self._tasks.spawn(self._mesh_send(msg), type(msg).__name__)

    def mesh_receive(self, src: int, msg: FixedMessage) -> None:
        self.counters[f"rx_{type(msg).__name__}"] += 1
        if isinstance(msg, MeshLayoutBegin):
            self._xfer = _Transfer(msg)  # one layout being assembled per bridge (§3.2 rule 4)
        elif isinstance(msg, MeshLayoutChunk):
            xfer = self._xfer
            if xfer is not None and msg.xfer_id == xfer.begin.xfer_id and msg.index < xfer.begin.chunk_count:
                xfer.chunks[msg.index] = msg.data
            else:
                self.counters["stray_chunks"] += 1
        elif isinstance(msg, MeshLayoutCommit):
            self._commit(msg.xfer_id)
        elif isinstance(msg, MeshLayoutCancel):
            self._cancel(msg.update_id)
        elif isinstance(msg, MeshResultAck):
            event = self._result_acks.get(msg.result_seq)
            if event is not None:
                event.set()
        elif isinstance(msg, MeshCapsGet):
            self._send_later(self.caps_status())
        elif isinstance(msg, MeshHealthGet):
            self._send_later(self.health_status())
        elif isinstance(msg, MeshAssignSet):
            status = self._assign_set(msg)
            self._send_later(MeshAssignStatus(msg.tag_id, msg.epoch, status))
        elif isinstance(msg, MeshAssignDel):
            status = self._assign_del(msg)
            self._send_later(MeshAssignStatus(msg.tag_id, msg.epoch, status))
        elif isinstance(msg, MeshTagCmd):
            self._tag_cmd(msg)
        elif isinstance(msg, MeshIdentify):
            self.counters["identify"] += 1
        else:
            self.counters["unexpected_messages"] += 1

    def caps_status(self) -> MeshCapsStatus:
        pack_id = self.fontpack_id
        flags = (1 if pack_id is not None else 0) | (2 if self._session_task is not None else 0)
        return MeshCapsStatus(PROTO_VERSION, *FW, self.board, pack_id or bytes(8), min(self.flash.size // MIB, U16),
                              MAX_TAGS_PER_BRIDGE, len(self.assignments), flags)

    def health_status(self) -> MeshHealthStatus:
        c = self.counters
        return MeshHealthStatus(self.uptime_s(), min(c["sessions_ok"], U16), min(c["sessions_fail"], U16),
                                min(c["suspend_count"], U16), min(c["suspend_max_ms"], U16), min(c["resume_fail"], U16),
                                min(sum(len(j) for j in self.jobs.values()), 255), int(self.last_status))

    # -- layout transfer (§3.3) ------------------------------------------------------------

    def _commit(self, xfer_id: int) -> None:
        status, missing = self._validate(xfer_id)
        self._send_later(MeshLayoutStatus(xfer_id, status, missing))

    def _validate(self, xfer_id: int) -> tuple[Status, int]:
        xfer = self._xfer
        if xfer is None or xfer.begin.xfer_id != xfer_id:
            return Status.NOT_FOUND, 0
        b = xfer.begin
        missing = sum(1 << i for i in range(b.chunk_count) if i not in xfer.chunks)
        if missing:
            self.counters["incomplete"] += 1
            return Status.INCOMPLETE, missing  # the transfer stays open for the resent chunks
        self._xfer = None
        data = b"".join(xfer.chunks[i] for i in range(b.chunk_count))
        if b.total_len > LAYOUT_HARD_MAX:
            return Status.TOO_LARGE, 0
        if len(data) != b.total_len or any(len(xfer.chunks[i]) != LAYOUT_CHUNK_DATA_MAX
                                           for i in range(b.chunk_count - 1)):
            return Status.INVALID, 0
        if layout_digest(data) != b.digest:
            return Status.DIGEST_MISMATCH, 0
        assignment = self.assignments.get(b.tag_id)
        if assignment is None or assignment.epoch < b.epoch:
            return Status.NOT_ASSIGNED, 0
        if assignment.epoch > b.epoch:
            return Status.STALE_EPOCH, 0
        history = self.history.get((b.tag_id, b.epoch))
        if history is not None and b.revision <= history.revision:
            if b.revision != history.revision or b.digest != history.digest:
                return Status.STALE_REVISION, 0
            duplicate = self._duplicate(b, history)
            if duplicate is not None:
                return duplicate, 0
            # Same revision, never displayed (DISPLAY_STATE_UNKNOWN, cancelled, ...): a re-delivery.
        pack = self.fontpack
        if pack is None or b.fontpack_id != pack.pack_id:
            return Status.FONTPACK_MISMATCH, 0
        try:
            check_strikes(decode_layout(data), pack.has_strike)
        except LayoutError as exc:
            return exc.status, 0
        self._accept(b, data)
        return Status.OK, 0

    def _duplicate(self, b: MeshLayoutBegin, history: History) -> Status | None:
        """Same revision and digest (§3.3): ``DUPLICATE`` when it was displayed or is still pending.

        A displayed revision gets its stored result re-sent, named by the new
        ``update_id``; a pending one adopts the new ``update_id`` for its eventual
        result. ``None``: the revision ended without being displayed, so it is
        accepted again as a re-delivery.
        """
        stored = history.result
        if stored is not None and stored.status == Status.OK:
            self.counters["duplicates"] += 1
            self._send_result(b.update_id, b.tag_id, b.epoch, b.revision, stored.status, stored.digest8,
                              stored.battery_mv, stored.timing)
            return Status.DUPLICATE
        pending = next((j for j in self.jobs.get(b.tag_id, []) if j.kind == "layout" and j.revision == b.revision
                        and j.epoch == b.epoch), None)
        if pending is not None:
            self.counters["duplicates"] += 1
            pending.update_id = b.update_id
            return Status.DUPLICATE
        return None

    def _accept(self, b: MeshLayoutBegin, data: bytes) -> None:
        self.history[(b.tag_id, b.epoch)] = History(b.revision, b.digest)
        for old in [j for j in self.jobs.get(b.tag_id, []) if j.kind == "layout" and not j.in_session]:
            self._finish(old, Status.SUPERSEDED)
        self.jobs.setdefault(b.tag_id, []).append(Job(
            "layout", b.update_id, b.tag_id, b.epoch, b.revision, data, b.digest, b.fontpack_id,
            validated_ms=self.clock.now_ms()))
        self.counters["layouts_accepted"] += 1

    def _cancel(self, update_id: int) -> None:
        for jobs in self.jobs.values():
            for job in jobs:
                if job.update_id == update_id and not job.in_session:
                    self._finish(job, Status.CANCELLED)
                    return

    # -- assignments and commands --------------------------------------------------------------

    def _assign_set(self, m: MeshAssignSet) -> Status:
        current = self.assignments.get(m.tag_id)
        if current is not None and current.epoch > m.epoch:
            return Status.STALE_EPOCH
        if current is None and len(self.assignments) >= MAX_TAGS_PER_BRIDGE:
            return Status.NO_RESOURCES
        if current is not None and current.epoch < m.epoch:
            self._cancel_tag(m.tag_id, older_than=m.epoch)
        self.assignments[m.tag_id] = Assignment(m.tag_id, m.epoch, m.key, m.flags)
        return Status.OK

    def _assign_del(self, m: MeshAssignDel) -> Status:
        current = self.assignments.get(m.tag_id)
        if current is None:
            return Status.OK  # idempotent
        if current.epoch > m.epoch:
            return Status.STALE_EPOCH
        del self.assignments[m.tag_id]
        self._cancel_tag(m.tag_id, older_than=m.epoch + 1)
        return Status.OK

    def _cancel_tag(self, tag_id: int, older_than: int) -> None:
        for job in [j for j in self.jobs.get(tag_id, []) if j.epoch < older_than and not j.in_session]:
            self._finish(job, Status.CANCELLED)

    def _tag_cmd(self, m: MeshTagCmd) -> None:
        assignment = self.assignments.get(m.tag_id)
        if assignment is None or assignment.epoch < m.epoch:
            self._send_result(m.update_id, m.tag_id, m.epoch, 0, Status.NOT_ASSIGNED)
        elif assignment.epoch > m.epoch:
            self._send_result(m.update_id, m.tag_id, m.epoch, 0, Status.STALE_EPOCH)
        else:
            self.jobs.setdefault(m.tag_id, []).append(Job("cmd", m.update_id, m.tag_id, m.epoch, cmd=m.cmd,
                                                          validated_ms=self.clock.now_ms()))

    # -- results -----------------------------------------------------------------------------

    def _finish(self, job: Job, status: Status, *, digest8: bytes = bytes(8), battery_mv: int = 0,
                timing: dict[str, int] | None = None) -> None:
        jobs = self.jobs.get(job.tag_id, [])
        if job in jobs:
            jobs.remove(job)
        if not jobs:
            self.jobs.pop(job.tag_id, None)
        timing = timing or {}
        if job.kind == "layout":
            history = self.history.get((job.tag_id, job.epoch))
            if history is not None and history.revision == job.revision and history.digest == job.layout_digest:
                history.result = StoredResult(status, digest8, battery_mv, timing)
        elif job.cmd == TagCommand.CLEAR and status == Status.OK:
            # The tag now stores revision 0 for this epoch (§5.6); mirror it so no stale DUPLICATE
            # short-circuits a re-delivery of the revision that was shown before the clear.
            self.history[(job.tag_id, job.epoch)] = History(0, bytes(LAYOUT_DIGEST_LEN))
        self.counters[f"result_{status.name}"] += 1
        self._send_result(job.update_id, job.tag_id, job.epoch, job.revision, status, digest8, battery_mv, timing)

    def _send_result(self, update_id: int, tag_id: int, epoch: int, revision: int, status: Status,
                     digest8: bytes = bytes(8), battery_mv: int = 0, timing: dict[str, int] | None = None) -> None:
        timing = timing or {}
        self._result_seq = (self._result_seq + 1) & U16
        msg = MeshDeliveryResult(self._result_seq, update_id, tag_id, epoch, revision, status, digest8[:8],
                                 min(battery_mv, U16), *(min(int(timing.get(k, 0)), U16) for k in (
                                     "wake_ms", "suspend_ms", "transfer_ms", "refresh_ms")))
        self._result_acks[msg.result_seq] = asyncio.Event()
        self._tasks.spawn(self._deliver_result(msg), f"result {msg.result_seq}")

    async def _deliver_result(self, msg: MeshDeliveryResult) -> None:
        acked = self._result_acks[msg.result_seq]
        try:
            for attempt in range(1 + MESH_RESULT_RETRIES):
                if attempt:
                    self.counters["result_resends"] += 1
                await self._mesh_send(msg)
                try:
                    await self.clock.wait_for(acked.wait(), MESH_RESULT_RETRY_MS)
                    return
                except TimeoutError:
                    continue
            self.counters["results_unacked"] += 1
        finally:
            self._result_acks.pop(msg.result_seq, None)

    def _stage(self, job: Job, stage: DeliveryStage) -> None:
        self._send_later(MeshDeliveryStage(job.update_id, job.tag_id, job.revision, stage))

    # -- scanning and scheduling (§5.2) ---------------------------------------------------------

    def scanning(self) -> bool:
        return self.provisioned and self.configured and not self._suspended

    def on_advert(self, data: bytes, rssi: int) -> None:
        advert = Advert.parse(data)
        if advert is None:
            return
        tag_id, now = advert.tag_id, self.clock.now_ms()
        self.counters["adverts_seen"] += 1
        last_seen = self._tag_seen_at.get(tag_id, -TAG_SEEN_INTERVAL_MS)
        if tag_id in self.assignments and now - last_seen >= TAG_SEEN_INTERVAL_MS:
            self._tag_seen_at[tag_id] = now
            self._send_later(MeshTagSeen(tag_id, max(-128, min(127, rssi)), self.tag_battery.get(tag_id, 0),
                                         advert.flags))
        if not self.jobs.get(tag_id) or self._session_task is not None or tag_id not in self.assignments:
            return
        if now < self._backoff_until.get(tag_id, 0.0):
            self.counters["backoff_skips"] += 1
            return
        while self._suspend_times and now - self._suspend_times[0] >= 60000.0:
            self._suspend_times.popleft()
        if len(self._suspend_times) >= BRIDGE_MAX_SUSPENDS_PER_MIN:
            self.counters["rate_limited"] += 1
            return
        self._session_task = self._tasks.spawn(self._attempt(tag_id), f"attempt {tag_id:08X}")

    def _backoff(self, tag_id: int, status: Status) -> None:
        self.last_status = status
        self._backoff_until[tag_id] = self.clock.now_ms() + BRIDGE_TAG_BACKOFF_MS

    def _suspend_fails(self) -> bool:
        if self.faults.suspend_fail_next > 0:
            self.faults.suspend_fail_next -= 1
            return True
        return self.faults.suspend_fail > 0 and self.rng.random() < self.faults.suspend_fail

    async def _attempt(self, tag_id: int) -> None:
        try:
            if self._sending:  # step 3: let our own mesh sends finish, or defer to a later advertisement
                deadline = self.clock.now_ms() + 500.0
                while self._sending and self.clock.now_ms() < deadline:
                    await self.clock.sleep_ms(20.0)
                if self._sending:
                    self.counters["deferred"] += 1
                    return
            started = self.clock.now_ms()
            self._suspend_times.append(started)
            self.counters["suspend_count"] += 1
            if self._suspend_fails():  # step 4
                self.counters["suspend_fail"] += 1
                self._backoff(tag_id, Status.MESH_SUSPEND_FAILED)
                return
            self._suspended = True
            self._resumed.clear()
            link: GattLink | None = None
            try:  # step 5
                link = await self.air.connect(self.name, tag_id, BRIDGE_CONN_ATTEMPT_MS)
            except ConnectFailed:
                pass
            resumed = await self._resume()  # step 6
            suspend_ms = int(self.clock.now_ms() - started)
            self.counters["suspend_max_ms"] = max(self.counters["suspend_max_ms"], suspend_ms)
            if not resumed:
                if link is not None:
                    link.disconnect("bridge mesh resume failed")
                self._backoff(tag_id, Status.MESH_RESUME_FAILED)
                return
            if link is None:
                self.counters["connect_failed"] += 1
                self._backoff(tag_id, Status.CONNECT_FAILED)
                return
            await self._session(link, tag_id, suspend_ms)  # step 8
        finally:
            self._session_task = None

    async def _resume(self) -> bool:
        if self.faults.resume_fail_next <= 0:
            self._suspended = False
            self._resumed.set()
            return True
        self.faults.resume_fail_next -= 1
        self.counters["resume_fail"] += 1
        self._tasks.spawn(self._resume_recovery(), "resume recovery")
        return False

    async def _resume_recovery(self) -> None:
        """§5.2 step 7: retry with back-off; reboot (mesh reloads from settings) after 5 s."""
        started = self.clock.now_ms()
        while self.clock.now_ms() - started < RESUME_RECOVERY_MS:
            await self.clock.sleep_ms(RESUME_RETRY_MS)
            if self.faults.resume_fail_next <= 0:
                self._suspended = False
                self._resumed.set()
                return
            self.faults.resume_fail_next -= 1
            self.counters["resume_fail"] += 1
        await self.reboot()

    async def _session(self, link: GattLink, tag_id: int, suspend_ms: int) -> None:
        session = _BridgeSession(self, link, tag_id, suspend_ms)
        try:
            await session.run()
            self.counters["sessions_ok"] += 1
            self.last_status = Status.OK
        except _SessionEnd:
            self.counters["sessions_ok" if session.status == Status.OK else "sessions_fail"] += 1
            if session.status != Status.OK:
                self._backoff(tag_id, session.status)
        except LinkLost:
            self.counters["sessions_fail"] += 1
            self._backoff(tag_id, Status.DISCONNECTED)
        except TimeoutError:
            self.counters["sessions_fail"] += 1
            self._backoff(tag_id, Status.TIMEOUT)
        except (FragmentError, crypto.AuthError, MessageError) as exc:
            self.counters["sessions_fail"] += 1
            self.counters["protocol_errors"] += 1
            log.warning("bridge %s: session with %08X failed: %s", self.name, tag_id, exc)
            self._backoff(tag_id, Status.INVALID)
        finally:
            link.disconnect("bridge ended the session")

    # -- maintenance port -------------------------------------------------------------------

    def _caps(self) -> dict[str, Any]:
        return {"max_frame": SERIAL_MAX_FRAME, "credits": self.maint.rx_buffers, "role": NodeRole.BRIDGE,
                "board": self.board}

    async def _maint(self, msg: SerialMsg, f: dict[str, Any]) -> Reply | dict[str, Any]:
        match msg:
            case SerialMsg.HELLO:
                if f.get("proto") != PROTO_VERSION:
                    return {"status": Status.VERSION_MISMATCH, "proto": PROTO_VERSION}
                return {"status": Status.OK, "proto": PROTO_VERSION, "fw": ".".join(map(str, FW)), "build": "sim",
                        "boot_id": self.boot_id, "caps": self._caps()}
            case SerialMsg.PING:
                return {"status": Status.OK, "uptime_s": self.uptime_s()}
            case SerialMsg.INFO:
                counters = {k: min(int(v), 0xFFFFFFFF) for k, v in (self.counters + self.maint.counters).items()
                            if v >= 0}
                return {"status": Status.OK, "fw": ".".join(map(str, FW)), "build": "sim", "boot_id": self.boot_id,
                        "caps": self._caps(), "counters": counters}
            case SerialMsg.REBOOT:
                return Reply({"status": Status.OK}, after=self.reboot)
            case SerialMsg.FONT_STATUS:
                record = self.fonts.active()
                out: dict[str, Any] = {"status": Status.OK, "flash_size": self.flash.size}
                if record is not None:
                    out.update(fontpack_id=record.pack_id, slot=record.slot, size=record.size)
                return out
            case SerialMsg.FONT_ABORT:
                self.fonts.abort()
                return {"status": Status.OK}
            case SerialMsg.FLASH_TEST:
                cached = self._flash_tests.get(f["op_id"])
                if cached is not None:
                    return {**cached, "detail": Status.DUPLICATE}
                items = [{"offset": off, "status": st} for off, st in self.fonts.flash_test()]
                response = {"status": Status.OK, "flash_size": self.flash.size, "items": items}
                self._flash_tests[f["op_id"]] = response
                return response
        try:
            match msg:
                case SerialMsg.FONT_BEGIN:
                    slot = self.fonts.begin(f["size"], f["digest"], f["fontpack_id"])
                    return {"status": Status.OK, "slot": slot, "flash_size": self.flash.size}
                case SerialMsg.FONT_DATA:
                    self.fonts.data(f["offset"], f["data"])
                    return {"status": Status.OK}
                case SerialMsg.FONT_COMMIT:
                    return {"status": Status.OK, "fontpack_id": self.fonts.commit()}
        except FlashError as exc:
            return {"status": exc.status, "text": str(exc)[:120]}
        return {"status": Status.UNSUPPORTED}

    # -- persistence ------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {"uuid": self.uuid.hex(), "name": self.name, "addr": self.addr, "configured": self.configured,
                "assignments": [{"tag_id": a.tag_id, "epoch": a.epoch, "key": a.key.hex(), "flags": a.flags}
                                for a in self.assignments.values()],
                "history": [{"tag_id": t, "epoch": e, "revision": h.revision, "digest": h.digest.hex(),
                             "result": None if h.result is None else {
                                 "status": int(h.result.status), "digest8": h.result.digest8.hex(),
                                 "battery_mv": h.result.battery_mv, "timing": h.result.timing}}
                            for (t, e), h in self.history.items()]}

    def load_state(self, data: dict[str, Any]) -> None:
        if data.get("addr") is not None:
            self.provision(int(data["addr"]))
            if data.get("configured"):
                self.configure(True, 0)
        for a in data.get("assignments", []):
            self.assignments[a["tag_id"]] = Assignment(a["tag_id"], a["epoch"], bytes.fromhex(a["key"]), a["flags"])
        for h in data.get("history", []):
            r = h.get("result")
            result = None if r is None else StoredResult(Status(r["status"]), bytes.fromhex(r["digest8"]),
                                                         r["battery_mv"], dict(r["timing"]))
            self.history[(h["tag_id"], h["epoch"])] = History(h["revision"], bytes.fromhex(h["digest"]), result)


class _BridgeSession:
    """Central side of one connection (§5.3–§5.6)."""

    def __init__(self, bridge: SimBridge, link: GattLink, tag_id: int, suspend_ms: int) -> None:
        self.bridge = bridge
        self.link = link
        self.tag_id = tag_id
        self.suspend_ms = suspend_ms
        self.connected_ms = bridge.clock.now_ms()
        self.ctrl_tx = Fragmenter(TAG_CTRL_MSG_MAX)
        self.data_tx = Fragmenter(TAG_RECORD_WIRE_MAX)
        self.ctrl_rx = Reassembler(TAG_CTRL_MSG_MAX)
        self.status_rx = Reassembler(TAG_RECORD_WIRE_MAX)
        self.sender: crypto.RecordSender | None = None
        self.receiver: crypto.RecordReceiver | None = None
        self.credits = 0
        self.credit_grants = 0
        self.ctrl_inbox: deque[bytes] = deque()
        self.records_in_event = 0
        self.status = Status.OK
        self.refreshing_reported = False
        self.job: Job | None = None
        self.epoch = 0
        self.pacer = Pacer(bridge.clock)

    @property
    def clock(self) -> SimClock:
        return self.bridge.clock

    # -- transport helpers --------------------------------------------------------------

    async def send_ctrl(self, message: bytes) -> None:
        for value in self.ctrl_tx.split(message):
            await self.link.write(GattChr.CTRL, value)

    async def send_record(self, record_type: RecordType, plaintext: bytes) -> None:
        assert self.sender is not None
        record = self.sender.seal(record_type, plaintext)
        for value in self.data_tx.split(record):
            await self.link.write(GattChr.DATA, value)
        self.credits -= 1
        self.records_in_event += 1
        if self.records_in_event >= RECORDS_PER_EVENT:  # at most 4 records per connection event
            self.records_in_event = 0
            await self.pacer.sleep_ms(CONN_INTERVAL_MS)

    async def pump(self, timeout_ms: float) -> RecResult | None:
        """Handle one incoming ATT value; returns a RESULT once one is complete."""
        chr_, value = await self.link.central_recv(self.clock, timeout_ms)
        if chr_ == GattChr.CTRL:
            message = self.ctrl_rx.feed(value)
            if message is not None:
                if message[0] == CtrlMsg.ERROR and self.receiver is not None:
                    self.tag_error(Status(CtrlError.unpack(message[1:]).status))
                self.ctrl_inbox.append(message)
            return None
        if chr_ != GattChr.STATUS:
            raise FragmentError(f"unexpected value on {chr_!r}")
        message = self.status_rx.feed(value)
        if message is None:
            return None
        if message[0] == PlainMsg.CREDIT:
            self.credits += PlainCredit.unpack(message[1:]).credits
            self.credit_grants += 1
            return None
        assert self.receiver is not None
        kind, plaintext = self.receiver.open(message)
        if kind == RecordType.PROGRESS:
            if RecProgress.unpack(plaintext).stage == DeliveryStage.REFRESHING and self.job is not None:
                if not self.refreshing_reported:
                    self.refreshing_reported = True
                    self.bridge._stage(self.job, DeliveryStage.REFRESHING)
            return None
        if kind == RecordType.RESULT:
            return RecResult.unpack(plaintext)
        raise FragmentError(f"unexpected record type {kind:#04x}")

    async def recv_ctrl(self) -> bytes:
        while not self.ctrl_inbox:
            await self.pump(STEP_TIMEOUT_MS)
        return self.ctrl_inbox.popleft()

    async def wait_credit(self) -> RecResult | None:
        while self.credits <= 0:
            result = await self.pump(STEP_TIMEOUT_MS)
            if result is not None:
                return result
        return None

    async def wait_result(self, timeout_ms: float) -> RecResult:
        deadline = self.clock.now_ms() + timeout_ms
        while True:
            result = await self.pump(max(1.0, deadline - self.clock.now_ms()))
            if result is not None:
                return result

    def tag_error(self, status: Status) -> None:
        """An ERROR from the tag: security/config errors end the tag's jobs, others are retried."""
        self.status = status
        if status in FINAL_TAG_ERRORS:
            for job in list(self.bridge.jobs.get(self.tag_id, [])):
                if job.epoch == self.epoch:
                    self.bridge._finish(job, status)
        raise _SessionEnd

    # -- session ------------------------------------------------------------------------------

    async def run(self) -> None:
        bridge = self.bridge
        assignment = bridge.assignments.get(self.tag_id)
        if assignment is None:
            raise _SessionEnd
        self.epoch = assignment.epoch
        caps = TagCaps.unpack(await self.link.read(GattChr.CAPS))
        if caps.tag_id != self.tag_id or caps.proto != PROTO_VERSION:
            self.status = Status.VERSION_MISMATCH if caps.proto != PROTO_VERSION else Status.NOT_FOUND
            raise _SessionEnd
        hello = bytes([CtrlMsg.HELLO]) + CtrlHello(PROTO_VERSION, self.tag_id, self.epoch,
                                                   bridge.rng.randbytes(16)).pack()
        await self.send_ctrl(hello)
        challenge_msg = await self.recv_ctrl()
        if challenge_msg[0] == CtrlMsg.ERROR:
            self.tag_error(Status(CtrlError.unpack(challenge_msg[1:]).status))
        if challenge_msg[0] != CtrlMsg.CHALLENGE:
            raise FragmentError("expected CHALLENGE")
        challenge = CtrlChallenge.unpack(challenge_msg[1:])
        bridge.tag_battery[self.tag_id] = challenge.battery_mv
        th = crypto.transcript_hash(hello, challenge_msg)
        mac = crypto.mac_b(assignment.key, th)
        await self.send_ctrl(bytes([CtrlMsg.AUTH]) + CtrlAuth(mac).pack())
        reply = await self.recv_ctrl()
        if reply[0] == CtrlMsg.ERROR:
            self.tag_error(Status(CtrlError.unpack(reply[1:]).status))
        if reply[0] != CtrlMsg.AUTH_OK:
            raise FragmentError("expected AUTH_OK")
        try:
            crypto.verify_mac_t(assignment.key, th, mac, CtrlAuthOk.unpack(reply[1:]).mac_t)
        except crypto.AuthError:
            self.tag_error(Status.AUTH_FAILED)
        k_b2t, k_t2b = crypto.session_keys(assignment.key, th)
        self.sender = crypto.RecordSender(k_b2t, RecordDir.B2T)
        self.receiver = crypto.RecordReceiver(k_t2b, RecordDir.T2B)
        while self.credit_grants == 0:  # CREDIT{caps.credits} follows AUTH_OK
            await self.pump(STEP_TIMEOUT_MS)
        for job in list(bridge.jobs.get(self.tag_id, [])):
            if job not in bridge.jobs.get(self.tag_id, []):
                continue  # finished meanwhile (cancelled, superseded)
            if job.epoch != self.epoch:
                bridge._finish(job, Status.NOT_ASSIGNED)
                continue
            self.job = job
            self.refreshing_reported = False
            job.in_session = True
            try:
                await self.run_job(job, caps, challenge)
            finally:
                job.in_session = False
                self.job = None

    def timing(self, job: Job, started: float, refresh_ms: int) -> dict[str, int]:
        return {"wake_ms": int(max(0.0, self.connected_ms - job.validated_ms)), "suspend_ms": self.suspend_ms,
                "transfer_ms": int(self.clock.now_ms() - started), "refresh_ms": refresh_ms}

    def finish_from(self, job: Job, result: RecResult, started: float) -> None:
        self.bridge._finish(job, Status(result.status), digest8=result.digest, battery_mv=result.battery_mv,
                            timing=self.timing(job, started, result.refresh_ms))

    async def run_job(self, job: Job, caps: TagCaps, challenge: CtrlChallenge) -> None:
        bridge = self.bridge
        started = self.clock.now_ms()
        if job.kind == "cmd":
            bridge._stage(job, DeliveryStage.TRANSFERRING)
            early = await self.wait_credit()
            if early is not None:
                self.finish_from(job, early, started)
                return
            await self.send_record(RecordType.CMD, RecCmd(job.cmd, job.update_id).pack())
            self.finish_from(job, await self.wait_result(RESULT_TIMEOUT_MS), started)
            return
        if (job.frame_end_sent and challenge.flags & 1
                and (challenge.stored_epoch, challenge.displayed_rev) == (job.epoch, job.revision)):
            bridge._finish(job, Status.DISPLAY_STATE_UNKNOWN, battery_mv=challenge.battery_mv,
                           timing=self.timing(job, started, 0))
            return
        pack = bridge.fontpack
        if pack is None or pack.pack_id != job.fontpack_id:
            bridge._finish(job, Status.FONTPACK_MISMATCH)
            return
        panel = Panel(caps.width, caps.height, caps.planes, caps.plane_flags)
        try:
            frame = await asyncio.to_thread(render_frame, job.layout, panel, pack)
        except LayoutError as exc:  # e.g. the logical size does not fit this panel (§4.4 Rotation)
            bridge._finish(job, exc.status)
            return
        bridge._stage(job, DeliveryStage.TRANSFERRING)
        started = self.clock.now_ms()  # rendering runs at host speed; time the transfer only
        plane_len = len(frame.planes[0])
        early = await self.wait_credit()
        if early is not None:
            self.finish_from(job, early, started)
            return
        grants = self.credit_grants
        await self.send_record(RecordType.FRAME_BEGIN, RecFrameBegin(job.revision, job.update_id, frame.digest,
                                                                     len(frame.planes), plane_len).pack())
        while self.credit_grants == grants:  # FRAME_BEGIN's credit, or the tag's immediate RESULT
            result = await self.pump(STEP_TIMEOUT_MS)
            if result is not None:
                self.finish_from(job, result, started)
                return
        for plane, data in enumerate(frame.planes):
            for offset in range(0, plane_len, TAG_PLANE_DATA_MAX):
                early = await self.wait_credit()
                if early is not None:
                    self.finish_from(job, early, started)
                    return
                await self.send_record(RecordType.PLANE_DATA, RecPlaneData(
                    plane, offset, data[offset : offset + TAG_PLANE_DATA_MAX]).pack())
        early = await self.wait_credit()
        if early is not None:
            self.finish_from(job, early, started)
            return
        await self.send_record(RecordType.FRAME_END, b"")
        job.frame_end_sent = True
        self.finish_from(job, await self.wait_result(RESULT_TIMEOUT_MS), started)
