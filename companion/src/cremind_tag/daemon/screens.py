"""The per-tag screen scheduler: card set -> composed screen -> revision -> ``DELIVER_LAYOUT``.

Each pass (on a wake-up, or every ``scan_interval_s`` to notice changes other
processes made):

1. **Expiry** — cards past ``expires_at`` leave their card set; unreported ones
   are receipted ``expired`` (``uncertain`` after ``DISPLAY_STATE_UNKNOWN``).
2. **Composition** — for every tag whose card set changed: skip it while the
   tag is blocked, waits for its ``clear_tag`` or has no owner; hold a
   progress-only change until ``progress_cadence_s`` after the previous
   revision; skip when the inputs (``content_key``) or the layout digest equal
   the current desired revision; otherwise compose (``compose_screen``, or
   ``compose_identify`` / ``compose_blank``), render the desired preview, and
   persist a NEW revision from ``Database.allocate_revision`` with the
   ``op_id`` its delivery will use — superseding older undelivered revisions,
   whose deliveries the new screen includes (connector-api.md "Screen model").
3. **Delivery** — every pending revision that is due goes out as
   ``DELIVER_LAYOUT`` with the tag's assignment (epoch, bridge) and the active
   font pack. ``ACCEPTED`` -> ``sent`` + receipts ``gateway_received``;
   ``BUSY``/``NO_RESOURCES`` -> retry later with the same op id;
   ``INVALID``/``TOO_LARGE`` -> the revision fails; an unknown bridge blocks the
   tag and re-syncs the inventory.

Results come back through :mod:`cremind_tag.daemon.events`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ..compose.api import ActiveCard, ComposedScreen, ScreenSettings, TagPanel
from ..gateway.errors import FrameTooLargeError, GatewayError
from ..protocol.ids import LAYOUT_SERIAL_MAX, Status
from ..protocol.layout import layout_digest
from .store import ComposeInput, RevisionRow, status_name

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

HOLD_S = 5.0
"""How long a revision waits when its tag cannot be delivered to right now (not assigned, no gateway)."""


_OFFSET = re.compile(r"(?i)(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?")


@functools.lru_cache(maxsize=64)
def iana_timezone(name: str | None) -> str:
    """A zone ICU knows. Cremind sends IANA names; defensively a Windows zone id (``SE Asia Standard Time``)
    is mapped with ICU's Windows table and a bare UTC offset (``+07:00``, ``UTC+7``) becomes ``GMT+07:00``.
    Anything else is UTC (the composer would fall back to it silently)."""
    import icu

    text = (name or "").strip() or "UTC"
    if icu.TimeZone.createTimeZone(text).getID() != "Etc/Unknown":
        return text
    mapped = str(icu.TimeZone.getIDForWindowsID(text) or "")
    if mapped and icu.TimeZone.createTimeZone(mapped).getID() != "Etc/Unknown":
        log.info("scheduler: Windows time zone %r read as %s", text, mapped)
        return mapped
    match = _OFFSET.fullmatch(text)
    if match:
        custom = f"GMT{match[1]}{int(match[2]):02d}:{int(match[3] or 0):02d}"
        if icu.TimeZone.createTimeZone(custom).getID() != "Etc/Unknown":
            return custom
    log.warning("scheduler: unknown time zone %r; showing UTC", text)
    return "UTC"


def screen_settings(settings: Any) -> ScreenSettings:
    return ScreenSettings(show_excerpts=bool(settings.show_excerpts), qr_links=bool(settings.qr_links),
                          timezone=iana_timezone(settings.timezone), language=settings.language or "en")


def panel_for(inp: ComposeInput) -> TagPanel:
    return TagPanel(tag_id=inp.tag.tag_id, width=inp.tag.width, height=inp.tag.height, planes=inp.tag.planes,
                    plane_flags=inp.tag.plane_flags, rotation=inp.view.rotation,
                    name=inp.view.name or inp.tag.name)


def content_key(purpose: str, panel: TagPanel, settings: ScreenSettings, cards: list[ActiveCard],
                pack_id: str) -> str:
    """Digest of everything a screen is composed from, except the clock (a screen is not re-sent only
    because a minute passed)."""
    doc = {
        "purpose": purpose, "pack": pack_id,
        "panel": [panel.tag_id, panel.width, panel.height, panel.planes, panel.plane_flags, panel.rotation,
                  panel.name],
        "settings": [settings.show_excerpts, settings.qr_links, settings.timezone, settings.language],
        "cards": [[c.delivery_id, c.kind, c.priority, c.created_at.isoformat(), c.card] for c in cards],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class ScreenScheduler:
    """Composes and delivers every tag's screen (see the module docstring)."""

    def __init__(self, svc: DaemonService) -> None:
        self.svc = svc
        self._wake = asyncio.Event()
        self._compose_lock = asyncio.Lock()
        self.composed = 0
        self.sent = 0
        self.holds: dict[int, str] = {}

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            next_ts = await self.pass_once()
            timeout = self.svc.settings.scan_interval_s
            if next_ts is not None:
                timeout = max(0.01, min(timeout, next_ts - self.svc.clock()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    async def pass_once(self) -> float | None:
        """One scheduling pass; returns when the next timed work is due (``None``: nothing timed)."""
        svc, store = self.svc, self.svc.store
        svc.apply_effects(await svc.db.run(store.expire))
        stuck = await svc.db.run(store.resend_stuck, svc.settings.result_timeout_s)
        if stuck:
            log.warning("scheduler: %d delivery(ies) without a result for %.0fs: sending again", stuck,
                        svc.settings.result_timeout_s)
        tags, orphans = await svc.db.run(store.tags_needing_work)
        for credential_id in orphans:
            svc.request_sync(credential_id)
        wakes: list[float] = []
        for tag_id in tags:
            try:
                due = await self.compose_tag(tag_id)
            except Exception:
                log.exception("scheduler: composing the screen of tag %08X failed", tag_id)
                due = svc.clock() + svc.settings.retry_initial_s
            if due is not None:
                wakes.append(due)
        await self.send_due()
        # A due revision only counts when it can be sent; otherwise the gateway connecting (or the pack
        # loading) wakes the scheduler, and the scan interval bounds the wait.
        can_send = svc.gateway is not None and svc.gateway.connected and svc.fonts is not None
        due_ts = await svc.db.run(store.next_due_ts) if can_send else None
        for ts in (due_ts, await svc.db.run(store.next_expiry_ts)):
            if ts is not None:
                wakes.append(ts)
        return min(wakes) if wakes else None

    # -- composition -------------------------------------------------------------------

    async def compose_tag(self, tag_id: int) -> float | None:
        svc, store = self.svc, self.svc.store
        inp = await svc.db.run(store.compose_input, tag_id)
        if inp is None:
            if await svc.db.run(svc.db.find_tag, tag_id) is None:
                detail = f"tag {tag_id:08X} is not enrolled on this companion (its secret and panel are unknown)"
                svc.apply_effects(await svc.db.run(lambda: store.block_tag(
                    tag_id, "not_enrolled", detail, status_code=int(Status.SECURITY_CONFIG))))
                log.error("scheduler: %s", detail)
            return None
        view = inp.view
        now = svc.clock()
        if view.blocked_reason:
            self.holds[tag_id] = f"blocked: {view.blocked_reason}"
            return None
        if svc.fonts is None:
            self.holds[tag_id] = "no font pack loaded"
            return None
        if view.override is None and view.clear_required:
            self.holds[tag_id] = "waiting for clear_tag"
            return None
        if view.override is None and not view.credential_id and not view.force:
            await svc.db.run(store.clear_dirty, tag_id, view.dirty_gen)  # nobody here owns it: nothing to show
            return None
        if not view.dirty and not view.force and view.override is None and view.progress_pending \
                and inp.last_created_ts is not None:
            due = inp.last_created_ts + inp.settings.progress_cadence_s
            if now < due:
                self.holds[tag_id] = "progress cadence"
                return due
        self.holds.pop(tag_id, None)

        panel = panel_for(inp)
        settings = screen_settings(inp.settings)
        cards = [ActiveCard(j.delivery_id, j.kind, j.priority, j.created_dt, j.card) for j in inp.cards]
        if view.override == "identify":
            purpose = "identify"
        elif not cards and view.blank:
            purpose = "blank"
        else:
            purpose = "refresh" if view.force and inp.current is not None else "screen"
        pack_hex = svc.fonts.pack_id.hex()
        key = content_key("identify" if purpose == "identify" else "screen", panel, settings,
                          cards if purpose in ("screen", "refresh") else [], pack_hex)
        if purpose == "blank":
            key = content_key("blank", panel, settings, [], pack_hex)
        current = inp.current
        if current is None and not cards and not inp.carry and not view.force and purpose != "identify":
            # Nothing to show and nothing shown from this database yet (a fresh start, or white after
            # clear_tag): leave the tag as it is rather than pushing an empty screen over it.
            await svc.db.run(store.clear_dirty, tag_id, view.dirty_gen)
            return None
        if not view.force and current is not None and current.content_key == key:
            await svc.db.run(store.attach_to_current, tag_id, current, inp.carry, view.dirty_gen, key)
            svc.wake_outbox()
            return None

        screen, png = await self._compose(purpose, panel, cards, settings)
        digest = layout_digest(screen.layout).hex()
        if not view.force and current is not None and current.layout_digest == digest:
            await svc.db.run(store.attach_to_current, tag_id, current, inp.carry, view.dirty_gen, key)
            svc.wake_outbox()
            return None
        if len(screen.layout) > LAYOUT_SERIAL_MAX:  # the composer guarantees this; never send more (§1.5)
            log.error("scheduler: tag %08X composed %d bytes > LAYOUT_SERIAL_MAX", tag_id, len(screen.layout))
            return None
        delivery_ids = [*screen.delivery_ids, *inp.carry]
        rev = await svc.db.run(lambda: store.create_revision(
            tag_id=tag_id, dirty_gen=view.dirty_gen, epoch=inp.tag.epoch, bridge_addr=inp.tag.bridge_addr,
            fontpack_id=pack_hex, purpose=purpose, layout=screen.layout, layout_digest=digest, content_key=key,
            delivery_ids=delivery_ids, pending_delivery_ids=list(screen.pending_delivery_ids), preview_png=png,
            preview_epoch=max(inp.tag.epoch, view.epoch)))
        self.composed += 1
        log.info("scheduler: tag %08X revision %d (%s) shows %s, %d more waiting, %d bytes", tag_id, rev.revision,
                 purpose, list(screen.delivery_ids), len(screen.pending_delivery_ids), len(screen.layout))
        svc.wake_outbox()
        svc.crash.hit("revision_persisted")
        return None

    async def _compose(self, purpose: str, panel: TagPanel, cards: list[ActiveCard],
                       settings: ScreenSettings) -> tuple[ComposedScreen, bytes | None]:
        """Compose + render the preview in a worker thread (CPU work, one at a time)."""
        fonts = self.svc.fonts
        assert fonts is not None
        now = dt.datetime.fromtimestamp(self.svc.clock(), tz=dt.UTC)

        def work() -> tuple[ComposedScreen, bytes | None]:
            from ..compose.preview import PreviewTooLarge, preview_png
            from ..compose.screen import compose_blank, compose_identify, compose_screen

            if purpose == "identify":
                screen = compose_identify(panel, fonts, panel.tag_id)
            elif purpose == "blank":
                screen = compose_blank(panel)
            else:
                screen = compose_screen(panel, cards, fonts, settings, now)
            try:
                png = preview_png(screen, panel, fonts)
            except (PreviewTooLarge, ValueError) as exc:
                log.warning("scheduler: no preview for tag %08X: %s", panel.tag_id, exc)
                png = None
            return screen, png

        async with self._compose_lock:
            return await asyncio.to_thread(work)

    # -- delivery ----------------------------------------------------------------------

    async def send_due(self) -> None:
        svc, store = self.svc, self.svc.store
        client = svc.gateway
        if client is None or not client.connected or svc.fonts is None:
            return
        for rev in await svc.db.run(store.due_revisions):
            if not client.connected:
                return
            await self._send(rev)

    async def _send(self, rev: RevisionRow) -> None:
        svc, store = self.svc, self.svc.store
        client = svc.gateway
        assert client is not None and svc.fonts is not None
        tag = await svc.db.run(svc.db.find_tag, rev.tag_id)
        view = await svc.db.run(store.get_view, rev.tag_id)
        reason = None
        if tag is None:
            reason = "not enrolled"
        elif view is not None and view.blocked_reason:
            reason = f"blocked: {view.blocked_reason}"
        elif not tag.bridge_addr or tag.epoch < 1:
            reason = "not assigned to a bridge yet"
        elif view is not None and view.epoch > tag.epoch:
            reason = f"waiting for assign_tag (Cremind epoch {view.epoch}, assigned {tag.epoch})"
        elif view is not None and view.clear_required and rev.purpose != "identify":
            reason = "waiting for clear_tag"
        if reason is not None:
            self.holds[rev.tag_id] = reason
            await svc.db.run(lambda: store.defer(rev.tag_id, rev.revision, reason, HOLD_S, count_attempt=False))
            return
        assert tag is not None and tag.bridge_addr is not None
        pack = svc.fonts.pack_id
        bridge_addr, epoch = tag.bridge_addr, tag.epoch
        ready = await svc.db.run(lambda: store.prepare_send(rev.tag_id, rev.revision, epoch=epoch,
                                                            bridge_addr=bridge_addr, fontpack_id=pack.hex()))
        if ready is None:
            return
        try:
            ack = await client.deliver_layout(bridge=tag.bridge_addr, tag_id=rev.tag_id, epoch=tag.epoch,
                                              revision=rev.revision, update_id=ready.op_id, fontpack_id=pack,
                                              layout=ready.layout, op_id=ready.op_id)
        except (FrameTooLargeError, ValueError) as exc:
            svc.apply_effects(await svc.db.run(store.fail_revision, rev.tag_id, rev.revision,
                                               int(Status.TOO_LARGE), f"the layout does not fit a frame: {exc}"))
            return
        except GatewayError as exc:
            delay = store.retry_delay(ready.attempts)
            log.info("scheduler: DELIVER_LAYOUT tag %08X rev %d not sent (%s); retry in %.1fs", rev.tag_id,
                     rev.revision, exc, delay)
            await svc.db.run(lambda: store.defer(rev.tag_id, rev.revision, "gateway unreachable", delay))
            return
        svc.crash.hit("deliver_sent")
        status = ack.status
        if ack.ok:
            if await svc.db.run(store.mark_sent, rev.tag_id, rev.revision, ready.op_id):
                self.sent += 1
                svc.wake_outbox()
            log.info("scheduler: tag %08X revision %d -> gateway (%s%s), bridge %#06x epoch %d", rev.tag_id,
                     rev.revision, status_name(status), ", duplicate" if ack.duplicate else "", tag.bridge_addr,
                     tag.epoch)
        elif status in (Status.BUSY, Status.NO_RESOURCES):
            # Back-pressure, not a failure: not counted, so a burst of BUSY never inflates the exponential
            # back-off of a later link failure (retry_delay(attempts)).
            await svc.db.run(lambda: store.defer(rev.tag_id, rev.revision, status_name(status),
                                                 min(store.retry_initial_s, 2.0), count_attempt=False))
        elif status in (Status.INVALID, Status.TOO_LARGE, Status.UNSUPPORTED):
            svc.apply_effects(await svc.db.run(store.fail_revision, rev.tag_id, rev.revision, int(status),
                                               f"DELIVER_LAYOUT answered {status_name(status)}"
                                               + (f": {ack.text}" if ack.text else "")))
        elif status == Status.NOT_FOUND:
            detail = f"the gateway does not know bridge {tag.bridge_addr:#06x} (re-syncing the assignment)"
            effects = await svc.db.run(lambda: store.block_tag(rev.tag_id, "bridge_not_found", detail,
                                                               status_code=int(status), epoch=tag.epoch,
                                                               fail_jobs=False))
            effects.inventory = True
            if view is not None and view.credential_id:
                effects.sync.add(view.credential_id)
            svc.apply_effects(effects)
        else:
            delay = store.retry_delay(ready.attempts)
            await svc.db.run(lambda: store.defer(rev.tag_id, rev.revision, status_name(status), delay))


__all__ = ["ScreenScheduler", "content_key", "panel_for", "screen_settings"]
