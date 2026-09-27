"""docs/protocol.md §10 delivery rules added after review: a repeated COMMIT answers DUPLICATE, and the
gateway emits exactly one EVT_RESULT per update_id."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cremind_tag.gateway import ResultEvent
from cremind_tag.protocol.ids import Status
from cremind_tag.sim import FaultSpecError, SimFaults, parse_fault
from cremind_tag.sim.harness import SimHarness, make_config, run_scenario

pytestmark = pytest.mark.timeout(120)

Card = Callable[..., bytes]


def results_for(h: SimHarness, update_id: int) -> list[ResultEvent]:
    return [e for e in h.events if isinstance(e, ResultEvent) and e.update_id == update_id]


def test_a_lost_layout_status_repeats_the_commit_and_gets_duplicate(tiny_pack: bytes, card: Card) -> None:
    config = make_config(fontpack=tiny_pack, seed=31)
    config.faults.mesh.drop_status = 1  # the bridge's LAYOUT_STATUS OK is lost once

    async def scenario() -> None:
        async with SimHarness(config) as h:
            tag_id = h.tag_ids[0]
            ack = await h.deliver(tag_id, card(0), revision=1)
            result = await h.wait_result(ack.update_id)
            assert result.status == Status.OK  # never NOT_FOUND
            assert h.sim.bridge(0).counters["commit_repeats"] == 1
            assert h.sim.gateway.counters["commit_resends"] == 1
            assert h.sim.mesh.counters["lost_MeshLayoutStatus"] == 1
            await h.client.drain_events()
            assert len(results_for(h, ack.update_id)) == 1
            assert h.sim.tag(tag_id).stats["refreshes"] == 1

    run_scenario(scenario())


def test_one_result_per_update_id(tiny_pack: bytes, card: Card) -> None:
    config = make_config(fontpack=tiny_pack, seed=32)
    config.faults.mesh.drop_status = 4  # the first COMMIT and all three re-sends: the gateway gives up (TIMEOUT)

    async def scenario() -> None:
        async with SimHarness(config) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            tag.out_of_range = True  # the bridge holds the (accepted) layout until the gateway gave up
            ack = await h.deliver(tag_id, card(1), revision=1)
            assert (await h.wait_result(ack.update_id)).status == Status.TIMEOUT
            tag.out_of_range = False
            await h.wait_until(lambda: h.sim.gateway.counters["duplicate_update_results"] == 1, 60)
            await h.client.drain_events()
            # The bridge's OK for the same update_id was acknowledged to it and dropped by the gateway.
            assert [r.status for r in results_for(h, ack.update_id)] == [Status.TIMEOUT]
            assert tag.stats["refreshes"] == 1
            # The companion retries (TIMEOUT is a link failure) under a new update_id: the bridge answers
            # from its history (§3.3 DUPLICATE) and nothing is refreshed again.
            again = await h.deliver(tag_id, card(1), revision=1)
            assert (await h.wait_result(again.update_id)).status == Status.OK
            assert tag.stats["refreshes"] == 1

    run_scenario(scenario())


def test_a_rebooted_gateway_forgets_which_results_it_reported(tiny_pack: bytes) -> None:
    """Results de-duplication is RAM (docs/gateway-firmware.md §6; the simulator reboots with empty RAM state):
    a bridge's result re-sent after a gateway reboot (its RESULT_ACK was lost) is reported in the new boot."""
    from cremind_tag.protocol.msgs import MeshDeliveryResult

    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=33) as h:
            gateway = h.sim.gateway
            tag_id = h.tag_ids[0]
            result = MeshDeliveryResult(7, 900, tag_id, 1, 1, Status.OK, bytes(8), 3000, 0, 0, 0, 0)
            gateway._on_result(h.tag_bridge(tag_id), result)
            gateway._on_result(h.tag_bridge(tag_id), result)  # re-sent in the same boot: de-duplicated
            assert (gateway.counters["results"], gateway.counters["duplicate_results"]) == (1, 1)
            await gateway.reboot()
            gateway._on_result(h.tag_bridge(tag_id), result)
            assert gateway.counters["results"] == 2

    run_scenario(scenario())


def test_status_loss_fault_spec() -> None:
    faults = SimFaults()
    parse_fault("status-loss=2", faults)
    assert faults.mesh.drop_status == 2
    with pytest.raises(FaultSpecError):
        parse_fault("status-loss=x", faults)
