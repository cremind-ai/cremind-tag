"""Provisioning through the simulated gateway: scan, provision, configure, list, identify, remove."""

from __future__ import annotations

from pathlib import Path

import pytest

from cremind_tag.gateway import (
    GatewayClient,
    GatewayEvent,
    NodeConfigured,
    NodeRemoved,
    Provisioned,
    UnprovBeacon,
    matches,
)
from cremind_tag.protocol.ids import MAX_BRIDGES, Status
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(120)


def config(state_file: Path | None = None) -> SimConfig:
    return SimConfig(seed=51, time_scale=200, state_file=state_file,
                     bridges=[BridgeSpec(), BridgeSpec(provisioned=False, name="new")])


def test_scan_provision_configure_remove(tmp_path: Path) -> None:
    state = tmp_path / "sim.json"

    async def scenario() -> None:
        async with Simulator(config(state)) as sim:
            fresh = sim.bridge(1)
            events: list[GatewayEvent] = []

            async def handler(event: GatewayEvent) -> None:
                events.append(event)

            client = GatewayClient(sim.gateway_url, reconnect=False)
            client.add_event_handler(handler)
            async with client:
                with client.expect(matches(UnprovBeacon, uuid=fresh.uuid)) as beacon:
                    assert (await client.scan_unprov(5)).status == Status.OK
                    seen = await beacon.wait(10)
                assert -100 < seen.rssi < 0

                op = client.new_op_id()
                with client.expect(matches(Provisioned, op_id=op)) as provisioned:
                    ack = await client.provision(fresh.uuid, "hall", op_id=op)
                    assert ack.status == Status.ACCEPTED
                    repeat = await client.provision(fresh.uuid, "hall", op_id=op)
                    assert repeat.duplicate  # same op_id: remembered, no second provisioning
                    event = await provisioned.wait(10)
                assert event.status == Status.OK and event.uuid == fresh.uuid and event.elements == 1
                addr = event.addr
                assert fresh.addr == addr and not fresh.configured

                refused = await client.deliver_layout(bridge=addr, tag_id=1, epoch=1, revision=1, update_id=1,
                                                      fontpack_id=bytes(8), layout=b"\x00")
                assert refused.status == Status.NOT_FOUND  # not configured yet

                with client.expect(matches(NodeConfigured, addr=addr)) as configured:
                    assert (await client.configure_node(addr)).status == Status.ACCEPTED
                    assert (await configured.wait(10)).status == Status.OK
                nodes = {n.addr: n for n in await client.list_nodes()}
                assert nodes[addr].configured and nodes[addr].name == "hall" and nodes[addr].uuid == fresh.uuid
                assert (await client.identify_node(addr)).status == Status.OK

                with client.expect(matches(NodeRemoved, addr=addr)) as removed:
                    assert (await client.remove_node(addr)).status == Status.ACCEPTED
                    assert (await removed.wait(10)).status == Status.OK
                assert addr not in {n.addr for n in await client.list_nodes()}
                assert not fresh.provisioned
                assert (await client.remove_node(addr)).status == Status.NOT_FOUND
                await client.drain_events()
            # Every retained event reached the handler, in order, and was acknowledged.
            kinds = [type(e).__name__ for e in events if e.retained]
            assert kinds == ["Provisioned", "NodeConfigured", "NodeRemoved"]
            assert sim.gateway.endpoint.retained_seqs == []

    run_scenario(scenario())


def test_cdb_survives_a_restart_through_the_state_file(tmp_path: Path) -> None:
    state = tmp_path / "sim.json"

    async def provision() -> int:
        async with Simulator(config(state)) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                with client.expect(matches(Provisioned)) as waiter:
                    await client.provision(sim.bridge(1).uuid)
                    return (await waiter.wait(10)).addr

    async def check(addr: int) -> None:
        async with Simulator(config(state)) as sim:
            assert sim.bridge(1).addr == addr
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                assert addr in {n.addr for n in await client.list_nodes()}

    addr = run_scenario(provision())
    assert state.exists()
    run_scenario(check(addr))


def test_provisioning_refusals() -> None:
    cfg = SimConfig(seed=52, time_scale=200, bridges=[BridgeSpec() for _ in range(MAX_BRIDGES)])

    async def scenario() -> None:
        async with Simulator(cfg) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                full = await client.provision(bytes(16))
                assert full.status == Status.NO_RESOURCES
                retry = await client.provision(bytes(16), op_id=full.op_id)
                assert not retry.duplicate  # a transient refusal is not remembered
                assert (await client.configure_node(0x7000)).status == Status.NOT_FOUND

    run_scenario(scenario())


def test_provisioning_an_unknown_uuid_fails_in_the_event() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                with client.expect(matches(Provisioned)) as waiter:
                    assert (await client.provision(b"\x42" * 16)).status == Status.ACCEPTED
                    assert (await waiter.wait(10)).status == Status.NOT_FOUND

    run_scenario(scenario())
