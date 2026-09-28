"""A Cremind Connect worker end to end against the simulator (docs/connect-setup.md §8): the daemon on a
secure link pinned to its v2 gateway, and the agent running Cremind's operations — claim the gateway,
find and pair a bridge (static OOB, tunnel, PAIR with both proofs), find and pair a tag (assign and clear
under ``K_epoch`` v2), remove the tag (two-stage release), release the gateway (the worker retires);
recover everything on a replacement computer; pair a released tag again above the epoch it kept."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connect.setup_flow import CONTROLLER_SCHEMA, write_private
from cremind_tag.connect.workerdir import WorkerSpec, load_worker, write_worker
from cremind_tag.protocol.ids import NodeRole, OwnerState
from cremind_tag.secrets import FileBackend, SecretStore
from cremind_tag.secure import identity
from cremind_tag.secure.codes import SetupPayload, parse_code
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator, TagSpec

pytestmark = pytest.mark.timeout(300)


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FakeV2Cremind = _load("fake_v2").FakeV2Cremind
SEED = 131


REPO = Path(__file__).resolve().parents[3]


def config(fontpack: bytes | None = None) -> SimConfig:
    return SimConfig(seed=SEED, time_scale=200, protocol=2, fontpack=fontpack, bridges=[BridgeSpec(provisioned=False)],
                     tags=[TagSpec.generate(SEED, 0, protocol=2)])


@pytest.fixture(scope="module")
def dev_fonts() -> tuple[Any, bytes]:
    """The dev font pack (the worker composes; the simulated bridge renders): removing a tag shows its new
    setup code, which needs fonts. Skipped where the pack was not built (CI without the font cache)."""
    from cremind_tag.fonts.fontset import FontSet

    pack, cache = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp", REPO / "fonts" / "cache"
    if not pack.is_file() or not cache.is_dir():
        pytest.skip(f"{pack} or {cache} is missing (run `cremind-tag fonts fetch` and `fonts build --profile dev`)")
    return FontSet.load(pack, cache), pack.read_bytes()


def make_worker(directory: Path, fake: Any, gateway_id: bytes, gateway_ik: bytes, *,
                bind_gateway: bool = True) -> bytes:
    """A worker directory as the setup window leaves it; the fake knows its controller key."""
    directory.mkdir(parents=True, exist_ok=True)
    priv, pub = identity.x25519_generate()
    write_private(directory / "controller.key",
                  json.dumps({"schema": CONTROLLER_SCHEMA, "private_key": priv.hex()}) + "\n")
    hardware = fake.add_credential("hardware")
    content = fake.add_credential("content", profile="anna")
    store = SecretStore(FileBackend(directory / "secrets.json"))
    store.set_credential("hardware", hardware.value)
    store.set_credential("content", content.value)
    write_worker(directory, WorkerSpec(directory.name, fake.url, "anna", fake.companion_id, gateway_id.hex(), True,
                                       extra={"profile_id": fake.profile_id,
                                              "authority_pub": fake.authority_pub.hex(),
                                              "gateway_ik": gateway_ik.hex()}))
    fake.controller_pub = pub
    if bind_gateway:
        fake.add_binding(gateway_id, "gateway", gateway_ik, 0)
    return priv


async def until(predicate: Any, timeout: float = 60.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what or predicate}")
        await asyncio.sleep(0.05)


async def run_op(fake: Any, kind: str, args: dict[str, Any], *, secret: bytes | None = None,
                 timeout: float = 120.0) -> dict[str, Any]:
    op_id = fake.queue_operation(kind, args, setup_secret=secret)
    await until(lambda: fake.op(op_id)["state"] in ("succeeded", "failed", "cancelled"), timeout, kind)
    op = fake.op(op_id)
    assert op["state"] == "succeeded", (kind, op)
    return op


async def discover(fake: Any, role: str, label: SetupPayload, bridges: list[str] | None = None) -> dict[str, Any]:
    op_id = fake.queue_operation("discovery", {"role": role, "short_id": label.short_id, "duration_s": 30,
                                               "bridges": bridges or []})
    await until(lambda: fake.op(op_id)["candidates"], 60, f"{role} discovery")
    fake.op(op_id)["state"] = "succeeded"  # a pairing starts from it (start_pairing closes the search)
    return dict(fake.op(op_id)["candidates"][0])


def label(sim: Simulator, role: str) -> SetupPayload:
    code = next(c["code"] for c in sim.setup_codes() if c["role"] == role)
    return parse_code(code, role=NodeRole.BRIDGE if role == "bridge" else NodeRole.TAG)


@dataclass
class Running:
    svc: Any
    agent: Any
    task: asyncio.Task[None]
    stop: asyncio.Event


@contextlib.asynccontextmanager
async def worker(directory: Path, sim: Simulator, paths: Any, fake: Any, fonts: Any = None
                 ) -> AsyncIterator[Running]:
    from cremind_tag.connect.worker import build, serve

    svc, agent = build(directory, sim.gateway_url, paths, transport=fake.transport, fonts=fonts)
    agent.discover_every_s = 0.2  # device time runs 200x faster here: a listening window is over in 0.1 s
    stop = asyncio.Event()
    task = asyncio.create_task(serve(svc, agent, stop=stop))
    try:
        yield Running(svc, agent, task, stop)
    finally:
        stop.set()
        if not task.done():
            await asyncio.wait_for(task, 30)


async def claim_and_pair(sim: Simulator, fake: Any) -> tuple[str, str, int]:
    """Claim the gateway, pair the bridge and the tag; returns (bridge device id, tag device id, tag id)."""
    gw = sim.gateway.secure
    assert gw is not None
    await run_op(fake, "claim_gateway", {"device_id": gw.device_id.hex(), "ik": gw.keys.ik_pub.hex(), "gen": 0,
                                         "mode": "claim"})
    bridge_label = label(sim, "bridge")
    cand = await discover(fake, "bridge", bridge_label)
    bridge_id = cand["uuid"]
    await run_op(fake, "pair_bridge", {"role": "bridge", "short_id": bridge_label.short_id, "uuid": bridge_id,
                                       "name": "Hall"}, secret=bridge_label.secret)
    tag_label = label(sim, "tag")
    cand = await discover(fake, "tag", tag_label, [f"br-{bridge_id}"])
    await run_op(fake, "pair_tag", {"role": "tag", "short_id": tag_label.short_id, "tag_id": tag_label.short_id,
                                    "bridge_hw_id": cand["bridge_hw_id"], "bridge_id": "b1", "name": "Desk"},
                 secret=tag_label.secret)
    tag = sim.tag(tag_label.short_id)
    assert tag.secure is not None
    return bridge_id, tag.secure.device_id.hex(), tag_label.short_id


def expected_setup_screen(sim: Simulator, tag_id: int, fonts: Any, payload: SetupPayload) -> bytes:
    """The digest of the planes the bridge renders for the setup-code screen of ``payload``."""
    import hashlib

    from cremind_tag.compose.api import TagPanel
    from cremind_tag.compose.screen import compose_setup_code
    from cremind_tag.render.reference import Panel, render_frame

    spec = sim.tag(tag_id).spec
    screen = compose_setup_code(TagPanel(tag_id, spec.width, spec.height, spec.planes, spec.plane_flags, 0, ""),
                                fonts, payload.code(), payload.qr_text())
    bridge_pack = sim.bridge(0).fontpack
    assert bridge_pack is not None
    frame = render_frame(screen.layout, Panel(spec.width, spec.height, spec.planes, spec.plane_flags), bridge_pack)
    return hashlib.sha256(b"".join(frame.planes)).digest()


def test_worker_claims_pairs_and_releases(paths: Any, dev_fonts: tuple[Any, bytes]) -> None:
    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
                # 8.1: the gateway is ours, pinned to this worker's controller key.
                assert gw.record.state == OwnerState.OWNED and gw.record.gen == 1
                assert gw.record.controller == fake.controller_pub
                assert fake.vault_latest(gw.device_id.hex())["stage"] == "committed"
                # 8.2: the bridge holds the mk the vault has.
                bridge = sim.bridge(0).secure
                assert bridge is not None and bridge.record.state == OwnerState.OWNED
                assert fake.bindings[bridge_id]["state"] == "ready" and fake.bindings[bridge_id]["generation"] == 1
                entry = fake.vault_latest(bridge_id)
                assert entry["stage"] == "committed" and bridge.record.op_key == bytes.fromhex(entry["state"]["mk"])
                # 8.3: the tag holds the root the vault has, is assigned and was cleared.
                tag = sim.tag(tag_id)
                assert tag.secure is not None and tag.secure.record.state == OwnerState.OWNED
                assert fake.bindings[tag_device]["state"] == "ready"
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert local is not None and local.epoch >= 1 and local.bridge_addr
                assert fake.vault_latest(tag_device)["state"]["root"] == tag.secure.record.op_key.hex()
                # Heartbeats carry the live generations the worker learned.
                await until(lambda: any(d.get("gen") == 1 and d.get("hw_id") == f"br-{bridge_id}"
                                        for hb in fake.heartbeats for d in hb.get("devices", [])), 90, "heartbeat")

                # 8.6: remove the tag: the release is prepared, the tag shows its fresh setup code, then the
                # release is committed and the assignment goes.
                unpair = await run_op(fake, "unpair", {"device_id": tag_device, "role": "tag",
                                                       "hw_id": f"{tag_id:08X}", "generation": 1,
                                                       "epoch": local.epoch})
                assert tag.secure.record.state == OwnerState.RELEASED
                assert await run.svc.db.run(run.svc.db.find_tag, tag_id) is None
                stages = [body.get("stage") for op_id, body in fake.progress_log if op_id == unpair["id"]]
                assert stages.index("showing_code") < len(stages) - 1
                fresh = label(sim, "tag")  # what the released tag pairs with now
                assert tag.displayed_digest == expected_setup_screen(sim, tag_id, fonts, fresh)

                # Remove the gateway: the bridge is released (and locked), then the gateway; the worker retires.
                await run_op(fake, "release_gateway", {
                    "device_id": gw.device_id.hex(), "role": "gateway",
                    "devices": [{"device_id": bridge_id, "role": "bridge", "generation": 1},
                                {"device_id": gw.device_id.hex(), "role": "gateway", "generation": 1}]})
                assert bridge.record.state == OwnerState.RELEASED and bridge.record.locked
                assert gw.record.state == OwnerState.UNOWNED
                await asyncio.wait_for(run.task, 30)
            spec = load_worker(directory)
            assert spec.enabled is False and spec.extra.get("removed") is True
            assert not (directory / "controller.key").exists() and not (directory / "secrets.json").exists()

    asyncio.run(scenario())


def test_recovery_on_a_replacement_computer(paths: Any) -> None:
    """8.4: a new worker (new controller key, new credentials, the same connection) gets the vault, takes
    the gateway with RECOVER, rekeys the bridge and the tag through tunnels, then assigns and clears the tag."""

    async def scenario() -> None:
        async with Simulator(config()) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            old_dir = paths.worker_dir("old")
            make_worker(old_dir, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(old_dir, sim, paths, fake):
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
            old_controller = fake.controller_pub
            bridge, tag = sim.bridge(0).secure, sim.tag(tag_id).secure
            assert bridge is not None and tag is not None
            old_mk, old_root = bridge.record.op_key, tag.record.op_key
            for cred in fake.credentials.values():  # the recovery revokes the old worker's credentials
                cred.revoked = True
            new_dir = paths.worker_dir("new")
            make_worker(new_dir, fake, gw.device_id, gw.keys.ik_pub, bind_gateway=False)
            assert fake.controller_pub != old_controller
            async with worker(new_dir, sim, paths, fake) as run:
                op = await run_op(fake, "recover_gateway", {}, timeout=180)
                assert op["result"] == {"pending": []}
                assert gw.record.controller == fake.controller_pub and gw.record.gen == 2
                assert bridge.record.controller == fake.controller_pub and bridge.record.op_key != old_mk
                assert tag.record.op_key != old_root and tag.record.gen == 2
                assert {d["state"] for d in op["devices"].values()} == {"rekeyed"}
                assert fake.vault_latest(tag_device)["state"]["root"] == tag.record.op_key.hex()
                assert fake.vault_latest(bridge_id)["state"]["mk"] == bridge.record.op_key.hex()
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                first_epoch = next(v["state"]["epoch"] for v in fake.vault[tag_device] if v["stage"] == "committed")
                assert local is not None and local.epoch > first_epoch

    asyncio.run(scenario())


def test_a_released_tag_pairs_again_with_its_fresh_code(paths: Any, dev_fonts: tuple[Any, bytes]) -> None:
    """A removed tag shows a fresh setup code; pairing it again works (the label's code no longer does), and
    the new assignment lands above the epoch the tag kept (its STALE_EPOCH raises the floor)."""

    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
                first = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert first is not None
                original = label(sim, "tag")
                await run_op(fake, "unpair", {"device_id": tag_device, "role": "tag", "hw_id": f"{tag_id:08X}",
                                              "generation": 1, "epoch": first.epoch})
                fresh_label = label(sim, "tag")  # the code the released tag pairs with now
                assert fresh_label.short_id == original.short_id and fresh_label.secret != original.secret
                old_root = bytes.fromhex(fake.vault_latest(tag_device)["state"]["root"])
                sim.tag(tag_id).nvs.stored_epoch = 5  # a tag with a longer history keeps its epoch through a release
                cand = await discover(fake, "tag", fresh_label, [f"br-{bridge_id}"])
                await run_op(fake, "pair_tag", {"role": "tag", "short_id": fresh_label.short_id, "tag_id": tag_id,
                                                "bridge_hw_id": cand["bridge_hw_id"], "bridge_id": "b1",
                                                "name": "Desk"}, secret=fresh_label.secret)
                tag = sim.tag(tag_id).secure
                assert tag is not None and tag.record.state == OwnerState.OWNED and tag.record.op_key != old_root
                again = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert again is not None and again.epoch > 5  # assigned again above the epoch the tag reported
                assert sim.tag(tag_id).nvs.stored_epoch == again.epoch  # and the clear under it went through

    asyncio.run(scenario())
