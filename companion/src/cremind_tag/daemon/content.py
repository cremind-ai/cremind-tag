"""One content credential's feed: ``sync``, then the ``events`` loop (docs/connector-api.md).

Start-up (and every ``resync_s``, after a ``410 cursor_expired``, a changed
``stream_id``, or when the scheduler meets a tag it has no view of) calls
``POST sync`` with the stored cursor; :meth:`QueueStore.apply_sync` rebuilds the
local jobs from ``outstanding`` when the stream changed, the cursor is invalid
or this database never synced, and otherwise reconciles deliveries Cremind
ended meanwhile.

Between syncs the worker polls ``GET events`` every ``active_poll_s`` while
jobs keep arriving, doubling up to ``idle_poll_s`` when the feed is idle (a
full page is followed immediately). Each page is committed in ONE transaction
together with the new cursor and the ``accepted`` acknowledgement, which the
outbox then POSTs — the order connector-api.md requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ..connector.client import (
    EVENTS_PAGE_LIMIT,
    Backoff,
    ConnectorAuthError,
    ConnectorClient,
    ConnectorError,
    ConnectorTlsError,
    CursorExpired,
)

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

MIN_REQUESTED_SYNC_S = 5.0
"""Syncs asked for by other components (not forced by the feed) are at least this far apart."""


class ContentWorker:
    """Keeps one content credential's jobs in the local queue (see the module docstring)."""

    def __init__(self, svc: DaemonService, client: ConnectorClient) -> None:
        self.svc = svc
        self.client = client
        self.credential_id = client.credential_id
        self.profile: str | None = None
        self.state = "starting"
        self.error: str | None = None
        self.caught_up = False
        self.last_sync_ts = 0.0
        self.syncs = 0
        self.pages = 0
        self._forced_sync = True
        self._requested_sync = False
        self._wake = asyncio.Event()

    def request_sync(self, *, forced: bool = False) -> None:
        if forced:
            self._forced_sync = True
        else:
            self._requested_sync = True
        self._wake.set()

    def _sync_due(self) -> bool:
        now = self.svc.clock()
        if self._forced_sync or now - self.last_sync_ts >= self.svc.settings.resync_s:
            return True
        return self._requested_sync and now - self.last_sync_ts >= MIN_REQUESTED_SYNC_S

    async def run(self) -> None:
        settings = self.svc.settings
        backoff = Backoff(1.0, settings.connector_retry_max_s)
        poll = settings.active_poll_s
        while True:
            try:
                if self._sync_due():
                    await self.sync()
                jobs, full = await self.poll_once()
                backoff.reset()
                self.state, self.error = "running", None
                if full:
                    continue
                self.caught_up = True
                poll = settings.active_poll_s if jobs else min(settings.idle_poll_s, poll * 2)
                await self._sleep(poll)
            except CursorExpired as exc:
                log.info("content: credential=%s cursor expired (oldest=%s head=%s): sync", self.credential_id,
                         exc.oldest_seq, exc.head_seq)
                self._forced_sync = True
            except (ConnectorAuthError, ConnectorTlsError) as exc:
                self.state, self.error = "stopped", str(exc)
                self.svc.credential_failed(self.credential_id, exc)
                log.error("content: credential=%s stopped: %s", self.credential_id, exc)
                return
            except ConnectorError as exc:
                delay = backoff.next()
                self.state, self.error = "retrying", str(exc)
                log.warning("content: credential=%s retry in %.1fs: %s", self.credential_id, delay, exc)
                await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        if self._sync_due():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def sync(self) -> None:
        stream = await self.svc.db.run(self.svc.store.get_stream, self.credential_id)
        result = await self.client.sync(stream.after_seq if stream is not None and stream.stream_id else None)
        outcome = await self.svc.db.run(self.svc.store.apply_sync, self.credential_id, result)
        self._forced_sync = self._requested_sync = False
        self.last_sync_ts = self.svc.clock()
        self.syncs += 1
        self.profile = result.profile
        log.info("content: credential=%s profile=%s sync stream=%s rebuilt=%s head=%d outstanding=%d inserted=%d "
                 "dropped=%d tags=%d", self.credential_id, result.profile, result.stream_id, outcome.rebuilt,
                 result.head_seq, len(result.outstanding), outcome.inserted, outcome.dropped, len(result.tags))
        self.svc.wake_scheduler()
        self.svc.wake_outbox(self.credential_id)

    async def poll_once(self) -> tuple[int, bool]:
        """One events page; returns (jobs in the page, whether it was full)."""
        stream = await self.svc.db.run(self.svc.store.get_stream, self.credential_id)
        if stream is None or not stream.stream_id:
            self._forced_sync = True
            return 0, True
        page = await self.client.events(stream.after_seq, EVENTS_PAGE_LIMIT)
        if page.stream_id != stream.stream_id:
            log.warning("content: credential=%s stream changed %s -> %s (Cremind restored?): sync",
                        self.credential_id, stream.stream_id, page.stream_id)
            self._forced_sync = True
            return 0, True
        self.svc.crash.hit("events_fetched")
        outcome = await self.svc.db.run(self.svc.store.accept_page, self.credential_id, page)
        self.svc.crash.hit("events_committed")
        self.pages += 1
        if page.jobs:
            log.info("content: credential=%s page jobs=%d inserted=%d refused=%d cursor=%d", self.credential_id,
                     len(page.jobs), outcome.inserted, outcome.refused, outcome.cursor)
            self.svc.wake_scheduler()
            self.svc.wake_outbox(self.credential_id)
        full = len(page.jobs) >= EVENTS_PAGE_LIMIT and page.next_after > stream.after_seq
        return len(page.jobs), full


__all__ = ["ContentWorker"]
