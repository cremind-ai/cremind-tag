"""Unit tests of the simulator's building blocks (no network)."""

from __future__ import annotations

import asyncio
import socket

import pytest

from cremind_tag.fontpack.format import FontPack
from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import MESH_COMPANY_ID, SerialFlag, SerialMsg, Status
from cremind_tag.protocol.msgs import (
    MESH_MESSAGES,
    MeshAssignSet,
    MeshDeliveryResult,
    MeshLayoutBegin,
    MeshLayoutChunk,
    MeshResultAck,
)
from cremind_tag.protocol.serial_frame import Frame, FrameReader, encode_frame, frame_to_wire
from cremind_tag.protocol.tag_txn import DisplayRecord, StoredState
from cremind_tag.sim import SimFaults, TagSpec, parse_fault, rng_stream
from cremind_tag.sim.core import SimClock
from cremind_tag.sim.device import DeviceEndpoint
from cremind_tag.sim.flash import BLOCK, MIB, SECTOR, FontStore, SimFlash
from cremind_tag.sim.mesh import MeshPduError, pack_pdu, parse_pdu, segments
from cremind_tag.sim.radio import Advert
from cremind_tag.sim.tag import TagNvs
from cremind_tag.sim.world import FaultSpecError


def test_mesh_pdus_round_trip_through_the_generated_codecs() -> None:
    begin = MeshLayoutBegin(7, 0x1A2B3C4D, 3, 18, 501, b"\x01" * 8, 300, 2, b"\x02" * 16)
    pdu = pack_pdu(begin)
    assert pdu[:3] == bytes([0xC0 | 0x01]) + MESH_COMPANY_ID.to_bytes(2, "little")
    assert parse_pdu(pdu) == begin
    chunk = MeshLayoutChunk(7, 1, bytes(150))
    assert len(pack_pdu(chunk)) == 3 + 3 + 150 and segments(len(pack_pdu(chunk))) == 13
    assert segments(len(pack_pdu(MeshResultAck(1)))) == 1
    with pytest.raises(MeshPduError):
        parse_pdu(b"\x81\xff\xff")
    for cls in MESH_MESSAGES.values():
        assert cls.LEN + 3 <= 3 + 160


def test_advert_encoding() -> None:
    advert = Advert(0x1A2B3C4D, 0x05, 0x1234)
    data = advert.to_bytes()
    assert data[:3] == b"\x02\x01\x06" and data[4] == 0xFF
    assert Advert.parse(data) == advert
    assert Advert.parse(b"\x02\x01\x06") is None


def test_flash_is_nor_like() -> None:
    flash = SimFlash(BLOCK)
    assert flash.read(0, 4) == b"\xff" * 4
    flash.write(0, b"\x0f")
    flash.write(0, b"\xf0")  # programming only clears bits
    assert flash.read(0, 1) == b"\x00"
    flash.erase(0, SECTOR)
    assert flash.read(0, 1) == b"\xff"


def test_slot_directory_flips_atomically(fixture_pack: bytes, tiny_pack: bytes) -> None:
    store = FontStore(SimFlash(64 * MIB))
    assert store.slot_size == 24 * MIB and store.dir_offsets == (48 * MIB, 48 * MIB + SECTOR)
    first = store.install(fixture_pack)
    record = store.active()
    assert record is not None and (record.slot, record.seq, record.pack_id) == (0, 1, first)
    second = store.install(tiny_pack)
    record = store.active()
    assert record is not None and (record.slot, record.seq, record.pack_id) == (1, 2, second)
    pack = store.active_pack()
    assert isinstance(pack, FontPack) and pack.pack_id == second
    # Corrupt the newest directory record: the previous one stays valid (power loss mid-write).
    store.flash.erase(store.dir_offsets[1], SECTOR)
    record = store.active()
    assert record is not None and record.pack_id == first


def test_parse_fault_specs() -> None:
    faults = SimFaults()
    for spec in ("chunk-loss=0.25", "drop-chunks=1,2", "result-loss=0.1", "send-fail=0.05", "suspend-fail=0.2",
                 "resume-fail=2", "connect-fail=0.3", "power-loss=1A2B3C4D", "auth-fail=1A2B3C4D:3",
                 "refresh-timeout=00000005", "disconnect=1A2B3C4D@12"):
        parse_fault(spec, faults)
    assert faults.mesh.chunk_loss == 0.25 and faults.mesh.drop_chunks == {1, 2}
    assert faults.bridge.suspend_fail == 0.2 and faults.bridge.resume_fail_next == 2
    assert faults.air.connect_fail == 0.3
    tag = faults.tags[0x1A2B3C4D]
    assert (tag.power_loss, tag.auth_fail, tag.disconnect_after_records) == (1, 3, 12)
    assert faults.tags[5].refresh_timeout == 1
    for bad in ("chunk-loss=2", "nonsense=1", "power-loss=xyz", "disconnect=12@x"):
        with pytest.raises(FaultSpecError):
            parse_fault(bad, SimFaults())


