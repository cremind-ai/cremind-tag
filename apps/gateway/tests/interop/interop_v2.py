#!/usr/bin/env python3
"""Drive the protocol v2 native_sim gateway (apps/gateway/tests/interop built
with v2.conf) as a Connect worker would: plaintext IDENTIFY and SECURE_OPEN,
then every request sealed with the companion's reference SecureChannel
(cremind_tag.secure.channel), grants signed with its reference grants module,
and a Noise session through a mesh tunnel to the simulated bridge's secure
endpoint (lib/secure in C). The companion's GatewayClient is not used (it
speaks v1); this is a small synchronous serial client over the PTY.

Usage (inside the NCS toolchain container, see README.md)::

    PYTHONPATH=/work/companion/src python3 interop_v2.py /build/gw-interop-v2/zephyr/zephyr.exe

Exit status 0 = every scenario passed.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

import serial

from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import (
    PROTO_VERSION,
    SECURE_PROTO_VERSION,
    AdvFlag,
    GrantOp,
    Link,
    NodeRole,
    OwnerState,
    PairKind,
    SerialFlag,
    SerialMsg,
    Status,
    TunnelState,
)
from cremind_tag.protocol.msgs import Ident2
from cremind_tag.protocol.serial_frame import Frame, FrameReader, frame_to_wire
from cremind_tag.secure import grants, identity
from cremind_tag.secure.channel import SecureChannel
from cremind_tag.secure.messages import pair_message, parse_pair_message

# The simulation's fixtures (src/main.c).
GW_IK = bytes([0x0A, 0x1B, 0x2C, 0x3D, 0x4E, 0x5F, 0x60, 0x71, 0x82, 0x93, 0xA4, 0xB5, 0xC6, 0xD7, 0xE8,
               0xF9, 0x01, 0x12, 0x23, 0x34, 0x45, 0x56, 0x67, 0x78, 0x89, 0x9A, 0xAB, 0xBC, 0xCD, 0xDE,
               0xEF, 0xF0])
BRIDGE_SECRET = b"SIM-BRIDGE"
NEWBIE_SECRET = b"NEWBIE-SEC"
SIM_TAG_ID = 0x13572468
BRIDGE = 0x0002

AUTH_SK, AUTH_PUB = identity.ed25519_generate()
OWNER = bytes(range(0x40, 0x50))
WORKER_A = identity.x25519_generate()[0]
WORKER_B = identity.x25519_generate()[0]


class Check(Exception):
    pass


def check(cond: Any, what: str) -> None:
    if not cond:
        raise Check(what)


class Gateway:
    """A minimal serial client: plaintext frames, one secure session."""

    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(port, 115200, timeout=0.02)
        self.reader = FrameReader()
        self.rid = 0
        self.channel: SecureChannel | None = None
        self.events: list[tuple[int, dict[str, Any]]] = []
        self.stale_sealed = 0  # sealed frames of a session this side already left
        self.ident: dict[str, Any] = {}

    def close(self) -> None:
        self.ser.close()

    def _next_rid(self) -> int:
        self.rid = self.rid % 0xFFFF + 1
        return self.rid

    def _send(self, frame: Frame) -> None:
        self.ser.write(frame_to_wire(frame))

    def _pump(self) -> list[tuple[int, int, dict[str, Any]]]:
        """Read what arrived: answers are returned, events queued."""
        data = self.ser.read(4096)
        out = []
        for frame in self.reader.feed(data) if data else []:
            got = self._collect(frame)
            if got is not None:
                out.append(got)
        return out

    def _collect(self, frame: Frame) -> tuple[int, int, dict[str, Any]] | None:
        """(flags, rid, fields) of an answer; events go to the queue."""
        if frame.type == SerialMsg.SECURE_DATA and frame.flags == 0:
            check(frame.request_id == 0, "outer SECURE_DATA request_id")
            outer = cbor_msgs.decode_request(SerialMsg.SECURE_DATA, frame.payload)
            if self.channel is None or not self.channel.open:
                self.stale_sealed += 1  # already on the wire when this side moved on
                return None
            msg = self.channel.unseal(outer["data"])
            fields = SecureChannel.decode(msg)
            if msg.flags & SerialFlag.EVENT:
                self.events.append((msg.type, fields))
                return None
            check(msg.flags & SerialFlag.RESPONSE, f"inner flags {msg.flags}")
            return msg.flags, msg.request_id, {"_type": msg.type, "_plain": False, **fields}
        if frame.flags & SerialFlag.EVENT:
            raise Check(f"plaintext event 0x{frame.type:02x} from a v2 gateway")
        if frame.flags & SerialFlag.RESPONSE:
            fields = cbor_msgs.decode_response(SerialMsg(frame.type), frame.payload)
            return frame.flags, frame.request_id, {"_type": frame.type, "_plain": True, **fields}
        raise Check(f"unexpected frame 0x{frame.type:02x} flags {frame.flags}")

    def _wait(self, rid: int, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for _flags, got_rid, fields in self._pump():
                if got_rid == rid:
                    return fields
        raise Check(f"no answer to request {rid}")

    def plain_request(self, mtype: SerialMsg, fields: dict[str, Any] | None = None,
                      credits: int = 8) -> dict[str, Any]:
        rid = self._next_rid()
        payload = cbor_msgs.encode_request(mtype, fields) if fields else b""
        self._send(Frame(int(mtype), rid, payload, 0, credits))
        return self._wait(rid)

    def secure_data(self, data: bytes, credits: int = 8) -> dict[str, Any]:
        """A raw SECURE_DATA frame (outer request_id 0) and its plaintext answer."""
        self._send(Frame(int(SerialMsg.SECURE_DATA), 0, cbor_msgs.encode_request(
            SerialMsg.SECURE_DATA, {"data": data}), 0, credits))
        return self._wait(0)

    def request(self, mtype: SerialMsg, fields: dict[str, Any] | None = None,
                credits: int = 8) -> dict[str, Any]:
        check(self.channel is not None and self.channel.open, "no session")
        assert self.channel is not None
        rid, sealed = self.channel.seal_request(mtype, fields or {})
        self._send(Frame(int(SerialMsg.SECURE_DATA), 0, cbor_msgs.encode_request(
            SerialMsg.SECURE_DATA, {"data": sealed}), 0, credits))
        answer = self._wait(rid)
        check(not answer["_plain"], f"{mtype.name}: a plaintext answer {answer}")
        return answer

    def hello(self) -> dict[str, Any]:
        self.channel = None
        return self.plain_request(SerialMsg.HELLO, {"proto": PROTO_VERSION, "name": "interop-v2"}, credits=16)

    def identify(self) -> dict[str, Any]:
        self.ident = self.plain_request(SerialMsg.IDENTIFY)
        return self.ident

    def open(self, controller: bytes) -> dict[str, Any]:
        if not self.ident:
            self.identify()
        ch = SecureChannel(controller, self.ident["ik"], self.ident["device_id"], Link.SERIAL)
        self.channel = None
        answer = self.plain_request(SerialMsg.SECURE_OPEN, {"data": ch.message1()})
        if answer["status"] == Status.OK:
            ch.finish(answer["data"])
            self.channel = ch
        return answer

    def session(self, controller: bytes) -> None:
        """HELLO, IDENTIFY, SECURE_OPEN: a fresh session for this controller."""
        self.hello()
        self.identify()
        answer = self.open(controller)
        check(answer["status"] == Status.OK, f"SECURE_OPEN {answer}")

    def pop_event(self, mtype: SerialMsg, timeout: float = 5.0,
                  match: Callable[[dict[str, Any]], bool] = lambda _f: True) -> dict[str, Any]:
        """The oldest queued event of a type (waiting for it); the rest stay queued."""
        deadline = time.monotonic() + timeout
        while True:
            for i, (t, f) in enumerate(self.events):
                if t == mtype and match(f):
                    del self.events[i]
                    return f
            if time.monotonic() >= deadline:
                raise Check(f"no {mtype.name}")
            self._pump()

    def collect_events(self, mtype: SerialMsg, seconds: float) -> list[dict[str, Any]]:
        """Every event of a type that arrives within the window."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump()
        found = [f for t, f in self.events if t == mtype]
        self.events = [(t, f) for t, f in self.events if t != mtype]
        return found

    def drop_events(self, mtype: SerialMsg) -> None:
        self.events = [(t, f) for t, f in self.events if t != mtype]


