"""The hardware credential's loops: inventory, heartbeat and the command queue (connector-api.md).

- **Inventory** (``POST inventory``) at start-up and whenever the hardware
  changes (a gateway session, provisioning, an assignment, a bridge reporting
  another font pack, a tag's epoch floor): gateways, bridges (refreshed from
  ``LIST_NODES`` and ``GET_INVENTORY`` when the gateway is connected, with
  ``max_tags`` from the bridge's CAPS and ``assigned``, the gateway's
  assignments for it) and enrolled tags with their panel and the highest
  ``epoch`` this companion used or the tag's epoch floor (a ``STALE_EPOCH``'s
  ``stored_epoch``) — Cremind keeps ``max(stored, reported)`` so it never
  assigns an epoch a tag refuses.
- **Heartbeat** every ``heartbeat_s``: companion version/host/start, queue
  depth and the oldest job's age, and per device battery, RSSI, last contact,
  displayed revision/digest and a status (``ok|pending|offline|error``).
- **Commands**: long-poll ``GET commands``; each command is persisted, then
  claimed, then run by :class:`~cremind_tag.daemon.commands.CommandExecutor`.
  Commands left ``running`` by a previous run resume first; a command still
  ``claiming`` (its claim failed, or its answer was lost although Cremind
  committed it — Cremind never offers a claimed command again) is claimed
  again before every poll, with back-off, until Cremind answers.

A 401/403 stops these loops only (content credentials keep working); a TLS
configuration error is retried every ``tls_retry_s``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..connector.client import (
    Backoff,
    ConnectorAuthError,
    ConnectorClient,
    ConnectorConflict,
    ConnectorError,
    ConnectorNotFound,
    ConnectorTlsError,
)
from ..connector.models import Command, tag_hw_id
from ..gateway.errors import GatewayError
from ..store.db import BridgeRecord
from .commands import CommandExecutor, bridge_capacity
from .store import CommandRow

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

OFFLINE_AFTER_S = 2 * 3600
MIN_INVENTORY_INTERVAL_S = 1.0


class _Stop(Exception):
    """The hardware credential is unusable: stop every hardware loop."""


class HardwareWorker:
    """Inventory, heartbeat and commands for the hardware credential (see the module docstring)."""

    def __init__(self, svc: DaemonService, client: ConnectorClient) -> None:
        self.svc = svc
        self.client = client
        self.credential_id = client.credential_id
        self.executor = CommandExecutor(svc, self.credential_id)
        self.companion_id: str | None = None
        self.state = "starting"
        self.error: str | None = None
        self.inventories = 0
        self.heartbeats = 0
        self.commands_claimed = 0
        self._inventory_wanted = asyncio.Event()
        self._inventory_wanted.set()
        self.inventory_done = asyncio.Event()
        self._claim_retry: dict[str, tuple[int, float]] = {}  # command id -> (failed claims, next try)

    def request_inventory(self) -> None:
        self._inventory_wanted.set()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._guard(self._inventory_loop()), name="hardware inventory"),
                 asyncio.create_task(self._guard(self._heartbeat_loop()), name="hardware heartbeat"),
                 asyncio.create_task(self._guard(self._command_loop()), name="hardware commands")]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, _Stop):
                    raise exc
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            await self.executor.aclose()

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except ConnectorAuthError as exc:
            self.state, self.error = "stopped", str(exc)
            self.svc.credential_failed(self.credential_id, exc)
            log.error("hardware: credential=%s stopped: %s", self.credential_id, exc)
            raise _Stop(str(exc)) from None

    async def _tls_pause(self, exc: ConnectorTlsError) -> None:
        """A TLS configuration error: report it and try again after ``tls_retry_s``."""
        self.state, self.error = "tls_error", str(exc)
        self.svc.credential_warning(self.credential_id, exc)
        log.error("hardware: credential=%s TLS problem (retry in %.0fs): %s", self.credential_id,
                  self.svc.settings.tls_retry_s, exc)
        await asyncio.sleep(self.svc.settings.tls_retry_s)

    # -- inventory -------------------------------------------------------------------------

    async def _inventory_loop(self) -> None:
        backoff = Backoff(1.0, self.svc.settings.connector_retry_max_s)
        while True:
            await self._inventory_wanted.wait()
            self._inventory_wanted.clear()
            try:
                if self.companion_id is None:
                    who = await self.client.whoami()
                    if who.kind != "hardware":
                        raise ConnectorAuthError(f"credential {self.credential_id} is a {who.kind} credential, "
                                                 "not a hardware credential", status=403)
                    self.companion_id = who.companion_id
                body = await self.build_inventory()
                result = await self.client.inventory(body)
                self.inventories += 1
                self.state, self.error = "running", None
                self.svc.credential_ok(self.credential_id)
                backoff.reset()
                self.inventory_done.set()
                log.info("hardware: inventory gateways=%d bridges=%d tags=%d assignments=%d",
                         len(body["gateways"]), len(body["bridges"]), len(body["tags"]), len(result.assignments))
            except ConnectorAuthError:
                raise
            except ConnectorTlsError as exc:
                await self._tls_pause(exc)
                self._inventory_wanted.set()
                continue
            except ConnectorError as exc:
                self.state, self.error = "retrying", str(exc)
                delay = backoff.next()
                log.warning("hardware: inventory retry in %.1fs: %s", delay, exc)
                await asyncio.sleep(delay)
                self._inventory_wanted.set()
                continue
            await asyncio.sleep(MIN_INVENTORY_INTERVAL_S)

    async def refresh_bridges(self) -> None:
        """Bring the local bridge rows up to date from the gateway's CDB and cached bridge info."""
        svc = self.svc
        gateway = svc.gateway
        if gateway is None or not gateway.connected:
            return
        try:
            nodes = await asyncio.wait_for(gateway.list_nodes(), 10)
            infos = {i.addr: i for i in await asyncio.wait_for(gateway.get_inventory(), 10)}
        except (GatewayError, TimeoutError) as exc:
            log.info("hardware: bridge refresh skipped: %s", exc)
            return
        for node in nodes:
            info = infos.get(node.addr)
            pack = info.fontpack_id.hex() if info and info.fontpack_id and any(info.fontpack_id) else None
            record = BridgeRecord(node.uuid.hex(), addr=node.addr, name=node.name, elements=node.elements,
                                  fw=info.fw if info else None,
                                  board=info.caps.board if info and isinstance(info.caps.board, int) else None,
                                  fontpack_id=pack, flash_size=info.flash_size if info else None,
                                  configured=node.configured, gateway_hw_id=svc.gateway_hw_id)
            await svc.db.run(svc.db.upsert_bridge, record)
            if info is not None:
                svc.note_capacity(record.hw_id, bridge_capacity(info))

    async def build_inventory(self) -> dict[str, Any]:
        svc = self.svc
        await self.refresh_bridges()
        gateways = await svc.db.run(svc.db.list_gateways)
        bridges = await svc.db.run(svc.db.list_bridges)
        tags = await svc.db.run(svc.db.list_tags)
        floors = await svc.db.run(svc.store.epoch_floors)
        bridge_items = []
        for b in bridges:
            if b.addr is None:
                continue
            item: dict[str, Any] = {"hw_id": b.hw_id, "addr": b.addr, "fw": b.fw, "board": b.board,
                                    "fontpack_id": b.fontpack_id, "flash_size": b.flash_size}
            # Assignment-table capacity (CAPS_STATUS max_tags) and use, when the gateway has reported them
            # (Cremind keeps the last good values when they are left out).
            item.update({k: v for k, v in svc.bridge_capacity.get(b.hw_id, {}).items() if v is not None})
            bridge_items.append(item)
        return {
            "gateways": [{"hw_id": g.hw_id, "fw": g.fw, "board": g.board, "boot_id": g.boot_id, "port": g.port}
                         for g in gateways],
            "bridges": bridge_items,
            # epoch: the highest epoch used here, or the tag's epoch floor (a STALE_EPOCH's stored_epoch, §10)
            # when that is higher; Cremind keeps max(stored, reported) and assigns above it.
            "tags": [{"tag_id": t.hw_id, "board": t.board, "panel": t.panel, "width": t.width, "height": t.height,
                      "planes": t.planes, "fw": t.fw, "epoch": max(t.epoch, floors.get(t.tag_id, 0))} for t in tags],
        }

    # -- heartbeat -------------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        backoff = Backoff(1.0, self.svc.settings.connector_retry_max_s)
        while True:
            try:
                result = await self.client.heartbeat(await self.build_heartbeat())
                self.heartbeats += 1
                backoff.reset()
                delay = self.svc.settings.heartbeat_s
                if result.commands_pending:
                    log.debug("hardware: %d command(s) pending in Cremind", result.commands_pending)
            except ConnectorAuthError:
                raise
            except ConnectorTlsError as exc:
                await self._tls_pause(exc)
                continue
            except ConnectorError as exc:
                delay = min(backoff.next(), self.svc.settings.heartbeat_s)
                log.info("hardware: heartbeat failed (%s); retry in %.1fs", exc, delay)
            await asyncio.sleep(delay)

    async def build_heartbeat(self) -> dict[str, Any]:
        svc = self.svc
        stats = await svc.db.run(svc.store.queue_stats)
        views = {v.tag_id: v for v in await svc.db.run(svc.store.list_views)}
        telemetry = await svc.db.run(svc.store.device_status)
        in_flight = {r.tag_id for r in await svc.db.run(svc.store.list_revisions, None, 500)
                     if r.state in ("pending", "sent")}
        now = svc.clock()
        devices: list[dict[str, Any]] = []
        for tag in await svc.db.run(svc.db.list_tags):
            hw = tag_hw_id(tag.tag_id)
            view = views.get(tag.tag_id)
            seen = telemetry.get(hw, {})
            last_ts = seen.get("last_contact_ts")
            if view is not None and view.blocked_reason:
                status = "error"
            elif tag.tag_id in in_flight:
                status = "pending"
            elif last_ts is not None and now - last_ts <= OFFLINE_AFTER_S:
                status = "ok"
            else:
                status = "offline"
            item: dict[str, Any] = {"hw_id": hw, "kind": "tag", "status": status}
            for key in ("battery_mv", "rssi", "last_contact_at"):
                if seen.get(key) is not None:
                    item[key] = seen[key]
            if view is not None and view.displayed_revision:
                item["displayed_revision"] = view.displayed_revision
                if view.displayed_digest:
                    item["displayed_digest"] = view.displayed_digest
            devices.append(item)
        connected = svc.gateway is not None and svc.gateway.connected
        for bridge in await svc.db.run(svc.db.list_bridges):
            if bridge.addr is not None:
                devices.append({"hw_id": bridge.hw_id, "kind": "bridge", "status": "ok" if connected else "offline"})
        if svc.gateway_hw_id:
            devices.append({"hw_id": svc.gateway_hw_id, "kind": "gateway", "status": "ok" if connected else "offline"})
        return {"companion": {"version": __version__, "host": socket.gethostname(), "started_at": svc.started_at},
                "queue": {"depth": stats["depth"], "oldest_age_s": stats["oldest_age_s"]},
                "devices": devices}

    # -- commands --------------------------------------------------------------------------

    async def _command_loop(self) -> None:
        svc = self.svc
        for row in await svc.db.run(svc.store.unfinished_commands):
            if row.state == "running":
                log.info("hardware: resuming command %s %s", row.kind, row.command_id)
                self.executor.submit(row)
        backoff = Backoff(1.0, svc.settings.connector_retry_max_s)
        while True:
            try:
                await self._retry_claims()
                commands = await self.client.commands(wait=svc.settings.command_wait_s)
                backoff.reset()
            except ConnectorAuthError:
                raise
            except ConnectorTlsError as exc:
                await self._tls_pause(exc)
                continue
            except ConnectorError as exc:
                delay = backoff.next()
                log.info("hardware: commands poll failed (%s); retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                continue
            for command in commands:
                await self._take(command)
            if not commands and svc.settings.command_wait_s == 0:
                await asyncio.sleep(svc.settings.active_poll_s)

    async def _take(self, command: Command) -> None:
        svc = self.svc
        expires = command.expires_at.timestamp() if command.expires_at else None
        row = await svc.db.run(lambda: svc.store.command_begin(command.id, command.kind, command.args, expires))
        if row.state != "claiming":
            return
        await self._claim_and_run(row)

    async def _retry_claims(self) -> None:
        """Claim again every command still ``claiming`` here whose retry time has come."""
        now = self.svc.clock()
        for row in await self.svc.db.run(self.svc.store.unfinished_commands):
            if row.state != "claiming":
                continue
            _, next_try = self._claim_retry.get(row.command_id, (0, 0.0))
            if next_try <= now:
                await self._claim_and_run(row)

    async def _claim_and_run(self, row: CommandRow) -> None:
        svc = self.svc
        try:
            await self.client.claim(row.command_id)
        except ConnectorConflict as exc:
            raw = exc.body.get("command")
            current = raw if isinstance(raw, dict) else {}
            if current.get("status") != "claimed":  # finished, expired or cancelled meanwhile
                self._claim_retry.pop(row.command_id, None)
                await svc.db.run(svc.store.command_forget, row.command_id)
                return
            # claimed, and persisted here before the claim: it is ours (the answer to our claim was lost)
        except ConnectorNotFound:
            self._claim_retry.pop(row.command_id, None)
            await svc.db.run(svc.store.command_forget, row.command_id)
            return
        except (ConnectorAuthError, ConnectorTlsError):
            raise
        except ConnectorError as exc:
            failures = self._claim_retry.get(row.command_id, (0, 0.0))[0] + 1
            delay = Backoff.delay_for(failures - 1, 1.0, svc.settings.connector_retry_max_s)
            self._claim_retry[row.command_id] = (failures, svc.clock() + delay)
            log.info("hardware: claim of %s failed (%s); claiming again in %.1fs", row.command_id, exc, delay)
            return
        self._claim_retry.pop(row.command_id, None)
        self.commands_claimed += 1
        svc.crash.hit("command_claimed")
        await svc.db.run(svc.store.command_state, row.command_id, "running")
        self.executor.submit(row)


__all__ = ["HardwareWorker"]
