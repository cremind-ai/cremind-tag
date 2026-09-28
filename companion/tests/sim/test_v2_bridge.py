"""Protocol v2 on simulated bridges (docs/connect-setup.md 3.4, 5.4, 6, 8.2, 8.5): the device_id beacon,
static-OOB provisioning, the tunnel to the bridge's own endpoint, PAIR (proof_d), REKEY, RELEASE (locked),
the maintenance port (MAINT_AUTH, RECOMMISSION, FACTORY_SETUP)."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from cremind_tag.protocol.ids import (
    GrantOp,
    NodeRole,
    OwnerState,
    PairKind,
    SerialMsg,
    Status,
    TunnelState,
)
from cremind_tag.secure import identity
from cremind_tag.secure.codes import SetupPayload, parse_code
from cremind_tag.secure.messages import pair_message
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator
from cremind_tag.sim.harness import run_scenario
from cremind_tag.sim.v2 import MESH_OOB_ON_BOX

v2host: Any = sys.modules["v2host"]  # loaded by conftest
Authority, Worker, V2Host, SessionFailed = v2host.Authority, v2host.Worker, v2host.V2Host, v2host.SessionFailed
TunnelClosed = v2host.TunnelClosed

pytestmark = pytest.mark.timeout(120)


def config(*bridges: BridgeSpec, seed: int = 81, **overrides: Any) -> SimConfig:
    return SimConfig(seed=seed, time_scale=300, protocol=2, bridges=list(bridges or [BridgeSpec(provisioned=False)]),
                     **overrides)


def label(sim: Simulator, index: int = 0) -> SetupPayload:
    return parse_code(sim.setup_codes()[index]["code"], role=NodeRole.BRIDGE)


async def claimed(sim: Simulator, host: Any) -> tuple[Any, Any]:
    auth, worker = Authority.new(), Worker()
    assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
    return auth, worker


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


async def provision_and_configure(host: Any, sim: Simulator, index: int = 0) -> int:
    bridge = sim.bridge(index)
    code = label(sim, index)
    provisioned = await v2host.provision(host, bridge.uuid, v2host.static_oob(code, bridge.uuid))
    assert provisioned["status"] == Status.OK
    assert (await v2host.configure(host, provisioned["addr"]))["status"] == Status.OK
    return provisioned["addr"]


def test_beacon_and_static_oob_provisioning() -> None:
    cfg = config(BridgeSpec(provisioned=False), BridgeSpec(provisioned=False, protocol=1))

    async def scenario() -> None:
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            await claimed(sim, host)
            v2_bridge, v1_bridge = sim.bridge(0), sim.bridge(1)
            assert v2_bridge.uuid == v2_bridge.secure.device_id and v1_bridge.secure is None
            await host.ok(SerialMsg.SCAN_UNPROV, {"duration_s": 3})
            beacon = await host.wait_event(SerialMsg.EVT_UNPROV_BEACON, uuid=v2_bridge.uuid)
            assert beacon.fields["oob"] == MESH_OOB_ON_BOX  # static OOB "on box"
            v1_beacon = await host.wait_event(SerialMsg.EVT_UNPROV_BEACON, uuid=v1_bridge.uuid)
            assert v1_beacon.fields["oob"] == 0
            # The label's short_id finds the beacon (8.2 step 2).
            code = label(sim)
            assert identity.short_id(beacon.fields["uuid"]) == code.short_id
            # A wrong value, a missing one: SECURITY_CONFIG, the bridge stays unprovisioned.
            wrong = v2host.static_oob(SetupPayload(NodeRole.BRIDGE, code.short_id, bytes(10)), v2_bridge.uuid)
            for value in (wrong, None):
                event = await v2host.provision(host, v2_bridge.uuid, value)
                assert (event["status"], event["addr"]) == (Status.SECURITY_CONFIG, 0)
                assert not v2_bridge.provisioned and v2_bridge.beaconing()
            # A v1 bridge offers no static OOB: a v2 gateway refuses it.
            event = await v2host.provision(host, v1_bridge.uuid, wrong)
            assert event["status"] == Status.SECURITY_CONFIG and not v1_bridge.provisioned
            event = await v2host.provision(host, v2_bridge.uuid, v2host.static_oob(code, v2_bridge.uuid), name="hall")
            assert event["status"] == Status.OK and v2_bridge.addr == event["addr"] and not v2_bridge.beaconing()
            assert sim.gateway.counters["provision_oob_failed"] == 3
            # CAPS2_STATUS follows CAPS_STATUS: the inventory knows the bridge's identity and ownership.
            since = len(host.events)
            assert (await v2host.configure(host, event["addr"]))["status"] == Status.OK
            info = await host.wait_event(SerialMsg.EVT_BRIDGE_INFO, since=since, addr=event["addr"])
            while "device_id" not in info.fields["caps"]:
                since = host.events.index(info) + 1
                info = await host.wait_event(SerialMsg.EVT_BRIDGE_INFO, since=since, addr=event["addr"])
            caps = info.fields["caps"]
            assert (caps["device_id"], caps["owner_state"], caps["gen"]) == (v2_bridge.uuid, OwnerState.UNOWNED, 0)

    run_scenario(scenario())


def test_pair_through_the_tunnel_then_rekey_from_another_controller() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, worker = await claimed(sim, host)
            bridge = sim.bridge(0)
            addr = await provision_and_configure(host, sim)
            # Tunnel rules at the gateway: an unknown tunnel, a bad duration.
            assert (await host.call(SerialMsg.TUNNEL_SEND, {"tunnel": 999, "data": b"x"}))["status"] == \
                Status.NOT_FOUND
            bad = await host.call(SerialMsg.TUNNEL_OPEN, {"op_id": host.new_op_id(), "bridge": addr, "tag_id": 0,
                                                          "duration_s": 0})
            assert bad["status"] == Status.INVALID
            op = host.new_op_id()
            opened = await host.ok(SerialMsg.TUNNEL_OPEN, {"op_id": op, "bridge": addr, "tag_id": 0,
                                                           "duration_s": 60})
            again = await host.ok(SerialMsg.TUNNEL_OPEN, {"op_id": op, "bridge": addr, "tag_id": 0,
                                                          "duration_s": 60})
            assert again["tunnel"] == opened["tunnel"] and again["detail"] == Status.DUPLICATE  # idempotent
            tunnel = v2host.HostTunnel(host, opened["tunnel"], addr, 0)
            ident = await tunnel.wait_open()
            assert (ident.proto, ident.role, ident.device_id, ident.ik) == (
                2, NodeRole.BRIDGE, bridge.uuid, bridge.secure.keys.ik_pub)
            assert (ident.owner_state, ident.gen, ident.authority_id) == (OwnerState.UNOWNED, 0, bytes(16))
            # One tunnel per bridge; a message the endpoint could never take.
            busy = await host.call(SerialMsg.TUNNEL_OPEN, {"op_id": host.new_op_id(), "bridge": addr, "tag_id": 0,
                                                           "duration_s": 60})
            assert busy["status"] == Status.BUSY
            too_large = await host.call(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel.tunnel, "data": bytes(401)})
            assert too_large["status"] == Status.TOO_LARGE
            await tunnel.handshake(worker)
            code = label(sim)
            mk = os.urandom(32)
            assert (await tunnel.pair(auth, worker, bytes(10), mk)) == (Status.PROOF_FAILED, False)
            assert bridge.secure.record.state == OwnerState.UNOWNED
            assert (await tunnel.pair(auth, worker, code.secret, mk)) == (Status.OK, True)  # proof_d checked
            record = bridge.secure.record
            assert (record.state, record.gen, record.op_key, record.controller) == (OwnerState.OWNED, 1, mk,
                                                                                      worker.pub)
            _, status = await tunnel.challenge()
            assert status["controller_match"] is True and status["authority_id"] == auth.authority_id
            assert "root_proof" not in status  # a tag's
            # MAINT_AUTH and RECOMMISSION belong to the USB port.
            assert (await tunnel.call(SerialMsg.MAINT_AUTH, {"proof": bytes(16)}))["status"] == Status.NOT_OWNER
            assert (await tunnel.call(SerialMsg.RECOMMISSION))["status"] == Status.NOT_OWNER
            await tunnel.close()
            # Recovery (8.4): a new worker REKEYs the bridge (new controller, new mk) through a new tunnel.
            new_worker, new_mk = Worker(), os.urandom(32)
            tunnel = await host.open_tunnel(addr)
            await tunnel.wait_open()
            await tunnel.handshake(new_worker)
            reply = await tunnel.grant_op(auth, new_worker, SerialMsg.REKEY, GrantOp.REKEY, op_key=new_mk)
            assert (reply["status"], reply["gen"]) == (Status.OK, 2)
            record = bridge.secure.record
            assert (record.controller, record.op_key) == (new_worker.pub, new_mk)
            # A worker without the pinned authority cannot rekey it.
            _, status = await tunnel.challenge()
            other = Authority.new(owner=b"\x44" * 16)
            grant = other.grant(GrantOp.REKEY, bridge.uuid, NodeRole.BRIDGE, new_worker.pub, 2, status["challenge"],
                                owner=other.owner)
            assert (await tunnel.call(SerialMsg.REKEY, {**grant, "op_key": bytes(32)}))["status"] == Status.NOT_OWNER
            await tunnel.close()
            # Each tunnel ended once: by the host's TUNNEL_CLOSE or by the bridge answering the worker's CLOSE.
            counters = sim.gateway.counters
            assert counters["tunnels_closed_by_host"] + counters["tunnels_closed_ok"] == 2

    run_scenario(scenario())


def test_a_bridge_refuses_a_tunnel_while_it_holds_one() -> None:
    """The bridge's own rule (a second gateway, or one that forgot its tunnel): the new tunnel is closed BUSY."""
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            _auth, _worker = await claimed(sim, host)
            addr = await provision_and_configure(host, sim)
            first = await host.open_tunnel(addr, duration_s=255)
            await first.wait_open()
            sim.gateway._tunnels.clear()  # the gateway forgets it (as after a reboot); the bridge still holds it
            second = await host.open_tunnel(addr)
            assert await second.wait_closed() == Status.BUSY
            assert sim.bridge(0).counters["tunnel_busy"] == 1

    run_scenario(scenario())


