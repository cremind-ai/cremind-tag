"""Bridge maintenance port: font installation and flash test against a simulated bridge."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest

from cremind_tag.bridge_maint import BridgeMaintClient, FontInstallError
from cremind_tag.fontpack.format import FontPack
from cremind_tag.protocol.ids import NodeRole, SerialMsg, Status
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator
from cremind_tag.sim.flash import MIB, SECTOR
from cremind_tag.sim.harness import SimHarness, make_config, run_scenario

pytestmark = pytest.mark.timeout(120)


def bare_bridge(**spec: object) -> SimConfig:
    return SimConfig(seed=41, time_scale=200, bridges=[BridgeSpec(install_pack=False, **spec)])  # type: ignore[arg-type]


def test_font_install_with_progress_and_resume_safety(fixture_pack: bytes, tiny_pack: bytes) -> None:
    async def scenario() -> None:
        async with Simulator(bare_bridge()) as sim:
            bridge = sim.bridge(0)
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                hello = maint.hello_info
                assert hello is not None and hello.caps.role == NodeRole.BRIDGE
                assert (await maint.font_status()).fontpack_id is None

                progress: list[tuple[int, int]] = []
                result = await maint.font_install(fixture_pack, chunk_size=256,
                                                  progress=lambda done, total: progress.append((done, total)))
                assert result.fontpack_id == FontPack(fixture_pack).pack_id and not result.skipped
                assert progress[-1] == (len(fixture_pack), len(fixture_pack))
                assert [d for d, _ in progress] == sorted(d for d, _ in progress)
                status = await maint.font_status()
                assert (status.fontpack_id, status.slot, status.size) == (result.fontpack_id, 0, len(fixture_pack))
                assert bridge.fontpack_id == result.fontpack_id

                assert (await maint.font_install(fixture_pack)).skipped  # already active

                # An interrupted install leaves the active pack alone; the next install starts cleanly.
                begin = await maint.link.request(SerialMsg.FONT_BEGIN, {
                    "size": len(tiny_pack), "digest": hashlib.sha256(tiny_pack).digest(),
                    "fontpack_id": FontPack(tiny_pack).pack_id})
                assert begin["status"] == Status.OK and begin["slot"] == 1
                assert (await maint.link.request(SerialMsg.FONT_DATA, {"offset": 0, "data": tiny_pack[:100]}))[
                    "status"] == Status.OK
                assert (await maint.font_status()).fontpack_id == result.fontpack_id
                second = await maint.font_install(tiny_pack)  # FONT_ABORT first, then a full install
                assert second.fontpack_id == FontPack(tiny_pack).pack_id and second.slot == 1
                assert bridge.fontpack_id == second.fontpack_id
                assert bridge.fonts.installs == 2

    run_scenario(scenario())


def test_commit_refuses_a_wrong_digest(fixture_pack: bytes) -> None:
    async def scenario() -> None:
        async with Simulator(bare_bridge()) as sim:
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                link = maint.link
                await link.request(SerialMsg.FONT_BEGIN, {"size": len(fixture_pack), "digest": bytes(32),
                                                          "fontpack_id": FontPack(fixture_pack).pack_id})
                for offset in range(0, len(fixture_pack), 1024):
                    await link.request(SerialMsg.FONT_DATA, {"offset": offset,
                                                             "data": fixture_pack[offset:offset + 1024]})
                commit = await link.request(SerialMsg.FONT_COMMIT, {})
                assert commit["status"] == Status.DIGEST_MISMATCH
                assert (await maint.font_status()).fontpack_id is None
                # Out-of-order data is refused.
                await link.request(SerialMsg.FONT_BEGIN, {"size": len(fixture_pack),
                                                          "digest": hashlib.sha256(fixture_pack).digest(),
                                                          "fontpack_id": FontPack(fixture_pack).pack_id})
                bad = await link.request(SerialMsg.FONT_DATA, {"offset": 512, "data": fixture_pack[512:600]})
                assert bad["status"] == Status.INVALID

    run_scenario(scenario())


def test_install_errors_abort_cleanly(fixture_pack: bytes) -> None:
    small = bare_bridge(flash_size=16 * MIB)  # no room for slots beside the 16 MiB working space

    async def scenario() -> None:
        async with Simulator(small) as sim:
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                with pytest.raises(FontInstallError) as info:
                    await maint.font_install(fixture_pack)
                assert info.value.status == Status.NO_RESOURCES

    run_scenario(scenario())


def test_new_pack_changes_what_bridges_accept(fixture_pack: bytes, tiny_pack: bytes,
                                              card: Callable[[int], bytes]) -> None:
    async def scenario() -> None:
        async with SimHarness(make_config(fontpack=fixture_pack, seed=42)) as h:
            tag_id = h.tag_ids[0]
            old_id = h.sim.bridge(0).fontpack_id
            async with BridgeMaintClient(h.sim.bridge_url(0)) as maint:
                await maint.font_install(tiny_pack)
            assert h.sim.bridge(0).fontpack_id == FontPack(tiny_pack).pack_id != old_id
            refused = await h.client.deliver_layout(bridge=h.tag_bridge(tag_id), tag_id=tag_id, epoch=1, revision=1,
                                                    update_id=1, fontpack_id=old_id, layout=card(0))
            assert refused.status == Status.ACCEPTED
            assert (await h.wait_result(1)).status == Status.FONTPACK_MISMATCH
            ok = await h.deliver(tag_id, card(0), revision=2)
            assert (await h.wait_result(ok.update_id)).status == Status.OK

    run_scenario(scenario())


def test_flash_test_reports_each_offset(fixture_pack: bytes) -> None:
    config = bare_bridge(bad_sectors=(16 * MIB,))
    config.fontpack = fixture_pack
    config.bridges[0].install_pack = True

    async def scenario() -> None:
        async with Simulator(config) as sim:
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                result = await maint.flash_test()
                slot_size = sim.bridge(0).fonts.slot_size
                by_offset = {i.offset: i.status for i in result.items}
                assert result.flash_size == 64 * MIB
                assert by_offset == {16 * MIB - SECTOR: Status.BUSY,  # inside the active slot 0
                                     16 * MIB: Status.BUSY,
                                     slot_size: Status.OK,  # first sector of the inactive slot
                                     64 * MIB - SECTOR: Status.OK}
                assert not result.ok
                assert (await maint.font_status()).fontpack_id == FontPack(fixture_pack).pack_id

    run_scenario(scenario())


def test_flash_test_finds_a_bad_sector() -> None:
    config = bare_bridge(bad_sectors=(16 * MIB,), flash_size=128 * MIB)

    async def scenario() -> None:
        async with Simulator(config) as sim:
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                result = await maint.flash_test(op_id=77)
                by_offset = {i.offset: i.status for i in result.items}
                assert by_offset[16 * MIB] == Status.STORAGE_ERROR
                assert by_offset[16 * MIB - SECTOR] == Status.OK
                repeat = await maint.link.request(SerialMsg.FLASH_TEST, {"op_id": 77})
                assert repeat["detail"] == Status.DUPLICATE  # idempotent by op_id

    run_scenario(scenario())


def test_maintenance_port_refuses_mesh_requests() -> None:
    async def scenario() -> None:
        async with Simulator(bare_bridge()) as sim:
            async with BridgeMaintClient(sim.bridge_url(0)) as maint:
                response = await maint.link.request(SerialMsg.LIST_NODES, {})
                assert response["status"] == Status.UNSUPPORTED
                info = await maint.info()
                assert info.boot_id == sim.bridge(0).boot_id
                assert await maint.ping() >= 0

    run_scenario(scenario())
