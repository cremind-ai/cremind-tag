#!/usr/bin/env python3
"""The first vertical slice, end to end: a THROWAWAY Cremind + the simulator + the companion daemon.

    cd cremind-tag
    uv run --project companion python tools/e2e_slice.py [--cremind-repo PATH] [--port 1181] [--keep]

What it does (every step timed; a JSON report is printed and saved in the scratch dir):

1. boots Cremind from its repository with a fresh ``CREMIND_SYSTEM_DIR`` under
   ``build/e2e-slice/<stamp>/`` — never a copy of ``~/.cremind`` — as
   ``CREMIND_SYSTEM_DIR=… CREMIND_UI_PORT=0 PORT=<port> HOST=127.0.0.1
   APP_URL=http://localhost:<port> .venv/Scripts/cremind.exe serve`` (it logs to
   that repository's ``logs/app.log``), and completes first setup through
   ``POST /api/config/setup`` (profile ``admin``, a dummy OpenAI key: no LLM is
   ever reached);
2. registers a companion (``POST /api/tags/hardware/companions``) and starts
   the simulator (one gateway, one bridge, one unassigned tag, the dev font
   pack ``fonts/out/dev/fontpack.ctfp``) and the daemon with the hardware
   credential: ``POST inventory`` makes the devices appear in Cremind;
3. claims the tag for ``admin`` (``POST /api/tags/hardware/tags/{id}/claim``);
   the daemon executes ``assign_tag`` (K_epoch -> ``ASSIGN_TAG``) and
   ``clear_tag`` (``TAG_COMMAND CLEAR`` -> ``EVT_RESULT OK``);
4. enables Tags for the profile, creates a content credential and restarts the
   daemon with both credentials;
5. produces durable Cremind events without an LLM: a pinned note
   (``POST /api/tags/devices/{id}/display``) and an event run (a Calendar &
   Schedule event that fires a few seconds later; its agent fails on the dummy
   key, which journals ``run.failed``); both must reach stage ``displayed`` with
   a revision and a digest, and the displayed preview must be stored;
6. stops everything and removes the scratch directory (``--keep`` keeps it).

Exit code 0 when the pinned note was displayed (the event-run path is reported
but best effort).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CREMIND = Path(os.environ.get("CREMIND_REPO", r"C:\Users\lyntc\DATA\Personal\Cremind\cremind"))
DEV_PACK = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp"
FONT_CACHE = REPO / "fonts" / "cache"
PROTOCOL_HEADER = {"X-Cremind-Client-Protocol": "2"}
STAGES = ("queued", "companion_accepted", "gateway_received", "bridge_received", "transferring", "refreshing",
          "displayed")


class SliceError(RuntimeError):
    pass


class Clock:
    """Named step timings (seconds since the start)."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> float:
        self.marks[name] = round(time.monotonic() - self.start, 3)
        print(f"[{self.marks[name]:7.2f}s] {name}", flush=True)
        return self.marks[name]


class CremindProcess:
    """A throwaway Cremind (fresh system dir, loopback only)."""

    def __init__(self, repo: Path, scratch: Path, port: int) -> None:
        self.repo = repo
        self.scratch = scratch
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.output = scratch / "cremind-serve.out"

    def start(self) -> None:
        exe = self.repo / ".venv" / "Scripts" / "cremind.exe"
        if not exe.is_file():
            exe = self.repo / ".venv" / "bin" / "cremind"
        if not exe.is_file():
            raise SliceError(f"no Cremind executable in {self.repo}/.venv")
        sysdir = self.scratch / "sys"
        sysdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "CREMIND_SYSTEM_DIR": str(sysdir), "CREMIND_UI_PORT": "0", "PORT": str(self.port),
               "HOST": "127.0.0.1", "APP_URL": f"http://localhost:{self.port}"}
        env.pop("VIRTUAL_ENV", None)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            httpx.get(f"{self.url}/health", timeout=1.0)
        except httpx.HTTPError:
            pass  # nothing listens there: good
        else:
            raise SliceError(f"something already listens on port {self.port}; pick another with --port")
        self.proc = subprocess.Popen([str(exe), "serve"], cwd=self.repo, env=env, stdout=self.output.open("wb"),
                                     stderr=subprocess.STDOUT, creationflags=flags)

    def wait_ready(self, timeout: float = 240.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise SliceError(f"cremind serve exited with {self.proc.returncode}; see {self.output}")
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{self.url}/health", timeout=2.0).status_code < 500:
                    return
            time.sleep(0.5)
        raise SliceError(f"Cremind did not answer /health within {timeout:.0f}s; see {self.output}")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            self.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(20)


class Api:
    """Cremind's REST API as the admin profile."""

    def __init__(self, url: str) -> None:
        self.http = httpx.Client(base_url=url, timeout=httpx.Timeout(180.0, connect=10.0), headers=PROTOCOL_HEADER)

    def login(self, token: str) -> None:
        self.http.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 202), **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        if response.status_code not in ok:
            raise SliceError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response.json() if response.headers.get("content-type", "").startswith("application/json") \
            else response

    def poll(self, fn: Any, what: str, timeout: float, interval: float = 0.5) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(interval)
        raise SliceError(f"timed out after {timeout:.0f}s waiting for {what}")


