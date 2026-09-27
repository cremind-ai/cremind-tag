"""The durable delivery queue: every state change the daemon makes, as one SQLite transaction each.

All methods are synchronous and run in a worker thread (``await db.run(store.method, ...)``);
each public method is ONE ``Database.transaction()`` (re-entrant: the inventory's
``allocate_revision`` and ``set_assignment`` join it). The async side never holds
state that is not in these tables, so a crash at any await point loses nothing
that was acknowledged (see docs/companion.md "Durability").

Vocabulary (schema in :mod:`cremind_tag.daemon.schema`):

- a **job** is one Cremind delivery; its ``state`` says whether its card is in
  the tag's card set (``active``) and its ``outcome`` whether the companion has
  reported a terminal outcome (NULL until then);
- a **revision** is one composed screen for a tag; it lists the deliveries it
  shows (``delivery_ids``) and those only counted in its footer
  (``pending_delivery_ids``);
- the **outbox** holds what Cremind must be told, until it confirms.

Receipts follow connector-api.md: a stage never moves backwards (``last_stage``
per job) and a terminal outcome is written once (``outcome IS NULL`` guards
every terminal update), so the companion never reports two different outcomes
for one delivery.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..connector.models import EventsPage, Job, ProfileSettings, SyncResult, iso, tag_hw_id
from ..gateway.opid import OpIdGenerator
from ..protocol.ids import Status
from ..store.db import Database, TagRecord
from .validator import REFUSED_DETAIL, card_problem

STAGES = ("queued", "companion_accepted", "gateway_received", "bridge_received", "transferring", "refreshing",
          "displayed")
STAGE_RANK = {name: rank for rank, name in enumerate(STAGES)}
TERMINAL_OUTCOMES = ("displayed", "superseded", "expired", "cancelled", "failed", "uncertain")
INSTRUCTION_KINDS = frozenset({"resolved", "clear"})
"""Kinds that change the card set but are never shown themselves."""
TERMINAL_STATE = {"displayed": "done", "superseded": "superseded", "expired": "expired", "cancelled": "cancelled",
                  "failed": "failed", "uncertain": "done"}
"""Local ``state`` of a job that arrived already final in Cremind."""

STALE_REVISION_JUMP = 1 << 16
"""Revisions skipped after ``STALE_REVISION`` (doubled per consecutive refusal, at most ``<< 8``)."""
MAX_REVISION = 0xFFFFFFFF

LINK_STATUSES = frozenset({
    Status.DISCONNECTED, Status.TIMEOUT, Status.CONNECT_FAILED, Status.MESH_SUSPEND_FAILED,
    Status.MESH_RESUME_FAILED, Status.BUSY, Status.NO_RESOURCES, Status.INCOMPLETE, Status.DIGEST_MISMATCH,
    Status.CRC_ERROR, Status.PANEL_ERROR, Status.REFRESH_TIMEOUT, Status.STORAGE_ERROR, Status.INTERNAL,
    Status.PROVISIONING_ACTIVE, Status.CANCELLED, Status.EXPIRED,
})
"""Results retried with back-off until the jobs' TTL (docs/protocol.md §10 "Delivery")."""
SECURITY_STATUSES = frozenset({
    Status.AUTH_FAILED, Status.SECURITY_CONFIG, Status.NOT_ASSIGNED, Status.STALE_EPOCH, Status.VERSION_MISMATCH,
    Status.NOT_FOUND,
})
"""Results that stop the tag's work of that epoch and re-sync its assignment (§10, connector-api.md Defaults).
``NOT_FOUND`` counts only after ``NOT_FOUND_ESCALATE`` consecutive answers for one revision (§10)."""
NOT_FOUND_ESCALATE = 3
"""``EVT_RESULT NOT_FOUND`` answers for one revision before it is treated as a security stop (§10)."""
LAYOUT_STATUSES = frozenset({Status.INVALID, Status.TOO_LARGE, Status.UNSUPPORTED})
"""Results that fail the revision (and the deliveries it shows) but not the tag."""
RECEIPTS_IN_ORDER = ("NOT (outbox.kind = 'receipts' AND EXISTS (SELECT 1 FROM outbox older"
                     " WHERE older.credential_id = outbox.credential_id AND older.dead = 0"
                     " AND older.kind = 'receipts' AND older.id < outbox.id AND older.next_attempt_ts > ?))")
"""SQL condition (one parameter: now) on an ``outbox`` row: not a receipts row behind an older receipts row
that waits for its retry. Receipts are posted in the order they were committed."""


def _status(value: int | Status) -> Status | int:
    try:
        return Status(int(value))
    except ValueError:
        return int(value)


def status_name(value: int | Status | None) -> str:
    if value is None:
        return "-"
    status = _status(value)
    return status.name if isinstance(status, Status) else str(status)


def ts_iso(ts: float) -> str:
    return iso(dt.datetime.fromtimestamp(ts, tz=dt.UTC))


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobRow:
    delivery_id: int
    credential_id: str
    profile: str
    seq: int
    tag_id: int
    epoch: int
    kind: str
    priority: int
    replace_key: str | None
    resolves: str | None
    card: dict[str, Any]
    created_at: str
    expires_at: str
    expires_ts: float
    state: str
    last_stage: str | None
    outcome: str | None
    status_code: int | None
    detail: str | None
    revision: int | None
    digest: str | None
    uncertain: bool
    received_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> JobRow:
        return cls(r["delivery_id"], r["credential_id"], r["profile"], r["seq"], r["tag_id"], r["epoch"], r["kind"],
                   r["priority"], r["replace_key"], r["resolves"], _loads(r["card"], {}), r["created_at"],
                   r["expires_at"], r["expires_ts"], r["state"], r["last_stage"], r["outcome"], r["status_code"],
                   r["detail"], r["revision"], r["digest"], bool(r["uncertain"]), r["received_at"],
                   r["finished_at"])

    @property
    def created_dt(self) -> dt.datetime:
        from ..connector.models import parse_time

        return parse_time(self.created_at)

    @property
    def title(self) -> str:
        value = self.card.get("title") if isinstance(self.card, dict) else None
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class RevisionRow:
    tag_id: int
    revision: int
    epoch: int
    bridge_addr: int | None
    fontpack_id: str | None
    purpose: str
    layout: bytes
    layout_digest: str
    content_key: str
    frame_digest: str | None
    delivery_ids: tuple[int, ...]
    pending_delivery_ids: tuple[int, ...]
    preview_png: bytes | None
    op_id: int
    state: str
    last_stage: str | None
    attempts: int
    uncertain_count: int
    not_found_count: int
    next_attempt_ts: float
    last_status: str | None
    detail: str | None
    created_at: str
    created_ts: float
    sent_at: str | None
    sent_ts: float | None
    finished_at: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> RevisionRow:
        return cls(r["tag_id"], r["revision"], r["epoch"], r["bridge_addr"], r["fontpack_id"], r["purpose"],
                   bytes(r["layout"]), r["layout_digest"], r["content_key"], r["frame_digest"],
                   tuple(_loads(r["delivery_ids"], [])), tuple(_loads(r["pending_delivery_ids"], [])),
                   bytes(r["preview_png"]) if r["preview_png"] is not None else None, r["op_id"], r["state"],
                   r["last_stage"], r["attempts"], r["uncertain_count"], r["not_found_count"],
                   r["next_attempt_ts"], r["last_status"],
                   r["detail"], r["created_at"], r["created_ts"], r["sent_at"], r["sent_ts"], r["finished_at"])


@dataclass(frozen=True, slots=True)
class TagView:
    tag_id: int
    credential_id: str | None
    profile: str | None
    name: str
    epoch: int
    bridge_hw_id: str | None
    rotation: int
    clear_required: bool
    cremind_desired: int
    cremind_displayed: int
    blank: bool
    override: str | None
    override_until: float | None
    blocked_reason: str | None
    blocked_epoch: int | None
    dirty: bool
    dirty_gen: int
    force: bool
    progress_pending: bool
    displayed_revision: int
    displayed_digest: str | None
    displayed_at: str | None
    stale_jumps: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> TagView:
        return cls(r["tag_id"], r["credential_id"], r["profile"], r["name"], r["epoch"], r["bridge_hw_id"],
                   r["rotation"], bool(r["clear_required"]), r["cremind_desired"], r["cremind_displayed"],
                   bool(r["blank"]), r["override"], r["override_until"], r["blocked_reason"], r["blocked_epoch"],
                   bool(r["dirty"]), r["dirty_gen"], bool(r["force"]), bool(r["progress_pending"]),
                   r["displayed_revision"], r["displayed_digest"], r["displayed_at"], r["stale_jumps"])


@dataclass(frozen=True, slots=True)
class StreamRow:
    credential_id: str
    profile: str | None
    companion_id: str | None
    stream_id: str | None
    after_seq: int
    head_seq: int
    settings: ProfileSettings
    synced_at: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> StreamRow:
        return cls(r["credential_id"], r["profile"], r["companion_id"], r["stream_id"], r["after_seq"], r["head_seq"],
                   ProfileSettings.from_json(_loads(r["settings"], {})), r["synced_at"])


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    kind: str
    credential_id: str
    dedupe_key: str | None
    payload: dict[str, Any]
    attempts: int
    next_attempt_ts: float
    last_error: str | None
    dead: bool
    created_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> OutboxRow:
        return cls(r["id"], r["kind"], r["credential_id"], r["dedupe_key"], _loads(r["payload"], {}), r["attempts"],
                   r["next_attempt_ts"], r["last_error"], bool(r["dead"]), r["created_at"])