def gateway_grant(gw: Gateway, op: GrantOp, controller_priv: bytes, gen_from: int,
                  challenge: bytes) -> dict[str, bytes]:
    g = grants.Grant(op, gw.ident["device_id"], NodeRole.GATEWAY, AUTH_PUB, OWNER,
                     identity.x25519_public(controller_priv), gen_from, gen_from + 1, challenge).encode()
    return {"grant": g, "sig": grants.sign(g, AUTH_SK)}


def status_of(gw: Gateway) -> dict[str, Any]:
    st = gw.request(SerialMsg.STATUS)
    check(st["status"] == Status.OK, f"STATUS {st}")
    return st


def op_id() -> int:
    return int.from_bytes(os.urandom(8), "little")


# ---------------------------------------------------------------------------


def s_plaintext_and_identify(gw: Gateway, notes: list[str]) -> None:
    h = gw.hello()
    check(h["status"] == Status.OK and h["proto"] == PROTO_VERSION, f"HELLO {h}")
    ident = gw.identify()
    check(ident["status"] == Status.OK and ident["proto"] == SECURE_PROTO_VERSION, f"IDENTIFY {ident}")
    check(ident["role"] == NodeRole.GATEWAY, "role")
    check(ident["ik"] == identity.x25519_public(GW_IK), "ik")
    check(ident["device_id"] == identity.device_id(NodeRole.GATEWAY, ident["ik"]), "device_id of 2.1")
    check(ident["owner_state"] == OwnerState.UNOWNED and ident["gen"] == 0, "factory state")
    check("authority_id" not in ident, "no authority while unowned")
    again = gw.identify()
    check(again["challenge"] != ident["challenge"], "a fresh challenge per IDENTIFY")
    for mtype, fields in ((SerialMsg.INFO, None), (SerialMsg.LIST_NODES, None),
                          (SerialMsg.REBOOT, {"op_id": 1}), (SerialMsg.STATUS, None)):
        a = gw.plain_request(mtype, fields)
        check(a["status"] == Status.AUTH_REQUIRED, f"plaintext {mtype.name}: {a}")
    ping = gw.plain_request(SerialMsg.PING)
    check(ping["status"] == Status.OK, "PING in plaintext")
    a = gw.secure_data(os.urandom(40))
    check(a["_plain"] and a["status"] == Status.AUTH_REQUIRED, f"SECURE_DATA without a session: {a}")
    bad = gw.plain_request(SerialMsg.SECURE_OPEN, {"data": os.urandom(96)})
    check(bad["status"] == Status.AUTH_FAILED, f"SECURE_OPEN with garbage: {bad}")
    notes.append(f"device_id {ident['device_id'].hex()}; plaintext v1 requests -> AUTH_REQUIRED")


