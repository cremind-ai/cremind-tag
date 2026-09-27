"""Crash injection at every durability boundary, with a restart each time.

For each boundary the daemon is killed (no graceful flush, like a killed
process) the first time it reaches it, then a new daemon starts on the same
database against the same simulator and fake Cremind. Afterwards every job
Cremind issued and the companion accepted must be displayed on the tag, and no
delivery may have been reported with two different terminal outcomes.
"""

from __future__ import annotations

from typing import Any

import pytest

from cremind_tag.daemon.crash import BOUNDARIES
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(150)

DELIVERY_BOUNDARIES = [b for b in BOUNDARIES if b != "command_claimed"]


@pytest.mark.parametrize("boundary", DELIVERY_BOUNDARIES)
def test_crash_at_boundary_loses_nothing(make_rig: Any, boundary: str) -> None:
    async def scenario() -> None:
        async with make_rig() as rig:
            rig.crash.arm(boundary)
            await rig.start()
            first = rig.fake.add_job("alice", rig.hw(), title="First update")
            second = rig.fake.add_job("alice", rig.hw(), title="Second update", kind="needs_input")
            assert await rig.wait_crash() == boundary
            assert rig.crash.fired == [boundary]
            await rig.start()
            await rig.wait(lambda: all(rig.stage(d) == "displayed" for d in (first, second)), 30,
                           what="both deliveries displayed after the restart")
            third = rig.fake.add_job("alice", rig.hw(), title="Third update")
            await rig.wait(lambda: rig.stage(third) == "displayed", 30, what="the next delivery")
            for did in (first, second, third):
                assert did in rig.fake.accepted_log
                assert rig.job_state(did) == ("active", "displayed")
            assert rig.fake.terminal_outcomes() == {first: {"displayed"}, second: {"displayed"},
                                                    third: {"displayed"}}
            with rig.db() as db, db.reading() as conn:
                assert conn.execute("SELECT COUNT(*) FROM outbox WHERE dead = 1").fetchone()[0] == 0
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=120)


def test_crash_after_a_command_claim_resumes_it(make_rig: Any) -> None:
    async def scenario() -> None:
        async with make_rig(bridges=2) as rig:
            rig.crash.arm("command_claimed")
            await rig.start()
            command = rig.fake.assign(rig.hw(), rig.bridge_hw(1))
            assert await rig.wait_crash() == "command_claimed"
            assert rig.fake.commands[command]["status"] == "claimed"
            await rig.start()
            await rig.wait(lambda: rig.fake.commands[command]["status"] == "succeeded", 30, what="assign done")
            result = rig.fake.commands[command]["result"]
            assert result["epoch"] == 2 and result["bridge_hw_id"] == rig.bridge_hw(1)
            did = rig.fake.add_job("alice", rig.hw(), title="Via the new bridge")
            await rig.wait(lambda: rig.stage(did) == "displayed", 30, what="delivery after the move")
            assert rig.fake.delivery(did)["epoch"] == 2

    run_scenario(scenario(), timeout=120)


def test_repeated_crashes_at_every_boundary(make_rig: Any) -> None:
    """One run that crashes at each delivery boundary in turn, with jobs arriving in between."""

    async def scenario() -> None:
        async with make_rig() as rig:
            ids: list[int] = []
            for index, boundary in enumerate(DELIVERY_BOUNDARIES):
                rig.crash.arm(boundary)
                await rig.start()
                ids.append(rig.fake.add_job("alice", rig.hw(), title=f"Update {index}"))
                assert await rig.wait_crash() == boundary
            await rig.start()
            await rig.wait(lambda: all(rig.stage(d) in ("displayed",) for d in ids[-4:]), 45,
                           what="the newest deliveries displayed")
            # older ones were displayed too, or are still on the tag's card set (counted in its footer)
            for did in ids:
                assert rig.stage(did) in ("displayed", "companion_accepted", "gateway_received"), (
                    did, rig.fake.delivery(did))
                assert rig.job_state(did)[0] == "active"
            rig.assert_consistent_receipts()

    run_scenario(scenario(), timeout=140)
