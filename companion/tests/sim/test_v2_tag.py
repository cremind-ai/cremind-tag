"""Protocol v2 on simulated tags (docs/connect-setup.md 3.3, 3.5, 5.1, 6, 7, 8.3): discovery, a tunnel through a
bridge to the tag's PAIR characteristic, PAIR, ASSIGN_TAG with K_epoch v2, CLEAR, a pinned layout, REKEY,
the two RELEASE stages and the anti-brute-force pause."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from cremind_tag.protocol.ids import (
    DISCOVER_MAX_S,
    RESULT_FLAG_ESCALATED,
    GrantOp,
    NodeRole,
    OwnerState,
    PairKind,
    SerialMsg,
    Status,
    TagCommand,
)
from cremind_tag.secure import identity
from cremind_tag.secure.codes import SetupPayload, parse_code
from cremind_tag.secure.messages import parse_pair_message
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator, TagSpec
from cremind_tag.sim.harness import run_scenario
from cremind_tag.sim.radio import ADV_FLAG_OWNED, ADV_FLAG_SETUP, ADV_VERSION_V2

v2host: Any = sys.modules["v2host"]  # loaded by conftest
Authority, Worker, V2Host = v2host.Authority, v2host.Worker, v2host.V2Host

pytestmark = pytest.mark.timeout(180)

SEED = 91


def config(*, tags: int = 1, fontpack: bytes | None = None, seed: int = SEED) -> SimConfig:
    # time_scale 200: a tag ends a pairing session after TAG_SESSION_TIMEOUT_MS (20 s) without progress, 0.1 s here.
    return SimConfig(seed=seed, time_scale=200, protocol=2, fontpack=fontpack,
                     bridges=[BridgeSpec(provisioned=False)],
                     tags=[TagSpec.generate(seed, i, protocol=2) for i in range(tags)])


class World:
    """A claimed gateway with one paired bridge, and the worker that owns them."""

    def __init__(self, sim: Simulator, host: Any) -> None:
        self.sim = sim
        self.host = host
        self.auth = Authority.new()
        self.worker = Worker()
        self.addr = 0

    async def setup(self) -> World:
        assert (await v2host.claim(self.host, self.auth, self.worker))["status"] == Status.OK
        code = next(c["code"] for c in self.sim.setup_codes() if c["role"] == "bridge")
        self.addr, _ = await v2host.add_bridge(self.host, self.auth, self.worker, code, os.urandom(32))
        return self

    def tag_label(self, tag_id: int) -> SetupPayload:
        code = next(c["code"] for c in self.sim.setup_codes() if c["role"] == "tag" and c["name"] == f"{tag_id:08X}")
        return parse_code(code, role=NodeRole.TAG)

    async def tunnel(self, tag_id: int, worker: Any = None) -> Any:
        tunnel = await self.host.open_tunnel(self.addr, tag_id, duration_s=90)
        ident = await tunnel.wait_open(timeout=30)
        assert (ident.role, ident.device_id) == (NodeRole.TAG, self.sim.tag(tag_id).secure.device_id)
        await tunnel.handshake(worker or self.worker)
        return tunnel

    async def pair(self, tag_id: int) -> bytes:
        """8.3 step 4: tunnel through the bridge, IDENT, PAIR with the label's proof, check proof_d."""
        root = os.urandom(32)
        tunnel = await self.tunnel(tag_id)
        assert tunnel.ident.owner_state == OwnerState.UNOWNED
        assert await tunnel.pair(self.auth, self.worker, self.tag_label(tag_id).secret, root) == (Status.OK, True)
        await tunnel.close()
        return root

    async def assign(self, tag_id: int, epoch: int, key: bytes) -> Status:
        op = self.host.new_op_id()
        await self.host.ok(SerialMsg.ASSIGN_TAG, {"op_id": op, "bridge": self.addr, "tag_id": tag_id,
                                                  "epoch": epoch, "key": key}, expect=Status.ACCEPTED)
        return Status((await self.host.wait_event(SerialMsg.EVT_ASSIGN_RESULT, op_id=op)).fields["status"])

    async def command(self, tag_id: int, epoch: int, cmd: TagCommand = TagCommand.CLEAR,
                      timeout: float = 30.0) -> dict[str, Any]:
        op = self.host.new_op_id() & 0xFFFFFFFF
        await self.host.ok(SerialMsg.TAG_COMMAND, {"op_id": op, "bridge": self.addr, "tag_id": tag_id,
                                                   "epoch": epoch, "cmd": cmd}, expect=Status.ACCEPTED)
        return (await self.host.wait_event(SerialMsg.EVT_RESULT, update_id=op, timeout=timeout)).fields

    async def deliver(self, tag_id: int, epoch: int, revision: int, layout: bytes) -> dict[str, Any]:
        update_id = self.host.new_op_id()
        pack = self.sim.bridge_at(self.addr).fontpack_id
        assert pack is not None
        await self.host.ok(SerialMsg.DELIVER_LAYOUT, {
            "op_id": self.host.new_op_id(), "bridge": self.addr, "tag_id": tag_id, "epoch": epoch,
            "revision": revision, "update_id": update_id, "fontpack_id": pack, "layout": layout},
            expect=Status.ACCEPTED)
        return (await self.host.wait_event(SerialMsg.EVT_RESULT, update_id=update_id, timeout=30)).fields


