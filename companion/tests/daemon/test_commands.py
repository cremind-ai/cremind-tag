"""Hardware commands from Cremind executed by the daemon against the simulator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connector.client import Credential
from cremind_tag.protocol.ids import Status
from cremind_tag.protocol.tag_txn import StoredState
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(150)

REPO = Path(__file__).resolve().parents[3]
FIXTURE_PACK = REPO / "protocol" / "fixtures" / "fontpack_test.ctfp"


async def command_done(rig: Any, command_id: str, timeout: float = 30.0) -> dict[str, Any]:
    await rig.wait(lambda: rig.fake.commands[command_id]["status"] in ("succeeded", "failed"), timeout,
                   what=f"command {rig.fake.commands[command_id]['kind']}")
    return rig.fake.commands[command_id]


def revisions(rig: Any) -> list[dict[str, Any]]:
    with rig.db() as db, db.reading() as conn:
        return [dict(r) for r in conn.execute("SELECT revision, state, purpose, delivery_ids FROM revisions"
                                              " ORDER BY revision")]


def test_claim_holds_content_until_the_tag_is_cleared(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(owner=None) as rig:
            # First without the hardware credential: nothing can execute the claim's commands.
            await rig.start(hardware_credential=None)
            assign, clear = rig.fake.claim_tag(rig.hw(), "alice", rig.bridge_hw())
            did = rig.fake.add_job("alice", rig.hw(), title="For the new owner")
            await rig.wait(lambda: rig.job_state(did)[0] == "active", what="job accepted")
            await asyncio.sleep(1.0)
            assert revisions(rig) == []  # held: the screen still belongs to the previous owner
            assert rig.fake.commands[assign]["status"] == "queued"
            await rig.stop()
            # Now with the hardware credential: assign_tag (epoch 2) then clear_tag, then the content.
            await rig.start()
            assert (await command_done(rig, assign))["status"] == "succeeded"
            cleared = await command_done(rig, clear)
            assert cleared["status"] == "succeeded", cleared
            assert rig.fake.tag(rig.hw())["clear_required"] is False
            await rig.wait(lambda: rig.stage(did) == "displayed", what="content after the clear")
            tag = rig.sim_tag()
            assert tag.nvs.stored_epoch == 2
            assert tag.nvs.record is not None and tag.nvs.record.state == StoredState.DISPLAYED
            assert rig.fake.delivery(did)["epoch"] == 2
            receipts = [r for r in rig.fake.receipt_log if r["delivery_id"] == did]
            assert receipts and all(r["epoch"] == 2 for r in receipts)
            await rig.wait(lambda: rig.fake.inventories[-1]["tags"][0]["epoch"] == 2, what="inventory at epoch 2")
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)


def test_assign_moves_the_tag_to_another_bridge(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(bridges=2) as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="On bridge one")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            old_bridge = rig.sim.bridge(0)
            command = rig.fake.assign(rig.hw(), rig.bridge_hw(1))
            done = await command_done(rig, command)
            assert done["status"] == "succeeded", done
            assert done["result"]["epoch"] == 2 and done["result"]["unassign_previous"] == "OK"
            assert rig.tag_id() not in old_bridge.assignments  # the old key is gone
            assert rig.sim.bridge(1).assignments[rig.tag_id()].epoch == 2
            second = rig.fake.add_job("alice", rig.hw(), title="On bridge two")
            await rig.wait(lambda: rig.stage(second) == "displayed", what="second displayed")
            assert rig.fake.delivery(second)["epoch"] == 2
            assert rig.sim_tag().nvs.stored_epoch == 2
            with rig.db() as db:
                tag = db.get_tag(rig.tag_id())
            assert (tag.epoch, tag.bridge_addr) == (2, rig.sim.bridge(1).addr)
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)


def test_stale_epoch_heals_through_the_reported_epoch_floor(make_rig: Any) -> None:
    """§10: a STALE_EPOCH whose stored_epoch is above every epoch known here (an assignment this companion
    lost, a restore) is recorded as the tag's epoch floor and reported as its inventory epoch; Cremind
    re-queues assign_tag above it and the refused card is shown at the new epoch, never failed."""
    async def scenario() -> None:
        async with make_rig(bridges=2) as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Shown at epoch 1")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            # The tag authenticated epoch 3 elsewhere (e.g. a restored companion): it now refuses the
            # epoch-1 key this companion still uses, three sessions in a row.
            rig.sim_tag().nvs.stored_epoch = 3
            refused = rig.fake.add_job("alice", rig.hw(), title="Refused at 1, shown at 4")
            await rig.wait(lambda: rig.fake.epoch_raises, 60, what="Cremind raising the epoch")
            assert rig.fake.epoch_raises == [(rig.hw(), 3, 4)]  # reported the floor 3, re-queued at 4
            assert any(t["epoch"] == 3 for inv in rig.fake.inventories for t in inv["tags"])
            await rig.wait(lambda: rig.stage(refused) == "displayed", 60, what="the card after the reassignment")
            delivery = rig.fake.delivery(refused)
            assert delivery["epoch"] == 4 and delivery["outcome"] == "displayed"
            assert rig.sim_tag().nvs.stored_epoch == 4
            view = rig.svc.store.get_view(rig.tag_id())
            assert view is not None and view.epoch_floor == 3 and view.blocked_reason is None
            assert rig.svc.db.find_tag(rig.tag_id()).epoch == 4
            await rig.wait(lambda: rig.fake.inventories[-1]["tags"][0]["epoch"] == 4, what="inventory at epoch 4")
            assert not any(r.get("outcome") == "failed" for r in rig.fake.receipt_log)
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)


def test_assign_to_a_full_bridge_fails_bridge_full(make_rig: Any) -> None:
    """ASSIGN_SET NO_RESOURCES (the bridge's table is full) fails assign_tag at once with
    {"error": "bridge_full", "max_tags": n}; the inventory reports each bridge's max_tags and assigned."""
    async def scenario() -> None:
        rig = make_rig(tags=2, bridges=2)
        rig.sim.bridges[0].max_tags = 1  # tag 0 fills it; the gateway learns max_tags from CAPS at boot
        async with rig:
            await rig.start()
            full, other = rig.bridge_hw(0), rig.bridge_hw(1)
            await rig.wait(lambda: any(b.get("max_tags") == 1 for inv in rig.fake.inventories
                                       for b in inv["bridges"]), what="max_tags in the inventory")
            bridges = {b["hw_id"]: b for b in rig.fake.inventories[-1]["bridges"]}
            assert (bridges[full]["max_tags"], bridges[full]["assigned"]) == (1, 1)
            assert (bridges[other]["max_tags"], bridges[other]["assigned"]) == (20, 1)
            command = rig.fake.assign(rig.hw(1), full)
            done = await command_done(rig, command)
            assert done["status"] == "failed", done
            assert done["error"] == "bridge_full" and done["result"] == {"error": "bridge_full", "max_tags": 1}
            assert rig.fake.tag(rig.hw(1))["status"] == "assign_failed"
            assert rig.fake.tag(rig.hw(1))["bridge_hw_id"] is None
            assert rig.sim.bridges[0].counters["assign_full"] == 1  # refused once, not retried
            assert rig.tag_id(1) not in rig.sim.bridges[0].assignments
            assert rig.sim.bridges[1].assignments[rig.tag_id(1)].epoch == 1  # still on its old bridge
            with rig.db() as db:
                tag = db.get_tag(rig.tag_id(1))
            assert (tag.epoch, tag.bridge_addr) == (1, rig.sim.bridges[1].addr)

    run_scenario(scenario(), timeout=120)


def test_identify_and_refresh(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            await rig.start()
            did = rig.fake.add_job("alice", rig.hw(), title="Regular screen")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="regular screen")
            identify = rig.fake.add_command("identify", {"hw_id": rig.hw()})
            done = await command_done(rig, identify)
            assert done["status"] == "succeeded", done
            identify_revisions = [r for r in revisions(rig) if r["purpose"] == "identify"]
            assert [r["state"] for r in identify_revisions] == ["displayed"]  # delivered once, not twice
            # after the hold the regular screen comes back (a new revision)
            await rig.wait(lambda: revisions(rig)[-1]["purpose"] == "screen"
                           and revisions(rig)[-1]["state"] == "displayed", 20, what="screen restored")
            assert len([r for r in revisions(rig) if r["purpose"] == "identify"]) == 1
            before = max(r["revision"] for r in revisions(rig))
            refresh = rig.fake.add_command("refresh_tag", {"tag_id": rig.hw()})
            done = await command_done(rig, refresh)
            assert done["status"] == "succeeded", done
            assert done["result"]["revision"] > before
            bridge_identify = rig.fake.add_command("identify", {"hw_id": rig.bridge_hw()})
            assert (await command_done(rig, bridge_identify))["status"] == "succeeded"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)


