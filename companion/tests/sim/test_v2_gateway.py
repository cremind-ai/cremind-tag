"""Protocol v2 on the simulated gateway (docs/connect-setup.md 4-5, 8.1): plaintext rules, CLAIM with a lost
answer, the §4.2 access table, RECOVER, session loss, sealed events, RELEASE, persistence."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.gateway import GatewayClient
from cremind_tag.protocol.ids import GrantOp, NodeRole, OwnerState, SerialMsg, Status
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator
from cremind_tag.sim.harness import run_scenario

v2host: Any = sys.modules["v2host"]  # loaded by conftest
Authority, Worker, V2Host, SessionFailed = v2host.Authority, v2host.Worker, v2host.V2Host, v2host.SessionFailed

pytestmark = pytest.mark.timeout(120)


def config(state_file: Path | None = None, **overrides: Any) -> SimConfig:
    return SimConfig(seed=71, time_scale=300, protocol=2, state_file=state_file,
                     bridges=[BridgeSpec(provisioned=False)], **overrides)


def test_plaintext_is_refused_except_the_v2_link_messages() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            hello = await host.hello()
            assert hello["status"] == Status.OK and hello["fw"] == "0.2.0"  # serial framing stays protocol 1
            for msg, fields in ((SerialMsg.INFO, {}), (SerialMsg.LIST_NODES, {}), (SerialMsg.EVENT_ACK, {"seq": 1}),
                                (SerialMsg.GET_INVENTORY, {}), (SerialMsg.STATUS, {}),
                                (SerialMsg.SCAN_UNPROV, {"duration_s": 1})):
                reply = await host.request(msg, fields)
                assert reply["status"] == Status.AUTH_REQUIRED, msg.name
            assert (await host.request(SerialMsg.PING))["status"] == Status.OK
            ident = await host.identify()
            device = sim.gateway.secure
            assert (ident["proto"], ident["role"], ident["device_id"], ident["ik"]) == (
                2, NodeRole.GATEWAY, device.device_id, device.keys.ik_pub)
            assert (ident["owner_state"], ident["gen"]) == (OwnerState.UNOWNED, 0) and "authority_id" not in ident
            again = await host.identify()
            assert again["challenge"] != ident["challenge"]  # fresh per IDENTIFY
            # A sealed frame without a session answers a plaintext AUTH_REQUIRED.
            await host.send_raw_secure(bytes(48))
            await asyncio.wait_for(_until(lambda: host.secure_failures == [Status.AUTH_REQUIRED]), 5)

    run_scenario(scenario())


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


def test_claim_survives_a_lost_answer() -> None:
    """8.1 step 5: the CLAIM's answer is lost; STATUS shows the gateway owned by our authority at gen_to."""
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, worker = Authority.new(), Worker()
            ident = await v2host.open_session(host, worker)
            # An unowned session: INFO, PING, STATUS, CLAIM; nothing else.
            assert (await host.call(SerialMsg.INFO))["status"] == Status.OK
            assert (await host.call(SerialMsg.PING))["status"] == Status.OK
            for msg, fields in ((SerialMsg.LIST_NODES, {}), (SerialMsg.EVENT_ACK, {"seq": 1}),
                                (SerialMsg.RECOVER, {"grant": b"x", "sig": bytes(64)})):
                assert (await host.call(msg, fields))["status"] == Status.NOT_OWNER, msg.name
            grant = auth.grant(GrantOp.CLAIM, ident["device_id"], NodeRole.GATEWAY, worker.pub, 0, ident["challenge"])
            sim.gateway.endpoint.drop_responses = 1
            with pytest.raises(TimeoutError):
                await host.call(SerialMsg.CLAIM, grant, timeout=0.5)
            status = await host.ok(SerialMsg.STATUS)
            assert status["owner_state"] == OwnerState.OWNED and status["gen"] == 1
            assert status["authority_id"] == auth.authority_id and status["owner"] == auth.owner
            assert status["controller_match"] is True
            # The same grant again: its challenge was single use.
            assert (await host.call(SerialMsg.CLAIM, grant))["status"] == Status.GRANT_INVALID
            assert (await host.ok(SerialMsg.LIST_NODES))["nodes"] == []  # owned, pinned controller: everything
            record = sim.gateway.secure.record
            assert (record.controller, record.authority_pub) == (worker.pub, auth.pub)

    run_scenario(scenario())


