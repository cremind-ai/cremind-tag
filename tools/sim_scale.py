#!/usr/bin/env python3
"""Simulated scale and fault test: one gateway, five bridges (one behind a relay), twenty tags.

The REAL companion daemon (``companion/src/cremind_tag/daemon``) runs against the
simulator (``companion/src/cremind_tag/sim``) and the stateful fake Cremind of the
daemon tests (``companion/tests/daemon/fake_cremind.py``, set up by their ``Rig``),
all on ONE asyncio event loop whose clock runs ``--time-scale`` times faster than
real time. Every timer in the process — the simulator's radio and mesh timings,
the daemon's poll intervals, back-offs and timeouts, the gateway client's request
timeouts, the fake Cremind's clock — therefore keeps its simulated meaning, and
every duration this tool reports is simulated time. Host work (SQLite commits,
composition, rendering, crypto, CBOR) runs at host speed and is stretched by the
same factor: a run is pessimistic by roughly ``time_scale x`` host time (reported
as ``host`` in the result).

    cd cremind-tag
    uv run --project companion python tools/sim_scale.py                     # 200 trials, baseline then faults
    uv run --project companion python tools/sim_scale.py --trials 20 --scenario baseline
    uv run --project companion python tools/sim_scale.py --json build/scale.json --markdown build/scale.md

A *trial* is one content card Cremind issues (a notification, a needs-input
question, a progress run's first card or its outcome); the traffic around the
trials — progress updates at cadence, answers (``resolved``), cancellations — is
measured too but reported separately. See docs/scale-test.md for the method,
what is and is not modelled, and the results.

Exit status 0 when every invariant holds in every scenario and the baseline meets
the target (delivery initiation within 60 s for at least 95 % of trials).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import math
import platform
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DAEMON_TESTS = REPO / "companion" / "tests" / "daemon"
DEV_PACK = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp"
FONT_CACHE = REPO / "fonts" / "cache"
PROFILE = "alice"  # the Rig's owner of every tag
STAGES = ("queued", "companion_accepted", "gateway_received", "bridge_received", "transferring", "refreshing",
          "displayed")
TERMINAL = ("displayed", "superseded", "expired", "cancelled", "failed", "uncertain")
STAGE_PAIRS = (("queued", "companion_accepted", "Cremind -> companion (events poll)"),
               ("companion_accepted", "gateway_received", "compose + DELIVER_LAYOUT accepted"),
               ("gateway_received", "bridge_received", "gateway queue + mesh transfer"),
               ("bridge_received", "transferring", "wait for the tag's wake + bridge scheduling"),
               ("transferring", "refreshing", "BLE frame transfer"),
               ("refreshing", "displayed", "panel refresh + result back to Cremind"))
TIMING_KEYS = ("wake_ms", "mesh_ms", "suspend_ms", "transfer_ms", "refresh_ms")

log = logging.getLogger("sim_scale")


class ScaleSetupError(RuntimeError):
    """The test cannot run here (no dev font pack, ...)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Competing:
    """Competing radio traffic on the mesh and the BLE advertising channels.

    Every hop of a segmented message loses each segment with ``segment_loss``; the
    lower transport retransmits a lost segment after ``retransmit_ms`` and gives the
    message up (a failed ``end`` callback) after ``retransmissions`` losses of one
    segment. An unsegmented message is lost on a hop with ``unsegmented_loss`` (the
    sender cannot tell). ``jitter_ms`` of extra latency per segment, uniformly
    drawn. ``connect_fail``: BLE connection attempts that fail although the tag
    advertises.
    """

    segment_loss: float = 0.03
    unsegmented_loss: float = 0.02
    retransmit_ms: float = 300.0
    retransmissions: int = 4
    jitter_ms: float = 6.0
    connect_fail: float = 0.05

    def scaled(self, level: float) -> Competing:
        return Competing(min(0.5, self.segment_loss * level), min(0.5, self.unsegmented_loss * level),
                         self.retransmit_ms, self.retransmissions, self.jitter_ms * level,
                         min(0.5, self.connect_fail * level))


@dataclass
class ScaleConfig:
    trials: int = 200
    scenario: str = "baseline"  # "baseline" | "faults"
    seed: int = 1
    time_scale: float = 10.0
    bridges: int = 5
    tags_per_bridge: int = 4
    relayed_bridges: int = 1
    """The last N bridges reach the gateway only through the bridge before them (one extra mesh hop)."""
    event_rate_per_min: float = 5.0
    """Traffic events per simulated minute (Poisson); an event is one card, a broadcast, a flurry, ..."""
    competing_level: float = 1.0
    """Multiplier of the :class:`Competing` defaults; 0 turns competing traffic off."""
    http_latency_ms: float = 40.0
    adv_window_ms: float | None = None
    """What-if: the tags' advertising window (``None``: the protocol's ``TAG_ADV_WINDOW_MS``, 2 s)."""
    sessions: int | None = None
    """What-if: tag sessions per bridge at once (``None``: the simulator's default, the nRF52840 bridge's)."""
    quick_retry: bool | None = None
    """What-if: one retry within the tag's window after ``CONNECT_FAILED`` (``None``: the simulator's default)."""
    chunk_loss: float = 0.05
    """Faults scenario: access-layer LAYOUT_CHUNK loss probability (the simulator's ``chunk-loss``)."""
    warmup_s: float = 60.0
    drain_s: float = 1500.0
    idle_s: float = 60.0
    target_s: float = 60.0
    target_fraction: float = 0.95
    work_dir: Path | None = None
    keep_work_dir: bool = False
    daemon_log: Path | None = None

    @property
    def tags(self) -> int:
        return self.bridges * self.tags_per_bridge


# ---------------------------------------------------------------------------
# One scaled clock for the whole process
# ---------------------------------------------------------------------------


class _ScaledSelector:
    """The loop's selector/proactor with its wait shortened by the time scale."""

    def __init__(self, inner: Any, scale: float) -> None:
        self._inner = inner
        self._scale = scale

    def select(self, timeout: float | None = None) -> Any:
        return self._inner.select(None if timeout is None else timeout / self._scale)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def scaled_loop_factory(scale: float) -> Callable[[], asyncio.AbstractEventLoop]:
    """An event loop whose ``time()`` runs ``scale`` times faster than real time.

    ``asyncio.sleep``, ``wait_for`` and every timeout are scheduled on ``loop.time()``,
    so all of them shrink by ``scale`` in real time; the loop's selector wait is divided
    by the same factor so it wakes when the scaled deadline is due. Threads and I/O run at
    host speed.
    """

    def factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        if scale != 1.0:
            real = loop.time
            origin = real()

            def scaled_time() -> float:
                return origin + (real() - origin) * scale

            loop.time = scaled_time  # type: ignore[method-assign]
            loop._selector = _ScaledSelector(loop._selector, scale)  # type: ignore[attr-defined]
            loop._clock_resolution *= scale  # type: ignore[attr-defined]
        return loop

    return factory


def run_scaled[T](coro_fn: Callable[[], Any], scale: float) -> T:
    from cremind_tag.sim.core import acquire_timer_resolution, release_timer_resolution

    acquire_timer_resolution()  # 1 ms host timer on Windows for the whole run
    try:
        with asyncio.Runner(loop_factory=scaled_loop_factory(scale)) as runner:
            return runner.run(coro_fn())
    finally:
        release_timer_resolution()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_rig() -> Any:
    """The daemon tests' Rig (simulator + fake Cremind + daemon), loaded the way their conftest does."""
    _load_module("fake_cremind", DAEMON_TESTS / "fake_cremind.py")
    return _load_module("rig", DAEMON_TESTS / "rig.py")


def load_fonts() -> tuple[Any, Any]:
    """The dev font pack as the companion's FontSet and as the bridges' FontPack (reference renderer)."""
    from cremind_tag.fontpack.format import FontPack
    from cremind_tag.fonts.fontset import FontSet

    if not DEV_PACK.is_file() or not FONT_CACHE.is_dir():
        raise ScaleSetupError(f"{DEV_PACK} or {FONT_CACHE} is missing (cremind-tag fonts fetch; "
                              "cremind-tag fonts build --profile dev)")
    try:
        return FontSet.load(DEV_PACK, FONT_CACHE), FontPack(DEV_PACK.read_bytes())
    except Exception as exc:  # noqa: BLE001 - any unusable pack means "cannot run here"
        raise ScaleSetupError(f"dev font pack not usable: {exc}") from exc


def percentile(values: Iterable[float], p: float) -> float | None:
    """Nearest-rank percentile (``None`` for no values)."""
    data = sorted(values)
    if not data:
        return None
    return data[max(0, math.ceil(p / 100.0 * len(data)) - 1)]


def summary(values: Iterable[float]) -> dict[str, Any]:
    data = sorted(v for v in values if v is not None)
    if not data:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    return {"n": len(data), "p50": percentile(data, 50), "p95": percentile(data, 95), "p99": percentile(data, 99),
            "max": data[-1], "mean": sum(data) / len(data)}


def max_in_window(times: list[float], window: float) -> int:
    """The most entries of ``times`` inside any half-open window of length ``window``."""
    data = sorted(times)
    best, start = 0, 0
    for end, t in enumerate(data):
        while t - data[start] >= window:
            start += 1
        best = max(best, end - start + 1)
    return best


class _SuspendLog(deque):  # type: ignore[type-arg]
    """A bridge's ``_suspend_times`` that also records every suspension (a pure observer)."""

    def __init__(self, sink: list[float]) -> None:
        super().__init__()
        self.sink = sink

    def append(self, value: Any) -> None:
        self.sink.append(float(value))
        super().append(value)