def s_claim(gw: Gateway, notes: list[str]) -> None:
    gw.session(WORKER_A)
    st = status_of(gw)
    check(st["owner_state"] == OwnerState.UNOWNED and not st["controller_match"], f"unowned {st}")
    recover = gateway_grant(gw, GrantOp.RECOVER, WORKER_A, 0, st["challenge"])
    for mtype, fields in ((SerialMsg.LIST_NODES, None), (SerialMsg.GET_COUNTERS, None),
                          (SerialMsg.RECOVER, recover)):
        a = gw.request(mtype, fields)
        check(a["status"] == Status.NOT_OWNER, f"unowned {mtype.name}: {a}")
    check(gw.request(SerialMsg.INFO)["status"] == Status.OK, "INFO while unowned")
    # A grant for another controller is refused (rule 4); a good one claims.
    bad = gateway_grant(gw, GrantOp.CLAIM, WORKER_B, 0, status_of(gw)["challenge"])
    check(gw.request(SerialMsg.CLAIM, bad)["status"] == Status.GRANT_INVALID, "grant for another controller")
    grant = gateway_grant(gw, GrantOp.CLAIM, WORKER_A, 0, status_of(gw)["challenge"])
    t0 = time.monotonic()
    ok = gw.request(SerialMsg.CLAIM, grant)
    dt = time.monotonic() - t0
    check(ok["status"] == Status.OK and ok["gen"] == 1, f"CLAIM {ok}")
    replay = gw.request(SerialMsg.CLAIM, grant)
    check(replay["status"] == Status.GRANT_INVALID, f"the same CLAIM again (challenge used): {replay}")
    st = status_of(gw)
    check(st["owner_state"] == OwnerState.OWNED and st["controller_match"], f"owned {st}")
    check(st.get("owner") == OWNER, "the owner, to the pinned controller")
    check(st["authority_id"] == identity.authority_id(AUTH_PUB), "authority_id")
    nodes = gw.request(SerialMsg.LIST_NODES)
    check(nodes["status"] == Status.OK and sorted(n["addr"] for n in nodes["nodes"]) == [2, 3], f"nodes {nodes}")
    notes.append(f"CLAIM gen 1 answered in {dt * 1000:.0f} ms (grant check with Ed25519)")