def test_access_table_and_recover_by_another_controller() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, a, b = Authority.new(), Worker(), Worker()
            assert (await v2host.claim(host, auth, a))["status"] == Status.OK
            # Another computer (controller b): INFO, PING, STATUS, RECOVER only.
            ident = await v2host.open_session(host, b)
            assert ident["owner_state"] == OwnerState.OWNED and ident["authority_id"] == auth.authority_id
            assert (await host.call(SerialMsg.INFO))["status"] == Status.OK
            status = await host.ok(SerialMsg.STATUS)
            assert status["controller_match"] is False
            for msg, fields in ((SerialMsg.LIST_NODES, {}),
                                (SerialMsg.DISCOVER, {"op_id": 1, "bridge": 0, "duration_s": 5, "tag_id": 0}),
                                (SerialMsg.TUNNEL_OPEN, {"op_id": 2, "bridge": 2, "tag_id": 0, "duration_s": 5})):
                assert (await host.call(msg, fields))["status"] == Status.NOT_OWNER, msg.name
            claim_again = auth.grant(GrantOp.CLAIM, ident["device_id"], NodeRole.GATEWAY, b.pub, 1,
                                     status["challenge"])
            assert (await host.call(SerialMsg.CLAIM, claim_again))["status"] == Status.NOT_OWNER
            # Another authority's RECOVER is refused; ours moves the controller to b.
            other = Authority.new(owner=b"\x22" * 16)
            status = await host.ok(SerialMsg.STATUS)
            foreign = other.grant(GrantOp.RECOVER, ident["device_id"], NodeRole.GATEWAY, b.pub, 1,
                                  status["challenge"], owner=other.owner)
            assert (await host.call(SerialMsg.RECOVER, foreign))["status"] == Status.NOT_OWNER
            status = await host.ok(SerialMsg.STATUS)
            stale = auth.grant(GrantOp.RECOVER, ident["device_id"], NodeRole.GATEWAY, b.pub, 0, status["challenge"])
            assert (await host.call(SerialMsg.RECOVER, stale))["status"] == Status.STALE_GENERATION
            status = await host.ok(SerialMsg.STATUS)
            recover = auth.grant(GrantOp.RECOVER, ident["device_id"], NodeRole.GATEWAY, b.pub, 1, status["challenge"])
            reply = await host.ok(SerialMsg.RECOVER, recover)
            assert reply["gen"] == 2
            assert (await host.ok(SerialMsg.LIST_NODES))["nodes"] == []
            # The old computer's key no longer operates the gateway.
            await v2host.open_session(host, a)
            assert (await host.call(SerialMsg.LIST_NODES))["status"] == Status.NOT_OWNER
            assert (await host.ok(SerialMsg.STATUS))["controller_match"] is False

    run_scenario(scenario())


def test_a_session_ends_on_a_bad_frame_and_on_hello() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, worker = Authority.new(), Worker()
            assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
            secure = sim.gateway.endpoint.secure
            assert secure is not None and secure.is_open
            # A frame that does not decrypt: plaintext AUTH_REQUIRED, the session is gone.
            channel = host.channel
            await host.send_raw_secure(bytes(40))
            await asyncio.wait_for(_until(lambda: Status.AUTH_REQUIRED in host.secure_failures), 5)
            assert not secure.is_open and host.channel is None
            _, sealed = channel.seal_request(SerialMsg.PING, {})
            await host.send_raw_secure(sealed)  # the old session's next message: no session any more
            await asyncio.wait_for(_until(lambda: len(host.secure_failures) == 2), 5)
            # A new session works; HELLO drops it again.
            await v2host.open_session(host, worker)
            assert (await host.ok(SerialMsg.LIST_NODES))["nodes"] == []
            channel = host.channel
            await host.hello()
            assert not secure.is_open
            _, sealed = channel.seal_request(SerialMsg.PING, {})
            await host.send_raw_secure(sealed)
            await asyncio.wait_for(_until(lambda: len(host.secure_failures) == 3), 5)
            assert sim.gateway.endpoint.counters["secure_failures"] == 3

    run_scenario(scenario())


def test_events_are_sealed_and_retained_ones_resent_in_a_new_session() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url, auto_ack=False) as host:
            auth, worker, other = Authority.new(), Worker(), Worker()
            assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
            op = host.new_op_id()
            await host.ok(SerialMsg.PROVISION, {"op_id": op, "uuid": b"\x42" * 16}, expect=Status.ACCEPTED)
            event = await host.wait_event(SerialMsg.EVT_PROVISIONED, op_id=op)
            assert event.fields["status"] == Status.NOT_FOUND and host.plain_events == []
            seq = event.fields["seq"]
            assert sim.gateway.endpoint.retained_seqs == [seq]
            # Another controller's session gets no events (retained or not).
            await v2host.open_session(host, other)
            await asyncio.sleep(0.2)
            assert host.count(SerialMsg.EVT_PROVISIONED) == 1
            # The owner's new session: the retained event is re-sent inside it; EVENT_ACK (sealed) releases it.
            await v2host.open_session(host, worker)
            await asyncio.wait_for(_until(lambda: host.count(SerialMsg.EVT_PROVISIONED, seq=seq) == 2), 5)
            assert (await host.ok(SerialMsg.EVENT_ACK, {"seq": seq}))["status"] == Status.OK
            assert sim.gateway.endpoint.retained_seqs == []

    run_scenario(scenario())


