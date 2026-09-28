"""The supervisor: USB watcher, workers with back-off, IPC ops (fake ports, probes, processes and clock)."""

from __future__ import annotations

import itertools
import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connect import ipc
from cremind_tag.connect.paths import ConnectPaths
from cremind_tag.connect.probe import BUSY, NO_ANSWER, V1_FIRMWARE, GatewayIdentity, ProbeResult
from cremind_tag.connect.service import BACKOFF_MAX_S, SUPERVISED_ENV, Supervisor, SupervisorOptions
from cremind_tag.connect.usb import PortInfo
from cremind_tag.connect.workerdir import WorkerSpec, load_worker, write_worker
from cremind_tag.protocol.ids import NodeRole
from cremind_tag.secure.device import DeviceKeys

LINK = ("cremind-connect://setup?v=1&server=https%3A%2F%2Fcremind.example.org&"
        "session=3f2b8c1e-5a4d-4e6f-9a0b-1c2d3e4f5a6b&token=tok_Ab-cd_ef0123456789XYZ")


def identity(role: NodeRole = NodeRole.GATEWAY) -> GatewayIdentity:
    keys = DeviceKeys.generate(role)
    return GatewayIdentity(keys.device_id, keys.ik_pub, int(role), 2, "0.2.0", "test", 1, 0, 0, None, os.urandom(16))


class FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    _pids = itertools.count(4000)

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str], log_path: Path,
                 obey_stop: bool = True) -> None:
        self.argv, self.cwd, self.env, self.log_path = argv, cwd, env, log_path
        self.pid = next(self._pids)
        self.stdin = FakeStdin()
        self.returncode: int | None = None
        self.obey_stop = obey_stop
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None and self.stdin.closed and self.obey_stop:
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        pass  # POSIX SIGTERM: the fake only reacts to its stdin closing

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def exit(self, code: int) -> None:
        self.returncode = code


class Rig:
    """A supervisor with everything around it faked."""

    def __init__(self, paths: ConnectPaths) -> None:
        self.paths = paths
        self.now = 1000.0
        self.ports: list[PortInfo] = []
        self.results: dict[str, ProbeResult] = {}
        self.probed: list[str] = []
        self.spawned: list[FakeProcess] = []
        self.windows: list[tuple[str, dict[str, str]]] = []
        self.obey_stop = True
        self.sup = Supervisor(paths, SupervisorOptions(
            list_ports=lambda: list(self.ports), probe=self.probe, spawn=self.spawn, open_window=self.open_window,
            clock=lambda: self.now, version="9.9.9", computer="test-pc", installation_id="ab" * 16))

    def probe(self, device: str) -> ProbeResult:
        self.probed.append(device)
        return self.results.get(device, ProbeResult(device, reason=NO_ANSWER))

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> FakeProcess:
        process = FakeProcess(argv, cwd, env, log_path, self.obey_stop)
        self.spawned.append(process)
        return process

    def open_window(self, url: str, env: dict[str, str]) -> FakeProcess:
        self.windows.append((url, env))
        return FakeProcess(["window"], self.paths.data_dir, env, self.paths.logs_dir / "w.log")

    def plug(self, device: str, ident: GatewayIdentity | None = None, *, serial: str = "SN1",
             reason: str | None = None) -> None:
        self.ports.append(PortInfo(device, 0x1209, 0x0002, serial, "Cremind Tag gateway", "gateway"))
        self.results[device] = ProbeResult(device, identity=ident, reason=None if ident else reason or NO_ANSWER)

    def unplug(self, device: str) -> None:
        self.ports = [p for p in self.ports if p.device != device]

    def worker(self, worker_id: str, ident: GatewayIdentity, **overrides: Any) -> Path:
        directory = self.paths.worker_dir(worker_id)
        spec = WorkerSpec(worker_id, "https://cremind.example.org", "Anna", f"comp-{worker_id}",
                          ident.device_id_hex, **overrides)
        write_worker(directory, spec)
        return directory

    def tick(self, advance: float = 0.0) -> None:
        self.now += advance
        self.sup.tick()

    def status(self) -> dict[str, Any]:
        return self.sup.handle({"op": "status"})

    def worker_state(self, worker_id: str) -> dict[str, Any]:
        return next(w for w in self.status()["workers"] if w["worker_id"] == worker_id)


