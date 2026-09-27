"""The bridge's tag scheduling (docs/protocol.md §5.2): two tag sessions at once on an nRF52840 bridge (a second
tag initiated while the first one refreshes), one on an nRF52832, and the quick retry of a failed connection
inside the tag's advertising window.

The tags' wake loops are stopped: each test opens the advertising windows itself, so which windows overlap is
decided here, not by the tags' random phases."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any

import pytest

from cremind_tag.protocol.ids import BRIDGE_TAG_BACKOFF_MS, Board, DeliveryStage, Status
from cremind_tag.sim.bridge import NRF52832_SESSIONS, NRF52840_SESSIONS, SimBridge
from cremind_tag.sim.core import SimClock
from cremind_tag.sim.harness import SimHarness, make_config, run_scenario
from cremind_tag.sim.radio import ConnectFailed, GattLink

pytestmark = pytest.mark.timeout(120)

Card = Callable[..., bytes]


async def until(h: SimHarness, predicate: Callable[[], bool], timeout_ms: float = 60000.0) -> None:
    deadline = h.sim.clock.now_ms() + timeout_ms
    while not predicate():
        assert h.sim.clock.now_ms() < deadline, "condition not reached"
        await h.sim.clock.sleep_ms(20.0)


async def quiet_tags(h: SimHarness) -> None:
    """No wake loops: the test opens each advertising window (SimTag._advertise_window)."""
    for tag_id in h.tag_ids:
        await h.sim.tag(tag_id)._tasks.cancel_all()


def window(h: SimHarness, tag_id: int) -> asyncio.Task[None]:
    return asyncio.create_task(h.sim.tag(tag_id)._advertise_window())


def transferring(h: SimHarness, update_id: int) -> bool:
    return any(s.update_id == update_id and s.stage == DeliveryStage.TRANSFERRING for s in h.stages)


def test_board_defaults() -> None:
    assert (NRF52840_SESSIONS, NRF52832_SESSIONS) == (2, 1)
    kwargs: dict[str, Any] = {"uuid": bytes(16), "clock": SimClock(1.0), "mesh": None, "air": None,
                              "rng": random.Random(1)}
    assert SimBridge("a", board=Board.NRF52840_BRIDGE, **kwargs).max_sessions == 2
    assert SimBridge("b", board=Board.NRF52832_BRIDGE, **kwargs).max_sessions == 1
    assert SimBridge("c", **kwargs).quick_retry


def test_a_second_tag_is_served_while_the_first_refreshes(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, tags=2, bridges=1, seed=31, time_scale=50) as h:
            a, b = h.tag_ids
            bridge = h.sim.bridge(0)
            await quiet_tags(h)
            ack_a = await h.deliver(a, card(0), revision=1)
            ack_b = await h.deliver(b, card(1), revision=1)
            await until(h, lambda: bool(bridge.jobs.get(a)) and bool(bridge.jobs.get(b)))
            win_a = window(h, a)
            # A's frame is out and its tag refreshes: the link idles.
            await until(h, lambda: a in bridge._sessions and bridge._sessions[a].link_idle)
            win_b = window(h, b)
            await until(h, lambda: transferring(h, ack_b.update_id))
            assert ack_a.update_id not in h.results, "B started while A was still refreshing"
            assert bridge.counters["concurrent_sessions"] == 1
            result_a, result_b = await h.wait_result(ack_a.update_id), await h.wait_result(ack_b.update_id)
            await asyncio.gather(win_a, win_b)
            assert result_a.status == result_b.status == Status.OK
            # One initiation per tag, each in its own mesh suspend window.
            assert bridge.counters["suspend_count"] == 2 and bridge.counters["max_links"] == 2
            assert not bridge._links and bridge._initiating is None

    run_scenario(scenario())


def test_one_session_bridge_misses_the_overlapping_window(tiny_pack: bytes, card: Card) -> None:
    """The nRF52832 (one session): B's window during A's refresh is lost; B is served at its next window."""
    async def scenario() -> None:
        config = make_config(fontpack=tiny_pack, tags=2, bridges=1, seed=31, time_scale=50)
        config.bridges[0].sessions = 1
        async with SimHarness(config) as h:
            a, b = h.tag_ids
            bridge = h.sim.bridge(0)
            assert bridge.max_sessions == 1
            await quiet_tags(h)
            ack_a = await h.deliver(a, card(0), revision=1)
            ack_b = await h.deliver(b, card(1), revision=1)
            await until(h, lambda: bool(bridge.jobs.get(a)) and bool(bridge.jobs.get(b)))
            win_a = window(h, a)
            await until(h, lambda: a in bridge._sessions and bridge._sessions[a].link_idle)
            await window(h, b)  # the whole window passes during A's refresh
            assert not transferring(h, ack_b.update_id)
            await win_a
            assert (await h.wait_result(ack_a.update_id)).status == Status.OK
            await window(h, b)
            assert (await h.wait_result(ack_b.update_id)).status == Status.OK
            assert bridge.counters["concurrent_sessions"] == 0 and bridge.counters["max_links"] == 1

    run_scenario(scenario())


def _flaky_connect(h: SimHarness, failures: int) -> list[float]:
    """The first ``failures`` connection attempts fail 100 ms in (the tag advertises meanwhile)."""
    air, clock = h.sim.air, h.sim.clock
    real = air.connect
    attempts: list[float] = []

    async def connect(central: str, tag_id: int, timeout_ms: float) -> GattLink:
        attempts.append(clock.now_ms())
        if len(attempts) <= failures:
            await clock.sleep_ms(100.0)
            raise ConnectFailed("injected")
        return await real(central, tag_id, timeout_ms)

    air.connect = connect  # type: ignore[method-assign]
    return attempts


@pytest.mark.parametrize("quick_retry", [True, False])
def test_a_failed_connection_is_retried_inside_the_window(tiny_pack: bytes, card: Card, quick_retry: bool) -> None:
    async def scenario() -> None:
        config = make_config(fontpack=tiny_pack, tags=1, bridges=1, seed=33, time_scale=50)
        config.bridges[0].quick_retry = quick_retry
        async with SimHarness(config) as h:
            (tag_id,) = h.tag_ids
            bridge = h.sim.bridge(0)
            await quiet_tags(h)
            ack = await h.deliver(tag_id, card(0), revision=1)
            await until(h, lambda: bool(bridge.jobs.get(tag_id)))
            attempts = _flaky_connect(h, 1)
            await window(h, tag_id)
            assert bridge.counters["connect_failed"] == 1
            if quick_retry:
                # One retry on the next advertisement of the same window: served, one suspension more.
                assert len(attempts) == 2 and attempts[1] - attempts[0] < 2000.0
                assert (bridge.counters["quick_retries"], bridge.counters["quick_retries_connected"]) == (1, 1)
                assert transferring(h, ack.update_id)
            else:
                # The back-off forfeits the rest of the window.
                assert len(attempts) == 1 and bridge.counters["backoff_skips"] > 0
                assert not transferring(h, ack.update_id)
            assert bridge.counters["suspend_count"] == len(attempts)
            await h.sim.clock.sleep_ms(BRIDGE_TAG_BACKOFF_MS)
            await window(h, tag_id)  # the next wake: the back-off is over
            assert (await h.wait_result(ack.update_id)).status == Status.OK

    run_scenario(scenario())


def test_only_one_quick_retry_per_failure(tiny_pack: bytes, card: Card) -> None:
    async def scenario() -> None:
        async with SimHarness(fontpack=tiny_pack, tags=1, bridges=1, seed=34, time_scale=50) as h:
            (tag_id,) = h.tag_ids
            bridge = h.sim.bridge(0)
            await quiet_tags(h)
            ack = await h.deliver(tag_id, card(0), revision=1)
            await until(h, lambda: bool(bridge.jobs.get(tag_id)))
            attempts = _flaky_connect(h, 2)
            await window(h, tag_id)
            # The failure, its one retry (failed too), then the back-off holds for the rest of the window.
            assert len(attempts) == 2 and bridge.counters["quick_retries"] == 1
            assert bridge.counters["quick_retries_failed"] == 1 and bridge.counters["backoff_skips"] > 0
            assert not transferring(h, ack.update_id)
            await h.sim.clock.sleep_ms(BRIDGE_TAG_BACKOFF_MS)
            await window(h, tag_id)
            assert (await h.wait_result(ack.update_id)).status == Status.OK
            assert len(attempts) == 3

    run_scenario(scenario())
