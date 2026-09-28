"""The delivery daemon: Cremind feed -> durable queue -> screens -> gateway -> receipts.

::

    options = DaemonOptions.from_config(load_config(), gateway_url="COM7", fontpack=Path("noto.ctfp"))
    await DaemonService(options).run()          # until stop() / Ctrl-C

Components (one asyncio task each, all sharing one SQLite database through
:class:`~cremind_tag.daemon.store.QueueStore`, every blocking call in a worker
thread):

==========================  ==========================================================
:class:`ContentWorker`      per content credential: ``sync`` + ``events`` -> jobs
:class:`HardwareWorker`     hardware credential: inventory, heartbeat, commands
:class:`ScreenScheduler`    card sets -> composed revisions -> ``DELIVER_LAYOUT``
:class:`GatewayEventHandler` gateway results -> revisions/receipts (commit, then ACK)
:class:`OutboxSender`       per credential: receipts, accepted, previews, results
==========================  ==========================================================

A revoked or invalid credential (401/403) stops only its own loops; a TLS
misconfiguration pauses them and retries every ``tls_retry_s``. The gateway connection is retried with back-off until it answers; the
link then reconnects by itself. Durability boundaries call
:meth:`CrashPoints.hit` so tests can kill the service at each of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .. import __version__
from ..connector.client import Backoff, ConnectorClient, Credential, parse_credential
from ..connector.models import iso_now
from ..connector.settings import CremindSettings
from ..gateway.client import GatewayClient
from ..gateway.errors import GatewayError
from ..gateway.events import SessionStarted
from ..gateway.opid import OpIdGenerator
from ..store.db import Database, GatewayRecord
from .commands import OpWaiters
from .content import ContentWorker
from .crash import CrashPoints, SimulatedCrash
from .events import GatewayEventHandler
from .hardware import HardwareWorker
from .outbox import OutboxSender
from .schema import open_database
from .screens import ScreenScheduler
from .settings import DaemonSettings
from .store import RECEIPTS_IN_ORDER, Effects, QueueStore

if TYPE_CHECKING:
    from ..config import Config
    from ..fonts.fontset import FontSet
    from ..secrets import SecretStore

log = logging.getLogger(__name__)

STATUS_FILE = "daemon-status.json"
STATUS_EVERY_S = 2.0


class DaemonConfigError(RuntimeError):
    """The daemon cannot start with this configuration."""


@dataclass
class DaemonOptions:
    """Everything the daemon needs, resolved (see :meth:`from_config`)."""

    db_path: Path
    data_dir: Path
    cremind_url: str | None = None
    ca_file: Path | None = None
    hardware_credential: Credential | None = None
    content_credentials: list[Credential] = field(default_factory=list)
    gateway_url: str | None = None
    bridge_url: str | None = None
    fontpack: Path | None = None
    font_cache: Path | None = None
    fonts: FontSet | None = None
    secrets: SecretStore | None = None
    settings: DaemonSettings = field(default_factory=DaemonSettings)
    timeout_s: float = 30.0
    transport: httpx.AsyncBaseTransport | None = None
    """Tests: an in-process Cremind (``httpx.MockTransport``)."""
    crash: CrashPoints | None = None
    clock: Callable[[], float] = time.time
    gateway_options: dict[str, Any] = field(default_factory=dict)
    write_status: bool = True
    gateway_hw_id: str | None = None
    """A fixed inventory id for the gateway (a protocol v2 worker: ``gw-<device_id>``)."""

    @classmethod
    def from_config(cls, config: Config, *, gateway_url: str | None = None, fontpack: Path | None = None,
                    secrets: SecretStore | None = None) -> DaemonOptions:
        """Resolve the ``[cremind]``, ``[hardware]`` and ``[daemon]`` sections and the credential secrets."""
        from ..secrets import SecretStore

        cremind = config.section(CremindSettings)
        hardware = config.hardware
        data_dir = config.ensure_data_dir()
        secrets = secrets or SecretStore.open(data_dir, config.secrets.backend)

        def credential(credential_id: str) -> Credential:
            value = secrets.get_credential(credential_id)
            if value is None:
                raise DaemonConfigError(f"the secret of credential {credential_id} is missing from "
                                        f"{secrets.describe()}; add it again with `cremind-tag connect add-…`")
            return parse_credential(value)

        return cls(
            db_path=config.db_path, data_dir=data_dir, cremind_url=cremind.url, ca_file=cremind.ca_file,
            hardware_credential=credential(cremind.hardware_credential) if cremind.hardware_credential else None,
            content_credentials=[credential(c) for c in cremind.content_credentials],
            gateway_url=gateway_url or hardware.gateway_url, bridge_url=hardware.bridge_url,
            fontpack=fontpack or hardware.fontpack, secrets=secrets, settings=config.section(DaemonSettings),
            timeout_s=cremind.timeout_s)


def load_fonts(pack: Path, cache_dir: Path | None = None) -> FontSet:
    from ..fonts.fontset import FontSet

    return FontSet.load(pack, cache_dir)


class DaemonService:
    """The daemon (see the module docstring)."""

    def __init__(self, options: DaemonOptions) -> None:
        self.options = options
        self.settings = options.settings
        self.clock = options.clock
        self.crash = options.crash or CrashPoints()
        self.op_ids = OpIdGenerator()
        self.secrets = options.secrets
        self.gateway_url = options.gateway_url
        self.bridge_url = options.bridge_url
        self.fontpack_path = options.fontpack
        self.fonts: FontSet | None = options.fonts
        self.db: Database = None  # type: ignore[assignment]  # opened in start()
        self.store: QueueStore = None  # type: ignore[assignment]
        self.gateway: GatewayClient | None = None
        self.gateway_hw_id: str | None = None
        # bridge hw_id -> {"max_tags", "assigned"} from the gateway's inventory (hardware.bridge_capacity)
        self.bridge_capacity: dict[str, dict[str, int | None]] = {}
        self.boot_generation = 0
        self.ops = OpWaiters()
        self.started_at = iso_now()
        self.scheduler = ScreenScheduler(self)
        self.handler = GatewayEventHandler(self)
        self.content_workers: dict[str, ContentWorker] = {}
        self.hardware: HardwareWorker | None = None
        self.senders: dict[str, OutboxSender] = {}
        self.clients: list[ConnectorClient] = []
        self.agent: Any = None
        """A Cremind Connect worker's agent (:class:`cremind_tag.connect.agent.ConnectAgent`): runs
        ``run_operation`` commands and adds live generations to heartbeats; ``None`` elsewhere."""
        self.failed_credentials: dict[str, str] = {}
        self.credential_warnings: dict[str, str] = {}  # TLS problems, retried slowly
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._crashed: SimulatedCrash | None = None
        self._failure: BaseException | None = None
        self._started = False
        self.caught_up = False
        self.final_status: dict[str, Any] = {}
        self.crash.on_crash = self._on_crash

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        """Open everything and start the loops (returns immediately)."""
        if self._started:
            return
        self._started = True
        opts = self.options
        self.db = await asyncio.to_thread(open_database, opts.db_path)
        self.store = QueueStore(self.db, clock=self.clock, op_ids=self.op_ids,
                                retry_initial_s=self.settings.retry_initial_s, retry_max_s=self.settings.retry_max_s,
                                uncertain_retries=self.settings.uncertain_retries)
        if self.fonts is None and opts.fontpack is not None:
            try:
                self.fonts = await asyncio.to_thread(load_fonts, opts.fontpack, opts.font_cache)
            except Exception as exc:  # the daemon still syncs and accepts; tags are held
                log.error("daemon: font pack %s not usable: %s — screens are held until it is fixed", opts.fontpack,
                          exc)
        # A different pack may be active now: give tags blocked on a mismatch another chance.
        await self.db.run(self.store.unblock_all, "fontpack_mismatch")
        if (opts.hardware_credential or opts.content_credentials) and not opts.cremind_url:
            raise DaemonConfigError("no Cremind URL configured (cremind-tag connect server URL)")
        if opts.gateway_url is not None:
            # gateway_hw_id is derived per session (record_gateway): before the port enumerates, the USB
            # serial number is unknown and a URL-based id would name a phantom gateway.
            client_options = {"reconnect": True, "name": "cremind-tag daemon", "op_ids": self.op_ids,
                              **opts.gateway_options}
            self.gateway = GatewayClient(opts.gateway_url, **client_options)
            self.gateway.add_event_handler(self.handler)
            self._spawn(self._connect_gateway(), "gateway connect")
        for credential in opts.content_credentials:
            client = self._client(credential)
            worker = ContentWorker(self, client)
            self.content_workers[credential.credential_id] = worker
            self.senders[credential.credential_id] = OutboxSender(self, client)
        if opts.hardware_credential is not None:
            client = self._client(opts.hardware_credential)
            self.hardware = HardwareWorker(self, client)
            self.senders[opts.hardware_credential.credential_id] = OutboxSender(self, client)
        for credential_id, worker in self.content_workers.items():
            self._spawn(worker.run(), f"content {credential_id}")
        if self.hardware is not None:
            self._spawn(self.hardware.run(), "hardware")
        for credential_id, sender in self.senders.items():
            self._spawn(sender.run(), f"outbox {credential_id}")
        self._spawn(self.scheduler.run(), "scheduler")
        if opts.write_status:
            self._spawn(self._status_loop(), "status")
        log.info("daemon: started (version %s, db %s, gateway %s, font pack %s, %d content credential(s), "
                 "hardware credential %s)", __version__, opts.db_path, opts.gateway_url or "-",
                 self.fonts.pack_id.hex() if self.fonts else "-", len(self.content_workers),
                 "yes" if self.hardware else "no")

    def _client(self, credential: Credential) -> ConnectorClient:
        assert self.options.cremind_url is not None
        client = ConnectorClient(self.options.cremind_url, credential, ca_file=self.options.ca_file,
                                 timeout=self.options.timeout_s, transport=self.options.transport)
        self.clients.append(client)
        return client

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, SimulatedCrash):
            self._on_crash(exc)
        elif exc is not None:
            log.error("daemon: task %s died: %r", task.get_name(), exc, exc_info=exc)
            if self._failure is None:
                self._failure = exc
            self._stop.set()

    def _on_crash(self, crash: SimulatedCrash) -> None:
        if self._crashed is None:
            self._crashed = crash
            log.warning("daemon: simulated crash at %s", crash)
        self._stop.set()

    async def run(self, *, until: Callable[[], Awaitable[bool]] | None = None, max_s: float | None = None) -> None:
        """Run until :meth:`stop`, a simulated crash, ``await until()`` turning true, or ``max_s`` seconds."""
        await self.start()
        deadline = None if max_s is None else time.monotonic() + max_s
        try:
            while not self._stop.is_set():
                if until is not None and await until():
                    self.caught_up = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), 0.05 if until is not None else 1.0)
        finally:
            with contextlib.suppress(Exception):
                self.final_status = await asyncio.to_thread(self.status_snapshot)
            await self.close()
        if self._crashed is not None:
            raise self._crashed
        if self._failure is not None:
            raise RuntimeError(f"a daemon task failed: {self._failure!r}") from self._failure

    async def run_once(self, *, max_s: float = 60.0, settle_s: float = 1.0) -> dict[str, Any]:
        """``daemon run --once``: run until the feed is caught up, nothing is due and every result sent for
        is back (or ``max_s``), then stop; the snapshot says whether it ``caught_up``."""
        quiet_since: list[float] = []

        async def idle() -> bool:
            if not await self.quiescent():
                quiet_since.clear()
                return False
            if not quiet_since:
                quiet_since.append(time.monotonic())
            return time.monotonic() - quiet_since[0] >= settle_s

        await self.run(until=idle, max_s=max_s)
        return {**self.final_status, "caught_up": self.caught_up}

    async def quiescent(self) -> bool:
        """Caught up with every feed, nothing to compose or send, no result outstanding, the outbox flushed."""
        if self.store is None:
            return False
        if any(w.state != "stopped" and not w.caught_up for w in self.content_workers.values()):
            return False
        if self.hardware is not None and self.hardware.state not in ("stopped",)                 and not self.hardware.inventory_done.is_set():
            return False
        if self.hardware is not None and not self.hardware.executor.idle():
            return False
        connected = self.gateway is not None and self.gateway.connected
        live = [c for c in self.senders if c not in self.failed_credentials]
        return await self.db.run(self._queue_idle, connected, live)

    def _queue_idle(self, connected: bool, live: list[str]) -> bool:
        now = self.clock()
        with self.db.reading() as conn:
            if conn.execute("SELECT 1 FROM tag_views WHERE (dirty = 1 OR force = 1) AND blocked_reason IS NULL"
                            " AND clear_required = 0 AND credential_id IS NOT NULL LIMIT 1").fetchone():
                return False
            if connected and conn.execute("SELECT 1 FROM revisions WHERE (state = 'pending' AND next_attempt_ts <= ?)"
                                          " OR state = 'sent' LIMIT 1", (now,)).fetchone():
                return False  # something is due, or on its way to a tag and its result still to come
            if live and conn.execute(f"SELECT 1 FROM outbox WHERE dead = 0 AND next_attempt_ts <= ? AND credential_id"
                                     f" IN ({','.join('?' * len(live))}) AND {RECEIPTS_IN_ORDER} LIMIT 1",
                                     (now, *live, now)).fetchone():
                return False
        return True

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        """Tear down: cancel every loop, close the gateway (no ACK is sent for anything unhandled)."""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self.hardware is not None:
            await self.hardware.executor.aclose()
        if self.gateway is not None:
            with contextlib.suppress(BaseException):
                await self.gateway.close()
        for client in self.clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        if self.options.write_status and self.db is not None and self._crashed is None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._write_status, stopped=True)
        if self.db is not None:
            with contextlib.suppress(Exception):
                self.db.close()

    async def _connect_gateway(self) -> None:
        assert self.gateway is not None
        backoff = Backoff(0.5, 30.0)
        while True:
            try:
                hello = await self.gateway.connect()
                log.info("daemon: gateway %s connected (fw %s, boot %08x)", self.gateway_url, hello.fw, hello.boot_id)
                self.wake_scheduler()
                return
            except (GatewayError, OSError, TimeoutError) as exc:
                delay = backoff.next()
                log.warning("daemon: gateway %s not reachable (%s); retry in %.1fs", self.gateway_url, exc, delay)
                await asyncio.sleep(delay)

    # -- coordination ----------------------------------------------------------------------

    def wake_scheduler(self) -> None:
        self.scheduler.wake()

    def wake_outbox(self, credential_id: str | None = None) -> None:
        if credential_id is None:
            for sender in self.senders.values():
                sender.wake()
        elif credential_id in self.senders:
            self.senders[credential_id].wake()

    def request_sync(self, credential_id: str, *, forced: bool = False) -> None:
        worker = self.content_workers.get(credential_id)
        if worker is not None:
            worker.request_sync(forced=forced)

    def request_inventory(self) -> None:
        if self.hardware is not None:
            self.hardware.request_inventory()

    def apply_effects(self, effects: Effects) -> None:
        for credential_id in effects.sync:
            self.request_sync(credential_id)
        if effects.inventory:
            self.request_inventory()
        if effects.outbox:
            self.wake_outbox()
        if effects.tags:
            self.wake_scheduler()

    def credential_failed(self, credential_id: str, exc: Exception) -> None:
        self.failed_credentials[credential_id] = str(exc)

    def credential_warning(self, credential_id: str, exc: Exception) -> None:
        self.credential_warnings[credential_id] = str(exc)

    def credential_ok(self, credential_id: str) -> None:
        self.credential_warnings.pop(credential_id, None)

    def gateway_session(self, event: SessionStarted, boot_changed: bool) -> None:
        if boot_changed:
            self.boot_generation += 1
            log.warning("daemon: gateway boot %08x: re-delivering everything that was in flight",
                        event.hello.boot_id)
        self.request_inventory()
        self.wake_scheduler()

    def record_gateway(self, event: SessionStarted) -> None:
        """Every session: the gateway's id (from its USB serial number now that the port is open) and row."""
        if self.gateway_url is None:
            return
        if self.options.gateway_hw_id is not None:
            self.gateway_hw_id = self.options.gateway_hw_id
        else:
            from ..cli._hardware import gateway_hw_id

            self.gateway_hw_id = gateway_hw_id(self.gateway_url)
        hello = event.hello
        board = hello.caps.board if isinstance(hello.caps.board, int) else None
        self.db.upsert_gateway(GatewayRecord(self.gateway_hw_id, port=self.gateway_url, boot_id=hello.boot_id,
                                             fw=hello.fw, build=hello.build, board=board))

    def note_bridge_info(self, addr: int, fontpack_id: str | None, fw: str | None,
                         capacity: dict[str, int | None] | None = None) -> bool:
        """``EVT_BRIDGE_INFO``: remember the bridge's pack/firmware and table capacity (``max_tags`` /
        ``assigned``); True when something the inventory reports changed."""
        bridge = self.db.find_bridge(addr=addr)
        if bridge is None:
            return False
        changes: dict[str, Any] = {}
        if fontpack_id and fontpack_id != bridge.fontpack_id:
            changes["fontpack_id"] = fontpack_id
        if fw and fw != bridge.fw:
            changes["fw"] = fw
        if changes:
            self.db.update_bridge(bridge.uuid, **changes)
        capacity_changed = capacity is not None and self.note_capacity(bridge.hw_id, capacity)
        return bool(changes) or capacity_changed

    def note_capacity(self, hw_id: str, capacity: dict[str, int | None]) -> bool:
        """Merge a bridge's known ``max_tags`` / ``assigned`` (an unknown value never replaces a known one:
        a rebooted gateway reports none until the bridge's CAPS arrive); True when a known value changed."""
        known = self.bridge_capacity.setdefault(hw_id, {})
        changed = False
        for key, value in capacity.items():
            if value is not None and known.get(key) != value:
                known[key] = value
                changed = True
        return changed

    # -- status ----------------------------------------------------------------------------

    def status_snapshot(self) -> dict[str, Any]:
        gateway = self.gateway
        stats = self.store.queue_stats() if self.store is not None else {}
        credentials: dict[str, Any] = {}
        for credential_id, worker in self.content_workers.items():
            credentials[credential_id] = {"kind": "content", "profile": worker.profile, "state": worker.state,
                                          "error": worker.error, "syncs": worker.syncs, "pages": worker.pages}
        if self.hardware is not None:
            credentials[self.hardware.credential_id] = {
                "kind": "hardware", "companion_id": self.hardware.companion_id, "state": self.hardware.state,
                "error": self.hardware.error, "inventories": self.hardware.inventories,
                "heartbeats": self.hardware.heartbeats, "commands_running": dict(self.hardware.executor.running)}
        for credential_id, error in self.credential_warnings.items():
            credentials.setdefault(credential_id, {})["state"] = "tls_error"
            credentials[credential_id]["error"] = error
        for credential_id, error in self.failed_credentials.items():
            credentials.setdefault(credential_id, {})["state"] = "stopped"
            credentials[credential_id]["error"] = error
        return {
            "pid": os.getpid(), "version": __version__, "started_at": self.started_at, "updated_at": iso_now(),
            "cremind_url": self.options.cremind_url,
            "gateway": {"url": self.gateway_url, "connected": bool(gateway and gateway.connected),
                        "boot_id": gateway.boot_id if gateway else None,
                        "stats": dict(gateway.stats) if gateway else {}},
            "fontpack_id": self.fonts.pack_id.hex() if self.fonts is not None else None,
            "credentials": credentials,
            "scheduler": {"composed": self.scheduler.composed, "sent": self.scheduler.sent,
                          "holds": {f"{k:08X}": v for k, v in self.scheduler.holds.items()}},
            "results": self.handler.results,
            "queue": stats,
        }

    def _write_status(self, *, stopped: bool = False) -> None:
        snapshot = self.status_snapshot()
        snapshot["running"] = not stopped
        path = self.options.data_dir / STATUS_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)

    async def _status_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._write_status)
            except OSError as exc:
                log.debug("daemon: status file not written: %s", exc)
            await asyncio.sleep(STATUS_EVERY_S)


def read_status(data_dir: Path) -> dict[str, Any] | None:
    """The last status snapshot a daemon wrote (``None`` if none)."""
    path = Path(data_dir) / STATUS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


__all__ = ["DaemonConfigError", "DaemonOptions", "DaemonService", "STATUS_FILE", "read_status"]