def test_tunnel_endpoint_session_rules() -> None:
    """A transport message without a session, a garbled one, a bad handshake: CLOSE{status} up the tunnel."""
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            _auth, worker = await claimed(sim, host)
            addr = await provision_and_configure(host, sim)
            tunnel = await host.open_tunnel(addr, duration_s=60)
            await tunnel.wait_open()
            await tunnel.send(pair_message(PairKind.TRANSPORT, bytes(40)))
            assert await tunnel.recv() == pair_message(PairKind.CLOSE, bytes([Status.AUTH_REQUIRED]))
            await tunnel.send(pair_message(PairKind.HANDSHAKE, bytes(10)))
            assert await tunnel.recv() == pair_message(PairKind.CLOSE, bytes([Status.AUTH_FAILED]))
            await tunnel.send(b"\x09garbage")
            assert await tunnel.recv() == pair_message(PairKind.CLOSE, bytes([Status.INVALID]))
            channel = await tunnel.handshake(worker)
            await tunnel.send(pair_message(PairKind.TRANSPORT, bytes(40)))  # breaks the session
            assert await tunnel.recv() == pair_message(PairKind.CLOSE, bytes([Status.AUTH_REQUIRED]))
            assert channel.open  # the worker learns it from the CLOSE and opens a new session
            await tunnel.handshake(worker)
            assert (await tunnel.call(SerialMsg.STATUS))["status"] == Status.OK
            await tunnel.close()
            await asyncio.wait_for(_until(lambda: sim.bridge(0).tunnel is None), 5)
            # Idle: the bridge closes a tunnel after duration_s without progress.
            idle = await host.open_tunnel(addr, duration_s=2)
            await idle.wait_open()
            assert await idle.wait_closed(timeout=10) == Status.TIMEOUT
            assert sim.bridge(0).tunnel is None and sim.bridge(0).counters["tunnel_closed_timeout"] == 1

    run_scenario(scenario())


