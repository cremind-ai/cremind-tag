"""CBOR payload rules (§1.1) and per-message schemas."""

from __future__ import annotations

import re
from typing import Any

import cbor2
import pytest

from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.cbor_msgs import CborError, decode_map, encode_map
from cremind_tag.protocol.ids import CborKey, SerialMsg


def test_every_cbor_key_has_a_kind() -> None:
    assert set(cbor_msgs.KEYS) == {k.name.lower() for k in CborKey}


def test_canonical_encoding() -> None:
    payload = encode_map({"tag_id": 0x1A2B3C4D, "status": 0, "relay": True, "uuid": bytes(16)})
    assert payload.hex() == "a4" "00" "00" "03" "50" + "00" * 16 + "04" "1a1a2b3c4d" "0f" "f5"
    assert encode_map({}) == b"" and decode_map(b"") == {}


def test_nested_maps_and_counters_round_trip() -> None:
    fields = {"status": 0, "caps": {"max_frame": 4096, "role": 1},
              "nodes": [{"addr": 2, "name": "desk", "configured": True}],
              "counters": {"crc_errors": 1}, "rssi": -61}
    assert decode_map(encode_map(fields)) == fields


def test_unknown_keys_are_ignored() -> None:
    assert decode_map(cbor2.dumps({0: 1, 999: "future"})) == {"status": 1}


@pytest.mark.parametrize("payload,reason", [
    ("a1001801", "non-shortest integer"),
    ("a200000000", "duplicate key"),
    ("bf0000ff", "indefinite map"),
    ("a100f93c00", "float"),
    ("a100c100", "tag"),
    ("a100f6", "null"),
    ("a1616100", "text key at top level"),
    ("a10000ff", "trailing byte"),
    ("a1005a00000001", "truncated"),
    ("a10078", "truncated head"),
    ("a1044100", "wrong kind (bstr for uint)"),
    ("a10320", "wrong kind (int for bstr)"),
    ("a1034100", "wrong bstr size"),
    ("a1001b0000000100000000", "uint32 out of range"),
    ("a10f01", "int for bool"),
    ("80", "not a map"),
])
def test_strict_decoding(payload: str, reason: str) -> None:
    with pytest.raises(CborError):
        decode_map(bytes.fromhex(payload))


def test_encode_rejects_bad_fields() -> None:
    with pytest.raises(CborError):
        encode_map({"no_such_field": 1})
    with pytest.raises(CborError):
        encode_map({"relay": 1})
    with pytest.raises(CborError):
        encode_map({"counters": {"x": -1}})


def test_message_schemas() -> None:
    payload = cbor_msgs.encode_request(SerialMsg.SCAN_UNPROV, {"duration_s": 10})
    assert cbor_msgs.decode_request(SerialMsg.SCAN_UNPROV, payload) == {"duration_s": 10}
    with pytest.raises(CborError, match="missing"):
        cbor_msgs.encode_request(SerialMsg.ASSIGN_TAG, {"op_id": 1})
    with pytest.raises(CborError, match="unexpected"):
        cbor_msgs.encode_request(SerialMsg.PING, {"seq": 1})
    with pytest.raises(CborError, match="missing"):
        cbor_msgs.decode_response(SerialMsg.PING, encode_map({"uptime_s": 1}))
    with pytest.raises(CborError, match="no event form"):
        cbor_msgs.encode_event(SerialMsg.PING, {})
    assert cbor_msgs.encode_response(SerialMsg.REBOOT, {"status": 2, "detail": 2})


def _identifiers(group: str) -> set[str]:
    while re.search(r"\{[^{}]*\}|\[[^\[\]]*\]", group[1:-1]):
        group = group[0] + re.sub(r"\{[^{}]*\}|\[[^\[\]]*\]", "", group[1:-1]) + group[-1]
    return set(re.findall(r"\b[a-z_][a-z0-9_]*\b", group))


def _first_group(text: str) -> str:
    start = text.index("{")
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start : i + 1]
    raise AssertionError(text)


def test_schemas_follow_the_spec_docs(codegen: Any) -> None:
    """Field lists here must match the {...} groups in spec serial.message_types."""
    types = codegen.load_spec(codegen.spec_text())["serial"]["message_types"]
    for name, entry in types.items():
        msg, doc = SerialMsg[name], entry["doc"]
        if entry["dir"] == "d2h":
            assert _identifiers(_first_group(doc)) == {f.rstrip("?") for f in cbor_msgs.EVENTS[msg]}, name
            continue
        request, _, response = doc.partition("->")
        assert _identifiers(_first_group(request)) == {f.rstrip("?") for f in cbor_msgs.REQUESTS[msg]}, name
        answer = _identifiers(_first_group(response)) if "{" in response.split(";")[0] else {"status"}
        assert answer == {"status"} | set(cbor_msgs.RESPONSES.get(msg, ())), name