def register_simulator(sim: Any, data_dir: Path) -> Any:
    """The simulated gateway, bridge and tag in the companion's inventory; tag secrets in a file store."""
    from cremind_tag.cli._hardware import gateway_hw_id
    from cremind_tag.daemon import open_database
    from cremind_tag.protocol.ids import Board
    from cremind_tag.secrets import FileBackend, SecretStore
    from cremind_tag.store import BridgeRecord, GatewayRecord, TagRecord

    secrets = SecretStore(FileBackend(data_dir / "secrets.json"))
    url = sim.gateway_url
    with open_database(data_dir / "companion.sqlite3") as db:
        db.upsert_gateway(GatewayRecord(gateway_hw_id(url), port=url, boot_id=sim.gateway.boot_id, fw="0.1.0",
                                        build="sim", board=Board.NRF52840DK_GATEWAY))
        for bridge in sim.bridges:
            pack = bridge.fontpack_id
            db.upsert_bridge(BridgeRecord(bridge.uuid.hex(), addr=bridge.addr, name=bridge.name,
                                          configured=bridge.configured, fw="0.1.0", board=bridge.board,
                                          fontpack_id=pack.hex() if pack else None, flash_size=bridge.flash.size,
                                          gateway_hw_id=gateway_hw_id(url)))
        for index, tag in enumerate(sim.tags.values()):
            spec = tag.spec
            ref = secrets.set_tag_secret(tag.tag_id, spec.secret)
            db.insert_tag(TagRecord(tag.tag_id, spec.board, spec.panel, spec.width, spec.height, spec.planes,
                                    spec.plane_flags, ref, name=f"e2e-{index + 1}", fw="0.1.0"))
    return secrets


def stage_timings(delivery: dict[str, Any]) -> dict[str, Any]:
    """Seconds from ``queued`` to each stage (Cremind's ``stage_times``, epoch ms)."""
    times = delivery.get("stage_times") or {}
    queued = times.get("queued")
    out: dict[str, Any] = {}
    previous = queued
    for stage in STAGES[1:]:
        if stage in times and queued is not None:
            out[stage] = {"since_queued_s": round((times[stage] - queued) / 1000, 3),
                          "since_previous_s": round((times[stage] - previous) / 1000, 3) if previous else None}
            previous = times[stage]
    return out


