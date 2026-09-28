"""A Cremind Connect worker's agent (docs/connect-setup.md §8, §9.3, §9.4, §10).

The worker is an ordinary companion daemon
(:class:`~cremind_tag.daemon.service.DaemonService`) whose gateway link is a
protocol v2 secure session pinned to its gateway. The agent adds what only a
private worker does:

- **Lease** — ``POST lease`` every ``renew_s`` (20 s). Without a lease younger
  than its TTL (60 s) the worker starts no new operation; the lease answer
  also says whether Cremind paused the gateway (no new pairings) or is
  removing it.
- **Operations** — ``run_operation {operation_id, kind}`` commands (from the
  ordinary command queue) run here: ``claim_gateway``, ``discovery``,
  ``pair_bridge``, ``pair_tag``, ``move_tag``, ``unpair``, ``release_gateway``,
  ``recover_gateway``. Each follows the durability rule — stage locally and in
  the vault, change the device under a durable op id, reconcile, commit, report
  — so a crash or a lost answer at any point resumes into the same outcome
  (``STATUS`` tells whether a grant already took effect). Grants come from
  Cremind (``POST grants``) for exactly the device, generation and challenge
  the device just reported.
- **Heartbeat generations** — every device the worker has talked to is
  reported with its live generation, which lets Cremind lift a restore's
  ``reconciling`` hold.

Keys: bridge maintenance keys (``mk``) and tag roots live in the worker's
owner-only secret store; the vault holds a copy for a recovery on another
computer. Nothing here logs a key, a setup secret or a grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..connector.client import (
    ConnectorAuthError,
    ConnectorClient,
    ConnectorConflict,
    ConnectorError,
    ConnectorNotFound,
    ConnectorRejected,
)
from ..gateway.errors import GatewayError, StatusError
from ..gateway.results import to_status
from ..gateway.events import Discovered, UnprovBeacon
from ..gateway.tunnel import Tunnel, TunnelError
from ..protocol.ids import NodeRole, OwnerState, SerialMsg, Status, TagCommand
from ..secure import identity
from ..secure.codes import SetupPayload
from ..store.db import BridgeRecord, TagRecord

if TYPE_CHECKING:
    from ..daemon.commands import CommandExecutor
    from ..daemon.service import DaemonService
    from ..daemon.store import CommandRow

log = logging.getLogger(__name__)

FINAL = frozenset({"succeeded", "failed", "cancelled"})
REMOVAL_KINDS = frozenset({"unpair", "release_gateway"})
BUSINESS_403 = frozenset({"grant_refused", "not_current_worker", "no_recovery", "legacy_companion"})
STATE_FILE = "agent.json"
LEASE_RETRY_S = 5.0
TUNNEL_S = 90
TAG_WAKE_S = 120.0
BRIDGE_OPEN_S = 30.0
DISCOVERY_POLL_S = 2.0
DISCOVER_EVERY_S = 10.0
"""How often a tag search asks each bridge again to listen (a DISCOVER may be lost in the mesh)."""
PENDING_RETRY_S = 60.0
STALE_EPOCH_RETRIES = 4
SHOW_CODE_S = 15 * 60.0


class OperationFailed(Exception):
    """The operation ends ``failed`` with ``code`` (docs/connect-setup.md §12) and a sentence for people."""

    def __init__(self, code: str, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class OperationCancelled(Exception):
    """Cremind cancelled the operation (or it ended) while it ran."""


class WorkerRemoved(Exception):
    """Cremind revoked this worker (its gateway was removed, or a recovery moved it elsewhere)."""


@dataclass(frozen=True)
class WorkerIdentity:
    """What the worker knows about itself (``worker.json``)."""

    companion_id: str
    profile_id: str
    authority_pub: bytes
    gateway_device_id: bytes
    gateway_ik: bytes
    operation: str = "connect_gateway"

    @property
    def authority_id(self) -> bytes:
        return identity.authority_id(self.authority_pub)

    @property
    def owner(self) -> bytes:
        import uuid

        try:
            return uuid.UUID(self.profile_id).bytes
        except ValueError:
            import hashlib

            return hashlib.sha256(self.profile_id.encode()).digest()[:16]

    @property
    def gateway_hw_id(self) -> str:
        return f"gw-{self.gateway_device_id.hex()}"


class AgentState:
    """``agent.json``: devices this worker owns (no secrets) and its vault versions; written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.devices: dict[str, dict[str, Any]] = {}
        self.vault_versions: dict[str, int] = {}
        self.load()

    def load(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("agent: %s unreadable (%s); starting empty", self.path, exc)
            return
        self.devices = {str(k): dict(v) for k, v in (doc.get("devices") or {}).items() if isinstance(v, dict)}
        self.vault_versions = {str(k): int(v) for k, v in (doc.get("vault_versions") or {}).items()}

    def save(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"devices": self.devices, "vault_versions": self.vault_versions}, indent=1,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def device(self, device_id: str) -> dict[str, Any] | None:
        return self.devices.get(device_id)

    def put(self, device_id: str, **fields: Any) -> dict[str, Any]:
        entry = self.devices.setdefault(device_id, {})
        entry.update(fields)
        self.save()
        return entry

    def forget(self, device_id: str) -> None:
        if self.devices.pop(device_id, None) is not None:
            self.save()

    def by_hw_id(self, hw_id: str) -> tuple[str, dict[str, Any]] | None:
        for device_id, entry in self.devices.items():
            if entry.get("hw_id") == hw_id:
                return device_id, entry
        return None


def _is_business_refusal(exc: ConnectorAuthError) -> bool:
    return exc.status == 403 and exc.code in BUSINESS_403


class ConnectAgent:
    """See the module docstring."""

    def __init__(self, svc: DaemonService, ident: WorkerIdentity, controller_priv: bytes, client: ConnectorClient,
                 directory: Path, *, on_removed: Callable[[], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.svc = svc
        self.ident = ident
        self.controller_priv = controller_priv
        self.controller_pub = identity.x25519_public(controller_priv)
        self.client = client
        self.directory = Path(directory)
        self.state = AgentState(self.directory / STATE_FILE)
        self.on_removed = on_removed
        self.clock = clock
        self.lease_until = 0.0
        self.renew_s = 20.0
        self.discover_every_s = DISCOVER_EVERY_S
        self.worker_state = "active"
        self.paused = False
        self.removed = False
        self._lease_changed = asyncio.Event()
        self.state.devices.setdefault(ident.gateway_device_id.hex(), {"role": "gateway", "hw_id": ident.gateway_hw_id})

    # ------------------------------------------------------------------ lease

    def lease_valid(self) -> bool:
        return self.clock() < self.lease_until and not self.removed

    async def run(self) -> None:
        """The lease loop (runs until cancelled or the worker is removed)."""
        while not self.removed:
            try:
                await self.renew_lease()
                delay = self.renew_s
            except WorkerRemoved:
                await self._removed("the worker's credential was revoked")
                return
            except ConnectorError as exc:
                log.info("agent: lease not renewed (%s); again in %.0f s", exc, LEASE_RETRY_S)
                delay = LEASE_RETRY_S
            await asyncio.sleep(delay)

    async def renew_lease(self) -> dict[str, Any]:
        try:
            answer = await self.client.lease()
        except ConnectorAuthError as exc:
            if _is_business_refusal(exc):
                raise ConnectorRejected(str(exc), status=exc.status, code=exc.code) from None
            raise WorkerRemoved(str(exc)) from None
        ttl = float(answer.get("ttl_s") or 60)
        self.renew_s = max(5.0, min(float(answer.get("renew_s") or 20), ttl / 2))
        self.lease_until = self.clock() + ttl - 2.0
        self.worker_state = str(answer.get("state") or "active")
        self.paused = bool(answer.get("paused"))
        self._lease_changed.set()
        return answer

    async def wait_lease(self, timeout: float = 90.0) -> None:
        deadline = self.clock() + timeout
        while not self.lease_valid():
            if self.removed:
                raise WorkerRemoved("this worker was removed")
            try:
                await self.renew_lease()
                continue
            except ConnectorError as exc:
                if self.clock() >= deadline:
                    raise OperationFailed("gateway_offline", f"Cremind did not renew this worker's lease ({exc}).") \
                        from None
            await asyncio.sleep(LEASE_RETRY_S)

    async def _removed(self, why: str) -> None:
        if self.removed:
            return
        self.removed = True
        log.warning("agent: %s; this worker stops", why)
        if self.on_removed is not None:
            await self.on_removed()

    # ------------------------------------------------------------------ heartbeat

    async def decorate_heartbeat(self, devices: list[dict[str, Any]]) -> None:
        """Add ``gen`` (live generation) to the devices this worker has talked to."""
        gens = {entry.get("hw_id"): entry.get("gen") for entry in self.state.devices.values()}
        for item in devices:
            gen = gens.get(item.get("hw_id"))
            if isinstance(gen, int):
                item["gen"] = gen

    # ------------------------------------------------------------------ operations

    async def run_operation(self, row: CommandRow, executor: CommandExecutor) -> dict[str, Any]:
        op_id = str(row.args["operation_id"])
        kind = str(row.args.get("kind") or "")
        op = await self._operation(op_id)
        if op.get("state") in FINAL:
            return {"operation_id": op_id, "skipped": op.get("state")}
        kind = str(op.get("kind") or kind)
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            await self._report(op_id, state="failed", error={"code": "unsupported",
                                                             "message": f"This worker cannot run {kind}."})
            return {"operation_id": op_id, "error": "unsupported"}
        ctx = OpContext(self, op, row, executor)
        try:
            await self.wait_lease()
            if self.paused and kind not in REMOVAL_KINDS and kind != "recover_gateway":
                raise OperationFailed("gateway_paused", "The gateway is paused; resume it in Cremind first.")
            result = await handler(ctx)
        except OperationCancelled:
            log.info("agent: operation %s (%s) cancelled", op_id, kind)
            with contextlib.suppress(Exception):
                await ctx.cleanup()
            return {"operation_id": op_id, "cancelled": True}
        except OperationFailed as exc:
            log.warning("agent: operation %s (%s) failed: %s (%s)", op_id, kind, exc.code, exc)
            with contextlib.suppress(Exception):
                await ctx.cleanup()
            with contextlib.suppress(ConnectorError):
                await self._report(op_id, state="failed", error={"code": exc.code, "message": str(exc)},
                                   result=exc.result)
            return {"operation_id": op_id, "error": exc.code}
        except WorkerRemoved as exc:
            await self._removed(str(exc))
            return {"operation_id": op_id, "error": "removed"}
        return {"operation_id": op_id, **(result or {})}

    async def _operation(self, op_id: str) -> dict[str, Any]:
        try:
            return await self.client.operation(op_id)
        except ConnectorNotFound:
            return {"id": op_id, "state": "cancelled"}
        except ConnectorAuthError as exc:
            if _is_business_refusal(exc):
                return {"id": op_id, "state": "cancelled"}
            raise WorkerRemoved(str(exc)) from None

    async def _report(self, op_id: str, **fields: Any) -> dict[str, Any]:
        try:
            return await self.client.progress(op_id, **fields)
        except ConnectorAuthError as exc:
            if _is_business_refusal(exc):
                raise OperationCancelled() from None
            raise WorkerRemoved(str(exc)) from None
        except ConnectorNotFound:
            raise OperationCancelled() from None

    async def grant(self, ctx: OpContext, op: str, device_id: bytes, role: str, gen_from: int, challenge: bytes,
                    ik: bytes | None = None) -> tuple[bytes, bytes]:
        try:
            grant, sig, authority_pub = await self.client.grant(operation_id=ctx.op_id, op=op, device_id=device_id,
                                                                role=role, gen_from=gen_from, challenge=challenge,
                                                                ik=ik)
        except ConnectorAuthError as exc:
            if _is_business_refusal(exc):
                raise OperationFailed("grant_refused", "Cremind did not authorise this change.") from None
            raise WorkerRemoved(str(exc)) from None
        except ConnectorConflict as exc:
            code = exc.code or "device_owned"
            raise OperationFailed(code, _conflict_message(code)) from None
        except ConnectorError as exc:
            raise OperationFailed("grant_refused", f"Cremind did not answer the grant request ({exc}).") from None
        if authority_pub != self.ident.authority_pub:
            raise OperationFailed("grant_refused", "The grant came from another authority.")
        return grant, sig

    async def vault_write(self, subject: str, state: dict[str, Any], *, stage: str, generation: int) -> None:
        """Compare-and-set one vault version (retried once on a lost answer or a stale local version)."""
        for _attempt in range(3):
            expected = self.state.vault_versions.get(subject)
            try:
                version = await self.client.vault_put(subject, state, stage=stage, generation=generation,
                                                      expected_version=expected)
            except ConnectorConflict as exc:
                current = exc.body.get("version") if isinstance(exc.body, dict) else None
                self.state.vault_versions[subject] = int(current) if isinstance(current, int) else 0
                if current is None:
                    self.state.vault_versions.pop(subject, None)
                self.state.save()
                continue
            except ConnectorAuthError as exc:
                if _is_business_refusal(exc):
                    raise OperationFailed("not_current_worker", "Cremind refused this worker's recovery data.") \
                        from None
                raise WorkerRemoved(str(exc)) from None
            self.state.vault_versions[subject] = version
            self.state.save()
            return
        raise OperationFailed("vault_conflict", "The recovery data changed meanwhile; try again.")

    # -- shared device steps --------------------------------------------------------------------

    async def gateway(self) -> Any:
        svc = self.svc
        while svc.gateway is None or not svc.gateway.connected:
            await asyncio.sleep(0.2)
        return svc.gateway

    def _check_owner_status(self, st: dict[str, Any]) -> bool:
        """``STATUS`` says the device is owned by this authority (and by this profile when it tells: a device
        names its owner only to its pinned controller; a grant for another owner fails on the device anyway)."""
        owner = st.get("owner")
        return (st.get("owner_state") == OwnerState.OWNED
                and bytes(st.get("authority_id") or b"") == self.ident.authority_id
                and (owner is None or bytes(owner) == self.ident.owner))

    async def open_tunnel(self, ctx: OpContext, *, bridge_addr: int, tag_id: int, wait_s: float,
                          expect: bytes | None = None, role: NodeRole) -> Tunnel:
        gw = await self.gateway()
        try:
            tunnel = await Tunnel.open(gw, bridge=bridge_addr, tag_id=tag_id, duration_s=TUNNEL_S)
        except StatusError as exc:
            raise TunnelError(f"TUNNEL_OPEN answered {exc}", exc.status) from None
        except GatewayError as exc:
            raise OperationFailed("gateway_offline", f"The gateway could not open a tunnel ({exc}).") from None
        try:
            ident = await tunnel.wait_open(wait_s)
            if ident.role != role or (expect is not None and ident.device_id != expect) or \
                    identity.device_id(ident.role, ident.ik) != ident.device_id:
                raise OperationFailed("not_found", "Another device answered.")
            if ident.proto < 2:
                raise OperationFailed("v1_firmware", "This device needs a firmware update.")
            await tunnel.handshake(self.controller_priv)
        except BaseException:
            await tunnel.close()
            raise
        return tunnel

    # ------------------------------------------------------------------ claim_gateway

    async def _op_claim_gateway(self, ctx: OpContext) -> dict[str, Any]:
        args = ctx.args
        device = bytes.fromhex(str(args.get("device_id") or self.ident.gateway_device_id.hex()))
        if device != self.ident.gateway_device_id:
            raise OperationFailed("wrong_gateway", "This worker drives another gateway.")
        await ctx.progress(stage="claiming")
        gw = await self.gateway()
        st = await gw.status()
        if self._check_owner_status(st) and st.get("controller_match"):
            gen = int(st["gen"])  # a claim whose answer was lost, or a restart after it
        else:
            op_name = "claim" if st.get("owner_state") != OwnerState.OWNED else "recover"
            gen_from = int(st["gen"])
            grant, sig = await self.grant(ctx, op_name, device, "gateway", gen_from, bytes(st["challenge"]))
            await self.vault_write(device.hex(), {"role": "gateway", "gen": gen_from + 1}, stage="pending",
                                   generation=gen_from + 1)
            try:
                answer = await (gw.claim(grant, sig) if op_name == "claim" else gw.recover(grant, sig))
                status = to_status(answer["status"])
            except GatewayError as exc:
                log.info("agent: %s answer lost (%s); reconciling", op_name.upper(), exc)
                status = None
            if status != Status.OK:
                st = await gw.status()
                if not (self._check_owner_status(st) and st.get("controller_match") and int(st["gen"]) == gen_from + 1):
                    raise OperationFailed(*_device_refusal(status, "gateway"))
            gen = gen_from + 1
        self.state.put(device.hex(), role="gateway", hw_id=self.ident.gateway_hw_id, gen=gen)
        await self.vault_write(device.hex(), {"role": "gateway", "gen": gen}, stage="committed", generation=gen)
        self.svc.request_inventory()
        info = gw.hello_info
        await ctx.finish(device={"gen": gen, "fw": info.fw if info else None})
        self.svc.request_heartbeat()  # the first heartbeat after the claim completes the setup in Cremind
        return {"gen": gen}

    # ------------------------------------------------------------------ discovery

    async def _op_discovery(self, ctx: OpContext) -> dict[str, Any]:
        args = ctx.args
        role = str(args.get("role"))
        short = int(args.get("short_id") or 0)
        duration = max(10, min(int(args.get("duration_s") or 45), 120))
        gw = await self.gateway()
        found = 0
        deadline = self.clock() + duration + 3
        if role == "bridge":
            seen: dict[str, int] = {}
            with gw.subscribe(types=UnprovBeacon) as sub:
                ack = await gw.scan_unprov(duration)
                if not ack.ok:
                    raise OperationFailed("gateway_offline", f"The gateway could not scan ({ack.status}).")
                next_poll = self.clock() + DISCOVERY_POLL_S
                while (remaining := deadline - self.clock()) > 0:
                    try:
                        event = await sub.get(timeout=min(remaining, DISCOVERY_POLL_S))
                    except TimeoutError:
                        event = None
                    if isinstance(event, UnprovBeacon) and identity.short_id(event.uuid) == short:
                        uuid = event.uuid.hex()
                        if uuid not in seen or event.rssi > seen[uuid]:
                            seen[uuid] = event.rssi
                            found += 1
                            await ctx.progress(candidates=[{"uuid": uuid, "rssi": event.rssi}])
                    if self.clock() >= next_poll:
                        await ctx.check_open()
                        next_poll = self.clock() + DISCOVERY_POLL_S
        else:
            bridges = [b for b in (await self._bridges_by_hw(args.get("bridges") or [])) if b.addr]
            # Every bridge is asked to listen, and asked again every ``discover_every_s`` while the search
            # lasts: a DISCOVER lost in the mesh (or a window that ended early) must not lose the search. A
            # busy bridge (finishing a tunnel or a frame session) answers BUSY and is asked again soon.
            addrs: list[int] = [b.addr for b in bridges if b.addr] or [0]
            next_ask = {addr: 0.0 for addr in addrs}
            with gw.subscribe(types=Discovered) as sub:
                next_poll = self.clock() + DISCOVERY_POLL_S
                while (remaining := deadline - self.clock()) > 0:
                    for addr in addrs:
                        if self.clock() < next_ask[addr]:
                            continue
                        ack = await gw.discover(bridge=addr, duration_s=max(1, min(int(remaining), duration)),
                                                tag_id=short)
                        if ack.ok:
                            next_ask[addr] = self.clock() + self.discover_every_s
                        elif ack.status in (Status.BUSY, Status.NO_RESOURCES):
                            next_ask[addr] = self.clock() + 0.5
                        else:
                            next_ask[addr] = float("inf")
                            log.info("agent: DISCOVER on bridge %#06x answered %s", addr, ack.status)
                    try:
                        event = await sub.get(timeout=max(0.01, min(remaining, 0.5)))
                    except TimeoutError:
                        event = None
                    if isinstance(event, Discovered) and event.tag_id == short:
                        bridge = await self.svc.db.run(lambda a=event.bridge: self.svc.db.find_bridge(addr=a))
                        if bridge is not None:
                            found += 1
                            await ctx.progress(candidates=[{"tag_id": event.tag_id, "bridge_hw_id": bridge.hw_id,
                                                            "rssi": event.rssi}])
                    if self.clock() >= next_poll:
                        await ctx.check_open()
                        next_poll = self.clock() + DISCOVERY_POLL_S
        await ctx.finish_discovery(found=found > 0)
        return {"found": found}

    async def _bridges_by_hw(self, hw_ids: list[str]) -> list[BridgeRecord]:
        out = []
        for hw in hw_ids:
            uuid = str(hw).removeprefix("br-")
            bridge = await self.svc.db.run(lambda u=uuid: self.svc.db.find_bridge(uuid=u))
            if bridge is not None:
                out.append(bridge)
        return out

    # ------------------------------------------------------------------ pair_bridge

    async def _op_pair_bridge(self, ctx: OpContext) -> dict[str, Any]:
        args, svc = ctx.args, self.svc
        secret = ctx.setup_secret()
        device = bytes.fromhex(str(args["uuid"]))
        if identity.short_id(device) != int(args.get("short_id") or -1):
            raise OperationFailed("setup_code_invalid", "The bridge found does not match the setup code.")
        name = str(args.get("name") or "")
        await ctx.progress(stage="provisioning")
        gw = await self.gateway()
        node = next((n for n in await gw.list_nodes() if n.uuid == device), None)
        progress = await ctx.local()
        if node is not None:
            addr, configured = node.addr, node.configured
        elif progress.get("addr"):
            addr, configured = int(progress["addr"]), False
        else:
            oob = identity.static_oob(secret, device)
            result = await ctx.step("provision", lambda g, op: g.provision_v2(device, oob, name or None, op_id=op),
                                    failure=("setup_code_rejected", "The bridge did not accept the setup code."))
            addr, configured = int(result.result["addr"]), False
            await ctx.save_local(addr=addr)
        ctx.on_cleanup(lambda: self._remove_node_quietly(ctx, addr, device))
        await svc.db.run(svc.db.upsert_bridge, BridgeRecord(device.hex(), addr=addr, name=name, configured=configured,
                                                            gateway_hw_id=svc.gateway_hw_id))
        if not configured:
            await ctx.step("configure", lambda g, op: g.configure_node(addr, op_id=op),
                           failure=("gateway_offline", "The bridge could not be configured."))
            await svc.db.run(lambda: svc.db.update_bridge(device.hex(), configured=True))
        await ctx.progress(stage="securing")
        mk = await ctx.staged_key("mk", device)
        tunnel = await self.open_tunnel(ctx, bridge_addr=addr, tag_id=0, wait_s=BRIDGE_OPEN_S, expect=device,
                                        role=NodeRole.BRIDGE)
        async with tunnel:
            gen = await self._pair_over(ctx, tunnel, "bridge", secret, mk)
        await asyncio.to_thread(svc.secrets.set_key, f"mk:{device.hex()}", mk)
        ctx.clear_cleanup()
        info = next((i for i in await gw.get_inventory() if i.addr == addr), None)
        pack = info.fontpack_id.hex() if info is not None and info.fontpack_id and any(info.fontpack_id) else None
        pack_ok = svc.fonts is not None and pack == svc.fonts.pack_id.hex()
        entry = self.state.put(device.hex(), role="bridge", hw_id=f"br-{device.hex()}", gen=gen, addr=addr)
        await self.vault_write(device.hex(), {"role": "bridge", "gen": gen, "mk": mk.hex(), "addr": addr},
                               stage="committed", generation=gen)
        await ctx.forget_staged_key("mk", device)
        svc.request_inventory()
        report: dict[str, Any] = {"gen": gen, "addr": addr, "fontpack_ok": pack_ok}
        if pack:
            report["fontpack_id"] = pack
        if info is not None:
            from ..daemon.commands import bridge_capacity

            cap = bridge_capacity(info)
            report.update({k: v for k, v in cap.items() if v is not None})
            if info.fw:
                report["fw"] = info.fw
        await ctx.finish(device=report)
        return {"hw_id": entry["hw_id"], "addr": addr, "gen": gen}

    async def _pair_over(self, ctx: OpContext, tunnel: Tunnel, role: str, secret: bytes, op_key: bytes) -> int:
        """PAIR over an open session: STATUS for the challenge, the grant, the setup proofs both ways.
        A device already owned by this authority and profile (a PAIR whose answer was lost) is accepted
        when it proves it holds ``op_key`` (tags: ``root_proof``) or pins this controller (bridges)."""
        assert tunnel.ident is not None and tunnel.channel is not None
        st = await tunnel.checked(SerialMsg.STATUS)
        if self._check_owner_status(st):
            if role == "tag" and st.get("root_proof") and identity.equal(bytes(st["root_proof"]),
                                                                        tunnel.channel.root_proof(op_key)):
                return int(st["gen"])
            if role == "bridge" and st.get("controller_match"):
                return int(st["gen"])
            raise OperationFailed("device_owned", "This device is already set up with other keys.")
        if st.get("owner_state") == OwnerState.OWNED:
            raise OperationFailed("device_owned", "This device belongs to another Cremind server or profile.")
        gen_from = int(st["gen"])
        ident = tunnel.ident
        grant, sig = await self.grant(ctx, "pair", ident.device_id, role, gen_from, bytes(st["challenge"]), ik=ident.ik)
        stage_state = {"role": role, "gen": gen_from + 1, ("mk" if role == "bridge" else "root"): op_key.hex()}
        await self.vault_write(ident.device_id.hex(), stage_state, stage="pending", generation=gen_from + 1)
        proof_s, k_set = tunnel.channel.setup_proof(secret, grant)
        try:
            answer = await tunnel.call(SerialMsg.PAIR, {"grant": grant, "sig": sig, "proof": proof_s,
                                                        "op_key": op_key})
        except TunnelError as exc:
            raise OperationFailed("timeout", f"The device stopped answering while pairing ({exc}).") from None
        status = to_status(answer["status"])
        if status != Status.OK:
            raise OperationFailed(*_device_refusal(status, role))
        if not isinstance(answer.get("proof"), bytes) or not tunnel.channel.check_device_proof(k_set, proof_s,
                                                                                               answer["proof"]):
            raise OperationFailed("setup_code_rejected", "The device could not prove it is the one on the label.")
        return int(answer["gen"])

    async def _remove_node_quietly(self, ctx: OpContext, addr: int, device: bytes) -> None:
        with contextlib.suppress(Exception):
            await ctx.step("remove", lambda g, op: g.remove_node(addr, op_id=op), attempts=2)
        with contextlib.suppress(Exception):
            await self.svc.db.run(lambda: self.svc.db.delete_bridge(uuid=device.hex()))

    # ------------------------------------------------------------------ pair_tag

    async def _op_pair_tag(self, ctx: OpContext) -> dict[str, Any]:
        args, svc = ctx.args, self.svc
        secret = ctx.setup_secret()
        tag_id = int(args.get("tag_id") or args.get("short_id") or 0)
        bridge = await self._bridge(str(args.get("bridge_hw_id") or ""))
        await ctx.progress(stage="waiting_for_device", detail="Waiting for the tag to wake")
        root = await ctx.staged_key("root", tag_id.to_bytes(4, "little"))
        tunnel = await self._tag_tunnel(ctx, bridge.addr, tag_id)
        async with tunnel:
            assert tunnel.ident is not None
            ident = tunnel.ident
            profile = _panel_for_board(ident.board)
            await ctx.progress(stage="pairing")
            gen = await self._pair_over(ctx, tunnel, "tag", secret, root)
        device = ident.device_id
        ref = await asyncio.to_thread(svc.secrets.set_tag_root, tag_id, root)
        record = await self._enroll_local(tag_id, ident, profile, ref, str(args.get("name") or ""))
        self.state.put(device.hex(), role="tag", hw_id=record.hw_id, gen=gen, tag_id=tag_id,
                       bridge=f"br-{bridge.uuid}")
        await ctx.forget_staged_key("root", tag_id.to_bytes(4, "little"))
        await ctx.progress(stage="clearing")
        epoch = await self._assign_and_clear(ctx, record, bridge)
        await self.vault_write(device.hex(), {"role": "tag", "gen": gen, "root": root.hex(), "tag_id": tag_id,
                                              "bridge": f"br-{bridge.uuid}", "epoch": epoch, "board": ident.board},
                               stage="committed", generation=gen)
        svc.request_inventory()
        await ctx.finish(device={"gen": gen, "epoch": epoch, "board": ident.board, "panel": record.panel,
                                 "width": record.width, "height": record.height, "planes": record.planes,
                                 "fw": f"{ident.fw_major}.{ident.fw_minor}.{ident.fw_patch}"})
        return {"hw_id": record.hw_id, "epoch": epoch, "gen": gen}

    async def _bridge(self, hw_id: str) -> BridgeRecord:
        bridge = await self.svc.db.run(lambda: self.svc.db.find_bridge(uuid=hw_id.removeprefix("br-")))
        if bridge is None or bridge.addr is None:
            raise OperationFailed("not_found", "That bridge is not in this gateway's mesh.")
        return bridge

    async def _tag_tunnel(self, ctx: OpContext, bridge_addr: int | None, tag_id: int) -> Tunnel:
        """A tunnel to the tag through its bridge, retried while the tag sleeps (it advertises every 30 s)."""
        assert bridge_addr is not None
        deadline = self.clock() + TAG_WAKE_S * 2
        while True:
            try:
                tunnel = await self.open_tunnel(ctx, bridge_addr=bridge_addr, tag_id=tag_id, wait_s=TAG_WAKE_S,
                                                role=NodeRole.TAG)
            except TunnelError as exc:
                await ctx.check_open()
                if self.clock() >= deadline or exc.status not in (Status.TIMEOUT, Status.BUSY, Status.NOT_FOUND,
                                                                  Status.CONNECT_FAILED, Status.DISCONNECTED):
                    raise OperationFailed("timeout", "The tag did not answer. Wake it (press its button or move "
                                                     "it closer to the bridge) and try again.") from None
                await asyncio.sleep(2.0)
                continue
            assert tunnel.ident is not None
            if identity.short_id(tunnel.ident.device_id) != tag_id:
                await tunnel.close()
                raise OperationFailed("not_found", "Another tag answered.")
            return tunnel

    async def _enroll_local(self, tag_id: int, ident: Any, profile: Any, ref: str, name: str) -> TagRecord:
        from ..enroll.hardware import BOARD_PANEL

        svc = self.svc
        existing = await svc.db.run(svc.db.find_tag, tag_id)
        if existing is not None:
            return await svc.db.run(lambda: svc.db.update_tag(tag_id, secret_ref=ref))
        panel = BOARD_PANEL.get(ident.board)
        record = TagRecord(tag_id=tag_id, board=int(ident.board), panel=int(panel) if panel is not None else 0,
                           width=profile.width, height=profile.height, planes=profile.planes,
                           plane_flags=profile.plane_flags, secret_ref=ref, name=name,
                           fw=f"{ident.fw_major}.{ident.fw_minor}.{ident.fw_patch}")
        return await svc.db.run(svc.db.insert_tag, record)

    async def _assign_and_clear(self, ctx: OpContext, tag: TagRecord, bridge: BridgeRecord, *, floor: int = 0,
                                clear: bool = True) -> int:
        """Assign ``K_epoch`` v2 above every epoch used, reported or known (``floor``), then ``CLEAR`` (a
        new owner's first screen is white); returns the epoch. A tag that stores a higher epoch (a released
        tag keeps its epoch) answers ``STALE_EPOCH``: the floor it reported is raised (bounded, §10) and the
        tag is assigned again above it."""
        for _attempt in range(STALE_EPOCH_RETRIES):
            try:
                return await self._assign_once(ctx, tag, bridge, floor=floor, clear=clear)
            except OperationFailed as exc:
                if exc.code != "stale_epoch":
                    raise
                log.info("agent: tag %08X stores a newer epoch; assigning again above it", tag.tag_id)
                await ctx.save_local(epoch=None, assigned=None, cleared=None)
                await ctx.reset_steps("assign", "clear")
                tag = await self.svc.db.run(self.svc.db.find_tag, tag.tag_id) or tag
        raise OperationFailed("stale_epoch", "The tag keeps reporting a newer epoch; try again later.")

    async def _assign_once(self, ctx: OpContext, tag: TagRecord, bridge: BridgeRecord, *, floor: int,
                           clear: bool) -> int:
        svc = self.svc
        progress = await ctx.local()
        epoch = int(progress.get("epoch") or 0)
        if not epoch:
            floors = await svc.db.run(svc.store.epoch_floors)
            epoch = max(tag.epoch, floors.get(tag.tag_id, 0), int(ctx.args.get("epoch") or 0), floor) + 1
            await ctx.save_local(epoch=epoch)
        addr = bridge.addr
        assert addr is not None
        if not progress.get("assigned"):
            key = await asyncio.to_thread(svc.secrets.k_epoch, tag.tag_id, epoch, tag.secret_ref)
            await ctx.step("assign", lambda g, op: g.assign_tag(addr, tag.tag_id, epoch, key, op_id=op),
                           failure=("gateway_offline", "The bridge did not take the tag's key."),
                           on_status={Status.NO_RESOURCES: ("bridge_full", "The bridge is full.")})
            await svc.db.run(lambda: svc.store.on_assigned(tag.tag_id, epoch=epoch, bridge_addr=addr,
                                                           bridge_hw_id=bridge.hw_id))
            await ctx.save_local(assigned=True)
        if clear and not progress.get("cleared"):
            await ctx.step("clear", lambda g, op: g.tag_command(bridge=addr, tag_id=tag.tag_id, epoch=epoch,
                                                                cmd=TagCommand.CLEAR, op_id=op),
                           failure=("timeout", "The tag did not confirm its first screen."),
                           on_status={Status.STALE_EPOCH: ("stale_epoch", "The tag stores a newer epoch.")})
            await svc.db.run(svc.store.on_cleared, tag.tag_id, epoch)
            await ctx.save_local(cleared=True)
        svc.wake_scheduler()
        return epoch

    # ------------------------------------------------------------------ move_tag

    async def _op_move_tag(self, ctx: OpContext) -> dict[str, Any]:
        args, svc = ctx.args, self.svc
        tag_id = int(args.get("tag_id") or 0)
        tag = await svc.db.run(svc.db.find_tag, tag_id)
        if tag is None:
            raise OperationFailed("not_found", "This worker does not know that tag.")
        bridge = await self._bridge(str(args.get("bridge_hw_id") or ""))
        previous = (tag.bridge_addr, tag.epoch)
        epoch = await self._assign_and_clear(ctx, tag, bridge, clear=False)
        if previous[0] and previous[0] != bridge.addr and previous[1]:
            with contextlib.suppress(Exception):
                await ctx.step("unassign", lambda g, op: g.unassign_tag(previous[0], tag_id, previous[1], op_id=op),
                               attempts=3)
        found = self.state.by_hw_id(tag.hw_id)
        if found is not None:
            self.state.put(found[0], bridge=f"br-{bridge.uuid}")
        svc.request_inventory()
        await ctx.finish(device={"epoch": epoch})
        return {"epoch": epoch}

    # ------------------------------------------------------------------ unpair / release_gateway

    async def _op_unpair(self, ctx: OpContext) -> dict[str, Any]:
        args = ctx.args
        role = str(args.get("role"))
        device = bytes.fromhex(str(args["device_id"]))
        if role == "tag":
            await self._release_tag(ctx, device, str(args.get("hw_id") or ""))
        elif role == "bridge":
            await self._release_bridge(ctx, device)
        else:
            raise OperationFailed("unsupported", "Remove the gateway itself instead.")
        self.svc.request_inventory()
        await ctx.finish()
        return {"removed": device.hex()}

    async def _release_tag(self, ctx: OpContext, device: bytes, hw_id: str) -> None:
        """Remove a tag (section 5.1 RELEASE, two stages): prepare (the tag arms a fresh setup secret), show that
        code on the tag, and only once it is displayed commit. A released tag that never showed its code
        could not be set up again without a factory reset."""
        svc = self.svc
        entry = self.state.device(device.hex()) or {}
        tag_id = int(entry.get("tag_id") or (int(hw_id, 16) if hw_id else identity.short_id(device)))
        tag = await svc.db.run(svc.db.find_tag, tag_id)
        progress = await ctx.local()
        if tag is not None and tag.bridge_addr and not progress.get("released"):
            if not progress.get("code_shown"):
                if svc.fonts is None:
                    raise OperationFailed("fonts_missing", "Cremind Connect has no font pack, so the tag cannot "
                                                           "show its new setup code; it stays set up for now.")
                await ctx.progress(stage="releasing", detail="Waiting for the tag to wake")
                payload = await self._release_stage(ctx, device, tag.bridge_addr, tag_id, 0)
                if payload is None:
                    raise OperationFailed("failed", "The tag did not hand over a new setup code.")
                await ctx.progress(stage="showing_code", detail="The tag is showing its new setup code")
                await self._show_setup_code(ctx, tag_id, payload.qr_text())
                await ctx.save_local(code_shown=True)
            await ctx.progress(stage="releasing", detail="Waiting for the tag to wake")
            await self._release_stage(ctx, device, tag.bridge_addr, tag_id, 1)
            await ctx.save_local(released=True)
        if tag is not None and tag.bridge_addr and tag.epoch:
            with contextlib.suppress(Exception):
                await ctx.step("unassign", lambda g, op: g.unassign_tag(tag.bridge_addr, tag_id, tag.epoch, op_id=op),
                               attempts=3)
        with contextlib.suppress(Exception):
            await svc.db.run(lambda: svc.store.set_override(tag_id, None, None, force=False))
        if tag is not None:
            await svc.db.run(svc.db.delete_tag, tag_id)
        await asyncio.to_thread(svc.secrets.delete_tag_root, tag_id)
        self.state.forget(device.hex())

    async def _release_stage(self, ctx: OpContext, device: bytes, bridge_addr: int, tag_id: int,
                             stage: int) -> SetupPayload | None:
        """One RELEASE stage through the tag's bridge; stage 0 returns the fresh setup payload. A tag already
        released (a stage 1 whose answer was lost) is taken as done."""
        tunnel = await self._tag_tunnel(ctx, bridge_addr, tag_id)
        async with tunnel:
            st = await tunnel.checked(SerialMsg.STATUS)
            if not self._check_owner_status(st):
                if stage == 1 and st.get("owner_state") == OwnerState.RELEASED:
                    return None
                raise OperationFailed("device_owned", "This tag is no longer set up for this Cremind server.")
            grant, sig = await self.grant(ctx, "release", device, "tag", int(st["gen"]), bytes(st["challenge"]))
            answer = await tunnel.call(SerialMsg.RELEASE, {"grant": grant, "sig": sig, "release_stage": stage})
            status = to_status(answer["status"])
            if status != Status.OK:
                raise OperationFailed(*_device_refusal(status, "tag"))
            return _setup_payload(answer.get("data")) if stage == 0 else None

    async def _show_setup_code(self, ctx: OpContext, tag_id: int, qr_text: str) -> None:
        """Deliver the setup-code screen (daemon.screens.SETUP_OVERRIDE) and wait until the tag reports it
        displayed."""
        from ..daemon.screens import SETUP_OVERRIDE

        svc = self.svc
        started = svc.clock()
        await svc.db.run(lambda: svc.store.set_override(tag_id, SETUP_OVERRIDE + qr_text, started + 86400))
        svc.wake_scheduler()
        deadline = self.clock() + SHOW_CODE_S
        next_check = self.clock() + DISCOVERY_POLL_S
        while True:
            rev = await svc.db.run(lambda: svc.store.latest_revision(tag_id, purposes=["setup_code"],
                                                                     since_ts=started))
            if rev is not None and rev.state == "displayed":
                return
            if (rev is not None and rev.state in ("failed", "uncertain")) or self.clock() >= deadline:
                raise OperationFailed("timeout", "The tag did not show its new setup code, so it stays set up. "
                                                 "Try removing it again when it is near its bridge.")
            if self.clock() >= next_check:
                await ctx.check_open()
                next_check = self.clock() + DISCOVERY_POLL_S
            await asyncio.sleep(0.2)

    async def _release_bridge(self, ctx: OpContext, device: bytes) -> None:
        svc = self.svc
        bridge = await svc.db.run(lambda: svc.db.find_bridge(uuid=device.hex()))
        progress = await ctx.local()
        if bridge is not None and bridge.addr and not progress.get("released"):
            await ctx.progress(stage="releasing")
            try:
                tunnel = await self.open_tunnel(ctx, bridge_addr=bridge.addr, tag_id=0, wait_s=BRIDGE_OPEN_S,
                                                expect=device, role=NodeRole.BRIDGE)
            except (OperationFailed, TunnelError) as exc:
                log.info("agent: bridge %s did not answer (%s); removing it from the mesh anyway", device.hex(), exc)
            else:
                async with tunnel:
                    st = await tunnel.checked(SerialMsg.STATUS)
                    if self._check_owner_status(st):
                        grant, sig = await self.grant(ctx, "release", device, "bridge", int(st["gen"]),
                                                      bytes(st["challenge"]))
                        answer = await tunnel.call(SerialMsg.RELEASE, {"grant": grant, "sig": sig})
                        if to_status(answer["status"]) != Status.OK:
                            raise OperationFailed(*_device_refusal(to_status(answer["status"]), "bridge"))
            await ctx.save_local(released=True)
        if bridge is not None and bridge.addr:
            addr = bridge.addr
            with contextlib.suppress(Exception):
                await ctx.step("remove", lambda g, op: g.remove_node(addr, op_id=op), attempts=3)
        await svc.db.run(lambda: svc.db.delete_bridge(uuid=device.hex()))
        await asyncio.to_thread(svc.secrets.delete_key, f"mk:{device.hex()}")
        self.state.forget(device.hex())

    async def _op_release_gateway(self, ctx: OpContext) -> dict[str, Any]:
        devices = [d for d in (ctx.args.get("devices") or []) if isinstance(d, dict)]
        pending = []
        for role in ("tag", "bridge"):
            for item in devices:
                if item.get("role") != role:
                    continue
                device = bytes.fromhex(str(item["device_id"]))
                sub = ctx.child(f"{role}:{device.hex()}")
                try:
                    if role == "tag":
                        entry = self.state.device(device.hex()) or {}
                        await self._release_tag(sub, device, str(entry.get("hw_id") or ""))
                    else:
                        await self._release_bridge(sub, device)
                except OperationFailed as exc:
                    log.warning("agent: %s %s not released: %s", role, device.hex(), exc)
                    pending.append(device.hex())
        gw = await self.gateway()
        st = await gw.status()
        if self._check_owner_status(st) and st.get("controller_match"):
            grant, sig = await self.grant(ctx, "release", self.ident.gateway_device_id, "gateway", int(st["gen"]),
                                          bytes(st["challenge"]))
            answer = await gw.release(grant, sig)
            if to_status(answer["status"]) != Status.OK:
                raise OperationFailed(*_device_refusal(to_status(answer["status"]), "gateway"))
        await ctx.finish(result={"left_behind": pending})
        await self._removed("its gateway was removed in Cremind")
        return {"released": True, "left_behind": pending}

    # ------------------------------------------------------------------ recover_gateway

    async def _op_recover_gateway(self, ctx: OpContext) -> dict[str, Any]:
        entries = await self._vault_entries()
        await ctx.progress(stage="recovering_gateway")
        gw = await self.gateway()
        device = self.ident.gateway_device_id
        st = await gw.status()
        if not (self._check_owner_status(st) and st.get("controller_match")):
            if not self._check_owner_status(st):
                raise OperationFailed("device_owned", "This gateway is no longer set up for this Cremind server.")
            gen_from = int(st["gen"])
            grant, sig = await self.grant(ctx, "recover", device, "gateway", gen_from, bytes(st["challenge"]))
            answer = await gw.recover(grant, sig)
            if to_status(answer["status"]) != Status.OK:
                st = await gw.status()
                if not st.get("controller_match"):
                    raise OperationFailed(*_device_refusal(to_status(answer["status"]), "gateway"))
            st = await gw.status()
        gen = int(st["gen"])
        self.state.put(device.hex(), role="gateway", hw_id=self.ident.gateway_hw_id, gen=gen)
        await self.vault_write(device.hex(), {"role": "gateway", "gen": gen}, stage="committed", generation=gen)
        await ctx.progress(devices=[{"device_id": device.hex(), "state": "rekeyed", "gen": gen}])
        self.svc.request_heartbeat()  # the recovered gateway's heartbeat completes the setup session
        pending: list[str] = []
        nodes = {n.uuid.hex(): n for n in await gw.list_nodes()}
        for dev_hex, entry in sorted(entries.items(), key=lambda kv: kv[1].get("role") != "bridge"):
            role = entry.get("role")
            if role == "bridge":
                node = nodes.get(dev_hex)
                if node is None:
                    await ctx.progress(devices=[{"device_id": dev_hex, "state": "failed"}])
                    continue
                await self.svc.db.run(self.svc.db.upsert_bridge, BridgeRecord(
                    dev_hex, addr=node.addr, name=node.name, configured=node.configured,
                    gateway_hw_id=self.svc.gateway_hw_id))
                ok = await self._rekey_bridge(ctx.child(f"bridge:{dev_hex}"), bytes.fromhex(dev_hex), node.addr)
            elif role == "tag":
                ok = await self._rekey_tag(ctx.child(f"tag:{dev_hex}"), bytes.fromhex(dev_hex), entry)
            else:
                continue
            if not ok:
                pending.append(dev_hex)
        if pending:
            await ctx.progress(state="pending_device", detail=f"{len(pending)} device(s) not reached yet")
        await ctx.finish(result={"pending": pending})
        self.svc.request_inventory()
        return {"pending": pending}

    async def _vault_entries(self) -> dict[str, dict[str, Any]]:
        try:
            entries = await self.client.vault_get()
        except ConnectorAuthError as exc:
            if _is_business_refusal(exc):
                raise OperationFailed("no_recovery", "Cremind has no recovery open for this worker.") from None
            raise WorkerRemoved(str(exc)) from None
        except ConnectorError as exc:
            if exc.status == 503:
                raise OperationFailed("recovery_key_unavailable", "Cremind cannot open its recovery data "
                                                                  "(its recovery key is missing).") from None
            raise
        latest: dict[str, dict[str, Any]] = {}
        for entry in sorted(entries, key=lambda e: int(e.get("version") or 0)):
            state = entry.get("state")
            if isinstance(state, dict) and entry.get("device_id") != "worker":
                latest[str(entry["device_id"])] = {**state, "_stage": entry.get("stage")}
                self.state.vault_versions[str(entry["device_id"])] = int(entry.get("version") or 0)
        self.state.save()
        return latest

    async def _rekey_bridge(self, ctx: OpContext, device: bytes, addr: int) -> bool:
        try:
            tunnel = await self.open_tunnel(ctx, bridge_addr=addr, tag_id=0, wait_s=BRIDGE_OPEN_S, expect=device,
                                            role=NodeRole.BRIDGE)
        except (OperationFailed, TunnelError) as exc:
            log.info("agent: bridge %s unreachable during recovery: %s", device.hex(), exc)
            await ctx.progress(devices=[{"device_id": device.hex(), "state": "pending"}])
            return False
        mk = await ctx.staged_key("mk", device)
        async with tunnel:
            gen = await self._rekey_over(ctx, tunnel, device, "bridge", mk)
        await asyncio.to_thread(self.svc.secrets.set_key, f"mk:{device.hex()}", mk)
        self.state.put(device.hex(), role="bridge", hw_id=f"br-{device.hex()}", gen=gen, addr=addr)
        await self.vault_write(device.hex(), {"role": "bridge", "gen": gen, "mk": mk.hex(), "addr": addr},
                               stage="committed", generation=gen)
        await ctx.forget_staged_key("mk", device)
        await ctx.progress(devices=[{"device_id": device.hex(), "state": "rekeyed", "gen": gen}])
        return True

    async def _rekey_tag(self, ctx: OpContext, device: bytes, entry: dict[str, Any]) -> bool:
        svc = self.svc
        tag_id = int(entry.get("tag_id") or identity.short_id(device))
        bridge_hw = str(entry.get("bridge") or "")
        try:
            bridge = await self._bridge(bridge_hw)
            tunnel = await self._tag_tunnel(ctx, bridge.addr, tag_id)
        except OperationFailed as exc:
            log.info("agent: tag %08X unreachable during recovery: %s", tag_id, exc)
            await ctx.progress(devices=[{"device_id": device.hex(), "state": "pending"}])
            return False
        root = await ctx.staged_key("root", device)
        async with tunnel:
            assert tunnel.ident is not None
            ident = tunnel.ident
            gen = await self._rekey_over(ctx, tunnel, device, "tag", root)
        ref = await asyncio.to_thread(svc.secrets.set_tag_root, tag_id, root)
        record = await self._enroll_local(tag_id, ident, _panel_for_board(ident.board), ref, "")
        self.state.put(device.hex(), role="tag", hw_id=record.hw_id, gen=gen, tag_id=tag_id, bridge=bridge_hw)
        epoch = await self._assign_and_clear(ctx, record, bridge, floor=int(entry.get("epoch") or 0))
        await self.vault_write(device.hex(), {"role": "tag", "gen": gen, "root": root.hex(), "tag_id": tag_id,
                                              "bridge": bridge_hw, "epoch": epoch, "board": ident.board},
                               stage="committed", generation=gen)
        await ctx.forget_staged_key("root", device)
        await ctx.progress(devices=[{"device_id": device.hex(), "state": "rekeyed", "gen": gen, "cleared": True}])
        return True

    async def _rekey_over(self, ctx: OpContext, tunnel: Tunnel, device: bytes, role: str, op_key: bytes) -> int:
        assert tunnel.channel is not None
        st = await tunnel.checked(SerialMsg.STATUS)
        if not self._check_owner_status(st):
            raise OperationFailed("device_owned", "This device is no longer set up for this Cremind server.")
        if role == "tag" and st.get("root_proof") and identity.equal(bytes(st["root_proof"]),
                                                                    tunnel.channel.root_proof(op_key)):
            return int(st["gen"])  # the REKEY already took effect (its answer was lost)
        if role == "bridge" and st.get("controller_match"):
            return int(st["gen"])
        gen_from = int(st["gen"])
        grant, sig = await self.grant(ctx, "rekey", device, role, gen_from, bytes(st["challenge"]))
        await self.vault_write(device.hex(), {"role": role, "gen": gen_from + 1,
                                              ("mk" if role == "bridge" else "root"): op_key.hex()},
                               stage="pending", generation=gen_from + 1)
        answer = await tunnel.call(SerialMsg.REKEY, {"grant": grant, "sig": sig, "op_key": op_key})
        status = to_status(answer["status"])
        if status != Status.OK:
            raise OperationFailed(*_device_refusal(status, role))
        return int(answer["gen"])


class OpContext:
    """One operation run: its Cremind record, the command row that carries its durable progress, and the
    executor's durable gateway steps."""

    def __init__(self, agent: ConnectAgent, op: dict[str, Any], row: CommandRow, executor: CommandExecutor,
                 prefix: str = "") -> None:
        self.agent = agent
        self.op = op
        self.op_id = str(op["id"])
        self.kind = str(op.get("kind"))
        self.args: dict[str, Any] = dict(op.get("args") or {})
        self.row = row
        self.executor = executor
        self.prefix = prefix
        self._cleanup: list[Callable[[], Awaitable[None]]] = []

    def child(self, name: str) -> OpContext:
        """Progress keys of a sub-step (one device of a gateway release or a recovery)."""
        return OpContext(self.agent, self.op, self.row, self.executor, prefix=f"{self.prefix}{name}/")

    # -- Cremind --------------------------------------------------------------------------------

    async def progress(self, **fields: Any) -> dict[str, Any]:
        op = await self.agent._report(self.op_id, **fields)
        if op.get("state") in FINAL and fields.get("state") not in FINAL:
            raise OperationCancelled()
        return op

    async def check_open(self) -> None:
        op = await self.agent._operation(self.op_id)
        if op.get("state") in FINAL:
            raise OperationCancelled()

    async def finish(self, *, device: dict[str, Any] | None = None, result: dict[str, Any] | None = None) -> None:
        await self.agent._report(self.op_id, state="succeeded", stage="done", device=device, result=result)

    async def finish_discovery(self, *, found: bool) -> None:
        with contextlib.suppress(OperationCancelled):
            await self.agent._report(self.op_id, state="succeeded" if found else "failed")

    def setup_secret(self) -> bytes:
        secret = self.op.get("setup_secret")
        if not isinstance(secret, str) or not secret:
            raise OperationFailed("setup_code_invalid", "The setup code is no longer available; search again.")
        return bytes.fromhex(secret)

    # -- durable local progress (the command row) -----------------------------------------------------

    async def local(self) -> dict[str, Any]:
        progress = await self.executor._progress(self.row)
        return {k[len(self.prefix):]: v for k, v in progress.items() if k.startswith(self.prefix)} \
            if self.prefix else progress

    async def save_local(self, **values: Any) -> None:
        await self.executor._save(self.row, **{f"{self.prefix}{k}": v for k, v in values.items()})

    async def reset_steps(self, *steps: str) -> None:
        """Forget the op ids of durable steps, so they are sent again as new work."""
        await self.executor._save(self.row, **{f"op:{self.prefix}{step}": None for step in steps})

    async def step(self, step: str, send: Callable[[Any, int], Awaitable[Any]], *,
                   failure: tuple[str, str] = ("gateway_offline", "The gateway did not complete the step."),
                   attempts: int | None = None, on_status: dict[Status, tuple[str, str]] | None = None) -> Any:
        """One durable gateway step (:meth:`CommandExecutor._step`); a final status listed in ``on_status``
        fails the operation with that code at once, any other failure with ``failure``."""
        from ..daemon.commands import CommandError, StepFailed

        final = frozenset(on_status or ())
        try:
            return await self.executor._step(self.row, f"{self.prefix}{step}", send, attempts=attempts, final=final)
        except StepFailed as exc:
            code, text = (on_status or {}).get(exc.status, failure)  # type: ignore[call-overload]
            raise OperationFailed(code, f"{text} ({exc})") from None
        except CommandError as exc:
            raise OperationFailed(failure[0], f"{failure[1]} ({exc})") from None

    # -- staged keys: generated once, kept in the secret store until committed ----------------------------

    async def staged_key(self, kind: str, subject: bytes) -> bytes:
        store = self.agent.svc.secrets
        name = f"staged-{kind}:{self.op_id}:{subject.hex()}"
        existing = await asyncio.to_thread(store.get_key, name)
        if existing is not None:
            return existing
        key = identity.random_bytes(32)
        await asyncio.to_thread(store.set_key, name, key)
        return key

    async def forget_staged_key(self, kind: str, subject: bytes) -> None:
        store = self.agent.svc.secrets
        await asyncio.to_thread(store.delete_key, f"staged-{kind}:{self.op_id}:{subject.hex()}")

    # -- cleanup after a failure or a cancellation ------------------------------------------------------

    def on_cleanup(self, action: Callable[[], Awaitable[None]]) -> None:
        self._cleanup.append(action)

    def clear_cleanup(self) -> None:
        self._cleanup.clear()

    async def cleanup(self) -> None:
        actions, self._cleanup = self._cleanup, []
        for action in reversed(actions):
            with contextlib.suppress(Exception):
                await action()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panel_for_board(board: int) -> Any:
    """The panel geometry a v2 tag's board ships with; refuses boards without a verified panel."""
    from ..enroll.hardware import BOARD_PANEL, PANEL_PROFILES
    from ..protocol.ids import Board

    try:
        panel = BOARD_PANEL[Board(board)]
    except (ValueError, KeyError):
        raise OperationFailed("unsupported_tag", f"This tag's board ({board}) is not supported yet.") from None
    profile = PANEL_PROFILES.get(panel)
    if profile is None:
        raise OperationFailed("unsupported_tag", "This tag's display is not supported yet (its panel is not "
                                                 "verified).")
    return profile


def _setup_payload(data: Any) -> SetupPayload | None:
    if not isinstance(data, bytes) or not data:
        return None
    try:
        return SetupPayload.unpack(data)
    except ValueError:
        return None


_REFUSALS: dict[Status, tuple[str, str]] = {
    Status.NOT_OWNER: ("device_owned", "The {role} belongs to someone else."),
    Status.GRANT_INVALID: ("grant_refused", "The {role} refused Cremind's authorisation."),
    Status.STALE_GENERATION: ("stale_generation", "The {role} reports a newer ownership history; try again."),
    Status.PROOF_FAILED: ("setup_code_rejected", "The {role} did not accept the setup code."),
    Status.LOCKED: ("locked", "The {role} is locked. Connect it to this computer by USB to set it up again."),
    Status.AUTH_REQUIRED: ("timeout", "The {role}'s secure session ended; try again."),
}


def _device_refusal(status: Status | int | None, role: str) -> tuple[str, str]:
    if status is None:
        return "timeout", f"The {role} stopped answering."
    name = status.name if isinstance(status, Status) else str(status)
    code, text = _REFUSALS.get(status, ("failed", "The {role} answered " + name + "."))  # type: ignore[call-overload]
    return code, text.format(role=role)


def _conflict_message(code: str) -> str:
    return {"device_owned": "That device is already set up.",
            "stale_generation": "The device's ownership history does not match Cremind's; try again."}.get(
        code, "Cremind refused the change.")


__all__ = ["AgentState", "ConnectAgent", "OpContext", "OperationCancelled", "OperationFailed", "WorkerIdentity",
           "WorkerRemoved"]
