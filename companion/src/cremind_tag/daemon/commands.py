"""Hardware commands from Cremind (connector-api.md "Hardware commands"), executed resumably.

Every command is persisted before it is claimed (``commands`` table, state
``claiming``), so a crash after the claim still finds it. Each side-effecting
gateway request of a command is a *step* whose ``op_id`` is persisted (in the
command's ``progress`` and in ``gateway_ops``) BEFORE the request is sent; the
retained result (``EVT_ASSIGN_RESULT``, ``EVT_PROVISIONED``, ``EVT_RESULT`` of a
``TAG_COMMAND`` …) is committed by the event handler before it is ACKed. On
restart, or when no result came within the step's timeout, a step re-sends
with the SAME op id — the gateway answers a repeated op id from memory (§1.4)
and does no new work, so a tag away for a day gets one ``CLEAR``, not one per
timeout — or finds its result already recorded. Only a gateway reboot (new
``boot_id``: its memory and queue are gone) sends under a new op id. Every step
is idempotent at the device (assigning the same key again, removing an absent
assignment, clearing a white screen). An ``EVT_RESULT NOT_FOUND`` is retried
(§10) and fails the step only after ``NOT_FOUND_ESCALATE`` answers.

Commands touching one tag (``assign_tag`` → ``clear_tag`` of a claim, ``identify``,
``refresh_tag``) run one after another in arrival order; mesh changes
(scan/provision/configure/remove) are serialised too; everything else runs
concurrently. A command's outcome and its ``POST result`` are one transaction
(the outbox delivers the POST).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..connector.models import MalformedResponse, parse_tag_hw_id, tag_hw_id
from ..gateway.errors import GatewayError
from ..gateway.events import UnprovBeacon
from ..gateway.results import Ack, BridgeInfo
from ..protocol.ids import Status, TagCommand
from ..store.db import BridgeRecord, TagRecord, normalize_uuid
from .store import CommandRow, OpResult, status_name

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

TRANSIENT_STEP = frozenset({Status.TIMEOUT, Status.BUSY, Status.NO_RESOURCES, Status.DISCONNECTED,
                            Status.CONNECT_FAILED, Status.MESH_SUSPEND_FAILED, Status.MESH_RESUME_FAILED,
                            Status.PROVISIONING_ACTIVE, Status.INTERNAL, Status.INCOMPLETE})
TRANSIENT_ACK = frozenset({Status.BUSY, Status.NO_RESOURCES, Status.PROVISIONING_ACTIVE})
NOT_FOUND_ESCALATE = 3
"""``NOT_FOUND`` results of one step before it fails (§10: ambiguous in ``EVT_RESULT``)."""
OWNERSHIP_KINDS = frozenset({"assign_tag", "clear_tag", "run_operation"})
"""Run to completion even past their expiry; Cremind accepts a late ``succeeded`` (connector-api.md). A
``run_operation``'s operation has its own deadline and state in Cremind, which the agent follows."""

STEP_TIMEOUT_S = {"assign": 60.0, "unassign": 60.0, "provision": 180.0, "configure": 180.0, "remove": 90.0,
                  "clear": 900.0}
"""How long a step waits for its retained result before it is re-sent with a new op id."""


class CommandError(Exception):
    """The command failed; the message is reported to Cremind as the result's ``error`` (and ``result``,
    when given, as the result's ``result``)."""

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


class StepFailed(CommandError):
    """A step's gateway request answered a final status."""

    def __init__(self, message: str, status: Status | int) -> None:
        super().__init__(message)
        self.status = status


BRIDGE_FULL = "bridge_full"
"""``assign_tag`` error when the bridge's assignment table is full (``ASSIGN_SET NO_RESOURCES``):
Cremind marks the tag ``assign_failed``, drops it from that bridge and records ``max_tags``."""


class OpWaiters:
    """Wakes the step waiting for an op id when the event handler recorded its result."""

    def __init__(self) -> None:
        self._events: dict[int, asyncio.Event] = {}

    def event(self, op_id: int) -> asyncio.Event:
        return self._events.setdefault(op_id, asyncio.Event())

    def resolve(self, op_id: int) -> None:
        self.event(op_id).set()

    def forget(self, op_id: int) -> None:
        self._events.pop(op_id, None)


