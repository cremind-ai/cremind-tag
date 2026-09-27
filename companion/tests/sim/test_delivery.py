"""End-to-end deliveries through the simulator: GatewayClient -> gateway -> mesh -> bridge -> tag."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from cremind_tag.fontpack.format import FontPack
from cremind_tag.gateway import ResultEvent, StageEvent
from cremind_tag.protocol.ids import DeliveryStage, Status, TagCommand
from cremind_tag.protocol.tag_txn import StoredState
from cremind_tag.render.reference import Panel, render_frame
from cremind_tag.sim import Assign, BridgeSpec
from cremind_tag.sim.harness import SimHarness, make_config, run_scenario

pytestmark = pytest.mark.timeout(120)

Card = Callable[..., bytes]


def expected_digest(layout: bytes, pack: bytes) -> bytes:
    return render_frame(layout, Panel(400, 300, 1, 0x01), FontPack(pack)).digest


def test_delivery_displays_the_reference_frame(tiny_pack: bytes, card: Card) -> None:
    layout = card(0)

    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=11) as h:
            tag_id = h.tag_ids[0]
            ack = await h.deliver(tag_id, layout, revision=1)
            assert ack.status == Status.ACCEPTED and not ack.duplicate
            result = await h.wait_result(ack.update_id)
            digest = expected_digest(layout, tiny_pack)
            assert result.status == Status.OK
            assert (result.tag_id, result.epoch, result.revision) == (tag_id, 1, 1)
            assert result.digest == digest[:8]
            assert result.retained and result.seq == 1 and result.boot_id == h.sim.gateway.boot_id
            assert result.timing.refresh_ms > 0 and result.timing.mesh_ms > 0
            tag = h.sim.tag(tag_id)
            assert tag.displayed_digest == digest
            assert tag.nvs.record is not None and tag.nvs.record.state == StoredState.DISPLAYED
            stages = [s.stage for s in h.stages if s.update_id == ack.update_id]
            assert stages[:1] == [DeliveryStage.BRIDGE_RECEIVED]
            assert DeliveryStage.TRANSFERRING in stages and DeliveryStage.REFRESHING in stages
            await h.client.drain_events()  # the handler succeeded -> EVENT_ACK released the event
            assert h.sim.gateway.endpoint.retained_seqs == []

    run_scenario(scenario())


def test_fixture_pack_matches_the_render_fixture(fixture_pack: bytes, render_scenarios: list[dict[str, Any]]) -> None:
    scenario_fx = render_scenarios[0]  # 400x300, rotation 0, 1 plane, plane_flags 1
    layout = bytes.fromhex(scenario_fx["layout_hex"])

    async def scenario() -> None:
        async with SimHarness(fontpack=fixture_pack, seed=3) as h:
            ack = await h.deliver(h.tag_ids[0], layout, revision=1)
            result = await h.wait_result(ack.update_id)
            assert result.status == Status.OK
            assert result.digest.hex() == scenario_fx["frame_digest"][:16]

    run_scenario(scenario())


def test_duplicate_delivery_returns_the_stored_ack(tiny_pack: bytes, card: Card) -> None:
    layout = card(1)

    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=12) as h:
            tag_id = h.tag_ids[0]
            first = await h.wait_result((await h.deliver(tag_id, layout, revision=1)).update_id)
            assert first.status == Status.OK
            tag, bridge = h.sim.tag(tag_id), h.sim.bridge(0)

            # Bridge level (§3.3): same revision + digest -> DUPLICATE, stored result for the new update_id.
            again = await h.deliver(tag_id, layout, revision=1)
            second = await h.wait_result(again.update_id)
            assert (second.status, second.digest) == (Status.OK, first.digest)
            assert bridge.counters["duplicates"] == 1 and tag.stats["refreshes"] == 1

            # Tag level (§5.6): a bridge without the history reaches the tag, which re-sends its stored ACK.
            bridge.history.clear()
            third = await h.wait_result((await h.deliver(tag_id, layout, revision=1)).update_id)
            assert (third.status, third.digest) == (Status.OK, first.digest)
            assert tag.stats["duplicates"] == 1 and tag.stats["refreshes"] == 1

    run_scenario(scenario())


def test_idempotent_op_id_does_no_new_work(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=13) as h:
            tag_id = h.tag_ids[0]
            op_id = h.client.new_op_id()
            first = await h.deliver(tag_id, card(2), revision=1, op_id=op_id, update_id=77)
            repeat = await h.deliver(tag_id, card(2), revision=1, op_id=op_id, update_id=77)
            assert first.status == Status.ACCEPTED and not first.duplicate
            assert repeat.status == Status.ACCEPTED and repeat.duplicate and repeat.detail == Status.DUPLICATE
            assert h.sim.gateway.counters["deliveries_accepted"] == 1
            assert (await h.wait_result(77)).status == Status.OK

    run_scenario(scenario())


def test_stale_revision_is_refused_by_the_bridge(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=14) as h:
            tag_id = h.tag_ids[0]
            assert (await h.wait_result((await h.deliver(tag_id, card(3), revision=2)).update_id)).status == Status.OK
            stale = await h.wait_result((await h.deliver(tag_id, card(4), revision=1)).update_id)
            assert stale.status == Status.STALE_REVISION
            assert h.sim.tag(tag_id).stats["refreshes"] == 1

    run_scenario(scenario())


def test_revision_conflict_is_refused_by_the_tag(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=15) as h:
            tag_id = h.tag_ids[0]
            assert (await h.wait_result((await h.deliver(tag_id, card(5), revision=2)).update_id)).status == Status.OK
            h.sim.bridge(0).history.clear()  # e.g. a replaced bridge: only the tag can tell
            conflict = await h.wait_result((await h.deliver(tag_id, card(6), revision=2)).update_id)
            assert conflict.status == Status.REVISION_CONFLICT
            assert h.sim.tag(tag_id).displayed_digest == expected_digest(card(5), tiny_pack)

    run_scenario(scenario())


def test_epoch_change_refuses_the_old_epoch(tiny_pack: bytes, card: Card) -> None:
    config = make_config(fontpack=tiny_pack, seed=16, bridges=2)

    async def scenario() -> None:
        async with SimHarness(config) as h:
            tag_id = h.tag_ids[0]
            old_bridge = h.tag_bridge(tag_id)
            new_bridge = next(b.addr for b in h.sim.bridges if b.addr != old_bridge)
            assert (await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id)).status == Status.OK

            ack = await h.client.assign_tag(new_bridge, tag_id, 2, h.k_epoch(tag_id, 2))
            assert ack.status == Status.ACCEPTED
            assigned = await h.wait_assign(ack.op_id)
            assert (assigned.status, assigned.bridge, assigned.epoch) == (Status.OK, new_bridge, 2)

            ok = await h.wait_result((await h.deliver(tag_id, card(1), revision=2, epoch=2,
                                                      bridge=new_bridge)).update_id)
            assert ok.status == Status.OK and h.sim.tag(tag_id).nvs.stored_epoch == 2

            # The old bridge still holds K_epoch for epoch 1: the tag refuses it now.
            refused = await h.wait_result((await h.deliver(tag_id, card(2), revision=3, epoch=1,
                                                           bridge=old_bridge)).update_id)
            assert refused.status == Status.STALE_EPOCH
            # The new bridge refuses an older epoch itself (§3.3).
            stale = await h.wait_result((await h.deliver(tag_id, card(3), revision=4, epoch=1,
                                                         bridge=new_bridge)).update_id)
            assert stale.status == Status.STALE_EPOCH
            assert h.sim.tag(tag_id).displayed_digest == expected_digest(card(1), tiny_pack)

    run_scenario(scenario())


def test_power_loss_reports_unknown_then_recovers(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=17) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            tag.faults.power_loss = 1  # between REFRESH_INTENT and DISPLAYED
            layout = card(7)
            unknown = await h.wait_result((await h.deliver(tag_id, layout, revision=1)).update_id)
            assert unknown.status == Status.DISPLAY_STATE_UNKNOWN
            assert tag.stats["power_losses"] == 1
            assert tag.nvs.record is not None
            assert (tag.nvs.record.status, tag.nvs.record.state) == (Status.DISPLAY_STATE_UNKNOWN,
                                                                     StoredState.REFRESH_INTENT)
            # The companion re-delivers the same revision; the tag repeats the refresh (§5.6 table).
            recovered = await h.wait_result((await h.deliver(tag_id, layout, revision=1)).update_id)
            assert recovered.status == Status.OK
            assert (tag.nvs.record.status, tag.nvs.record.state) == (Status.OK, StoredState.DISPLAYED)
            assert tag.displayed_digest == expected_digest(layout, tiny_pack)

    run_scenario(scenario())


def test_lost_chunks_are_resent(tiny_pack: bytes, long_card: bytes) -> None:
    config = make_config(fontpack=tiny_pack, seed=18)
    config.faults.mesh.drop_chunks = {1, 3}
    assert len(long_card) > 4 * 150

    async def scenario() -> None:
        async with SimHarness(config) as h:
            result = await h.wait_result((await h.deliver(h.tag_ids[0], long_card, revision=1)).update_id)
            assert result.status == Status.OK
            assert h.sim.bridge(0).counters["incomplete"] == 1
            assert h.sim.gateway.counters["chunks_resent"] == 2
            assert h.sim.mesh.counters["lost_MeshLayoutChunk"] == 2

    run_scenario(scenario())


def test_busy_backpressure_and_retry_with_the_same_op_id(tiny_pack: bytes, card: Card) -> None:
    config = make_config(fontpack=tiny_pack, seed=19, tags=3, delivery_queue=2)

    async def scenario() -> None:
        async with SimHarness(config) as h:
            h.sim.gateway.pause_deliveries()
            tags = h.tag_ids
            first = await h.deliver(tags[0], card(0), revision=1)
            second = await h.deliver(tags[1], card(1), revision=1)
            op_id = h.client.new_op_id()
            busy = await h.deliver(tags[2], card(2), revision=1, op_id=op_id, update_id=900)
            assert first.status == second.status == Status.ACCEPTED
            assert busy.busy and not busy.ok
            h.sim.gateway.resume_deliveries()
            await h.wait_until(lambda: h.sim.gateway.queue_depth == 0)
            retry = await h.deliver(tags[2], card(2), revision=1, op_id=op_id, update_id=900)
            assert retry.status == Status.ACCEPTED and not retry.duplicate  # BUSY is not remembered
            for update_id in (first.update_id, second.update_id, 900):
                assert (await h.wait_result(update_id)).status == Status.OK

    run_scenario(scenario())


def test_newer_revision_supersedes_a_pending_one(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=20) as h:
            tag_id = h.tag_ids[0]
            h.sim.tag(tag_id).out_of_range = True
            older = await h.deliver(tag_id, card(0), revision=1)
            await h.wait_until(lambda: any(s.update_id == older.update_id and s.stage == DeliveryStage.BRIDGE_RECEIVED
                                           for s in h.stages))
            newer = await h.deliver(tag_id, card(1), revision=2)
            assert (await h.wait_result(older.update_id)).status == Status.SUPERSEDED
            h.sim.tag(tag_id).out_of_range = False
            assert (await h.wait_result(newer.update_id)).status == Status.OK

    run_scenario(scenario())


def test_cancel_a_delivery_waiting_at_the_bridge(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=21) as h:
            tag_id = h.tag_ids[0]
            h.sim.tag(tag_id).out_of_range = True
            ack = await h.deliver(tag_id, card(0), revision=1)
            await h.wait_until(lambda: any(isinstance(s, StageEvent) and s.update_id == ack.update_id
                                           for s in h.stages))
            cancel = await h.client.cancel_delivery(ack.update_id)
            assert cancel.ok
            assert (await h.wait_result(ack.update_id)).status == Status.CANCELLED
            assert (await h.client.cancel_delivery(123456)).status == Status.NOT_FOUND

    run_scenario(scenario())


def test_tag_command_clear(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=22) as h:
            tag_id = h.tag_ids[0]
            assert (await h.wait_result((await h.deliver(tag_id, card(0), revision=5)).update_id)).status == Status.OK
            ack = await h.client.tag_command(bridge=h.tag_bridge(tag_id), tag_id=tag_id, epoch=1, cmd=TagCommand.CLEAR)
            result = await h.wait_result(ack.op_id)  # EVT_RESULT.update_id = op_id
            tag = h.sim.tag(tag_id)
            assert result.status == Status.OK and result.revision == 0
            assert tag.panel_planes == tag.spec.white_planes()
            assert tag.nvs.record is not None and tag.nvs.record.revision == 0
            # After CLEAR the bridge must not answer the old revision from its history: it is re-shown.
            again = await h.wait_result((await h.deliver(tag_id, card(0), revision=5)).update_id)
            assert again.status == Status.OK and tag.stats["refreshes"] == 3
            assert tag.displayed_digest == expected_digest(card(0), tiny_pack)

    run_scenario(scenario())


def test_wrong_key_fails_authentication(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=23) as h:
            tag_id = h.tag_ids[0]
            bridge = h.tag_bridge(tag_id)
            ack = await h.client.assign_tag(bridge, tag_id, 2, bytes(16))  # not K_epoch of this tag
            assert (await h.wait_assign(ack.op_id)).status == Status.OK
            result = await h.wait_result((await h.deliver(tag_id, card(0), revision=1, epoch=2)).update_id)
            assert result.status == Status.AUTH_FAILED
            assert h.sim.tag(tag_id).stats["auth_failures"] == 1
            assert h.sim.tag(tag_id).nvs.stored_epoch == 0  # never persisted without a verified AUTH

    run_scenario(scenario())


def test_disconnect_mid_transfer_restarts_from_offset_zero(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=24) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            tag.faults.disconnect_after_records = 20
            result = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id)
            assert result.status == Status.OK
            assert tag.stats["fault_disconnects"] == 1 and tag.stats["refreshes"] == 1
            assert h.sim.bridge(0).counters["sessions_fail"] >= 1

    run_scenario(scenario())


def test_suspend_failures_are_retried(tiny_pack: bytes, card: Card) -> None:
    config = make_config(fontpack=tiny_pack, seed=25)
    config.faults.bridge.suspend_fail_next = 2

    async def scenario() -> None:
        async with SimHarness(config) as h:
            result = await h.wait_result((await h.deliver(h.tag_ids[0], card(0), revision=1)).update_id)
            assert result.status == Status.OK
            assert h.sim.bridge(0).counters["suspend_fail"] == 2
            assert h.sim.bridge(0).last_status == Status.OK

    run_scenario(scenario())


def test_font_pack_mismatch(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=26) as h:
            tag_id = h.tag_ids[0]
            ack = await h.client.deliver_layout(bridge=h.tag_bridge(tag_id), tag_id=tag_id, epoch=1, revision=1,
                                                update_id=5, fontpack_id=b"\x01" * 8, layout=card(0))
            assert ack.status == Status.ACCEPTED
            result: ResultEvent = await h.wait_result(5)
            assert result.status == Status.FONTPACK_MISMATCH

    run_scenario(scenario())


def test_two_plane_tag(tiny_pack: bytes, card: Card) -> None:
    from cremind_tag.protocol.ids import Panel as PanelId

    config = make_config(fontpack=tiny_pack, seed=27, panel=PanelId.UC8176_420_BWR)
    config.assignments = [Assign(config.tags[0].tag_id, 0, 1)]
    config.bridges = [BridgeSpec()]

    async def scenario() -> None:
        async with SimHarness(config) as h:
            layout = card(0)
            result = await h.wait_result((await h.deliver(h.tag_ids[0], layout, revision=1)).update_id)
            frame = render_frame(layout, Panel(400, 300, 2, 0x03), FontPack(tiny_pack))
            assert result.status == Status.OK and result.digest == frame.digest[:8]
            assert h.sim.tag(h.tag_ids[0]).panel_planes == frame.planes

    run_scenario(scenario())
