"""Test rig: simulator (gateway, bridges, tags) + fake Cremind + a daemon on one event loop.

::

    async with Rig(tmp_path, fonts) as rig:
        await rig.start()                                  # a DaemonService on the rig's database
        did = rig.fake.add_job("alice", rig.hw(0), title="Hello")
        await rig.wait(lambda: rig.fake.delivery(did)["stage"] == "displayed")
        await rig.crash_at("revision_persisted")           # arm, wait for the crash, restart

The simulator's hardware is registered in the rig's inventory and file-backed
secret store (never the OS keyring) exactly as ``cremind-tag sim run --register``
does; the fake Cremind owns the same tags for profile ``alice`` at epoch 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cremind_tag.cli._hardware import gateway_hw_id
from cremind_tag.connector.client import Credential
from cremind_tag.daemon import CrashPoints, DaemonOptions, DaemonService, SimulatedCrash, open_database
from cremind_tag.daemon.settings import DaemonSettings
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.protocol.ids import Board, Panel
from cremind_tag.secrets import FileBackend, SecretStore
from cremind_tag.sim import BridgeSpec, Simulator
from cremind_tag.sim.harness import make_config
from cremind_tag.store import BridgeRecord, GatewayRecord, TagRecord

FAST = dict(active_poll_s=0.05, idle_poll_s=0.2, heartbeat_s=0.3, resync_s=60.0, command_wait_s=1,
            scan_interval_s=0.1, retry_initial_s=0.1, retry_max_s=1.0, connector_retry_max_s=0.3,
            result_timeout_s=60.0, identify_hold_s=0.5)


def fast_settings(**overrides: Any) -> DaemonSettings:
    return DaemonSettings(**{**FAST, **overrides})


async def wait_until(predicate: Callable[[], bool], timeout: float = 30.0, interval: float = 0.05,
                     what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(interval)


class Rig:
    """See the module docstring."""

    def __init__(self, tmp_path: Path, fonts: FontSet, *, tags: int = 1, bridges: int = 1, seed: int = 5,
                 time_scale: float = 200.0, panel: int = Panel.UC8176_420_BW, owner: str | None = "alice",
                 assign: bool = True, settings: dict[str, Any] | None = None, unprovisioned: int = 0,
                 bridge_pack: bytes | None = None, **sim_overrides: Any) -> None:
        from fake_cremind import FakeCremind  # type: ignore[import-not-found]  # loaded by conftest

        self.tmp_path = tmp_path
        self.fonts = fonts
        self.data_dir = tmp_path / "data"
        self.db_path = self.data_dir / "companion.sqlite3"
        self.secrets = SecretStore(FileBackend(self.data_dir / "secrets.json"))
        self.config = make_config(fontpack=Path(fonts.pack_path).read_bytes(), tags=tags, bridges=bridges, seed=seed,
                                  time_scale=time_scale, assign=assign, panel=panel, **sim_overrides)
        if bridge_pack is not None:  # bridges start with another font pack active
            for spec in self.config.bridges:
                spec.fontpack = bridge_pack
        self.config.bridges += [BridgeSpec(name=f"spare-{i + 1}", provisioned=False) for i in range(unprovisioned)]
        self.sim = Simulator(self.config)
        self.fake = FakeCremind()
        self.hardware_cred = self.fake.add_credential("hardware")
        self.owner = owner
        self.content_cred = self.fake.add_credential("content", owner or "alice")
        self.settings_overrides = settings or {}
        self.svc: DaemonService | None = None
        self.task: asyncio.Task[None] | None = None
        self.crash = CrashPoints()
        self.runs = 0

    # -- names ---------------------------------------------------------------------------------

    def tag_id(self, index: int = 0) -> int:
        return self.config.tags[index].tag_id

    def hw(self, index: int = 0) -> str:
        return f"{self.tag_id(index):08X}"

    def bridge_hw(self, index: int = 0) -> str:
        return f"br-{self.sim.bridges[index].uuid.hex()}"

    def sim_tag(self, index: int = 0) -> Any:
        return self.sim.tag(self.tag_id(index))

    # -- lifecycle -------------------------------------------------------------------------------

    async def __aenter__(self) -> Rig:
        await self.sim.start()
        self.register()
        assigned = {a.tag_id: a for a in self.config.assignments}
        for index, spec in enumerate(self.config.tags):
            a = assigned.get(spec.tag_id)
            self.fake.add_tag(f"{spec.tag_id:08X}", owner=self.owner if a else None, epoch=a.epoch if a else 0,
                              bridge_hw_id=self.bridge_hw(a.bridge) if a else None, width=spec.width,
                              height=spec.height, planes=spec.planes, name=f"Tag {index + 1}")
        for index in range(len(self.sim.bridges)):
            self.fake.add_bridge(self.bridge_hw(index))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
        await self.sim.stop()

    def register(self) -> None:
        """What ``cremind-tag sim run --register`` does, into the rig's own database and secret store."""
        url = self.sim.gateway_url
        with open_database(self.db_path) as db:
            db.upsert_gateway(GatewayRecord(gateway_hw_id(url), port=url, boot_id=self.sim.gateway.boot_id,
                                            fw="0.1.0", build="sim", board=Board.NRF52840DK_GATEWAY))
            for bridge in self.sim.bridges:
                if bridge.addr is None:
                    continue
                pack = bridge.fontpack_id
                db.upsert_bridge(BridgeRecord(bridge.uuid.hex(), addr=bridge.addr, name=bridge.name,
                                              configured=bridge.configured, fw="0.1.0", board=bridge.board,
                                              fontpack_id=pack.hex() if pack else None,
                                              flash_size=bridge.flash.size, gateway_hw_id=gateway_hw_id(url)))
            assigned = {a.tag_id: a for a in self.config.assignments}
            for index, tag in enumerate(self.sim.tags.values()):
                if db.tag_exists(tag.tag_id):
                    continue
                ref = self.secrets.set_tag_secret(tag.tag_id, tag.spec.secret)
                a = assigned.get(tag.tag_id)
                spec = tag.spec
                db.insert_tag(TagRecord(tag.tag_id, spec.board, spec.panel, spec.width, spec.height, spec.planes,
                                        spec.plane_flags, ref, name=f"sim-{index + 1}", fw="0.1.0",
                                        epoch=a.epoch if a else 0,
                                        bridge_addr=self.sim.bridges[a.bridge].addr if a else None))

    def options(self, **overrides: Any) -> DaemonOptions:
        values: dict[str, Any] = dict(
            db_path=self.db_path, data_dir=self.data_dir, cremind_url=self.fake.url,
            hardware_credential=Credential(self.hardware_cred.id, self.hardware_cred.secret),
            content_credentials=[Credential(self.content_cred.id, self.content_cred.secret)],
            gateway_url=self.sim.gateway_url, fontpack=Path(self.fonts.pack_path), fonts=self.fonts,
            secrets=self.secrets, settings=fast_settings(**self.settings_overrides), transport=self.fake.transport,
            crash=self.crash, gateway_options={"request_timeout": 2.0, "handler_backoff": (0.05, 0.5)})
        values.update(overrides)
        return DaemonOptions(**values)

    async def start(self, **overrides: Any) -> DaemonService:
        """Start a daemon (a new process, as far as the database and the gateway can tell)."""
        assert self.task is None or self.task.done(), "a daemon is already running"
        self.svc = DaemonService(self.options(**overrides))
        self.runs += 1
        self.task = asyncio.create_task(self.svc.run(), name=f"daemon run {self.runs}")
        await wait_until(lambda: self.svc is not None and self.svc.store is not None, 10, what="daemon start")
        return self.svc

    async def stop(self) -> None:
        if self.svc is not None:
            self.svc.stop()
        if self.task is not None:
            with contextlib.suppress(SimulatedCrash, Exception):
                await self.task
        self.task = None

    async def wait_crash(self, timeout: float = 30.0) -> str:
        """Wait for the running daemon to crash at its armed boundary; returns the boundary."""
        assert self.task is not None
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout)
        except SimulatedCrash as crash:
            self.task = None
            return str(crash)
        raise AssertionError("the daemon stopped without crashing")

    async def wait(self, predicate: Callable[[], bool], timeout: float = 30.0, what: str = "condition") -> None:
        async def check() -> None:
            await wait_until(predicate, timeout, what=what)

        if self.task is None:
            await check()
            return
        waiter = asyncio.create_task(check())
        done, _ = await asyncio.wait({waiter, self.task}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            waiter.result()
            return
        waiter.cancel()
        exc = self.task.exception() if not self.task.cancelled() else None
        raise AssertionError(f"the daemon stopped while waiting for {what}: {exc!r}")

    # -- inspection --------------------------------------------------------------------------------

    def db(self) -> Any:
        return open_database(self.db_path)

    def job_state(self, delivery_id: int) -> tuple[str, str | None]:
        with self.db() as db, db.reading() as conn:
            row = conn.execute("SELECT state, outcome FROM jobs WHERE delivery_id = ?", (delivery_id,)).fetchone()
        return (row["state"], row["outcome"]) if row else ("missing", None)

    def stage(self, delivery_id: int) -> str:
        return str(self.fake.delivery(delivery_id)["stage"])

    def assert_consistent_receipts(self) -> None:
        """No delivery was ever reported with two different terminal outcomes."""
        bad = {d: o for d, o in self.fake.terminal_outcomes().items() if len(o) > 1}
        assert not bad, f"deliveries receipted with conflicting outcomes: {bad}"


__all__ = ["FAST", "Rig", "fast_settings", "wait_until"]