def test_release_wipes_the_network_and_keeps_the_generation() -> None:
    cfg = SimConfig(seed=72, time_scale=300, protocol=2, bridges=[BridgeSpec()])  # one bridge in the CDB

    async def scenario() -> None:
        async with Simulator(cfg) as sim:
            async with V2Host(sim.gateway_url, auto_ack=False) as host:
                auth, worker = Authority.new(), Worker()
                assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
                assert len((await host.ok(SerialMsg.LIST_NODES))["nodes"]) == 1
                op = host.new_op_id()  # an unacknowledged retained event of the owner
                await host.ok(SerialMsg.PROVISION, {"op_id": op, "uuid": b"\x42" * 16}, expect=Status.ACCEPTED)
                await host.wait_event(SerialMsg.EVT_PROVISIONED, op_id=op)
                assert sim.gateway.endpoint.retained_seqs != []
                status = await host.ok(SerialMsg.STATUS)
                device_id = sim.gateway.secure.device_id
                boot_id = sim.gateway.boot_id
                release = auth.grant(GrantOp.RELEASE, device_id, NodeRole.GATEWAY, worker.pub, 1,
                                     status["challenge"])
                reply = await host.ok(SerialMsg.RELEASE, release)
                assert reply["gen"] == 2
                record = sim.gateway.secure.record
                assert (record.state, record.gen, record.controller) == (OwnerState.UNOWNED, 2, b"")
                assert sim.gateway.cdb == {} and sim.gateway.assigned == {}
                assert sim.gateway.endpoint.retained_seqs == []  # the old owner's events never reach the next one
                # The gateway reboots once the answer is out (a new network on the next boot).
                await asyncio.wait_for(_until(lambda: sim.gateway.boot_id != boot_id), 5)
            async with V2Host(sim.gateway_url) as host:
                ident = await host.identify()
                assert (ident["owner_state"], ident["gen"]) == (OwnerState.UNOWNED, 2)
                # A new owner claims from generation 2; the old network is gone.
                other, next_worker = Authority.new(owner=b"\x33" * 16), Worker()
                assert (await v2host.claim(host, other, next_worker))["gen"] == 3
                assert (await host.ok(SerialMsg.LIST_NODES))["nodes"] == []
                assert sim.bridge(0).provisioned  # an orphan of the released network

    run_scenario(scenario())


def test_identity_and_ownership_survive_a_restart(tmp_path: Path) -> None:
    state = tmp_path / "sim.json"
    auth, worker = Authority.new(), Worker()

    async def first() -> bytes:
        async with Simulator(config(state)) as sim, V2Host(sim.gateway_url) as host:
            assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
            return sim.gateway.secure.device_id

    async def second(device_id: bytes) -> None:
        async with Simulator(config(state)) as sim, V2Host(sim.gateway_url) as host:
            ident = await host.identify()
            assert ident["device_id"] == device_id and ident["authority_id"] == auth.authority_id
            assert (ident["owner_state"], ident["gen"]) == (OwnerState.OWNED, 1)
            await v2host.open_session(host, worker)
            assert (await host.ok(SerialMsg.STATUS))["controller_match"] is True

    device_id = run_scenario(first())
    run_scenario(second(device_id))


def test_a_v1_gateway_does_not_speak_v2() -> None:
    async def scenario() -> None:
        async with Simulator(SimConfig(seed=73, time_scale=300)) as sim:
            async with GatewayClient(sim.gateway_url, reconnect=False) as client:
                for msg in (SerialMsg.IDENTIFY, SerialMsg.STATUS):
                    assert (await client.link.request(msg, {}))["status"] == Status.UNSUPPORTED
                reply = await client.link.request(SerialMsg.TUNNEL_CLOSE, {"tunnel": 1})
                assert reply["status"] == Status.UNSUPPORTED
                assert (await client.link.request(SerialMsg.INFO, {}))["status"] == Status.OK
            assert sim.gateway.secure is None and not sim.v2_devices()

    run_scenario(scenario())