def test_release_locks_the_bridge_until_it_is_recommissioned_over_usb() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, worker = await claimed(sim, host)
            bridge = sim.bridge(0)
            code = label(sim)
            mk = os.urandom(32)
            addr, _ = await v2host.add_bridge(host, auth, worker, sim.setup_codes()[0]["code"], mk)
            tunnel = await host.open_tunnel(addr)
            await tunnel.wait_open()
            await tunnel.handshake(worker)
            reply = await tunnel.grant_op(auth, worker, SerialMsg.RELEASE, GrantOp.RELEASE)
            assert (reply["status"], reply["gen"]) == (Status.OK, 2)
            assert await tunnel.wait_closed() == Status.OK
            record = bridge.secure.record
            assert (record.state, record.locked, bridge.provisioned) == (OwnerState.RELEASED, True, False)
            # "Remove bridge": the worker removes the node; the locked bridge does not beacon.
            op = host.new_op_id()
            await host.ok(SerialMsg.REMOVE_NODE, {"op_id": op, "addr": addr}, expect=Status.ACCEPTED)
            await host.wait_event(SerialMsg.EVT_NODE_REMOVED, op_id=op)
            since = len(host.events)
            await host.ok(SerialMsg.SCAN_UNPROV, {"duration_s": 2})
            event = await v2host.provision(host, bridge.uuid, v2host.static_oob(code, bridge.uuid))
            assert event["status"] == Status.NOT_FOUND  # (provisioning outlasts the scan)
            assert not bridge.beaconing()
            assert not any(e.matches(SerialMsg.EVT_UNPROV_BEACON, uuid=bridge.uuid) for e in host.events[since:])
            # Local USB: a released bridge recommissions without a grant and shows a fresh setup code.
            async with V2Host(sim.bridge_url(0)) as usb:
                ident = await usb.identify()
                assert (ident["role"], ident["owner_state"], ident["gen"]) == (NodeRole.BRIDGE, OwnerState.RELEASED, 2)
                assert (await usb.request(SerialMsg.FONT_STATUS))["status"] == Status.OK  # not owned: plaintext
                await v2host.open_session(usb, Worker())
                reply = await usb.ok(SerialMsg.RECOMMISSION)
                fresh = SetupPayload.unpack(reply["data"])
                assert fresh.short_id == code.short_id and fresh.secret != code.secret and reply["gen"] == 2
            assert bridge.beaconing() and not bridge.secure.record.locked
            assert sim.setup_codes()[0]["code"] == fresh.code()
            # The old label no longer provisions; the fresh code does, and the bridge pairs again.
            event = await v2host.provision(host, bridge.uuid, v2host.static_oob(code, bridge.uuid))
            assert event["status"] == Status.SECURITY_CONFIG
            addr, _ = await v2host.add_bridge(host, auth, worker, fresh.code(), os.urandom(32))
            assert bridge.secure.record.state == OwnerState.OWNED and bridge.secure.record.gen == 3

    run_scenario(scenario())


