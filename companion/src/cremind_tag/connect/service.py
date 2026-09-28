"""The supervisor: ``cremind-connect service`` (docs/connect-setup.md §11.1, §11.6).

One per OS user (:mod:`.instance`). It stays headless and does four things:

**IPC** (:mod:`.ipc`) — ops:

===============  ==================================================================================
``ping``         ``{version, pid, installation_id}``
``status``       ``{version, installation_id, computer, pid, started_at, workers: [{worker_id, server,
                 profile, gateway_device_id, state, port, pid, last_error, ...}], ports: [{device,
                 vid, pid, serial_number, identity|null, reason, held_by|null}]}``
``list_gateways`` probes every free candidate port *now* (fresh challenges, for the setup window);
                 ports held by workers are reported in use, never opened
``open_link``    ``{url, env?}``: validates the link (:mod:`.links`) and starts a separate
                 ``window`` process of this program for it (one per setup session), so the service
                 never needs a display
``add_worker``   ``{worker_id}``: (re)load ``workers/<id>/worker.json`` now
``remove_worker`` ``{worker_id, delete?}``: stop the worker; disable it (``enabled: false``) or,
                 with ``delete: true``, remove its directory
``stop``         shut down gracefully
===============  ==================================================================================

**USB watcher** — every 2 s: enumerate candidate ports (:mod:`.usb`); probe
(:mod:`.probe`) ports that are new or changed and held by no worker; cache the
result by ``(device, serial number)``. A found identity is kept until the port
disappears or its worker exits; ``busy``/``no_answer``/errors are retried after
a pause; ``v1_firmware`` is not retried until the device is plugged in again.

**Workers** — each ``workers/<id>/worker.json`` (:mod:`.workerdir`) whose gateway
``device_id`` is attached and not in use gets a child ``<this program> worker
--dir <dir> --port <device>`` (stdout/stderr to ``workers/<id>/logs/worker-console.log``,
environment ``CREMIND_CONNECT_SUPERVISED=1``). A worker that exits is restarted
with exponential back-off, 1 s doubling to 60 s (reset after a minute of
running; a gateway that re-appears starts at once). Never two workers for one
gateway or one port. Contract with the worker: its **stdin is a pipe that the
supervisor closes to ask it to stop** (EOF also arrives when the supervisor
dies); it has 10 s before it is terminated. On Windows every worker is in a job
object that kills it with the supervisor.

**Shutdown** — SIGTERM/SIGINT/SIGBREAK, console control events, or ``stop``:
the IPC server stops, workers are asked to stop and then terminated, the lock
is released. Setup windows are independent processes and stay.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import ipc
from .instance import InstanceLock
from .links import LinkError, parse_setup_link, redact
from .paths import ConnectPaths
from .probe import BUSY, ERROR, GONE, NO_ACCESS, NO_ANSWER, V1_FIRMWARE, ProbeResult, probe_port
from .runtime import connect_version, executable, is_frozen, is_newer, no_window_flags, self_command, spawn_detached
from .usb import PortInfo, list_candidate_ports
from .workerdir import WorkerDirError, WorkerSpec, list_workers, load_worker, write_worker

log = logging.getLogger(__name__)

SCAN_INTERVAL_S = 2.0
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_RUN_S = 60.0
STOP_GRACE_S = 10.0
LINK_ENV = "CREMIND_CONNECT_LINK"
SUPERVISED_ENV = "CREMIND_CONNECT_SUPERVISED"
# When to probe a port again after an unsuccessful probe (None: not until it is plugged in again).
RETRY_AFTER_S: dict[str | None, float | None] = {BUSY: 5.0, NO_ANSWER: 10.0, GONE: 2.0, NO_ACCESS: 30.0,
                                                 ERROR: 30.0, V1_FIRMWARE: None}
# Display environment a URL handler forwards for the window it asks for (Linux desktops).
WINDOW_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                   "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE")


class Process(Protocol):
    """What the supervisor needs from a child (``subprocess.Popen`` satisfies it)."""

    pid: int
    stdin: Any

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Spawner = Callable[[list[str], Path, dict[str, str], Path], Process]
Prober = Callable[[str], ProbeResult]
PortLister = Callable[[], list[PortInfo]]


@dataclass
class PortState:
    info: PortInfo
    probe: ProbeResult | None = None
    probed_at: float | None = None
    held_by: str | None = None
    fresh: bool = True
    """Not probed since the port appeared (a gateway found now was just plugged in)."""
    stale: bool = False
    """The last result is from before a worker used the port: shown, but probed again before use."""

    @property
    def gateway_id(self) -> str | None:
        """The device id of the v2 *gateway* on this port, if the last (current) probe found one."""
        identity = self.probe.identity if self.probe and not self.stale else None
        return identity.device_id_hex if identity is not None and identity.is_gateway else None

    def as_json(self, *, include_challenge: bool = False) -> dict[str, Any]:
        identity = self.probe.identity if self.probe else None
        return {**self.info.as_json(), "held_by": self.held_by,
                "identity": identity.as_json(include_challenge=include_challenge) if identity else None,
                "reason": self.probe.reason if self.probe else None,
                "detail": self.probe.detail if self.probe else None}


@dataclass
class WorkerState:
    worker_id: str
    directory: Path
    spec: WorkerSpec | None
    error: str | None = None
    process: Process | None = None
    port: str | None = None
    started_at: float | None = None
    next_start: float = 0.0
    failures: int = 0
    restarts: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    stopping: bool = False
    conflict: bool = False

    @property
    def running(self) -> bool:
        return self.process is not None

    def state(self, now: float) -> str:
        if self.spec is None:
            return "invalid"
        if self.stopping:
            return "stopping"
        if self.process is not None:
            return "running"
        if not self.spec.enabled:
            return "disabled"
        if self.conflict:
            return "conflict"
        if self.next_start > now:
            return "backoff"
        return "waiting_for_gateway"

    def as_json(self, now: float) -> dict[str, Any]:
        spec = self.spec
        return {"worker_id": self.worker_id, "server": spec.server_origin if spec else None,
                "profile": spec.profile if spec else None, "companion_id": spec.companion_id if spec else None,
                "gateway_device_id": spec.gateway_device_id if spec else None,
                "enabled": spec.enabled if spec else False, "state": self.state(now), "port": self.port,
                "pid": self.process.pid if self.process is not None else None,
                "last_error": self.error or self.last_error, "last_exit_code": self.last_exit_code,
                "restarts": self.restarts,
                "retry_in_s": round(max(0.0, self.next_start - now), 1) if self.state(now) == "backoff" else None}


@dataclass
class _Window:
    process: Process
    session: str


@dataclass
class SupervisorOptions:
    """Injection points (tests); the defaults are the real thing."""

    list_ports: PortLister = list_candidate_ports
    probe: Prober = probe_port
    spawn: Spawner | None = None
    open_window: Callable[[str, dict[str, str]], Process] | None = None
    clock: Callable[[], float] = time.monotonic
    worker_command: Callable[[Path, str], list[str]] | None = None
    version: str | None = None
    computer: str | None = None
    installation_id: str | None = None
    probe_workers: int = 4
    extra: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    """The service (see the module docstring)."""

    def __init__(self, paths: ConnectPaths, options: SupervisorOptions | None = None) -> None:
        self.paths = paths
        self.opts = options or SupervisorOptions()
        self.version = self.opts.version or connect_version()
        self.computer = self.opts.computer
        self.installation_id = self.opts.installation_id
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._lock = threading.RLock()
        self._port_locks: dict[str, threading.Lock] = {}
        self._ports: dict[str, PortState] = {}
        self._workers: dict[str, WorkerState] = {}
        self._windows: list[_Window] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._job = _KillOnCloseJob.create() if sys.platform == "win32" and self.opts.spawn is None else None
        self._server: ipc.IpcServer | None = None
        self._instance: InstanceLock | None = None

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> int:
        """The service's main: lock, identity, IPC, the watch loop until stopped. Returns the exit code."""
        self.paths.ensure()
        self._instance = InstanceLock(self.paths.service_lock, self.paths.service_info)
        if not self._instance.acquire():
            log.info("service: another Cremind Connect service is running for this user; exiting")
            return 0
        try:
            if (newer := _newer_installed(self.paths, self.version)) is not None:
                log.info("service: Cremind Connect %s is installed at %s; this copy (%s) steps aside",
                         newer.get("version"), newer.get("exe"), self.version)
                return 0
            self._instance.write_info(version=self.version, exe=str(executable()), frozen=is_frozen())
            self._load_identity()
            threading.Thread(target=self._sync_font_assets, name="font assets", daemon=True).start()
            self._server = ipc.IpcServer(self.paths, self.handle)
            self._server.start()
            self._install_signal_handlers()
            log.info("service: Cremind Connect %s running (pid %d, data %s)", self.version, os.getpid(),
                     self.paths.data_dir)
            self.serve_forever()
            return 0
        finally:
            self.shutdown()
            self._instance.release()

    def _load_identity(self) -> None:
        from .installation import computer_name, load_or_create

        if self.installation_id is None:
            try:
                self.installation_id = load_or_create(self.paths).id
            except Exception:
                log.exception("service: the installation identity is unusable")
        if self.computer is None:
            self.computer = computer_name()

    def _sync_font_assets(self) -> None:
        """Copy the bundle's verified font packs to ``<data>/assets`` (shared by workers of every version)."""
        from ..resources import FontAssetsError, bundled_asset_dirs, font_assets_in, install_font_assets

        for root in bundled_asset_dirs():
            for assets in font_assets_in(root):
                try:
                    installed = install_font_assets(assets, self.paths.assets_dir)
                    log.info("service: font pack %s available at %s", installed.pack_id, installed.root)
                except (FontAssetsError, OSError) as exc:
                    log.warning("service: font pack %s not installed: %s", assets.pack_id, exc)

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("service: scan failed")
            self._wake.wait(SCAN_INTERVAL_S)
            self._wake.clear()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def shutdown(self) -> None:
        """Stop IPC and every worker (idempotent)."""
        self._stop.set()
        if self._server is not None:
            self._server.stop()
            self._server = None
        with self._lock:
            workers = [w for w in self._workers.values() if w.process is not None]
            for worker in workers:
                worker.stopping = True
        _stop_processes([w.process for w in workers if w.process is not None], STOP_GRACE_S)
        with self._lock:
            for worker in workers:
                self._reaped(worker, stopped=True)
        if self._job is not None:
            self._job.close()
            self._job = None
        log.info("service: stopped")

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, _frame: Any) -> None:
            log.info("service: signal %d", signum)
            self.request_stop()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(sig, handler)
        if sys.platform == "win32":
            _install_console_handler(self.request_stop)

    # ------------------------------------------------------------------ scanning

    def tick(self) -> None:
        """One scan: ports, probes, worker directories, exits, starts."""
        ports = self.opts.list_ports()
        to_probe = self._update_ports(ports)
        for state in to_probe:
            self._probe_one(state.info)
        self._sync_workers()
        self._reap()
        self._start_due()
        self._reap_windows()

    def _update_ports(self, ports: Sequence[PortInfo]) -> list[PortState]:
        now = self.opts.clock()
        with self._lock:
            seen = {p.device: p for p in ports}
            for device in list(self._ports):
                if device not in seen:
                    log.info("service: port %s disappeared", device)
                    del self._ports[device]
            for info in ports:
                state = self._ports.get(info.device)
                if state is not None and state.info.key != info.key:
                    state = None  # another device on the same port name
                if state is None:
                    holder = next((w.worker_id for w in self._workers.values()
                                   if w.process is not None and w.port == info.device), None)
                    self._ports[info.device] = PortState(info, held_by=holder)
                    log.info("service: port %s appeared (%s %s)", info.device, info.usb_id or "-", info.role_hint)
                else:
                    state.info = info
            due = []
            for state in self._ports.values():
                if state.held_by is not None:
                    continue
                if state.probe is None or state.stale:
                    due.append(state)
                    continue
                if state.probe.identity is not None:
                    continue
                retry = RETRY_AFTER_S.get(state.probe.reason, 30.0)
                if retry is not None and state.probed_at is not None and now - state.probed_at >= retry:
                    due.append(state)
            return due

    def _port_lock(self, device: str) -> threading.Lock:
        with self._lock:
            return self._port_locks.setdefault(device, threading.Lock())

    def _probe_one(self, info: PortInfo, *, wait: bool = False) -> ProbeResult | None:
        """Probe one port unless a worker holds it or another probe runs on it."""
        lock = self._port_lock(info.device)
        if not lock.acquire(blocking=wait, timeout=10.0 if wait else -1):
            return None
        try:
            with self._lock:
                state = self._ports.get(info.device)
                if state is not None and state.held_by is not None:
                    return None
            result = self.opts.probe(info.device)
            with self._lock:
                state = self._ports.get(info.device)
                if state is not None and state.info.key == info.key and state.held_by is None:
                    state.probe = result
                    state.probed_at = self.opts.clock()
                    state.stale = False
                    if result.identity is not None:
                        if state.fresh and result.identity.is_gateway:
                            self._gateway_appeared(result.identity.device_id_hex)  # just plugged in: no back-off
                        state.fresh = False
            if result.identity is not None:
                log.info("service: %s is a %s (%s…, %s, gen %d)", info.device, result.identity.role_name,
                         result.identity.device_id_hex[:8], result.identity.owner_state_name, result.identity.gen)
            else:
                log.info("service: %s: %s (%s)", info.device, result.reason, result.detail)
            return result
        finally:
            lock.release()

    def _gateway_appeared(self, device_id: str) -> None:
        now = self.opts.clock()
        for worker in self._workers.values():
            if worker.spec is not None and worker.spec.gateway_device_id == device_id and worker.process is None:
                worker.next_start = min(worker.next_start, now)

    # ------------------------------------------------------------------ workers

    def _sync_workers(self) -> None:
        found = list_workers(self.paths.workers_dir)
        stop: list[WorkerState] = []
        with self._lock:
            names = set()
            for directory, spec in found:
                names.add(directory.name)
                self._apply_spec(directory, spec, stop)
            for name in [n for n in self._workers if n not in names]:
                worker = self._workers[name]
                if worker.process is not None:
                    worker.stopping = True
                    stop.append(worker)
                else:
                    del self._workers[name]
        self._stop_workers(stop)

    def _apply_spec(self, directory: Path, spec: WorkerSpec | WorkerDirError, stop: list[WorkerState]) -> None:
        worker = self._workers.get(directory.name)
        if worker is None:
            worker = self._workers[directory.name] = WorkerState(directory.name, directory, None)
        if isinstance(spec, WorkerDirError):
            worker.error = str(spec)
            if worker.process is None:
                worker.spec = None
            return
        previous, worker.spec, worker.error = worker.spec, spec, None
        changed_gateway = previous is not None and previous.gateway_device_id != spec.gateway_device_id
        if worker.process is not None and not worker.stopping and (not spec.enabled or changed_gateway):
            worker.stopping = True
            stop.append(worker)

    def _stop_workers(self, workers: list[WorkerState]) -> None:
        if not workers:
            return
        _stop_processes([w.process for w in workers if w.process is not None], STOP_GRACE_S)
        with self._lock:
            for worker in workers:
                self._reaped(worker, stopped=True)
                if not worker.directory.exists():
                    self._workers.pop(worker.worker_id, None)

    def _reap(self) -> None:
        with self._lock:
            for worker in self._workers.values():
                if worker.process is not None and not worker.stopping and worker.process.poll() is not None:
                    self._reaped(worker, stopped=False)

    def _reaped(self, worker: WorkerState, *, stopped: bool) -> None:
        """Book-keeping after a worker process ended (caller holds the lock)."""
        process = worker.process
        if process is None:
            worker.stopping = False
            return
        code = process.poll()
        now = self.opts.clock()
        ran = now - (worker.started_at or now)
        worker.process = None
        worker.stopping = False
        worker.last_exit_code = code
        if worker.port is not None:
            port = self._ports.get(worker.port)
            if port is not None and port.held_by == worker.worker_id:
                port.held_by = None
                port.stale = True  # look at the port afresh before using it again: the device may have changed
        worker.port = None
        if stopped:
            worker.next_start = now
            return
        if ran >= HEALTHY_RUN_S:
            worker.failures = 0
        worker.failures += 1
        delay = min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2 ** (worker.failures - 1))
        worker.next_start = now + delay
        worker.last_error = f"exited with code {code} after {ran:.0f} s"
        log.warning("service: worker %s exited with code %s after %.0f s; restarting in %.0f s", worker.worker_id,
                    code, ran, delay)

    def _start_due(self) -> None:
        now = self.opts.clock()
        with self._lock:
            busy_gateways = {w.spec.gateway_device_id for w in self._workers.values()
                             if w.process is not None and w.spec is not None}
            for worker in sorted(self._workers.values(), key=lambda w: w.worker_id):
                spec = worker.spec
                worker.conflict = False
                if spec is None or not spec.enabled or worker.process is not None or worker.stopping:
                    continue
                if spec.gateway_device_id in busy_gateways:
                    worker.conflict = True  # another worker directory drives this gateway
                    continue
                if worker.next_start > now:
                    continue
                port = next((p for p in self._ports.values()
                             if p.held_by is None and p.gateway_id == spec.gateway_device_id), None)
                if port is None:
                    continue
                if not self._port_lock(port.info.device).acquire(blocking=False):
                    continue  # a probe is running on it right now: next scan
                try:
                    self._start_worker(worker, port)
                finally:
                    self._port_locks[port.info.device].release()
                if worker.process is not None:
                    busy_gateways.add(spec.gateway_device_id)

    def _worker_command(self, directory: Path, device: str) -> list[str]:
        if self.opts.worker_command is not None:
            return self.opts.worker_command(directory, device)
        return self_command("worker", "--dir", str(directory), "--port", device)

    def _start_worker(self, worker: WorkerState, port: PortState) -> None:
        argv = self._worker_command(worker.directory, port.info.device)
        env = {**os.environ, SUPERVISED_ENV: "1", "CREMIND_CONNECT_WORKER_ID": worker.worker_id,
               "PYTHONUNBUFFERED": "1"}
        log_path = worker.directory / "logs" / "worker-console.log"
        try:
            process = (self.opts.spawn or self._spawn)(argv, worker.directory, env, log_path)
        except OSError as exc:
            worker.last_error = f"could not start: {exc}"
            worker.failures += 1
            worker.next_start = self.opts.clock() + min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2 ** (worker.failures - 1))
            log.error("service: cannot start worker %s: %s", worker.worker_id, exc)
            return
        worker.process = process
        worker.port = port.info.device
        worker.started_at = self.opts.clock()
        worker.restarts += 1 if worker.last_exit_code is not None else 0
        port.held_by = worker.worker_id
        log.info("service: started worker %s on %s (pid %s)", worker.worker_id, port.info.device, process.pid)

    def _spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> Process:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as out:
            kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": out, "stderr": subprocess.STDOUT,
                                      "cwd": str(cwd), "env": env, "close_fds": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = no_window_flags()
            process = subprocess.Popen(argv, **kwargs)
        if self._job is not None:
            self._job.assign(process)
        return process

    # ------------------------------------------------------------------ windows

    def _open_window(self, url: str, env: dict[str, str]) -> Process:
        if self.opts.open_window is not None:
            return self.opts.open_window(url, env)
        child_env = {**os.environ, **env, LINK_ENV: url}
        return spawn_detached(self_command("window"), env=child_env, cwd=self.paths.data_dir,
                              log_path=self.paths.logs_dir / "window-console.log")

    def _reap_windows(self) -> None:
        with self._lock:
            self._windows = [w for w in self._windows if w.process.poll() is None]

    # ------------------------------------------------------------------ IPC

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        handler = {
            "ping": self._op_ping, "status": self._op_status, "list_gateways": self._op_list_gateways,
            "open_link": self._op_open_link, "add_worker": self._op_add_worker,
            "remove_worker": self._op_remove_worker, "stop": self._op_stop,
        }.get(str(op))
        if handler is None:
            return ipc.error("unknown_op", f"unknown op {op!r}")
        return handler(request)

    def _op_ping(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "version": self.version, "pid": os.getpid(), "installation_id": self.installation_id}

    def status(self) -> dict[str, Any]:
        now = self.opts.clock()
        with self._lock:
            return {"ok": True, "version": self.version, "installation_id": self.installation_id,
                    "computer": self.computer, "pid": os.getpid(), "started_at": self.started_at,
                    "workers": [w.as_json(now) for w in sorted(self._workers.values(), key=lambda w: w.worker_id)],
                    "ports": [p.as_json() for p in sorted(self._ports.values(), key=lambda p: p.info.device)]}

    def _op_status(self, _request: dict[str, Any]) -> dict[str, Any]:
        return self.status()

    def list_gateways(self) -> list[dict[str, Any]]:
        """Probe every free candidate port now; held ports are reported, never opened."""
        ports = self.opts.list_ports()
        self._update_ports(ports)
        with self._lock:
            free = [s.info for s in self._ports.values() if s.held_by is None]
        if free:
            with ThreadPoolExecutor(max_workers=max(1, min(self.opts.probe_workers, len(free)))) as pool:
                list(pool.map(lambda info: self._probe_one(info, wait=True), free))
        with self._lock:
            out = []
            for state in sorted(self._ports.values(), key=lambda p: p.info.device):
                item = state.as_json(include_challenge=state.held_by is None)
                item["in_use"] = state.held_by is not None
                out.append(item)
            return out

    def _op_list_gateways(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "ports": self.list_gateways()}

    def _op_open_link(self, request: dict[str, Any]) -> dict[str, Any]:
        url = request.get("url")
        try:
            link = parse_setup_link(url if isinstance(url, str) else "")
        except LinkError as exc:
            log.warning("service: refused a link (%s): %s", exc.code, exc)
            return ipc.error("invalid_link", str(exc), code=exc.code)
        env_in = request.get("env") if isinstance(request.get("env"), dict) else {}
        env = {k: str(v) for k, v in env_in.items() if k in WINDOW_ENV_KEYS and isinstance(v, str)}
        with self._lock:
            for window in self._windows:
                if window.session == link.session and window.process.poll() is None:
                    return {"ok": True, "already_open": True, "pid": window.process.pid}
            try:
                process = self._open_window(link.to_url(), env)
            except OSError as exc:
                log.error("service: cannot open the setup window: %s", exc)
                return ipc.error("window_failed", f"could not open the setup window: {exc}")
            self._windows.append(_Window(process, link.session))
        log.info("service: setup window for %s (pid %s)", redact(link.to_url()), process.pid)
        return {"ok": True, "already_open": False, "pid": process.pid}

    def _op_add_worker(self, request: dict[str, Any]) -> dict[str, Any]:
        worker_id = request.get("worker_id")
        try:
            directory = self.paths.worker_dir(str(worker_id))
            spec = load_worker(directory)
        except (ValueError, WorkerDirError) as exc:
            return ipc.error("invalid_worker", str(exc))
        stop: list[WorkerState] = []
        with self._lock:
            self._apply_spec(directory, spec, stop)
            worker = self._workers[directory.name]
            worker.next_start = min(worker.next_start, self.opts.clock())
            worker.failures = 0
        self._stop_workers(stop)
        self._wake.set()
        with self._lock:
            return {"ok": True, "worker": worker.as_json(self.opts.clock())}

    def _op_remove_worker(self, request: dict[str, Any]) -> dict[str, Any]:
        worker_id = request.get("worker_id")
        delete = request.get("delete") is True
        try:
            directory = self.paths.worker_dir(str(worker_id))
        except ValueError as exc:
            return ipc.error("invalid_worker", str(exc))
        with self._lock:
            worker = self._workers.get(directory.name)
            if worker is not None and worker.process is not None:
                worker.stopping = True
        if worker is not None and worker.process is not None:
            self._stop_workers([worker])
        with self._lock:
            if delete:
                self._workers.pop(directory.name, None)
        if delete:
            _remove_tree(directory)
        elif directory.is_dir():
            try:
                spec = load_worker(directory)
            except WorkerDirError as exc:
                return ipc.error("invalid_worker", str(exc))
            write_worker(directory, replace(spec, enabled=False))
            with self._lock:
                if (state := self._workers.get(directory.name)) is not None:
                    state.spec = replace(spec, enabled=False)
        elif worker is None:
            return ipc.error("not_found", f"no worker {worker_id}")
        return {"ok": True, "deleted": delete}

    def _op_stop(self, _request: dict[str, Any]) -> dict[str, Any]:
        log.info("service: stop requested over IPC")
        threading.Timer(0.1, self.request_stop).start()  # answer first
        return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_processes(processes: Sequence[Process], grace: float) -> None:
    """Ask every process to stop (close stdin; SIGTERM on POSIX), then terminate those still running."""
    for process in processes:
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        if sys.platform != "win32":
            with contextlib.suppress(Exception):
                process.terminate()
    deadline = time.monotonic() + grace
    for process in processes:
        try:
            process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(5.0)
        except Exception:  # noqa: BLE001 - a fake or an already reaped process
            pass