@pytest.fixture
def rig(paths: ConnectPaths) -> Rig:
    return Rig(paths)


def test_a_worker_starts_when_its_gateway_is_attached(rig: Rig) -> None:
    gw = identity()
    directory = rig.worker("w1", gw)
    rig.tick()
    assert rig.spawned == [] and rig.worker_state("w1")["state"] == "waiting_for_gateway"
    rig.plug("COM7", gw)
    rig.tick()
    (process,) = rig.spawned
    assert process.argv[-5:] == ["worker", "--dir", str(directory), "--port", "COM7"]
    assert process.env[SUPERVISED_ENV] == "1" and process.cwd == directory
    assert process.log_path == directory / "logs" / "worker-console.log"
    state = rig.worker_state("w1")
    assert state["state"] == "running" and state["port"] == "COM7" and state["pid"] == process.pid
    assert state["server"] == "https://cremind.example.org" and state["gateway_device_id"] == gw.device_id_hex
    (port,) = rig.status()["ports"]
    assert port["held_by"] == "w1" and port["identity"]["device_id"] == gw.device_id_hex
    assert "challenge" not in port["identity"]
    probes = len(rig.probed)
    rig.tick(10)
    rig.tick(10)
    assert len(rig.probed) == probes and len(rig.spawned) == 1  # a held port is never probed or reused


def test_exponential_back_off_capped_at_a_minute(rig: Rig) -> None:
    gw = identity()
    rig.worker("w1", gw)
    rig.plug("COM7", gw)
    rig.tick()
    delays = []
    for _ in range(9):
        rig.spawned[-1].exit(1)
        rig.tick()  # reaped: back-off starts
        before = len(rig.spawned)
        assert rig.worker_state("w1")["state"] == "backoff"
        waited = 0.0
        while len(rig.spawned) == before:
            rig.tick(0.5)
            waited += 0.5
            assert waited <= BACKOFF_MAX_S + 1
        delays.append(waited)
    assert delays[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0] and delays[7:] == [60.0, 60.0]
    assert rig.worker_state("w1")["restarts"] == 9
    rig.tick(61)  # a minute of healthy running resets the back-off
    rig.spawned[-1].exit(1)
    rig.tick()
    rig.tick(1.0)
    assert len(rig.spawned) == 11


def test_a_port_is_probed_again_after_its_worker_exits(rig: Rig) -> None:
    gw = identity()
    rig.worker("w1", gw)
    rig.plug("COM7", gw)
    rig.tick()
    probes = rig.probed.count("COM7")
    rig.spawned[-1].exit(1)
    rig.tick()
    (port,) = rig.status()["ports"]
    assert port["held_by"] is None and port["identity"]["device_id"] == gw.device_id_hex  # still shown
    rig.tick(1)
    assert rig.probed.count("COM7") == probes + 1 and len(rig.spawned) == 2


def test_never_two_workers_for_one_gateway(rig: Rig) -> None:
    gw = identity()
    rig.worker("a", gw)
    rig.worker("b", gw)
    rig.plug("COM7", gw)
    rig.tick()
    rig.tick(1)
    assert len(rig.spawned) == 1
    assert rig.worker_state("a")["state"] == "running" and rig.worker_state("b")["state"] == "conflict"


def test_two_gateways_two_workers(rig: Rig) -> None:
    one, two = identity(), identity()
    rig.worker("a", one)
    rig.worker("b", two)
    rig.plug("COM7", one, serial="S1")
    rig.plug("COM8", two, serial="S2")
    rig.tick()
    assert sorted(p.argv[-1] for p in rig.spawned) == ["COM7", "COM8"]