def test_maintenance_port_of_an_owned_bridge() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim:
            bridge = sim.bridge(0)
            # Unowned: the factory's plaintext maintenance catalogue.
            async with V2Host(sim.bridge_url(0)) as usb:
                assert (await usb.request(SerialMsg.FONT_STATUS))["status"] == Status.OK
                assert (await usb.request(SerialMsg.INFO))["status"] == Status.OK
                ident = await usb.identify()
                assert (ident["device_id"], ident["owner_state"]) == (bridge.uuid, OwnerState.UNOWNED)
            async with V2Host(sim.gateway_url) as host:
                auth, worker = await claimed(sim, host)
                mk = os.urandom(32)
                addr, _ = await v2host.add_bridge(host, auth, worker, sim.setup_codes()[0]["code"], mk)
            code = label(sim)
            async with V2Host(sim.bridge_url(0)) as usb:
                for msg in (SerialMsg.FONT_STATUS, SerialMsg.INFO, SerialMsg.FLASH_TEST, SerialMsg.FACTORY_SETUP):
                    fields = {"op_id": 1} if msg == SerialMsg.FLASH_TEST else {}
                    if msg == SerialMsg.FACTORY_SETUP:
                        fields = {"data": bytes(10)}
                    assert (await usb.request(msg, fields))["status"] == Status.AUTH_REQUIRED, msg.name
                assert (await usb.request(SerialMsg.PING))["status"] == Status.OK
                replacement = Worker()  # 8.5: the replacement computer, with the recovered mk
                await v2host.open_session(usb, replacement)
                assert (await usb.call(SerialMsg.FONT_STATUS))["status"] == Status.NOT_OWNER
                status = await usb.ok(SerialMsg.STATUS)
                grant = auth.grant(GrantOp.MAINT, bridge.uuid, NodeRole.BRIDGE, replacement.pub, 1,
                                   status["challenge"])
                assert (await usb.call(SerialMsg.RECOMMISSION, grant))["status"] == Status.NOT_OWNER  # MAINT_AUTH first
                assert (await usb.call(SerialMsg.MAINT_AUTH,
                                       {"proof": usb.channel.maint_proof(os.urandom(32))}))["status"] == \
                    Status.PROOF_FAILED
                await usb.ok(SerialMsg.MAINT_AUTH, {"proof": usb.channel.maint_proof(mk)})
                status = await usb.ok(SerialMsg.FONT_STATUS)
                assert status["flash_size"] == bridge.flash.size and status.get("fontpack_id") == bridge.fontpack_id
                status = await usb.ok(SerialMsg.STATUS)
                grant = auth.grant(GrantOp.MAINT, bridge.uuid, NodeRole.BRIDGE, replacement.pub, 1,
                                   status["challenge"])
                reply = await usb.ok(SerialMsg.RECOMMISSION, grant)
                fresh = SetupPayload.unpack(reply["data"])
                assert reply["gen"] == 2 and fresh.secret != code.secret
            record = bridge.secure.record
            assert (record.state, record.gen, record.override_secret, bridge.provisioned) == (
                OwnerState.RELEASED, 2, fresh.secret, False)
            assert bridge.beaconing()  # recommissioned: it pairs with the fresh code into a (new) mesh

    run_scenario(scenario())


