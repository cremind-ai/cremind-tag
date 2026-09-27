"""The outbox sender: tells Cremind what the queue committed, until Cremind confirms.

One sender per credential (a revoked credential stops only its own sender).
Rows are sent oldest first; consecutive ``receipts`` rows are merged into one
``POST receipts`` (≤ 500 receipts). A row is deleted only after Cremind
answered 2xx, so a crash between commit and POST re-sends it after the restart
— every connector write is idempotent (connector-api.md: receipts never move a
stage backwards, ``accepted`` only moves ``queued`` deliveries, a command
result is idempotent for the same status).

Errors: transient ones (5xx, network) retry with bounded exponential back-off
per row; a 4xx refusal marks the row dead (kept for ``cremind-tag queue`` and
diagnostics); 401/403 stop the sender, a TLS misconfiguration pauses it for
``tls_retry_s``; both are reported in ``daemon status``.

``POST receipts`` may list ``rejected`` receipts: ``terminal``, ``not_owned``
and ``unknown`` ones are dropped (Cremind already has the delivery's final
word, or it is not this profile's); ``epoch_mismatch`` means an assignment
moved the delivery to another epoch — the credential re-syncs, and the sync
re-sends the terminal receipts of deliveries Cremind still lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ..connector.client import (
    Backoff,
    ConnectorAuthError,
    ConnectorClient,
    ConnectorConflict,
    ConnectorError,
    ConnectorNotFound,
    ConnectorRejected,
    ConnectorTlsError,
)
from .store import OutboxRow

if TYPE_CHECKING:
    from .service import DaemonService

log = logging.getLogger(__name__)

MAX_RECEIPTS_PER_POST = 500


class OutboxSender:
    """Flushes one credential's outbox (see the module docstring)."""

    def __init__(self, svc: DaemonService, client: ConnectorClient) -> None:
        self.svc = svc
        self.client = client
        self.credential_id = client.credential_id
        self._wake = asyncio.Event()
        self.sent = 0
        self.stopped: str | None = None

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        store = self.svc.store
        while True:
            self._wake.clear()
            rows = await self.svc.db.run(store.due_outbox, self.credential_id, 200)
            if rows:
                if not await self._send_batch(rows):
                    return
                continue
            next_ts = await self.svc.db.run(store.next_outbox_ts, self.credential_id)
            timeout = None if next_ts is None else max(0.05, next_ts - self.svc.clock())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    async def _send_batch(self, rows: list[OutboxRow]) -> bool:
        """Send the first request's worth of rows; False when the sender must stop."""
        first = rows[0]
        batch = [first]
        if first.kind == "receipts":
            count = len(first.payload.get("receipts") or [])
            for row in rows[1:]:
                size = len(row.payload.get("receipts") or [])
                if row.kind != "receipts" or count + size > MAX_RECEIPTS_PER_POST:
                    break
                batch.append(row)
                count += size
        ids = [r.id for r in batch]
        try:
            await self._post(batch)
        except ConnectorAuthError as exc:
            self.stopped = str(exc)
            self.svc.credential_failed(self.credential_id, exc)
            log.error("outbox: credential=%s stopped: %s", self.credential_id, exc)
            return False
        except ConnectorTlsError as exc:
            self.svc.credential_warning(self.credential_id, exc)
            log.error("outbox: credential=%s TLS problem (retry in %.0fs): %s", self.credential_id,
                      self.svc.settings.tls_retry_s, exc)
            await self.svc.db.run(self.svc.store.outbox_retry, ids, str(exc), self.svc.settings.tls_retry_s)
            return True
        except ConnectorConflict as exc:
            if first.kind == "previews" and exc.code == "epoch_mismatch":
                # Rendered for an epoch the tag has left (an assignment or owner change): never retried.
                log.info("outbox: credential=%s preview of tag %s refused (epoch_mismatch); re-syncing",
                         self.credential_id, first.payload.get("tag_id"))
                await self.svc.db.run(self.svc.store.outbox_done, ids)
                self.svc.request_sync(self.credential_id)
                return True
            if first.kind == "command_result":
                log.info("outbox: command=%s already finished in Cremind (%s)", first.payload.get("command_id"),
                         exc.code)
                await self.svc.db.run(self.svc.store.outbox_done, ids)
            else:
                await self.svc.db.run(self.svc.store.outbox_dead, ids, str(exc))
            return True
        except (ConnectorRejected, ConnectorNotFound) as exc:
            log.warning("outbox: credential=%s kind=%s refused by Cremind, kept as dead: %s", self.credential_id,
                        first.kind, exc)
            await self.svc.db.run(self.svc.store.outbox_dead, ids, str(exc))
            return True
        except ConnectorError as exc:
            delay = Backoff.delay_for(first.attempts, 1.0, self.svc.settings.connector_retry_max_s)
            log.info("outbox: credential=%s kind=%s will retry in %.1fs: %s", self.credential_id, first.kind, delay,
                     exc)
            await self.svc.db.run(self.svc.store.outbox_retry, ids, str(exc), delay)
            return True
        await self.svc.db.run(self.svc.store.outbox_done, ids)
        self.svc.credential_ok(self.credential_id)
        self.sent += len(batch)
        return True

    async def _post(self, batch: list[OutboxRow]) -> None:
        row = batch[0]
        payload = row.payload
        if row.kind == "receipts":
            receipts = [r for item in batch for r in (item.payload.get("receipts") or [])]
            self.svc.crash.hit("before_receipt_post")
            result = await self.client.receipts(receipts)
            log.debug("outbox: credential=%s receipts=%d applied=%d rejected=%d", self.credential_id, len(receipts),
                      result.applied, len(result.rejected))
            reasons = result.reasons()
            for reason, ids_ in reasons.items():
                log.info("outbox: credential=%s %d receipt(s) rejected (%s): %s", self.credential_id, len(ids_),
                         reason, ids_[:20])
            if "epoch_mismatch" in reasons:
                self.svc.request_sync(self.credential_id)
        elif row.kind == "accepted":
            accepted = await self.client.accepted(int(payload.get("through_seq") or 0),
                                                  [int(i) for i in payload.get("delivery_ids") or []])
            log.debug("outbox: credential=%s accepted=%d", self.credential_id, accepted)
        elif row.kind == "previews":
            epoch = payload.get("epoch")
            await self.client.previews(tag_id=str(payload["tag_id"]), revision=int(payload["revision"]),
                                       kind=str(payload["kind"]), png_base64=str(payload["png_base64"]),
                                       delivery_ids=[int(i) for i in payload.get("delivery_ids") or []],
                                       epoch=int(epoch) if isinstance(epoch, int) else None)
        elif row.kind == "command_result":
            await self.client.result(str(payload["command_id"]), str(payload["status"]), payload.get("result"),
                                     payload.get("error"))
        else:  # pragma: no cover - the schema's CHECK forbids it
            raise ConnectorRejected(f"unknown outbox kind {row.kind}")


__all__ = ["OutboxSender"]