def test_replugged_gateway_restarts_at_once_on_its_new_port(rig: Rig) -> None:
    gw = identity()
    rig.worker("w1", gw)
    rig.plug("COM7", gw)
    rig.tick()
    for _ in range(5):  # build up a back-off
        rig.spawned[-1].exit(1)
        rig.tick()
        rig.tick(40)
    rig.unplug("COM7")
    rig.spawned[-1].exit(1)
    rig.tick()
    assert rig.worker_state("w1")["state"] == "backoff" and rig.worker_state("w1")["retry_in_s"] > 1
    rig.plug("COM11", gw, serial="SN1")
    rig.tick()
    assert rig.spawned[-1].argv[-1] == "COM11" and rig.spawned[-1].poll() is None


def test_bridges_and_v1_devices_never_get_workers(rig: Rig) -> None:
    gw = identity()
    rig.worker("w1", gw)
    rig.plug("COM9", identity(NodeRole.BRIDGE), serial="B")
    rig.plug("COM5", None, serial="V1", reason=V1_FIRMWARE)
    rig.tick()
    rig.tick(60)
    assert rig.spawned == [] and rig.probed.count("COM5") == 1  # v1 firmware is not asked again


def test_busy_ports_are_retried(rig: Rig) -> None:
    gw = identity()
    rig.worker("w1", gw)
    rig.plug("COM7", None, reason=BUSY)
    rig.tick()
    rig.results["COM7"] = ProbeResult("COM7", identity=gw)
    rig.tick(1)
    assert rig.spawned == []
    rig.tick(5)
    assert len(rig.spawned) == 1 and rig.probed.count("COM7") == 2


def test_disabling_in_worker_json_stops_the_worker(rig: Rig) -> None:
    gw = identity()
    directory = rig.worker("w1", gw)
    rig.plug("COM7", gw)
    rig.tick()
    write_worker(directory, replace(load_worker(directory), enabled=False))
    rig.tick()
    process = rig.spawned[0]
    assert process.stdin.closed and process.returncode == 0
    assert rig.worker_state("w1")["state"] == "disabled"
    rig.tick(100)
    assert len(rig.spawned) == 1