def s_access_and_recover(gw: Gateway, notes: list[str]) -> None:
    gw.session(WORKER_B)
    st = status_of(gw)
    check(st["owner_state"] == OwnerState.OWNED and not st["controller_match"], f"{st}")
    check("owner" not in st and st["authority_id"] == identity.authority_id(AUTH_PUB),
          f"another controller sees the authority, not the owner: {st}")
    check(gw.request(SerialMsg.INFO)["status"] == Status.OK, "INFO for any controller")
    for mtype in (SerialMsg.LIST_NODES, SerialMsg.GET_INVENTORY, SerialMsg.SCAN_UNPROV):
        a = gw.request(mtype, {"duration_s": 1} if mtype == SerialMsg.SCAN_UNPROV else None)
        check(a["status"] == Status.NOT_OWNER, f"B {mtype.name}: {a}")
    claim = gateway_grant(gw, GrantOp.CLAIM, WORKER_B, 1, status_of(gw)["challenge"])
    check(gw.request(SerialMsg.CLAIM, claim)["status"] == Status.NOT_OWNER, "CLAIM by B")
    rec = gw.request(SerialMsg.RECOVER, gateway_grant(gw, GrantOp.RECOVER, WORKER_B, 1,
                                                      status_of(gw)["challenge"]))
    check(rec["status"] == Status.OK and rec["gen"] == 2, f"RECOVER {rec}")
    check(gw.request(SerialMsg.LIST_NODES)["status"] == Status.OK, "B pinned")
    gw.session(WORKER_A)
    check(gw.request(SerialMsg.LIST_NODES)["status"] == Status.NOT_OWNER, "A is not pinned any more")
    notes.append("B: no owner in STATUS, NOT_OWNER, RECOVER gen 2, then pinned; A refused")


def s_events_sealed(gw: Gateway, notes: list[str]) -> None:
    gw.session(WORKER_B)
    op = op_id()
    layout = os.urandom(600)
    a = gw.request(SerialMsg.DELIVER_LAYOUT, {"op_id": op, "bridge": BRIDGE, "tag_id": 0x0A0B0C01, "epoch": 1,
                                              "revision": 1, "update_id": op, "fontpack_id": bytes(8),
                                              "layout": layout})
    check(a["status"] == Status.ACCEPTED, f"DELIVER_LAYOUT {a}")
    res = gw.pop_event(SerialMsg.EVT_RESULT, timeout=10, match=lambda f: f["update_id"] == op)
    check(res["status"] == Status.OK, f"EVT_RESULT {res}")
    check(res["digest"] == hashlib.sha256(layout).digest()[:8], "digest")
    check(gw.request(SerialMsg.EVENT_ACK, {"seq": res["seq"]})["status"] == Status.OK, "EVENT_ACK")
    gw.drop_events(SerialMsg.EVT_STAGE)
    info = gw.request(SerialMsg.INFO)
    c = info["counters"]
    check(c["secure_opens"] >= 1 and c["claims"] == 1 and c["recovers"] == 1, f"v2 counters {c}")
    notes.append(f"EVT_RESULT sealed (seq {res['seq']}); secure heap peak {c['secure_heap_peak']} B, "
                 f"{c['secure_heap_failures']} failures")


def s_discover(gw: Gateway, notes: list[str]) -> None:
    a = gw.request(SerialMsg.DISCOVER, {"op_id": op_id(), "bridge": 0, "duration_s": 10, "tag_id": 0})
    check(a["status"] == Status.ACCEPTED, f"DISCOVER {a}")
    found = gw.collect_events(SerialMsg.EVT_DISCOVERED, 1.5)
    check(found, "EVT_DISCOVERED")
    check(all(f["tag_id"] == SIM_TAG_ID and f["flags"] & AdvFlag.SETUP for f in found), f"{found}")
    per_bridge = sorted({f["bridge"] for f in found})
    check(per_bridge == [2, 3], f"both configured bridges: {per_bridge}")
    check(len(found) == len(per_bridge), f"rate limit: {len(found)} events for {len(per_bridge)} bridges")
    bad = gw.request(SerialMsg.DISCOVER, {"op_id": op_id(), "bridge": 0, "duration_s": 121, "tag_id": 0})
    check(bad["status"] == Status.INVALID, f"DISCOVER for 121 s: {bad}")
    c = gw.request(SerialMsg.GET_COUNTERS)["counters"]
    check(c["discovered"] == 2 and c["discovered_limited"] == 2, f"counters {c}")
    notes.append(f"{len(found)} candidates (bridges {per_bridge}), 2 duplicates rate-limited")


