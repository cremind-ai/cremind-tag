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


def test_stale_epoch_blocks_the_tag_until_reassigned(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(bridges=2) as rig:
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="Shown at epoch 1")
            await rig.wait(lambda: rig.stage(first) == "displayed", what="first displayed")
            # The tag authenticated epoch 3 elsewhere (e.g. a restored companion): it now refuses the
            # epoch-1 key this companion still uses.
            rig.sim_tag().nvs.stored_epoch = 3
            blocked = rig.fake.add_job("alice", rig.hw(), title="Refused")
            await rig.wait(lambda: rig.stage(blocked) == "failed", what="stale epoch failure")
            delivery = rig.fake.delivery(blocked)
            assert delivery["status_code"] == int(Status.STALE_EPOCH)
            view = rig.svc.store.get_view(rig.tag_id())
            assert view is not None and view.blocked_reason == "stale_epoch"
            # Cremind learns the epoch and re-assigns above it.
            rig.fake.tag(rig.hw())["epoch"] = 3
            command = rig.fake.assign(rig.hw(), rig.bridge_hw(0))
            assert (await command_done(rig, command))["status"] == "succeeded"
            after = rig.fake.add_job("alice", rig.hw(), title="Back at epoch 4")
            await rig.wait(lambda: rig.stage(after) == "displayed", what="delivery after reassignment")
            assert rig.fake.delivery(after)["epoch"] == 4
            rig.assert_consistent_receipts()

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
            assert any(r["purpose"] == "identify" and r["state"] == "displayed" for r in revisions(rig))
            # after the hold the regular screen comes back (a new revision)
            await rig.wait(lambda: revisions(rig)[-1]["purpose"] == "screen"
                           and revisions(rig)[-1]["state"] == "displayed", 20, what="screen restored")
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
