"""Simulated gateway (docs/protocol.md §1, §2, §3.2, §3.4).

Serial side: a :class:`~cremind_tag.sim.device.DeviceEndpoint` (framing,
credits, retained events) with ``boot_id``, the ``SERIAL_IDEMPOTENCY_SLOTS``
most recent ``op_id`` answers (a repeat returns the remembered status with
``detail = DUPLICATE`` and does no work; transient refusals — ``BUSY``,
``NO_RESOURCES``, ``PROVISIONING_ACTIVE`` — are not remembered, so a retry with the
same ``op_id`` can succeed), and a bounded delivery queue (``BUSY`` when full).

Mesh side: the CDB (addresses, names, configured flag; persisted in the
simulator state file), provisioning of simulated unprovisioned bridges (beacons
during ``SCAN_UNPROV``, ``PROVISION``, ``CONFIGURE_NODE``, ``REMOVE_NODE``), and
deliveries that follow §3.2: **one outstanding segmented send** gateway-wide,
each failed send retried up to 3 times (then ``TIMEOUT``), ``LAYOUT_BEGIN`` ->
150-byte ``LAYOUT_CHUNK``s -> ``LAYOUT_COMMIT``, ``INCOMPLETE`` answered by
resending exactly the missing chunks (at most 3 rounds), commit re-sent after
10 s without ``LAYOUT_STATUS`` (3 times). A rejection in ``LAYOUT_STATUS``
becomes the delivery's ``EVT_RESULT`` here; otherwise the bridge's
``DELIVERY_RESULT`` does (deduplicated by ``(bridge, result_seq)``, always
answered with ``RESULT_ACK``).

Protocol v2 (docs/connect-setup.md; the gateway runs it when it is given a
:class:`~cremind_tag.secure.device.SecureDevice`): the serial port answers only
``HELLO``, ``PING``, ``IDENTIFY``, ``SECURE_OPEN`` and ``SECURE_DATA`` in
plaintext; inside a session the §4.2 access table applies (unowned: ``INFO``,
``PING``, ``STATUS``, ``CLAIM``; owned, pinned controller: everything; owned,
another controller: ``INFO``, ``PING``, ``STATUS``, ``RECOVER``; else
``NOT_OWNER``) and events flow only to the pinned controller. ``CLAIM``,
``RECOVER`` and ``RELEASE`` run on the ``SecureDevice``; ``RELEASE`` also wipes
the CDB, the assignments and the retained events and reboots after its answer
(the bridges of the old network become orphans the gateway no longer hears).
``PROVISION`` of a bridge needs its static OOB (``SECURITY_CONFIG`` otherwise).
``DISCOVER`` asks bridges for v2 tags in setup mode (``EVT_DISCOVERED`` inside the
window asked for, at most one per bridge and tag per
``DISCOVERED_MIN_INTERVAL_MS``); ``TUNNEL_OPEN``/``SEND``/``CLOSE`` run mesh
tunnels, one per bridge and one message in flight per tunnel (``EVT_TUNNEL``
``OPEN`` with the endpoint's ``ident2``, ``DATA``, ``CLOSED``). Mesh messages from
nodes outside the CDB are ignored. Where the protocol text leaves a choice, the
simulator follows the gateway firmware (apps/gateway/src/core/gw_secure.c,
gw_tunnel.c).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from ..protocol.ids import (
    DISCOVER_MAX_S,
    DISCOVERED_MIN_INTERVAL_MS,
    LAYOUT_CHUNK_DATA_MAX,
    MAX_BRIDGES,
    MAX_TAGS,
    PROTO_VERSION,
    SERIAL_IDEMPOTENCY_SLOTS,
    SERIAL_MAX_FRAME,
    TUNNEL_MSG_MAX,
    Board,
    DeliveryStage,
    Link,
    NodeRole,
    OwnerState,
    SerialMsg,
    Status,
    TunnelState,
)
from ..protocol.layout import layout_digest
from ..protocol.msgs import (
    FixedMessage,
    MeshAssignDel,
    MeshAssignSet,
    MeshAssignStatus,
    MeshCaps2Status,
    MeshCapsGet,
    MeshCapsStatus,
    MeshDeliveryResult,
    MeshDeliveryStage,
    MeshDiscover,
    MeshDiscovered,
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
    MeshTunnelClose,
    MeshTunnelData,
    MeshTunnelOpen,
    MeshTunnelUp,
)
from ..secure.device import SecureDevice
from ..secure.messages import FRAG_CLOSE, Reassembler, SecureFrameError, fragments
from .bridge import SimBridge
from .core import SimClock, TaskSet
from .device import DeviceEndpoint, Reply
from .mesh import GATEWAY_ADDR, UNSEGMENTED_MAX, MeshNetwork, pack_pdu
from .v2 import SECURE_MESSAGES, LinkSessions, SecureSerial, device_state, load_device_state, outcome_fields

log = logging.getLogger(__name__)

FW = "0.1.0"
FW_V2 = "0.2.0"
SEND_RETRIES = 3  # §3.2 rule 1
INCOMPLETE_ROUNDS = 3  # §3.2 rule 3
COMMIT_RESENDS = 3
REPORTED_UPDATE_IDS = 1024  # update_ids remembered for the one-result-per-update_id rule (§10)
STATUS_TIMEOUT_MS = 10000.0
ASSIGN_TIMEOUT_MS = 5000.0
PROVISION_MS = 3000.0
CONFIGURE_MS = 2000.0
REMOVE_MS = 1000.0
TRANSIENT = frozenset({Status.BUSY, Status.NO_RESOURCES, Status.PROVISIONING_ACTIVE})
SIDE_EFFECTING = frozenset({SerialMsg.PROVISION, SerialMsg.CONFIGURE_NODE, SerialMsg.REMOVE_NODE, SerialMsg.ASSIGN_TAG,
                            SerialMsg.UNASSIGN_TAG, SerialMsg.DELIVER_LAYOUT, SerialMsg.CANCEL_DELIVERY,
                            SerialMsg.TAG_COMMAND, SerialMsg.REBOOT, SerialMsg.IDENTIFY_NODE, SerialMsg.DISCOVER,
                            SerialMsg.TUNNEL_OPEN})
V2_ONLY = frozenset({SerialMsg.IDENTIFY, SerialMsg.SECURE_OPEN, SerialMsg.SECURE_DATA, SerialMsg.DISCOVER,
                     SerialMsg.TUNNEL_OPEN, SerialMsg.TUNNEL_SEND, SerialMsg.TUNNEL_CLOSE}) | SECURE_MESSAGES
"""Requests a v1 gateway answers ``UNSUPPORTED`` (it predates them)."""
REMEMBERED = ("status", "text", "tunnel")  # fields an idempotency slot keeps (TUNNEL_OPEN's tunnel id)
UNOWNED_ALLOWED = frozenset({SerialMsg.INFO, SerialMsg.PING, SerialMsg.STATUS, SerialMsg.CLAIM})
OTHER_CONTROLLER_ALLOWED = frozenset({SerialMsg.INFO, SerialMsg.PING, SerialMsg.STATUS, SerialMsg.RECOVER})
MAX_TUNNELS = MAX_BRIDGES  # a bridge holds one tunnel at a time
TUNNEL_GRACE_MS = 5000.0  # after the bridge's own idle timeout: the gateway forgets a silent tunnel
DISCOVER_GRACE_MS = 2000.0  # DISCOVERED already on its way when a bridge's discovery window ends
BRIDGE_TAG_MESSAGES = frozenset({SerialMsg.PAIR, SerialMsg.REKEY, SerialMsg.MAINT_AUTH, SerialMsg.RECOMMISSION,
                                 SerialMsg.FACTORY_SETUP})
"""v2 messages of bridges and tags: a gateway answers them UNSUPPORTED, before its access table."""


@dataclass
class CdbNode:
    uuid: bytes
    addr: int
    elements: int = 1
    name: str = ""
    configured: bool = False
    last_seen_ms: float = 0.0


@dataclass(eq=False)
class Delivery:
    op_id: int
    bridge: int
    tag_id: int
    epoch: int
    revision: int
    update_id: int
    fontpack_id: bytes
    layout: bytes
    state: str = "queued"  # queued | transferring | at_bridge | done
    mesh_ms: int = 0


@dataclass(eq=False)
class GwTunnel:
    """The gateway's end of one mesh tunnel (connect-setup.md 5.2, 6)."""

    tunnel: int
    bridge: int
    tag_id: int
    timeout_s: int
    last_ms: float
    opened: bool = False  # the endpoint's ident2 arrived (EVT_TUNNEL OPEN)
    rx: Reassembler = field(default_factory=Reassembler)
    tx: asyncio.Task[None] | None = None  # the message being sent (TUNNEL_SEND answers BUSY meanwhile)
    watch: asyncio.Task[None] | None = None  # the idle timer

    @property
    def tx_busy(self) -> bool:
        return self.tx is not None and not self.tx.done()


