"""GatewayClient against the simulated gateway over TCP (socket:// through pyserial)."""

from __future__ import annotations

import asyncio

import pytest

from cremind_tag.gateway import (
    AssignResult,
    GatewayClient,
    GatewayEvent,
    GatewayTimeout,
    SessionStarted,
    StatusError,
    matches,
)
from cremind_tag.protocol.ids import PROTO_VERSION, NodeRole, SerialMsg, Status
from cremind_tag.sim import Simulator
from cremind_tag.sim.harness import make_config, run_scenario

pytestmark = pytest.mark.timeout(120)


def sim_config(**overrides: object) -> object:
    return make_config(fontpack=None, seed=31, **overrides)


async def assign_event(client: GatewayClient, sim: Simulator, tag_id: int, epoch: int) -> int:
    """Trigger one retained event (EVT_ASSIGN_RESULT); returns the op_id."""
    bridge = sim.bridge(0).addr
    assert bridge is not None
    ack = await client.assign_tag(bridge, tag_id, epoch, bytes(16))
    assert ack.status == Status.ACCEPTED
    assert ack.op_id is not None
    return ack.op_id


def test_hello_info_and_simple_requests() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                hello = client.hello_info
                assert hello is not None and hello.proto == PROTO_VERSION
                assert hello.boot_id == sim.gateway.boot_id == client.boot_id
                assert hello.caps.role == NodeRole.GATEWAY and hello.caps.credits == 4
                assert hello.caps.max_frame == 4096
                info = await client.info()
                assert info.boot_id == hello.boot_id and info.counters["hellos"] == 1
                assert await client.ping() >= 0
                nodes = await client.list_nodes()
                assert [n.addr for n in nodes] == [sim.bridge(0).addr] and nodes[0].configured
                assert nodes[0].uuid == sim.bridge(0).uuid
                counters = await client.get_counters()
                assert counters["nodes"] == 1
                inventory = await client.get_inventory()
                assert inventory[0].addr == sim.bridge(0).addr
                with pytest.raises(StatusError) as info:  # maintenance-port messages are not for the gateway
                    await client._checked(SerialMsg.FONT_STATUS)
                assert info.value.status == Status.UNSUPPORTED

    run_scenario(scenario())


def test_credits_hold_under_load() -> None:
    config = sim_config(processing_delay_s=0.003)

    async def scenario() -> None:
        async with Simulator(config) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                results = await asyncio.gather(*(client.ping() for _ in range(60)),
                                               *(client.info() for _ in range(20)))
                assert len(results) == 80
                counters = sim.gateway.endpoint.counters
                assert counters["credit_violations"] == 0
                assert counters["overruns"] == 0
                assert counters["hellos"] == 1  # no timeout, no resync
                assert client.link.credits >= 1

    run_scenario(scenario())


def test_lost_response_is_retried_with_the_same_op_id() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            tag_id = sim.config.tags[0].tag_id
            client = GatewayClient(sim.gateway_url, reconnect=False, request_timeout=0.3)
            async with client:
                sim.gateway.endpoint.drop_responses = 1  # the gateway does the work; its answer is lost
                bridge = sim.bridge(0).addr
                assert bridge is not None
                ack = await client.assign_tag(bridge, tag_id, 2, bytes(16))
                assert ack.status == Status.ACCEPTED and ack.duplicate
                assert sim.gateway.counters["duplicate_ops"] == 1
                assert client.link.stats["resyncs"] == 2  # connect + the resync after the timeout
                assert client.link.stats["timeouts"] == 1

    run_scenario(scenario())


def test_request_gives_up_after_all_attempts() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            client = GatewayClient(sim.gateway_url, reconnect=False, request_timeout=0.2, attempts=2)
            async with client:
                sim.gateway.endpoint.drop_responses = 5
                with pytest.raises(GatewayTimeout):
                    await client.ping()

    run_scenario(scenario())