def tunnel_message(gw: Gateway, tunnel: int, timeout: float = 5) -> tuple[int, bytes]:
    ev = gw.pop_event(SerialMsg.EVT_TUNNEL, timeout, match=lambda f: f["tunnel"] == tunnel)
    if ev["state"] == TunnelState.CLOSED:
        raise Check(f"tunnel closed: {ev}")
    return ev["state"], ev["data"]


def s_tunnel_pair(gw: Gateway, notes: list[str]) -> None:
    op = op_id()
    open_req = {"op_id": op, "bridge": BRIDGE, "tag_id": 0, "duration_s": 30}
    a = gw.request(SerialMsg.TUNNEL_OPEN, open_req)
    check(a["status"] == Status.OK and a["tunnel"] > 0, f"TUNNEL_OPEN {a}")
    tunnel = a["tunnel"]
    again = gw.request(SerialMsg.TUNNEL_OPEN, open_req)
    check(again["status"] == Status.OK and again["tunnel"] == tunnel and again.get("detail") == Status.DUPLICATE,
          f"the same TUNNEL_OPEN again: {again}")
    busy = gw.request(SerialMsg.TUNNEL_OPEN, {**open_req, "op_id": op + 1})
    check(busy["status"] == Status.BUSY, f"a second tunnel to the bridge: {busy}")
    state, data = tunnel_message(gw, tunnel)
    check(state == TunnelState.OPEN, "the first message is OPEN")
    ident = Ident2.unpack(data)
    check(ident.role == NodeRole.BRIDGE and ident.owner_state == OwnerState.UNOWNED, f"{ident}")
    check(ident.device_id == identity.device_id(NodeRole.BRIDGE, ident.ik), "bridge device_id")
    worker = WORKER_B
    ch = SecureChannel(worker, ident.ik, ident.device_id, Link.TUNNEL)
    t0 = time.monotonic()
    sent = gw.request(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel,
                                              "data": pair_message(PairKind.HANDSHAKE, ch.message1())})
    check(sent["status"] == Status.OK, f"TUNNEL_SEND msg1 {sent}")
    state, data = tunnel_message(gw, tunnel)
    kind, body = parse_pair_message(data)
    check(state == TunnelState.DATA and kind == PairKind.HANDSHAKE, "msg2")
    ch.finish(body)
    handshake_ms = (time.monotonic() - t0) * 1000

    def call(mtype: SerialMsg, fields: dict[str, Any]) -> dict[str, Any]:
        rid, sealed = ch.seal_request(mtype, fields)
        r = gw.request(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel, "data": pair_message(PairKind.TRANSPORT, sealed)})
        check(r["status"] == Status.OK, f"TUNNEL_SEND {r}")
        _state, raw = tunnel_message(gw, tunnel)
        k, b = parse_pair_message(raw)
        check(k == PairKind.TRANSPORT, "transport answer")
        msg = ch.unseal(b)
        check(msg.request_id == rid and msg.flags & SerialFlag.RESPONSE, "answer header")
        return SecureChannel.decode(msg)

    st = call(SerialMsg.STATUS, {})
    check(st["status"] == Status.OK and st["owner_state"] == OwnerState.UNOWNED, f"bridge STATUS {st}")
    g = grants.Grant(GrantOp.PAIR, ident.device_id, NodeRole.BRIDGE, AUTH_PUB, OWNER,
                     identity.x25519_public(worker), st["gen"], st["gen"] + 1, st["challenge"]).encode()
    proof_s, k_set = ch.setup_proof(BRIDGE_SECRET, g)
    mk = os.urandom(32)
    t1 = time.monotonic()
    ans = call(SerialMsg.PAIR, {"grant": g, "sig": grants.sign(g, AUTH_SK), "proof": proof_s, "op_key": mk})
    pair_ms = (time.monotonic() - t1) * 1000
    check(ans["status"] == Status.OK and ans["gen"] == st["gen"] + 1, f"PAIR {ans}")
    check(ch.check_device_proof(k_set, proof_s, ans["proof"]), "the bridge's proof_d")
    st2 = call(SerialMsg.STATUS, {})
    check(st2["owner_state"] == OwnerState.OWNED and st2["controller_match"] and st2.get("owner") == OWNER,
          f"after PAIR {st2}")
    maint = call(SerialMsg.MAINT_AUTH, {"proof": ch.maint_proof(mk)})
    check(maint["status"] == Status.NOT_OWNER, f"MAINT_AUTH through a tunnel: {maint}")
    closed = gw.request(SerialMsg.TUNNEL_CLOSE, {"tunnel": tunnel})
    check(closed["status"] == Status.OK, "TUNNEL_CLOSE")
    gone = gw.request(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel, "data": b"x"})
    check(gone["status"] == Status.NOT_FOUND, f"TUNNEL_SEND after the close: {gone}")
    other = gw.request(SerialMsg.TUNNEL_OPEN, {"op_id": op + 2, "bridge": 3, "tag_id": 0, "duration_s": 5})
    check(other["status"] == Status.OK, f"a tunnel to 0x0003: {other}")
    ev = gw.pop_event(SerialMsg.EVT_TUNNEL, 3, match=lambda f: f["tunnel"] == other["tunnel"])
    check(ev["state"] == TunnelState.CLOSED and ev.get("status") == Status.BUSY, f"0x0003 {ev}")
    notes.append(f"Noise IK through the tunnel in {handshake_ms:.0f} ms, PAIR (proof_d ok) in {pair_ms:.0f} ms, "
                 f"MAINT_AUTH over the mesh NOT_OWNER")


