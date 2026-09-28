#!/usr/bin/env python3
"""Generate the firmware's protocol bindings from protocol/spec.yaml.

Outputs (deterministic, LF line endings, DO-NOT-EDIT banner carrying the
SHA-256 of the LF-normalised spec):

- include/ctag/proto_ids.h    C99 identifiers and constants
- include/ctag/proto_msgs.h   C99 structs + little-endian pack/unpack

Host software generates its own bindings from the published contract
artifact (tools/contract.py), never from this checkout: Cremind's are
``app/tags/runtime/protocol/ids.py`` and ``msgs.py``.

Usage::

    uv run python tools/codegen.py          # regenerate
    uv run python tools/codegen.py --check  # CI: exit 1 if stale

Only PyYAML is required. Before generating, the spec is checked for internal
consistency (unique ids, id ranges, derived limits); a violation aborts.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "protocol" / "spec.yaml"
OUT_IDS_H = ROOT / "include" / "ctag" / "proto_ids.h"
OUT_MSGS_H = ROOT / "include" / "ctag" / "proto_msgs.h"


class SpecError(Exception):
    """The spec is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class HexInt(int):
    """An int written in hex in the spec; emitted in hex again with as many digits."""

    digits: int


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    value = loader.construct_yaml_int(node)
    literal = node.value.lower().lstrip("+-")
    if not literal.startswith("0x"):
        return value
    hex_value = HexInt(value)
    hex_value.digits = len(literal) - 2
    return hex_value


class _SpecLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    # YAML keeps the LAST of two equal keys silently; a duplicated name in the
    # spec (a CBOR key, a message) would renumber the first one. Refuse it.
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise SpecError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    loader.flatten_mapping(node)
    return dict(loader.construct_pairs(node, deep=True))