def test_discovery_reports_setup_mode_tags_once_per_window() -> None:
    cfg = config(tags=2)

    async def scenario() -> None:
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            first, second = (spec.tag_id for spec in cfg.tags)
            tag = sim.tag(first)
            advert = tag.advert()
            assert (advert.version, advert.flags & ADV_FLAG_SETUP, advert.tag_id) == (
                ADV_VERSION_V2, ADV_FLAG_SETUP, tag.secure.keys.short_id)
            reply = await host.call(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": 0,
                                                         "duration_s": DISCOVER_MAX_S + 1, "tag_id": 0})
            assert reply["status"] == Status.INVALID
            await host.ok(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": 0, "duration_s": 100,
                                               "tag_id": first}, expect=Status.ACCEPTED)
            found = await host.wait_event(SerialMsg.EVT_DISCOVERED, tag_id=first, timeout=30)
            assert found.fields["bridge"] == world.addr and found.fields["flags"] & ADV_FLAG_SETUP
            assert -128 <= found.fields["rssi"] < 0
            wakes = tag.stats["wakes"]
            await asyncio.wait_for(_until(lambda: tag.stats["wakes"] >= wakes + 2), 30)
            reports = host.count(SerialMsg.EVT_DISCOVERED, tag_id=first)
            # One report per advertising window (every advertisement is seen, DISCOVERED_MIN_INTERVAL_MS apart).
            assert 1 <= reports <= tag.stats["wakes"] - wakes + 1
            assert tag.stats["adverts"] > reports
            # The tag_id filter: the other tag was never reported.
            assert host.count(SerialMsg.EVT_DISCOVERED, tag_id=second) == 0
            # duration_s 0 stops discovery.
            await host.ok(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": world.addr, "duration_s": 0,
                                               "tag_id": 0}, expect=Status.ACCEPTED)
            await asyncio.sleep(0.05)
            before = host.count(SerialMsg.EVT_DISCOVERED)
            wakes = tag.stats["wakes"]
            await asyncio.wait_for(_until(lambda: tag.stats["wakes"] >= wakes + 2), 30)
            assert host.count(SerialMsg.EVT_DISCOVERED) == before

    run_scenario(scenario())


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


