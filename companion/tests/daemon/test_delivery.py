"""The daemon end to end against the fake Cremind and the simulator: jobs become screens on tags."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cremind_tag.daemon.store import STALE_REVISION_JUMP
from cremind_tag.protocol.ids import Status
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(150)


async def displayed(rig: Any, *delivery_ids: int, timeout: float = 30.0) -> None:
    await rig.wait(lambda: all(rig.stage(d) == "displayed" for d in delivery_ids), timeout,
                   what=f"deliveries {delivery_ids} displayed")


def revisions(rig: Any) -> list[dict[str, Any]]:
    with rig.db() as db, db.reading() as conn:
        return [dict(r) for r in conn.execute("SELECT tag_id, revision, state, purpose, delivery_ids, last_status"
                                              " FROM revisions ORDER BY revision")]


def test_job_is_composed_delivered_and_receipted(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Approve deployment?", kind="needs_input")
            await displayed(rig, did)
            delivery = rig.fake.delivery(did)
            assert delivery["revision"] >= 1 and len(delivery["digest"]) == 16
            assert delivery["status_code"] == int(Status.OK)
            assert set(delivery["timing"]) >= {"wake_ms", "mesh_ms", "transfer_ms", "refresh_ms"}
            # what was receipted is the frame digest the tag itself computed
            assert rig.sim_tag().displayed_digest[:8].hex() == delivery["digest"]
            stages = [r["stage"] for r in rig.fake.receipt_log if r["delivery_id"] == did]
            assert stages[0] == "gateway_received" and stages[-1] == "displayed"
            assert did in rig.fake.accepted_log
            await rig.wait(lambda: (rig.hw(), "displayed") in rig.fake.previews, what="displayed preview")
            assert (rig.hw(), "desired") in rig.fake.previews
            preview = rig.fake.previews[(rig.hw(), "displayed")]
            assert preview["revision"] == delivery["revision"] and preview["png"].startswith(b"\x89PNG")
            assert rig.fake.tag(rig.hw())["displayed_revision"] == delivery["revision"]
            await rig.wait(lambda: any(d.get("displayed_revision") == delivery["revision"]
                                       for h in rig.fake.heartbeats for d in h["devices"]), what="heartbeat")
            inventory = rig.fake.inventories[-1]
            assert inventory["tags"][0]["tag_id"] == rig.hw() and inventory["tags"][0]["epoch"] == 1
            assert inventory["bridges"][0]["hw_id"] == rig.bridge_hw()
            assert svc.status_snapshot()["gateway"]["connected"]
            assert [r["revision"] for r in revisions(rig)] == [delivery["revision"]]  # no empty screen first
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_footer_cards_stay_pending_until_shown(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            await rig.start()
            ids = [rig.fake.add_job("alice", rig.hw(), title=f"Update number {i}", priority=40 + i)
                   for i in range(7)]
            await rig.wait(lambda: any(r["state"] == "sent" and len(eval(r["delivery_ids"])) >= 2
                                       for r in revisions(rig)), what="a screen with several cards sent")
            rig.sim_tag().out_of_range = False
            await rig.wait(lambda: sum(rig.stage(d) == "displayed" for d in ids) >= 2, what="some displayed")
            await asyncio.sleep(0.5)
            shown = [d for d in ids if rig.stage(d) == "displayed"]
            waiting = [d for d in ids if d not in shown]
            assert 2 <= len(shown) <= 4 and waiting, (shown, waiting)
            for d in waiting:  # counted in the footer only: not displayed, and never superseded either
                assert rig.stage(d) in ("companion_accepted", "gateway_received"), rig.fake.delivery(d)
                assert rig.job_state(d) == ("active", None)
            # the highest priorities are the ones on the screen
            assert set(shown) == set(sorted(ids, reverse=True)[:len(shown)])
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_resolved_and_replaced_cards_leave_the_screen(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            question = rig.fake.add_job("alice", rig.hw(), kind="needs_input", title="Approve plan?",
                                        replace_key="run:7:input")
            note = rig.fake.add_job("alice", rig.hw(), title="Build finished", replace_key="run:8")
            await displayed(rig, question, note)
            newer = rig.fake.add_job("alice", rig.hw(), title="Build finished again", replace_key="run:8")
            answered = rig.fake.add_job("alice", rig.hw(), kind="resolved", title="Answered",
                                        resolves="run:7:input")
            await displayed(rig, newer, answered)
            last = max(revisions(rig), key=lambda r: r["revision"])
            assert eval(last["delivery_ids"]) == [newer, answered]  # the question and the old note are gone
            assert rig.job_state(question)[0] == "resolved" and rig.job_state(note)[0] == "superseded"
            assert rig.job_state(answered) == ("done", "displayed")
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_clear_job_blanks_the_tag(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Something")
            await displayed(rig, first)
            clear = rig.fake.add_job("alice", rig.hw(), kind="clear", title="Cleared", cancel_first=True)
            await displayed(rig, clear)
            last = max(revisions(rig), key=lambda r: r["revision"])
            assert last["purpose"] == "blank" and eval(last["delivery_ids"]) == [clear]
            tag = rig.sim_tag()
            assert tag.panel_planes == tag.spec.white_planes()
            later = rig.fake.add_job("alice", rig.hw(), title="After the clear")
            await displayed(rig, later)
            assert rig.job_state(first)[0] == "cancelled"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_refused_card_is_never_shown(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            otp = rig.fake.add_job("alice", rig.hw(), title="Your verification code is 482913")
            token = rig.fake.add_job("alice", rig.hw(), title="Deploy", body="Authorization: Bearer abcdEFGH12345678")
            shell = rig.fake.add_job("alice", rig.hw(), title="Output", body="$ rm -rf build\nremoved")
            fine = rig.fake.add_job("alice", rig.hw(), title="Deploy finished", body="password=[redacted]")
            await displayed(rig, fine)
            await rig.wait(lambda: all(rig.stage(d) == "failed" for d in (otp, token, shell)), what="refusals")
            for d in (otp, token, shell):
                assert rig.fake.delivery(d)["detail"] == "refused_by_companion"
            for rev in revisions(rig):
                assert not {otp, token, shell} & set(eval(rev["delivery_ids"]))
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_expired_job_is_receipted_expired(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            await rig.start()
            short = rig.fake.add_job("alice", rig.hw(), title="Brief", ttl_s=1.5)
            await rig.wait(lambda: rig.job_state(short)[1] == "expired", 10, what="expiry")
            assert rig.job_state(short)[0] == "expired"
            await rig.wait(lambda: any(r["delivery_id"] == short and r["outcome"] == "expired"
                                       for r in rig.fake.receipt_log), 10, what="the expired receipt")
            assert rig.stage(short) == "expired"
            rig.sim_tag().out_of_range = False
            lasting = rig.fake.add_job("alice", rig.hw(), title="Lasting")
            await displayed(rig, lasting)
            last = max(revisions(rig), key=lambda r: r["revision"])
            assert eval(last["delivery_ids"]) == [lasting]
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_power_loss_redelivers_the_same_revision(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().faults.power_loss = 1
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Refresh interrupted")
            await displayed(rig, did)
            revs = revisions(rig)
            assert len(revs) == 1 and revs[0]["state"] == "displayed"  # the SAME revision, re-delivered
            assert rig.sim_tag().stats["power_losses"] == 1
            assert rig.fake.delivery(did)["revision"] == revs[0]["revision"]
            outcomes = [r["outcome"] for r in rig.fake.receipt_log if r["delivery_id"] == did and r["outcome"]]
            assert outcomes == ["displayed"]

    run_scenario(scenario(), timeout=100)


def test_unknown_display_state_until_expiry_is_uncertain(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(settings={"uncertain_retries": 1}) as rig:
            rig.sim_tag().faults.power_loss = 1000
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Never sure", ttl_s=2.0)
            await rig.wait(lambda: rig.stage(did) == "uncertain", 15, what="uncertain")
            assert rig.fake.delivery(did)["detail"] == "display state unknown when the job expired"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_stale_revision_jumps_the_allocator(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Before the restore")
            await displayed(rig, first)
            shown = rig.fake.delivery(first)["revision"]
            await rig.stop()
            # A restored companion database: the allocator is behind what the bridge and tag have seen.
            with rig.db() as db, db.transaction() as conn:
                conn.execute("DELETE FROM revisions")
                conn.execute("UPDATE tags SET last_revision = 0")
                conn.execute("UPDATE tag_views SET displayed_revision = 0")
            rig.fake.tag(rig.hw())["desired_revision"] = rig.fake.tag(rig.hw())["displayed_revision"] = 0
            await rig.start()
            second = rig.fake.add_job("alice", rig.hw(), title="After the restore")
            await displayed(rig, second)
            revs = revisions(rig)
            assert any(r["last_status"] == "STALE_REVISION" for r in revs), revs
            assert rig.fake.delivery(second)["revision"] > STALE_REVISION_JUMP > shown
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_bridge_answers_a_repeated_revision_from_its_stored_ack(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Shown once")
            await displayed(rig, did)
            await rig.stop()
            # The result is lost before it was recorded: the revision goes out again.
            with rig.db() as db, db.transaction() as conn:
                conn.execute("UPDATE revisions SET state = 'pending', op_id = op_id + 1")
            await rig.start()
            await rig.wait(lambda: all(r["state"] == "displayed" for r in revisions(rig)), what="re-displayed")
            await asyncio.sleep(0.3)
            assert rig.sim.bridge(0).counters["duplicates"] == 1
            assert rig.sim_tag().stats["refreshes"] == 1  # the panel was not refreshed twice
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_gateway_reboot_redelivers_what_was_in_flight(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            svc = await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Across a reboot")
            await rig.wait(lambda: rig.stage(did) in ("gateway_received", "bridge_received"), what="in flight")
            boot = rig.sim.gateway.boot_id
            assert svc.gateway is not None
            await svc.gateway.reboot()
            await rig.wait(lambda: rig.sim.gateway.boot_id != boot and svc.boot_generation >= 1, what="reboot")
            rig.sim_tag().out_of_range = False
            await displayed(rig, did)
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_progress_updates_follow_the_cadence(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.fake.settings("alice", progress_cadence_s=2.0)
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), kind="progress", title="Indexing", replace_key="run:1",
                                     progress={"done": 1, "total": 10})
            await displayed(rig, first)
            count = len(revisions(rig))
            ids = []
            for done in range(2, 7):
                ids.append(rig.fake.add_job("alice", rig.hw(), kind="progress", title="Indexing",
                                            replace_key="run:1", progress={"done": done, "total": 10}))
                await asyncio.sleep(0.15)
            assert len(revisions(rig)) == count  # progress-only changes wait for the cadence
            await displayed(rig, ids[-1], timeout=10)
            assert len(revisions(rig)) == count + 1
            for d in ids[:-1]:
                assert rig.stage(d) == "superseded"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_a_lost_layout_status_is_not_a_failure(make_rig: Any) -> None:
    """Review regression: a lost LAYOUT_STATUS OK used to come back as EVT_RESULT NOT_FOUND and block the tag."""

    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim.mesh.faults.drop_status = 1
            await rig.start()
            older = rig.fake.add_job("alice", rig.hw(), title="Older card")
            await displayed(rig, older)
            rig.sim.mesh.faults.drop_status = 1  # the next delivery's OK is lost once
            newer = rig.fake.add_job("alice", rig.hw(), title="Newer card")
            await displayed(rig, older, newer)
            assert rig.sim.bridge(0).counters["commit_repeats"] >= 1
            assert rig.svc is not None and rig.svc.store.get_view(rig.tag_id()).blocked_reason is None
            assert not [r for r in rig.fake.receipt_log if r.get("outcome") == "failed"]
            assert all(r["last_status"] in ("OK", None) for r in revisions(rig))
            later = rig.fake.add_job("alice", rig.hw(), title="Still delivered")
            await displayed(rig, later)
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_no_busy_loop_while_the_gateway_is_away(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(settings={"scan_interval_s": 1.0}) as rig:
            svc = await rig.start(gateway_url="socket://127.0.0.1:9")  # nothing listens there
            passes = 0
            original = svc.scheduler.pass_once

            async def counting() -> float | None:
                nonlocal passes
                passes += 1
                return await original()

            svc.scheduler.pass_once = counting  # type: ignore[method-assign]
            rig.fake.add_job("alice", rig.hw(), title="Waits for the gateway")
            await rig.wait(lambda: any(r["state"] == "pending" for r in revisions(rig)), what="a pending revision")
            before = passes
            await asyncio.sleep(3.0)
            assert passes - before <= 6, passes - before  # about one per scan interval (plus wake-ups)
            assert svc.gateway_hw_id is None  # never connected: no gateway id invented from the URL

    run_scenario(scenario(), timeout=100)


def test_gateway_id_is_derived_per_session(make_rig: Any) -> None:
    from cremind_tag.cli._hardware import gateway_hw_id

    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            await rig.wait(lambda: svc.gateway_hw_id is not None, what="the first session")
            assert svc.gateway_hw_id == gateway_hw_id(rig.sim.gateway_url)
            beat = await svc.hardware.build_heartbeat() if svc.hardware else {}
            assert {"hw_id": svc.gateway_hw_id, "kind": "gateway", "status": "ok"} in beat["devices"]

    run_scenario(scenario(), timeout=100)


def test_a_cremind_cancel_resolves_a_card_that_is_on_its_way(make_rig: Any) -> None:
    """A cancel arrives as a `resolved` job naming the card's replace_key: the card leaves the screen being
    delivered and is never receipted displayed."""

    async def scenario() -> None:
        async with make_rig() as rig:
            rig.sim_tag().out_of_range = True
            await rig.start()
            keep = rig.fake.add_job("alice", rig.hw(), title="Stays")
            gone = rig.fake.add_job("alice", rig.hw(), title="Cancelled before it was shown")
            assert rig.fake.delivery(gone)["replace_key"] == f"delivery:{gone}"
            await rig.wait(lambda: any(gone in eval(r["delivery_ids"]) and r["state"] == "sent"
                                       for r in revisions(rig)), what="the card on its way")
            resolving = rig.fake.cancel(gone)
            await rig.wait(lambda: rig.job_state(gone)[0] == "resolved", what="the cancel applied")
            rig.sim_tag().out_of_range = False
            await displayed(rig, keep, resolving)
            last = max(revisions(rig), key=lambda r: r["revision"])
            assert gone not in eval(last["delivery_ids"]) and keep in eval(last["delivery_ids"])
            assert rig.stage(gone) == "cancelled"
            assert not [r for r in rig.fake.receipt_log if r["delivery_id"] == gone and r.get("outcome")]
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=100)


def test_previews_carry_their_epoch_and_a_refusal_is_not_retried(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Previewed")
            await displayed(rig, first)
            await rig.wait(lambda: (rig.hw(), "displayed") in rig.fake.previews, what="displayed preview")
            assert {p.get("epoch") for p in rig.fake.preview_log} == {1}
            rig.fake.tag(rig.hw())["epoch"] = 5  # Cremind moved on (an assignment the companion has not run yet)
            worker = svc.content_workers[rig.content_cred.id]
            syncs = worker.syncs
            rig.fake.add_job("alice", rig.hw(), title="Previewed at the old epoch")
            await rig.wait(lambda: any(p.get("refused") == "epoch_mismatch" for p in rig.fake.preview_log),
                           what="a refused preview")
            await rig.wait(lambda: worker.syncs > syncs, what="the re-sync")
            with rig.db() as db, db.reading() as conn:
                rows = conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'previews'").fetchone()[0]
            assert rows == 0  # dropped, not retried forever (and not kept as dead)

    run_scenario(scenario(), timeout=100)