class IdempotencySlots:
    """The last N ``op_id`` -> answer pairs (§1.4)."""

    def __init__(self, size: int = SERIAL_IDEMPOTENCY_SLOTS) -> None:
        self.size = size
        self._slots: OrderedDict[int, dict[str, Any]] = OrderedDict()

    def get(self, op_id: int) -> dict[str, Any] | None:
        return self._slots.get(op_id)

    def put(self, op_id: int, answer: dict[str, Any]) -> None:
        self._slots[op_id] = answer
        self._slots.move_to_end(op_id)
        while len(self._slots) > self.size:
            self._slots.popitem(last=False)

    def clear(self) -> None:
        self._slots.clear()


class SimGateway:
    """The simulated gateway (see the module docstring)."""

    def __init__(self, *, clock: SimClock, mesh: MeshNetwork, rng: random.Random, bridges: list[SimBridge],
                 delivery_queue: int = 4, mesh_ops: int = 8, rx_buffers: int = 4,
                 processing_delay_s: float = 0.0, secure: SecureDevice | None = None) -> None:
        self.clock = clock
        self.mesh = mesh
        self.rng = rng
        self.bridges = bridges
        self.delivery_queue = delivery_queue
        self.mesh_ops = mesh_ops
        self.counters: Counter[str] = Counter()
        self.endpoint = DeviceEndpoint("gateway", self._handle, rx_buffers=rx_buffers,
                                       processing_delay_s=processing_delay_s, supported=(
                                           m for m in SerialMsg if m < SerialMsg.FONT_BEGIN))
        # Protocol v2 (docs/connect-setup.md): the identity, the ownership record and the secure serial layer.
        self.secure = secure
        self.fw = FW_V2 if secure is not None else FW
        self.sessions: LinkSessions | None = None
        if secure is not None:
            self.sessions = LinkSessions(secure)
            self.endpoint.secure = SecureSerial(self.sessions, fw=self.fw, plaintext=lambda _msg: False,
                                                handler=self._handle_secure, events=self._operations_allowed)
        self._tunnels: dict[int, GwTunnel] = {}
        self._next_tunnel = 0
        self._discovered: dict[tuple[int, int], float] = {}  # (bridge, tag) -> last EVT_DISCOVERED
        self._discover_until: dict[int, float] = {}  # bridge -> end of the discovery window it was asked for
        self.boot_id = rng.getrandbits(32)
        self._boot_ms = clock.now_ms()
        self.cdb: dict[int, CdbNode] = {}
        self.assigned: dict[int, dict[int, int]] = {}  # bridge -> tag -> epoch (from ASSIGN_STATUS OK)
        self.bridge_info: dict[int, dict[str, Any]] = {}
        self._slots = IdempotencySlots()
        self._queue: deque[Delivery] = deque()
        self._queue_changed = asyncio.Event()
        self._deliveries_enabled = asyncio.Event()
        self._deliveries_enabled.set()
        self._by_update: dict[int, Delivery] = {}
        self._seg_lock = asyncio.Lock()
        self._xfer_id = rng.getrandbits(16)
        self._status_waiters: dict[tuple[int, int], asyncio.Future[tuple[Status, int]]] = {}
        self._assign_waiters: dict[tuple[int, int, int], asyncio.Future[Status]] = {}
        self._seen_results: deque[tuple[int, int]] = deque(maxlen=256)
        self._reported: OrderedDict[int, None] = OrderedDict()  # update_ids whose EVT_RESULT was emitted
        self._ops_in_flight = 0
        self._provisioning = False
        self._tasks = TaskSet("gateway")
        self._worker: asyncio.Task[None] | None = None
        self.on_state_change: list[Any] = []  # callables(): the simulator saves its state file

    # -- lifecycle ----------------------------------------------------------------------

    @property
    def url(self) -> str:
        return self.endpoint.url

    @property
    def v2(self) -> bool:
        return self.secure is not None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        self.mesh.attach(GATEWAY_ADDR, self)
        self._worker = self._tasks.spawn(self._delivery_worker(), "delivery worker")
        self._query_known_bridges()
        return await self.endpoint.start(host, port)

    def _query_known_bridges(self) -> None:
        """At boot the gateway asks every configured node for CAPS/HEALTH (fills GET_INVENTORY)."""
        for node in self.cdb.values():
            if node.configured:
                self._tasks.spawn(self._refresh_info(node.addr), "boot inventory")

    async def stop(self) -> None:
        await self.endpoint.stop()
        await self._tasks.cancel_all()
        self.mesh.detach(GATEWAY_ADDR)

    def pause_deliveries(self) -> None:
        """Hold queued deliveries in the gateway (tests: fill the queue -> BUSY)."""
        self._deliveries_enabled.clear()

    def resume_deliveries(self) -> None:
        self._deliveries_enabled.set()

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def uptime_s(self) -> int:
        return int((self.clock.now_ms() - self._boot_ms) / 1000)

    async def reboot(self) -> None:
        """New boot_id; RAM state (queue, idempotency slots, retained events) is lost; the CDB survives."""
        self.counters["reboots"] += 1
        self.boot_id = self.rng.getrandbits(32)
        self._boot_ms = self.clock.now_ms()
        self.endpoint.new_boot()
        self.endpoint.drop_connection()
        await self._tasks.cancel_all()
        self._slots.clear()
        self._seen_results.clear()  # results de-duplication is RAM (docs/gateway-firmware.md §6)
        self._reported.clear()
        self._queue.clear()
        self._by_update.clear()
        self._status_waiters.clear()
        self._assign_waiters.clear()
        self._ops_in_flight = 0
        self._provisioning = False
        self._seg_lock = asyncio.Lock()
        if self.sessions is not None:  # v2: sessions, the challenge, tunnels and discovery bookkeeping are RAM
            self.sessions.clear()
            self._tunnels.clear()
            self._discovered.clear()
            self._discover_until.clear()
        self._worker = self._tasks.spawn(self._delivery_worker(), "delivery worker")
        self._query_known_bridges()

    def _changed(self) -> None:
        for callback in self.on_state_change:
            callback()

    # -- helpers ----------------------------------------------------------------------------

    def _caps(self) -> dict[str, Any]:
        return {"max_frame": SERIAL_MAX_FRAME, "credits": self.endpoint.rx_buffers, "max_bridges": MAX_BRIDGES,
                "max_tags": MAX_TAGS, "role": NodeRole.GATEWAY, "board": Board.NRF52840DK_GATEWAY}

    def all_counters(self) -> dict[str, int]:
        merged = self.counters + self.endpoint.counters
        merged["queue_depth"] = len(self._queue)
        merged["nodes"] = len(self.cdb)
        return {k: min(int(v), 0xFFFFFFFF) for k, v in merged.items() if v >= 0}

    def _emit(self, msg: SerialMsg, fields: dict[str, Any], retained: bool) -> int | None:
        return self.endpoint.emit(msg, fields, retained=retained)

    def _bridge_by_uuid(self, uuid: bytes) -> SimBridge | None:
        return next((b for b in self.bridges if b.uuid == uuid), None)

    def _bridge_at(self, addr: int) -> SimBridge | None:
        return next((b for b in self.bridges if b.addr == addr), None)

    def _node_ok(self, addr: int) -> dict[str, Any] | None:
        node = self.cdb.get(addr)
        if node is None:
            return {"status": Status.NOT_FOUND, "text": f"no node at {addr:#06x}"}
        if not node.configured:
            return {"status": Status.NOT_FOUND, "text": f"node {addr:#06x} is not configured"}
        return None

    async def _send(self, dst: int, msg: FixedMessage) -> bool:
        """Send with §3.2 rule 1: one outstanding segmented send, a failed ``end`` retried 3 times."""
        if len(pack_pdu(msg)) <= UNSEGMENTED_MAX:
            return await self.mesh.send(GATEWAY_ADDR, dst, msg)
        async with self._seg_lock:
            for attempt in range(1 + SEND_RETRIES):
                if attempt:
                    self.counters["mesh_send_retries"] += 1
                if await self.mesh.send(GATEWAY_ADDR, dst, msg):
                    return True
            self.counters["mesh_send_failures"] += 1
            return False

    # -- serial requests ---------------------------------------------------------------------

    async def _handle(self, msg: SerialMsg, f: dict[str, Any]) -> Reply | dict[str, Any]:
        if msg in V2_ONLY and not self.v2:
            return {"status": Status.UNSUPPORTED}
        if msg in SIDE_EFFECTING:
            op_id = f["op_id"]
            remembered = self._slots.get(op_id)
            if remembered is not None:
                self.counters["duplicate_ops"] += 1
                return {**remembered, "detail": Status.DUPLICATE}
            reply = await self._side_effect(msg, f)
            fields = reply.fields if isinstance(reply, Reply) else reply
            if fields["status"] not in TRANSIENT:
                self._slots.put(op_id, {k: v for k, v in fields.items() if k in REMEMBERED})
            return reply
        match msg:
            case SerialMsg.HELLO:
                if f.get("proto") != PROTO_VERSION:
                    return {"status": Status.VERSION_MISMATCH, "proto": PROTO_VERSION}
                return {"status": Status.OK, "proto": PROTO_VERSION, "fw": self.fw, "build": "sim",
                        "boot_id": self.boot_id, "caps": self._caps()}
            case SerialMsg.PING:
                return {"status": Status.OK, "uptime_s": self.uptime_s()}
            case SerialMsg.INFO:
                return {"status": Status.OK, "fw": self.fw, "build": "sim", "boot_id": self.boot_id,
                        "caps": self._caps(), "counters": self.all_counters()}
            case SerialMsg.TUNNEL_SEND:
                return self._tunnel_send(f["tunnel"], f["data"])
            case SerialMsg.TUNNEL_CLOSE:
                return self._tunnel_close(f["tunnel"])
            case SerialMsg.GET_COUNTERS:
                return {"status": Status.OK, "counters": self.all_counters()}
            case SerialMsg.SCAN_UNPROV:
                self._tasks.spawn(self._scan(f["duration_s"], f.get("uuid_filter", b"")), "scan")
                return {"status": Status.OK}
            case SerialMsg.LIST_NODES:
                now = self.clock.now_ms()
                return {"status": Status.OK, "nodes": [
                    {"addr": n.addr, "uuid": n.uuid, "elements": n.elements, "configured": n.configured,
                     "name": n.name, "last_seen_s": int(max(0.0, now - n.last_seen_ms) / 1000)}
                    for n in sorted(self.cdb.values(), key=lambda n: n.addr)]}
            case SerialMsg.GET_INVENTORY:
                for node in self.cdb.values():
                    if node.configured:
                        self._tasks.spawn(self._refresh_info(node.addr), "inventory")
                return {"status": Status.OK, "items": [self._inventory_item(n) for n in
                                                       sorted(self.cdb.values(), key=lambda n: n.addr)]}
        return {"status": Status.UNSUPPORTED}

    async def _side_effect(self, msg: SerialMsg, f: dict[str, Any]) -> Reply | dict[str, Any]:
        match msg:
            case SerialMsg.REBOOT:
                return Reply({"status": Status.OK}, after=self.reboot)
            case SerialMsg.PROVISION:
                return self._provision(f)
            case SerialMsg.CONFIGURE_NODE:
                if f["addr"] not in self.cdb:
                    return {"status": Status.NOT_FOUND}
                if self._provisioning:
                    return {"status": Status.PROVISIONING_ACTIVE}
                self._tasks.spawn(self._configure(f["op_id"], f["addr"], f["relay"], f["ttl"]), "configure")
                return {"status": Status.ACCEPTED}
            case SerialMsg.REMOVE_NODE:
                if f["addr"] not in self.cdb:
                    return {"status": Status.NOT_FOUND}
                self._tasks.spawn(self._remove(f["op_id"], f["addr"]), "remove")
                return {"status": Status.ACCEPTED}
            case SerialMsg.IDENTIFY_NODE:
                if (error := self._node_ok(f["addr"])) is not None:
                    return error
                self._tasks.spawn(self._send(f["addr"], MeshIdentify(5)), "identify")
                return {"status": Status.OK}
        if msg in (SerialMsg.ASSIGN_TAG, SerialMsg.UNASSIGN_TAG, SerialMsg.TAG_COMMAND):
            if (error := self._node_ok(f["bridge"])) is not None:
                return error
            if self._ops_in_flight >= self.mesh_ops:
                self.counters["busy"] += 1
                return {"status": Status.BUSY}
            self._ops_in_flight += 1
            self._tasks.spawn(self._mesh_op(msg, f), msg.name.lower())
            return {"status": Status.ACCEPTED}
        if msg == SerialMsg.DELIVER_LAYOUT:
            return self._deliver(f)
        if msg == SerialMsg.CANCEL_DELIVERY:
            return self._cancel(f["update_id"])
        if msg == SerialMsg.DISCOVER:
            return self._discover(f)
        if msg == SerialMsg.TUNNEL_OPEN:
            return self._tunnel_open(f)
        return {"status": Status.UNSUPPORTED}

    # -- protocol v2: the secure session (connect-setup.md 4.2, 5) ----------------------------

    def _operations_allowed(self) -> bool:
        """The session's controller is the pinned one of an owned gateway (events flow to it)."""
        assert self.sessions is not None
        with self.sessions.use(Link.SERIAL) as dev:
            return dev.operations_allowed()

    async def _handle_secure(self, msg: SerialMsg, f: dict[str, Any]) -> Reply | dict[str, Any]:
        """A request inside the secure session: the §4.2 access table, then the v2 or v1 handler."""
        assert self.sessions is not None
        if msg in BRIDGE_TAG_MESSAGES:
            self.counters["unsupported"] += 1
            return {"status": Status.UNSUPPORTED}
        changed = False
        with self.sessions.use(Link.SERIAL) as dev:
            if dev.record.state != OwnerState.OWNED:
                allowed: frozenset[SerialMsg] | None = UNOWNED_ALLOWED
            elif dev.controller_match():
                allowed = None  # the pinned controller: everything
            else:
                allowed = OTHER_CONTROLLER_ALLOWED
            if allowed is not None and msg not in allowed:
                self.counters["not_owner"] += 1
                return {"status": Status.NOT_OWNER}
            if msg not in SECURE_MESSAGES:
                outcome = None
            else:
                before = dev.record
                outcome = dev.handle(msg, f)
                changed = dev.record is not before
        if outcome is None:
            return await self._handle(msg, f)
        if changed:
            self._changed()  # persist, then acknowledge
        fields = outcome_fields(outcome)
        if outcome.released:
            # Nothing of the previous owner stays, and the next boot starts a new network (as the firmware:
            # it reboots once the answer is out, gw_secure.c).
            self._wipe_network()
            return Reply(fields, after=self.reboot)
        return fields

    def _wipe_network(self) -> None:
        """A gateway ``RELEASE``: the CDB (and with it the mesh), the assignments and the retained events go.
        The bridges of the old network keep their mesh state: orphans the gateway no longer hears."""
        self.counters["releases"] += 1
        self.cdb.clear()
        self.assigned.clear()
        self.bridge_info.clear()
        self.endpoint.clear_retained()
        self._changed()

    # -- protocol v2: discovery and tunnels (connect-setup.md 5.2, 6) ---------------------------

    def _discover(self, f: dict[str, Any]) -> dict[str, Any]:
        """``DISCOVER``: the mesh ``DISCOVER`` to the bridge (0: every configured one) and a window per bridge in
        which its ``DISCOVERED`` become ``EVT_DISCOVERED`` (``duration_s`` 0 closes it)."""
        duration, bridge, tag_id = f["duration_s"], f["bridge"], f["tag_id"]
        if duration > DISCOVER_MAX_S:
            return {"status": Status.INVALID, "text": f"duration_s > {DISCOVER_MAX_S}"}
        if bridge:
            if (error := self._node_ok(bridge)) is not None:
                return error
            targets = [bridge]
        else:
            targets = sorted(addr for addr, node in self.cdb.items() if node.configured)
        until = self.clock.now_ms() + duration * 1000.0 + DISCOVER_GRACE_MS if duration else 0.0
        for addr in targets:
            self._discover_until[addr] = until
            self._tasks.spawn(self._send(addr, MeshDiscover(duration, tag_id)), "discover")
        self.counters["discoveries"] += 1
        return {"status": Status.ACCEPTED}

    def _on_discovered(self, src: int, msg: MeshDiscovered) -> None:
        now, key = self.clock.now_ms(), (src, msg.tag_id)
        if now > self._discover_until.get(src, 0.0):
            self.counters["discovered_unasked"] += 1  # no window open: candidates are asked for, never kept
            return
        if now - self._discovered.get(key, -DISCOVERED_MIN_INTERVAL_MS) < DISCOVERED_MIN_INTERVAL_MS:
            self.counters["discovered_suppressed"] += 1
            return
        self._discovered[key] = now
        self._emit(SerialMsg.EVT_DISCOVERED, {"bridge": src, "tag_id": msg.tag_id, "rssi": msg.rssi,
                                              "flags": msg.flags}, retained=False)

    def _alloc_tunnel(self) -> int:
        for _ in range(0xFFFF):
            self._next_tunnel = self._next_tunnel % 0xFFFF + 1
            if self._next_tunnel not in self._tunnels:
                return self._next_tunnel
        raise RuntimeError("no free tunnel id")

    def _tunnel_open(self, f: dict[str, Any]) -> dict[str, Any]:
        if (error := self._node_ok(f["bridge"])) is not None:
            return error
        duration = f["duration_s"]
        if not 1 <= duration <= 0xFF:
            return {"status": Status.INVALID, "text": "duration_s is 1..255"}
        if any(t.bridge == f["bridge"] for t in self._tunnels.values()) or len(self._tunnels) >= MAX_TUNNELS:
            self.counters["busy"] += 1  # one tunnel per bridge (a bridge holds one at a time)
            return {"status": Status.BUSY, "text": "a tunnel to this bridge is open"}
        tunnel = GwTunnel(self._alloc_tunnel(), f["bridge"], f["tag_id"], duration, self.clock.now_ms())
        self._tunnels[tunnel.tunnel] = tunnel
        self._tasks.spawn(self._send(tunnel.bridge, MeshTunnelOpen(tunnel.tunnel, tunnel.tag_id, duration)),
                          "tunnel open")
        tunnel.watch = self._tasks.spawn(self._tunnel_watch(tunnel), f"tunnel {tunnel.tunnel} idle")
        self.counters["tunnels_opened"] += 1
        return {"status": Status.OK, "tunnel": tunnel.tunnel}

    def _tunnel_send(self, tunnel_id: int, data: bytes) -> dict[str, Any]:
        tunnel = self._tunnels.get(tunnel_id)
        if tunnel is None:
            return {"status": Status.NOT_FOUND}
        if not data:
            return {"status": Status.INVALID, "text": "an empty tunnel message"}
        if len(data) > TUNNEL_MSG_MAX:
            return {"status": Status.TOO_LARGE, "text": f"a tunnel message is at most {TUNNEL_MSG_MAX} bytes"}
        if tunnel.tx_busy:
            self.counters["busy"] += 1
            return {"status": Status.BUSY, "text": "one tunnel message at a time"}
        tunnel.last_ms = self.clock.now_ms()
        tunnel.tx = self._tasks.spawn(self._tunnel_tx(tunnel, bytes(data)), f"tunnel {tunnel.tunnel} data")
        return {"status": Status.OK}

    def _tunnel_close(self, tunnel_id: int) -> dict[str, Any]:
        tunnel = self._tunnels.get(tunnel_id)
        if tunnel is None:
            return {"status": Status.NOT_FOUND}
        self._tunnel_end(tunnel, Status.OK, event=False)  # a message still in flight is dropped
        self.counters["tunnels_closed_by_host"] += 1
        return {"status": Status.OK}

    def _tunnel_end(self, tunnel: GwTunnel, status: Status, *, event: bool = True, tell_bridge: bool = True) -> None:
        """Forget the tunnel; tell the bridge (mesh ``TUNNEL_CLOSE``) and the host (``EVT_TUNNEL CLOSED``)."""
        if self._tunnels.get(tunnel.tunnel) is not tunnel:
            return
        del self._tunnels[tunnel.tunnel]
        current = asyncio.current_task()
        for task in (tunnel.tx, tunnel.watch):
            if task is not None and task is not current:
                task.cancel()
        if tell_bridge:
            self._tasks.spawn(self._send(tunnel.bridge, MeshTunnelClose(tunnel.tunnel, status)), "tunnel close")
        if event:
            self.counters[f"tunnels_closed_{status.name.lower()}"] += 1
            self._emit(SerialMsg.EVT_TUNNEL, {"tunnel": tunnel.tunnel, "bridge": tunnel.bridge,
                                              "tag_id": tunnel.tag_id, "state": TunnelState.CLOSED,
                                              "status": status}, retained=False)

    async def _tunnel_watch(self, tunnel: GwTunnel) -> None:
        """A tunnel without traffic for its timeout plus a grace time closes with ``TIMEOUT``."""
        limit_ms = tunnel.timeout_s * 1000.0 + TUNNEL_GRACE_MS
        while self._tunnels.get(tunnel.tunnel) is tunnel:
            remaining = tunnel.last_ms + limit_ms - self.clock.now_ms()
            if remaining <= 0:
                self._tunnel_end(tunnel, Status.TIMEOUT)
                return
            await self.clock.sleep_ms(remaining)

    async def _tunnel_tx(self, tunnel: GwTunnel, message: bytes) -> None:
        """One message as ``TUNNEL_DATA`` fragments (acknowledged segmented sends under the one-outstanding
        rule); a fragment that cannot be sent loses the message and ends the tunnel."""
        for seq, flags, part in fragments(message):
            if not await self._send(tunnel.bridge, MeshTunnelData(tunnel.tunnel, seq, flags, part)):
                self._tunnel_end(tunnel, Status.TIMEOUT)
                return
            tunnel.last_ms = self.clock.now_ms()
        self.counters["tunnel_messages_down"] += 1

    def _on_tunnel_up(self, src: int, msg: MeshTunnelUp) -> None:
        tunnel = self._tunnels.get(msg.tunnel)
        if tunnel is None or tunnel.bridge != src:
            self.counters["stray_tunnel_up"] += 1
            return
        tunnel.last_ms = self.clock.now_ms()
        if msg.flags & FRAG_CLOSE:  # the bridge (or the tag behind it) closed it: data = status u8
            status = msg.data[0] if msg.data else Status.INTERNAL
            try:
                reason = Status(status)
            except ValueError:
                reason = Status.INVALID
            self._tunnel_end(tunnel, reason, tell_bridge=False)
            return
        try:
            message = tunnel.rx.feed(msg.seq, msg.flags, msg.data)
        except SecureFrameError:
            self.counters["tunnel_gaps"] += 1  # a gap ends the message; the Noise session above it fails
            return
        if message is None:
            return
        state = TunnelState.DATA if tunnel.opened else TunnelState.OPEN
        tunnel.opened = True
        self.counters["tunnel_messages_up"] += 1
        self._emit(SerialMsg.EVT_TUNNEL, {"tunnel": tunnel.tunnel, "bridge": src, "tag_id": tunnel.tag_id,
                                          "state": state, "data": message}, retained=False)

    # -- provisioning --------------------------------------------------------------------------

    async def _scan(self, duration_s: int, uuid_filter: bytes) -> None:
        end = self.clock.now_ms() + duration_s * 1000.0
        seen: set[bytes] = set()
        await self.clock.sleep_ms(self.rng.uniform(100, 600))
        while self.clock.now_ms() < end:
            for bridge in self.bridges:
                if bridge.beaconing() and bridge.uuid.startswith(uuid_filter) and bridge.uuid not in seen:
                    seen.add(bridge.uuid)
                    self._emit(SerialMsg.EVT_UNPROV_BEACON, {"uuid": bridge.uuid, "rssi": self.rng.randint(-80, -40),
                                                             "oob": bridge.beacon_oob()}, retained=False)
            await self.clock.sleep_ms(1000.0)

    def _provision(self, f: dict[str, Any]) -> dict[str, Any]:
        if self._provisioning:
            return {"status": Status.PROVISIONING_ACTIVE}
        existing = next((n for n in self.cdb.values() if n.uuid == f["uuid"]), None)
        if existing is None and len(self.cdb) >= MAX_BRIDGES:
            return {"status": Status.NO_RESOURCES, "text": f"MAX_BRIDGES ({MAX_BRIDGES}) reached"}
        self._provisioning = True
        self._tasks.spawn(self._provision_run(f["op_id"], f["uuid"], f.get("name", ""), f.get("static_oob")),
                          "provision")
        return {"status": Status.ACCEPTED}

    async def _provision_run(self, op_id: int, uuid: bytes, name: str, static_oob: bytes | None = None) -> None:
        try:
            await self.clock.sleep_ms(PROVISION_MS)
            existing = next((n for n in self.cdb.values() if n.uuid == uuid), None)
            bridge = self._bridge_by_uuid(uuid)
            if existing is not None:
                fields = {"uuid": uuid, "addr": existing.addr, "elements": existing.elements, "status": Status.OK}
            elif bridge is None or not bridge.beaconing():
                fields = {"uuid": uuid, "addr": 0, "elements": 0, "status": Status.NOT_FOUND}
            elif (self.v2 or bridge.v2) and not bridge.static_oob_matches(static_oob if self.v2 else None):
                # connect-setup.md 3.4, 6: static OOB only. A missing or wrong value, or a bridge that offers no
                # static OOB, fails the authentication step; the bridge stays unprovisioned.
                self.counters["provision_oob_failed"] += 1
                fields = {"uuid": uuid, "addr": 0, "elements": 0, "status": Status.SECURITY_CONFIG}
            else:
                # (an address still held by a node of a released network is skipped too)
                addr = next(a for a in range(GATEWAY_ADDR + 1, 0x8000)
                            if a not in self.cdb and a not in self.mesh.nodes)
                bridge.provision(addr)
                if name:
                    bridge.name = name
                self.cdb[addr] = CdbNode(uuid, addr, 1, name or bridge.name, False, self.clock.now_ms())
                self._changed()
                fields = {"uuid": uuid, "addr": addr, "elements": 1, "status": Status.OK}
            self._emit(SerialMsg.EVT_PROVISIONED, {"op_id": op_id, **fields}, retained=True)
        finally:
            self._provisioning = False

    async def _configure(self, op_id: int, addr: int, relay: bool, ttl: int) -> None:
        await self.clock.sleep_ms(CONFIGURE_MS)
        node, bridge = self.cdb.get(addr), self._bridge_at(addr)
        status = Status.OK
        if node is None or bridge is None:
            status = Status.TIMEOUT  # the node does not answer the configuration client
        else:
            bridge.configure(relay, ttl)
            node.configured = True
            node.last_seen_ms = self.clock.now_ms()
            self._changed()
        self._emit(SerialMsg.EVT_NODE_CONFIGURED, {"op_id": op_id, "addr": addr, "status": status}, retained=True)
        if status == Status.OK:
            await self._refresh_info(addr)

    async def _remove(self, op_id: int, addr: int) -> None:
        await self.clock.sleep_ms(REMOVE_MS)
        bridge = self._bridge_at(addr)
        if bridge is not None:
            bridge.reset_node()
        self.cdb.pop(addr, None)
        self.assigned.pop(addr, None)
        self.bridge_info.pop(addr, None)
        self._changed()
        self._emit(SerialMsg.EVT_NODE_REMOVED, {"op_id": op_id, "addr": addr, "status": Status.OK}, retained=True)

    # -- inventory ---------------------------------------------------------------------------------

    async def _refresh_info(self, addr: int) -> None:
        await self._send(addr, MeshCapsGet())
        await self._send(addr, MeshHealthGet())

    def _inventory_item(self, node: CdbNode) -> dict[str, Any]:
        info = self.bridge_info.get(node.addr, {})
        item: dict[str, Any] = {"addr": node.addr, "uuid": node.uuid, "name": node.name, "configured": node.configured,
                                "last_seen_s": int(max(0.0, self.clock.now_ms() - node.last_seen_ms) / 1000),
                                "assigned": [{"tag_id": t, "epoch": e} for t, e in
                                             sorted(self.assigned.get(node.addr, {}).items())]}
        for key in ("fw", "fontpack_id", "caps", "counters", "board", "flash_size"):
            if key in info:
                item[key] = info[key]
        if "caps" in item and "v2" in info:
            item["caps"] = {**item["caps"], **info["v2"]}  # CAPS2_STATUS: device_id, gen, owner_state
        return item

    def _bridge_info_event(self, addr: int) -> None:
        info = self.bridge_info.get(addr, {})
        if "caps" not in info:
            return
        self._emit(SerialMsg.EVT_BRIDGE_INFO, {
            "addr": addr, "fw": info["fw"], "fontpack_id": info["fontpack_id"],
            "caps": {**info["caps"], **info.get("v2", {})},
            "assigned": [{"tag_id": t, "epoch": e} for t, e in sorted(self.assigned.get(addr, {}).items())],
            "counters": info.get("counters", {})}, retained=False)

    # -- mesh receive ---------------------------------------------------------------------------

    @property
    def mesh_suspended(self) -> bool:
        return False

    async def wait_mesh_resumed(self) -> None:
        return None

    def mesh_accepts(self, msg: FixedMessage) -> bool:
        return True

    def mesh_receive(self, src: int, msg: FixedMessage) -> None:
        node = self.cdb.get(src)
        if node is not None:
            node.last_seen_ms = self.clock.now_ms()
        elif self.v2:
            self.counters["foreign_mesh"] += 1  # a node of another (released) network: its keys are not ours
            return
        if self.v2 and isinstance(msg, MeshTunnelUp):
            self._on_tunnel_up(src, msg)
        elif self.v2 and isinstance(msg, MeshDiscovered):
            self._on_discovered(src, msg)
        elif self.v2 and isinstance(msg, MeshCaps2Status):
            info = self.bridge_info.setdefault(src, {})
            info["v2"] = {"device_id": msg.device_id, "gen": msg.gen, "owner_state": msg.owner_state}
            self._bridge_info_event(src)
        elif isinstance(msg, MeshLayoutStatus):
            waiter = self._status_waiters.get((src, msg.xfer_id))
            if waiter is not None and not waiter.done():
                waiter.set_result((Status(msg.status), msg.missing))
        elif isinstance(msg, MeshDeliveryStage):
            self._emit(SerialMsg.EVT_STAGE, {"update_id": msg.update_id, "tag_id": msg.tag_id,
                                             "revision": msg.revision, "stage": msg.stage}, retained=False)
        elif isinstance(msg, MeshDeliveryResult):
            self._on_result(src, msg)
        elif isinstance(msg, MeshAssignStatus):
            assign_waiter = self._assign_waiters.get((src, msg.tag_id, msg.epoch))
            if assign_waiter is not None and not assign_waiter.done():
                assign_waiter.set_result(Status(msg.status))
        elif isinstance(msg, MeshCapsStatus):
            info = self.bridge_info.setdefault(src, {})
            info.update(fw=f"{msg.fw_major}.{msg.fw_minor}.{msg.fw_patch}", fontpack_id=msg.fontpack_id,
                        board=msg.board, flash_size=msg.flash_mib * (1 << 20),
                        caps={"board": msg.board, "flash_size": msg.flash_mib * (1 << 20), "max_tags": msg.max_tags,
                              "assigned_count": msg.assigned, "flags": msg.flags, "proto": msg.proto})
            self._bridge_info_event(src)
        elif isinstance(msg, MeshHealthStatus):
            info = self.bridge_info.setdefault(src, {})
            info["counters"] = {"uptime_s": msg.uptime_s, "sessions_ok": msg.sessions_ok,
                                "sessions_fail": msg.sessions_fail, "suspend_count": msg.suspend_count,
                                "suspend_max_ms": msg.suspend_max_ms, "resume_fail": msg.resume_fail,
                                "queue_depth": msg.queue_depth, "last_status": msg.last_status}
            self._bridge_info_event(src)
        elif isinstance(msg, MeshTagSeen):
            self._emit(SerialMsg.EVT_TAG_SEEN, {"bridge": src, "tag_id": msg.tag_id, "rssi": msg.rssi,
                                                "battery_mv": msg.battery_mv, "flags": msg.flags}, retained=False)
        else:
            self.counters["unexpected_mesh"] += 1

    def _on_result(self, src: int, msg: MeshDeliveryResult) -> None:
        self._tasks.spawn(self.mesh.send(GATEWAY_ADDR, src, MeshResultAck(msg.result_seq)), "result ack")
        key = (src, msg.result_seq)
        if key in self._seen_results:
            self.counters["duplicate_results"] += 1
            return
        self._seen_results.append(key)
        delivery = self._by_update.pop(msg.update_id, None)
        if delivery is not None:
            delivery.state = "done"
        self._emit_result({
            "update_id": msg.update_id, "bridge": src, "tag_id": msg.tag_id, "epoch": msg.epoch,
            "revision": msg.revision, "status": msg.status, "digest": msg.digest, "battery_mv": msg.battery_mv,
            "timing": {"wake_ms": msg.wake_ms, "mesh_ms": delivery.mesh_ms if delivery else 0,
                       "transfer_ms": msg.transfer_ms, "refresh_ms": msg.refresh_ms, "suspend_ms": msg.suspend_ms},
            "flags": msg.flags, "stored_epoch": msg.stored_epoch})  # the bridge's report, as is (§1.5)

    def _emit_result(self, fields: dict[str, Any]) -> None:
        """§10: exactly one ``EVT_RESULT`` per ``update_id``; a later result for it is dropped (the bridge
        already got its ``RESULT_ACK``)."""
        update_id = fields["update_id"]
        if update_id in self._reported:
            self.counters["duplicate_update_results"] += 1
            return
        self._reported[update_id] = None
        while len(self._reported) > REPORTED_UPDATE_IDS:
            self._reported.popitem(last=False)
        self._emit(SerialMsg.EVT_RESULT, fields, retained=True)
        self.counters["results"] += 1

    # -- assignments and tag commands ---------------------------------------------------------------

    async def _mesh_op(self, msg: SerialMsg, f: dict[str, Any]) -> None:
        try:
            if msg == SerialMsg.TAG_COMMAND:
                sent = await self._send(f["bridge"], MeshTagCmd(f["op_id"], f["tag_id"], f["epoch"], f["cmd"]))
                if not sent:
                    self._emit_result(self._result_fields(f["op_id"], f["bridge"], f["tag_id"], f["epoch"], 0,
                                                          Status.TIMEOUT))
                return
            bridge, tag_id, epoch = f["bridge"], f["tag_id"], f["epoch"]
            request = (MeshAssignSet(tag_id, epoch, f["key"], 1) if msg == SerialMsg.ASSIGN_TAG
                       else MeshAssignDel(tag_id, epoch))
            key = (bridge, tag_id, epoch)
            waiter: asyncio.Future[Status] = asyncio.get_running_loop().create_future()
            self._assign_waiters[key] = waiter
            status = Status.TIMEOUT
            try:
                for _ in range(1 + SEND_RETRIES):
                    if not await self._send(bridge, request):
                        break
                    try:
                        status = await self.clock.wait_for(asyncio.shield(waiter), ASSIGN_TIMEOUT_MS)
                        break
                    except TimeoutError:
                        continue
            finally:
                self._assign_waiters.pop(key, None)
            if status == Status.OK:
                table = self.assigned.setdefault(bridge, {})
                if msg == SerialMsg.ASSIGN_TAG:
                    table[tag_id] = epoch
                elif table.get(tag_id, epoch) <= epoch:
                    table.pop(tag_id, None)
            self._emit(SerialMsg.EVT_ASSIGN_RESULT, {"op_id": f["op_id"], "bridge": bridge, "tag_id": tag_id,
                                                     "epoch": epoch, "status": status}, retained=True)
        finally:
            self._ops_in_flight -= 1

    # -- deliveries (§3.2) ----------------------------------------------------------------------------

    def _deliver(self, f: dict[str, Any]) -> dict[str, Any]:
        if (error := self._node_ok(f["bridge"])) is not None:
            return error
        if not f["layout"]:
            return {"status": Status.INVALID, "text": "empty layout"}
        if len(self._queue) >= self.delivery_queue:
            self.counters["busy"] += 1
            return {"status": Status.BUSY}
        delivery = Delivery(f["op_id"], f["bridge"], f["tag_id"], f["epoch"], f["revision"], f["update_id"],
                            f["fontpack_id"], f["layout"])
        self._queue.append(delivery)
        self._by_update[delivery.update_id] = delivery
        self._queue_changed.set()
        self.counters["deliveries_accepted"] += 1
        return {"status": Status.ACCEPTED}

    def _cancel(self, update_id: int) -> dict[str, Any]:
        delivery = self._by_update.get(update_id)
        if delivery is None:
            return {"status": Status.NOT_FOUND}
        if delivery.state == "queued":
            self._queue.remove(delivery)
            self._gateway_result(delivery, Status.CANCELLED)
            return {"status": Status.OK}
        self._tasks.spawn(self._send(delivery.bridge, MeshLayoutCancel(update_id)), "cancel")
        return {"status": Status.ACCEPTED}

    def _result_fields(self, update_id: int, bridge: int, tag_id: int, epoch: int, revision: int, status: Status,
                       mesh_ms: int = 0) -> dict[str, Any]:
        # The gateway's own results: no tag report (flags 0, stored_epoch 0, §1.5).
        return {"update_id": update_id, "bridge": bridge, "tag_id": tag_id, "epoch": epoch, "revision": revision,
                "status": status, "digest": bytes(8), "battery_mv": 0,
                "timing": {"wake_ms": 0, "mesh_ms": mesh_ms, "transfer_ms": 0, "refresh_ms": 0, "suspend_ms": 0},
                "flags": 0, "stored_epoch": 0}

    def _gateway_result(self, d: Delivery, status: Status) -> None:
        d.state = "done"
        self._by_update.pop(d.update_id, None)
        self._emit_result(self._result_fields(d.update_id, d.bridge, d.tag_id, d.epoch, d.revision, status,
                                              d.mesh_ms))

    async def _delivery_worker(self) -> None:
        while True:
            await self._deliveries_enabled.wait()
            if not self._queue:
                self._queue_changed.clear()
                await self._queue_changed.wait()
                continue
            delivery = self._queue.popleft()
            try:
                await self._transfer(delivery)
            except Exception:
                log.exception("gateway: delivery %d failed", delivery.update_id)
                self._gateway_result(delivery, Status.INTERNAL)

    def _next_xfer_id(self) -> int:
        self._xfer_id = (self._xfer_id + 1) & 0xFFFF
        return self._xfer_id

    async def _transfer(self, d: Delivery) -> None:
        d.state = "transferring"
        started = self.clock.now_ms()
        xfer_id = self._next_xfer_id()
        chunks = [d.layout[i : i + LAYOUT_CHUNK_DATA_MAX] for i in range(0, len(d.layout), LAYOUT_CHUNK_DATA_MAX)]
        begin = MeshLayoutBegin(xfer_id, d.tag_id, d.epoch, d.revision, d.update_id, d.fontpack_id, len(d.layout),
                                len(chunks), layout_digest(d.layout))
        status: Status = Status.TIMEOUT
        if await self._send(d.bridge, begin) and await self._send_chunks(d.bridge, xfer_id, chunks, range(len(chunks))):
            for round_ in range(1 + INCOMPLETE_ROUNDS):
                status, missing = await self._commit(d.bridge, xfer_id)
                if status != Status.INCOMPLETE or round_ == INCOMPLETE_ROUNDS:
                    break
                resend = [i for i in range(len(chunks)) if missing >> i & 1]
                self.counters["chunks_resent"] += len(resend)
                if not await self._send_chunks(d.bridge, xfer_id, chunks, resend):
                    status = Status.TIMEOUT
                    break
        d.mesh_ms = int(self.clock.now_ms() - started)
        if status in (Status.OK, Status.DUPLICATE):
            # §10: DUPLICATE is treated like OK — a repeated COMMIT whose first OK was lost, or a revision
            # the bridge already has (it re-sends the stored result or delivers the pending one).
            d.state = "at_bridge"
            self._emit(SerialMsg.EVT_STAGE, {"update_id": d.update_id, "tag_id": d.tag_id, "revision": d.revision,
                                             "stage": DeliveryStage.BRIDGE_RECEIVED}, retained=False)
        else:
            self._gateway_result(d, status)

    async def _send_chunks(self, bridge: int, xfer_id: int, chunks: list[bytes], indices: Any) -> bool:
        for index in indices:
            if not await self._send(bridge, MeshLayoutChunk(xfer_id, index, chunks[index])):
                return False
        return True

    async def _commit(self, bridge: int, xfer_id: int) -> tuple[Status, int]:
        key = (bridge, xfer_id)
        waiter: asyncio.Future[tuple[Status, int]] = asyncio.get_running_loop().create_future()
        self._status_waiters[key] = waiter
        try:
            for _ in range(1 + COMMIT_RESENDS):
                if not await self._send(bridge, MeshLayoutCommit(xfer_id)):
                    return Status.TIMEOUT, 0
                try:
                    return await self.clock.wait_for(asyncio.shield(waiter), STATUS_TIMEOUT_MS)
                except TimeoutError:
                    self.counters["commit_resends"] += 1
            return Status.TIMEOUT, 0
        finally:
            self._status_waiters.pop(key, None)

    # -- persistence ------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cdb": [{"uuid": n.uuid.hex(), "addr": n.addr, "elements": n.elements, "name": n.name,
                     "configured": n.configured} for n in self.cdb.values()],
            "assigned": [{"bridge": b, "tag_id": t, "epoch": e} for b, tags in self.assigned.items()
                         for t, e in tags.items()]}
        if self.secure is not None:
            out["v2"] = device_state(self.secure)  # settings: identity key and ctag/gw/own
        return out

    def load_state(self, data: dict[str, Any]) -> None:
        for n in data.get("cdb", []):
            self.cdb[n["addr"]] = CdbNode(bytes.fromhex(n["uuid"]), n["addr"], n["elements"], n["name"],
                                          n["configured"],
                                          self.clock.now_ms())
        for a in data.get("assigned", []):
            self.assigned.setdefault(a["bridge"], {})[a["tag_id"]] = a["epoch"]
        if self.secure is not None:
            load_device_state(self.secure, data.get("v2"), adopt_identity=True)  # its flash wins over the seed