def s_provision_static_oob(gw: Gateway, notes: list[str]) -> None:
    a = gw.request(SerialMsg.SCAN_UNPROV, {"duration_s": 3})
    check(a["status"] == Status.OK, f"SCAN_UNPROV {a}")
    beacon = gw.pop_event(SerialMsg.EVT_UNPROV_BEACON, timeout=3)
    uuid = beacon["uuid"]
    op = op_id()
    missing = gw.request(SerialMsg.PROVISION, {"op_id": op, "uuid": uuid})
    check(missing["status"] == Status.INVALID, f"PROVISION without static_oob: {missing}")
    wrong = gw.request(SerialMsg.PROVISION, {"op_id": op + 1, "uuid": uuid, "static_oob": bytes(32)})
    check(wrong["status"] == Status.ACCEPTED, f"PROVISION (wrong OOB) {wrong}")
    ev = gw.pop_event(SerialMsg.EVT_PROVISIONED, 5, match=lambda f: f["op_id"] == op + 1)
    check(ev["status"] == Status.SECURITY_CONFIG and ev["addr"] == 0, f"wrong OOB: {ev}")
    gw.request(SerialMsg.EVENT_ACK, {"seq": ev["seq"]})
    oob = identity.static_oob(NEWBIE_SECRET, uuid)
    ok = gw.request(SerialMsg.PROVISION, {"op_id": op + 2, "uuid": uuid, "static_oob": oob, "name": "newbie"})
    check(ok["status"] == Status.ACCEPTED, f"PROVISION {ok}")
    ev = gw.pop_event(SerialMsg.EVT_PROVISIONED, 5, match=lambda f: f["op_id"] == op + 2)
    check(ev["status"] == Status.OK and ev["addr"] == 4, f"right OOB: {ev}")
    gw.request(SerialMsg.EVENT_ACK, {"seq": ev["seq"]})
    c = gw.request(SerialMsg.GET_COUNTERS)["counters"]
    check(c["prov_security"] == 1, f"prov_security {c['prov_security']}")
    notes.append("no static_oob -> INVALID, wrong static OOB -> SECURITY_CONFIG (final), "
                 "derived static OOB -> 0x0004")


def s_session_failures(gw: Gateway, notes: list[str]) -> None:
    assert gw.channel is not None
    _rid, sealed = gw.channel.seal_request(SerialMsg.PING, {})
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    a = gw.secure_data(tampered)
    check(a["_plain"] and a["status"] == Status.AUTH_REQUIRED, f"tampered: {a}")
    # The gateway's session is gone: even a well-sealed message is refused now.
    _rid, sealed = gw.channel.seal_request(SerialMsg.PING, {})
    a = gw.secure_data(sealed)
    check(a["_plain"] and a["status"] == Status.AUTH_REQUIRED, f"after the failure: {a}")
    gw.session(WORKER_B)
    counters = gw.request(SerialMsg.GET_COUNTERS)["counters"]
    check(counters["decrypt_failures"] == 1, f"decrypt_failures {counters['decrypt_failures']}")
    notes.append("tampered SECURE_DATA -> plaintext AUTH_REQUIRED, the session ends, a new one works")