def _remove_tree(path: Path, attempts: int = 20) -> None:
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)  # Windows: a just-exited worker's files may still be closing


def _newer_installed(paths: ConnectPaths, version: str) -> dict[str, Any] | None:
    """The active installation when it is a *newer* copy elsewhere (only a frozen bundle steps aside)."""
    if not is_frozen():
        return None
    from .install import read_record

    record = read_record(paths)
    if record is None or not isinstance(record.get("version"), str) or not is_newer(record["version"], version):
        return None
    exe = Path(str(record.get("exe", "")))
    if not exe.is_file():
        return None
    with contextlib.suppress(OSError):
        if exe.resolve() == executable():
            return None
    return record


def _install_console_handler(callback: Callable[[], None]) -> None:
    """Windows console close/logoff/shutdown events (only delivered when a console is attached)."""
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_event(event: int) -> bool:
            if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                callback()
                time.sleep(STOP_GRACE_S / 2)  # give the main loop time to stop the workers
                return True
            return False

        _install_console_handler.keep = handler_type(on_event)  # type: ignore[attr-defined]
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_install_console_handler.keep, True)  # type: ignore[attr-defined]


class _KillOnCloseJob:
    """A Windows job object whose processes die when the supervisor does."""

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def create(cls) -> _KillOnCloseJob | None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                         wintypes.DWORD]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes",
                                                               "o_bytes")]

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return None
            return cls(int(handle))
        except Exception:  # noqa: BLE001 - robustness aid only
            log.debug("service: no job object", exc_info=True)
            return None

    def assign(self, process: Any) -> None:
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.AssignProcessToJobObject(ctypes.c_void_p(self.handle),  # type: ignore[attr-defined]
                                                            ctypes.c_void_p(int(process._handle)))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))  # type: ignore[attr-defined]


def run_service(paths: ConnectPaths) -> int:
    """Entry point of ``cremind-connect service``."""
    return Supervisor(paths).run()


__all__ = ["BACKOFF_INITIAL_S", "BACKOFF_MAX_S", "LINK_ENV", "SCAN_INTERVAL_S", "SUPERVISED_ENV", "PortState",
           "Supervisor", "SupervisorOptions", "WorkerState", "run_service"]
