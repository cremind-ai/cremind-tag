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
    from cremind_tag.store import BridgeRecord

    db = open_database(tmp_path / "c.sqlite3")
    for addr in (2, 3):
        db.upsert_bridge(BridgeRecord(f"{addr:032x}", addr=addr, configured=True))
    db.insert_tag(TagRecord(TAG, 16, 1, 400, 300, 1, 1, "file:tag:1A2B3C4D", epoch=1, bridge_addr=2))
    return QueueStore(db)


def outbox(s: QueueStore, kind: str) -> list[dict[str, Any]]:
    with s.db.reading() as conn:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE kind = ? ORDER BY id",
                                                       (kind,))]


def test_schema_version(tmp_path: Path) -> None:
    with open_database(tmp_path / "c.sqlite3") as db:
        assert db.schema_version == QUEUE_MIGRATIONS[-1].version == 3
    with open_database(tmp_path / "c.sqlite3") as db:  # re-open: nothing to do
        assert [v for v, _, _ in db.applied_migrations()] == [1, 2, 3]


def test_a_v2_database_upgrades(tmp_path: Path) -> None:
    from cremind_tag.store import Database

    path = tmp_path / "old.sqlite3"
    with Database.open(path, extra_migrations=QUEUE_MIGRATIONS[:1]) as db:  # what b6c48b3 created
        db.insert_tag(TagRecord(TAG, 16, 1, 400, 300, 1, 1, "file:tag:1A2B3C4D", epoch=1))
        with db.transaction() as conn:
            conn.execute("INSERT INTO revisions (tag_id, revision, epoch, layout, layout_digest, content_key, op_id,"
                         " state, created_at, created_ts) VALUES (?, 1, 1, x'00', 'd', 'k', 7, 'sent', 'now', 0)",
                         (TAG,))
    with open_database(path) as db:
        assert db.schema_version == 3
        rev = QueueStore(db).get_revision(TAG, 1)
        assert rev is not None and rev.not_found_count == 0 and rev.state == "sent"


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


def sent_revision(s: QueueStore, delivery_ids: list[int], *, epoch: int = 1) -> Any:
    rev = s.create_revision(tag_id=TAG, dirty_gen=-1, epoch=epoch, bridge_addr=2, fontpack_id="00" * 8,
                            purpose="screen", layout=b"x", layout_digest="d", content_key="k",
                            delivery_ids=delivery_ids, pending_delivery_ids=[], preview_png=None)
    assert s.mark_sent(TAG, rev.revision, rev.op_id)
    return s.get_revision(TAG, rev.revision)


def result(s: QueueStore, rev: Any, status: int, *, epoch: int = 1, update_id: int | None = None) -> None:
    s.apply_result(update_id=rev.op_id if update_id is None else update_id, tag_id=TAG, epoch=epoch,
                   revision=rev.revision, status=status, digest=bytes(range(8)), battery_mv=0, timing={})


def test_not_found_is_retried_then_escalated_and_a_late_ok_still_counts(tmp_path: Path) -> None:
    from cremind_tag.daemon.store import NOT_FOUND_ESCALATE
    from cremind_tag.protocol.ids import Status

    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1)], 1))
    rev = sent_revision(s, [501])
    for attempt in range(1, NOT_FOUND_ESCALATE):
        result(s, rev, Status.NOT_FOUND)
        again = s.get_revision(TAG, rev.revision)
        assert (again.state, again.not_found_count) == ("pending", attempt) and again.op_id != rev.op_id
        assert s.get_view(TAG).blocked_reason is None and s.get_job(501).outcome is None
        assert s.mark_sent(TAG, again.revision, again.op_id)
        rev = s.get_revision(TAG, rev.revision)
    result(s, rev, Status.NOT_FOUND)  # the third: a stop
    assert s.get_view(TAG).blocked_reason == "not_found" and s.get_job(501).outcome == "failed"
    assert s.get_revision(TAG, rev.revision).state == "failed"
    result(s, rev, Status.OK)  # the screen was displayed after all
    assert s.get_revision(TAG, rev.revision).state == "displayed"
    view = s.get_view(TAG)
    assert view.blocked_reason is None and view.displayed_revision == rev.revision


def test_a_security_result_of_an_old_epoch_attempt_is_ignored(tmp_path: Path) -> None:
    from cremind_tag.protocol.ids import Status

    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1)], 1))
    old = sent_revision(s, [501])  # sent at epoch 1 (op X)
    s.on_assigned(TAG, epoch=2, bridge_addr=3, bridge_hw_id=None)
    newer = sent_revision(s, [501], epoch=2)
    result(s, old, Status.STALE_EPOCH, epoch=1, update_id=old.op_id)  # the old bridge, long after
    assert s.get_view(TAG).blocked_reason is None
    assert s.get_revision(TAG, newer.revision).state == "sent"
    assert s.get_job(501).outcome is None
    result(s, newer, Status.STALE_EPOCH, epoch=2)  # the current attempt at the current epoch: a real stop
    assert s.get_view(TAG).blocked_reason == "stale_epoch"