def test_retained_event_is_acked_only_after_the_handler_succeeds() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            tag_id = sim.config.tags[0].tag_id
            gate = asyncio.Event()
            calls: list[GatewayEvent] = []

            async def handler(event: GatewayEvent) -> None:
                if isinstance(event, AssignResult):
                    calls.append(event)
                    if len(calls) == 1:
                        raise RuntimeError("database is locked")  # first attempt fails
                    await gate.wait()  # second attempt: "committing"

            client = GatewayClient(sim.gateway_url, reconnect=False, handler_backoff=(0.05, 0.05))
            client.add_event_handler(handler)
            async with client:
                op_id = await assign_event(client, sim, tag_id, 2)
                while len(calls) < 2:
                    await asyncio.sleep(0.01)
                assert all(e.op_id == op_id for e in calls)
                seq = calls[0].seq
                # Failed once, still running: the gateway keeps the event.
                assert sim.gateway.endpoint.retained_seqs == [seq]
                assert client.stats["handler_failures"] == 1 and client.stats["acks"] == 0
                gate.set()
                await client.drain_events()
                assert sim.gateway.endpoint.retained_seqs == []
                assert client.stats["acks"] == 1

    run_scenario(scenario())


def test_unacked_events_are_resent_after_reconnect() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            tag_id = sim.config.tags[0].tag_id
            # A read-only client (no handler): observes but never acknowledges.
            async with GatewayClient(sim.gateway_url, reconnect=False) as observer:
                with observer.expect(matches(AssignResult)) as waiter:
                    op_id = await assign_event(observer, sim, tag_id, 2)
                    first = await waiter.wait(10)
            assert sim.gateway.endpoint.retained_seqs == [first.seq]

            seen: list[GatewayEvent] = []

            async def handler(event: GatewayEvent) -> None:
                seen.append(event)

            client = GatewayClient(sim.gateway_url, reconnect=False)
            client.add_event_handler(handler)
            async with client:
                await asyncio.wait_for(_until(lambda: any(isinstance(e, AssignResult) for e in seen)), 10)
                await client.drain_events()
                assert isinstance(seen[0], SessionStarted) and not seen[0].boot_changed
                resent = next(e for e in seen if isinstance(e, AssignResult))
                assert (resent.op_id, resent.seq, resent.boot_id) == (op_id, first.seq, first.boot_id)
                assert sim.gateway.endpoint.retained_seqs == []

                # A re-sent copy of an already handled event is not handled again, only re-acknowledged.
                await client.hello()
                await client.drain_events()
                assert sum(isinstance(e, AssignResult) for e in seen) == 1

    run_scenario(scenario())


def test_boot_id_change_is_reported_in_order() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            sessions: list[SessionStarted] = []

            async def handler(event: GatewayEvent) -> None:
                if isinstance(event, SessionStarted):
                    sessions.append(event)

            client = GatewayClient(sim.gateway_url, reconnect=True, request_timeout=0.5)
            client.add_event_handler(handler)
            async with client:
                old_boot = client.boot_id
                ack = await client.reboot()
                assert ack.status == Status.OK
                await asyncio.wait_for(_until(lambda: len(sessions) >= 2), 15)
                assert sessions[1].boot_changed and sessions[1].previous_boot_id == old_boot
                assert client.boot_id == sim.gateway.boot_id != old_boot
                assert client.link.stats["reconnects"] == 1
                assert await client.ping() >= 0  # usable after the reconnect

    run_scenario(scenario())


def test_events_of_an_old_boot_are_not_acknowledged() -> None:
    async def scenario() -> None:
        async with Simulator(sim_config()) as sim:
            tag_id = sim.config.tags[0].tag_id
            gate = asyncio.Event()

            async def handler(event: GatewayEvent) -> None:
                if isinstance(event, AssignResult):
                    await gate.wait()

            client = GatewayClient(sim.gateway_url, reconnect=True, request_timeout=0.5)
            client.add_event_handler(handler)
            async with client:
                await assign_event(client, sim, tag_id, 2)
                await asyncio.wait_for(_until(lambda: sim.gateway.endpoint.last_seq == 1), 10)
                old_boot = client.boot_id
                await client.reboot()
                await asyncio.wait_for(_until(lambda: client.boot_id not in (None, old_boot)), 15)
                assert client.boot_id == sim.gateway.boot_id
                gate.set()
                await client.drain_events()
                assert client.stats["acks"] == 0  # seq 1 belongs to the previous boot

    run_scenario(scenario())


async def _until(predicate: object) -> None:
    while not predicate():  # type: ignore[operator]
        await asyncio.sleep(0.01)