class _ErrorLog(logging.Handler):
    """Counts ERROR records of the companion (a task that died, a failed simulator task, ...)."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("cremind_tag"):
            self.records.append(f"{record.name}: {record.getMessage()[:300]}")


# ---------------------------------------------------------------------------
# Traffic and fault schedules (deterministic for a seed)
# ---------------------------------------------------------------------------

TITLES = ("Build finished on the main branch", "Deployment to staging completed", "Nightly backup verified",
          "Calendar: design review at three", "Weekly report is ready", "New comment on the launch plan",
          "Invoice draft saved for review", "Meeting notes were summarised", "Disk usage back to normal",
          "Package delivered to reception", "Research digest has five new papers", "Laptop battery is charged")
QUESTIONS = ("Approve the production deployment?", "Merge the release branch now?", "Send the summary to the team?",
             "Retry the failed import job?", "Accept the meeting at four?", "Archive last month's reports?")
BODIES = (None, "All checks passed and the artefacts are stored.", "Open Cremind for the details.",
          "Nothing else needs attention right now.")


@dataclass
class Act:
    t: float  # simulated seconds after the traffic started
    op: str  # "card" | "resolve" | "cancel"
    group: str  # notification, broadcast, flurry, needs_input, progress, progress_update, outcome, resolve, cancel
    tag: int = 0
    kind: str = "notification"
    title: str = ""
    body: str | None = None
    ttl_s: float = 3600.0
    replace_key: str | None = None
    resolves: str | None = None
    progress: tuple[int, int] | None = None
    ref: str | None = None
    target: str | None = None
    trial: bool = False


def make_traffic(cfg: ScaleConfig) -> list[Act]:
    """``cfg.trials`` content cards (plus the traffic around them) as a Poisson stream of events."""
    from cremind_tag.sim.core import rng_stream

    rng = rng_stream(cfg.seed, "scale-traffic")
    acts: list[Act] = []
    t = 0.0
    n = 0
    trials = 0

    def card(at: float, group: str, tag: int, **kw: Any) -> Act:
        nonlocal trials
        act = Act(at, "card", group, tag, **kw)
        acts.append(act)
        trials += act.trial
        return act

    def notification_ttl() -> float:
        return rng.uniform(180.0, 420.0) if rng.random() < 0.1 else rng.uniform(1800.0, 7200.0)

    while trials < cfg.trials:
        t += rng.expovariate(cfg.event_rate_per_min / 60.0)
        n += 1
        r = rng.random()
        tag = rng.randrange(cfg.tags)
        if r < 0.30:  # one notification; sometimes cancelled in Cremind while it is on its way
            act = card(t, "notification", tag, title=rng.choice(TITLES), body=rng.choice(BODIES),
                       ttl_s=notification_ttl(), ref=f"n{n}", trial=True)
            if rng.random() < 0.15:
                acts.append(Act(t + rng.uniform(3.0, 40.0), "cancel", "cancel", tag, target=act.ref))
        elif r < 0.40:  # the same notification to several tags at once
            for other in rng.sample(range(cfg.tags), rng.randint(3, 6)):
                card(t, "broadcast", other, title=rng.choice(TITLES), ttl_s=notification_ttl(), ref=f"b{n}.{other}",
                     trial=True)
        elif r < 0.60:  # a question, answered 1-5 minutes later
            key = f"run:{n}:input"
            card(t, "needs_input", tag, kind="needs_input", title=rng.choice(QUESTIONS), ttl_s=7 * 86400.0,
                 replace_key=key, ref=f"q{n}", trial=True)
            acts.append(Act(t + rng.uniform(60.0, 300.0), "resolve", "resolve", tag, kind="resolved",
                            title="Answered", resolves=key))
        elif r < 0.72:  # a progress run: first card, updates at a cadence, then its outcome
            key = f"run:{n}:progress"
            steps = rng.randint(2, 5)
            step_s = rng.uniform(30.0, 90.0)
            card(t, "progress", tag, kind="progress", title="Indexing documents", progress=(0, 100), ttl_s=3600.0,
                 replace_key=key, ref=f"p{n}", trial=True)
            for k in range(1, steps + 1):
                card(t + k * step_s, "progress_update", tag, kind="progress", title="Indexing documents",
                     progress=(k * 100 // (steps + 1), 100), ttl_s=3600.0, replace_key=key)
            card(t + (steps + 1) * step_s, "outcome", tag, kind="task_outcome", title="Indexing finished",
                 body="Every document is searchable.", ttl_s=rng.uniform(1800.0, 7200.0), replace_key=key,
                 ref=f"o{n}", trial=True)
        else:  # a flurry of cards to one tag within a few seconds (coalescing, the footer)
            refs = []
            for k in range(rng.randint(3, 6)):
                act = card(t + rng.uniform(0.0, 4.0), "flurry", tag, title=rng.choice(TITLES), body=rng.choice(BODIES),
                           ttl_s=notification_ttl(), ref=f"f{n}.{k}", trial=True)
                refs.append(act.ref)
            if rng.random() < 0.2:
                acts.append(Act(t + rng.uniform(5.0, 30.0), "cancel", "cancel", tag, target=rng.choice(refs)))
    acts.sort(key=lambda a: a.t)
    return acts


@dataclass
class FaultAct:
    t: float
    kind: str
    target: Any = None
    duration: float = 0.0
    armed_at: float | None = None
    fired_at: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def make_faults(cfg: ScaleConfig, duration: float) -> list[FaultAct]:
    """The faults scenario: periodic link faults, one bridge (the relay) reboot, one gateway reboot, one USB
    re-enumeration and two Cremind 5xx bursts, spread over the traffic."""
    from cremind_tag.sim.core import rng_stream

    if cfg.scenario != "faults":
        return []
    rng = rng_stream(cfg.seed, "scale-faults")
    faults: list[FaultAct] = []
    for kind, every in (("status_loss", 150.0), ("disconnect", 120.0), ("power_loss", 150.0)):
        t = rng.uniform(15.0, min(every, max(30.0, duration * 0.3)))
        while t < duration:
            faults.append(FaultAct(t, kind))
            t += rng.uniform(0.6, 1.4) * every
    relay = cfg.bridges - cfg.relayed_bridges - 1 if cfg.relayed_bridges else rng.randrange(cfg.bridges)
    faults.append(FaultAct(duration * 0.35, "bridge_reboot", target=max(0, relay), duration=3.0))
    faults.append(FaultAct(duration * 0.55, "gateway_reboot"))
    faults.append(FaultAct(duration * 0.75, "usb_replug"))
    faults.append(FaultAct(duration * 0.20, "cremind_5xx", duration=45.0))
    faults.append(FaultAct(duration * 0.65, "cremind_5xx", duration=45.0))
    faults.sort(key=lambda f: f.t)
    return faults


# ---------------------------------------------------------------------------
# Mesh overlay: the relay hop and competing traffic (the simulator itself is untouched)
# ---------------------------------------------------------------------------


class MeshOverlay:
    """Wraps ``Simulator.mesh.send``: messages to/from a relayed bridge take an extra hop through its relay
    (latency, and they wait while the relay's mesh is suspended for a BLE connection, failing after the
    lower transport's retransmission budget, like a suspended destination), and every hop carries
    :class:`Competing` traffic."""

    def __init__(self, sim: Any, relay_pairs: list[tuple[Any, Any]], competing: Competing | None, rng: Any) -> None:
        from cremind_tag.sim.mesh import GATEWAY_ADDR, pack_pdu, segments

        self._gateway = GATEWAY_ADDR
        self._pack = pack_pdu
        self._segments = segments
        self.mesh = sim.mesh
        self.clock = sim.clock
        self.relay_pairs = relay_pairs
        self.competing = competing
        self.rng = rng
        self.counters: Counter[str] = Counter()
        self._inner = self.mesh.send
        self.mesh.send = self.send

    def _relay_for(self, addr: int) -> Any:
        for far, relay in self.relay_pairs:
            if far.addr == addr:
                return relay
        return None

    async def send(self, src: int, dst: int, msg: Any) -> bool:
        timing = self.mesh.timing
        far = src if dst == self._gateway else dst
        relay = self._relay_for(far)
        segs = self._segments(len(self._pack(msg)))
        hops = 1
        if relay is not None:
            hops = 2
            self.counters["relayed"] += 1
            await self.clock.sleep_ms(timing.base_ms + segs * timing.segment_ms)  # the relay's hop
            node = self.mesh.nodes.get(relay.addr) if relay.addr is not None else None
            if node is None:
                self.counters["relay_unreachable"] += 1
                return False
            if node.mesh_suspended:
                self.counters["relay_waited_for_resume"] += 1
                try:
                    await self.clock.wait_for(node.wait_mesh_resumed(), timing.suspend_tolerance_ms)
                except TimeoutError:
                    self.counters["relay_suspend_timeouts"] += 1
                    return False
        c = self.competing
        if c is not None:
            if segs > 1:
                extra = self.rng.uniform(0.0, c.jitter_ms) * segs
                for _ in range(segs * hops):
                    losses = 0
                    while self.rng.random() < c.segment_loss:
                        losses += 1
                        self.counters["segment_retransmissions"] += 1
                        if losses > c.retransmissions:
                            self.counters["competing_send_failed"] += 1
                            await self.clock.sleep_ms(extra)
                            return False
                        extra += c.retransmit_ms
                await self.clock.sleep_ms(extra)
            elif self.rng.random() < 1.0 - (1.0 - c.unsegmented_loss) ** hops:
                self.counters[f"competing_lost_{type(msg).__name__}"] += 1
                await self.clock.sleep_ms(timing.base_ms)
                return True  # unacknowledged: the sender cannot tell
        return bool(await self._inner(src, dst, msg))


# ---------------------------------------------------------------------------
# Cremind's side of the wire: latency and 5xx bursts
# ---------------------------------------------------------------------------


class CremindWire:
    """Wraps the fake Cremind's handler: per-request latency and 5xx windows (half of the failed requests
    were applied before their answer was lost, which exercises the companion's idempotent re-sends)."""

    def __init__(self, world: World, latency_ms: float, rng: Any) -> None:
        import httpx

        self.world = world
        self.windows: list[tuple[float, float]] = []
        self.counters: Counter[str] = Counter()
        self.times: list[float] = []  # every request (simulated seconds), for peak rates
        inner = world.fake.handle

        async def handle(request: httpx.Request) -> httpx.Response:
            self.times.append(world.now())
            if latency_ms > 0:
                await asyncio.sleep(latency_ms * rng.uniform(0.5, 1.5) / 1000.0)
            now = world.now()
            if any(a <= now < b for a, b in self.windows):
                self.counters["http_503"] += 1
                self.counters[f"http_503 {request.method} {request.url.path.rsplit('/v1', 1)[-1]}"] += 1
                if rng.random() < 0.5:
                    self.counters["applied_then_503"] += 1
                    await inner(request)
                return httpx.Response(503, json={"error": "unavailable", "detail": "injected 5xx burst"})
            return await inner(request)

        world.fake.transport = httpx.MockTransport(handle)


# ---------------------------------------------------------------------------
# The world: simulator + fake Cremind + the daemon, instrumented
# ---------------------------------------------------------------------------


class World:
    def __init__(self, cfg: ScaleConfig, fonts: Any, pack: Any, work_dir: Path) -> None:
        from cremind_tag.sim.core import rng_stream

        self.cfg = cfg
        self.pack = pack
        rig_mod = load_rig()
        self.fake_mod = sys.modules["fake_cremind"]
        self.rig = rig_mod.Rig(work_dir, fonts, tags=cfg.tags, bridges=cfg.bridges, seed=cfg.seed,
                               time_scale=cfg.time_scale)
        self.sim = self.rig.sim
        self.fake = self.rig.fake
        for bridge in self.sim.bridges:  # the scheduling policy (docs/scale-test.md §6.2)
            if cfg.sessions is not None:
                bridge.max_sessions = cfg.sessions
            if cfg.quick_retry is not None:
                bridge.quick_retry = cfg.quick_retry
        self.loop = asyncio.get_running_loop()
        self.t0 = self.loop.time()
        self.wall0 = time.time()
        # One clock: the simulator's (sim ms since the start), the daemon's and the fake Cremind's (epoch
        # seconds) all read the scaled loop, so they agree with every asyncio timer.
        clock = self.sim.clock
        clock.now_ms = lambda: self.now() * 1000.0
        clock.real_s = lambda sim_ms: max(0.0, sim_ms) / 1000.0
        self.fake.clock = self.wall
        # Tag index -> bridge index (make_config assigns round-robin); the last bridges sit behind a relay.
        self.bridge_of = {i: i % cfg.bridges for i in range(cfg.tags)}
        self.relayed = set(range(cfg.bridges - cfg.relayed_bridges, cfg.bridges)) if cfg.relayed_bridges else set()
        pairs = [(self.sim.bridges[b], self.sim.bridges[b - 1]) for b in sorted(self.relayed) if b > 0]
        self.competing = Competing().scaled(cfg.competing_level) if cfg.competing_level > 0 else None
        self.overlay = MeshOverlay(self.sim, pairs, self.competing, rng_stream(cfg.seed, "scale-competing"))
        if self.competing is not None:
            self.sim.air.faults.connect_fail = self.competing.connect_fail
        if cfg.scenario == "faults":
            self.sim.mesh.faults.chunk_loss = cfg.chunk_loss
        self.wire = CremindWire(self, cfg.http_latency_ms, rng_stream(cfg.seed, "scale-http"))
        self.fault_rng = rng_stream(cfg.seed, "scale-fault-targets")
        # observations
        self.gt_stage: list[tuple[float, int, int, int, str]] = []  # (t, tag, revision, update_id, stage)
        self.gt_finish: list[tuple[float, int, int, int, str]] = []  # (t, tag, revision, update_id, status)
        self.gw_results: Counter[str] = Counter()
        self.suspends: dict[str, list[float]] = {}
        self.attempts: list[tuple[float, float, str, int, str]] = []  # (start, end, bridge, tag, outcome)
        self.windows: list[tuple[float, int, str, bool, bool]] = []  # (start, tag, bridge, backoff, rate limited)
        self.window_records: list[tuple[float, int, str]] = []  # (start, tag, outcome), filled by windows()
        self.decisions: dict[int, list[tuple[float, str]]] = defaultdict(list)  # tag -> (t, why no attempt)
        for bridge in self.sim.bridges:
            self._observe_bridge(bridge)
        for index in range(cfg.tags):
            self._observe_tag(self.sim.tag(self.rig.tag_id(index)), self.sim.bridges[self.bridge_of[index]])
        emit = self.sim.gateway._emit_result

        def emit_result(fields: dict[str, Any]) -> None:
            self.gw_results[_status_name(fields.get("status"))] += 1
            emit(fields)

        self.sim.gateway._emit_result = emit_result
        self.meta: dict[int, Act] = {}  # delivery id -> the act that created it
        self.refs: dict[str, int] = {}
        self.cancels: list[dict[str, Any]] = []
        self.ended_at: dict[int, float] = {}  # Cremind ended a delivery itself (replace_key, resolved, cancel)
        self.faults: list[FaultAct] = []
        self.passes = 0
        self.db_real_s = 0.0
        self.db_calls = 0
        self.phase_marks: dict[str, dict[str, Any]] = {}
        self.traffic_start = 0.0
        self.traffic_end = 0.0
        self.drained = False
        self.drain_detail: dict[str, Any] = {}
        self.errors = _ErrorLog()

    # -- clocks ---------------------------------------------------------------------------------

    def now(self) -> float:
        """Simulated seconds since the world was created."""
        return self.loop.time() - self.t0

    def wall(self) -> float:
        """Simulated epoch seconds (the daemon's and the fake Cremind's clock)."""
        return self.wall0 + self.now()

    def from_wall_ms(self, ms: float) -> float:
        return ms / 1000.0 - self.wall0

    async def sleep_until(self, t: float) -> None:
        delay = t - self.now()
        if delay > 0:
            await asyncio.sleep(delay)

    # -- instrumentation --------------------------------------------------------------------------

    def _observe_bridge(self, bridge: Any) -> None:
        stage, finish = bridge._stage, bridge._finish
        world = self

        def on_stage(job: Any, st: Any) -> None:
            world.gt_stage.append((world.now(), job.tag_id, job.revision, job.update_id, getattr(st, "name", str(st))))
            stage(job, st)

        def on_finish(job: Any, status: Any, **kw: Any) -> None:
            world.gt_finish.append((world.now(), job.tag_id, job.revision, job.update_id,
                                    getattr(status, "name", str(status))))
            finish(job, status, **kw)

        bridge._stage = on_stage
        bridge._finish = on_finish
        self.suspends[bridge.name] = []
        bridge._suspend_times = _SuspendLog(self.suspends[bridge.name])
        attempt = bridge._attempt
        keys = ("sessions_ok", "sessions_fail", "connect_failed", "suspend_fail", "resume_fail", "deferred")

        async def on_attempt(tag_id: int, **kwargs: Any) -> None:
            before = [bridge.counters[k] for k in keys]
            started = world.now()
            try:
                await attempt(tag_id, **kwargs)
            finally:
                changed = [k for k, b in zip(keys, before, strict=True) if bridge.counters[k] > b]
                world.attempts.append((started, world.now(), bridge.name, tag_id, changed[0] if changed else "other"))

        bridge._attempt = on_attempt
        decide = bridge._advert_decision

        def on_decision(tag_id: int, now: float) -> str | None:
            reason = decide(tag_id, now)
            if reason not in (None, "no_work", "quick_retry"):
                world.decisions[tag_id].append((world.now(), reason))
            return reason

        bridge._advert_decision = on_decision

    def _observe_tag(self, tag: Any, bridge: Any) -> None:
        """Every advertising window in which the tag had work waiting at its bridge (see :func:`windows`)."""
        window = tag._advertise_window
        world = self

        async def on_window() -> None:
            started = world.now()
            pending = bool(bridge.jobs.get(tag.tag_id))
            backoff = bridge._backoff_until.get(tag.tag_id, 0.0) > bridge.clock.now_ms()
            limited = bridge.counters["rate_limited"]
            try:
                await window()
            finally:
                if pending:
                    world.windows.append((started, tag.tag_id, bridge.name, backoff,
                                          bridge.counters["rate_limited"] > limited))

        tag._advertise_window = on_window

    def mark(self, name: str) -> None:
        """Counters at a phase boundary (rates per phase)."""
        svc = self.rig.svc
        gw = self.sim.gateway.endpoint.counters
        self.phase_marks[name] = {
            "sim_s": self.now(), "real_s": time.perf_counter(), "cpu_s": time.process_time(),
            "passes": self.passes, "requests": len(self.fake.requests),
            "frames": gw["frames_rx"] + gw["frames_tx"],
            "deliver_layout": self.sim.gateway.counters["deliveries_accepted"] + self.sim.gateway.counters["busy"],
            "db_real_s": self.db_real_s, "db_calls": self.db_calls,
            "composed": svc.scheduler.composed if svc else 0}

    # -- lifecycle ------------------------------------------------------------------------------

    async def start_daemon(self) -> None:
        from cremind_tag.daemon.settings import DaemonSettings

        # Production defaults: with the scaled loop every value keeps its real-hardware meaning.
        await self.rig.start(settings=DaemonSettings(), clock=self.wall, gateway_options={}, write_status=False)
        svc = self.rig.svc
        scheduler_pass = svc.scheduler.pass_once

        async def counted_pass() -> float | None:
            self.passes += 1
            return await scheduler_pass()

        svc.scheduler.pass_once = counted_pass
        db_run = svc.db.run

        async def timed_run(fn: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return await db_run(fn, *args, **kwargs)
            finally:
                self.db_real_s += time.perf_counter() - started
                self.db_calls += 1

        svc.db.run = timed_run

    def _check_daemon(self) -> None:
        task = self.rig.task
        if task is not None and task.done():
            exc = task.exception() if not task.cancelled() else None
            raise RuntimeError(f"the daemon stopped during the run: {exc!r}")

    async def warm_up(self) -> None:
        svc = self.rig.svc
        deadline = self.now() + self.cfg.warmup_s
        while self.now() < deadline:
            self._check_daemon()
            workers = list(svc.content_workers.values())
            if svc.gateway is not None and svc.gateway.connected and workers and all(w.caught_up for w in workers) \
                    and svc.hardware is not None and svc.hardware.inventory_done.is_set():
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("the daemon did not connect and catch up within the warm-up")

    # -- traffic --------------------------------------------------------------------------------

    def _active_ids(self) -> set[int]:
        active = self.fake_mod.ACTIVE
        return {i for i, d in self.fake.deliveries.items() if d["stage"] in active}

    def _execute(self, act: Act) -> None:
        before = self._active_ids()
        tag_hw = self.rig.hw(act.tag)
        if act.op == "card":
            did = self.fake.add_job(PROFILE, tag_hw, kind=act.kind, title=act.title, body=act.body, ttl_s=act.ttl_s,
                                    replace_key=act.replace_key,
                                    progress={"done": act.progress[0], "total": act.progress[1]}
                                    if act.progress else None)
        elif act.op == "resolve":
            did = self.fake.add_job(PROFILE, tag_hw, kind="resolved", title=act.title, resolves=act.resolves)
        else:  # cancel in Cremind: only a card still on its way can be cancelled
            target = self.refs.get(act.target or "")
            record: dict[str, Any] = {"t": self.now(), "delivery_id": target, "resolving_id": None}
            if target is None or target not in before:
                record["skipped"] = "already final in Cremind" if target is not None else "unknown"
                self.cancels.append(record)
                return
            did = self.fake.cancel(target)
            record["resolving_id"] = did
            self.cancels.append(record)
        self.meta[did] = act
        if act.ref:
            self.refs[act.ref] = did
        now = self.now()
        for gone in before - self._active_ids():
            self.ended_at.setdefault(gone, now)

    async def run_traffic(self, acts: list[Act]) -> None:
        start = self.traffic_start
        for act in acts:
            await self.sleep_until(start + act.t)
            self._check_daemon()
            self._execute(act)

    # -- faults ---------------------------------------------------------------------------------

    def _busy_tag(self) -> int:
        """A tag with work waiting at its bridge (so the fault fires soon), else any tag."""
        waiting = sorted(t for b in self.sim.bridges for t, jobs in b.jobs.items() if jobs)
        if waiting:
            return self.fault_rng.choice(waiting)
        return self.rig.tag_id(self.fault_rng.randrange(self.cfg.tags))

    async def _inject(self, f: FaultAct) -> None:
        sim = self.sim
        f.armed_at = self.now()
        if f.kind == "status_loss":
            f.detail["lost_before"] = sim.mesh.counters["lost_MeshLayoutStatus"]
            sim.mesh.faults.drop_status += 1
        elif f.kind in ("disconnect", "power_loss"):
            tag_id = self._busy_tag()
            tag = sim.tag(tag_id)
            f.target = f"{tag_id:08X}"
            if f.kind == "disconnect":
                f.detail["records"] = self.fault_rng.randint(3, 70)
                f.detail["before"] = tag.stats["fault_disconnects"]
                tag.faults.disconnect_after_records = f.detail["records"]
            else:
                f.detail["before"] = tag.stats["power_losses"]
                tag.faults.power_loss += 1
        elif f.kind == "bridge_reboot":
            bridge = sim.bridges[int(f.target)]
            f.detail["bridge"] = bridge.name
            addr = bridge.addr
            # Reset it while the gateway transfers a layout to it (or through it, as a relay): the harder case.
            behind = {far.addr for far, relay in self.overlay.relay_pairs if relay is bridge} | {addr}
            deadline = self.now() + 90.0
            hit = None
            while hit is None and self.now() < deadline:
                hit = next((d for d in list(sim.gateway._by_update.values())
                            if d.state == "transferring" and d.bridge in behind), None)
                if hit is None:
                    await asyncio.sleep(0.05)
            f.detail["during_transfer"] = hit is not None
            if hit is not None:
                f.detail["hit"] = [hit.tag_id, hit.revision]
            f.detail["bridges"] = sorted(i for i, b in enumerate(sim.bridges) if b.addr in behind)
            sim.mesh.detach(addr)
            with contextlib.suppress(ValueError):
                sim.air.scanners.remove(bridge)
            await bridge.reboot()  # RAM state gone; mesh settings, assignments and pending layouts kept
            f.fired_at = self.now()
            await asyncio.sleep(f.duration)  # powered off / booting: off the mesh, not scanning
            sim.mesh.attach(addr, bridge)
            sim.air.scanners.append(bridge)
        elif f.kind == "gateway_reboot":
            await sim.gateway.reboot()
            f.fired_at = self.now()
        elif f.kind == "usb_replug":
            sim.gateway.endpoint.drop_connection()
            f.fired_at = self.now()
        elif f.kind == "cremind_5xx":
            self.wire.windows.append((self.now(), self.now() + f.duration))
            f.fired_at = self.now()

    def _poll_fired(self) -> None:
        for f in self.faults:
            if f.armed_at is None or f.fired_at is not None:
                continue
            if f.kind == "status_loss":
                if self.sim.mesh.counters["lost_MeshLayoutStatus"] > f.detail["lost_before"]:
                    f.fired_at = self.now()
            elif f.kind in ("disconnect", "power_loss"):
                tag = self.sim.tag(int(f.target, 16))
                key = "fault_disconnects" if f.kind == "disconnect" else "power_losses"
                if tag.stats[key] > f.detail["before"]:
                    f.fired_at = self.now()

    async def run_faults(self) -> None:
        start = self.traffic_start
        for f in self.faults:
            await self.sleep_until(start + f.t)
            self._check_daemon()
            await self._inject(f)

    async def monitor(self) -> None:
        while True:
            self._poll_fired()
            await asyncio.sleep(0.25)

    # -- drain ------------------------------------------------------------------------------------

    def _settled_db(self, active: set[int]) -> dict[str, Any]:
        """(worker thread) Whether every delivery Cremind still lists waits only in a footer, nothing is being
        sent and the outbox is empty."""
        db = self.rig.svc.db
        with db.reading() as conn:
            revisions = conn.execute("SELECT COUNT(*) FROM revisions WHERE state IN ('pending', 'sent')").fetchone()[0]
            outbox = conn.execute("SELECT COUNT(*) FROM outbox WHERE dead = 0").fetchone()[0]
            footer: set[int] = set()
            for row in conn.execute("SELECT r.pending_delivery_ids FROM revisions r JOIN tag_views v"
                                    " ON v.tag_id = r.tag_id AND v.displayed_revision = r.revision"
                                    " WHERE r.state = 'displayed'"):
                footer |= set(json.loads(row[0] or "[]"))
            jobs = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT delivery_id, state, outcome FROM jobs")}
        waiting = sorted(d for d in active if not (d in footer and jobs.get(d) == ("active", None)))
        return {"pending_or_sent_revisions": revisions, "outbox": outbox, "waiting": waiting[:20],
                "waiting_count": len(waiting), "footer_count": len(active) - len(waiting)}

    async def drain(self) -> None:
        deadline = self.now() + self.cfg.drain_s
        detail: dict[str, Any] = {}
        while self.now() < deadline:
            self._check_daemon()
            active = self._active_ids()
            detail = await self.rig.svc.db.run(self._settled_db, active)
            armed = [f for f in self.faults if f.fired_at is None and f.kind in ("disconnect", "power_loss")]
            if detail["pending_or_sent_revisions"] == 0 and detail["outbox"] == 0 and detail["waiting_count"] == 0:
                self.drained = True
                break
            detail["armed_faults"] = len(armed)
            await asyncio.sleep(5.0)
        self.drain_detail = {**detail, "drain_s": self.now() - self.traffic_end}

    # -- the end ---------------------------------------------------------------------------------

    def live_snapshot(self) -> dict[str, Any]:
        """What only the running simulator can tell (before everything stops)."""
        sim = self.sim
        svc = self.rig.svc
        return {
            "panels": {f"{t.tag_id:08X}": t.displayed_digest[:8].hex() for t in sim.tags.values()},
            "gateway": sim.gateway.all_counters(),
            "bridges": {b.name: {k: int(v) for k, v in b.counters.items()} for b in sim.bridges},
            "mesh": dict(sim.mesh.counters), "air": dict(sim.air.counters),
            "tags": {f"{t.tag_id:08X}": {k: int(v) for k, v in t.stats.items()} for t in sim.tags.values()},
            "armed_left": {f"{t.tag_id:08X}": {"power_loss": t.faults.power_loss,
                                               "disconnect_after_records": t.faults.disconnect_after_records}
                           for t in sim.tags.values() if t.faults.power_loss or t.faults.disconnect_after_records},
            "overlay": dict(self.overlay.counters), "wire": dict(self.wire.counters),
            "gw_results": dict(self.gw_results),
            "daemon": {"composed": svc.scheduler.composed, "sent": svc.scheduler.sent,
                       "results": svc.handler.results, "stages": svc.handler.stages,
                       "outbox_sent": {k: s.sent for k, s in svc.senders.items()},
                       "syncs": {k: w.syncs for k, w in svc.content_workers.items()},
                       "gateway_stats": dict(svc.gateway.stats) if svc.gateway else {}},
        }

    def read_db(self) -> dict[str, Any]:
        with self.rig.db() as db, db.reading() as conn:
            jobs = {r["delivery_id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
            revisions = [dict(r) for r in conn.execute(
                "SELECT tag_id, revision, epoch, purpose, layout, layout_digest, frame_digest, delivery_ids,"
                " pending_delivery_ids, state, attempts, uncertain_count, last_status, timing, created_ts, sent_ts"
                " FROM revisions")]
            views = {r["tag_id"]: dict(r) for r in conn.execute("SELECT * FROM tag_views")}
            outbox = [dict(r) for r in conn.execute("SELECT kind, dead, attempts, last_error FROM outbox")]
        for r in revisions:
            r["delivery_ids"] = json.loads(r["delivery_ids"] or "[]")
            r["pending_delivery_ids"] = json.loads(r["pending_delivery_ids"] or "[]")
            r["timing"] = json.loads(r["timing"]) if r["timing"] else None
        return {"jobs": jobs, "revisions": revisions, "views": views, "outbox": outbox}


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


async def _run_world(cfg: ScaleConfig, fonts: Any, pack: Any, work_dir: Path) -> dict[str, Any]:
    real_start = time.perf_counter()
    world = World(cfg, fonts, pack, work_dir)
    logging.getLogger().addHandler(world.errors)
    acts = make_traffic(cfg)
    duration = acts[-1].t if acts else 0.0
    world.faults = make_faults(cfg, duration)
    live: dict[str, Any] = {}
    monitor: asyncio.Task[None] | None = None
    try:
        async with world.rig:
            await world.start_daemon()
            await world.warm_up()
            monitor = asyncio.create_task(world.monitor(), name="scale monitor")
            world.traffic_start = world.now()
            world.mark("traffic")
            await asyncio.gather(world.run_traffic(acts), world.run_faults())
            world.traffic_end = world.now()
            world.mark("drain")
            await world.drain()
            world.mark("idle")
            await asyncio.sleep(cfg.idle_s)  # nothing to do: the daemon and the simulator must stay quiet
            world.mark("end")
            world._poll_fired()
            live = world.live_snapshot()
            monitor.cancel()
    finally:
        if monitor is not None:
            monitor.cancel()
        logging.getLogger().removeHandler(world.errors)
    db = world.read_db()
    report = analyze(world, acts, live, db)
    report["real_s"] = time.perf_counter() - real_start
    return report


def run_scale(cfg: ScaleConfig, fonts: tuple[Any, Any] | None = None) -> dict[str, Any]:
    """Run one scenario and return its report (see :func:`analyze`)."""
    font_set, pack = fonts or load_fonts()
    work_dir = cfg.work_dir or Path(tempfile.mkdtemp(prefix="cremind-tag-scale-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    handler: logging.Handler | None = None
    if cfg.daemon_log is not None:
        cfg.daemon_log.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(cfg.daemon_log, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.setLevel(logging.INFO)
        logging.getLogger("cremind_tag").addHandler(handler)
        logging.getLogger("cremind_tag").setLevel(logging.INFO)
    import cremind_tag.sim.tag as sim_tag

    adv_window = sim_tag.TAG_ADV_WINDOW_MS
    if cfg.adv_window_ms is not None:
        sim_tag.TAG_ADV_WINDOW_MS = cfg.adv_window_ms  # what-if only: the simulated tags read it per window
    try:
        return run_scaled(lambda: _run_world(cfg, font_set, pack, work_dir), cfg.time_scale)
    finally:
        sim_tag.TAG_ADV_WINDOW_MS = adv_window
        if handler is not None:
            logging.getLogger("cremind_tag").removeHandler(handler)
            handler.close()
        if cfg.work_dir is None and not cfg.keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze(world: World, acts: list[Act], live: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    from cremind_tag.protocol.ids import BRIDGE_MAX_SUSPENDS_PER_MIN
    from cremind_tag.render.reference import Panel, render_frame

    cfg, fake = world.cfg, world.fake
    jobs, revisions, views = db["jobs"], db["revisions"], db["views"]
    tag_index = {world.rig.tag_id(i): i for i in range(cfg.tags)}
    rev_by_key = {(r["tag_id"], r["revision"]): r for r in revisions}
    shown_in: dict[int, list[tuple[int, int]]] = defaultdict(list)
    footer_in: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in revisions:
        for d in r["delivery_ids"]:
            shown_in[d].append((r["tag_id"], r["revision"]))
        for d in r["pending_delivery_ids"]:
            footer_in[d].append((r["tag_id"], r["revision"]))
    gt_transfer: dict[tuple[int, int], float] = {}
    for t, tag, rev, _, stage in world.gt_stage:
        if stage == "TRANSFERRING":
            gt_transfer.setdefault((tag, rev), t)
    gt_shown: dict[tuple[int, int], float] = {}
    for t, tag, rev, _, status in world.gt_finish:
        if status == "OK":
            gt_shown.setdefault((tag, rev), t)
    end_t = world.now()
    target = cfg.target_s

    # -- per delivery ----------------------------------------------------------------------------
    rows: list[dict[str, Any]] = []
    for did, d in sorted(fake.deliveries.items()):
        act = world.meta.get(did)
        created = world.from_wall_ms(d["created_at"])
        stages = {k: world.from_wall_ms(v) - created for k, v in d["stage_times"].items()}
        job = jobs.get(did)
        tag = tag_index.get(int(d["tag"], 16))
        keys_shown = sorted(shown_in.get(did, []), key=lambda k: k[1])
        keys_footer = sorted(footer_in.get(did, []), key=lambda k: k[1])
        # Footer-held: counted in a footer before any screen showing it was displayed — the screen model defers it
        # (newer or higher-priority cards took the four places), even when an earlier screen that was never
        # displayed had shown it.
        shown_displayed = [k[1] for k in keys_shown if rev_by_key.get(k, {}).get("state") == "displayed"]
        first_displayed = min(shown_displayed) if shown_displayed else None
        footer_held = bool(keys_footer) and (first_displayed is None or keys_footer[0][1] < first_displayed)
        gt_tx = [gt_transfer[k] for k in keys_shown if k in gt_transfer]
        gt_disp = [gt_shown[k] for k in keys_shown if k in gt_shown]
        composed = [rev_by_key[k]["created_ts"] for k in keys_shown + keys_footer if k in rev_by_key]
        row = {
            "id": did, "group": act.group if act else "cancel_resolve" if d["kind"] == "resolved" else "other",
            "kind": d["kind"], "trial": bool(act and act.trial), "tag": tag,
            "bridge": world.bridge_of.get(tag) if tag is not None else None,
            "relayed": world.bridge_of.get(tag) in world.relayed if tag is not None else False,
            "created": created, "final": d["stage"], "companion_outcome": job["outcome"] if job else None,
            "accepted": did in set(fake.accepted_log), "in_db": job is not None,
            "stages": stages, "footer_held": footer_held, "shown": bool(keys_shown),
            "composed_s": (min(composed) - world.wall0 - created) if composed else None,
            "gt_transfer_s": (min(gt_tx) - created) if gt_tx else None,
            "gt_displayed_s": (min(gt_disp) - created) if gt_disp else None,
            "ended_s": (world.ended_at[did] - created) if did in world.ended_at else None,
            "expires_s": world.from_wall_ms(d["expires_at"]) - created,
            "timing": d.get("timing"),
        }
        initiation = stages.get("transferring")
        row["initiation_source"] = "receipt" if initiation is not None else None
        if initiation is None and row["gt_transfer_s"] is not None:
            initiation, row["initiation_source"] = row["gt_transfer_s"], "simulator"
        row["initiation_s"] = initiation
        rows.append(row)

    # -- the acceptance population ---------------------------------------------------------------
    population, latencies, misses, excluded = [], [], [], Counter()
    for row in rows:
        if not row["trial"]:
            continue
        if row["footer_held"]:
            excluded["footer_held"] += 1
            continue
        if row["initiation_s"] is not None:
            population.append(row)
            latencies.append(row["initiation_s"])
            continue
        # never started: it left the card set first (cancelled, answered, replaced), failed, or is stuck
        left = row["ended_s"]
        if left is None and row["final"] == "expired":
            left = row["expires_s"]
        if left is not None and row["final"] in ("cancelled", "superseded", "expired") \
                and row["companion_outcome"] not in ("failed", "uncertain"):
            if left <= target:
                excluded[f"ended_before_initiation_{row['final']}"] += 1
                continue
            population.append(row)
            misses.append(left)  # censored: it waited at least this long
            row["censored_s"] = left
            continue
        population.append(row)
        waited = end_t - row["created"]
        misses.append(max(waited, target + 1e-3))
        row["censored_s"] = waited
    within = sum(1 for v in latencies if v <= target)
    counted = len(latencies) + len(misses)
    initiation_all = latencies + misses
    displays = [r["stages"]["displayed"] for r in population if "displayed" in r["stages"]]
    acceptance = {
        "target_s": target, "target_fraction": cfg.target_fraction, "trials": sum(r["trial"] for r in rows),
        "population": counted, "initiated": len(latencies), "not_initiated": len(misses),
        "excluded": dict(excluded), "within_target": within,
        "fraction_within_target": within / counted if counted else None,
        "met": bool(counted) and within / counted >= cfg.target_fraction,
        "initiation": summary(initiation_all), "initiation_initiated_only": summary(latencies),
        "initiation_simulator": summary(r["gt_transfer_s"] for r in population if r["gt_transfer_s"] is not None),
        "initiation_source": dict(Counter(r["initiation_source"] for r in population if r["initiation_s"] is not None)),
        "display": summary(displays),
    }

    # -- the tail: why each trial missed the target ---------------------------------------------
    window_summary = windows(world)
    tail: Counter[str] = Counter()
    tail_rows = []
    for row in population:
        latency = row["initiation_s"] if row["initiation_s"] is not None else row.get("censored_s")
        if latency is None or latency <= target:
            continue
        st, created = row["stages"], row["created"]
        tag_id = world.rig.tag_id(row["tag"])
        at_bridge = st.get("bridge_received")
        missed = [o for t, tag, o in world.window_records if tag == tag_id and at_bridge is not None
                  and created + at_bridge <= t < created + latency and o != "served"]
        if st.get("companion_accepted", 0.0) > target / 2:
            cause = "Cremind -> companion took over 30 s (5xx burst)"
        elif missed:
            cause = f"missed wake window: {missed[0]}"
        elif at_bridge is None or at_bridge > target / 2:
            cause = "gateway queue + mesh took over 30 s"
        else:
            cause = "no window missed: arrived late in the wake cycle"
        tail[cause] += 1
        tail_rows.append({"id": row["id"], "latency_s": round(latency, 1), "cause": cause,
                          "at_bridge_s": round(at_bridge, 1) if at_bridge is not None else None,
                          "missed_windows": missed})

    # -- stage breakdown and tag-side timing ------------------------------------------------------
    breakdown = []
    for a, b, meaning in STAGE_PAIRS:
        values = [r["stages"][b] - r["stages"][a] for r in population if a in r["stages"] and b in r["stages"]]
        breakdown.append({"from": a, "to": b, "meaning": meaning, **summary(values)})
    composed_values = [r["composed_s"] - r["stages"].get("companion_accepted", 0.0) for r in population
                       if r["composed_s"] is not None and "companion_accepted" in r["stages"]]
    shown_revisions = [r for r in revisions if r["state"] == "displayed" and r["timing"]]
    tag_side = {k: summary(r["timing"].get(k, 0) / 1000.0 for r in shown_revisions if k in r["timing"])
                for k in TIMING_KEYS}
    # transfer_ms is FRAME_BEGIN -> RESULT (docs/bridge-firmware.md): the BLE part is transfer - refresh
    tag_side["ble_transfer_ms"] = summary(max(0, r["timing"].get("transfer_ms", 0) - r["timing"].get("refresh_ms", 0))
                                          / 1000.0 for r in shown_revisions if "transfer_ms" in r["timing"])
    saturated = {k: sum(1 for r in shown_revisions if r["timing"].get(k, 0) >= 0xFFFF) for k in TIMING_KEYS}

    # -- groups, tags, bridges -------------------------------------------------------------------
    groups: dict[str, Any] = {}
    for group in sorted({r["group"] for r in rows}):
        members = [r for r in rows if r["group"] == group]
        groups[group] = {"count": len(members), "final": dict(Counter(r["final"] for r in members)),
                         "initiation": summary(r["initiation_s"] for r in members if r["initiation_s"] is not None),
                         "display": summary(r["stages"]["displayed"] for r in members if "displayed" in r["stages"])}
    per_tag = {}
    for i in range(cfg.tags):
        members = [r for r in population if r["tag"] == i]
        per_tag[i] = {"bridge": world.bridge_of[i], "relayed": world.bridge_of[i] in world.relayed,
                      "trials": len(members), **{k: v for k, v in summary(
                          r["initiation_s"] if r["initiation_s"] is not None else r.get("censored_s", 0.0)
                          for r in members).items() if k in ("p50", "p95", "max")}}
    per_bridge = {}
    for b in range(cfg.bridges):
        members = [r for r in population if r["bridge"] == b]
        values = [r["initiation_s"] if r["initiation_s"] is not None else r.get("censored_s", 0.0) for r in members]
        timing_rows = [r for r in shown_revisions if world.bridge_of.get(tag_index.get(r["tag_id"])) == b]
        per_bridge[b] = {"name": world.sim.bridges[b].name, "relayed": b in world.relayed, "trials": len(members),
                         "within_target": sum(1 for v in values if v <= target), **summary(values),
                         "mesh_ms_p50": percentile((r["timing"].get("mesh_ms", 0) for r in timing_rows), 50),
                         "suspends_max_per_min": max_in_window(world.suspends[world.sim.bridges[b].name], 60000.0)}
    overall_p95 = acceptance["initiation"]["p95"] or 0.0
    starved = [i for i, v in per_tag.items() if v["trials"] and (v["p95"] or 0) > max(target, 2 * overall_p95)]

    # -- the bridges' scheduling (the policy of docs/scale-test.md §6.2) and the relay hop ------------
    live_bridges = live.get("bridges", {})
    traffic_min = max(1e-9, (world.traffic_end - world.traffic_start) / 60.0)
    in_traffic = {name: sum(1 for t in times if world.traffic_start * 1000.0 <= t <= world.traffic_end * 1000.0)
                  for name, times in world.suspends.items()}
    scheduler = {
        "sessions": world.sim.bridges[0].max_sessions, "quick_retry": world.sim.bridges[0].quick_retry,
        **{k: sum(int(b.get(k, 0)) for b in live_bridges.values())
           for k in ("suspend_count", "connect_failed", "quick_retries", "quick_retries_connected",
                     "quick_retries_failed", "concurrent_attempts", "concurrent_sessions", "rate_limited",
                     "backoff_skips", "deferred", "sessions_ok", "sessions_fail")},
        "max_links": max((int(b.get("max_links", 0)) for b in live_bridges.values()), default=0),
        "suspends_per_min_per_bridge": {name: n / traffic_min for name, n in in_traffic.items()},
        "max_suspends_per_rolling_min": max((max_in_window(t, 60000.0) for t in world.suspends.values()), default=0),
        "mesh_suspended_fraction": {name: int(b.get("suspended_ms", 0)) / max(1e-9, end_t * 1000.0)
                                    for name, b in live_bridges.items()},
    }
    relayed_rows = [r for r in shown_revisions if world.bridge_of.get(tag_index.get(r["tag_id"])) in world.relayed]
    direct_rows = [r for r in shown_revisions if world.bridge_of.get(tag_index.get(r["tag_id"])) not in world.relayed]
    overlay = live.get("overlay", {})
    relay = {
        "mesh_s_relayed": summary(r["timing"].get("mesh_ms", 0) / 1000.0 for r in relayed_rows),
        "mesh_s_direct": summary(r["timing"].get("mesh_ms", 0) / 1000.0 for r in direct_rows),
        "gateway_to_bridge_s_relayed": summary(r["stages"]["bridge_received"] - r["stages"]["gateway_received"]
                                               for r in population if r["relayed"]
                                               and {"gateway_received", "bridge_received"} <= r["stages"].keys()),
        "messages_relayed": overlay.get("relayed", 0),
        "waited_for_relay_resume": overlay.get("relay_waited_for_resume", 0),
        "relay_suspend_timeouts": overlay.get("relay_suspend_timeouts", 0),
    }

    # -- faults ------------------------------------------------------------------------------------
    fault_rows = []
    for f in world.faults:  # (FaultAct.detail carries the bridges a relay's reboot cut off)
        entry: dict[str, Any] = {"kind": f.kind, "t": round(f.t, 1), "target": f.target, "armed_at": f.armed_at,
                                 "fired_at": f.fired_at, **{k: v for k, v in f.detail.items() if k != "before"
                                                            and k != "lost_before"}}
        entry["affected"], entry["screen"] = _affected(rows, f, gt_transfer, gt_shown, shown_in, rev_by_key)
        fault_rows.append(entry)
    fault_summary = {}
    for kind in sorted({f["kind"] for f in fault_rows}):
        items = [f for f in fault_rows if f["kind"] == kind]
        affected = [a for f in items for a in f["affected"]]
        if kind in ("disconnect", "power_loss", "bridge_reboot"):  # the screen being transferred when it fired
            metric = "fault -> that screen (or a newer one) displayed"
            values = [f["screen"]["back_s"] for f in items if f["screen"] and f["screen"]["back_s"] is not None]
        elif kind == "cremind_5xx":
            metric = "queued -> displayed, cards queued during the burst"
            values = [a["recovery_s"] for a in affected if a["recovery_s"] is not None]
        else:
            metric = "fault -> displayed, cards in flight at the fault"
            values = [a["recovery_s"] for a in affected if a["recovery_s"] is not None]
        fault_summary[kind] = {
            "injected": len(items), "fired": sum(1 for f in items if f["fired_at"] is not None),
            "affected_deliveries": len(affected), "outcomes": dict(Counter(a["final"] for a in affected)),
            "screens": dict(Counter(f["screen"]["state"] for f in items if f["screen"])),
            "metric": metric, "recovery_s": summary(values), "counters": {}}
    if cfg.scenario == "faults":  # faults whose effect is a protocol repair, not one delivery
        gw, mesh, bridges = live.get("gateway", {}), live.get("mesh", {}), live.get("bridges", {}).values()
        if "status_loss" in fault_summary:
            fault_summary["status_loss"]["counters"] = {
                "LAYOUT_STATUS lost": mesh.get("lost_MeshLayoutStatus", 0),
                "commits re-sent": gw.get("commit_resends", 0),
                "repeated commits answered DUPLICATE": sum(b.get("commit_repeats", 0) for b in bridges)}
        fault_summary["chunk_loss"] = {
            "injected": f"p = {cfg.chunk_loss:g} per chunk", "fired": mesh.get("lost_MeshLayoutChunk", 0),
            "affected_deliveries": 0, "outcomes": {}, "screens": {}, "metric": "", "recovery_s": summary([]),
            "counters": {"chunks lost": mesh.get("lost_MeshLayoutChunk", 0), "INCOMPLETE answers":
                         sum(b.get("incomplete", 0) for b in bridges), "chunks re-sent": gw.get("chunks_resent", 0)}}

    # -- invariants -------------------------------------------------------------------------------
    invariants: dict[str, dict[str, Any]] = {}

    def check(name: str, problems: list[Any], **info: Any) -> None:
        invariants[name] = {"ok": not problems, "violations": len(problems), "examples": problems[:10], **info}

    final_footer = set()
    for tag_id, view in views.items():
        rev = rev_by_key.get((tag_id, view["displayed_revision"]))
        if rev is not None:
            final_footer |= set(rev["pending_delivery_ids"])
    lost = []
    for row in rows:
        d = fake.deliveries[row["id"]]
        if row["accepted"] and not row["in_db"]:
            lost.append({"id": row["id"], "problem": "accepted but not in the companion's database"})
        elif row["final"] not in TERMINAL and row["expires_s"] > end_t - row["created"]:
            job = jobs.get(row["id"])
            footer_ok = row["id"] in final_footer and job is not None and job["state"] == "active" \
                and job["outcome"] is None
            if not footer_ok:
                lost.append({"id": row["id"], "problem": f"still {d['stage']} in Cremind at the end",
                             "companion": (job or {}).get("state"), "outcome": (job or {}).get("outcome")})
    check("accepted_jobs_not_lost", lost, counted_in_a_footer_at_end=len(final_footer))
    conflicting = [{"id": k, "outcomes": sorted(v)} for k, v in fake.terminal_outcomes().items() if len(v) > 1]
    check("one_terminal_outcome_per_delivery", conflicting)

    digest_problems = []
    reference_checked = 0
    for r in revisions:
        if r["state"] != "displayed" or r["purpose"] == "blank":
            continue
        tag = world.sim.tag(r["tag_id"]).spec
        frame = render_frame(bytes(r["layout"]), Panel(tag.width, tag.height, tag.planes, tag.plane_flags), world.pack)
        reference_checked += 1
        if frame.digest[:8].hex() != r["frame_digest"]:
            digest_problems.append({"tag": f"{r['tag_id']:08X}", "revision": r["revision"],
                                    "reported": r["frame_digest"], "reference": frame.digest[:8].hex()})
    receipts_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for rec in fake.receipt_log:
        receipts_by_id[int(rec["delivery_id"])].append(rec)
    footer_displayed, cancel_problems = [], []
    for did, recs in receipts_by_id.items():
        for rec in recs:
            if rec.get("outcome") != "displayed":
                continue
            rev = rev_by_key.get((int(rec["tag_id"], 16), rec.get("revision")))
            if rev is None or did not in rev["delivery_ids"]:
                footer_displayed.append({"id": did, "revision": rec.get("revision"),
                                         "in_footer": bool(rev and did in rev["pending_delivery_ids"])})
            elif rec.get("digest") != rev["frame_digest"]:
                digest_problems.append({"id": did, "revision": rec.get("revision"), "receipt": rec.get("digest"),
                                        "revision_digest": rev["frame_digest"]})
    panel_problems = []
    if world.drained:
        for tag_id, view in views.items():
            panel = live.get("panels", {}).get(f"{tag_id:08X}")
            if view["displayed_revision"] and panel != view["displayed_digest"]:
                panel_problems.append({"tag": f"{tag_id:08X}", "panel": panel, "companion": view["displayed_digest"],
                                       "revision": view["displayed_revision"]})
    check("displayed_frame_is_reference_render", digest_problems + panel_problems, revisions_rendered=reference_checked,
          panels_compared=len(views) if world.drained else 0)
    check("footer_cards_never_receipted_displayed", footer_displayed)

    backwards = []
    rank = {s: i for i, s in enumerate(STAGES)}
    for did, recs in receipts_by_id.items():
        best, terminal = -1, None
        seen: set[str] = set()
        for rec in recs:
            identity = json.dumps(rec, sort_keys=True, default=str)
            if identity in seen:
                continue  # the same receipt again: a retried POST whose answer was lost (idempotent)
            seen.add(identity)
            outcome = rec.get("outcome")
            if terminal is not None:
                if outcome != terminal:
                    backwards.append({"id": did, "problem": f"{rec.get('stage')}/{outcome} after terminal {terminal}"})
                continue
            r_ = rank.get(rec.get("stage") or "", -1)
            if r_ < best:
                backwards.append({"id": did, "problem": f"stage {rec.get('stage')} after {STAGES[best]}"})
            best = max(best, r_)
            if outcome in TERMINAL:
                terminal = outcome
    check("receipts_monotonic", backwards, receipts=len(fake.receipt_log))

    for c in world.cancels:
        if c.get("resolving_id") is None:
            continue
        did = c["delivery_id"]
        resolving = jobs.get(c["resolving_id"])
        known_at = _ts(resolving["received_at"]) if resolving else None
        for rec in receipts_by_id.get(did, []):
            if rec.get("outcome") != "displayed":
                continue
            rev = rev_by_key.get((int(rec["tag_id"], 16), rec.get("revision")))
            if rev is not None and known_at is not None and rev["created_ts"] > known_at:
                cancel_problems.append({"id": did, "revision": rev["revision"],
                                        "problem": "displayed by a screen composed after the cancel arrived"})
    check("cancelled_cards_never_shown_after_the_cancel", cancel_problems,
          cancels=sum(1 for c in world.cancels if c.get("resolving_id")),
          skipped=sum(1 for c in world.cancels if c.get("skipped")))

    suspend_problems = [{"bridge": name, "max_per_min": max_in_window(times, 60000.0)}
                        for name, times in world.suspends.items()
                        if max_in_window(times, 60000.0) > BRIDGE_MAX_SUSPENDS_PER_MIN]
    check("bridge_suspends_within_rate_limit", suspend_problems, limit=BRIDGE_MAX_SUSPENDS_PER_MIN,
          max_per_min={name: max_in_window(times, 60000.0) for name, times in world.suspends.items()})

    load = _load_report(world, revisions, live)
    busy = []
    idle = load["phases"].get("idle", {})
    active = load["phases"].get("traffic", {})
    limits = {"idle_scheduler_passes_per_min": 45.0, "idle_requests_per_min": 30.0, "idle_frames_per_min": 120.0,
              "idle_cpu_fraction": 0.5, "scheduler_passes_per_min": 200.0, "requests_per_min": 200.0,
              "max_revision_attempts": 12 if cfg.scenario == "faults" else 6, "peak_requests_per_min": 300.0,
              "deliver_layout_per_displayed_revision": 6.0 if cfg.scenario == "faults" else 3.0}
    for key, value in (("idle_scheduler_passes_per_min", idle.get("passes_per_min")),
                       ("idle_requests_per_min", idle.get("requests_per_min")),
                       ("idle_frames_per_min", idle.get("frames_per_min")),
                       ("idle_cpu_fraction", idle.get("cpu_fraction")),
                       ("scheduler_passes_per_min", active.get("passes_per_min")),
                       ("requests_per_min", active.get("requests_per_min")),
                       ("max_revision_attempts", load["max_revision_attempts"]),
                       ("peak_requests_per_min", load["peak_requests_per_min"]),
                       ("deliver_layout_per_displayed_revision", load["deliver_layout_per_displayed_revision"])):
        if value is not None and value > limits[key]:
            busy.append({"metric": key, "value": value, "limit": limits[key]})
    check("no_busy_loops_or_runaway_retries", busy, limits=limits)
    check("no_errors_logged", world.errors.records)
    check("settled_after_traffic", [] if world.drained else [world.drain_detail], drain=world.drain_detail)

    return {
        "config": {**{k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items()},
                   "competing": asdict(world.competing) if world.competing else None},
        "host": {"python": sys.version.split()[0], "platform": platform.platform(),
                 "machine": platform.machine()},
        "sim_s": end_t, "traffic_s": world.traffic_end - world.traffic_start,
        "acceptance": acceptance, "breakdown": breakdown, "compose_s": summary(composed_values),
        "tag_side": tag_side, "tag_side_saturated_u16": saturated, "windows": window_summary, "groups": groups,
        "tail": {"causes": dict(tail.most_common()), "trials": tail_rows}, "per_tag": per_tag,
        "per_bridge": per_bridge, "starved_tags": starved, "scheduler": scheduler, "relay": relay,
        "faults": fault_summary, "fault_events": fault_rows, "cancels": world.cancels,
        "invariants": invariants, "load": load, "live": {k: v for k, v in live.items() if k != "panels"},
        "outbox_left": db["outbox"], "errors": world.errors.records[:20],
        "revisions": {"count": len(revisions), "states": dict(Counter(r["state"] for r in revisions)),
                      "last_status": dict(Counter(r["last_status"] for r in revisions if r["last_status"]))},
        "outcomes": dict(Counter(r["final"] for r in rows)),
        "deliveries": len(rows),
        "detail": [_detail(r, rev_by_key, shown_in, footer_in) for r in rows],
    }


def _detail(row: dict[str, Any], rev_by_key: dict[tuple[int, int], dict[str, Any]],
            shown_in: dict[int, list[tuple[int, int]]], footer_in: dict[int, list[tuple[int, int]]]) -> dict[str, Any]:
    """One delivery, compactly (for explaining the tail)."""

    def rnd(v: Any) -> Any:
        return round(v, 2) if isinstance(v, float) else v

    revs = []
    for key in sorted(set(shown_in.get(row["id"], [])) | set(footer_in.get(row["id"], [])), key=lambda k: k[1]):
        rev = rev_by_key.get(key)
        if rev is not None:
            revs.append({"revision": key[1], "shown": key in shown_in.get(row["id"], []), "state": rev["state"],
                         "attempts": rev["attempts"], "last_status": rev["last_status"],
                         "timing": rev["timing"]})
    keys = ("id", "group", "kind", "trial", "tag", "bridge", "relayed", "created", "final", "companion_outcome",
            "footer_held", "initiation_s", "initiation_source", "gt_transfer_s", "gt_displayed_s", "composed_s",
            "ended_s", "censored_s")
    return {**{k: rnd(row.get(k)) for k in keys}, "stages": {k: rnd(v) for k, v in row["stages"].items()},
            "revisions": revs}


def windows(world: World) -> dict[str, Any]:
    """Advertising windows of tags with work waiting at their bridge, by what became of them: ``served`` (a
    session, possibly after a quick retry), ``session_failed``, ``connect_failed``, ... (the attempts in the
    window failed), ``backoff`` (the tag's back-off after a failure), ``bridge_busy`` (the bridge could not
    start an attempt: another initiation in progress, every session slot taken, or a session streaming;
    with one session per bridge any session with another tag), ``rate_limited``, ``not_attempted``
    (anything else: bridge off the mesh, deferred)."""
    import cremind_tag.sim.tag as sim_tag
    from cremind_tag.sim.bridge import BUSY_REASONS

    adv_s = sim_tag.TAG_ADV_WINDOW_MS / 1000.0
    by_tag: dict[int, list[tuple[float, float, str, int, str]]] = defaultdict(list)
    by_bridge: dict[str, list[tuple[float, float, str, int, str]]] = defaultdict(list)
    for a in world.attempts:
        by_tag[a[3]].append(a)
        by_bridge[a[2]].append(a)
    outcomes: Counter[str] = Counter()
    per_bridge: dict[str, Counter[str]] = defaultdict(Counter)
    records: list[tuple[float, int, str]] = []
    served_by_retry = 0
    for started, tag, bridge, backoff, limited in world.windows:
        own = [a for a in by_tag[tag] if started - 0.05 <= a[0] <= started + adv_s + 0.3]
        reasons = {r for t, r in world.decisions.get(tag, ()) if started - 0.05 <= t <= started + adv_s + 0.05}
        if own:
            ok = [a for a in own if a[4] == "sessions_ok"]
            outcome = "served" if ok else own[0][4].removesuffix("s")
            served_by_retry += bool(ok) and ok[0] is not own[0]
        elif backoff:
            outcome = "backoff"
        elif reasons & BUSY_REASONS or any(a[3] != tag and a[0] < started + adv_s and a[1] > started
                                           for a in by_bridge[bridge] if not reasons):
            outcome = "bridge_busy"
        elif limited or "rate_limited" in reasons:
            outcome = "rate_limited"
        else:
            outcome = "not_attempted"
        outcomes[outcome] += 1
        per_bridge[bridge][outcome] += 1
        records.append((started, tag, outcome))
    world.window_records = records
    total = sum(outcomes.values())
    return {"with_work": total, "served_fraction": outcomes["served"] / total if total else None,
            "outcomes": dict(outcomes.most_common()), "served_after_quick_retry": served_by_retry,
            "by_bridge": {b: dict(c) for b, c in sorted(per_bridge.items())},
            "attempts": dict(Counter(a[4] for a in world.attempts))}


def _status_name(value: Any) -> str:
    from cremind_tag.protocol.ids import Status

    try:
        return Status(int(value)).name
    except (TypeError, ValueError):
        return str(value)


def _ts(iso_text: str | None) -> float | None:
    if not iso_text:
        return None
    import datetime as dt

    return dt.datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp()


def _affected(rows: list[dict[str, Any]], f: FaultAct, gt_transfer: dict[tuple[int, int], float],
              gt_shown: dict[tuple[int, int], float], shown_in: dict[int, list[tuple[int, int]]],
              rev_by_key: dict[tuple[int, int], dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The deliveries a fault hit (final stage, time from the fault to their display) and, for a tag fault, the
    screen it hit (the revision the bridge was transferring when it fired) and when the tag showed it or a
    newer one."""
    if f.fired_at is None or f.kind == "status_loss":  # a lost status is attributed through the counters
        return [], None
    fired = f.fired_at

    def entry(r: dict[str, Any], since: float) -> dict[str, Any]:
        displayed = r["stages"].get("displayed")
        return {"id": r["id"], "final": r["final"],
                "recovery_s": (r["created"] + displayed - since) if displayed is not None else None}

    def screen_hit(tag_id: int, rev: int) -> dict[str, Any]:
        back = [t for (tag, r), t in gt_shown.items() if tag == tag_id and r >= rev and t >= fired]
        return {"tag": f"{tag_id:08X}", "revision": rev, "state": rev_by_key.get((tag_id, rev), {}).get("state"),
                "back_s": (min(back) - fired) if back else None}

    if f.kind in ("disconnect", "power_loss"):
        tag_id = int(f.target, 16)
        started = [(t, rev) for (tag, rev), t in gt_transfer.items() if tag == tag_id and t <= fired]
        if not started:
            return [], None
        _, rev = max(started)
        screen = screen_hit(tag_id, rev)
        hit = []
        for r in rows:
            displayed = r["stages"].get("displayed")
            if (tag_id, rev) in shown_in.get(r["id"], []) and (displayed is None or r["created"] + displayed > fired):
                hit.append(entry(r, fired))
        return hit, screen
    hit = []
    for r in rows:
        if r["footer_held"] or (f.kind == "bridge_reboot" and r["bridge"] not in f.detail.get("bridges", [f.target])):
            continue
        if f.kind == "cremind_5xx":
            if fired <= r["created"] <= fired + f.duration:
                hit.append(entry(r, r["created"]))
            continue
        sent, displayed, ended = r["stages"].get("gateway_received"), r["stages"].get("displayed"), r["ended_s"]
        if sent is None or r["created"] + sent > fired:
            continue  # not at the gateway yet
        if (displayed is not None and r["created"] + displayed <= fired) or (ended is not None
                                                                            and r["created"] + ended <= fired):
            continue  # already over
        hit.append(entry(r, fired))
    screen = screen_hit(*f.detail["hit"]) if f.kind == "bridge_reboot" and f.detail.get("hit") else None
    return hit, screen


def _load_report(world: World, revisions: list[dict[str, Any]], live: dict[str, Any]) -> dict[str, Any]:
    marks = world.phase_marks
    phases: dict[str, Any] = {}
    for name, nxt in (("traffic", "drain"), ("drain", "idle"), ("idle", "end")):
        a, b = marks.get(name), marks.get(nxt)
        if a is None or b is None:
            continue
        minutes = max(1e-9, (b["sim_s"] - a["sim_s"]) / 60.0)
        real = max(1e-9, b["real_s"] - a["real_s"])
        phases[name] = {
            "sim_s": b["sim_s"] - a["sim_s"], "real_s": real,
            "passes_per_min": (b["passes"] - a["passes"]) / minutes,
            "requests_per_min": (b["requests"] - a["requests"]) / minutes,
            "frames_per_min": (b["frames"] - a["frames"]) / minutes,
            "cpu_fraction": (b["cpu_s"] - a["cpu_s"]) / real,
            "db_real_s": b["db_real_s"] - a["db_real_s"], "db_calls": b["db_calls"] - a["db_calls"],
        }
    displayed = sum(1 for r in revisions if r["state"] == "displayed")
    deliver = live.get("gateway", {}).get("deliveries_accepted", 0)
    requests = Counter(route for _, route, _ in world.fake.requests)
    return {
        "phases": phases, "max_revision_attempts": max((r["attempts"] for r in revisions), default=0),
        "deliver_layout_per_displayed_revision": deliver / displayed if displayed else None,
        "requests_by_route": dict(requests.most_common(12)), "db_calls": world.db_calls,
        "transfers": deliver, "superseded_at_bridge": live.get("gw_results", {}).get("SUPERSEDED", 0),
        "peak_requests_per_min": max_in_window(world.wire.times, 60.0),
        "peak_requests_per_10s": max_in_window(world.wire.times, 10.0),
        "db_real_s": world.db_real_s, "scheduler_passes": world.passes,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _f(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown(reports: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for rep in reports:
        cfg = rep["config"]
        acc = rep["acceptance"]
        out.append(f"### Scenario `{cfg['scenario']}` — seed {cfg['seed']}, time scale {cfg['time_scale']:g}, "
                   f"{acc['trials']} trials")
        out.append("")
        out.append(f"Simulated {rep['sim_s'] / 60:.1f} min (traffic {rep['traffic_s'] / 60:.1f} min) in "
                   f"{rep['real_s'] / 60:.1f} min of wall time; {rep['deliveries']} deliveries in total "
                   f"({', '.join(f'{k} {v}' for k, v in sorted(rep['outcomes'].items()))}).")
        out.append("")
        frac = acc["fraction_within_target"]
        out.append(f"**Delivery initiation within {acc['target_s']:.0f} s: "
                   f"{acc['within_target']}/{acc['population']} = {_f(frac * 100 if frac is not None else None)} %** "
                   f"(target {acc['target_fraction'] * 100:.0f} %: {'met' if acc['met'] else 'NOT met'}). "
                   f"Excluded from the population: {acc['excluded'] or 'none'}.")
        out.append("")
        out.append("| Measure (simulated s) | n | p50 | p95 | p99 | max |")
        out.append("|---|---|---|---|---|---|")
        for label, s in (("initiation: queued -> transferring (receipt)", acc["initiation"]),
                         ("initiation, simulator ground truth (bridge starts the frame)", acc["initiation_simulator"]),
                         ("end to end: queued -> displayed", acc["display"])):
            out.append(f"| {label} | {s['n']} | {_f(s['p50'])} | {_f(s['p95'])} | {_f(s['p99'])} | {_f(s['max'])} |")
        out.append("")
        out.append("| Stage (trials) | meaning | n | p50 | p95 | p99 | max |")
        out.append("|---|---|---|---|---|---|---|")
        for b in rep["breakdown"]:
            out.append(f"| {b['from']} -> {b['to']} | {b['meaning']} | {b['n']} | {_f(b['p50'])} | {_f(b['p95'])} "
                       f"| {_f(b['p99'])} | {_f(b['max'])} |")
        out.append("")
        out.append("| Tag side, per displayed screen (s) | n | p50 | p95 | p99 | max |")
        out.append("|---|---|---|---|---|---|")
        for key, s in rep["tag_side"].items():
            out.append(f"| {key.removesuffix('_ms')} | {s['n']} | {_f(s['p50'], 2)} | {_f(s['p95'], 2)} "
                       f"| {_f(s['p99'], 2)} | {_f(s['max'], 2)} |")
        out.append("")
        w = rep["windows"]
        out.append(f"Advertising windows with work waiting at the bridge: {w['with_work']}, served "
                   f"{_f((w['served_fraction'] or 0) * 100)} %; "
                   + ", ".join(f"{k} {v}" for k, v in w["outcomes"].items()) + ".")
        out.append("")
        if rep["tail"]["causes"]:
            out.append("Trials over the target, by cause: "
                       + ", ".join(f"{k} {v}" for k, v in rep["tail"]["causes"].items()) + ".")
            out.append("")
        sc, relay = rep["scheduler"], rep["relay"]
        out.append(f"Bridge scheduling: {sc['sessions']} session(s) per bridge, quick retry "
                   f"{'on' if sc['quick_retry'] else 'off'}; {sc['suspend_count']} suspensions (at most "
                   f"{sc['max_suspends_per_rolling_min']} in a rolling minute, "
                   f"{_f(max(sc['suspends_per_min_per_bridge'].values(), default=0.0), 2)}/min on the busiest bridge "
                   f"during the traffic), {sc['connect_failed']} failed connections, {sc['quick_retries']} quick "
                   f"retries ({sc['quick_retries_connected']} connected), {sc['concurrent_sessions']} sessions "
                   f"alongside another; relay hop: mesh p50/p95 {_f(relay['mesh_s_relayed']['p50'], 2)}/"
                   f"{_f(relay['mesh_s_relayed']['p95'], 2)} s relayed, {_f(relay['mesh_s_direct']['p50'], 2)}/"
                   f"{_f(relay['mesh_s_direct']['p95'], 2)} s direct; {relay['waited_for_relay_resume']} messages "
                   f"waited for the relay's resume ({relay['relay_suspend_timeouts']} timed out).")
        out.append("")
        out.append("| Bridge | relayed | trials | within target | p50 | p95 | max | mesh_ms p50 | max suspends/min |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for b in rep["per_bridge"].values():
            out.append(f"| {b['name']} | {'yes' if b['relayed'] else 'no'} | {b['trials']} | {b['within_target']} "
                       f"| {_f(b['p50'])} | {_f(b['p95'])} | {_f(b['max'])} | {_f(b['mesh_ms_p50'])} "
                       f"| {b['suspends_max_per_min']} |")
        out.append("")
        out.append("| Traffic class | count | final stages | initiation p50 / p95 | display p50 / p95 |")
        out.append("|---|---|---|---|---|")
        for g, s in rep["groups"].items():
            out.append(f"| {g} | {s['count']} | {', '.join(f'{k} {v}' for k, v in sorted(s['final'].items()))} "
                       f"| {_f(s['initiation']['p50'])} / {_f(s['initiation']['p95'])} "
                       f"| {_f(s['display']['p50'])} / {_f(s['display']['p95'])} |")
        if rep["faults"]:
            out.append("")
            out.append("| Fault | injected | fired | affected deliveries: final stage | screens hit | measured "
                       "| p50 / max (s) |")
            out.append("|---|---|---|---|---|---|---|")
            for kind, s in rep["faults"].items():
                counters = ", ".join(f"{k} {v}" for k, v in s["counters"].items())
                out.append(f"| {kind} | {s['injected']} | {s['fired']} | {s['affected_deliveries']}: "
                           f"{', '.join(f'{k} {v}' for k, v in sorted(s['outcomes'].items())) or '-'} "
                           f"| {', '.join(f'{k} {v}' for k, v in sorted(s['screens'].items())) or '-'} "
                           f"| {'; '.join(x for x in (s['metric'], counters) if x) or '-'} "
                           f"| {_f(s['recovery_s']['p50'])} / {_f(s['recovery_s']['max'])} |")
        out.append("")
        out.append("| Invariant | holds | detail |")
        out.append("|---|---|---|")
        for name, inv in rep["invariants"].items():
            extra = {k: v for k, v in inv.items() if k not in ("ok", "violations", "examples")}
            detail = (json.dumps(extra, default=str)[:160] if inv["ok"]
                      else json.dumps(inv["examples"][:2], default=str)[:200])
            out.append(f"| {name} | {'yes' if inv['ok'] else f'NO ({inv['violations']})'} | {detail} |")
        load = rep["load"]
        out.append("")
        phases = load["phases"]
        out.append(f"Mesh transfers: {load['transfers']}, of which {load['superseded_at_bridge']} were screens "
                   f"superseded at the bridge before a tag saw them. "
                   f"Connector requests: peak {load['peak_requests_per_min']}/min, "
                   f"{load['peak_requests_per_10s']} in 10 s; max attempts of one revision "
                   f"{load['max_revision_attempts']}; DELIVER_LAYOUT accepted per displayed screen "
                   f"{_f(load['deliver_layout_per_displayed_revision'], 2)}.")
        out.append("")
        out.append("| Phase | sim min | scheduler passes/min | connector requests/min | serial frames/min "
                   "| CPU (of one core) |")
        out.append("|---|---|---|---|---|---|")
        for name, p in phases.items():
            out.append(f"| {name} | {p['sim_s'] / 60:.1f} | {_f(p['passes_per_min'])} | {_f(p['requests_per_min'])} "
                       f"| {_f(p['frames_per_min'])} | {_f(p['cpu_fraction'] * 100)} % |")
        out.append("")
    return "\n".join(out)


def console_summary(rep: dict[str, Any]) -> str:
    acc = rep["acceptance"]
    inv_bad = [k for k, v in rep["invariants"].items() if not v["ok"]]
    frac = acc["fraction_within_target"]
    lines = [f"[{rep['config']['scenario']}] trials={acc['trials']} population={acc['population']} "
             f"within {acc['target_s']:.0f}s={acc['within_target']} ({_f(frac * 100 if frac is not None else None)}%) "
             f"initiation p50/p95/p99={_f(acc['initiation']['p50'])}/{_f(acc['initiation']['p95'])}/"
             f"{_f(acc['initiation']['p99'])}s display p50/p95/p99={_f(acc['display']['p50'])}/"
             f"{_f(acc['display']['p95'])}/{_f(acc['display']['p99'])}s sim={rep['sim_s'] / 60:.1f}min "
             f"wall={rep['real_s'] / 60:.1f}min",
             f"[{rep['config']['scenario']}] invariants: "
             f"{'all hold' if not inv_bad else 'VIOLATED: ' + ', '.join(inv_bad)}"]
    sc = rep.get("scheduler")
    if sc:
        w = rep["windows"]
        lines.append(f"[{rep['config']['scenario']}] sessions={sc['sessions']} quick_retry={sc['quick_retry']} "
                     f"windows served={_f((w['served_fraction'] or 0) * 100)}% {w['outcomes']} "
                     f"max suspends/min={sc['max_suspends_per_rolling_min']} quick retries={sc['quick_retries']} "
                     f"concurrent sessions={sc['concurrent_sessions']} tail={rep['tail']['causes']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--scenario", choices=("baseline", "faults", "both"), default="both")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--time-scale", type=float, default=10.0)
    parser.add_argument("--rate", type=float, default=5.0, help="traffic events per simulated minute")
    parser.add_argument("--competing", type=float, default=1.0, help="competing-traffic level (0 = off)")
    parser.add_argument("--relayed", type=int, default=1, help="bridges behind a relay (the last N)")
    parser.add_argument("--adv-window-ms", type=float, help="what-if: the tags' advertising window (default 2000)")
    parser.add_argument("--sessions", type=int, help="what-if: tag sessions per bridge at once (default: the "
                        "simulator's, the nRF52840 bridge's)")
    parser.add_argument("--quick-retry", action=argparse.BooleanOptionalAction, default=None,
                        help="what-if: one retry within the tag's window after a failed connection (default: the "
                        "simulator's)")
    parser.add_argument("--json", type=Path, help="write the full report(s) here")
    parser.add_argument("--markdown", type=Path, help="write the result tables here")
    parser.add_argument("--daemon-log", type=Path, help="write the daemon's and simulator's INFO log here")
    parser.add_argument("--keep", type=Path, help="keep the run's data directory here (database, secrets)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)  # --daemon-log raises cremind_tag to INFO; keep the console quiet
    try:
        fonts = load_fonts()
    except ScaleSetupError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    scenarios = ("baseline", "faults") if args.scenario == "both" else (args.scenario,)
    reports = []
    for scenario in scenarios:
        work = args.keep / scenario if args.keep else None
        cfg = ScaleConfig(trials=args.trials, scenario=scenario, seed=args.seed, time_scale=args.time_scale,
                          event_rate_per_min=args.rate, competing_level=args.competing, relayed_bridges=args.relayed,
                          adv_window_ms=args.adv_window_ms, sessions=args.sessions, quick_retry=args.quick_retry,
                          work_dir=work, keep_work_dir=bool(args.keep),
                          daemon_log=(args.daemon_log.with_name(f"{args.daemon_log.stem}-{scenario}{args.daemon_log.suffix}")
                                      if args.daemon_log else None))
        print(f"running {scenario}: {cfg.trials} trials, seed {cfg.seed}, time scale {cfg.time_scale:g} ...",
              flush=True)
        report = run_scale(cfg, fonts)
        reports.append(report)
        print(console_summary(report), flush=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(reports, indent=1, default=str), encoding="utf-8")
    text = markdown(reports)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(text, encoding="utf-8")
    ok = all(v["ok"] for rep in reports for v in rep["invariants"].values())
    ok = ok and all(rep["acceptance"]["met"] for rep in reports if rep["config"]["scenario"] == "baseline")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