def test_block_fails_only_revisions_of_that_epoch(tmp_path: Path) -> None:
    from cremind_tag.protocol.ids import Status

    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1)], 1))
    rev = sent_revision(s, [501])
    with s.db.transaction() as conn:  # a later revision already at epoch 2 (the tag moved meanwhile)
        conn.execute("INSERT INTO revisions (tag_id, revision, epoch, layout, layout_digest, content_key, op_id,"
                     " state, created_at, created_ts) VALUES (?, 99, 2, x'00', 'd', 'k', 42, 'pending', 'now', 0)",
                     (TAG,))
    result(s, rev, Status.AUTH_FAILED)
    assert s.get_revision(TAG, rev.revision).state == "failed"
    assert s.get_revision(TAG, 99).state == "pending"


def test_a_progress_update_during_composition_is_not_lost(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.apply_sync("cred", sync_result([], 0))
    s.accept_page("cred", EventsPage("s1", (job(501, 1, kind="progress", replace_key="run:1"),), next_after=1,
                                     head_seq=1))
    snapshot = s.compose_input(TAG)  # the scheduler starts composing with card 501 ...
    assert snapshot is not None
    s.accept_page("cred", EventsPage("s1", (job(502, 2, kind="progress", replace_key="run:1"),), next_after=2,
                                     head_seq=2))  # ... while 502 replaces it
    s.create_revision(tag_id=TAG, dirty_gen=snapshot.view.dirty_gen, epoch=1, bridge_addr=2, fontpack_id="00" * 8,
                      purpose="screen", layout=b"x", layout_digest="d", content_key="k", delivery_ids=[501],
                      pending_delivery_ids=[], preview_png=None)
    view = s.get_view(TAG)
    assert view.progress_pending  # still to do
    assert TAG in s.tags_needing_work()[0]
    assert s.get_job(502).state == "active"


def test_receipts_are_posted_in_order_after_a_failed_post(tmp_path: Path) -> None:
    """A receipts row waiting for its retry holds back newer receipts rows: a later stage (the terminal
    outcome) never overtakes an earlier one, and an outage costs one receipts POST per back-off."""
    now = [1000.0]
    s = store(tmp_path)
    s.clock = lambda: now[0]
    for payload in ({"receipts": [{"delivery_id": 501, "stage": "transferring"}]},
                    {"receipts": [{"delivery_id": 501, "stage": "displayed", "outcome": "displayed"}]}):
        s.enqueue("receipts", "cred", payload)
    s.enqueue("previews", "cred", {"tag_id": "1A2B3C4D", "revision": 1})
    rows = s.due_outbox("cred", 50)
    assert [r.kind for r in rows] == ["receipts", "receipts", "previews"]
    s.outbox_retry([rows[0].id], "HTTP 503", 30.0)  # the first POST failed (it carried only the first row)
    s.enqueue("receipts", "cred", {"receipts": [{"delivery_id": 502, "stage": "gateway_received"}]})
    assert [r.kind for r in s.due_outbox("cred", 50)] == ["previews"]  # other kinds keep their own back-off
    assert s.next_outbox_ts("cred") <= now[0]  # the preview is due now
    s.outbox_done([r.id for r in s.due_outbox("cred", 50)])
    assert s.due_outbox("cred", 50) == []
    assert s.next_outbox_ts("cred") == 1030.0  # the oldest receipts row decides
    now[0] = 1030.0
    due = s.due_outbox("cred", 50)
    assert [r.payload["receipts"][0]["stage"] for r in due] == ["transferring", "displayed", "gateway_received"]


def test_stages_of_a_revision_superseded_while_in_flight_are_receipted(tmp_path: Path) -> None:
    """A newer screen supersedes revision R here while the bridge already transfers R to the tag: R's stages
    (and its OK) still happen on the tag, so its cards get transferring/refreshing, not a jump to displayed."""
    from cremind_tag.protocol.ids import Status

    s = store(tmp_path)
    s.apply_sync("cred", sync_result([job(501, 1), job(502, 2, replace_key="k502")], 2))
    older = sent_revision(s, [501])
    sent_revision(s, [501, 502])  # composed after 502 arrived: supersedes the older one
    assert s.get_revision(TAG, older.revision).state == "superseded"
    assert s.apply_stage(TAG, older.revision, "transferring")
    assert s.apply_stage(TAG, older.revision, "refreshing")
    result(s, older, Status.OK)
    stages = [r["stage"] for p in outbox(s, "receipts") for r in p["receipts"] if r["delivery_id"] == 501]
    assert stages == ["gateway_received", "transferring", "refreshing", "displayed"]
    with s.db.transaction() as conn:  # a card that left the set (resolved) is not receipted by a stale screen
        conn.execute("UPDATE jobs SET outcome = 'superseded', state = 'resolved' WHERE delivery_id = 502")
    assert not s.apply_stage(TAG, older.revision + 1, "transferring")  # 501 is displayed, 502 left the set