async def run_slice(cremind_repo: Path, port: int, scratch: Path, *, time_scale: float = 10.0,
                    event_run: bool = True) -> dict[str, Any]:
    from cremind_tag.connector import parse_credential
    from cremind_tag.daemon import DaemonOptions, DaemonService
    from cremind_tag.daemon.settings import DaemonSettings
    from cremind_tag.fonts.fontset import FontSet
    from cremind_tag.sim import Simulator
    from cremind_tag.sim.harness import make_config

    clock = Clock()
    report: dict[str, Any] = {"cremind_repo": str(cremind_repo), "port": port, "scratch": str(scratch),
                              "time_scale": time_scale, "steps": clock.marks}
    server = CremindProcess(cremind_repo, scratch, port)
    api = Api(server.url)
    sim: Any = None
    svc: DaemonService | None = None
    task: asyncio.Task[None] | None = None

    async def start_daemon(credentials: list[str]) -> DaemonService:
        nonlocal svc, task
        if svc is not None:
            svc.stop()
            assert task is not None
            await task
        hardware = parse_credential(credentials[0])
        options = DaemonOptions(
            db_path=data_dir / "companion.sqlite3", data_dir=data_dir, cremind_url=server.url,
            hardware_credential=hardware, content_credentials=[parse_credential(c) for c in credentials[1:]],
            gateway_url=sim.gateway_url, fontpack=DEV_PACK, fonts=fonts, secrets=secrets,
            settings=DaemonSettings(active_poll_s=0.5, idle_poll_s=2.0, heartbeat_s=5.0, command_wait_s=10,
                                    scan_interval_s=0.5, retry_initial_s=1.0, retry_max_s=10.0,
                                    connector_retry_max_s=5.0))
        svc = DaemonService(options)
        task = asyncio.create_task(svc.run(), name="daemon")
        return svc

    async def wait(fn: Any, what: str, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await asyncio.to_thread(fn)
            if value:
                return value
            if task is not None and task.done():
                raise SliceError(f"the daemon stopped while waiting for {what}: {task.exception()!r}")
            await asyncio.sleep(0.5)
        raise SliceError(f"timed out after {timeout:.0f}s waiting for {what}")

    data_dir = scratch / "companion"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. a throwaway Cremind + first setup
        server.start()
        await asyncio.to_thread(server.wait_ready)
        clock.mark("cremind ready")
        setup = await asyncio.to_thread(api.call, "POST", "/api/config/setup", json={
            "profile": "admin", "server_config": {"db_provider": "sqlite"},
            "llm_config": {"default_provider": "openai", "model_group.high": "openai/gpt-4o-mini",
                           "openai.auth_method": "api_key", "openai.api_key": "sk-dummy"}})
        api.login(setup["token"])
        clock.mark("setup done")

        # 2. companion + simulator + daemon (hardware credential)
        registered = await asyncio.to_thread(api.call, "POST", "/api/tags/hardware/companions",
                                             json={"name": "e2e companion"})
        companion_id = registered["companion"]["id"]
        hardware_auth = registered["authorization"]
        fonts = await asyncio.to_thread(FontSet.load, DEV_PACK, FONT_CACHE)
        config = make_config(fontpack=DEV_PACK.read_bytes(), tags=1, bridges=1, seed=42, time_scale=time_scale,
                             assign=False)
        sim = Simulator(config)
        await sim.start()
        secrets = await asyncio.to_thread(register_simulator, sim, data_dir)
        tag_hw = f"{next(iter(sim.tags)):08X}"
        await start_daemon([hardware_auth])
        inventory = await wait(lambda: [d for d in api.call("GET", "/api/tags/hardware")["devices"]
                                        if d["kind"] == "tag" and d["hw_id"] == tag_hw], "the tag in Cremind", 60)
        device_id = inventory[0]["id"]
        clock.mark("inventory in Cremind")

        # 3. claim -> assign_tag + clear_tag
        claim = await asyncio.to_thread(api.call, "POST", f"/api/tags/hardware/tags/{device_id}/claim",
                                        json={"owner": "admin", "name": "E2E desk"})
        commands = {c["kind"]: c["id"] for c in claim["commands"]}

        def command_status(kind: str) -> str | None:
            status = api.call("GET", f"/api/tags/hardware/commands/{commands[kind]}")["command"]["status"]
            return status if status in ("succeeded", "failed", "expired", "cancelled") else None

        report["assign_tag"] = await wait(lambda: command_status("assign_tag"), "assign_tag", 90)
        clock.mark(f"assign_tag {report['assign_tag']}")
        report["clear_tag"] = await wait(lambda: command_status("clear_tag"), "clear_tag", 120)
        clock.mark(f"clear_tag {report['clear_tag']}")
        if report["assign_tag"] != "succeeded" or report["clear_tag"] != "succeeded":
            raise SliceError(f"claim commands: assign_tag {report['assign_tag']}, clear_tag {report['clear_tag']}")

        # 4. Tags on, a content credential, the daemon with both credentials
        await asyncio.to_thread(api.call, "PUT", "/api/tags/settings", json={"enabled": True})
        content = await asyncio.to_thread(api.call, "POST", "/api/tags/credentials",
                                          json={"companion_id": companion_id, "label": "e2e"})
        await start_daemon([hardware_auth, content["authorization"]])
        clock.mark("daemon restarted with the content credential")

        # 5a. a pinned note
        def display() -> Any:
            response = api.http.post(f"/api/tags/devices/{device_id}/display",
                                     json={"title": "Hello from the e2e slice",
                                           "body": "Composed by the companion, drawn by the bridge.", "ttl_s": 3600})
            if response.status_code == 409:  # clear_pending: the clear_tag result is still on its way
                return None
            if response.status_code != 201:
                raise SliceError(f"display -> {response.status_code}: {response.text[:300]}")
            return response.json()["delivery"]

        note = await wait(display, "display accepted", 30)
        clock.mark(f"pinned note queued (delivery {note['id']})")

        def delivery(delivery_id: int) -> dict[str, Any]:
            return api.call("GET", f"/api/tags/deliveries/{delivery_id}")["delivery"]

        shown = await wait(lambda: (d := delivery(note["id"]))["stage"] in ("displayed", "failed", "expired",
                                                                           "cancelled", "uncertain") and d,
                           "the pinned note displayed", 120)
        clock.mark(f"pinned note {shown['stage']}")
        report["pinned_note"] = {
            "delivery_id": shown["id"], "stage": shown["stage"], "revision": shown["revision"],
            "digest": shown["digest"], "status_code": shown["status_code"], "timing": shown["timing"],
            "stages": stage_timings(shown)}
        if shown["stage"] != "displayed":
            raise SliceError(f"the pinned note ended {shown['stage']}: {shown.get('detail')}")
        preview = await wait(lambda: (r := api.http.get(f"/api/tags/devices/{device_id}/preview",
                                                        params={"kind": "displayed"})).status_code == 200
                             and int(r.headers.get("x-tag-revision", 0)) >= shown["revision"] and r,
                             "the displayed preview", 30)
        (scratch / "displayed.png").write_bytes(preview.content)
        report["pinned_note"]["preview"] = {"bytes": len(preview.content),
                                            "revision": int(preview.headers["x-tag-revision"]),
                                            "png": preview.content.startswith(b"\x89PNG")}
        clock.mark("displayed preview stored")

        # 5b. an event run without an LLM (best effort)
        if event_run:
            try:
                report["event_run"] = await event_run_path(api, device_id, wait, clock)
            except SliceError as exc:
                report["event_run"] = {"ok": False, "error": str(exc)}
        device = await asyncio.to_thread(api.call, "GET", f"/api/tags/devices/{device_id}")
        report["device"] = {k: device["device"][k] for k in ("hw_id", "epoch", "desired_revision",
                                                              "displayed_revision", "displayed_digest",
                                                              "clear_required", "status")}
        report["daemon_status"] = svc.status_snapshot() if svc is not None else None
        report["ok"] = True
    except BaseException as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if svc is not None:
            svc.stop()
        if task is not None:
            with contextlib.suppress(BaseException):
                await task
        if sim is not None:
            await sim.stop()
        await asyncio.to_thread(server.stop)
        api.http.close()
        report["total_s"] = round(time.monotonic() - clock.start, 3)
        with contextlib.suppress(OSError):
            (scratch / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


async def event_run_path(api: Api, device_id: str, wait: Any, clock: Clock) -> dict[str, Any]:
    """A Calendar & Schedule event fires, runs an agent that fails on the dummy key: ``run.failed``."""
    before = {d["id"] for d in api.call("GET", "/api/tags/deliveries", params={"device": device_id,
                                                                                "limit": 200})["deliveries"]}
    fire_at = dt.datetime.now() + dt.timedelta(seconds=8)
    api.call("POST", "/api/calendar/events", json={"title": "E2E schedule", "action": "Say hello",
                                                   "dtstart": fire_at.strftime("%Y-%m-%dT%H:%M:%S"),
                                                   "schedule_kind": "instant"})
    clock.mark("schedule event created")

    def outcome() -> dict[str, Any] | None:
        rows = api.call("GET", "/api/tags/deliveries", params={"device": device_id, "limit": 200})["deliveries"]
        new = [d for d in rows if d["id"] not in before and d["kind"] == "task_outcome"]
        return next((d for d in new if d["stage"] in ("displayed", "failed", "expired", "uncertain")), None)

    shown = await wait(outcome, "the event run's outcome card displayed", 180)
    clock.mark(f"event run card {shown['stage']}")
    return {"ok": shown["stage"] == "displayed", "delivery_id": shown["id"], "kind": shown["kind"],
            "title": (shown.get("card") or {}).get("title"), "stage": shown["stage"], "revision": shown["revision"],
            "digest": shown["digest"], "stages": stage_timings(shown)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cremind-repo", type=Path, default=DEFAULT_CREMIND)
    parser.add_argument("--port", type=int, default=1181)
    parser.add_argument("--time-scale", type=float, default=10.0)
    parser.add_argument("--no-event-run", action="store_true", help="Skip the schedule/event-run path.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch directory.")
    args = parser.parse_args()
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "cremind_tag.sim"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    scratch = REPO / "build" / "e2e-slice" / stamp
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(run_slice(args.cremind_repo, args.port, scratch, time_scale=args.time_scale,
                                       event_run=not args.no_event_run))
    except Exception as exc:
        print(f"E2E SLICE FAILED: {exc}", file=sys.stderr)
        print(f"scratch kept at {scratch}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, default=str))
    if args.keep:
        print(f"scratch kept at {scratch}")
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
