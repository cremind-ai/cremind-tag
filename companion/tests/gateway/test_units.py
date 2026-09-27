"""Op ids, typed events and typed responses (no device)."""

from __future__ import annotations

import cbor2
import pytest

from cremind_tag.gateway import Ack, OpIdGenerator, ResultEvent, SessionStarted, StageEvent, UnknownEvent
from cremind_tag.gateway.events import parse_event
from cremind_tag.gateway.results import BridgeInfo, FlashTestResult, HelloInfo, to_status
from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import (
    RESULT_FLAG_DUPLICATE,
    RESULT_FLAG_ESCALATED,
    CborKey,
    DeliveryStage,
    SerialMsg,
    Status,
)


def test_op_ids_are_monotonic_and_time_based() -> None:
    now = [1_700_000_000_000]
    gen = OpIdGenerator(clock_ms=lambda: now[0], random_bits=lambda bits: (1 << bits) - 1)
    first = gen.next()
    assert first >> 20 == now[0]
    second = gen.next()  # same millisecond, same random part: forced upwards
    assert second == first + 1
    now[0] -= 5_000  # the clock stepped back: still increasing
    assert gen.next() > second
    ids = [OpIdGenerator().next() for _ in range(3)]
    assert all(0 < i < 1 << 64 for i in ids)


def test_result_event_parsing() -> None:
    fields = {"seq": 4, "update_id": 501, "bridge": 2, "tag_id": 0x1A2B3C4D, "epoch": 3, "revision": 18,
              "status": 23, "digest": bytes(8), "battery_mv": 2900,
              "timing": {"wake_ms": 1, "mesh_ms": 2, "transfer_ms": 3, "refresh_ms": 4, "suspend_ms": 5},
              "flags": 0, "stored_epoch": 3}
    event = parse_event(SerialMsg.EVT_RESULT, cbor_msgs.encode_event(SerialMsg.EVT_RESULT, fields), 99)
    assert isinstance(event, ResultEvent) and event.retained and event.seq == 4 and event.boot_id == 99
    assert event.status == Status.DISPLAY_STATE_UNKNOWN and not event.displayed
    assert event.timing.as_dict() == fields["timing"]
    assert event.stored_epoch == 3 and event.flags == 0 and not event.duplicate and not event.escalated


def test_result_event_carries_the_tag_report() -> None:
    """§3.4: flags (bit0 duplicate stored ACK, bit1 escalated) and the tag's stored_epoch (CBOR keys 32, 62)."""
    fields = {"seq": 5, "update_id": 502, "bridge": 2, "tag_id": 7, "epoch": 3, "revision": 0,
              "status": Status.STALE_EPOCH, "digest": bytes(8), "battery_mv": 0,
              "timing": {"wake_ms": 0, "mesh_ms": 0, "transfer_ms": 0, "refresh_ms": 0, "suspend_ms": 0},
              "flags": RESULT_FLAG_ESCALATED, "stored_epoch": 9}
    payload = cbor_msgs.encode_event(SerialMsg.EVT_RESULT, fields)
    wire = cbor2.loads(payload)
    assert wire[CborKey.FLAGS] == RESULT_FLAG_ESCALATED and wire[CborKey.STORED_EPOCH] == 9
    event = parse_event(SerialMsg.EVT_RESULT, payload, 1)
    assert isinstance(event, ResultEvent) and event.escalated and not event.duplicate and event.stored_epoch == 9
    dup = parse_event(SerialMsg.EVT_RESULT, cbor_msgs.encode_event(SerialMsg.EVT_RESULT, {
        **fields, "status": Status.OK, "flags": RESULT_FLAG_DUPLICATE}), 1)
    assert isinstance(dup, ResultEvent) and dup.duplicate and dup.displayed
    missing = {k: v for k, v in fields.items() if k != "stored_epoch"}
    with pytest.raises(cbor_msgs.CborError):  # both keys are required in protocol v1
        parse_event(SerialMsg.EVT_RESULT, cbor_msgs.encode_map(missing), 1)


