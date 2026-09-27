"""Resynchronisation: expired cursors, a restored Cremind, a lost local database, revoked credentials."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connector.client import Credential
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(150)

IN_FLIGHT = ("gateway_received", "bridge_received", "transferring")


def shown_ids(rig: Any) -> list[int]:
    with rig.db() as db, db.reading() as conn:
        row = conn.execute("SELECT delivery_ids FROM revisions ORDER BY created_ts DESC, revision DESC"
                           " LIMIT 1").fetchone()
    return eval(row["delivery_ids"]) if row else []


def test_expired_cursor_triggers_sync(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Before")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            worker = next(iter(svc.content_workers.values()))
            syncs = worker.syncs
            rig.fake.expire_cursor_once = True  # the next events call answers 410 cursor_expired
            later = rig.fake.add_job("alice", rig.hw(), title="After the 410")
            await rig.wait(lambda: rig.stage(later) == "displayed", what="displayed after the resync")
            assert worker.syncs == syncs + 1
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_offline_beyond_retention_rebuilds_at_start(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Before the outage")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            await rig.stop()
            later = [rig.fake.add_job("alice", rig.hw(), title=f"While away {i}") for i in range(3)]
            rig.fake.prune("alice", rig.fake.streams["alice"].head)  # retention moved past our cursor
            await rig.start()
            await rig.wait(lambda: all(rig.stage(d) == "displayed" for d in later), what="rebuilt and displayed")
            with rig.db() as db, db.reading() as conn:
                cursor = conn.execute("SELECT after_seq FROM streams").fetchone()[0]
            assert cursor == rig.fake.streams["alice"].head
            assert rig.job_state(first)[0] == "cancelled"  # a rebuild keeps only what Cremind lists
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_restored_cremind_changes_the_stream(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            await rig.start()
            old = rig.fake.add_job("alice", rig.hw(), title="Lost in the restore")
            await rig.wait(lambda: rig.stage(old) in IN_FLIGHT, what="old job in flight")
            rig.fake.restore("alice")  # cancels it, new stream_id, ids jump by 2**32
            new = rig.fake.add_job("alice", rig.hw(), title="After the restore")
            assert new > 1 << 32
            rig.sim_tag().out_of_range = False
            await rig.wait(lambda: rig.stage(new) == "displayed", what="new job displayed")
            assert old not in shown_ids(rig)
            assert rig.job_state(old)[0] == "cancelled"
            assert rig.stage(old) == "cancelled"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_lost_local_database_recovers_from_outstanding(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Shown before the loss")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            shown = rig.fake.delivery(first)["revision"]
            await rig.stop()
            rig.sim_tag().out_of_range = True
            pending = rig.fake.add_job("alice", rig.hw(), title="Waiting in Cremind", kind="needs_input")
            for suffix in ("", "-wal", "-shm"):
                Path(str(rig.db_path) + suffix).unlink(missing_ok=True)
            rig.register()  # the inventory is re-created (e.g. from a backup of the enrollment)
            await rig.start()
            await rig.wait(lambda: rig.stage(pending) in IN_FLIGHT, what="rebuilt from outstanding")
            rig.sim_tag().out_of_range = False
            await rig.wait(lambda: rig.stage(pending) == "displayed", what="pending displayed")
            # revisions continue above what Cremind had seen, so the tag never refuses them
            assert rig.fake.delivery(pending)["revision"] > shown
            with rig.db() as db, db.reading() as conn:
                statuses = [r[0] for r in conn.execute("SELECT last_status FROM revisions")]
            assert "STALE_REVISION" not in statuses
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_revoked_credential_stops_only_its_loop(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(tags=2) as rig:
            bob = rig.fake.add_credential("content", "bob")
            rig.fake.tag(rig.hw(1))["owner_profile"] = "bob"
            options = {"content_credentials": [Credential(rig.content_cred.id, rig.content_cred.secret),
                                               Credential(bob.id, bob.secret)]}
            svc = await rig.start(**options)
            a = rig.fake.add_job("alice", rig.hw(0), title="For alice")
            b = rig.fake.add_job("bob", rig.hw(1), title="For bob")
            await rig.wait(lambda: rig.stage(a) == rig.stage(b) == "displayed", what="both displayed")
            rig.fake.revoke(rig.content_cred.id)
            a2 = rig.fake.add_job("alice", rig.hw(0), title="Never fetched")
            b2 = rig.fake.add_job("bob", rig.hw(1), title="Still delivered")
            await rig.wait(lambda: rig.stage(b2) == "displayed", what="bob still served")
            await rig.wait(lambda: svc.content_workers[rig.content_cred.id].state == "stopped", what="alice stopped")
            assert "credential_revoked" in (svc.content_workers[rig.content_cred.id].error or "")
            assert rig.stage(a2) == "queued"
            beats = len(rig.fake.heartbeats)
            await asyncio.sleep(0.8)
            assert len(rig.fake.heartbeats) > beats  # the hardware credential is unaffected
            status = svc.status_snapshot()["credentials"]
            assert status[rig.content_cred.id]["state"] == "stopped"
            assert status[bob.id]["state"] == "running"

    run_scenario(scenario(), timeout=100)


def test_transient_errors_are_retried(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.fake.fail_next["receipts"] = [503, 502, 500]
            rig.fake.fail_next["events"] = [503]
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Despite the hiccups")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="displayed despite 5xx")
            assert not rig.fake.fail_next["receipts"] and not rig.fake.fail_next["events"]

    run_scenario(scenario(), timeout=100)


def test_an_epoch_mismatch_rejection_resyncs_and_resends(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            svc = await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Epoch moves under it")
            await rig.wait(lambda: rig.stage(did) in IN_FLIGHT, what="in flight")
            worker = svc.content_workers[rig.content_cred.id]
            syncs = worker.syncs
            rig.fake.delivery(did)["epoch"] = 2  # Cremind moved the delivery to another epoch meanwhile
            rig.sim_tag().out_of_range = False
            await rig.wait(lambda: any(r["delivery_id"] == did and r["reason"] == "epoch_mismatch"
                                       for r in rig.fake.rejected_log), what="the rejection")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="re-sent with the new epoch")
            assert worker.syncs > syncs
            final = [r for r in rig.fake.receipt_log if r["delivery_id"] == did and r["outcome"] == "displayed"]
            assert [r["epoch"] for r in final][-1] == 2
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_a_tls_error_pauses_and_retries_instead_of_stopping(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(settings={"tls_retry_s": 0.5}) as rig:
            rig.fake.tls_fail_next = 3  # the first requests meet an untrusted certificate
            svc = await rig.start()
            await rig.wait(lambda: any(v.get("state") == "tls_error"
                                       for v in svc.status_snapshot()["credentials"].values()), what="reported")
            did = rig.fake.add_job("alice", rig.hw(), title="After the certificate was fixed")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="displayed after the retry")
            assert not svc.failed_credentials
            await rig.wait(lambda: not svc.credential_warnings, what="the warning cleared")

    run_scenario(scenario(), timeout=100)


def test_jobs_already_final_in_cremind_are_recorded_not_shown(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Before the break")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            await rig.stop()
            cancelled = rig.fake.add_job("alice", rig.hw(), title="Cancelled while away")
            rig.fake._end(rig.fake.delivery(cancelled), "cancelled", "cancelled by the user")
            later = rig.fake.add_job("alice", rig.hw(), title="Shown after the break")
            await rig.start()
            await rig.wait(lambda: rig.stage(later) == "displayed", what="the live job displayed")
            assert rig.job_state(cancelled) == ("cancelled", "cancelled")
            with rig.db() as db, db.reading() as conn:
                shown = [eval(r[0]) for r in conn.execute("SELECT delivery_ids FROM revisions")]
            assert all(cancelled not in ids for ids in shown)
            assert not [r for r in rig.fake.receipt_log if r["delivery_id"] == cancelled]

    run_scenario(scenario(), timeout=100)
