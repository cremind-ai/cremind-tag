"""The gateway event handler: every retained event is committed to the queue BEFORE it is ACKed.

Registered as the :class:`~cremind_tag.gateway.client.GatewayClient`'s only
(catch-all) handler, so the client sends ``EVENT_ACK`` for an event only after
this coroutine returned — i.e. after its SQLite transaction committed with
``synchronous=FULL``. A crash before that leaves the event unacknowledged; the
gateway re-sends it after the next HELLO and the handler, idempotent by
``(tag_id, revision)`` / ``op_id``, applies it once.

- ``EVT_RESULT``: a tag command's result (``update_id`` = the op id a command
  persisted) goes to ``gateway_ops``; any other result is a delivery's
  (:meth:`QueueStore.apply_result`: displayed -> receipts + displayed preview;
  ``DISPLAY_STATE_UNKNOWN`` -> re-deliver; ``STALE_REVISION`` -> jump the
  allocator; security statuses -> block the tag and re-sync; link failures ->
  back-off).
- ``EVT_ASSIGN_RESULT``, ``EVT_PROVISIONED``, ``EVT_NODE_CONFIGURED``,
  ``EVT_NODE_REMOVED``: results of command steps (by ``op_id``).
- ``EVT_STAGE``: non-terminal receipts (best effort, not retained).
- ``EVT_TAG_SEEN``: battery/RSSI/last contact for the heartbeat.
- ``SessionStarted``: a changed ``boot_id`` (also across companion restarts)
  re-delivers every revision that was sent but has no result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..gateway.events import (
    AssignResult,
    BridgeInfoEvent,
    GatewayEvent,
    NodeConfigured,
    NodeRemoved,
    Provisioned,
    ResultEvent,
    SessionStarted,
    StageEvent,
    TagSeen,
)
from ..protocol.ids import DeliveryStage
from .store import status_name

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

STAGE_NAMES = {
    DeliveryStage.GATEWAY_RECEIVED: "gateway_received",
    DeliveryStage.BRIDGE_RECEIVED: "bridge_received",
    DeliveryStage.TRANSFERRING: "transferring",
    DeliveryStage.REFRESHING: "refreshing",
}


class GatewayEventHandler:
    """The daemon's ACK-gating gateway event handler (see the module docstring)."""

    def __init__(self, svc: DaemonService) -> None:
        self.svc = svc
        self.results = 0
        self.stages = 0

    async def __call__(self, event: GatewayEvent) -> None:
        if isinstance(event, ResultEvent):
            await self._result(event)
        elif isinstance(event, AssignResult | Provisioned | NodeConfigured | NodeRemoved):
            await self._op_result(event)
        elif isinstance(event, StageEvent):
            await self._stage(event)
        elif isinstance(event, SessionStarted):
            await self._session(event)
        elif isinstance(event, TagSeen):
            await self.svc.db.run(lambda: self.svc.store.record_sighting(event.tag_id, rssi=event.rssi,
                                                                          battery_mv=event.battery_mv))
        elif isinstance(event, BridgeInfoEvent):
            await self._bridge_info(event)

    async def _result(self, event: ResultEvent) -> None:
        svc = self.svc
        svc.crash.hit("handler_before_commit")
        if await svc.db.run(svc.store.is_op, event.update_id):
            fields = {"tag_id": event.tag_id, "bridge": event.bridge, "epoch": event.epoch,
                      "revision": event.revision, "digest": event.digest.hex(), "battery_mv": event.battery_mv}
            await svc.db.run(svc.store.record_op_result, event.update_id, int(event.status), fields)
            svc.ops.resolve(event.update_id)
            log.info("gateway: command op %d tag %08X: %s", event.update_id, event.tag_id, status_name(event.status))
        else:
            effects = await svc.db.run(lambda: svc.store.apply_result(
                update_id=event.update_id, tag_id=event.tag_id, epoch=event.epoch, revision=event.revision,
                status=int(event.status), digest=bytes(event.digest), battery_mv=event.battery_mv,
                timing=event.timing.as_dict()))
            self.results += 1
            log.info("gateway: result tag %08X revision %d epoch %d: %s (digest %s, refresh %d ms)", event.tag_id,
                     event.revision, event.epoch, status_name(event.status), event.digest.hex(),
                     event.timing.refresh_ms)
            svc.apply_effects(effects)
        svc.crash.hit("result_committed")
        svc.wake_scheduler()
        svc.wake_outbox()

    async def _op_result(self, event: AssignResult | Provisioned | NodeConfigured | NodeRemoved) -> None:
        svc = self.svc
        svc.crash.hit("handler_before_commit")
        fields = {k: (v.hex() if isinstance(v, bytes) else int(v) if hasattr(v, "value") else v)
                  for k, v in event.raw.items() if k not in ("seq", "status")}
        if await svc.db.run(svc.store.record_op_result, event.op_id, int(event.status), fields):
            svc.ops.resolve(event.op_id)
            log.info("gateway: %s op %d: %s", type(event).__name__, event.op_id, status_name(event.status))

    async def _stage(self, event: StageEvent) -> None:
        name = STAGE_NAMES.get(event.stage) if isinstance(event.stage, DeliveryStage) else None
        if name is None:
            return
        if await self.svc.db.run(self.svc.store.apply_stage, event.tag_id, event.revision, name):
            self.stages += 1
            self.svc.wake_outbox()

    async def _session(self, event: SessionStarted) -> None:
        svc = self.svc
        changed = await svc.db.run(svc.store.on_session, event.hello.boot_id)
        await svc.db.run(svc.record_gateway, event)
        svc.gateway_session(event, changed or event.boot_changed)

    async def _bridge_info(self, event: BridgeInfoEvent) -> None:
        svc = self.svc
        info = event.info
        pack = info.fontpack_id.hex() if info.fontpack_id and any(info.fontpack_id) else None
        changed = await svc.db.run(svc.note_bridge_info, info.addr, pack, info.fw)
        if changed:
            svc.request_inventory()
        if pack is not None and svc.fonts is not None and pack == svc.fonts.pack_id.hex():
            if await svc.db.run(svc.store.unblock_all, "fontpack_mismatch"):
                svc.wake_scheduler()


__all__ = ["GatewayEventHandler", "STAGE_NAMES"]