def s_release(gw: Gateway, notes: list[str]) -> None:
    st = status_of(gw)
    rel = gw.request(SerialMsg.RELEASE, gateway_grant(gw, GrantOp.RELEASE, WORKER_B, st["gen"], st["challenge"]))
    check(rel["status"] == Status.OK and rel["gen"] == st["gen"] + 1, f"RELEASE {rel}")
    time.sleep(1.0)  # the gateway reboots once the answer is out
    gw.hello()
    ident = gw.identify()
    check(ident["owner_state"] == OwnerState.UNOWNED and ident["gen"] == st["gen"] + 1, f"after RELEASE {ident}")
    check(gw.open(WORKER_A)["status"] == Status.OK, "SECURE_OPEN")
    c = gw.request(SerialMsg.CLAIM, gateway_grant(gw, GrantOp.CLAIM, WORKER_A, ident["gen"],
                                                  status_of(gw)["challenge"]))
    check(c["status"] == Status.OK and c["gen"] == ident["gen"] + 1, f"CLAIM after RELEASE {c}")
    nodes = gw.request(SerialMsg.LIST_NODES)
    check(nodes["status"] == Status.OK and nodes["nodes"] == [], f"the old network is gone: {nodes}")
    notes.append(f"RELEASE gen {rel['gen']} -> reboot, UNOWNED; CLAIM gen {c['gen']} on an empty network")


SCENARIOS: list[tuple[str, Callable[[Gateway, list[str]], None]]] = [
    ("plaintext layer: HELLO, IDENTIFY, AUTH_REQUIRED", s_plaintext_and_identify),
    ("SECURE_OPEN + CLAIM (grant) -> owned", s_claim),
    ("access table: another controller, RECOVER", s_access_and_recover),
    ("sealed answers and events (DELIVER_LAYOUT -> EVT_RESULT)", s_events_sealed),
    ("DISCOVER -> EVT_DISCOVERED (rate-limited)", s_discover),
    ("tunnel to the bridge endpoint: Noise IK + PAIR", s_tunnel_pair),
    ("PROVISION with static OOB", s_provision_static_oob),
    ("decrypt failure ends the session", s_session_failures),
    ("RELEASE -> UNOWNED, network wiped", s_release),
]


def start_gateway(exe: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    for line in proc.stdout:
        sys.stdout.write("  gateway| " + line)
        m = re.search(r"pseudotty: (/dev/pts/\d+)", line)
        if m:
            return proc, m.group(1)
        if time.monotonic() > deadline:
            break
    proc.kill()
    raise SystemExit("the gateway did not report its PTY")


def main(exe: str) -> int:
    proc, port = start_gateway(exe)

    def pump_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write("  gateway| " + line)

    threading.Thread(target=pump_output, daemon=True).start()
    time.sleep(0.5)
    gw = Gateway(port)
    rows: list[tuple[str, str, str, float]] = []
    try:
        for name, fn in SCENARIOS:
            notes: list[str] = []
            t0 = time.monotonic()
            try:
                fn(gw, notes)
                rows.append((name, "PASS", "; ".join(notes), time.monotonic() - t0))
            except Exception as exc:  # noqa: BLE001 - report every failure
                traceback.print_exc()
                rows.append((name, "FAIL", f"{type(exc).__name__}: {exc}", time.monotonic() - t0))
            print(f"{rows[-1][1]} {name} ({rows[-1][3]:.1f} s): {rows[-1][2]}", flush=True)
    finally:
        gw.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n| Scenario | Result | Time | Details |\n|---|---|---|---|")
    for name, res, detail, dt in rows:
        print(f"| {name} | {res} | {dt:.1f} s | {detail} |")
    failed = sum(1 for r in rows if r[1] != "PASS")
    print(f"\n{len(rows) - failed}/{len(rows)} scenarios passed"
          + (f" ({gw.stale_sealed} stale sealed frames ignored)" if gw.stale_sealed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1]))