_SpecLoader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_SpecLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_spec(text: str) -> dict[str, Any]:
    spec = yaml.load(text, Loader=_SpecLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(spec, dict):
        raise SpecError("spec root must be a mapping")
    return spec


def spec_text() -> str:
    return SPEC_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

INT_TYPES: dict[str, tuple[int, bool]] = {
    "u8": (1, False), "u16": (2, False), "u32": (4, False), "u64": (8, False),
    "i8": (1, True), "i16": (2, True), "i32": (4, True),
}
_FIXED_BYTES = re.compile(r"bytes\[(\d+)\]$")
_VARIABLE = re.compile(r"(glyph|str)\[(\w+)\]$")
_C_TYPES = {"u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
            "i8": "int8_t", "i16": "int16_t", "i32": "int32_t"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    doc: str
    max: str | None
    default: int | None = None

    @property
    def kind(self) -> str:
        if self.type in INT_TYPES:
            return "int"
        if _FIXED_BYTES.match(self.type):
            return "bytes_n"
        if self.type == "bytes":
            return "tail"
        if _VARIABLE.match(self.type):
            return "variable"
        raise SpecError(f"field {self.name}: unknown type {self.type!r}")

    @property
    def size(self) -> int:
        if self.kind == "int":
            return INT_TYPES[self.type][0]
        if self.kind == "bytes_n":
            match = _FIXED_BYTES.match(self.type)
            assert match
            return int(match.group(1))
        return 0


@dataclass(frozen=True)
class Message:
    title: str
    c_name: str
    type_ref: tuple[str, str] | None
    fields: tuple[Field, ...]
    variable: Field | None

    @property
    def fixed(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.kind != "tail")

    @property
    def tail(self) -> Field | None:
        return next((f for f in self.fields if f.kind == "tail"), None)

    @property
    def fixed_len(self) -> int:
        return sum(f.size for f in self.fixed)

    @property
    def len_macro(self) -> str:
        return f"CTAG_{self.c_name.upper()}_LEN"


def _fields(raw: list[dict[str, Any]], where: str) -> tuple[tuple[Field, ...], Field | None]:
    fields = [Field(f["name"], str(f["type"]), str(f.get("doc", "")), f.get("max"), f.get("default"))
              for f in raw]
    for i, field in enumerate(fields):
        if field.kind in ("tail", "variable") and i != len(fields) - 1:
            raise SpecError(f"{where}.{field.name}: a variable field must be last")
        if field.kind == "tail" and not field.max:
            raise SpecError(f"{where}.{field.name}: a trailing bytes field needs max: <constant>")
        if field.default is not None:
            if field.kind != "int" or not isinstance(field.default, int):
                raise SpecError(f"{where}.{field.name}: only integer fields take an integer default")
            if any(f.default is None for f in fields[i + 1 :]):
                raise SpecError(f"{where}.{field.name}: fields after a default need one too")
    if fields and fields[-1].kind == "variable":
        return tuple(fields[:-1]), fields[-1]
    return tuple(fields), None


def collect_messages(spec: dict[str, Any]) -> list[Message]:
    out: list[Message] = []

    def add(title: str, c_name: str, ref: tuple[str, str] | None, raw: list[dict[str, Any]]) -> None:
        fields, variable = _fields(raw or [], c_name)
        out.append(Message(title, c_name, ref, fields, variable))

    add("Serial frame header", "serial_header", None, spec["serial"]["header"])
    for name, op in spec["mesh"]["opcodes"].items():
        add(f"Mesh {name} parameters (opcode 0x{op['value']:02X}, handled by {op['model']})",
            f"mesh_{name.lower()}", ("MeshOp", name), op["fields"])
    add("Layout header", "layout_header", None, spec["layout"]["header"])
    for name, cmd in spec["layout"]["commands"].items():
        add(f"Layout command {name} (op 0x{cmd['value']:02X}), after the op byte",
            f"layout_cmd_{name.lower()}", ("LayoutCmd", name),
            cmd["fields"])
    add("One GLYPHS entry", "layout_glyph", None, spec["layout"]["glyph"])
    add("Tag CAPS characteristic value", "tag_caps", None, spec["gatt"]["tag_caps"])
    for name, msg in spec["gatt"]["ctrl_messages"].items():
        add(f"CTRL {name} (type 0x{msg['value']:02X}, {msg['dir']}), after the type byte",
            f"ctrl_{name.lower()}", ("CtrlMsg", name), msg["fields"])
    for name, msg in spec["gatt"]["record_types"].items():
        add(f"Record {name} plaintext (type 0x{msg['value']:02X}, {msg['dir']})",
            f"rec_{name.lower()}", ("RecordType", name), msg["fields"])
    for name, msg in spec["gatt"]["plain_messages"].items():
        add(f"STATUS plain {name} (type 0x{msg['value']:02X}, {msg['dir']}), after the type byte",
            f"plain_{name.lower()}", ("PlainMsg", name), msg["fields"])
    add("Enrollment blob (UICR.CUSTOMER)", "enrollment", None,
        spec["enrollment"]["fields"])
    v2 = spec["v2"]
    add("v2 IDENT characteristic value / tunnel OPEN data", "ident2", None, v2["ident2"])
    add("v2 enrollment blob (UICR.CUSTOMER[0..5])", "enrollment2", None, v2["enrollment2"])
    add("v2 identity copy (UICR.CUSTOMER at uicr_identity_offset)", "uicr_identity", None,
        v2["uicr_identity"])
    return out


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------


def _values(group: dict[str, Any], key: str = "value") -> dict[str, int]:
    return {name: (v[key] if isinstance(v, dict) else v) for name, v in group.items()}


def check_spec(spec: dict[str, Any], messages: list[Message]) -> None:
    errors: list[str] = []
    const = _values(spec["constants"])
    by_name = {m.c_name: m for m in messages}

    def unique(label: str, values: dict[str, int]) -> None:
        seen: dict[int, str] = {}
        for name, value in values.items():
            if value in seen:
                errors.append(f"{label}: {name} and {seen[value]} share value {value}")
            seen[value] = name

    for label, group, key in [
        ("status_codes", spec["status_codes"], "value"),
        ("delivery_stages", spec["delivery_stages"], "value"),
        ("delivery_outcomes", spec["delivery_outcomes"], "value"),
        ("serial.message_types", spec["serial"]["message_types"], "value"),
        ("serial.cbor_keys", spec["serial"]["cbor_keys"], "value"),
        ("tag_commands", spec["tag_commands"], "value"),
        ("node_roles", spec["node_roles"], "value"),
        ("mesh.models", spec["mesh"]["models"], "id"),
        ("mesh.opcodes", spec["mesh"]["opcodes"], "value"),
        ("layout.commands", spec["layout"]["commands"], "value"),
        ("boards", spec["boards"], "value"),
        ("panels", spec["panels"], "value"),
        ("gatt.characteristics", spec["gatt"]["characteristics"], "short"),
        ("v2.grant_ops", spec["v2"]["grant_ops"], "value"),
        ("v2.owner_states", spec["v2"]["owner_states"], "value"),
        ("v2.links", spec["v2"]["links"], "value"),
        ("v2.tunnel_states", spec["v2"]["tunnel_states"], "value"),
        ("v2.pair_kinds", spec["v2"]["pair_kinds"], "value"),
        ("v2.adv_flags", spec["v2"]["adv_flags"], "value"),
    ]:
        unique(label, _values(group, key))
    unique("gatt message types", {
        **_values(spec["gatt"]["ctrl_messages"]), **_values(spec["gatt"]["record_types"]),
        **_values(spec["gatt"]["plain_messages"]),
    })
    unique("icons", {i["name"]: i["id"] for i in spec["icons"]})

    def in_range(label: str, values: dict[str, int], lo: int, hi: int) -> None:
        for name, value in values.items():
            if not lo <= value <= hi:
                errors.append(f"{label}.{name} = {value:#x} outside {lo:#x}..{hi:#x}")

    in_range("mesh.opcodes", _values(spec["mesh"]["opcodes"]), 0x00, 0x3F)
    in_range("gatt.ctrl_messages", _values(spec["gatt"]["ctrl_messages"]), 0x01, 0x0F)
    in_range("gatt.record_types", _values(spec["gatt"]["record_types"]), 0x10, 0x2F)
    in_range("gatt.plain_messages", _values(spec["gatt"]["plain_messages"]), 0xC0, 0xCF)

    for name in [f.max for m in messages for f in m.fields if f.max]:
        if name not in const:
            errors.append(f"max: {name} is not a constant")

    def expect(label: str, actual: int, wanted: int) -> None:
        if actual != wanted:
            errors.append(f"{label} = {actual}, expected {wanted}")

    frame = const["SERIAL_MAX_FRAME"]
    expect("SERIAL_HEADER_LEN", const["SERIAL_HEADER_LEN"], by_name["serial_header"].fixed_len)
    expect("SERIAL_MAX_PAYLOAD", const["SERIAL_MAX_PAYLOAD"],
           frame - const["SERIAL_HEADER_LEN"] - const["SERIAL_CRC_LEN"])
    if const["SERIAL_MAX_ENCODED"] < frame + math.ceil(frame / 254) + 1:
        errors.append("SERIAL_MAX_ENCODED is below the COBS worst case")
    expect("LAYOUT_MAX_CHUNKS", const["LAYOUT_MAX_CHUNKS"],
           math.ceil(const["LAYOUT_HARD_MAX"] / const["LAYOUT_CHUNK_DATA_MAX"]))
    if const["LAYOUT_MAX_CHUNKS"] > 32:
        errors.append("LAYOUT_MAX_CHUNKS exceeds the 32-bit LAYOUT_STATUS.missing bitmap")
    expect("ATT_VALUE_MAX", const["ATT_VALUE_MAX"], const["ATT_MTU"] - 3)
    expect("FRAG_PAYLOAD_MAX", const["FRAG_PAYLOAD_MAX"], const["ATT_VALUE_MAX"] - 1)
    expect("TAG_PLANE_DATA_MAX", const["TAG_PLANE_DATA_MAX"],
           const["TAG_RECORD_PAYLOAD_MAX"] - by_name["rec_plane_data"].fixed_len)
    expect("TAG_RECORD_WIRE_MAX", const["TAG_RECORD_WIRE_MAX"],
           1 + 4 + const["TAG_RECORD_PAYLOAD_MAX"] + const["TAG_RECORD_MIC_LEN"])
    if const["TAG_RECORD_BUF"] < const["TAG_RECORD_WIRE_MAX"]:
        errors.append("TAG_RECORD_BUF is smaller than TAG_RECORD_WIRE_MAX")
    expect("SETUP_PAYLOAD_LEN", const["SETUP_PAYLOAD_LEN"], 1 + 4 + const["SETUP_SECRET_LEN"])
    expect("SETUP_CODE_LEN", const["SETUP_CODE_LEN"], math.ceil(const["SETUP_PAYLOAD_LEN"] * 8 / 5) + 1)
    if const["TUNNEL_MSG_MAX"] > const["SERIAL_MAX_PAYLOAD"] // 2:
        errors.append("TUNNEL_MSG_MAX must leave room in a serial frame")
    if const["PAIR_MSG_MAX"] > 2 * const["TAG_RECORD_BUF"]:
        errors.append("PAIR_MSG_MAX exceeds the tag's two record buffers")
    ident = by_name["ident2"]
    if 1 + ident.fixed_len > const["TUNNEL_MSG_MAX"]:
        errors.append("ident2 does not fit a tunnel message")
    offset = spec["v2"]["uicr_identity_offset"]
    if offset < by_name["enrollment2"].fixed_len or offset + by_name["uicr_identity"].fixed_len > 128:
        errors.append("uicr_identity_offset overlaps enrollment2 or leaves UICR.CUSTOMER")

    for m in messages:
        longest = m.fixed_len + (const[m.tail.max] if m.tail and m.tail.max else 0)
        if m.c_name.startswith("mesh_") and longest > const["MESH_MAX_VENDOR_PARAMS"]:
            errors.append(f"{m.c_name}: {longest} bytes exceed MESH_MAX_VENDOR_PARAMS")
        if m.c_name.startswith("ctrl_") and 1 + longest > const["TAG_CTRL_MSG_MAX"]:
            errors.append(f"{m.c_name}: {1 + longest} bytes exceed TAG_CTRL_MSG_MAX")
        if m.c_name.startswith("rec_") and longest > const["TAG_RECORD_PAYLOAD_MAX"]:
            errors.append(f"{m.c_name}: {longest} bytes exceed TAG_RECORD_PAYLOAD_MAX")
        if m.len_macro[len("CTAG_"):] in const and const[m.len_macro[len("CTAG_"):]] != m.fixed_len:
            errors.append(f"{m.len_macro} collides with a constant of a different value")
    if errors:
        raise SpecError("spec consistency:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

_ASCII = str.maketrans({"—": "-", "–": "-", "×": "x", "≤": "<=", "≥": ">=", "→": "->",
                        "·": ".", "‖": "|", "’": "'"})


def c_comment(text: str) -> str:
    text = text.translate(_ASCII).replace("*/", "* /")
    return text.encode("ascii", "replace").decode("ascii")


def fmt_int(value: int, *, c: bool = False) -> str:
    if isinstance(value, HexInt):
        text = f"0x{value:0{value.digits}X}"
    else:
        text = str(value)
    if c and value > 0x7FFFFFFF:
        text += "u"
    return text


def gatt_uuid(base: str, short: int) -> uuid.UUID:
    raw = bytearray(uuid.UUID(base).bytes)
    raw[2:4] = short.to_bytes(2, "big")
    return uuid.UUID(bytes=bytes(raw))


def cbor_key_comments(text: str) -> dict[str, str]:
    block = text.split("  cbor_keys:", 1)[1].split("  message_types:", 1)[0]
    comments: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"\s+(\w+):\s*\d+\s*(?:#\s*(.*))?$", line)
        if match and match.group(2):
            comments[match.group(1)] = match.group(2).strip()
    return comments


def banner(sha: str) -> list[str]:
    return [
        "DO NOT EDIT: generated by tools/codegen.py from protocol/spec.yaml.",
        f"spec sha256: {sha}",
    ]


# ---------------------------------------------------------------------------
# proto_ids.h
# ---------------------------------------------------------------------------


def gen_ids_h(spec: dict[str, Any], sha: str, text: str) -> str:
    L: list[str] = ["/*"]
    L += [f" * {line}" for line in banner(sha)]
    L += [" *", " * Protocol identifiers and constants for Cremind Tag firmware.",
          " * Narrative and rules: docs/protocol.md. Field layouts: proto_msgs.h.", " */",
          "#ifndef CTAG_PROTO_IDS_H_", "#define CTAG_PROTO_IDS_H_", "",
          "#include <stdbool.h>", "#include <stddef.h>", "#include <stdint.h>", "",
          "#ifdef __cplusplus", 'extern "C" {', "#endif", "",
          f"#define CTAG_SPEC_VERSION {spec['spec_version']}",
          f'#define CTAG_SPEC_SHA256 "{sha}"', ""]

    def section(title: str) -> None:
        L.extend([f"/* ---- {title} ---- */", ""])

    def enum(name: str, prefix: str, items: list[tuple[str, int, str]], doc: str) -> None:
        L.append(f"/* {c_comment(doc)} */")
        L.append(f"enum {name} {{")
        for item, value, item_doc in items:
            comment = f" /* {c_comment(item_doc)} */" if item_doc else ""
            L.append(f"\t{prefix}{item} = {fmt_int(value, c=True)},{comment}")
        L.extend(["};", ""])

    def items(group: dict[str, Any], key: str = "value") -> list[tuple[str, int, str]]:
        return [(n, v[key] if isinstance(v, dict) else v,
                 v.get("doc", "") if isinstance(v, dict) else "") for n, v in group.items()]

    section("Constants (spec: constants)")
    for name, entry in spec["constants"].items():
        value, doc = entry["value"], c_comment(entry["doc"])
        if isinstance(value, list):
            ctype = "uint8_t" if max(value) < 0x100 else "uint32_t"
            fn = f"ctag_{name.lower()}"
            L += ["", f"/* {doc} */",
                  f"#define CTAG_{name}_COUNT {len(value)}",
                  f"#define CTAG_{name}_LIST {', '.join(str(v) for v in value)}",
                  f"static inline uint32_t {fn}_at(size_t i)", "{",
                  f"\tstatic const {ctype} v[CTAG_{name}_COUNT] = {{CTAG_{name}_LIST}};", "",
                  f"\treturn i < CTAG_{name}_COUNT ? v[i] : 0u;", "}", "",
                  f"static inline bool {fn}_contains(uint32_t value)", "{",
                  "\tsize_t i;", "",
                  f"\tfor (i = 0; i < CTAG_{name}_COUNT; i++) {{",
                  f"\t\tif ({fn}_at(i) == value) {{", "\t\t\treturn true;", "\t\t}", "\t}",
                  "\treturn false;", "}", ""]
        else:
            L.append(f"#define CTAG_{name} {fmt_int(value, c=True)} /* {doc} */")
    L.append("")

    section("Status codes, delivery stages and outcomes")
    enum("ctag_status", "CTAG_STATUS_", items(spec["status_codes"]),
         "Status codes shared by serial, mesh, GATT and receipts. Never renumbered.")
    enum("ctag_stage", "CTAG_STAGE_", items(spec["delivery_stages"]), "Delivery stages.")
    enum("ctag_outcome", "CTAG_OUTCOME_", items(spec["delivery_outcomes"]),
         "Terminal delivery outcomes.")

    section("Serial protocol (docs/protocol.md 1)")
    enum("ctag_serial_flag", "CTAG_SERIAL_FLAG_", items(spec["serial"]["flags"]),
         "Serial header flags bits.")
    enum("ctag_serial_msg", "CTAG_SERIAL_MSG_",
         [(n, v["value"], f"{v['dir']}: {v['doc']}") for n, v in spec["serial"]["message_types"].items()],
         "Serial message types (header byte 1).")
    comments = cbor_key_comments(text)
    enum("ctag_cbor_key", "CTAG_CBOR_KEY_",
         [(n.upper(), v, comments.get(n, "")) for n, v in spec["serial"]["cbor_keys"].items()],
         "Integer keys of serial CBOR maps (one global dictionary).")
    enum("ctag_tag_cmd", "CTAG_TAG_CMD_", items(spec["tag_commands"]), "Tag commands.")
    enum("ctag_node_role", "CTAG_NODE_ROLE_", items(spec["node_roles"]), "Node roles.")

    section("Bluetooth Mesh vendor models (docs/protocol.md 2-3)")
    enum("ctag_mesh_model", "CTAG_MESH_MODEL_",
         [(n, v["id"], f"on {v['on']}: {v['doc']}") for n, v in spec["mesh"]["models"].items()],
         "Vendor model ids (company CTAG_MESH_COMPANY_ID).")
    enum("ctag_mesh_op", "CTAG_MESH_OP_",
         [(n, v["value"], f"handled by {v['model']}") for n, v in spec["mesh"]["opcodes"].items()],
         "6-bit vendor opcode numbers.")
    L += ["/*",
          " * 3-octet vendor opcode, equal to Zephyr's BT_MESH_MODEL_OP_3(op, CTAG_MESH_COMPANY_ID):",
          " * bits 16..23 = 0xC0 | op, bits 0..15 = the company id in HOST order (no byte",
          " * swap). bt_mesh_model_msg_init() writes octet 0xC0|op and then the company id",
          " * little-endian, so the on-air octets are {0xC0|op, cid & 0xFF, cid >> 8}.",
          " */",
          "#define CTAG_MESH_VENDOR_OPCODE(op) \\",
          "\t((uint32_t)(((0xC0u | (uint32_t)(op)) << 16) | (uint32_t)CTAG_MESH_COMPANY_ID))"]
    for name in spec["mesh"]["opcodes"]:
        L.append(f"#define CTAG_MESH_OPCODE_{name} CTAG_MESH_VENDOR_OPCODE(CTAG_MESH_OP_{name})")
    L.append("")

    section("Logical screen layout (docs/protocol.md 4)")
    layout = spec["layout"]
    L.append(f"#define CTAG_LAYOUT_MAGIC {fmt_int(layout['magic'], c=True)} /* bytes 'C','L' */")
    L.append("")
    enum("ctag_color", "CTAG_COLOR_", items(layout["colors"]), "Layout colours.")
    enum("ctag_qr_ecc", "CTAG_QR_ECC_", items(layout["qr_ecc"]),
         "QR error-correction levels (= Nayuki qrcodegen_Ecc).")
    enum("ctag_layout_cmd", "CTAG_LAYOUT_CMD_",
         [(n, v["value"], v.get("doc", "")) for n, v in layout["commands"].items()],
         "Layout command ops (first byte of every command).")
    enum("ctag_icon", "CTAG_ICON_", [(i["name"].upper(), i["id"], "") for i in spec["icons"]],
         "Built-in icon ids (glyph ids of face 0).")
    L += [f"#define CTAG_ICON_COUNT {len(spec['icons'])}",
          f"#define CTAG_ICON_MAX_ID {max(i['id'] for i in spec['icons'])}", ""]

    section("Tag GATT service (docs/protocol.md 5)")
    gatt = spec["gatt"]
    L += [f'#define CTAG_GATT_BASE_UUID_STR "{gatt["base_uuid"]}"', "",
          "/* Apply a macro to an argument list macro, e.g.",
          " * CTAG_APPLY(BT_UUID_128_ENCODE, CTAG_GATT_SERVICE_UUID_ARGS). */",
          "#define CTAG_APPLY(m, ...) m(__VA_ARGS__)", ""]
    uuids = [("SERVICE", gatt["service_short"], "Tag service")] + [
        (f"CHR_{n}", v["short"], f"{', '.join(v['props'])}: {v['doc']}")
        for n, v in gatt["characteristics"].items()]
    for name, short, doc in uuids:
        u = gatt_uuid(gatt["base_uuid"], short)
        h = u.hex
        args = f"0x{h[0:8]}, 0x{h[8:12]}, 0x{h[12:16]}, 0x{h[16:20]}, 0x{h[20:32]}"
        le = ", ".join(f"0x{b:02x}" for b in reversed(u.bytes))
        L += [f"/* {c_comment(doc)}: {u} */",
              f"#define CTAG_GATT_{name}_SHORT {fmt_int(short, c=True)}",
              f"#define CTAG_GATT_{name}_UUID_ARGS {args}",
              f"#define CTAG_GATT_{name}_UUID_VAL CTAG_APPLY(BT_UUID_128_ENCODE, CTAG_GATT_{name}_UUID_ARGS)",
              f"#define CTAG_GATT_{name}_UUID_BYTES {le}", ""]
    for name, entry in gatt["fragment_header"].items():
        L.append(f"#define CTAG_FRAG_{name} {fmt_int(entry['value'], c=True)} /* {c_comment(entry['doc'])} */")
    L.append("")
    enum("ctag_ctrl_msg", "CTAG_CTRL_",
         [(n, v["value"], v["dir"]) for n, v in gatt["ctrl_messages"].items()],
         "CTRL message types (plaintext, first byte of the reassembled message).")
    enum("ctag_rec_type", "CTAG_REC_",
         [(n, v["value"], v["dir"]) for n, v in gatt["record_types"].items()],
         "Authenticated record types (record header byte 0).")
    enum("ctag_rec_dir", "CTAG_REC_DIR_", items(gatt["record_dir"]),
         "Record nonce direction byte.")
    enum("ctag_plain_msg", "CTAG_PLAIN_",
         [(n, v["value"], v["dir"]) for n, v in gatt["plain_messages"].items()],
         "Plaintext STATUS message types.")

    section("Key schedule labels (docs/protocol.md 5.4)")
    for name, label in spec["crypto"].items():
        L += [f'#define CTAG_CRYPTO_{name.upper()} "{label}"',
              f"#define CTAG_CRYPTO_{name.upper()}_LEN {len(label.encode())}"]
    L.append("")

    section("Protocol v2: identity, ownership, grants (docs/connect-setup.md)")
    v2 = spec["v2"]
    enum("ctag_grant_op", "CTAG_GRANT_OP_", items(v2["grant_ops"]), "Grant operations (grant key 1).")
    enum("ctag_owner_state", "CTAG_OWNER_", items(v2["owner_states"]), "Device ownership states.")
    enum("ctag_link", "CTAG_LINK_", items(v2["links"]), "Noise prologue link byte.")
    enum("ctag_tunnel_state", "CTAG_TUNNEL_", items(v2["tunnel_states"]), "EVT_TUNNEL states.")
    enum("ctag_pair_kind", "CTAG_PAIR_KIND_", items(v2["pair_kinds"]),
         "First byte of a PAIR / tunnel message.")
    enum("ctag_adv_flag", "CTAG_ADV_FLAG_", items(v2["adv_flags"]), "Tag advertising flags.")
    for name, label in v2["crypto"].items():
        L += [f'#define CTAG_V2_{name.upper()} "{label}"',
              f"#define CTAG_V2_{name.upper()}_LEN {len(label.encode())}"]
    L += [f"#define CTAG_V2_UICR_IDENTITY_OFFSET {v2['uicr_identity_offset']}", ""]

    section("Enrollment, boards and panels (docs/protocol.md 9)")
    L += [f"#define CTAG_ENROLLMENT_MAGIC {fmt_int(spec['enrollment']['magic'], c=True)} /* bytes 'C','T','A','G' */", ""]
    enum("ctag_board", "CTAG_BOARD_", items(spec["boards"]), "Board ids.")
    enum("ctag_panel", "CTAG_PANEL_", items(spec["panels"]), "Panel ids.")

    section("Font pack (docs/fontpack.md)")
    for name, value in spec["fontpack"].items():
        L.append(f"#define CTAG_FONTPACK_{name.upper()} {fmt_int(value, c=True)}")
    L += ["", "#ifdef __cplusplus", "}", "#endif", "", "#endif /* CTAG_PROTO_IDS_H_ */", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# proto_msgs.h
# ---------------------------------------------------------------------------

_C_HELPERS = """\
/* Little-endian accessors: byte by byte, no unaligned access, no host-endian
 * assumption, no implementation-defined signed conversions. */
static inline void ctag_put_le16(uint8_t *p, uint16_t v)
{
\tp[0] = (uint8_t)v;
\tp[1] = (uint8_t)(v >> 8);
}

static inline void ctag_put_le32(uint8_t *p, uint32_t v)
{
\tp[0] = (uint8_t)v;
\tp[1] = (uint8_t)(v >> 8);
\tp[2] = (uint8_t)(v >> 16);
\tp[3] = (uint8_t)(v >> 24);
}

static inline void ctag_put_le64(uint8_t *p, uint64_t v)
{
\tctag_put_le32(p, (uint32_t)v);
\tctag_put_le32(p + 4, (uint32_t)(v >> 32));
}

static inline uint16_t ctag_get_le16(const uint8_t *p)
{
\treturn (uint16_t)((uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8));
}

static inline uint32_t ctag_get_le32(const uint8_t *p)
{
\treturn (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
\t       ((uint32_t)p[3] << 24);
}

static inline uint64_t ctag_get_le64(const uint8_t *p)
{
\treturn (uint64_t)ctag_get_le32(p) | ((uint64_t)ctag_get_le32(p + 4) << 32);
}

static inline int8_t ctag_get_i8(const uint8_t *p)
{
\treturn p[0] < 0x80u ? (int8_t)p[0] : (int8_t)((int)p[0] - 256);
}

static inline int16_t ctag_get_le16s(const uint8_t *p)
{
\tuint16_t u = ctag_get_le16(p);

\treturn u < 0x8000u ? (int16_t)u : (int16_t)((int32_t)u - 65536);
}

static inline int32_t ctag_get_le32s(const uint8_t *p)
{
\tuint32_t u = ctag_get_le32(p);

\treturn u < 0x80000000u ? (int32_t)u : -(int32_t)(~u) - 1;
}
"""

_C_PUT = {"u8": "buf[{o}] = m->{n};", "i8": "buf[{o}] = (uint8_t)m->{n};",
          "u16": "ctag_put_le16(&buf[{o}], m->{n});",
          "i16": "ctag_put_le16(&buf[{o}], (uint16_t)m->{n});",
          "u32": "ctag_put_le32(&buf[{o}], m->{n});",
          "i32": "ctag_put_le32(&buf[{o}], (uint32_t)m->{n});",
          "u64": "ctag_put_le64(&buf[{o}], m->{n});"}
_C_GET = {"u8": "m->{n} = buf[{o}];", "i8": "m->{n} = ctag_get_i8(&buf[{o}]);",
          "u16": "m->{n} = ctag_get_le16(&buf[{o}]);", "i16": "m->{n} = ctag_get_le16s(&buf[{o}]);",
          "u32": "m->{n} = ctag_get_le32(&buf[{o}]);", "i32": "m->{n} = ctag_get_le32s(&buf[{o}]);",
          "u64": "m->{n} = ctag_get_le64(&buf[{o}]);"}


def _c_message(m: Message, constants: set[str]) -> list[str]:
    L: list[str] = []
    note = f"{m.fixed_len} bytes"
    if m.tail:
        note += f" + {m.tail.name} (<= CTAG_{m.tail.max})"
    if m.variable:
        note += f"; fixed part only, followed by {m.variable.type} (hand-written codec)"
    L.append(f"/* {c_comment(m.title)}: {note}. */")
    if m.len_macro[len("CTAG_"):] not in constants:
        L.append(f"#define {m.len_macro} {m.fixed_len}")
    if m.tail:
        L.append(f"#define CTAG_{m.c_name.upper()}_MAX_LEN ({m.len_macro} + CTAG_{m.tail.max})")
    if not m.fields:
        L.append("")
        return L
    struct = f"struct ctag_{m.c_name}"
    L.append(f"{struct} {{")
    for f in m.fields:
        doc = f" /* {c_comment(f.doc)} */" if f.doc else ""
        if f.kind == "int":
            L.append(f"\t{_C_TYPES[f.type]} {f.name};{doc}")
        elif f.kind == "bytes_n":
            L.append(f"\tuint8_t {f.name}[{f.size}];{doc}")
        else:
            L.append(f"\tconst uint8_t *{f.name};{doc}")
            L.append(f"\tsize_t {f.name}_len;")
    L.extend(["};", ""])

    # pack
    L.append(f"static inline int ctag_{m.c_name}_pack(const {struct} *m, uint8_t *buf, size_t size)")
    L.append("{")
    tail = m.tail
    if tail:
        L += [f"\tif (m->{tail.name}_len > CTAG_{tail.max}) {{", "\t\treturn -EMSGSIZE;", "\t}",
              f"\tif (m->{tail.name}_len > 0u && m->{tail.name} == NULL) {{", "\t\treturn -EINVAL;", "\t}",
              f"\tif (size < {m.len_macro} + m->{tail.name}_len) {{"]
    else:
        L.append(f"\tif (size < {m.len_macro}) {{")
    L += ["\t\treturn -EMSGSIZE;", "\t}"]
    offset = 0
    for f in m.fixed:
        if f.kind == "int":
            L.append("\t" + _C_PUT[f.type].format(o=offset, n=f.name))
        else:
            L.append(f"\tmemcpy(&buf[{offset}], m->{f.name}, {f.size});")
        offset += f.size
    if tail:
        L += [f"\tif (m->{tail.name}_len > 0u) {{",
              f"\t\tmemcpy(&buf[{offset}], m->{tail.name}, m->{tail.name}_len);", "\t}",
              f"\treturn (int)({m.len_macro} + m->{tail.name}_len);"]
    else:
        L.append(f"\treturn {m.len_macro};")
    L.extend(["}", ""])

    # unpack
    L.append(f"static inline int ctag_{m.c_name}_unpack({struct} *m, const uint8_t *buf, size_t len)")
    L.append("{")
    L += [f"\tif (len < {m.len_macro}) {{", "\t\treturn -EINVAL;", "\t}"]
    if tail:
        L += [f"\tif (len > CTAG_{m.c_name.upper()}_MAX_LEN) {{", "\t\treturn -EMSGSIZE;", "\t}"]
    elif not m.variable:
        L += [f"\tif (len > {m.len_macro}) {{", "\t\treturn -EMSGSIZE;", "\t}"]
    offset = 0
    for f in m.fixed:
        if f.kind == "int":
            L.append("\t" + _C_GET[f.type].format(o=offset, n=f.name))
        else:
            L.append(f"\tmemcpy(m->{f.name}, &buf[{offset}], {f.size});")
        offset += f.size
    if tail:
        L += [f"\tm->{tail.name} = &buf[{offset}];",
              f"\tm->{tail.name}_len = len - {m.len_macro};"]
    L += ["\treturn 0;", "}", ""]
    return L


def gen_msgs_h(spec: dict[str, Any], sha: str, messages: list[Message]) -> str:
    L: list[str] = ["/*"]
    L += [f" * {line}" for line in banner(sha)]
    L += [" *",
          " * Fixed-layout protocol messages: one plain struct (natural alignment, not packed)",
          " * per message plus static inline little-endian codecs.",
          " *",
          " * - Layouts exclude the type/opcode byte (mesh opcode, GATT message type,",
          " *   layout op); the caller frames it.",
          " * - ctag_<msg>_pack(m, buf, size) returns the bytes written, -EMSGSIZE when buf",
          " *   is too small or a trailing field exceeds its max, -EINVAL for a NULL",
          " *   trailing pointer with a non-zero length.",
          " * - ctag_<msg>_unpack(m, buf, len) returns 0, -EINVAL when len is short, or",
          " *   -EMSGSIZE when len is long (fixed messages must match exactly). A trailing",
          " *   bytes field points into buf (zero copy). Messages followed by a variable",
          " *   part (GLYPHS glyphs, QR text) decode the fixed part and accept any longer",
          " *   len.",
          " * - Messages without fields have only a _LEN macro (0).",
          " */",
          "#ifndef CTAG_PROTO_MSGS_H_", "#define CTAG_PROTO_MSGS_H_", "",
          "#include <errno.h>", "#include <stddef.h>", "#include <stdint.h>", "#include <string.h>", "",
          '#include "proto_ids.h"', "",
          "#ifdef __cplusplus", 'extern "C" {', "#endif", "", _C_HELPERS]
    constants = set(spec["constants"])
    for m in messages:
        L += _c_message(m, constants)
    L += ["#ifdef __cplusplus", "}", "#endif", "", "#endif /* CTAG_PROTO_MSGS_H_ */", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate() -> dict[Path, str]:
    text = spec_text()
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    spec = load_spec(text)
    messages = collect_messages(spec)
    check_spec(spec, messages)
    return {
        OUT_IDS_H: gen_ids_h(spec, sha, text),
        OUT_MSGS_H: gen_msgs_h(spec, sha, messages),
    }


def stale_outputs(outputs: dict[Path, str]) -> list[Path]:
    stale = []
    for path, content in outputs.items():
        current = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != content:
            stale.append(path)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a generated file is stale")
    args = parser.parse_args(argv)
    try:
        outputs = generate()
    except SpecError as exc:
        print(f"codegen: {exc}", file=sys.stderr)
        return 2
    if args.check:
        stale = stale_outputs(outputs)
        for path in stale:
            print(f"stale: {path.relative_to(ROOT).as_posix()}", file=sys.stderr)
        return 1 if stale else 0
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