def test_a_worker_that_ignores_the_stop_request_is_killed(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cremind_tag.connect.service.STOP_GRACE_S", 0.2)
    gw = identity()
    rig.worker("w1", gw)
    rig.obey_stop = False
    rig.plug("COM7", gw)
    rig.tick()
    answer = rig.sup.handle({"op": "remove_worker", "worker_id": "w1"})
    assert answer == {"ok": True, "deleted": False}
    assert rig.spawned[0].killed


def test_remove_worker_disables_or_deletes(rig: Rig) -> None:
    gw = identity()
    directory = rig.worker("w1", gw)
    rig.plug("COM7", gw)
    rig.tick()
    assert rig.sup.handle({"op": "remove_worker", "worker_id": "w1"}) == {"ok": True, "deleted": False}
    assert load_worker(directory).enabled is False and rig.spawned[0].stdin.closed
    (port,) = rig.status()["ports"]
    assert port["held_by"] is None
    rig.tick(10)
    assert len(rig.spawned) == 1
    assert rig.sup.handle({"op": "remove_worker", "worker_id": "w1", "delete": True})["deleted"] is True
    assert not directory.exists() and all(w["worker_id"] != "w1" for w in rig.status()["workers"])
    assert rig.sup.handle({"op": "remove_worker", "worker_id": "w1"})["error"] == "not_found"
    assert rig.sup.handle({"op": "remove_worker", "worker_id": "../x"})["error"] == "invalid_worker"


def test_add_worker_starts_it_without_waiting(rig: Rig) -> None:
    gw = identity()
    rig.plug("COM7", gw)
    rig.tick()
    assert rig.sup.handle({"op": "add_worker", "worker_id": "nope"})["error"] == "invalid_worker"
    rig.worker("w1", gw)
    answer = rig.sup.handle({"op": "add_worker", "worker_id": "w1"})
    assert answer["ok"] and answer["worker"]["worker_id"] == "w1"
    rig.tick()
    assert len(rig.spawned) == 1


def test_an_invalid_worker_directory_is_reported(rig: Rig) -> None:
    (rig.paths.workers_dir / "broken").mkdir(parents=True)
    rig.tick()
    state = rig.worker_state("broken")
    assert state["state"] == "invalid" and "worker.json" in state["last_error"]


def test_list_gateways_probes_free_ports_now_and_reports_held_ones(rig: Rig) -> None:
    held, free = identity(), identity()
    rig.worker("w1", held)
    rig.plug("COM7", held, serial="A")
    rig.tick()
    rig.plug("COM8", free, serial="B")
    before = rig.probed.count("COM7")
    answer = rig.sup.handle({"op": "list_gateways"})
    ports = {p["device"]: p for p in answer["ports"]}
    assert ports["COM7"]["in_use"] and ports["COM7"]["held_by"] == "w1"
    assert "challenge" not in ports["COM7"]["identity"] and rig.probed.count("COM7") == before
    assert not ports["COM8"]["in_use"] and ports["COM8"]["identity"]["challenge"] == free.challenge.hex()
    assert ports["COM8"]["identity"]["role"] == "gateway" and ports["COM8"]["identity"]["ik"] == free.ik.hex()


def test_open_link_starts_one_window_per_session(rig: Rig) -> None:
    answer = rig.sup.handle({"op": "open_link", "url": LINK, "env": {"DISPLAY": ":0", "PATH": "/evil", "X": 1}})
    assert answer["ok"] and not answer["already_open"]
    ((url, env),) = rig.windows
    assert url.startswith("cremind-connect://setup?") and "tok_Ab-cd_ef0123456789XYZ" in url
    assert env == {"DISPLAY": ":0"}
    again = rig.sup.handle({"op": "open_link", "url": LINK})
    assert again["already_open"] and len(rig.windows) == 1
    bad = rig.sup.handle({"op": "open_link", "url": LINK.replace("v=1", "v=9")})
    assert bad["ok"] is False and bad["error"] == "invalid_link" and bad["code"] == "unsupported_version"


def test_unknown_ops_and_ping(rig: Rig) -> None:
    assert rig.sup.handle({"op": "fly"})["error"] == "unknown_op"
    ping = rig.sup.handle({"op": "ping"})
    assert ping["ok"] and ping["version"] == "9.9.9" and ping["pid"] == os.getpid()
    status = rig.status()
    assert {"version", "installation_id", "computer", "workers", "ports", "pid", "started_at"} <= set(status)


def test_shutdown_stops_every_worker(rig: Rig) -> None:
    one, two = identity(), identity()
    rig.worker("a", one)
    rig.worker("b", two)
    rig.plug("COM7", one, serial="S1")
    rig.plug("COM8", two, serial="S2")
    rig.tick()
    rig.sup.shutdown()
    assert all(p.stdin.closed and p.returncode == 0 for p in rig.spawned)
    assert all(w["state"] != "running" for w in rig.status()["workers"])


def test_ipc_end_to_end(rig: Rig) -> None:
    server = ipc.IpcServer(rig.paths, rig.sup.handle)
    server.start()
    try:
        status = ipc.request(rig.paths, "status")
        assert status["ok"] and status["version"] == "9.9.9" and status["computer"] == "test-pc"
        assert ipc.request(rig.paths, "stop") == {"ok": True}
        deadline = time.monotonic() + 5
        while not rig.sup.stopping and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rig.sup.stopping
    finally:
        server.stop()


def test_the_service_runs_once_per_user_and_stops_over_ipc(paths: ConnectPaths) -> None:
    options = SupervisorOptions(list_ports=lambda: [], version="9.9.9")
    service = Supervisor(paths, options)
    exit_codes: list[int] = []
    thread = threading.Thread(target=lambda: exit_codes.append(service.run()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while ipc.ping(paths) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    ping = ipc.ping(paths)
    assert ping is not None and ping["version"] == "9.9.9" and len(ping["installation_id"]) == 32
    assert paths.installation_key.is_file() and paths.service_info.is_file()
    assert Supervisor(paths, options).run() == 0  # a second service steps aside at once
    assert ipc.request(paths, "stop")["ok"]
    thread.join(30)
    assert exit_codes == [0] and ipc.ping(paths) is None and not paths.service_info.exists()
