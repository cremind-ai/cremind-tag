"""What the tag said reaches the companion (docs/protocol.md §3.4, §5.4, §10): the CAPS bytes are bound into
the handshake, every ERROR carries the tag's stored epoch, unauthenticated statuses end jobs only after 3
consecutive sessions (flagged ``RESULT_FLAG_ESCALATED``), and a stored ACK is flagged ``RESULT_FLAG_DUPLICATE``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from cremind_tag.protocol.ids import GattChr, Status
from cremind_tag.protocol.msgs import TagCaps
from cremind_tag.sim import radio
from cremind_tag.sim.bridge import UNAUTH_REPEATS
from cremind_tag.sim.harness import SimHarness, run_scenario

pytestmark = pytest.mark.timeout(180)

Card = Callable[..., bytes]


def test_stale_epoch_escalates_after_three_sessions_with_the_stored_epoch(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=61) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            tag.nvs.stored_epoch = 5  # an assignment at epoch 5 the companion lost (the bridge holds epoch 1)
            result = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 120)
            assert result.status == Status.STALE_EPOCH and result.escalated and not result.duplicate
            assert result.stored_epoch == 5  # from the tag's ERROR: the companion must assign above it
            assert tag.stats["stale_epoch_refused"] == UNAUTH_REPEATS
            assert tag.stats["refreshes"] == 0

    run_scenario(scenario())


def test_refusals_short_of_three_are_link_failures(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=62) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            tag.faults.auth_fail = UNAUTH_REPEATS - 1  # two sessions refused, the third authenticates
            result = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 120)
            assert result.status == Status.OK and result.flags == 0
            assert result.stored_epoch == 1  # persisted before AUTH_OK (§5.4)
            assert tag.stats["auth_failures"] == UNAUTH_REPEATS - 1 and tag.stats["refreshes"] == 1
            bridge = h.sim.bridge_at(h.tag_bridge(tag_id))
            assert bridge.counters["unauth_statuses"] == UNAUTH_REPEATS - 1
            assert bridge.counters["unauth_final"] == 0
            assert bridge.assignments[tag_id].unauth_count == 0  # AUTH_OK restarted the count

    run_scenario(scenario())


def test_unauthenticated_statuses_count_per_tag_and_epoch(tiny_pack: bytes) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=63) as h:
            tag_id = h.tag_ids[0]
            bridge = h.sim.bridge_at(h.tag_bridge(tag_id))
            count = bridge._unauth_status
            assert not count(tag_id, 1, Status.AUTH_FAILED) and not count(tag_id, 1, Status.AUTH_FAILED)
            assert not count(tag_id, 1, Status.STALE_EPOCH)  # another status restarts the run
            assert not count(tag_id, 1, Status.STALE_EPOCH)
            assert count(tag_id, 1, Status.STALE_EPOCH)  # the third in a row is final
            assert not count(tag_id, 1, Status.STALE_EPOCH)  # ... and the count restarts
            assert not count(tag_id, 2, Status.STALE_EPOCH)  # not the assigned epoch: nothing to end
            assert not count(tag_id, 1, Status.STALE_EPOCH)
            bridge._tag_authenticated(tag_id, 1)
            assert not count(tag_id, 1, Status.STALE_EPOCH) and not count(tag_id, 1, Status.STALE_EPOCH)
            assert count(tag_id, 1, Status.STALE_EPOCH)

    run_scenario(scenario())


def test_a_relay_that_alters_caps_breaks_the_handshake(tiny_pack: bytes, card: Card,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.4: the bridge hashes the CAPS it read, the tag the CAPS it serves; a flipped plane-flag bit on the
    way makes mac_b fail at the tag, and nothing is ever rendered from the altered CAPS."""
    genuine = radio.GattLink.read

    async def relayed(self: radio.GattLink, chr: GattChr) -> bytes:
        value = await genuine(self, chr)
        if chr != GattChr.CAPS:
            return value
        caps = TagCaps.unpack(value)
        return replace(caps, plane_flags=caps.plane_flags ^ 1).pack()

    monkeypatch.setattr(radio.GattLink, "read", relayed)

    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=64) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            before = tag.displayed_digest
            result = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 120)
            assert result.status == Status.AUTH_FAILED and result.escalated
            assert tag.stats["auth_failures"] == UNAUTH_REPEATS and tag.stats["refreshes"] == 0
            assert tag.displayed_digest == before and tag.nvs.stored_epoch == 0

    run_scenario(scenario())


def test_a_stored_ack_is_flagged_duplicate(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, seed=65) as h:
            tag_id = h.tag_ids[0]
            tag = h.sim.tag(tag_id)
            bridge = h.sim.bridge_at(h.tag_bridge(tag_id))
            first = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 60)
            assert first.status == Status.OK and first.flags == 0 and first.stored_epoch == 1
            bridge.history.clear()  # a bridge without the history (e.g. a replacement): it sends the frame
            again = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 60)
            assert again.status == Status.OK and again.duplicate and not again.escalated
            assert again.stored_epoch == 1
            assert tag.stats["duplicates"] == 1 and tag.stats["refreshes"] == 1  # nothing was redrawn
            # A third copy is answered from the bridge's history, which kept the report (§3.4).
            third = await h.wait_result((await h.deliver(tag_id, card(0), revision=1)).update_id, 60)
            assert third.status == Status.OK and third.duplicate and third.stored_epoch == 1
            assert bridge.counters["duplicates"] == 1 and tag.stats["duplicates"] == 1

    run_scenario(scenario())
