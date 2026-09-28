"""The simulator: one gateway, its mesh, bridges and tags (docs/simulator.md).

Programmatic use (tests, the daemon's integration tests)::

    config = SimConfig(seed=7, time_scale=200, fontpack=pack_bytes,
                       bridges=[BridgeSpec()], tags=[TagSpec.generate(7, 0)])
    async with Simulator(config) as sim:
        client = GatewayClient(sim.gateway_url)          # socket://127.0.0.1:<port>
        ...
        sim.tag(tag_id).faults.power_loss = 1             # arm a fault at any time

:class:`SimulatorThread` runs the same thing on its own event loop in a
background thread, for callers whose loop must stay free of simulator work.

Protocol v2 (docs/simulator.md "Protocol v2"): ``SimConfig.protocol = 2`` makes
the gateway a v2 gateway and every bridge whose ``BridgeSpec.protocol`` is left
``None`` a v2 bridge; a tag is v2 when its ``TagSpec`` is
(``TagSpec.generate(seed, i, protocol=2)``). v2 devices start as they leave the
factory: unowned, identity keys and label secrets drawn from the seed
(:meth:`Simulator.setup_codes` gives their labels); the state file keeps their
identity and ownership record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..protocol.ids import Board, NodeRole
from ..protocol.session import derive_k_epoch
from ..secure import identity
from ..secure.device import SecureDevice
from .bridge import Assignment, BridgeFaults, SimBridge
from .core import SimClock, acquire_timer_resolution, release_timer_resolution, rng_stream
from .flash import MIB
from .gateway import CdbNode, SimGateway
from .mesh import MeshFaults, MeshNetwork, MeshTiming
from .radio import Air, AirFaults
from .tag import SimTag, TagFaults, TagSpec
from .v2 import generate_keys, new_secure_device, setup_payload

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class BridgeSpec:
    name: str = ""
    provisioned: bool = True  # False: an unprovisioned device that beacons during SCAN_UNPROV
    configured: bool = True
    fontpack: bytes | None = None  # None: the simulator-wide pack (if any)
    install_pack: bool = True
    flash_size: int = 64 * MIB
    bad_sectors: tuple[int, ...] = ()
    maintenance_port: bool = True
    max_tags: int | None = None  # assignment table size (CAPS max_tags); None: MAX_TAGS_PER_BRIDGE
    sessions: int | None = None  # tag sessions at once (§5.2); None: the board's (CONFIG_CTAG_BRIDGE_SESSIONS)
    quick_retry: bool | None = None  # one retry within the tag's window after CONNECT_FAILED; None: the default
    protocol: int | None = None  # 1 or 2; None: SimConfig.protocol
    labelled: bool = True  # v2: left the factory with a setup secret (False: FACTORY_SETUP stores one)


@dataclass
class Assign:
    """A pre-seeded assignment: the simulator derives ``K_epoch`` from the tag's secret (v1 tags only: a v2 tag
    is paired by a worker before anything can be assigned to it)."""

    tag_id: int
    bridge: int  # index into ``SimConfig.bridges``
    epoch: int = 1


@dataclass
class SimFaults:
    mesh: MeshFaults = field(default_factory=MeshFaults)
    bridge: BridgeFaults = field(default_factory=BridgeFaults)  # applied to every bridge
    air: AirFaults = field(default_factory=AirFaults)
    tags: dict[int, TagFaults] = field(default_factory=dict)


@dataclass
class SimConfig:
    seed: int = 1
    time_scale: float = 50.0
    host: str = "127.0.0.1"
    gateway_port: int = 0
    maintenance_port_base: int = 0  # 0 = ephemeral ports
    bridges: list[BridgeSpec] = field(default_factory=lambda: [BridgeSpec()])
    tags: list[TagSpec] = field(default_factory=list)
    assignments: list[Assign] = field(default_factory=list)
    fontpack: bytes | None = None
    faults: SimFaults = field(default_factory=SimFaults)
    delivery_queue: int = 4
    rx_buffers: int = 4
    processing_delay_s: float = 0.0
    mesh_timing: MeshTiming = field(default_factory=MeshTiming)
    state_file: Path | None = None
    protocol: int = 1  # 2: a v2 gateway and (by default) v2 bridges (docs/connect-setup.md)


class FaultSpecError(ValueError):
    """A ``--fault`` specification could not be parsed."""


def parse_fault(spec: str, faults: SimFaults) -> None:
    """Apply one ``--fault`` spec (see docs/simulator.md for the list)."""
    name, _, arg = spec.partition("=")
    name = name.strip().lower().replace("_", "-")

    def prob() -> float:
        try:
            value = float(arg)
        except ValueError:
            raise FaultSpecError(f"{spec}: expected a probability") from None
        if not 0.0 <= value <= 1.0:
            raise FaultSpecError(f"{spec}: probability outside 0..1")
        return value

    def tag_and_count(default: int = 1) -> tuple[int, int]:
        tag, _, count = arg.partition(":")
        try:
            return int(tag, 16), int(count) if count else default
        except ValueError:
            raise FaultSpecError(f"{spec}: expected TAGID[:COUNT]") from None

    match name:
        case "chunk-loss":
            faults.mesh.chunk_loss = prob()
        case "drop-chunks":
            faults.mesh.drop_chunks |= {int(i) for i in arg.split(",") if i.strip()}
        case "result-loss":
            faults.mesh.result_loss = prob()
        case "status-loss":
            try:
                faults.mesh.drop_status = int(arg or 1)
            except ValueError:
                raise FaultSpecError(f"{spec}: expected a count") from None
        case "send-fail":
            faults.mesh.send_fail = prob()
        case "suspend-fail":
            faults.bridge.suspend_fail = prob()
        case "resume-fail":
            faults.bridge.resume_fail_next = int(arg or 1)
        case "connect-fail":
            faults.air.connect_fail = prob()
        case "power-loss":
            tag, count = tag_and_count()
            faults.tags.setdefault(tag, TagFaults()).power_loss = count
        case "auth-fail":
            tag, count = tag_and_count()
            faults.tags.setdefault(tag, TagFaults()).auth_fail = count
        case "refresh-timeout":
            tag, count = tag_and_count()
            faults.tags.setdefault(tag, TagFaults()).refresh_timeout = count
        case "disconnect":
            tag_text, _, records = arg.partition("@")
            try:
                faults.tags.setdefault(int(tag_text, 16), TagFaults()).disconnect_after_records = int(records or 10)
            except ValueError:
                raise FaultSpecError(f"{spec}: expected TAGID@RECORDS") from None
        case _:
            raise FaultSpecError(f"unknown fault {name!r}")


class Simulator:
    """Gateway + mesh + bridges + tags on the current event loop."""

    def __init__(self, config: SimConfig) -> None:
        self.config = config
        seed = config.seed
        self.clock = SimClock(config.time_scale)
        self.mesh = MeshNetwork(self.clock, rng_stream(seed, "mesh"), config.faults.mesh, config.mesh_timing)
        self.air = Air(self.clock, rng_stream(seed, "air"), config.faults.air)
        self.bridges: list[SimBridge] = []
        for index, spec in enumerate(config.bridges):
            rng = rng_stream(seed, "bridge", index)
            policy: dict[str, Any] = {} if spec.quick_retry is None else {"quick_retry": spec.quick_retry}
            uuid = rng.randbytes(16)
            secure: SecureDevice | None = None
            if (spec.protocol or config.protocol) >= 2:
                keys = generate_keys(seed, NodeRole.BRIDGE, index, board=Board.NRF52840_BRIDGE,
                                     labelled=spec.labelled)
                secure = new_secure_device(keys, seed, ("bridge", index))
            bridge = SimBridge(spec.name or f"bridge-{index + 1}", uuid=uuid, clock=self.clock,
                               mesh=self.mesh, air=self.air, rng=rng, flash_size=spec.flash_size,
                               board=Board.NRF52840_BRIDGE, faults=config.faults.bridge, bad_sectors=spec.bad_sectors,
                               max_tags=spec.max_tags, sessions=spec.sessions, secure=secure, **policy)
            self.bridges.append(bridge)
        gateway_secure: SecureDevice | None = None
        if config.protocol >= 2:
            keys = generate_keys(seed, NodeRole.GATEWAY, "gateway", board=Board.NRF52840DK_GATEWAY)
            gateway_secure = new_secure_device(keys, seed, "gateway")
        self.gateway = SimGateway(clock=self.clock, mesh=self.mesh, rng=rng_stream(seed, "gateway"),
                                  bridges=self.bridges, delivery_queue=config.delivery_queue,
                                  rx_buffers=config.rx_buffers, processing_delay_s=config.processing_delay_s,
                                  secure=gateway_secure)
        self.tags: dict[int, SimTag] = {}
        for tag_spec in config.tags:
            rng = rng_stream(seed, "tag", tag_spec.tag_id)
            tag_secure = None
            if tag_spec.protocol >= 2:
                assert tag_spec.keys is not None
                tag_secure = new_secure_device(tag_spec.keys, seed, ("tag", tag_spec.tag_id))
            self.tags[tag_spec.tag_id] = SimTag(tag_spec, self.clock, self.air, rng,
                                                faults=config.faults.tags.get(tag_spec.tag_id), secure=tag_secure)
        self._started = False

    # -- accessors ----------------------------------------------------------------------

    @property
    def gateway_url(self) -> str:
        return self.gateway.url

    def bridge_url(self, index: int) -> str:
        return self.bridges[index].maint.url

    def tag(self, tag_id: int) -> SimTag:
        return self.tags[tag_id]

    def bridge(self, index: int) -> SimBridge:
        return self.bridges[index]

    def bridge_at(self, addr: int) -> SimBridge:
        return next(b for b in self.bridges if b.addr == addr)

    # -- setup ------------------------------------------------------------------------------

    def _setup_network(self) -> None:
        """Provision/configure bridges and seed assignments the way the operator would have."""
        for index, (spec, bridge) in enumerate(zip(self.config.bridges, self.bridges, strict=True)):
            pack = spec.fontpack if spec.fontpack is not None else self.config.fontpack
            if pack is not None and spec.install_pack and bridge.fonts.active() is None:
                bridge.fonts.install(pack)
            if spec.provisioned and not bridge.provisioned:
                addr = next(a for a in range(2, 0x8000) if a not in self.gateway.cdb)
                bridge.provision(addr)
                self.gateway.cdb[addr] = CdbNode(bridge.uuid, addr, 1, bridge.name, spec.configured,
                                                 self.clock.now_ms())
                if spec.configured:
                    bridge.configure(True, 5)
            log.debug("sim: bridge %d %s at %s", index, bridge.uuid.hex(), bridge.addr)
        for assign in self.config.assignments:
            tag = self.tags[assign.tag_id]
            bridge = self.bridges[assign.bridge]
            if bridge.addr is None:
                raise ValueError(f"assignment to unprovisioned bridge {assign.bridge}")
            if tag.secure is not None:
                raise ValueError(f"tag {tag.tag_id:08X} is a v2 tag: a worker pairs it before it can be assigned")
            key = derive_k_epoch(tag.spec.secret, tag.tag_id, assign.epoch)
            bridge.assignments[tag.tag_id] = Assignment(tag.tag_id, assign.epoch, key, 1)
            self.gateway.assigned.setdefault(bridge.addr, {})[tag.tag_id] = assign.epoch

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        acquire_timer_resolution()
        if self.config.state_file is not None and self.config.state_file.exists():
            self.load_state(self.config.state_file)
        self._setup_network()
        self.gateway.on_state_change.append(self.save_state)
        for device in (*self.bridges, *self.tags.values()):
            device.on_state_change.append(self.save_state)  # v2 ownership records, FACTORY_SETUP secrets
        if self.v2_devices():
            self.save_state()  # the labels (setup codes) are in the state file from the start
        await self.gateway.start(self.config.host, self.config.gateway_port)
        for index, (spec, bridge) in enumerate(zip(self.config.bridges, self.bridges, strict=True)):
            bridge.start()
            if spec.maintenance_port:
                port = self.config.maintenance_port_base + index if self.config.maintenance_port_base else 0
                await bridge.maint.start(self.config.host, port)
        for tag in self.tags.values():
            tag.start()
        log.info("sim: gateway on %s, %d bridge(s), %d tag(s), time scale %g", self.gateway_url,
                 len(self.bridges), len(self.tags), self.config.time_scale)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for tag in self.tags.values():
            await tag.stop()
        for bridge in self.bridges:
            await bridge.stop()
        await self.gateway.stop()
        self.save_state()
        release_timer_resolution()

    async def __aenter__(self) -> Simulator:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- state file ---------------------------------------------------------------------------

    # -- protocol v2 --------------------------------------------------------------------------

    def v2_devices(self) -> bool:
        return self.gateway.v2 or any(b.v2 for b in self.bridges) or any(t.secure for t in self.tags.values())

    def setup_codes(self) -> list[dict[str, Any]]:
        """The labels of the v2 bridges and tags: what a person scans or types to pair them.

        Each entry: ``role``, ``name``, ``device_id`` (hex), ``short_id``, and the
        setup ``code`` and ``qr`` text of the secret the device pairs with *now*
        (the label's, or the fresh one a release or recommission armed); both are
        ``None`` for a bridge that still waits for ``FACTORY_SETUP``. Setup codes
        are pairing credentials: the simulator prints them because they stand for
        the printed labels.
        """
        out: list[dict[str, Any]] = []
        devices: list[tuple[str, str, SecureDevice]] = [
            ("bridge", b.name, b.secure) for b in self.bridges if b.secure is not None]
        devices += [("tag", f"{t.tag_id:08X}", t.secure) for t in self.tags.values() if t.secure is not None]
        for role, name, device in devices:
            payload = setup_payload(device)
            out.append({"role": role, "name": name, "device_id": identity.device_id_text(device.device_id),
                        "short_id": f"{device.keys.short_id:08X}", "owner_state": int(device.record.state),
                        "code": payload.code() if payload else None, "qr": payload.qr_text() if payload else None})
        return out

    def gateway_identity(self) -> dict[str, Any] | None:
        secure = self.gateway.secure
        if secure is None:
            return None
        return {"device_id": identity.device_id_text(secure.device_id), "owner_state": int(secure.record.state),
                "gen": secure.record.gen}

    # -- state file ---------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        out = {"version": STATE_VERSION, "seed": self.config.seed, "gateway": self.gateway.state(),
               "bridges": [b.state() for b in self.bridges],
               "tags": {f"{t.tag_id:08X}": t.state() for t in self.tags.values()}}
        if self.v2_devices():
            out["setup_codes"] = self.setup_codes()  # informational: the labels; never read back
        return out

    def save_state(self, path: Path | None = None) -> None:
        path = path or self.config.state_file
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.state(), indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def load_state(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"{path}: unsupported simulator state version")
        self.gateway.load_state(data.get("gateway", {}))
        by_uuid = {b.uuid.hex(): b for b in self.bridges}
        for entry in data.get("bridges", []):
            bridge = by_uuid.get(entry.get("uuid", ""))
            if bridge is not None:
                bridge.load_state(entry)
        for key, nvs in data.get("tags", {}).items():
            tag = self.tags.get(int(key, 16))
            if tag is not None:
                tag.load_state(nvs)


class SimulatorThread:
    """A :class:`Simulator` on its own event loop in a daemon thread."""

    def __init__(self, config: SimConfig) -> None:
        self.config = config
        self.sim: Simulator | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop: asyncio.Event | None = None

    @property
    def gateway_url(self) -> str:
        assert self.sim is not None
        return self.sim.gateway_url

    def start(self, timeout: float = 10.0) -> SimulatorThread:
        self._thread = threading.Thread(target=self._main, name="cremind-tag simulator", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("simulator did not start")
        if self._error is not None:
            raise self._error
        return self

    def _main(self) -> None:
        async def run() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            try:
                self.sim = Simulator(self.config)
                await self.sim.start()
            except BaseException as exc:
                self._error = exc
                self._ready.set()
                return
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                await self.sim.stop()

        asyncio.run(run())

    def call[T](self, fn: Callable[[Simulator], T]) -> T:
        """Run ``fn(sim)`` on the simulator's loop and return its result."""
        assert self._loop is not None and self.sim is not None
        sim = self.sim

        async def wrapper() -> T:
            return fn(sim)

        return asyncio.run_coroutine_threadsafe(wrapper(), self._loop).result(10.0)

    def run[T](self, coro_fn: Callable[[Simulator], Coroutine[Any, Any, T]], timeout: float = 30.0) -> T:
        assert self._loop is not None and self.sim is not None
        return asyncio.run_coroutine_threadsafe(coro_fn(self.sim), self._loop).result(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        if self._loop is not None and self._stop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout)

    def __enter__(self) -> SimulatorThread:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
