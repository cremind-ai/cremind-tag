"""The queue store's transactions: idempotent acceptance, once-only outcomes, sync reconciliation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from cremind_tag.connector.models import EventsPage, Job, ProfileSettings, SyncResult, TagInfo
from cremind_tag.daemon import QUEUE_MIGRATIONS, open_database
from cremind_tag.daemon.store import QueueStore
from cremind_tag.store import TagRecord

TAG = 0x1A2B3C4D
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def job(delivery_id: int, seq: int, *, title: str = "Hello", kind: str = "notification", ttl_s: float = 3600,
        replace_key: str | None = None, resolves: str | None = None, stage: str = "queued", epoch: int = 1) -> Job:
    return Job(delivery_id=delivery_id, seq=seq, tag_id=TAG, epoch=epoch, kind=kind, priority=40,
               replace_key=replace_key, resolves=resolves, created_at=NOW, expires_at=NOW + dt.timedelta(seconds=ttl_s),
               stage=stage, card={"v": 1, "kind": kind, "title": title})


def sync_result(outstanding: list[Job], head: int, *, stream: str = "s1", valid: bool = True) -> SyncResult:
    return SyncResult(profile="alice", companion_id="c1", stream_id=stream, cursor_valid=valid, oldest_seq=1,
                      head_seq=head, outstanding=tuple(outstanding),
                      tags=(TagInfo(TAG, "Desk", 1, "br-" + "0" * 32, 400, 300, 1, 0, 5, 4, False),),
                      settings=ProfileSettings())


def store(tmp_path: Path) -> QueueStore:
    db = open_database(tmp_path / "c.sqlite3")
    db.insert_tag(TagRecord(TAG, 16, 1, 400, 300, 1, 1, "file:tag:1A2B3C4D", epoch=1))
    return QueueStore(db)


def outbox(s: QueueStore, kind: str) -> list[dict[str, Any]]:
    with s.db.reading() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE kind = ? ORDER BY id",
                                                       (kind,))]


def test_schema_version(tmp_path: Path) -> None:
    with open_database(tmp_path / "c.sqlite3") as db:
        assert db.schema_version == QUEUE_MIGRATIONS[-1].version == 2
    with open_database(tmp_path / "c.sqlite3") as db:  # re-open: nothing to do
        assert [v for v, _, _ in db.applied_migrations()] == [1, 2]


def test_events_page_commits_jobs_cursor_and_accepted_together(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.apply_sync("cred", sync_result([], 0))
    page = EventsPage("s1", (job(501, 1), job(502, 2)), next_after=2, head_seq=2)
    first = s.accept_page("cred", page)
    again = s.accept_page("cred", page)  # a page fetched twice (crash before the cursor moved elsewhere)
    assert (first.inserted, again.inserted) == (2, 0)
    assert s.get_stream("cred").after_seq == 2
    assert [p["delivery_ids"] for p in outbox(s, "accepted")] == [[501, 502], [501, 502]]
    assert [j.delivery_id for j in s.list_jobs()] == [502, 501]


def test_outcome_is_written_once_and_stages_never_go_back(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1)], 1))
    rev = s.create_revision(tag_id=TAG, dirty_gen=0, epoch=1, bridge_addr=None, fontpack_id="00" * 8,
                            purpose="screen", layout=b"x", layout_digest="d", content_key="k", delivery_ids=[501],
                            pending_delivery_ids=[], preview_png=None)
    assert s.mark_sent(TAG, rev.revision, rev.op_id)
    assert not s.mark_sent(TAG, rev.revision, rev.op_id)  # only pending -> sent
    assert s.apply_stage(TAG, rev.revision, "transferring")
    assert not s.apply_stage(TAG, rev.revision, "bridge_received")  # never backwards
    ok = dict(update_id=rev.op_id, tag_id=TAG, epoch=1, revision=rev.revision, status=0, digest=bytes(range(8)),
              battery_mv=2900, timing={"refresh_ms": 1})
    s.apply_result(**ok)
    s.apply_result(**ok)  # the same retained event again: nothing new
    s.expire()
    receipts = [r for p in outbox(s, "receipts") for r in p["receipts"]]
    assert [(r["stage"], r["outcome"]) for r in receipts] == [
        ("gateway_received", None), ("transferring", None), ("displayed", "displayed")]
    assert receipts[-1]["digest"] == "0001020304050607" and receipts[-1]["revision"] == rev.revision
    assert s.get_job(501).state == "active"  # displayed cards stay on the screen


def test_sync_reconciles_without_losing_displayed_cards(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1), job(502, 2)], 2))
    with s.db.transaction() as conn:  # 501 was displayed (terminal in Cremind); 502 still waits
        conn.execute("UPDATE jobs SET outcome = 'displayed' WHERE delivery_id = 501")
    outcome = s.apply_sync("cred", sync_result([job(503, 3)], 3))  # 502 was cancelled in Cremind
    assert outcome.dropped == 1 and not outcome.rebuilt
    assert s.get_job(501).state == "active" and s.get_job(502).state == "cancelled"
    assert s.get_job(503).state == "active"
    # Cremind still lists a delivery that ended here: the terminal receipt is sent again.
    with s.db.transaction() as conn:
        conn.execute("UPDATE jobs SET outcome = 'displayed', revision = 9 WHERE delivery_id = 503")
    s.apply_sync("cred", sync_result([job(503, 3, epoch=2)], 3))
    resent = [r for p in outbox(s, "receipts") for r in p["receipts"] if r["delivery_id"] == 503]
    assert resent and resent[-1]["outcome"] == "displayed" and resent[-1]["epoch"] == 2
    # A new stream (Cremind restored): rebuild from outstanding only.
    rebuilt = s.apply_sync("cred", sync_result([job(900, 1)], 1, stream="s2"))
    assert rebuilt.rebuilt and s.get_job(501).state == "cancelled" and s.get_job(900).state == "active"
    assert s.get_stream("cred").after_seq == 1


def test_card_set_rules(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.apply_sync("cred", sync_result([], 0))
    s.accept_page("cred", EventsPage("s1", (
        job(501, 1, kind="needs_input", replace_key="run:1:input"),
        job(502, 2, replace_key="note"),
        job(503, 3, replace_key="note"),
        job(504, 4, kind="resolved", resolves="run:1:input"),
        job(505, 5, title="code 123456"),
        job(506, 6, stage="superseded"),
        job(507, 7, ttl_s=-5),
    ), next_after=7, head_seq=7))
    states = {j.delivery_id: (j.state, j.outcome) for j in s.list_jobs(include_finished=True)}
    assert states[501] == ("resolved", "superseded") and states[502] == ("superseded", "superseded")
    assert states[503] == ("active", None) and states[504] == ("active", None)
    assert states[505] == ("failed", "failed") and states[506] == ("superseded", "superseded")
    assert states[507] == ("expired", "expired")
    refused = [r for p in outbox(s, "receipts") for r in p["receipts"] if r["delivery_id"] == 505]
    assert refused[0]["detail"] == "refused_by_companion"
    inp = s.compose_input(TAG)
    assert inp is not None and [c.delivery_id for c in inp.cards] == [503] and inp.carry == (504,)