def test_mesh_commands_scan_provision_remove(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(unprovisioned=1) as rig:
            await rig.start()
            spare = rig.sim.bridges[-1].uuid.hex()
            scan = rig.fake.add_command("scan_unprovisioned", {"duration_s": 2})
            done = await command_done(rig, scan)
            assert done["status"] == "succeeded", done
            assert [b["uuid"] for b in done["result"]["beacons"]] == [spare]
            provision = rig.fake.add_command("provision_bridge", {"uuid": spare, "name": "hall"})
            done = await command_done(rig, provision, 60)
            assert done["status"] == "succeeded", done
            addr = done["result"]["addr"]
            with rig.db() as db:
                record = db.get_bridge(uuid=spare)
            assert record.addr == addr and record.configured and record.name == "hall"
            await rig.wait(lambda: any(b["hw_id"] == f"br-{spare}" for b in rig.fake.inventories[-1]["bridges"]),
                           what="inventory with the new bridge")
            remove = rig.fake.add_command("remove_bridge", {"hw_id": f"br-{spare}"})
            done = await command_done(rig, remove, 60)
            assert done["status"] == "succeeded", done
            with rig.db() as db:
                assert db.find_bridge(uuid=spare) is None
            diag = rig.fake.add_command("collect_diagnostics", {})
            done = await command_done(rig, diag)
            assert done["status"] == "succeeded"
            assert done["result"]["gateway"]["boot_id"] == rig.sim.gateway.boot_id
            assert "depth" in done["result"]["queue"]

    run_scenario(scenario(), timeout=140)


def test_fontpack_mismatch_then_install_fontpack(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(bridge_pack=FIXTURE_PACK.read_bytes()) as rig:
            await rig.start()
            refused = rig.fake.add_job("alice", rig.hw(), title="Needs the right pack")
            await rig.wait(lambda: rig.stage(refused) == "failed", what="font pack mismatch")
            assert rig.fake.delivery(refused)["status_code"] == int(Status.FONTPACK_MISMATCH)
            # without a maintenance port the command explains what the operator has to do
            install = rig.fake.add_command("install_fontpack", {"bridge_hw_id": rig.bridge_hw()})
            done = await command_done(rig, install)
            assert done["status"] == "failed" and "fonts-install" in done["error"]
            await rig.stop()
            rig.settings_overrides["bridge_maintenance"] = [f"{rig.bridge_hw()}={rig.sim.bridge_url(0)}"]
            await rig.start()
            install = rig.fake.add_command("install_fontpack", {"bridge_hw_id": rig.bridge_hw()})
            done = await command_done(rig, install, 90)
            assert done["status"] == "succeeded", done
            assert done["result"]["fontpack_id"] == rig.fonts.pack_id.hex()
            after = rig.fake.add_job("alice", rig.hw(), title="Drawn with the right pack")
            await rig.wait(lambda: rig.stage(after) == "displayed", what="delivery after the install")
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=140)


def test_revoked_hardware_credential_keeps_content_running(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            svc = await rig.start()
            await rig.wait(lambda: rig.fake.heartbeats, what="first heartbeat")
            rig.fake.revoke(rig.hardware_cred.id)
            await rig.wait(lambda: svc.hardware is not None and svc.hardware.state == "stopped", what="stopped")
            did = rig.fake.add_job("alice", rig.hw(), title="Content still flows")
            await rig.wait(lambda: rig.stage(did) == "displayed", what="displayed")
            assert Credential(rig.hardware_cred.id, "x").credential_id in svc.failed_credentials

    run_scenario(scenario(), timeout=100)


def test_clear_tag_waits_with_one_op_while_the_tag_is_away(make_rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review regression: a timed-out clear step re-sent TAG_COMMAND CLEAR under new op ids; the bridge queued
    every one and refreshed the panel once per copy."""
    from cremind_tag.daemon import commands as commands_module

    monkeypatch.setitem(commands_module.STEP_TIMEOUT_S, "clear", 0.4)

    async def scenario() -> None:
        async with make_rig(owner=None) as rig:
            tag = rig.sim_tag()
            tag.out_of_range = True
            await rig.start()
            assign, clear = rig.fake.claim_tag(rig.hw(), "alice", rig.bridge_hw())
            assert (await command_done(rig, assign))["status"] == "succeeded"
            await asyncio.sleep(3.0)  # several step timeouts while the tag is away
            queued = [j for j in rig.sim.bridge(0).jobs.get(rig.tag_id(), []) if j.kind == "cmd"]
            assert len(queued) == 1, queued
            refreshes = tag.stats["refreshes"]
            tag.out_of_range = False
            assert (await command_done(rig, clear))["status"] == "succeeded"
            await asyncio.sleep(1.0)
            assert tag.stats["refreshes"] == refreshes + 1
            assert rig.sim.gateway.counters["deliveries_accepted"] == 0

    run_scenario(scenario(), timeout=100)


def test_a_claim_whose_answer_was_lost_still_runs(make_rig: Any) -> None:
    """Review regression: Cremind committed the claim but its answer was lost; the command sat 'claiming'."""

    async def scenario() -> None:
        async with make_rig() as rig:
            rig.fake.claim_answer_lost = 1
            svc = await rig.start()
            command = rig.fake.add_command("collect_diagnostics", {})
            done = await command_done(rig, command, 20)
            assert done["status"] == "succeeded", done
            assert rig.fake.claim_answer_lost == 0  # the lost answer really happened
            assert svc.hardware is not None and svc.hardware.commands_claimed == 1  # claimed again: 409 = ours
            assert rig.runs == 1  # no restart needed

    run_scenario(scenario(), timeout=100)


def test_an_ownership_command_finishes_after_its_expiry(make_rig: Any) -> None:
    """Cremind accepts a late `succeeded`; a clear it re-queues meanwhile is recognised as already done."""

    async def scenario() -> None:
        async with make_rig() as rig:
            tag = rig.sim_tag()
            tag.out_of_range = True
            await rig.start()
            command = rig.fake.add_command("clear_tag", {"tag_id": rig.hw(), "epoch": 1}, ttl_s=1.0)
            await rig.wait(lambda: rig.fake.commands[command]["status"] == "claimed", what="claimed")
            await asyncio.sleep(2.0)  # past its expiry, the tag still away
            assert rig.fake.commands[command]["status"] == "claimed"
            tag.out_of_range = False
            done = await command_done(rig, command)
            assert done["status"] == "succeeded", done
            refreshes = tag.stats["refreshes"]
            again = rig.fake.add_command("clear_tag", {"tag_id": rig.hw(), "epoch": 1})  # Cremind's re-queue
            done = await command_done(rig, again)
            assert done["status"] == "succeeded" and done["result"]["already_cleared"] is True
            assert tag.stats["refreshes"] == refreshes

    run_scenario(scenario(), timeout=100)
