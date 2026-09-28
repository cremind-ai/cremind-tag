"""CBOR payloads of serial frames (docs/protocol.md §1.1–§1.2).

A payload is one CBOR map keyed by ``CborKey`` integers, or empty (a request
without arguments). This API uses the lower-case field names of
``serial.cbor_keys``; nested maps (``caps``, ``timing`` and the entries of
``nodes``, ``items``, ``assigned``) use the same keys, while ``counters`` maps
text names to unsigned integers.

Encoding is canonical (RFC 7049 §3.9): definite lengths, shortest integers and
lengths, map keys sorted by their encoding (shorter first, so integer keys
ascend), no tags, no floats; an empty field set encodes as an empty payload.
Decoding enforces the same well-formedness in any key order, rejects duplicate
keys, checks the value kind of every known key and ignores unknown keys
(forward compatibility).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

import cbor2

from .ids import GRANT_MAX, GRANT_SIG_LEN, LAYOUT_HARD_MAX, SERIAL_MAX_PAYLOAD, CborKey, SerialMsg

MAX_DEPTH = 8


class CborError(ValueError):
    """Payload is not a well-formed, well-typed map per §1.1."""


class Kind(Enum):
    UINT = auto()
    INT = auto()
    BOOL = auto()
    BSTR = auto()
    TSTR = auto()
    MAP = auto()  # nested map with CborKey keys
    MAPS = auto()  # array of nested maps with CborKey keys
    COUNTERS = auto()  # map text -> uint


@dataclass(frozen=True, slots=True)
class KeySpec:
    kind: Kind
    bits: int = 32
    size: int | None = None  # exact byte length
    max_size: int | None = None


_U = KeySpec(Kind.UINT)
_U16 = KeySpec(Kind.UINT, 16)
_U64 = KeySpec(Kind.UINT, 64)
_T = KeySpec(Kind.TSTR)

# Value kinds from the comments of spec serial.cbor_keys; "uint" without a
# width is taken as uint32.
KEYS: dict[str, KeySpec] = {
    "status": _U, "op_id": _U64, "addr": _U16, "uuid": KeySpec(Kind.BSTR, size=16),
    "tag_id": _U, "epoch": _U, "revision": _U, "update_id": _U64,
    "fontpack_id": KeySpec(Kind.BSTR, size=8), "layout": KeySpec(Kind.BSTR, max_size=LAYOUT_HARD_MAX),
    "stage": _U, "detail": _U, "rssi": KeySpec(Kind.INT), "oob": _U, "duration_s": _U,
    "relay": KeySpec(Kind.BOOL), "ttl": _U, "key": KeySpec(Kind.BSTR, size=16), "fw": _T,
    "build": _T, "boot_id": _U, "caps": KeySpec(Kind.MAP), "nodes": KeySpec(Kind.MAPS),
    "elements": _U, "name": _T, "counters": KeySpec(Kind.COUNTERS), "battery_mv": _U,
    "timing": KeySpec(Kind.MAP), "digest": KeySpec(Kind.BSTR, max_size=32), "proto": _U,
    "max_frame": _U, "credits": _U, "flags": _U, "bridge": _U16, "items": KeySpec(Kind.MAPS),
    "cmd": _U, "seq": _U, "time_ms": _U, "text": _T, "size": _U, "offset": _U,
    "data": KeySpec(Kind.BSTR, max_size=SERIAL_MAX_PAYLOAD), "role": _U,
    "configured": KeySpec(Kind.BOOL), "last_seen_s": _U, "board": _U, "panel": _U,
    "flash_size": _U, "slot": _U, "assigned": KeySpec(Kind.MAPS), "uptime_s": _U,
    "wake_ms": _U, "mesh_ms": _U, "transfer_ms": _U, "refresh_ms": _U, "suspend_ms": _U,
    "queue_depth": _U, "max_bridges": _U, "max_tags": _U,
    "uuid_filter": KeySpec(Kind.BSTR, max_size=16), "net_idx": _U16, "app_idx": _U16,
    "stored_epoch": _U, "assigned_count": _U,
    # protocol v2 (docs/connect-setup.md 5.3)
    "device_id": KeySpec(Kind.BSTR, size=16), "ik": KeySpec(Kind.BSTR, size=32), "owner_state": _U,
    "gen": _U, "authority_id": KeySpec(Kind.BSTR, size=16), "challenge": KeySpec(Kind.BSTR, size=16),
    "grant": KeySpec(Kind.BSTR, max_size=GRANT_MAX), "sig": KeySpec(Kind.BSTR, size=GRANT_SIG_LEN),
    "static_oob": KeySpec(Kind.BSTR, size=32), "tunnel": _U16, "state": _U,
    "proof": KeySpec(Kind.BSTR, size=16), "owner": KeySpec(Kind.BSTR, size=16),
    "controller_match": KeySpec(Kind.BOOL), "root_proof": KeySpec(Kind.BSTR, size=16),
    "release_stage": _U, "op_key": KeySpec(Kind.BSTR, size=32),
}

# Message fields from the docs of spec serial.message_types; "?" marks an
# optional field. Response fields other than status are present on success.
REQUESTS: dict[SerialMsg, tuple[str, ...]] = {
    SerialMsg.HELLO: ("proto", "name"),
    SerialMsg.PING: (),
    SerialMsg.REBOOT: ("op_id",),
    SerialMsg.EVENT_ACK: ("seq",),
    SerialMsg.INFO: (),
    SerialMsg.SCAN_UNPROV: ("duration_s", "uuid_filter?"),
    SerialMsg.PROVISION: ("op_id", "uuid", "name?", "static_oob?"),
    SerialMsg.CONFIGURE_NODE: ("op_id", "addr", "relay", "ttl"),
    SerialMsg.REMOVE_NODE: ("op_id", "addr"),
    SerialMsg.LIST_NODES: (),
    SerialMsg.ASSIGN_TAG: ("op_id", "bridge", "tag_id", "epoch", "key"),
    SerialMsg.UNASSIGN_TAG: ("op_id", "bridge", "tag_id", "epoch"),
    SerialMsg.DELIVER_LAYOUT: ("op_id", "bridge", "tag_id", "epoch", "revision", "update_id",
                               "fontpack_id", "layout"),
    SerialMsg.CANCEL_DELIVERY: ("op_id", "update_id"),
    SerialMsg.TAG_COMMAND: ("op_id", "bridge", "tag_id", "epoch", "cmd"),
    SerialMsg.GET_INVENTORY: (),
    SerialMsg.GET_COUNTERS: (),
    SerialMsg.IDENTIFY_NODE: ("op_id", "addr"),
    SerialMsg.FONT_BEGIN: ("size", "digest", "fontpack_id"),
    SerialMsg.FONT_DATA: ("offset", "data"),
    SerialMsg.FONT_COMMIT: (),
    SerialMsg.FONT_STATUS: (),
    SerialMsg.FONT_ABORT: (),
    SerialMsg.FLASH_TEST: ("op_id",),
    # protocol v2 (docs/connect-setup.md 5)
    SerialMsg.IDENTIFY: (),
    SerialMsg.SECURE_OPEN: ("data",),
    SerialMsg.SECURE_DATA: ("data",),
    SerialMsg.CLAIM: ("grant", "sig"),
    SerialMsg.RECOVER: ("grant", "sig"),
    SerialMsg.RELEASE: ("grant", "sig", "release_stage?"),
    SerialMsg.STATUS: (),
    SerialMsg.PAIR: ("grant", "sig", "proof", "op_key"),
    SerialMsg.REKEY: ("grant", "sig", "op_key"),
    SerialMsg.MAINT_AUTH: ("proof",),
    SerialMsg.DISCOVER: ("op_id", "bridge", "duration_s", "tag_id"),
    SerialMsg.RECOMMISSION: ("grant?", "sig?"),
    SerialMsg.TUNNEL_OPEN: ("op_id", "bridge", "tag_id", "duration_s"),
    SerialMsg.TUNNEL_SEND: ("tunnel", "data"),
    SerialMsg.TUNNEL_CLOSE: ("tunnel",),
    SerialMsg.FACTORY_SETUP: ("data",),
}
RESPONSES: dict[SerialMsg, tuple[str, ...]] = {
    SerialMsg.HELLO: ("proto", "fw", "build", "boot_id", "caps"),
    SerialMsg.PING: ("uptime_s",),
    SerialMsg.INFO: ("fw", "build", "boot_id", "caps", "counters"),
    SerialMsg.LIST_NODES: ("nodes",),
    SerialMsg.GET_INVENTORY: ("items",),
    SerialMsg.GET_COUNTERS: ("counters",),
    SerialMsg.FONT_BEGIN: ("slot", "flash_size"),
    SerialMsg.FONT_COMMIT: ("fontpack_id",),
    SerialMsg.FONT_STATUS: ("fontpack_id", "slot", "size", "flash_size"),
    SerialMsg.FLASH_TEST: ("flash_size", "items"),
    SerialMsg.IDENTIFY: ("proto", "role", "device_id", "ik", "fw", "build", "board", "owner_state", "gen",
                         "authority_id", "challenge"),
    SerialMsg.SECURE_OPEN: ("data",),
    SerialMsg.SECURE_DATA: ("data",),
    SerialMsg.CLAIM: ("gen",),
    SerialMsg.RECOVER: ("gen",),
    SerialMsg.RELEASE: ("gen", "data"),
    SerialMsg.STATUS: ("owner_state", "gen", "authority_id", "owner", "controller_match", "challenge",
                       "root_proof"),
    SerialMsg.PAIR: ("gen", "proof"),
    SerialMsg.REKEY: ("gen",),
    SerialMsg.RECOMMISSION: ("gen", "data"),
    SerialMsg.TUNNEL_OPEN: ("tunnel",),
}
# Every response carries status and may carry detail (§1.4) and text.
RESPONSE_COMMON = ("status", "detail?", "text?")
EVENTS: dict[SerialMsg, tuple[str, ...]] = {
    SerialMsg.EVT_LOG: ("text",),
    SerialMsg.EVT_UNPROV_BEACON: ("uuid", "rssi", "oob"),
    SerialMsg.EVT_PROVISIONED: ("seq", "op_id", "uuid", "addr", "elements", "status"),
    SerialMsg.EVT_NODE_CONFIGURED: ("seq", "op_id", "addr", "status"),
    SerialMsg.EVT_NODE_REMOVED: ("seq", "op_id", "addr", "status"),
    SerialMsg.EVT_ASSIGN_RESULT: ("seq", "op_id", "bridge", "tag_id", "epoch", "status"),
    SerialMsg.EVT_STAGE: ("update_id", "tag_id", "revision", "stage"),
    SerialMsg.EVT_RESULT: ("seq", "update_id", "bridge", "tag_id", "epoch", "revision", "status",
                           "digest", "battery_mv", "timing", "flags", "stored_epoch"),
    SerialMsg.EVT_BRIDGE_INFO: ("addr", "fw", "fontpack_id", "caps", "assigned", "counters"),
    SerialMsg.EVT_TAG_SEEN: ("bridge", "tag_id", "rssi", "battery_mv", "flags"),
    SerialMsg.EVT_TUNNEL: ("tunnel", "bridge", "tag_id", "state", "data?", "status?"),
    SerialMsg.EVT_DISCOVERED: ("bridge", "tag_id", "rssi", "flags"),
}


# ---------------------------------------------------------------------------
# Well-formedness
# ---------------------------------------------------------------------------


def _head(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """Return (major, additional info, argument, next position) of an item head."""
    if pos >= len(data):
        raise CborError("truncated item")
    initial = data[pos]
    major, info = initial >> 5, initial & 0x1F
    if info < 24:
        return major, info, info, pos + 1
    if info == 31:
        raise CborError("indefinite length")
    if info > 27:
        raise CborError(f"reserved additional info {info}")
    if major == 7:
        raise CborError("floats and simple values other than false/true are not allowed")
    width = 1 << (info - 24)
    if pos + 1 + width > len(data):
        raise CborError("truncated head")
    arg = int.from_bytes(data[pos + 1 : pos + 1 + width], "big")
    if arg < (24 if width == 1 else 1 << (4 * width)):
        raise CborError("integer or length not in shortest form")
    return major, info, arg, pos + 1 + width


def _scan(data: bytes, pos: int, depth: int) -> int:
    if depth > MAX_DEPTH:
        raise CborError("nesting too deep")
    major, info, arg, pos = _head(data, pos)
    if major in (0, 1):
        return pos
    if major in (2, 3):
        end = pos + arg
        if end > len(data):
            raise CborError("truncated string")
        if major == 3:
            try:
                data[pos:end].decode("utf-8")
            except UnicodeDecodeError:
                raise CborError("text string is not UTF-8") from None
        return end
    if major == 4:
        for _ in range(arg):
            pos = _scan(data, pos, depth + 1)
        return pos
    if major == 5:
        keys: set[bytes] = set()
        for _ in range(arg):
            start = pos
            pos = _scan(data, pos, depth + 1)
            if data[start:pos] in keys:
                raise CborError("duplicate map key")
            keys.add(data[start:pos])
            pos = _scan(data, pos, depth + 1)
        return pos
    if major == 6:
        raise CborError("tags are not allowed")
    if info in (20, 21):
        return pos
    raise CborError("floats and simple values other than false/true are not allowed")


def check_well_formed(data: bytes) -> None:
    """Raise CborError unless ``data`` is exactly one item following §1.1."""
    if _scan(data, 0, 0) != len(data):
        raise CborError("trailing bytes after the item")


# ---------------------------------------------------------------------------
# Name <-> key conversion with value checks
# ---------------------------------------------------------------------------


def _check_value(name: str, value: Any, spec: KeySpec) -> None:
    kind = spec.kind
    if kind in (Kind.UINT, Kind.INT):
        if not isinstance(value, int) or isinstance(value, bool):
            raise CborError(f"{name}: expected an integer")
        lo, hi = (0, 1 << spec.bits) if kind is Kind.UINT else (-(1 << (spec.bits - 1)), 1 << (spec.bits - 1))
        if not lo <= value < hi:
            raise CborError(f"{name}: {value} out of range")
    elif kind is Kind.BOOL:
        if not isinstance(value, bool):
            raise CborError(f"{name}: expected a bool")
    elif kind is Kind.BSTR:
        if not isinstance(value, bytes | bytearray):
            raise CborError(f"{name}: expected a byte string")
        if spec.size is not None and len(value) != spec.size:
            raise CborError(f"{name}: expected {spec.size} bytes")
        if spec.max_size is not None and len(value) > spec.max_size:
            raise CborError(f"{name}: more than {spec.max_size} bytes")
    elif kind is Kind.TSTR:
        if not isinstance(value, str):
            raise CborError(f"{name}: expected a text string")
    elif kind is Kind.COUNTERS:
        if not isinstance(value, Mapping) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1 << 32
            for k, v in value.items()
        ):
            raise CborError(f"{name}: expected a map of text -> uint32")


def _to_wire(fields: Mapping[str, Any]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for name, value in fields.items():
        try:
            key = CborKey[name.upper()]
        except KeyError:
            raise CborError(f"unknown field {name!r}") from None
        spec = KEYS[name]
        if spec.kind is Kind.MAP:
            if not isinstance(value, Mapping):
                raise CborError(f"{name}: expected a map")
            value = _to_wire(value)
        elif spec.kind is Kind.MAPS:
            if not isinstance(value, list | tuple) or not all(isinstance(v, Mapping) for v in value):
                raise CborError(f"{name}: expected an array of maps")
            value = [_to_wire(v) for v in value]
        else:
            _check_value(name, value, spec)
            value = bytes(value) if spec.kind is Kind.BSTR else value
        out[int(key)] = value
    return out


def _from_wire(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise CborError("expected a map")
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, int) or isinstance(key, bool) or key < 0:
            raise CborError("map keys must be unsigned integers")
        try:
            name = CborKey(key).name.lower()
        except ValueError:
            continue  # unknown key: ignored (§1.1)
        spec = KEYS[name]
        if spec.kind is Kind.MAP:
            value = _from_wire(value)
        elif spec.kind is Kind.MAPS:
            if not isinstance(value, list):
                raise CborError(f"{name}: expected an array")
            value = [_from_wire(v) for v in value]
        else:
            _check_value(name, value, spec)
        out[name] = value
    return out


def encode_map(fields: Mapping[str, Any]) -> bytes:
    """Canonical payload for ``fields`` (b"" when empty)."""
    if not fields:
        return b""
    return cbor2.dumps(_to_wire(fields), canonical=True)


def decode_map(payload: bytes) -> dict[str, Any]:
    """Strictly decode a payload into named fields (b"" -> {})."""
    if not payload:
        return {}
    check_well_formed(payload)
    return _from_wire(cbor2.loads(payload))


# ---------------------------------------------------------------------------
# Per-message helpers
# ---------------------------------------------------------------------------


def _split(schema: tuple[str, ...]) -> tuple[set[str], set[str]]:
    required = {f for f in schema if not f.endswith("?")}
    return required, required | {f.rstrip("?") for f in schema}


def _response_schema(msg: SerialMsg) -> tuple[str, ...]:
    return RESPONSE_COMMON + tuple(f"{f.rstrip('?')}?" for f in RESPONSES.get(msg, ()))


def _schema(kind: str, msg: SerialMsg) -> tuple[str, ...]:
    table = {"request": REQUESTS, "event": EVENTS}.get(kind)
    if table is None:
        return _response_schema(msg)
    if msg not in table:
        raise CborError(f"{msg.name} has no {kind} form")
    return table[msg]


def _encode(kind: str, msg: SerialMsg, fields: Mapping[str, Any]) -> bytes:
    required, allowed = _split(_schema(kind, msg))
    if missing := required - fields.keys():
        raise CborError(f"{msg.name} {kind}: missing {sorted(missing)}")
    if extra := fields.keys() - allowed:
        raise CborError(f"{msg.name} {kind}: unexpected {sorted(extra)}")
    return encode_map(fields)


def _decode(kind: str, msg: SerialMsg, payload: bytes) -> dict[str, Any]:
    fields = decode_map(payload)
    required, _ = _split(_schema(kind, msg))
    if missing := required - fields.keys():
        raise CborError(f"{msg.name} {kind}: missing {sorted(missing)}")
    return fields


def encode_request(msg: SerialMsg, fields: Mapping[str, Any]) -> bytes:
    return _encode("request", msg, fields)


def encode_response(msg: SerialMsg, fields: Mapping[str, Any]) -> bytes:
    return _encode("response", msg, fields)


def encode_event(msg: SerialMsg, fields: Mapping[str, Any]) -> bytes:
    return _encode("event", msg, fields)


def decode_request(msg: SerialMsg, payload: bytes) -> dict[str, Any]:
    return _decode("request", msg, payload)


def decode_response(msg: SerialMsg, payload: bytes) -> dict[str, Any]:
    return _decode("response", msg, payload)


def decode_event(msg: SerialMsg, payload: bytes) -> dict[str, Any]:
    return _decode("event", msg, payload)