def test_factory_setup_stores_the_label_secret_once() -> None:
    cfg = config(BridgeSpec(provisioned=False, labelled=False))

    async def scenario() -> None:
        async with Simulator(cfg) as sim:
            bridge = sim.bridge(0)
            entry = sim.setup_codes()[0]
            assert entry["code"] is None and not bridge.beaconing()
            secret = os.urandom(10)
            async with V2Host(sim.bridge_url(0)) as usb:
                reply = await usb.request(SerialMsg.FACTORY_SETUP, {"data": secret[:9]})
                assert reply["status"] == Status.INVALID
                assert (await usb.request(SerialMsg.FACTORY_SETUP, {"data": secret}))["status"] == Status.OK
                assert (await usb.request(SerialMsg.FACTORY_SETUP, {"data": secret}))["status"] == Status.LOCKED
                # Inside a session too (the factory station may open one): still locked.
                await v2host.open_session(usb, Worker())
                assert (await usb.call(SerialMsg.FACTORY_SETUP, {"data": secret}))["status"] == Status.LOCKED
            code = parse_code(sim.setup_codes()[0]["code"], role=NodeRole.BRIDGE)
            assert code.secret == secret and bridge.beaconing()
            async with V2Host(sim.gateway_url) as host:
                auth, worker = await claimed(sim, host)
                await v2host.add_bridge(host, auth, worker, code.code(), os.urandom(32))
            assert bridge.secure.record.state == OwnerState.OWNED

    run_scenario(scenario())


def test_bridge_ownership_and_label_survive_a_restart(tmp_path: Any) -> None:
    state = tmp_path / "sim.json"
    cfg = config(BridgeSpec(provisioned=False, labelled=False), state_file=state)
    secret, mk = os.urandom(10), os.urandom(32)

    async def first() -> None:
        async with Simulator(cfg) as sim:
            async with V2Host(sim.bridge_url(0)) as usb:
                assert (await usb.request(SerialMsg.FACTORY_SETUP, {"data": secret}))["status"] == Status.OK
            async with V2Host(sim.gateway_url) as host:
                auth, worker = await claimed(sim, host)
                code = SetupPayload(NodeRole.BRIDGE, sim.bridge(0).secure.keys.short_id, secret).code()
                await v2host.add_bridge(host, auth, worker, code, mk)

    async def second() -> None:
        async with Simulator(cfg) as sim:
            bridge = sim.bridge(0)
            record = bridge.secure.record
            assert (record.state, record.gen, record.op_key) == (OwnerState.OWNED, 1, mk)
            assert bridge.secure.keys.factory_secret == secret and bridge.provisioned
            assert sim.gateway.secure.record.state == OwnerState.OWNED and bridge.addr in sim.gateway.cdb

    run_scenario(first())
    run_scenario(second())


def test_tunnel_events_carry_the_tunnel_state() -> None:
    """EVT_TUNNEL OPEN carries the ident2, DATA the endpoint's messages, CLOSED the status (never retained)."""
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            _auth, worker = await claimed(sim, host)
            addr = await provision_and_configure(host, sim)
            tunnel = await host.open_tunnel(addr)
            await tunnel.wait_open()
            await tunnel.handshake(worker)
            await tunnel.close()
            states = [e.fields["state"] for e in host.events if e.msg == SerialMsg.EVT_TUNNEL
                      and e.fields["tunnel"] == tunnel.tunnel]
            assert states[:2] == [TunnelState.OPEN, TunnelState.DATA]
            assert all("seq" not in e.fields for e in host.events if e.msg == SerialMsg.EVT_TUNNEL)
            # A closed tunnel is gone at the gateway.
            assert (await host.call(SerialMsg.TUNNEL_CLOSE, {"tunnel": tunnel.tunnel}))["status"] == Status.NOT_FOUND
            reply = await host.call(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel.tunnel, "data": b"\x01"})
            assert reply["status"] == Status.NOT_FOUND
            # A tunnel to a tag nobody hears ends when the bridge's idle timer does.
            lost = await host.open_tunnel(addr, tag_id=0x0BADF00D, duration_s=2)
            with pytest.raises(TunnelClosed) as closed:
                await lost.wait_open(timeout=10)
            assert closed.value.status == Status.TIMEOUT

    run_scenario(scenario())