def test_pair_assign_clear_and_show_a_pinned_layout(fixture_pack: bytes,
                                                    render_scenarios: list[dict[str, Any]]) -> None:
    fixture = render_scenarios[0]  # 400x300, rotation 0, one plane: the tag's panel
    cfg = config(fontpack=fixture_pack)

    async def scenario() -> None:
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            tag_id = cfg.tags[0].tag_id
            tag = sim.tag(tag_id)
            # Unpaired, the tag authenticates nobody: it has no root.
            assert tag.secure.k_epoch(tag_id, 1) is None
            root = await world.pair(tag_id)
            record = tag.secure.record
            assert (record.state, record.gen, record.op_key) == (OwnerState.OWNED, 1, root)
            assert tag.advert().flags & (ADV_FLAG_OWNED | ADV_FLAG_SETUP) == ADV_FLAG_OWNED
            # 8.3 step 4: assign with K_epoch v2, then CLEAR; ready after the tag's authenticated OK.
            assert await world.assign(tag_id, 1, identity.k_epoch_v2(root, tag_id, 1)) == Status.OK
            cleared = await world.command(tag_id, 1)
            assert (cleared["status"], cleared["tag_id"], cleared["epoch"]) == (Status.OK, tag_id, 1)
            assert tag.stats["clears"] == 1 and tag.nvs.stored_epoch == 1
            # A pinned layout: the frame digest is the render fixture's.
            result = await world.deliver(tag_id, 1, 1, bytes.fromhex(fixture["layout_hex"]))
            assert result["status"] == Status.OK
            assert result["digest"].hex() == fixture["frame_digest"][:16]
            assert tag.displayed_digest.hex() == fixture["frame_digest"]
            # Retained results reached the worker sealed and were acknowledged.
            await asyncio.wait_for(_until(lambda: sim.gateway.endpoint.retained_seqs == []), 5)
            assert host.plain_events == []

    run_scenario(scenario())


def test_rekey_kills_the_old_assignment_until_the_tag_is_reassigned() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            tag_id = sim.config.tags[0].tag_id
            tag = sim.tag(tag_id)
            root = await world.pair(tag_id)
            assert await world.assign(tag_id, 1, identity.k_epoch_v2(root, tag_id, 1)) == Status.OK
            assert (await world.command(tag_id, 1))["status"] == Status.OK
            # 8.4: a recovered worker installs a new root (and becomes the tag's controller).
            recovered, new_root = Worker(), os.urandom(32)
            tunnel = await world.tunnel(tag_id, recovered)
            _, status = await tunnel.challenge()
            assert status["root_proof"] == tunnel.channel.root_proof(root)  # which root the tag holds
            reply = await tunnel.grant_op(world.auth, recovered, SerialMsg.REKEY, GrantOp.REKEY, op_key=new_root)
            assert (reply["status"], reply["gen"]) == (Status.OK, 2)
            _, status = await tunnel.challenge()
            assert status["root_proof"] == tunnel.channel.root_proof(new_root)
            await tunnel.close()
            assert tag.stats["rekeyed"] == 1
            # The bridge still holds K_epoch of the old root: the tag refuses it (unauthenticated AUTH_FAILED,
            # escalated after three sessions, protocol.md §10).
            failed = await world.command(tag_id, 1, timeout=60)
            assert failed["status"] == Status.AUTH_FAILED and failed["flags"] & RESULT_FLAG_ESCALATED
            assert failed["stored_epoch"] == 1 and tag.stats["auth_failures"] >= 3
            # Re-assigned with the new root (an epoch above the stored one), the tag works again.
            assert await world.assign(tag_id, 2, identity.k_epoch_v2(new_root, tag_id, 2)) == Status.OK
            assert (await world.command(tag_id, 2, timeout=60))["status"] == Status.OK
            assert tag.nvs.stored_epoch == 2

    run_scenario(scenario())