def test_seeded_streams_are_reproducible() -> None:
    assert TagSpec.generate(9, 0) == TagSpec.generate(9, 0)
    assert TagSpec.generate(9, 0).tag_id != TagSpec.generate(9, 1).tag_id
    assert rng_stream(1, "a").random() == rng_stream(1, "a").random() != rng_stream(2, "a").random()


def test_tag_nvs_json_round_trip() -> None:
    nvs = TagNvs(DisplayRecord(5, 2, 9, 77, bytes(range(32)), Status.DISPLAY_STATE_UNKNOWN,
                               StoredState.REFRESH_INTENT), 2)
    assert TagNvs.from_json(nvs.to_json()) == nvs
    assert TagNvs.from_json(TagNvs().to_json()) == TagNvs()


def test_sim_clock_scales_time() -> None:
    clock = SimClock(1000.0)
    assert clock.real_s(1000.0) == 0.001

    async def scenario() -> float:
        start = clock.now_ms()
        await clock.sleep_ms(5000.0)
        return clock.now_ms() - start

    assert asyncio.run(scenario()) >= 5000.0


def test_device_endpoint_framing_rules() -> None:
    """Raw socket: no answer before HELLO, UNSUPPORTED types, CRC errors counted."""

    async def handler(msg: SerialMsg, fields: dict[str, object]) -> dict[str, object]:
        if msg == SerialMsg.HELLO:
            return {"status": Status.OK, "proto": 1, "fw": "x", "build": "y", "boot_id": 1, "caps": {}}
        return {"status": Status.OK}

    async def scenario() -> None:
        endpoint = DeviceEndpoint("t", handler, supported=[SerialMsg.PING])
        await endpoint.start()
        try:
            reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
            frames = FrameReader()

            async def next_frame() -> Frame:
                while True:
                    got = frames.feed(await asyncio.wait_for(reader.read(4096), 5))
                    if got:
                        return got[0]

            writer.write(frame_to_wire(Frame(SerialMsg.PING, 1)))  # before HELLO: ignored
            writer.write(frame_to_wire(Frame(SerialMsg.HELLO, 2, cbor_msgs.encode_request(
                SerialMsg.HELLO, {"proto": 1, "name": "t"}))))
            hello = await next_frame()
            assert (hello.type, hello.request_id, hello.flags) == (SerialMsg.HELLO, 2, SerialFlag.RESPONSE)
            writer.write(frame_to_wire(Frame(0x7E, 3)))  # unknown type
            unknown = await next_frame()
            assert cbor_msgs.decode_map(unknown.payload)["status"] == Status.UNSUPPORTED
            assert unknown.credits == 1  # the buffer is granted back on the answer
            bad = bytearray(encode_frame(Frame(SerialMsg.PING, 4)))
            bad[-1] ^= 0xFF
            from cremind_tag.protocol import cobs

            writer.write(cobs.encode(bytes(bad)) + b"\x00")
            writer.write(frame_to_wire(Frame(SerialMsg.PING, 5)))
            ping = await next_frame()
            assert ping.request_id == 5
            assert endpoint.counters["crc_errors"] == 1 and endpoint.counters["overruns"] == 1
            writer.close()
        finally:
            await endpoint.stop()

    asyncio.run(scenario())


def test_simulator_thread(tiny_pack: bytes) -> None:
    from cremind_tag.gateway import GatewayClient
    from cremind_tag.sim import SimulatorThread
    from cremind_tag.sim.harness import make_config

    with SimulatorThread(make_config(fontpack=tiny_pack, seed=61)) as thread:
        host, port = thread.gateway_url.removeprefix("socket://").split(":")
        with socket.create_connection((host, int(port)), timeout=5):
            pass
        assert thread.call(lambda sim: len(sim.bridges)) == 1

        async def scenario() -> int:
            async with GatewayClient(thread.gateway_url, reconnect=False) as client:
                return len(await client.list_nodes())

        assert asyncio.run(scenario()) == 1


def test_unused_imports_are_messages() -> None:
    assert MeshAssignSet.LEN == 25 and MeshDeliveryResult.LEN > 0