@dataclass(frozen=True, slots=True)
class CommandRow:
    command_id: str
    kind: str
    args: dict[str, Any]
    state: str
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    expires_ts: float | None
    created_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> CommandRow:
        return cls(r["command_id"], r["kind"], _loads(r["args"], {}), r["state"], _loads(r["progress"], {}),
                   _loads(r["result"], None), r["error"], r["expires_ts"], r["created_at"])


@dataclass(frozen=True, slots=True)
class OpResult:
    op_id: int
    kind: str
    status: int
    result: dict[str, Any]


@dataclass(frozen=True)
class ComposeInput:
    """Everything the scheduler needs to compose one tag's screen, read in one snapshot."""

    tag: TagRecord
    view: TagView
    settings: ProfileSettings
    cards: tuple[JobRow, ...]
    carry: tuple[int, ...]
    current: RevisionRow | None
    last_created_ts: float | None


@dataclass
class Effects:
    """Follow-ups the async side must trigger after a store call."""

    sync: set[str] = field(default_factory=set)
    inventory: bool = False
    tags: set[int] = field(default_factory=set)
    outbox: bool = False

    def merge(self, other: Effects) -> Effects:
        self.sync |= other.sync
        self.inventory = self.inventory or other.inventory
        self.tags |= other.tags
        self.outbox = self.outbox or other.outbox
        return self


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    rebuilt: bool
    inserted: int
    dropped: int
    accepted_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PageOutcome:
    inserted: int
    refused: int
    cursor: int


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class QueueStore:
    """Queue operations over one :class:`Database` (see the module docstring)."""

    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time,
                 op_ids: OpIdGenerator | None = None, retry_initial_s: float = 5.0, retry_max_s: float = 600.0,
                 uncertain_retries: int = 5) -> None:
        self.db = db
        self.clock = clock
        self.op_ids = op_ids or OpIdGenerator()
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self.uncertain_retries = uncertain_retries

    # -- small helpers -------------------------------------------------------------

    def _now(self) -> tuple[float, str]:
        ts = self.clock()
        return ts, ts_iso(ts)

    def retry_delay(self, attempts: int) -> float:
        from ..connector.client import Backoff

        return Backoff.delay_for(attempts, self.retry_initial_s, self.retry_max_s, jitter=0.1)

    @staticmethod
    def _enqueue(conn: sqlite3.Connection, kind: str, credential_id: str, payload: dict[str, Any], now_iso: str,
                 dedupe_key: str | None = None) -> None:
        if dedupe_key is not None:
            conn.execute("DELETE FROM outbox WHERE kind = ? AND credential_id = ? AND dedupe_key = ? AND dead = 0",
                         (kind, credential_id, dedupe_key))
        conn.execute("INSERT INTO outbox (kind, credential_id, dedupe_key, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                     (kind, credential_id, dedupe_key, _dumps(payload), now_iso))

    @staticmethod
    def _ensure_view(conn: sqlite3.Connection, tag_id: int, now_iso: str) -> None:
        conn.execute("INSERT OR IGNORE INTO tag_views (tag_id, updated_at) VALUES (?, ?)", (tag_id, now_iso))

    @classmethod
    def _mark_dirty(cls, conn: sqlite3.Connection, tag_id: int, now_iso: str, *, force: bool = False,
                    progress_only: bool = False) -> None:
        cls._ensure_view(conn, tag_id, now_iso)
        if progress_only:
            # A new generation too: a composition that started before this change must not clear it.
            conn.execute("UPDATE tag_views SET progress_pending = 1, dirty_gen = dirty_gen + 1, updated_at = ?"
                         " WHERE tag_id = ?", (now_iso, tag_id))
            return
        conn.execute("UPDATE tag_views SET dirty = 1, dirty_gen = dirty_gen + 1, force = MAX(force, ?),"
                     " updated_at = ? WHERE tag_id = ?", (int(force), now_iso, tag_id))

    @staticmethod
    def _jobs(conn: sqlite3.Connection, where: str, params: Sequence[Any]) -> list[JobRow]:
        return [JobRow.from_row(r) for r in conn.execute(f"SELECT * FROM jobs WHERE {where}", params).fetchall()]

    @staticmethod
    def _receipt(job: JobRow, *, stage: str, outcome: str | None, at: str, **fields: Any) -> dict[str, Any]:
        receipt: dict[str, Any] = {"delivery_id": job.delivery_id, "stage": stage, "outcome": outcome, "at": at,
                                   "tag_id": tag_hw_id(job.tag_id), "epoch": job.epoch}
        receipt.update({k: v for k, v in fields.items() if v is not None})
        return receipt

    def _post_receipts(self, conn: sqlite3.Connection, receipts: Iterable[tuple[str, dict[str, Any]]],
                       now_iso: str) -> int:
        by_credential: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for credential_id, receipt in receipts:
            by_credential[credential_id].append(receipt)
        for credential_id, items in by_credential.items():
            for start in range(0, len(items), 200):
                self._enqueue(conn, "receipts", credential_id, {"receipts": items[start:start + 200]}, now_iso)
        return sum(len(v) for v in by_credential.values())

    def _stage(self, conn: sqlite3.Connection, delivery_ids: Iterable[int], stage: str, now_iso: str,
               **fields: Any) -> int:
        """Receipt ``stage`` for the unfinished deliveries that have not reached it yet."""
        ids = sorted(set(delivery_ids))
        if not ids:
            return 0
        rank = STAGE_RANK[stage]
        out = []
        for job in self._jobs(conn, f"delivery_id IN ({_placeholders(len(ids))}) AND outcome IS NULL", ids):
            if STAGE_RANK.get(job.last_stage or "queued", 0) >= rank:
                continue
            conn.execute("UPDATE jobs SET last_stage = ?, updated_at = ? WHERE delivery_id = ?",
                         (stage, now_iso, job.delivery_id))
            out.append((job.credential_id, self._receipt(job, stage=stage, outcome=None, at=now_iso, **fields)))
        return self._post_receipts(conn, out, now_iso)

    def _finish(self, conn: sqlite3.Connection, jobs: Iterable[JobRow], outcome: str, now_iso: str, *,
                state: str | None = None, receipt: bool = True, revision: int | None = None,
                digest: str | None = None, status_code: int | None = None, detail: str | None = None,
                timing: dict[str, int] | None = None, local_detail: str | None = None) -> list[JobRow]:
        """Record ``outcome`` for jobs that have none yet (once, ever) and receipt it; returns those jobs.

        ``state`` also moves the card out of (or keeps it in) the card set; for
        ``displayed`` a content card stays ``active`` and an instruction becomes ``done``.
        """
        finished = []
        receipts = []
        for job in jobs:
            if job.outcome is not None:
                if state is not None and job.state == "active":
                    conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE delivery_id = ?",
                                 (state, now_iso, job.delivery_id))
                continue
            new_state = state
            if outcome == "displayed" and state is None:
                new_state = "done" if job.kind in INSTRUCTION_KINDS else job.state
            stage = "displayed" if outcome == "displayed" else (job.last_stage or "companion_accepted")
            conn.execute(
                "UPDATE jobs SET outcome = ?, state = COALESCE(?, state), last_stage = ?, revision = COALESCE(?, revision),"
                " digest = COALESCE(?, digest), status_code = ?, detail = ?, timing = ?, finished_at = ?,"
                " updated_at = ? WHERE delivery_id = ? AND outcome IS NULL",
                (outcome, new_state, stage, revision, digest, status_code, local_detail or detail,
                 _dumps(timing) if timing else None, now_iso, now_iso, job.delivery_id))
            finished.append(job)
            if receipt:
                receipts.append((job.credential_id, self._receipt(
                    job, stage=stage, outcome=outcome, at=now_iso, status_code=status_code, revision=revision,
                    digest=digest, timing=timing, detail=detail)))
        self._post_receipts(conn, receipts, now_iso)
        return finished

    def _drop(self, conn: sqlite3.Connection, jobs: Iterable[JobRow], state: str, outcome: str, now_iso: str,
              detail: str) -> list[int]:
        """Take cards out of the set because Cremind already ended them (no receipt needed)."""
        tags = []
        for job in jobs:
            conn.execute("UPDATE jobs SET state = ?, outcome = COALESCE(outcome, ?), detail = COALESCE(detail, ?),"
                         " finished_at = COALESCE(finished_at, ?), updated_at = ? WHERE delivery_id = ?",
                         (state, outcome, detail, now_iso, now_iso, job.delivery_id))
            tags.append(job.tag_id)
        return tags

    # -- jobs and the card set -----------------------------------------------------

    def _insert_jobs(self, conn: sqlite3.Connection, credential_id: str, profile: str, jobs: Iterable[Job],
                     now_ts: float, now_iso: str) -> tuple[int, int, set[int]]:
        """Insert new jobs idempotently and maintain each tag's card set; returns (inserted, refused, tags)."""
        inserted = refused = 0
        tags: set[int] = set()
        for job in sorted(jobs, key=lambda j: j.seq):
            row = conn.execute("SELECT epoch, outcome, state FROM jobs WHERE delivery_id = ?",
                               (job.delivery_id,)).fetchone()
            if row is not None:
                if row["epoch"] != job.epoch and row["outcome"] is None:
                    conn.execute("UPDATE jobs SET epoch = ?, updated_at = ? WHERE delivery_id = ?",
                                 (job.epoch, now_iso, job.delivery_id))
                continue
            expires_ts = job.expires_at.timestamp()
            conn.execute(
                "INSERT INTO jobs (delivery_id, credential_id, profile, seq, tag_id, epoch, kind, priority,"
                " replace_key, resolves, card, created_at, expires_at, expires_ts, state, last_stage, received_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'companion_accepted', ?, ?)",
                (job.delivery_id, credential_id, profile, job.seq, job.tag_id, job.epoch, job.kind, job.priority,
                 job.replace_key, job.resolves, _dumps(job.card), iso(job.created_at), iso(job.expires_at),
                 expires_ts, now_iso, now_iso))
            inserted += 1
            new = self._jobs(conn, "delivery_id = ?", (job.delivery_id,))[0]
            self._ensure_view(conn, job.tag_id, now_iso)
            if job.stage in TERMINAL_OUTCOMES:
                # Already final in Cremind (superseded, cancelled, expired … while this companion was away):
                # recorded by its stage, never displayed (connector-api.md "events").
                self._drop(conn, [new], TERMINAL_STATE[job.stage], job.stage, now_iso,
                           f"final in Cremind before it arrived ({job.stage})")
                continue
            problem = card_problem(job.card)
            if problem is not None:
                refused += 1
                self._finish(conn, [new], "failed", now_iso, state="failed", detail=REFUSED_DETAIL,
                             local_detail=f"{REFUSED_DETAIL}: {problem}")
                continue
            if expires_ts <= now_ts:
                self._finish(conn, [new], "expired", now_iso, state="expired")
                continue
            tags.add(job.tag_id)
            self._apply_to_card_set(conn, new, now_iso)
        return inserted, refused, tags

    def _apply_to_card_set(self, conn: sqlite3.Connection, job: JobRow, now_iso: str) -> None:
        """connector-api.md: ``resolved`` removes the card keyed ``resolves``; ``replace_key`` replaces;
        ``clear`` blanks the tag. Cremind already ended the replaced deliveries, so no receipts."""
        tag = job.tag_id
        older = "tag_id = ? AND state = 'active' AND delivery_id < ? AND kind NOT IN ('resolved', 'clear')"
        if job.kind == "clear":
            self._drop(conn, self._jobs(conn, older, (tag, job.delivery_id)), "cancelled", "cancelled", now_iso,
                       "cleared")
            conn.execute("UPDATE tag_views SET blank = 1 WHERE tag_id = ?", (tag,))
            self._mark_dirty(conn, tag, now_iso)
            return
        progress_only = False
        for key, state in ((job.resolves, "resolved"), (job.replace_key, "superseded")):
            if not key:
                continue
            victims = self._jobs(conn, older + " AND replace_key = ?", (tag, job.delivery_id, key))
            if job.kind == "progress" and state == "superseded" and victims \
                    and all(v.kind == "progress" for v in victims):
                progress_only = True
            self._drop(conn, victims, state, "superseded", now_iso, "resolved" if state == "resolved" else "replaced")
        if job.kind not in INSTRUCTION_KINDS:
            conn.execute("UPDATE tag_views SET blank = 0 WHERE tag_id = ?", (tag,))
        self._mark_dirty(conn, tag, now_iso, progress_only=progress_only)

    # -- streams: sync and events -----------------------------------------------------

    def get_stream(self, credential_id: str) -> StreamRow | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM streams WHERE credential_id = ?", (credential_id,)).fetchone()
        return StreamRow.from_row(row) if row is not None else None

    def list_streams(self) -> list[StreamRow]:
        with self.db.reading() as conn:
            return [StreamRow.from_row(r) for r in conn.execute("SELECT * FROM streams ORDER BY credential_id")]

    def apply_sync(self, credential_id: str, result: SyncResult) -> SyncOutcome:
        """Apply ``POST sync``: rebuild from ``outstanding`` when the stream changed, the cursor is
        invalid or this database never synced; otherwise reconcile (connector-api.md ``sync``)."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM streams WHERE credential_id = ?", (credential_id,)).fetchone()
            previous = StreamRow.from_row(row) if row is not None else None
            rebuild = previous is None or previous.stream_id != result.stream_id or not result.cursor_valid
            outstanding = {j.delivery_id: j for j in result.outstanding}
            dropped = 0
            local = self._jobs(conn, "credential_id = ? AND state = 'active'", (credential_id,))
            owned = {t.tag_id for t in result.tags}
            for job in local:
                if job.delivery_id in outstanding:
                    continue
                gone = job.tag_id not in owned
                if rebuild or gone or (job.outcome is None and job.seq <= result.head_seq):
                    dropped += len(self._drop(conn, [job], "cancelled", "cancelled", now_iso,
                                              "no longer outstanding in Cremind"))
            # A delivery Cremind still lists although it ended here: the receipt was lost or refused
            # (e.g. an epoch moved in between) — send the terminal receipt again with today's epoch.
            resend = []
            known = self._jobs(conn, f"delivery_id IN ({_placeholders(len(outstanding))}) AND outcome IS NOT NULL",
                               list(outstanding)) if outstanding else []
            for job in known:
                current = outstanding[job.delivery_id]
                if current.epoch == job.epoch:
                    continue  # the receipt is on its way (the outbox keeps it until Cremind answers)
                conn.execute("UPDATE jobs SET epoch = ? WHERE delivery_id = ?", (current.epoch, job.delivery_id))
                job = self._jobs(conn, "delivery_id = ?", (job.delivery_id,))[0]
                resend.append((job.credential_id, self._receipt(
                    job, stage=job.last_stage or "companion_accepted", outcome=job.outcome, at=job.finished_at or now_iso,
                    status_code=job.status_code, revision=job.revision, digest=job.digest,
                    detail=REFUSED_DETAIL if (job.detail or "").startswith(REFUSED_DETAIL) else job.detail)))
            self._post_receipts(conn, resend, now_iso)
            inserted, _, _ = self._insert_jobs(conn, credential_id, result.profile, result.outstanding, now_ts,
                                               now_iso)
            accepted = [j.delivery_id for j in result.outstanding if j.stage == "queued"]
            cursor = result.head_seq if rebuild else max(previous.after_seq if previous else 0, 0)
            settings_json = _dumps(result.settings.as_json())
            settings_changed = previous is None or previous.settings != result.settings
            conn.execute(
                "INSERT INTO streams (credential_id, profile, companion_id, stream_id, after_seq, head_seq, settings,"
                " synced_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(credential_id) DO UPDATE SET profile = excluded.profile,"
                " companion_id = excluded.companion_id, stream_id = excluded.stream_id,"
                " after_seq = excluded.after_seq, head_seq = excluded.head_seq, settings = excluded.settings,"
                " synced_at = excluded.synced_at, updated_at = excluded.updated_at",
                (credential_id, result.profile, result.companion_id, result.stream_id, cursor, result.head_seq,
                 settings_json, now_iso, now_iso))
            for start in range(0, len(accepted), 500):  # Cremind takes at most 1000 ids per request
                self._enqueue(conn, "accepted", credential_id,
                              {"through_seq": cursor, "delivery_ids": accepted[start:start + 500]}, now_iso)
            self._apply_tag_views(conn, credential_id, result, settings_changed, now_iso)
        return SyncOutcome(rebuild, inserted, dropped, tuple(accepted))

    def _apply_tag_views(self, conn: sqlite3.Connection, credential_id: str, result: SyncResult,
                         settings_changed: bool, now_iso: str) -> None:
        listed = set()
        for info in result.tags:
            listed.add(info.tag_id)
            self._ensure_view(conn, info.tag_id, now_iso)
            row = conn.execute("SELECT * FROM tag_views WHERE tag_id = ?", (info.tag_id,)).fetchone()
            old = TagView.from_row(row)
            # A clear this companion already performed at this epoch wins over a sync that raced the
            # clear_tag result on its way to Cremind.
            clear_required = info.clear_required and int(row["cleared_epoch"]) < info.epoch
            conn.execute(
                "UPDATE tag_views SET credential_id = ?, profile = ?, name = ?, epoch = ?, bridge_hw_id = ?,"
                " rotation = ?, clear_required = ?, cremind_desired = ?, cremind_displayed = ?, updated_at = ?"
                " WHERE tag_id = ?",
                (credential_id, result.profile, info.name, info.epoch, info.bridge_hw_id, info.rotation,
                 int(clear_required), info.desired_revision, info.displayed_revision, now_iso, info.tag_id))
            # Revisions continue above everything Cremind has seen (the allocator never decreases).
            conn.execute("UPDATE tags SET last_revision = MAX(last_revision, ?) WHERE tag_id = ?",
                         (min(MAX_REVISION, max(info.desired_revision, info.displayed_revision)), info.tag_id))
            if (settings_changed or old.credential_id != credential_id or old.name != info.name
                    or old.rotation != info.rotation or old.clear_required != clear_required):
                self._mark_dirty(conn, info.tag_id, now_iso)
        for row in conn.execute("SELECT tag_id FROM tag_views WHERE credential_id = ?", (credential_id,)).fetchall():
            if row["tag_id"] not in listed:
                conn.execute("UPDATE tag_views SET credential_id = NULL, profile = NULL, updated_at = ?"
                             " WHERE tag_id = ?", (now_iso, row["tag_id"]))

    def accept_page(self, credential_id: str, page: EventsPage) -> PageOutcome:
        """One events page in ONE transaction: jobs + the cursor + the ``accepted`` acknowledgement
        (connector-api.md ``POST accepted``: the companion commits jobs and cursor together first)."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT profile, stream_id, after_seq FROM streams WHERE credential_id = ?",
                               (credential_id,)).fetchone()
            if row is None or row["stream_id"] != page.stream_id:
                raise ValueError("the events page belongs to another stream; sync first")
            inserted, refused, _ = self._insert_jobs(conn, credential_id, row["profile"] or "", page.jobs, now_ts,
                                                     now_iso)
            cursor = max(int(row["after_seq"]), page.next_after)
            conn.execute("UPDATE streams SET after_seq = ?, head_seq = MAX(head_seq, ?), updated_at = ?"
                         " WHERE credential_id = ?", (cursor, page.head_seq, now_iso, credential_id))
            ids = [j.delivery_id for j in page.jobs]
            if ids:
                self._enqueue(conn, "accepted", credential_id, {"through_seq": cursor, "delivery_ids": ids}, now_iso)
        return PageOutcome(inserted, refused, cursor)

    # -- tag views ---------------------------------------------------------------------

    def get_view(self, tag_id: int) -> TagView | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM tag_views WHERE tag_id = ?", (tag_id,)).fetchone()
        return TagView.from_row(row) if row is not None else None

    def list_views(self) -> list[TagView]:
        with self.db.reading() as conn:
            return [TagView.from_row(r) for r in conn.execute("SELECT * FROM tag_views ORDER BY tag_id")]

    def mark_dirty(self, tag_id: int, *, force: bool = False) -> None:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            self._mark_dirty(conn, tag_id, now_iso, force=force)

    def set_override(self, tag_id: int, override: str | None, until: float | None, *, force: bool = True) -> None:
        """Show ``override`` (``identify``) until ``until``; ``force=False`` only moves the hold's end
        (the screen already shows it, so no new revision)."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            self._ensure_view(conn, tag_id, now_iso)
            conn.execute("UPDATE tag_views SET override = ?, override_until = ? WHERE tag_id = ?",
                         (override, until, tag_id))
            if force:
                self._mark_dirty(conn, tag_id, now_iso, force=True)

    def tags_needing_work(self) -> tuple[list[int], set[str]]:
        """Tags to (re)compose, and credentials whose tags lack a view (they need a ``sync``)."""
        now = self.clock()
        with self.db.reading() as conn:
            tags = [r["tag_id"] for r in conn.execute(
                "SELECT tag_id FROM tag_views WHERE dirty = 1 OR force = 1 OR progress_pending = 1"
                " OR (override IS NOT NULL AND override_until <= ?) ORDER BY tag_id", (now,))]
            orphans = {r["credential_id"] for r in conn.execute(
                "SELECT DISTINCT j.credential_id FROM jobs j LEFT JOIN tag_views v ON v.tag_id = j.tag_id"
                " WHERE j.state = 'active' AND (v.credential_id IS NULL OR v.credential_id != j.credential_id)"
                " AND j.outcome IS NULL")}
        return tags, orphans

    def compose_input(self, tag_id: int) -> ComposeInput | None:
        """A consistent snapshot of one tag's composition inputs (``None`` when it is not enrolled here)."""
        now = self.clock()
        with self.db.reading() as conn:
            tag = self.db.find_tag(tag_id)
            view_row = conn.execute("SELECT * FROM tag_views WHERE tag_id = ?", (tag_id,)).fetchone()
            if tag is None or view_row is None:
                return None
            view = TagView.from_row(view_row)
            if view.override is not None and (view.override_until or 0) <= now:
                view = dataclasses.replace(view, override=None, override_until=None)
            settings = ProfileSettings()
            if view.credential_id:
                s = conn.execute("SELECT settings FROM streams WHERE credential_id = ?",
                                 (view.credential_id,)).fetchone()
                if s is not None:
                    settings = ProfileSettings.from_json(_loads(s["settings"], {}))
            jobs = self._jobs(conn, "tag_id = ? AND state = 'active' AND credential_id = ? ORDER BY delivery_id",
                              (tag_id, view.credential_id or "")) if view.credential_id else []
            cards = tuple(j for j in jobs if j.kind not in INSTRUCTION_KINDS)
            carry = tuple(j.delivery_id for j in jobs if j.kind in INSTRUCTION_KINDS and j.outcome is None)
            current = self._current_revision(conn, tag_id, view)
            last = conn.execute("SELECT MAX(created_ts) FROM revisions WHERE tag_id = ?", (tag_id,)).fetchone()[0]
        return ComposeInput(tag, view, settings, cards, carry, current, last)

    @staticmethod
    def _current_revision(conn: sqlite3.Connection, tag_id: int, view: TagView) -> RevisionRow | None:
        row = conn.execute(
            "SELECT * FROM revisions WHERE tag_id = ? AND (state IN ('pending', 'sent')"
            " OR (state = 'displayed' AND revision = ?)) ORDER BY revision DESC LIMIT 1",
            (tag_id, view.displayed_revision)).fetchone()
        return RevisionRow.from_row(row) if row is not None else None

    def clear_dirty(self, tag_id: int, dirty_gen: int) -> None:
        """The tag's screen is up to date as of ``dirty_gen`` (a later change keeps it dirty)."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0, updated_at = ?"
                         " WHERE tag_id = ? AND dirty_gen = ?", (now_iso, tag_id, dirty_gen))
            self._expire_override(conn, tag_id, now_ts)

    # -- revisions ---------------------------------------------------------------------

    def create_revision(self, *, tag_id: int, dirty_gen: int, epoch: int, bridge_addr: int | None,
                        fontpack_id: str | None, purpose: str, layout: bytes, layout_digest: str, content_key: str,
                        delivery_ids: Sequence[int], pending_delivery_ids: Sequence[int],
                        preview_png: bytes | None, preview_epoch: int | None = None) -> RevisionRow:
        """Allocate the next revision (``Database.allocate_revision``), supersede older undelivered
        revisions (their deliveries are in this one) and persist the new one with its ``op_id``
        BEFORE anything is sent; the desired preview goes to the outbox in the same transaction."""
        now_ts, now_iso = self._now()
        op_id = self.op_ids.next()
        with self.db.transaction() as conn:
            revision = self.db.allocate_revision(tag_id)
            conn.execute("UPDATE revisions SET state = 'superseded', finished_at = ?, detail = ?"
                         " WHERE tag_id = ? AND state IN ('pending', 'sent') AND revision < ?",
                         (now_iso, f"superseded by revision {revision}", tag_id, revision))
            conn.execute(
                "INSERT INTO revisions (tag_id, revision, epoch, bridge_addr, fontpack_id, purpose, layout,"
                " layout_digest, content_key, delivery_ids, pending_delivery_ids, preview_png, op_id, state,"
                " next_attempt_ts, created_at, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',"
                " ?, ?, ?)",
                (tag_id, revision, epoch, bridge_addr, fontpack_id, purpose, layout, layout_digest, content_key,
                 _dumps(list(delivery_ids)), _dumps(list(pending_delivery_ids)), preview_png, op_id, now_ts,
                 now_iso, now_ts))
            conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0, updated_at = ?"
                         " WHERE tag_id = ? AND dirty_gen = ?", (now_iso, tag_id, dirty_gen))
            self._expire_override(conn, tag_id, now_ts)
            view = conn.execute("SELECT credential_id FROM tag_views WHERE tag_id = ?", (tag_id,)).fetchone()
            if preview_png is not None and view is not None and view["credential_id"]:
                import base64

                self._enqueue(conn, "previews", view["credential_id"], {
                    "tag_id": tag_hw_id(tag_id), "epoch": preview_epoch if preview_epoch is not None else epoch,
                    "revision": revision, "kind": "desired",
                    "png_base64": base64.b64encode(preview_png).decode("ascii"),
                    "delivery_ids": list(delivery_ids)}, now_iso, dedupe_key=f"{tag_hw_id(tag_id)}:desired")
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                               (tag_id, revision)).fetchone()
        return RevisionRow.from_row(row)

    def attach_to_current(self, tag_id: int, current: RevisionRow, carry: Sequence[int], dirty_gen: int,
                          content_key: str) -> None:
        """The screen did not change: instruction deliveries ride on the current revision (or, when it is
        already displayed, are receipted ``displayed`` with it right away)."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                               (tag_id, current.revision)).fetchone()
            if row is not None:
                rev = RevisionRow.from_row(row)
                ids = sorted(set(rev.delivery_ids) | set(carry))
                conn.execute("UPDATE revisions SET delivery_ids = ?, content_key = ? WHERE tag_id = ? AND revision = ?",
                             (_dumps(ids), content_key, tag_id, rev.revision))
                if rev.state == "displayed" and carry:
                    jobs = self._jobs(conn, f"delivery_id IN ({_placeholders(len(carry))})", list(carry))
                    self._finish(conn, jobs, "displayed", now_iso, revision=rev.revision,
                                 digest=rev.frame_digest, status_code=int(Status.OK))
            conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0, updated_at = ?"
                         " WHERE tag_id = ? AND dirty_gen = ?", (now_iso, tag_id, dirty_gen))
            self._expire_override(conn, tag_id, now_ts)

    @staticmethod
    def _expire_override(conn: sqlite3.Connection, tag_id: int, now_ts: float) -> None:
        conn.execute("UPDATE tag_views SET override = NULL, override_until = NULL"
                     " WHERE tag_id = ? AND override IS NOT NULL AND override_until <= ?", (tag_id, now_ts))

    def get_revision(self, tag_id: int, revision: int) -> RevisionRow | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                               (tag_id, revision)).fetchone()
        return RevisionRow.from_row(row) if row is not None else None

    def latest_revision(self, tag_id: int, *, purposes: Sequence[str] | None = None,
                        since_ts: float | None = None) -> RevisionRow | None:
        where = "tag_id = ?"
        params: list[Any] = [tag_id]
        if purposes:
            where += f" AND purpose IN ({_placeholders(len(purposes))})"
            params += list(purposes)
        if since_ts is not None:
            where += " AND created_ts >= ?"
            params.append(since_ts)
        with self.db.reading() as conn:
            row = conn.execute(f"SELECT * FROM revisions WHERE {where} ORDER BY revision DESC LIMIT 1",
                               params).fetchone()
        return RevisionRow.from_row(row) if row is not None else None

    def due_revisions(self) -> list[RevisionRow]:
        now = self.clock()
        with self.db.reading() as conn:
            rows = conn.execute("SELECT * FROM revisions WHERE state = 'pending' AND next_attempt_ts <= ?"
                                " ORDER BY next_attempt_ts, tag_id", (now,)).fetchall()
        return [RevisionRow.from_row(r) for r in rows]

    def next_due_ts(self) -> float | None:
        with self.db.reading() as conn:
            return conn.execute("SELECT MIN(next_attempt_ts) FROM revisions WHERE state = 'pending'").fetchone()[0]

    def prepare_send(self, tag_id: int, revision: int, *, epoch: int, bridge_addr: int,
                     fontpack_id: str) -> RevisionRow | None:
        """The revision as it will be sent. A changed assignment or font pack gets a NEW op id
        (persisted first): the gateway answers a repeated op id from memory without doing the work."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ? AND state = 'pending'",
                               (tag_id, revision)).fetchone()
            if row is None:
                return None
            rev = RevisionRow.from_row(row)
            if (rev.epoch, rev.bridge_addr, rev.fontpack_id) != (epoch, bridge_addr, fontpack_id):
                conn.execute("UPDATE revisions SET epoch = ?, bridge_addr = ?, fontpack_id = ?, op_id = ?"
                             " WHERE tag_id = ? AND revision = ?",
                             (epoch, bridge_addr, fontpack_id, self.op_ids.next(), tag_id, revision))
                rev = RevisionRow.from_row(conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                                                        (tag_id, revision)).fetchone())
        return rev

    def mark_sent(self, tag_id: int, revision: int, op_id: int) -> bool:
        """``DELIVER_LAYOUT`` was accepted: stage ``gateway_received`` for the revision's deliveries."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE revisions SET state = 'sent', sent_at = ?, sent_ts = ?, attempts = attempts + 1,"
                               " last_stage = 'gateway_received' WHERE tag_id = ? AND revision = ? AND op_id = ?"
                               " AND state = 'pending'", (now_iso, now_ts, tag_id, revision, op_id))
            if cur.rowcount == 0:
                return False
            rev = RevisionRow.from_row(conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                                                    (tag_id, revision)).fetchone())
            self._stage(conn, rev.delivery_ids, "gateway_received", now_iso, revision=revision)
        return True

    def defer(self, tag_id: int, revision: int, status: str, delay: float, *, count_attempt: bool = True) -> None:
        """Try again later (BUSY, the gateway unreachable, ...), with the same op id."""
        now_ts, _ = self._now()
        with self.db.transaction() as conn:
            conn.execute("UPDATE revisions SET next_attempt_ts = ?, last_status = ?, attempts = attempts + ?"
                         " WHERE tag_id = ? AND revision = ? AND state = 'pending'",
                         (now_ts + delay, status, int(count_attempt), tag_id, revision))

    def fail_revision(self, tag_id: int, revision: int, status: int, detail: str) -> Effects:
        """A layout-level refusal (``INVALID``, ``TOO_LARGE``): the revision and the cards it shows fail."""
        _, now_iso = self._now()
        effects = Effects(outbox=True, tags={tag_id})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                               (tag_id, revision)).fetchone()
            if row is None:
                return effects
            rev = RevisionRow.from_row(row)
            self._fail_revision(conn, rev, status, detail, now_iso)
        return effects

    def _fail_revision(self, conn: sqlite3.Connection, rev: RevisionRow, status: int, detail: str,
                       now_iso: str) -> None:
        conn.execute("UPDATE revisions SET state = 'failed', last_status = ?, detail = ?, finished_at = ?"
                     " WHERE tag_id = ? AND revision = ?", (status_name(status), detail, now_iso, rev.tag_id,
                                                            rev.revision))
        ids = list(rev.delivery_ids)
        if ids:
            jobs = self._jobs(conn, f"delivery_id IN ({_placeholders(len(ids))})", ids)
            self._finish(conn, jobs, "failed", now_iso, state="failed", status_code=int(status), detail=detail,
                         revision=rev.revision)
        self._mark_dirty(conn, rev.tag_id, now_iso)

    # -- gateway results -------------------------------------------------------------

    def apply_stage(self, tag_id: int, revision: int, stage: str) -> bool:
        """``EVT_STAGE`` (best effort): a non-terminal receipt for the revision's deliveries.

        A revision superseded here while the bridge already transfers it still reaches the tag (its ``OK`` is
        recorded as displayed, :meth:`apply_result`), so its stages count too; cards that left the set meanwhile
        have an outcome and get no receipt."""
        if stage not in STAGE_RANK or stage in ("queued", "companion_accepted", "displayed"):
            return False
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?"
                               " AND state IN ('pending', 'sent', 'superseded')", (tag_id, revision)).fetchone()
            if row is None:
                return False
            rev = RevisionRow.from_row(row)
            if STAGE_RANK.get(rev.last_stage or "queued", 0) < STAGE_RANK[stage]:
                conn.execute("UPDATE revisions SET last_stage = ? WHERE tag_id = ? AND revision = ?",
                             (stage, tag_id, revision))
            return self._stage(conn, rev.delivery_ids, stage, now_iso, revision=revision) > 0

    def apply_result(self, *, update_id: int, tag_id: int, epoch: int, revision: int, status: int, digest: bytes,
                     battery_mv: int, timing: dict[str, int]) -> Effects:
        """``EVT_RESULT`` of a delivery, in the transaction that must commit before the event is ACKed."""
        now_ts, now_iso = self._now()
        effects = Effects(tags={tag_id})
        st = _status(status)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND revision = ?",
                               (tag_id, revision)).fetchone()
            if battery_mv:
                self._contact(conn, tag_id, now_ts, now_iso, battery_mv=battery_mv)
            if row is None:
                return effects
            rev = RevisionRow.from_row(row)
            view_row = conn.execute("SELECT * FROM tag_views WHERE tag_id = ?", (tag_id,)).fetchone()
            view = TagView.from_row(view_row) if view_row is not None else None
            if rev.state == "displayed":
                return effects  # a re-sent result: already applied
            current_attempt = update_id == rev.op_id
            if st == Status.OK:
                # The tag shows it, whatever this database concluded meanwhile (a revision failed on an
                # ambiguous result, or superseded, is recorded displayed; outcomes already reported stay).
                self._displayed(conn, rev, digest[:8].hex(), timing, now_iso, view)
                if view is not None and view.blocked_reason == "not_found":
                    conn.execute("UPDATE tag_views SET blocked_reason = NULL, blocked_epoch = NULL WHERE tag_id = ?",
                                 (tag_id,))
                effects.outbox = True
                return effects
            if rev.state in ("failed", "uncertain"):
                return effects
            if not current_attempt:
                return effects  # the failure of an attempt already replaced (or of an older epoch's attempt)
            conn.execute("UPDATE revisions SET last_status = ? WHERE tag_id = ? AND revision = ?",
                         (status_name(st), tag_id, revision))
            assigned = conn.execute("SELECT epoch FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()
            current_epoch = max(int(assigned["epoch"]) if assigned is not None else 0,
                                view.epoch if view is not None else 0)
            if st in SECURITY_STATUSES and epoch < current_epoch:
                # An attempt under an epoch the tag has moved on from (a bridge still holding the old key):
                # it says nothing about the current assignment.
                return effects
            if rev.state == "superseded" and st != Status.STALE_REVISION \
                    and (st not in SECURITY_STATUSES or st == Status.NOT_FOUND):
                return effects  # a newer revision carries these cards
            if st == Status.NOT_FOUND:
                # §10: ambiguous in EVT_RESULT (the bridge lost the transfer, or the tag refused the id):
                # retried like a link failure, a stop only after repeated NOT_FOUND for this revision.
                count = rev.not_found_count + 1
                conn.execute("UPDATE revisions SET not_found_count = ? WHERE tag_id = ? AND revision = ?",
                             (count, tag_id, revision))
                if count < NOT_FOUND_ESCALATE:
                    conn.execute("UPDATE revisions SET state = 'pending', op_id = ?, next_attempt_ts = ?, detail = ?"
                                 " WHERE tag_id = ? AND revision = ?",
                                 (self.op_ids.next(), now_ts + self.retry_delay(count - 1),
                                  f"NOT_FOUND ({count}/{NOT_FOUND_ESCALATE}); re-delivering", tag_id, revision))
                    return effects
            if st == Status.DISPLAY_STATE_UNKNOWN:
                # §6/§10: re-deliver the SAME revision (new op id); the tag repeats the refresh.
                delay = self.retry_initial_s if rev.uncertain_count < self.uncertain_retries \
                    else self.retry_delay(rev.uncertain_count)
                conn.execute("UPDATE revisions SET state = 'pending', op_id = ?, uncertain_count = uncertain_count + 1,"
                             " next_attempt_ts = ?, detail = 'display state unknown; re-delivering'"
                             " WHERE tag_id = ? AND revision = ?", (self.op_ids.next(), now_ts + delay, tag_id,
                                                                    revision))
                ids = list(rev.delivery_ids)
                if ids:
                    conn.execute(f"UPDATE jobs SET uncertain = 1 WHERE delivery_id IN ({_placeholders(len(ids))})"
                                 " AND outcome IS NULL", ids)
                return effects
            if st == Status.STALE_REVISION:
                self._stale_revision(conn, rev, view, now_iso)
                return effects
            if st == Status.REVISION_CONFLICT:
                conn.execute("UPDATE revisions SET state = 'failed', detail = 'revision conflict at the tag',"
                             " finished_at = ? WHERE tag_id = ? AND revision = ?", (now_iso, tag_id, revision))
                self._mark_dirty(conn, tag_id, now_iso, force=True)
                return effects
            if st == Status.SUPERSEDED:
                conn.execute("UPDATE revisions SET state = 'superseded', finished_at = ? WHERE tag_id = ?"
                             " AND revision = ?", (now_iso, tag_id, revision))
                newer = conn.execute("SELECT 1 FROM revisions WHERE tag_id = ? AND revision > ?"
                                     " AND state IN ('pending', 'sent', 'displayed')", (tag_id, revision)).fetchone()
                if newer is None:
                    self._mark_dirty(conn, tag_id, now_iso, force=True)
                return effects
            if st in SECURITY_STATUSES:
                repeated = f" {NOT_FOUND_ESCALATE} times" if st == Status.NOT_FOUND else ""
                detail = (f"stopped: the bridge or tag answered {status_name(st)}{repeated} for epoch {epoch}; "
                          "the companion re-syncs the tag's assignment")
                self._block(conn, tag_id, status_name(st).lower(), epoch, int(st), detail, now_iso)
                effects.inventory = True
                effects.outbox = True
                if view is not None and view.credential_id:
                    effects.sync.add(view.credential_id)
                return effects
            if st == Status.FONTPACK_MISMATCH:
                detail = f"the bridge's active font pack is not {rev.fontpack_id} (install_fontpack)"
                self._fail_revision(conn, rev, int(st), detail, now_iso)
                self._block(conn, tag_id, "fontpack_mismatch", epoch, int(st), detail, now_iso, fail_jobs=False)
                effects.outbox = True
                effects.inventory = True
                return effects
            if st in LAYOUT_STATUSES:
                self._fail_revision(conn, rev, int(st), f"the layout was refused: {status_name(st)}", now_iso)
                effects.outbox = True
                return effects
            # Link-level and other transient results: same revision, new op id, back-off; the TTL bounds it.
            conn.execute("UPDATE revisions SET state = 'pending', op_id = ?, next_attempt_ts = ?, detail = ?"
                         " WHERE tag_id = ? AND revision = ?",
                         (self.op_ids.next(), now_ts + self.retry_delay(rev.attempts), f"retrying after "
                          f"{status_name(st)}", tag_id, revision))
        return effects

    def _displayed(self, conn: sqlite3.Connection, rev: RevisionRow, digest_hex: str, timing: dict[str, int],
                   now_iso: str, view: TagView | None) -> None:
        import base64

        conn.execute("UPDATE revisions SET state = 'displayed', frame_digest = ?, finished_at = ?, last_stage = ?,"
                     " timing = ?, last_status = 'OK' WHERE tag_id = ? AND revision = ?",
                     (digest_hex, now_iso, "displayed", _dumps(timing), rev.tag_id, rev.revision))
        ids = list(rev.delivery_ids)
        if ids:
            jobs = self._jobs(conn, f"delivery_id IN ({_placeholders(len(ids))})", ids)
            self._finish(conn, jobs, "displayed", now_iso, revision=rev.revision, digest=digest_hex,
                         status_code=int(Status.OK), timing=timing)
        # Older undelivered revisions are moot once a newer screen is up.
        conn.execute("UPDATE revisions SET state = 'superseded', finished_at = ? WHERE tag_id = ?"
                     " AND revision < ? AND state IN ('pending', 'sent')", (now_iso, rev.tag_id, rev.revision))
        self._ensure_view(conn, rev.tag_id, now_iso)
        current = view.displayed_revision if view is not None else 0
        if rev.revision >= current:
            conn.execute("UPDATE tag_views SET displayed_revision = ?, displayed_digest = ?, displayed_at = ?,"
                         " stale_jumps = 0, updated_at = ? WHERE tag_id = ?",
                         (rev.revision, digest_hex, now_iso, now_iso, rev.tag_id))
        credential = view.credential_id if view is not None else None
        if rev.preview_png is not None and credential and rev.revision >= current:  # never over a newer one
            self._enqueue(conn, "previews", credential, {
                "tag_id": tag_hw_id(rev.tag_id), "epoch": rev.epoch, "revision": rev.revision, "kind": "displayed",
                "png_base64": base64.b64encode(rev.preview_png).decode("ascii"), "delivery_ids": ids},
                now_iso, dedupe_key=f"{tag_hw_id(rev.tag_id)}:displayed")

    def _stale_revision(self, conn: sqlite3.Connection, rev: RevisionRow, view: TagView | None,
                        now_iso: str) -> None:
        """``STALE_REVISION``: the tag (or bridge) has seen a higher revision than this database knows —
        e.g. after a restore. The refusal does not say how high, so the allocator skips a block of
        ``STALE_REVISION_JUMP`` revisions above everything used so far, doubling per consecutive refusal
        (at most 256 blocks), and the screen is composed again under the new number."""
        jumps = view.stale_jumps if view is not None else 0
        step = STALE_REVISION_JUMP << min(jumps, 8)
        conn.execute("UPDATE revisions SET state = 'failed', detail = 'stale revision', finished_at = ?"
                     " WHERE tag_id = ? AND revision = ?", (now_iso, rev.tag_id, rev.revision))
        conn.execute("UPDATE revisions SET state = 'superseded', finished_at = ? WHERE tag_id = ?"
                     " AND state IN ('pending', 'sent')", (now_iso, rev.tag_id))
        conn.execute("UPDATE tags SET last_revision = MIN(?, MAX(last_revision, ?) + ?) WHERE tag_id = ?",
                     (MAX_REVISION - 1, rev.revision, step, rev.tag_id))
        self._ensure_view(conn, rev.tag_id, now_iso)
        conn.execute("UPDATE tag_views SET stale_jumps = stale_jumps + 1 WHERE tag_id = ?", (rev.tag_id,))
        self._mark_dirty(conn, rev.tag_id, now_iso, force=True)

    def _block(self, conn: sqlite3.Connection, tag_id: int, reason: str, epoch: int, status_code: int, detail: str,
               now_iso: str, *, fail_jobs: bool = True) -> None:
        """Stop a tag's work (docs/protocol.md §10: security results end the tag's jobs of that epoch)."""
        self._ensure_view(conn, tag_id, now_iso)
        conn.execute("UPDATE tag_views SET blocked_reason = ?, blocked_epoch = ?, updated_at = ? WHERE tag_id = ?",
                     (reason, epoch, now_iso, tag_id))
        if not fail_jobs:
            return
        for row in conn.execute("SELECT * FROM revisions WHERE tag_id = ? AND state IN ('pending', 'sent')"
                                " AND epoch <= ?", (tag_id, epoch)).fetchall():
            conn.execute("UPDATE revisions SET state = 'failed', last_status = ?, detail = ?, finished_at = ?"
                         " WHERE tag_id = ? AND revision = ?", (status_name(status_code), detail, now_iso, tag_id,
                                                                row["revision"]))
        jobs = self._jobs(conn, "tag_id = ? AND state = 'active' AND outcome IS NULL AND epoch <= ?"
                                " AND kind != 'clear'", (tag_id, epoch))
        self._finish(conn, jobs, "failed", now_iso, state="failed", status_code=status_code, detail=detail)

    def unblock(self, tag_id: int, *, reasons: Sequence[str] | None = None) -> bool:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            where = "tag_id = ? AND blocked_reason IS NOT NULL"
            params: list[Any] = [tag_id]
            if reasons:
                where += f" AND blocked_reason IN ({_placeholders(len(reasons))})"
                params += list(reasons)
            cur = conn.execute(f"UPDATE tag_views SET blocked_reason = NULL, blocked_epoch = NULL, updated_at = ?"
                               f" WHERE {where}", (now_iso, *params))
            if cur.rowcount:
                self._mark_dirty(conn, tag_id, now_iso)
            return cur.rowcount > 0

    def unblock_all(self, reason: str) -> list[int]:
        with self.db.reading() as conn:
            tags = [r["tag_id"] for r in conn.execute("SELECT tag_id FROM tag_views WHERE blocked_reason = ?",
                                                      (reason,))]
        for tag in tags:
            self.unblock(tag, reasons=[reason])
        return tags

    def block_tag(self, tag_id: int, reason: str, detail: str, *, status_code: int, epoch: int | None = None,
                  fail_jobs: bool = True) -> Effects:
        """Block a tag from the scheduler (e.g. it is not enrolled on this companion)."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            self._block(conn, tag_id, reason, epoch if epoch is not None else MAX_REVISION, status_code, detail,
                        now_iso, fail_jobs=fail_jobs)
        return Effects(outbox=True)

    def on_session(self, boot_id: int) -> bool:
        """A HELLO answered. When the gateway's ``boot_id`` differs from the last one recorded (also across
        companion restarts), its in-flight state is gone: every sent-but-unresolved revision is sent again."""
        now_ts, _ = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT value FROM daemon_state WHERE key = 'gateway_boot_id'").fetchone()
            conn.execute("INSERT INTO daemon_state (key, value) VALUES ('gateway_boot_id', ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(boot_id),))
            if row is None or int(row["value"]) == boot_id:
                return False
            for rev in conn.execute("SELECT tag_id, revision FROM revisions WHERE state = 'sent'").fetchall():
                conn.execute("UPDATE revisions SET state = 'pending', op_id = ?, next_attempt_ts = ?,"
                             " detail = 'gateway restarted; re-delivering' WHERE tag_id = ? AND revision = ?",
                             (self.op_ids.next(), now_ts, rev["tag_id"], rev["revision"]))
        return True

    def resend_stuck(self, timeout_s: float) -> int:
        """Sent revisions without any result for ``timeout_s``: send again with a new op id
        (a result the gateway dropped from its retention ring is recovered this way, §1.2)."""
        now_ts, _ = self._now()
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT tag_id, revision FROM revisions WHERE state = 'sent' AND sent_ts <= ?",
                                (now_ts - timeout_s,)).fetchall()
            for r in rows:
                conn.execute("UPDATE revisions SET state = 'pending', op_id = ?, next_attempt_ts = ?,"
                             " detail = 'no result; re-delivering' WHERE tag_id = ? AND revision = ?",
                             (self.op_ids.next(), now_ts, r["tag_id"], r["revision"]))
        return len(rows)

    # -- expiry ------------------------------------------------------------------------

    def expire(self) -> Effects:
        """Cards past ``expires_at`` leave the card set; an unreported one is receipted ``expired``
        (``uncertain`` when its last attempt ended ``DISPLAY_STATE_UNKNOWN``)."""
        now_ts, now_iso = self._now()
        effects = Effects()
        with self.db.transaction() as conn:
            jobs = self._jobs(conn, "state = 'active' AND expires_ts <= ?", (now_ts,))
            for job in jobs:
                effects.tags.add(job.tag_id)
                if job.outcome is None:
                    self._finish(conn, [job], "uncertain" if job.uncertain else "expired", now_iso, state="expired",
                                 detail="display state unknown when the job expired" if job.uncertain else None)
                    effects.outbox = True
                else:
                    conn.execute("UPDATE jobs SET state = 'expired', updated_at = ? WHERE delivery_id = ?",
                                 (now_iso, job.delivery_id))
            for tag in effects.tags:
                self._mark_dirty(conn, tag, now_iso)
        return effects

    def next_expiry_ts(self) -> float | None:
        with self.db.reading() as conn:
            return conn.execute("SELECT MIN(expires_ts) FROM jobs WHERE state = 'active'").fetchone()[0]

    # -- assignment side effects ---------------------------------------------------------

    def on_assigned(self, tag_id: int, *, epoch: int, bridge_addr: int, bridge_hw_id: str | None) -> None:
        """``assign_tag`` succeeded: record it in the inventory and move the tag's unfinished work to the
        new epoch/bridge (Cremind moved the active deliveries too); a block ends."""
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            self.db.set_assignment(tag_id, bridge_addr, epoch)
            conn.execute("UPDATE jobs SET epoch = ?, updated_at = ? WHERE tag_id = ? AND outcome IS NULL"
                         " AND state = 'active' AND epoch < ?", (epoch, now_iso, tag_id, epoch))
            self._ensure_view(conn, tag_id, now_iso)
            conn.execute("UPDATE tag_views SET epoch = MAX(epoch, ?), bridge_hw_id = COALESCE(?, bridge_hw_id),"
                         " blocked_reason = NULL, blocked_epoch = NULL, updated_at = ? WHERE tag_id = ?",
                         (epoch, bridge_hw_id, now_iso, tag_id))
            for rev in conn.execute("SELECT revision FROM revisions WHERE tag_id = ? AND state IN ('pending', 'sent')",
                                    (tag_id,)).fetchall():
                conn.execute("UPDATE revisions SET state = 'pending', epoch = ?, bridge_addr = ?, op_id = ?,"
                             " next_attempt_ts = ? WHERE tag_id = ? AND revision = ?",
                             (epoch, bridge_addr, self.op_ids.next(), now_ts, tag_id, rev["revision"]))
            self._mark_dirty(conn, tag_id, now_iso)

    def cleared_at(self, tag_id: int, epoch: int) -> bool:
        """Whether this companion already cleared the tag at ``epoch`` (and nothing was shown since)."""
        with self.db.reading() as conn:
            row = conn.execute("SELECT cleared_epoch, displayed_revision FROM tag_views WHERE tag_id = ?",
                               (tag_id,)).fetchone()
        return row is not None and int(row["cleared_epoch"]) >= epoch and int(row["displayed_revision"]) == 0

    def on_cleared(self, tag_id: int, epoch: int) -> None:
        """``clear_tag`` succeeded: the tag shows white at revision 0 of ``epoch`` and the bridge forgot
        its history (§10). Earlier owners' cards are gone (Cremind cancelled them at the claim);
        content of the current epoch resumes."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            self._ensure_view(conn, tag_id, now_iso)
            conn.execute("UPDATE tag_views SET clear_required = 0, cleared_epoch = MAX(cleared_epoch, ?), blank = 1,"
                         " displayed_revision = 0, displayed_digest = NULL, displayed_at = ?, blocked_reason = NULL,"
                         " blocked_epoch = NULL, override = NULL, override_until = NULL, updated_at = ?"
                         " WHERE tag_id = ?", (epoch, now_iso, now_iso, tag_id))
            conn.execute("UPDATE revisions SET state = 'superseded', finished_at = ?, detail = 'cleared'"
                         " WHERE tag_id = ? AND state IN ('pending', 'sent')", (now_iso, tag_id))
            self._drop(conn, self._jobs(conn, "tag_id = ? AND state = 'active' AND epoch < ?", (tag_id, epoch)),
                       "cancelled", "cancelled", now_iso, "tag cleared for a new assignment")
            current = conn.execute("SELECT 1 FROM jobs WHERE tag_id = ? AND state = 'active' AND kind NOT IN"
                                   " ('resolved', 'clear')", (tag_id,)).fetchone()
            if current is not None:
                self._mark_dirty(conn, tag_id, now_iso)
            else:
                conn.execute("UPDATE tag_views SET dirty = 0, force = 0, progress_pending = 0 WHERE tag_id = ?",
                             (tag_id,))

    # -- telemetry -----------------------------------------------------------------------

    @staticmethod
    def _contact(conn: sqlite3.Connection, tag_id: int, now_ts: float, now_iso: str, *, battery_mv: int | None = None,
                 rssi: int | None = None) -> None:
        conn.execute(
            "INSERT INTO device_status (hw_id, kind, battery_mv, rssi, last_contact_at, last_contact_ts, updated_at)"
            " VALUES (?, 'tag', ?, ?, ?, ?, ?) ON CONFLICT(hw_id) DO UPDATE SET"
            " battery_mv = COALESCE(excluded.battery_mv, device_status.battery_mv),"
            " rssi = COALESCE(excluded.rssi, device_status.rssi), last_contact_at = excluded.last_contact_at,"
            " last_contact_ts = excluded.last_contact_ts, updated_at = excluded.updated_at",
            (tag_hw_id(tag_id), battery_mv or None, rssi, now_iso, now_ts, now_iso))

    def record_sighting(self, tag_id: int, *, rssi: int, battery_mv: int) -> None:
        now_ts, now_iso = self._now()
        with self.db.transaction() as conn:
            self._contact(conn, tag_id, now_ts, now_iso, battery_mv=battery_mv, rssi=rssi)

    def device_status(self) -> dict[str, dict[str, Any]]:
        with self.db.reading() as conn:
            return {r["hw_id"]: dict(r) for r in conn.execute("SELECT * FROM device_status")}

    # -- outbox --------------------------------------------------------------------------

    def enqueue(self, kind: str, credential_id: str, payload: dict[str, Any], *, dedupe_key: str | None = None) -> None:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            self._enqueue(conn, kind, credential_id, payload, now_iso, dedupe_key)

    def due_outbox(self, credential_id: str, limit: int = 50) -> list[OutboxRow]:
        """Rows to send now, oldest first. Receipts go out strictly in order: a receipts row waiting for its
        retry holds back the newer receipts rows (:data:`RECEIPTS_IN_ORDER`), so a later stage or outcome never
        overtakes an earlier one and an outage costs one receipts request per back-off, not one per row."""
        now = self.clock()
        with self.db.reading() as conn:
            rows = conn.execute("SELECT * FROM outbox WHERE credential_id = ? AND dead = 0 AND next_attempt_ts <= ?"
                                f" AND {RECEIPTS_IN_ORDER} ORDER BY id LIMIT ?",
                                (credential_id, now, now, limit)).fetchall()
        return [OutboxRow.from_row(r) for r in rows]

    def next_outbox_ts(self, credential_id: str) -> float | None:
        """When :meth:`due_outbox` next returns something: the oldest receipts row decides for the receipts."""
        with self.db.reading() as conn:
            return conn.execute(
                "SELECT MIN(next_attempt_ts) FROM outbox WHERE credential_id = ? AND dead = 0 AND (kind != 'receipts'"
                " OR id = (SELECT MIN(id) FROM outbox WHERE credential_id = ? AND dead = 0 AND kind = 'receipts'))",
                (credential_id, credential_id)).fetchone()[0]

    def outbox_done(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        with self.db.transaction() as conn:
            conn.execute(f"DELETE FROM outbox WHERE id IN ({_placeholders(len(ids))})", list(ids))

    def outbox_retry(self, ids: Sequence[int], error: str, delay: float) -> None:
        now = self.clock()
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE outbox SET attempts = attempts + 1, next_attempt_ts = ?, last_error = ?"
                         f" WHERE id IN ({_placeholders(len(ids))})", (now + delay, error[:500], *ids))

    def outbox_dead(self, ids: Sequence[int], error: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE outbox SET dead = 1, attempts = attempts + 1, last_error = ?"
                         f" WHERE id IN ({_placeholders(len(ids))})", (error[:500], *ids))

    def outbox_counts(self) -> dict[str, int]:
        with self.db.reading() as conn:
            return {f"{r['kind']}{'_dead' if r['dead'] else ''}": r["n"] for r in conn.execute(
                "SELECT kind, dead, COUNT(*) AS n FROM outbox GROUP BY kind, dead")}

    # -- commands ------------------------------------------------------------------------

    def command_begin(self, command_id: str, kind: str, args: dict[str, Any], expires_ts: float | None) -> CommandRow:
        """Persist a command BEFORE claiming it, so a crash after the claim still finds it here."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO commands (command_id, kind, args, state, expires_at, expires_ts,"
                         " created_at, updated_at) VALUES (?, ?, ?, 'claiming', ?, ?, ?, ?)",
                         (command_id, kind, _dumps(args), ts_iso(expires_ts) if expires_ts else None, expires_ts,
                          now_iso, now_iso))
            return CommandRow.from_row(conn.execute("SELECT * FROM commands WHERE command_id = ?",
                                                    (command_id,)).fetchone())

    def get_command(self, command_id: str) -> CommandRow | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
        return CommandRow.from_row(row) if row is not None else None

    def command_state(self, command_id: str, state: str) -> None:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            conn.execute("UPDATE commands SET state = ?, updated_at = ? WHERE command_id = ?",
                         (state, now_iso, command_id))

    def command_forget(self, command_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM commands WHERE command_id = ? AND state = 'claiming'", (command_id,))

    def command_progress(self, command_id: str, **values: Any) -> dict[str, Any]:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT progress FROM commands WHERE command_id = ?", (command_id,)).fetchone()
            progress = _loads(row["progress"], {}) if row is not None else {}
            progress.update(values)
            conn.execute("UPDATE commands SET progress = ?, updated_at = ? WHERE command_id = ?",
                         (_dumps(progress), now_iso, command_id))
        return progress

    def command_finish(self, command_id: str, status: str, *, credential_id: str, result: dict[str, Any] | None,
                       error: str | None) -> None:
        """The command's outcome and its ``POST result`` (outbox) in one transaction."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE commands SET state = ?, result = ?, error = ?, updated_at = ?"
                               " WHERE command_id = ? AND state IN ('claiming', 'running')",
                               ("succeeded" if status == "succeeded" else "failed", _dumps(result) if result else None,
                                error, now_iso, command_id))
            if cur.rowcount:
                payload: dict[str, Any] = {"command_id": command_id, "status": status}
                if result is not None:
                    payload["result"] = result
                if error is not None:
                    payload["error"] = error[:1000]
                self._enqueue(conn, "command_result", credential_id, payload, now_iso, dedupe_key=command_id)

    def unfinished_commands(self) -> list[CommandRow]:
        with self.db.reading() as conn:
            return [CommandRow.from_row(r) for r in conn.execute(
                "SELECT * FROM commands WHERE state IN ('claiming', 'running') ORDER BY created_at")]

    def op_begin(self, op_id: int, kind: str, command_id: str | None, boot_id: int | None) -> None:
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO gateway_ops (op_id, command_id, kind, boot_id, created_at)"
                         " VALUES (?, ?, ?, ?, ?)", (op_id, command_id, kind, boot_id, now_iso))

    def record_op_result(self, op_id: int, status: int, fields: dict[str, Any]) -> bool:
        """A retained result of a command's gateway op (committed before the ACK); False if unknown."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE gateway_ops SET status = COALESCE(status, ?), result = COALESCE(result, ?),"
                               " done_at = COALESCE(done_at, ?) WHERE op_id = ?",
                               (int(status), _dumps(fields), now_iso, op_id))
            return cur.rowcount > 0

    def op_result(self, op_id: int) -> OpResult | None:
        with self.db.reading() as conn:
            row = conn.execute("SELECT * FROM gateway_ops WHERE op_id = ?", (op_id,)).fetchone()
        if row is None or row["status"] is None:
            return None
        return OpResult(row["op_id"], row["kind"], row["status"], _loads(row["result"], {}))

    def is_op(self, op_id: int) -> bool:
        with self.db.reading() as conn:
            return conn.execute("SELECT 1 FROM gateway_ops WHERE op_id = ?", (op_id,)).fetchone() is not None

    # -- statistics and the CLI -------------------------------------------------------------

    def queue_stats(self) -> dict[str, Any]:
        now = self.clock()
        with self.db.reading() as conn:
            pending = conn.execute("SELECT COUNT(*), MIN(received_at) FROM jobs WHERE state = 'active'"
                                   " AND outcome IS NULL").fetchone()
            oldest = None
            if pending[1]:
                from ..connector.models import parse_time

                oldest = max(0.0, now - parse_time(pending[1]).timestamp())
            states = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
            revisions = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM revisions GROUP BY state")}
            blocked = {r["tag_id"]: r["blocked_reason"] for r in conn.execute(
                "SELECT tag_id, blocked_reason FROM tag_views WHERE blocked_reason IS NOT NULL")}
        return {"depth": int(pending[0]), "oldest_age_s": round(oldest) if oldest is not None else 0,
                "jobs": states, "revisions": revisions, "outbox": self.outbox_counts(),
                "blocked": {tag_hw_id(k): v for k, v in blocked.items()}}

    def list_jobs(self, *, include_finished: bool = False, tag_id: int | None = None,
                  limit: int = 100) -> list[JobRow]:
        where = "1 = 1" if include_finished else "(state = 'active' OR outcome IS NULL)"
        params: list[Any] = []
        if tag_id is not None:
            where += " AND tag_id = ?"
            params.append(tag_id)
        with self.db.reading() as conn:
            return self._jobs(conn, f"{where} ORDER BY delivery_id DESC LIMIT ?", (*params, limit))

    def get_job(self, delivery_id: int) -> JobRow | None:
        with self.db.reading() as conn:
            rows = self._jobs(conn, "delivery_id = ?", (delivery_id,))
        return rows[0] if rows else None

    def revisions_with(self, delivery_id: int) -> list[RevisionRow]:
        with self.db.reading() as conn:
            rows = conn.execute("SELECT r.* FROM revisions r, json_each(r.delivery_ids) d WHERE d.value = ?"
                                " UNION SELECT r.* FROM revisions r, json_each(r.pending_delivery_ids) d"
                                " WHERE d.value = ? ORDER BY revision", (delivery_id, delivery_id)).fetchall()
        return [RevisionRow.from_row(r) for r in rows]

    def list_revisions(self, tag_id: int | None = None, limit: int = 50) -> list[RevisionRow]:
        with self.db.reading() as conn:
            if tag_id is None:
                rows = conn.execute("SELECT * FROM revisions ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM revisions WHERE tag_id = ? ORDER BY revision DESC LIMIT ?",
                                    (tag_id, limit)).fetchall()
        return [RevisionRow.from_row(r) for r in rows]

    def retry(self, *, delivery_ids: Sequence[int] = (), tag_ids: Sequence[int] = ()) -> list[int]:
        """Operator retry: unblock the tags, send their pending revisions now and compose afresh."""
        _, now_iso = self._now()
        tags = set(tag_ids)
        with self.db.transaction() as conn:
            if delivery_ids:
                tags |= {j.tag_id for j in self._jobs(conn, f"delivery_id IN ({_placeholders(len(delivery_ids))})",
                                                      list(delivery_ids))}
            for tag in tags:
                conn.execute("UPDATE tag_views SET blocked_reason = NULL, blocked_epoch = NULL WHERE tag_id = ?",
                             (tag,))
                conn.execute("UPDATE revisions SET next_attempt_ts = 0 WHERE tag_id = ? AND state = 'pending'", (tag,))
                self._mark_dirty(conn, tag, now_iso, force=True)
            conn.execute("UPDATE outbox SET next_attempt_ts = 0 WHERE dead = 0")
        return sorted(tags)

    def cancel_job(self, delivery_id: int) -> JobRow | None:
        """Operator cancel: the card leaves the set and Cremind is told ``cancelled``."""
        _, now_iso = self._now()
        with self.db.transaction() as conn:
            jobs = self._jobs(conn, "delivery_id = ?", (delivery_id,))
            if not jobs:
                return None
            job = jobs[0]
            if job.outcome is None:
                self._finish(conn, [job], "cancelled", now_iso, state="cancelled", detail="cancelled on the companion")
            elif job.state == "active":
                conn.execute("UPDATE jobs SET state = 'cancelled', updated_at = ? WHERE delivery_id = ?",
                             (now_iso, delivery_id))
            self._mark_dirty(conn, job.tag_id, now_iso)
            return self._jobs(conn, "delivery_id = ?", (delivery_id,))[0]

    def purge(self, *, older_than_s: float, dead_outbox: bool = True) -> dict[str, int]:
        """Delete finished jobs/revisions older than ``older_than_s`` (never active cards or current screens)."""
        cutoff_ts = self.clock() - older_than_s
        cutoff = ts_iso(cutoff_ts)
        with self.db.transaction() as conn:
            jobs = conn.execute("DELETE FROM jobs WHERE state != 'active' AND outcome IS NOT NULL"
                                " AND updated_at < ?", (cutoff,)).rowcount
            revisions = conn.execute(
                "DELETE FROM revisions WHERE state IN ('superseded', 'failed', 'uncertain') AND created_ts < ?"
                " OR (state = 'displayed' AND created_ts < ? AND revision < (SELECT COALESCE(MAX(v.displayed_revision),"
                " 0) FROM tag_views v WHERE v.tag_id = revisions.tag_id))", (cutoff_ts, cutoff_ts)).rowcount
            outbox = conn.execute("DELETE FROM outbox WHERE dead = 1").rowcount if dead_outbox else 0
            commands = conn.execute("DELETE FROM commands WHERE state IN ('succeeded', 'failed') AND updated_at < ?"
                                    " AND command_id NOT IN (SELECT dedupe_key FROM outbox WHERE"
                                    " kind = 'command_result')", (cutoff,)).rowcount
            ops = conn.execute("DELETE FROM gateway_ops WHERE created_at < ?", (cutoff,)).rowcount
        return {"jobs": jobs, "revisions": revisions, "outbox": outbox, "commands": commands, "gateway_ops": ops}


__all__ = [
    "INSTRUCTION_KINDS", "LAYOUT_STATUSES", "LINK_STATUSES", "NOT_FOUND_ESCALATE", "RECEIPTS_IN_ORDER",
    "SECURITY_STATUSES", "STAGES", "STAGE_RANK", "STALE_REVISION_JUMP", "CommandRow", "ComposeInput", "Effects", "JobRow", "OpResult", "OutboxRow",
    "PageOutcome", "QueueStore", "RevisionRow", "StreamRow", "SyncOutcome", "TagView", "status_name", "ts_iso",
]