def test_release_in_two_stages_arms_a_fresh_code() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            tag_id = sim.config.tags[0].tag_id
            tag = sim.tag(tag_id)
            factory = world.tag_label(tag_id)
            root = await world.pair(tag_id)
            assert await world.assign(tag_id, 1, identity.k_epoch_v2(root, tag_id, 1)) == Status.OK
            tunnel = await world.tunnel(tag_id)
            # Stage 0 (prepare): the fresh code to show; the tag stays owned.
            reply = await tunnel.grant_op(world.auth, world.worker, SerialMsg.RELEASE, GrantOp.RELEASE,
                                          release_stage=0)
            assert (reply["status"], reply["gen"]) == (Status.OK, 1)
            fresh = SetupPayload.unpack(reply["data"])
            assert fresh.short_id == tag_id and fresh.secret != factory.secret and tag.owned
            # Stage 1 (commit) by another controller (its own session and grant): refused, the tag stays owned.
            stranger = Worker()
            await tunnel.handshake(stranger)
            reply = await tunnel.grant_op(world.auth, stranger, SerialMsg.RELEASE, GrantOp.RELEASE, release_stage=1)
            assert reply["status"] == Status.INVALID and tag.owned
            # A grant naming another controller than the session's is refused before that (rule 4).
            _, status = await tunnel.challenge()
            grant = world.auth.grant(GrantOp.RELEASE, tag.secure.device_id, NodeRole.TAG, world.worker.pub, 1,
                                     status["challenge"])
            assert (await tunnel.call(SerialMsg.RELEASE, {**grant, "release_stage": 1}))["status"] == \
                Status.GRANT_INVALID
            # The same controller as stage 0: released.
            await tunnel.handshake(world.worker)
            reply = await tunnel.grant_op(world.auth, world.worker, SerialMsg.RELEASE, GrantOp.RELEASE,
                                          release_stage=1)
            assert (reply["status"], reply["gen"]) == (Status.OK, 2)
            record = tag.secure.record
            assert (record.state, record.op_key, record.override_secret) == (OwnerState.RELEASED, b"", fresh.secret)
            assert tag.secure.k_epoch(tag_id, 1) is None and tag.advert().flags & ADV_FLAG_SETUP
            assert next(c for c in sim.setup_codes() if c["role"] == "tag")["code"] == fresh.code()
            # The label's code no longer pairs; the fresh one does (a new owner, from generation 2).
            new_root = os.urandom(32)
            assert await tunnel.pair(world.auth, world.worker, factory.secret, new_root) == (Status.PROOF_FAILED,
                                                                                               False)
            assert await tunnel.pair(world.auth, world.worker, fresh.secret, new_root) == (Status.OK, True)
            assert tag.secure.record.gen == 3 and tag.owned
            await tunnel.close()

    run_scenario(scenario())


def test_three_wrong_proofs_end_the_session_and_skip_a_wake_window() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            tag_id = sim.config.tags[0].tag_id
            tag = sim.tag(tag_id)
            tunnel = await world.tunnel(tag_id)
            for attempt in range(3):
                status, _ = await tunnel.pair(world.auth, world.worker, os.urandom(10), os.urandom(32))
                assert status == Status.PROOF_FAILED, attempt
            kind, body = parse_pair_message(await tunnel.recv())
            assert (kind, body) == (PairKind.CLOSE, bytes([Status.LOCKED]))
            assert await tunnel.wait_closed() == Status.DISCONNECTED  # the tag ended the connection
            assert tag.stats["pairing_paused"] == 1 and tag.secure.record.state == OwnerState.UNOWNED
            wakes = tag.stats["wakes"]
            await asyncio.wait_for(_until(lambda: tag.stats["wakes"] >= wakes + 2), 30)
            assert tag.stats["skipped_windows"] == 1
            # Afterwards the right code pairs.
            await world.pair(tag_id)
            assert tag.owned

    run_scenario(scenario())


def test_tag_ownership_survives_a_restart(tmp_path: Any) -> None:
    state = tmp_path / "sim.json"
    cfg = config()
    cfg.state_file = state
    keys: dict[str, Any] = {}

    async def first() -> None:
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            world = await World(sim, host).setup()
            tag_id = cfg.tags[0].tag_id
            keys["root"] = await world.pair(tag_id)
            keys["world"] = (world.auth, world.worker)

    async def second() -> None:
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            tag_id = cfg.tags[0].tag_id
            tag = sim.tag(tag_id)
            assert tag.owned and tag.secure.record.op_key == keys["root"]
            auth, worker = keys["world"]
            await v2host.open_session(host, worker)
            world = World(sim, host)
            world.auth, world.worker = auth, worker
            world.addr = sim.bridge(0).addr
            assert await world.assign(tag_id, 1, identity.k_epoch_v2(keys["root"], tag_id, 1)) == Status.OK
            assert (await world.command(tag_id, 1))["status"] == Status.OK

    run_scenario(first())
    run_scenario(second())