class CommandExecutor:
    """Runs claimed commands (see the module docstring)."""

    def __init__(self, svc: DaemonService, credential_id: str) -> None:
        self.svc = svc
        self.credential_id = credential_id
        self._chains: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.running: dict[str, str] = {}
        self.finished = 0

    @staticmethod
    def chain_key(row: CommandRow) -> str:
        args = row.args
        if row.kind in ("assign_tag", "clear_tag", "refresh_tag"):
            return f"tag:{str(args.get('tag_id', '')).upper()}"
        if row.kind == "identify":
            hw_id = str(args.get("hw_id", ""))
            return f"tag:{hw_id.upper()}" if not hw_id.startswith("br-") else f"bridge:{hw_id}"
        if row.kind in ("scan_unprovisioned", "provision_bridge", "configure_bridge", "remove_bridge"):
            return "mesh"
        if row.kind == "install_fontpack":
            return f"bridge:{args.get('bridge_hw_id')}"
        if row.kind == "run_operation":
            # Ownership changes one at a time; a discovery (a bounded scan) runs beside them.
            return "operation:discovery" if args.get("kind") == "discovery" else "operations"
        return f"command:{row.command_id}"

    def submit(self, row: CommandRow) -> asyncio.Task[None]:
        key = self.chain_key(row)
        previous = self._chains.get(key)
        task = asyncio.create_task(self._chained(previous, row), name=f"command {row.kind} {row.command_id}")
        self._chains[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _chained(self, previous: asyncio.Task[None] | None, row: CommandRow) -> None:
        if previous is not None and not previous.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(previous)
        await self._run(row)

    def idle(self) -> bool:
        return not self._tasks

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task

    async def _run(self, row: CommandRow) -> None:
        svc = self.svc
        self.running[row.command_id] = row.kind
        timeout = None if row.expires_ts is None else max(1.0, row.expires_ts - svc.clock())
        if row.kind in OWNERSHIP_KINDS:
            timeout = None  # finished late is better than never: Cremind accepts a late `succeeded`
        status, result, error = "failed", None, None
        log.info("command %s %s: start %s", row.kind, row.command_id, _safe_args(row))
        try:
            handler = getattr(self, f"_do_{row.kind}", None)
            if handler is None:
                raise CommandError(f"this companion does not know the command kind {row.kind!r}")
            result = await asyncio.wait_for(handler(row), timeout)
            status = "succeeded"
        except CommandError as exc:
            error, result = str(exc), exc.result
        except TimeoutError:
            error = "the command expired before it completed"
        except (KeyError, ValueError, TypeError, MalformedResponse) as exc:
            error = f"invalid arguments: {exc}"
        except asyncio.CancelledError:
            self.running.pop(row.command_id, None)
            raise  # the daemon is stopping: the command stays 'running' and resumes at the next start
        except Exception as exc:
            log.exception("command %s %s failed", row.kind, row.command_id)
            error = f"internal error: {type(exc).__name__}: {exc}"
        self.running.pop(row.command_id, None)
        await svc.db.run(lambda: svc.store.command_finish(row.command_id, status, credential_id=self.credential_id,
                                                          result=result, error=error))
        self.finished += 1
        log.info("command %s %s: %s%s", row.kind, row.command_id, status, f" ({error})" if error else "")
        svc.wake_outbox(self.credential_id)

    # -- plumbing ------------------------------------------------------------------------

    async def _gateway(self) -> Any:
        svc = self.svc
        if svc.gateway_url is None:
            raise CommandError("no gateway is configured on this companion (hardware.gateway_url)")
        while svc.gateway is None or not svc.gateway.connected:
            await asyncio.sleep(0.2)
        return svc.gateway

    async def _progress(self, row: CommandRow) -> dict[str, Any]:
        current = await self.svc.db.run(self.svc.store.get_command, row.command_id)
        return dict(current.progress) if current is not None else {}

    async def _save(self, row: CommandRow, **values: Any) -> dict[str, Any]:
        return await self.svc.db.run(lambda: self.svc.store.command_progress(row.command_id, **values))

    async def _step(self, row: CommandRow, step: str, send: Callable[[Any, int], Awaitable[Ack]], *,
                    timeout: float | None = None, attempts: int | None = None,
                    final: frozenset[Status] = frozenset()) -> OpResult:
        """Run one side-effecting gateway request to its retained result (module docstring); a retained
        status in ``final`` fails the step at once even when it is otherwise retried (:class:`StepFailed`)."""
        svc = self.svc
        timeout = timeout or STEP_TIMEOUT_S.get(step, 120.0)
        key = f"op:{step}"
        tries = 0
        not_found = 0
        delay = svc.settings.retry_initial_s
        while True:
            progress = await self._progress(row)
            op_id = progress.get(key)
            if op_id is not None:
                done = await svc.db.run(svc.store.op_result, int(op_id))
                if done is not None:
                    if done.status == Status.OK:
                        return done
                    status = _status(done.status)
                    if status == Status.NOT_FOUND:
                        not_found += 1
                    if status not in final and (
                            (status in TRANSIENT_STEP and (attempts is None or tries < attempts))
                            or (status == Status.NOT_FOUND and not_found < NOT_FOUND_ESCALATE)):
                        tries += 1
                        log.info("command %s: step %s answered %s; retrying", row.command_id, step,
                                 status_name(done.status))
                        await self._save(row, **{key: None})
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, svc.settings.retry_max_s)
                        continue
                    raise StepFailed(f"{step} answered {status_name(done.status)}", status)
            gateway = await self._gateway()
            if op_id is None:
                op_id = svc.op_ids.next()
                await svc.db.run(svc.store.op_begin, op_id, step, row.command_id, gateway.boot_id)
                await self._save(row, **{key: op_id})
            generation = svc.boot_generation
            try:
                ack = await send(gateway, int(op_id))
            except GatewayError as exc:
                log.info("command %s: step %s not sent (%s)", row.command_id, step, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, svc.settings.retry_max_s)
                continue
            if not ack.ok:
                status = _status(ack.status)
                if status in TRANSIENT_ACK:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, svc.settings.retry_max_s)
                    continue
                raise CommandError(f"{ack.msg.name} answered {status_name(ack.status)}"
                                   + (f": {ack.text}" if ack.text else ""))
            done = await self._wait_op(int(op_id), generation, timeout)
            if done is None:
                if svc.boot_generation != generation:  # the gateway rebooted: its op memory is gone
                    log.info("command %s: step %s lost in a gateway reboot; sending again", row.command_id, step)
                    await self._save(row, **{key: None})
                else:  # still waiting (a tag away): ask again with the same op id, which does no new work
                    log.info("command %s: step %s has no result yet; still waiting", row.command_id, step)

    async def _wait_op(self, op_id: int, generation: int, timeout: float) -> OpResult | None:
        svc = self.svc
        event = svc.ops.event(op_id)
        deadline = svc.clock() + timeout
        try:
            while True:
                event.clear()  # before the read: a result recorded after it sets the event again
                done = await svc.db.run(svc.store.op_result, op_id)
                if done is not None:
                    return done
                if svc.boot_generation != generation or svc.clock() >= deadline:
                    return None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), min(2.0, max(0.05, deadline - svc.clock())))
        finally:
            svc.ops.forget(op_id)

    async def _max_tags(self, bridge: BridgeRecord) -> int | None:
        """The bridge's ``max_tags`` (CAPS) as the gateway reports it now, else the last one seen."""
        svc = self.svc
        gateway = svc.gateway
        if gateway is not None and gateway.connected:
            try:
                for info in await asyncio.wait_for(gateway.get_inventory(), 10):
                    if info.addr == bridge.addr:
                        svc.note_capacity(bridge.hw_id, bridge_capacity(info))
                        break
            except (GatewayError, TimeoutError) as exc:
                log.info("command: bridge inventory unavailable (%s)", exc)
        return svc.bridge_capacity.get(bridge.hw_id, {}).get("max_tags")

    async def _tag(self, hw_id: Any) -> TagRecord:
        tag_id = parse_tag_hw_id(str(hw_id).upper())
        tag = await self.svc.db.run(self.svc.db.find_tag, tag_id)
        if tag is None:
            raise CommandError(f"tag {tag_hw_id(tag_id)} is not enrolled on this companion")
        return tag

    async def _bridge(self, hw_id: Any) -> BridgeRecord:
        text = str(hw_id)
        if not text.startswith("br-"):
            raise CommandError(f"not a bridge id: {text!r}")
        uuid = normalize_uuid(text[3:])
        bridge = await self.svc.db.run(lambda: self.svc.db.find_bridge(uuid=uuid))
        if bridge is not None and bridge.addr is not None:
            return bridge
        gateway = await self._gateway()
        for node in await gateway.list_nodes():
            if node.uuid.hex() == uuid:
                record = BridgeRecord(uuid, addr=node.addr, name=node.name, elements=node.elements,
                                      configured=node.configured, gateway_hw_id=self.svc.gateway_hw_id)
                return await self.svc.db.run(self.svc.db.upsert_bridge, record)
        raise CommandError(f"bridge {text} is not in this gateway's mesh")

    # -- tag commands ----------------------------------------------------------------------

    async def _do_assign_tag(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        tag = await self._tag(row.args["tag_id"])
        epoch = int(row.args["epoch"])
        bridge = await self._bridge(row.args["bridge_hw_id"])
        assert bridge.addr is not None
        addr = bridge.addr
        progress = await self._progress(row)
        if "previous" not in progress:
            progress = await self._save(row, previous=[tag.bridge_addr, tag.epoch])
        if epoch < tag.epoch and not progress.get("assigned"):
            svc.request_inventory()  # reports the epoch this companion used; Cremind re-queues above it
            raise CommandError(f"epoch {epoch} is older than epoch {tag.epoch} this companion already used for "
                               f"tag {tag.hw_id}; the inventory now reports it")
        view = await svc.db.run(svc.store.get_view, tag.tag_id)
        floor = view.epoch_floor if view is not None else 0
        if epoch <= floor and not progress.get("assigned"):
            svc.request_inventory()  # reports the floor; Cremind re-queues above it
            raise CommandError(f"epoch {epoch} is not above epoch {floor}, which tag {tag.hw_id} reported as its "
                               "stored epoch (STALE_EPOCH); the inventory now reports it")
        if not progress.get("assigned") and (tag.epoch, tag.bridge_addr) == (epoch, bridge.addr):
            progress = await self._save(row, assigned=True)  # already done (an earlier copy of this command)
        if not progress.get("assigned"):
            if svc.secrets is None:
                raise CommandError("no secret store is available to derive K_epoch")
            key = await asyncio.to_thread(svc.secrets.k_epoch, tag.tag_id, epoch, tag.secret_ref)
            try:
                await self._step(row, "assign", lambda gw, op: gw.assign_tag(addr, tag.tag_id, epoch, key, op_id=op),
                                 final=frozenset({Status.NO_RESOURCES}))
            except StepFailed as exc:
                if exc.status != Status.NO_RESOURCES:
                    raise
                # ASSIGN_SET NO_RESOURCES: the bridge's assignment table is full (CAPS max_tags). Retrying
                # cannot help; Cremind marks the tag assign_failed and an admin picks another bridge.
                max_tags = await self._max_tags(bridge)
                log.warning("command %s: bridge %s is full (max_tags %s): tag %s not assigned", row.command_id,
                            bridge.hw_id, max_tags, tag.hw_id)
                svc.request_inventory()
                result: dict[str, Any] = {"error": BRIDGE_FULL}
                if max_tags is not None:
                    result["max_tags"] = max_tags
                raise CommandError(BRIDGE_FULL, result=result) from None
            await svc.db.run(lambda: svc.store.on_assigned(tag.tag_id, epoch=epoch, bridge_addr=addr,
                                                           bridge_hw_id=bridge.hw_id))
            progress = await self._save(row, assigned=True)
        previous_addr, previous_epoch = progress.get("previous") or [None, 0]
        unassigned = None
        if previous_addr and previous_addr != bridge.addr and previous_epoch:
            try:
                await self._step(row, "unassign", lambda gw, op: gw.unassign_tag(previous_addr, tag.tag_id,
                                                                                 previous_epoch, op_id=op),
                                 attempts=3)
                unassigned = "OK"
            except CommandError as exc:
                unassigned = f"failed: {exc}"
                log.warning("command %s: removing the old key from bridge %#06x failed: %s", row.command_id,
                            previous_addr, exc)
        svc.request_inventory()
        svc.wake_scheduler()
        return {"tag_id": tag.hw_id, "bridge_hw_id": bridge.hw_id, "bridge_addr": bridge.addr, "epoch": epoch,
                "previous_bridge_addr": previous_addr, "unassign_previous": unassigned}

    async def _do_clear_tag(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        tag = await self._tag(row.args["tag_id"])
        epoch = int(row.args["epoch"])
        if tag.epoch > epoch:
            svc.request_inventory()
            raise CommandError(f"the tag is already at epoch {tag.epoch} (> {epoch}); the inventory reports it")
        if tag.epoch < epoch or not tag.bridge_addr:
            raise CommandError(f"the tag is assigned at epoch {tag.epoch}, not {epoch}: assign_tag must succeed first")
        if await svc.db.run(svc.store.cleared_at, tag.tag_id, epoch):
            # Cremind re-queued a clear this companion already did (its result was late): nothing to send.
            return {"tag_id": tag.hw_id, "epoch": epoch, "revision": 0, "already_cleared": True}
        bridge = tag.bridge_addr
        result = await self._step(row, "clear", lambda gw, op: gw.tag_command(
            bridge=bridge, tag_id=tag.tag_id, epoch=epoch, cmd=TagCommand.CLEAR, op_id=op))
        await svc.db.run(svc.store.on_cleared, tag.tag_id, epoch)
        svc.wake_scheduler()
        return {"tag_id": tag.hw_id, "epoch": epoch, "revision": 0, "digest": result.result.get("digest")}

    async def _wait_revision(self, row: CommandRow, tag: TagRecord, purposes: list[str],
                             started: float) -> dict[str, Any]:
        svc = self.svc
        while True:
            view = await svc.db.run(svc.store.get_view, tag.tag_id)
            if view is not None and view.blocked_reason:
                raise CommandError(f"tag {tag.hw_id} is blocked: {view.blocked_reason}")
            rev = await svc.db.run(lambda: svc.store.latest_revision(tag.tag_id, purposes=purposes,
                                                                     since_ts=started))
            if rev is not None and rev.state == "displayed":
                return {"tag_id": tag.hw_id, "revision": rev.revision, "digest": rev.frame_digest}
            if rev is not None and rev.state in ("failed", "uncertain"):
                raise CommandError(f"revision {rev.revision} {rev.state}: {rev.last_status} {rev.detail or ''}")
            await asyncio.sleep(0.5)

    async def _do_identify(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        hw_id = str(row.args["hw_id"])
        if hw_id.startswith("br-"):
            bridge = await self._bridge(hw_id)
            gateway = await self._gateway()
            ack = await gateway.identify_node(bridge.addr, op_id=svc.op_ids.next())
            if not ack.ok:
                raise CommandError(f"IDENTIFY_NODE answered {status_name(ack.status)}")
            return {"hw_id": hw_id, "addr": bridge.addr, "status": status_name(ack.status)}
        tag = await self._tag(hw_id)
        progress = await self._progress(row)
        started = progress.get("started_ts")
        if started is None:
            started = svc.clock()
            await self._save(row, started_ts=started)
            # Hold the identify screen while it is delivered; the hold restarts once it is displayed.
            await svc.db.run(svc.store.set_override, tag.tag_id, "identify", started + 24 * 3600)
            svc.wake_scheduler()
        try:
            result = await self._wait_revision(row, tag, ["identify"], float(started))
        except BaseException:
            await svc.db.run(svc.store.set_override, tag.tag_id, None, None)
            svc.wake_scheduler()
            raise
        await svc.db.run(lambda: svc.store.set_override(tag.tag_id, "identify",
                                                        svc.clock() + svc.settings.identify_hold_s, force=False))
        svc.wake_scheduler()
        return result

    async def _do_refresh_tag(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        tag = await self._tag(row.args["tag_id"])
        progress = await self._progress(row)
        started = progress.get("started_ts")
        if started is None:
            started = svc.clock()
            await self._save(row, started_ts=started)
            await svc.db.run(lambda: svc.store.mark_dirty(tag.tag_id, force=True))
            svc.wake_scheduler()
        return await self._wait_revision(row, tag, ["screen", "refresh", "blank"], float(started))

    # -- mesh commands ------------------------------------------------------------------------

    async def _do_scan_unprovisioned(self, row: CommandRow) -> dict[str, Any]:
        duration = max(1, min(int(row.args.get("duration_s", 60)), 600))
        gateway = await self._gateway()
        beacons: dict[str, dict[str, Any]] = {}
        with gateway.subscribe(types=UnprovBeacon) as sub:
            ack = await gateway.scan_unprov(duration)
            if not ack.ok:
                raise CommandError(f"SCAN_UNPROV answered {status_name(ack.status)}")
            deadline = self.svc.clock() + duration + 2
            while (remaining := deadline - self.svc.clock()) > 0:
                try:
                    event = await sub.get(timeout=remaining)
                except TimeoutError:
                    break
                assert isinstance(event, UnprovBeacon)
                uuid = event.uuid.hex()
                seen = beacons.get(uuid)
                if seen is None or event.rssi > seen["rssi"]:
                    beacons[uuid] = {"uuid": uuid, "hw_id": f"br-{uuid}", "rssi": event.rssi, "oob": event.oob}
        return {"duration_s": duration, "beacons": sorted(beacons.values(), key=lambda b: -b["rssi"])[:40]}

    async def _do_provision_bridge(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        uuid = normalize_uuid(str(row.args["uuid"]).removeprefix("br-"))
        name = str(row.args.get("name") or "")
        gateway = await self._gateway()
        node = next((n for n in await gateway.list_nodes() if n.uuid.hex() == uuid), None)
        progress = await self._progress(row)
        if node is not None:
            addr, configured = node.addr, node.configured
        elif progress.get("addr"):
            addr, configured = int(progress["addr"]), False
        else:
            result = await self._step(row, "provision", lambda gw, op: gw.provision(bytes.fromhex(uuid), name or None,
                                                                                    op_id=op))
            addr, configured = int(result.result["addr"]), False
            await self._save(row, addr=addr)
        record = BridgeRecord(uuid, addr=addr, name=name or (node.name if node else ""), configured=configured,
                              gateway_hw_id=svc.gateway_hw_id)
        await svc.db.run(svc.db.upsert_bridge, record)
        if not configured:
            await self._step(row, "configure", lambda gw, op: gw.configure_node(addr, op_id=op))
            await svc.db.run(lambda: svc.db.update_bridge(uuid, configured=True))
        svc.request_inventory()
        return {"hw_id": f"br-{uuid}", "addr": addr, "name": record.name, "configured": True}

    async def _do_configure_bridge(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        bridge = await self._bridge(row.args["hw_id"])
        addr = bridge.addr
        await self._step(row, "configure", lambda gw, op: gw.configure_node(addr, op_id=op))
        await svc.db.run(lambda: svc.db.update_bridge(bridge.uuid, configured=True))
        svc.request_inventory()
        return {"hw_id": bridge.hw_id, "addr": addr, "configured": True}

    async def _do_remove_bridge(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        hw_id = str(row.args["hw_id"])
        uuid = normalize_uuid(hw_id.removeprefix("br-"))
        existing = await svc.db.run(lambda: svc.db.find_bridge(uuid=uuid))
        progress = await self._progress(row)
        if existing is None and progress.get("removed"):
            return {"hw_id": hw_id, "removed": True}
        bridge = await self._bridge(hw_id)
        addr = bridge.addr
        await self._step(row, "remove", lambda gw, op: gw.remove_node(addr, op_id=op))
        await svc.db.run(lambda: svc.db.delete_bridge(uuid=uuid))
        await self._save(row, removed=True)
        svc.request_inventory()
        return {"hw_id": hw_id, "addr": addr, "removed": True}

    async def _do_install_fontpack(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        hw_id = str(row.args["bridge_hw_id"])
        port = svc.settings.maintenance_port(hw_id)
        if port is None and svc.bridge_url is not None:
            bridges = await svc.db.run(svc.db.list_bridges)
            if len(bridges) == 1 and bridges[0].hw_id == hw_id:
                port = svc.bridge_url
        pack_path = svc.fontpack_path
        if port is None:
            raise CommandError(
                f"no maintenance port is configured for {hw_id}: connect the bridge's USB maintenance port to this "
                f"PC and run `cremind-tag bridge fonts-install {pack_path or '<pack.ctfp>'} --url <port>`, or set "
                f"`daemon.bridge_maintenance = [\"{hw_id}=<port>\"]` and queue install_fontpack again")
        if pack_path is None:
            raise CommandError("no font pack is configured on this companion (hardware.fontpack or --pack)")
        from ..bridge_maint import BridgeMaintClient

        data = await asyncio.to_thread(pack_path.read_bytes)
        async with BridgeMaintClient(port, name="cremind-tag daemon") as maint:
            result = await maint.font_install(data)
        pack_hex = result.fontpack_id.hex()
        bridge = await svc.db.run(lambda: svc.db.find_bridge(uuid=normalize_uuid(hw_id.removeprefix("br-"))))
        if bridge is not None:
            await svc.db.run(lambda: svc.db.update_bridge(bridge.uuid, fontpack_id=pack_hex))
        await svc.db.run(svc.store.unblock_all, "fontpack_mismatch")
        svc.request_inventory()
        svc.wake_scheduler()
        return {"bridge_hw_id": hw_id, "fontpack_id": pack_hex, "slot": result.slot, "skipped": result.skipped}

    async def _do_run_operation(self, row: CommandRow) -> dict[str, Any]:
        """A Cremind Connect operation (docs/connect-setup.md §9.4), run by the worker's agent."""
        agent = self.svc.agent
        if agent is None:
            raise CommandError("this companion does not run Cremind Connect operations (a manual setup)")
        return await agent.run_operation(row, self)

    async def _do_collect_diagnostics(self, row: CommandRow) -> dict[str, Any]:
        svc = self.svc
        stats = await svc.db.run(svc.store.queue_stats)
        out: dict[str, Any] = {
            "companion": {"version": __version__, "host": socket.gethostname(), "started_at": svc.started_at},
            "fontpack_id": svc.fonts.pack_id.hex() if svc.fonts is not None else None,
            "queue": {k: stats[k] for k in ("depth", "oldest_age_s", "jobs", "revisions", "outbox")},
            "blocked": stats["blocked"],
            "credentials": {cid: w.state for cid, w in svc.content_workers.items()},
        }
        gateway = svc.gateway
        if gateway is not None and gateway.connected:
            try:
                info = await gateway.info()
                out["gateway"] = {"fw": info.fw, "build": info.build, "boot_id": info.boot_id,
                                  "counters": dict(list(info.counters.items())[:40])}
                out["bridges"] = [{"addr": b.addr, "fw": b.fw, "fontpack_id": b.fontpack_id.hex()
                                   if b.fontpack_id else None, "assigned": len(b.assigned)}
                                  for b in await gateway.get_inventory()][:20]
            except GatewayError as exc:
                out["gateway"] = {"error": str(exc)}
        else:
            out["gateway"] = {"connected": False}
        return out


def bridge_capacity(info: BridgeInfo) -> dict[str, int | None]:
    """``max_tags`` and ``assigned`` of one ``GET_INVENTORY`` item or ``EVT_BRIDGE_INFO``: both from the
    bridge's ``CAPS_STATUS`` as the gateway caches it (its caps map's ``max_tags`` and ``assigned_count``);
    ``assigned`` falls back to the assignments the gateway holds for the bridge when the caps lack the count
    (an older gateway), which misses tags assigned to the bridge behind the gateway's back."""
    max_tags = info.caps.max_tags
    valid = isinstance(max_tags, int) and 1 <= max_tags <= 255
    count = info.caps.assigned_count
    assigned = count if isinstance(count, int) and 0 <= count <= 255 else min(len(info.assigned), 255)
    return {"max_tags": max_tags if valid else None, "assigned": assigned}

def _status(value: int) -> Status | int:
    try:
        return Status(value)
    except ValueError:
        return value


def _safe_args(row: CommandRow) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(row.args.items()) if k in (
        "tag_id", "epoch", "bridge_hw_id", "hw_id", "uuid", "duration_s"))


__all__ = ["BRIDGE_FULL", "CommandError", "CommandExecutor", "OpWaiters", "StepFailed", "bridge_capacity"]