def test_stage_and_unknown_events() -> None:
    stage = parse_event(SerialMsg.EVT_STAGE, cbor_msgs.encode_event(SerialMsg.EVT_STAGE, {
        "update_id": 1, "tag_id": 2, "revision": 3, "stage": DeliveryStage.REFRESHING}), 1)
    assert isinstance(stage, StageEvent) and not stage.retained and stage.stage == DeliveryStage.REFRESHING
    unknown = parse_event(0x9F, cbor_msgs.encode_map({"text": "future"}), 1)
    assert isinstance(unknown, UnknownEvent) and unknown.type_code == 0x9F
    with pytest.raises(cbor_msgs.CborError):
        parse_event(SerialMsg.EVT_RESULT, cbor_msgs.encode_map({"seq": 1}), 1)


def test_session_started() -> None:
    hello = HelloInfo.from_fields({"proto": 1, "fw": "0.1.0", "build": "x", "boot_id": 7, "caps": {"credits": 4}})
    assert not SessionStarted(hello=hello, previous_boot_id=None).boot_changed
    assert not SessionStarted(hello=hello, previous_boot_id=7).boot_changed
    assert SessionStarted(hello=hello, previous_boot_id=8).boot_changed


def test_ack_semantics() -> None:
    accepted = Ack.from_fields(SerialMsg.DELIVER_LAYOUT, {"status": 1}, 5)
    assert accepted.ok and not accepted.duplicate and accepted.op_id == 5
    duplicate = Ack.from_fields(SerialMsg.DELIVER_LAYOUT, {"status": 1, "detail": 2})
    assert duplicate.ok and duplicate.duplicate
    legacy = Ack.from_fields(SerialMsg.DELIVER_LAYOUT, {"status": 2})  # §1.5 wording: status DUPLICATE
    assert legacy.ok and legacy.duplicate
    busy = Ack.from_fields(SerialMsg.DELIVER_LAYOUT, {"status": 3})
    assert busy.busy and not busy.ok
    with pytest.raises(Exception, match="BUSY"):
        busy.raise_for_status()
    ok = Ack.from_fields(SerialMsg.PING, {"status": 0})
    assert ok.status is Status.OK and ok.ok  # Status.OK is falsy: never test it for truth
    assert to_status(200) == 200


def test_a_layout_too_large_for_one_serial_frame_is_refused_locally() -> None:
    import asyncio

    from cremind_tag.gateway import FrameTooLargeError, GatewayClient
    from cremind_tag.protocol.ids import LAYOUT_HARD_MAX

    async def scenario() -> None:
        client = GatewayClient("socket://127.0.0.1:9", reconnect=False)  # never connected
        with pytest.raises(FrameTooLargeError):
            await client.deliver_layout(bridge=2, tag_id=1, epoch=1, revision=1, update_id=1,
                                        fontpack_id=bytes(8), layout=bytes(LAYOUT_HARD_MAX))
        with pytest.raises(ValueError):
            await client.deliver_layout(bridge=2, tag_id=1, epoch=1, revision=1, update_id=1,
                                        fontpack_id=bytes(8), layout=bytes(LAYOUT_HARD_MAX + 1))
        await client.close()

    asyncio.run(scenario())


def test_bridge_info_and_flash_results() -> None:
    info = BridgeInfo.from_map({"addr": 2, "fw": "0.1.0", "fontpack_id": bytes(8), "caps": {"board": 3},
                                "assigned": [{"tag_id": 5, "epoch": 2}], "counters": {"sessions_ok": 1}})
    assert info.assigned[0].tag_id == 5 and info.caps.board == 3
    result = FlashTestResult.from_fields({"status": 0, "flash_size": 1, "items": [{"offset": 0, "status": 0}]})
    assert result.ok
    failing = FlashTestResult.from_fields({"status": 0, "items": [{"offset": 0, "status": 22}]})
    assert not failing.ok
