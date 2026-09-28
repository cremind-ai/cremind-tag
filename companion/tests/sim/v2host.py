"""A minimal host-side protocol v2 driver for the simulator tests (docs/connect-setup.md 5-7, docs/simulator.md).

What a Connect worker does on the wire, without its durability: ``HELLO`` and
serial credits; plaintext ``IDENTIFY`` and ``SECURE_OPEN``; sealed requests in
``SECURE_DATA`` frames (``request_id = 0``, ``flags = 0``) matched by the inner
``request_id``; sealed events (retained ones acknowledged with a sealed
``EVENT_ACK``); tunnels (``TUNNEL_OPEN``/``SEND``/``CLOSE``, ``EVT_TUNNEL``) with the
worker's side of a ``PAIR`` endpoint. Grants are signed by a test
:class:`Authority` as ``tests/protocol/test_v2_device.py`` does. The worker side
of every session is :class:`cremind_tag.secure.channel.SecureChannel`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cremind_tag.protocol import cbor_msgs
from cremind_tag.protocol.ids import (
    PROTO_VERSION,
    GrantOp,
    Link,
    NodeRole,
    PairKind,
    SerialFlag,
    SerialMsg,
    Status,
    TunnelState,
)
from cremind_tag.protocol.msgs import Ident2
from cremind_tag.protocol.serial_frame import Frame, FrameReader, frame_to_wire
from cremind_tag.secure import grants, identity
from cremind_tag.secure.channel import SecureChannel, SecureChannelError
from cremind_tag.secure.codes import SetupPayload
from cremind_tag.secure.messages import pair_message, parse_pair_message

HOST_GRANT = 250  # the HELLO's credit byte: frames the device may send beyond the default window
GRANT_EVERY = 32  # owed grants that make the host send a PING carrying them


class SessionFailed(Exception):
    """A secure session ended: a plaintext ``SECURE_DATA`` answer, a ``CLOSE``, or a refused ``SECURE_OPEN``."""

    def __init__(self, status: int, where: str = "") -> None:
        self.status = Status(status)
        super().__init__(f"secure session failed{' in ' + where if where else ''}: {self.status.name}")


class TunnelClosed(Exception):
    def __init__(self, status: int) -> None:
        self.status = Status(status)
        super().__init__(f"tunnel closed: {self.status.name}")


@dataclass
class Authority:
    """A Cremind installation's grant signer (connect-setup.md 3.1)."""

    sk: bytes
    pub: bytes
    owner: bytes = b"\x11" * 16

    @classmethod
    def new(cls, owner: bytes = b"\x11" * 16) -> Authority:
        sk, pub = identity.ed25519_generate()
        return cls(sk, pub, owner)

    @property
    def authority_id(self) -> bytes:
        return identity.authority_id(self.pub)

    def grant(self, op: GrantOp, device_id: bytes, role: NodeRole, controller: bytes, gen_from: int,
              challenge: bytes, *, owner: bytes | None = None) -> dict[str, bytes]:
        raw = grants.Grant(op, device_id, role, self.pub, owner or self.owner, controller, gen_from, gen_from + 1,
                           challenge).encode()
        return {"grant": raw, "sig": grants.sign(raw, self.sk)}


class Worker:
    """A controller key (the Noise initiator's static key)."""

    def __init__(self) -> None:
        self.priv, self.pub = identity.x25519_generate()


@dataclass
class Event:
    msg: SerialMsg
    fields: dict[str, Any]

    def matches(self, msg: SerialMsg, **fields: Any) -> bool:
        return self.msg == msg and all(self.fields.get(k) == v for k, v in fields.items())


class V2Host:
    """One serial connection to a simulated v2 device (see the module docstring)."""

    def __init__(self, url: str, *, name: str = "v2-test", auto_ack: bool = True) -> None:
        self.url = url
        self.name = name
        self.auto_ack = auto_ack
        self.channel: SecureChannel | None = None
        self.credits = 0
        self.owed = 0
        self.events: list[Event] = []
        self.plain_events: list[Frame] = []
        self.secure_failures: list[Status] = []  # plaintext SECURE_DATA answers
        self.stats: dict[str, int] = {"stray_sealed": 0, "unmatched": 0, "grant_pings": 0}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._frames = FrameReader()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._credit = asyncio.Event()
        self._changed = asyncio.Event()
        self._rids = itertools.count(1)
        self._op_ids = itertools.count(random.randrange(1, 1 << 40))
        self._plain: dict[int, asyncio.Future[Frame]] = {}
        self._opening: dict[int, SecureChannel] = {}
        self._sealed: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._hello: tuple[int, asyncio.Future[Frame]] | None = None
        self._tunnels: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._granting = False

    # -- lifecycle ------------------------------------------------------------------------------

    async def __aenter__(self) -> V2Host:
        await self.connect()
        await self.hello()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        host, port = self.url.removeprefix("socket://").rsplit(":", 1)
        self._reader, self._writer = await asyncio.open_connection(host, int(port))
        self._task = asyncio.create_task(self._read_loop(), name="v2host reader")

    async def close(self) -> None:
        for task in [self._task, *self._background]:
            if task is not None:
                task.cancel()
        for task in [self._task, *self._background]:
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    def new_op_id(self) -> int:
        return next(self._op_ids)

    # -- sending --------------------------------------------------------------------------------

    async def _send(self, msg: int, request_id: int, payload: Callable[[], bytes], *, internal: bool = False) -> None:
        """One frame: wait for a device credit (one is kept for grant PINGs), build the payload (a sealed one is
        sealed here, so nonces follow the wire order), piggyback what the host owes."""
        assert self._writer is not None
        async with self._lock:
            need = 1 if internal else 2
            while self.credits < need:
                self._credit.clear()
                await asyncio.wait_for(self._credit.wait(), 10)
            body = payload()
            self.credits -= 1
            grant = min(self.owed, 255)
            self.owed -= grant
            self._writer.write(frame_to_wire(Frame(msg, request_id, body, 0, grant)))
        await self._writer.drain()

    async def hello(self) -> dict[str, Any]:
        """HELLO: resets the link (credits) and drops any secure session."""
        assert self._writer is not None
        rid = next(self._rids)
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._hello = (rid, future)
        payload = cbor_msgs.encode_request(SerialMsg.HELLO, {"proto": PROTO_VERSION, "name": self.name})
        self._writer.write(frame_to_wire(Frame(SerialMsg.HELLO, rid, payload, 0, HOST_GRANT)))
        await self._writer.drain()
        frame = await asyncio.wait_for(future, 5)
        return cbor_msgs.decode_response(SerialMsg.HELLO, frame.payload)

    async def request(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                      timeout: float = 5.0) -> dict[str, Any]:
        """A plaintext request."""
        rid = next(self._rids) % 0xFFFF or next(self._rids)
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._plain[rid] = future
        body = cbor_msgs.encode_request(msg, fields or {})
        await self._send(msg, rid, lambda: body)
        try:
            frame = await asyncio.wait_for(future, timeout)
        finally:
            self._plain.pop(rid, None)
        return cbor_msgs.decode_response(msg, frame.payload)

    async def identify(self) -> dict[str, Any]:
        reply = await self.request(SerialMsg.IDENTIFY)
        assert reply["status"] == Status.OK, reply
        return reply

    async def secure_open(self, worker: Worker, ik: bytes, device_id: bytes) -> SecureChannel:
        """SECURE_OPEN; the channel is installed while the answer is handled, before the sealed frames after it."""
        channel = SecureChannel(worker.priv, ik, device_id, Link.SERIAL)
        self.channel = None
        rid = next(self._rids) % 0xFFFF or next(self._rids)
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._plain[rid] = future
        self._opening[rid] = channel
        body = cbor_msgs.encode_request(SerialMsg.SECURE_OPEN, {"data": channel.message1()})
        await self._send(SerialMsg.SECURE_OPEN, rid, lambda: body)
        try:
            frame = await asyncio.wait_for(future, 5)
        finally:
            self._plain.pop(rid, None)
            self._opening.pop(rid, None)
        reply = cbor_msgs.decode_response(SerialMsg.SECURE_OPEN, frame.payload)
        if reply["status"] != Status.OK:
            raise SessionFailed(reply["status"], "SECURE_OPEN")
        assert self.channel is channel
        return channel

    async def call(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                   timeout: float = 5.0) -> dict[str, Any]:
        """A request sealed inside the session; returns the decoded sealed answer."""
        channel = self.channel
        if channel is None or not channel.open:
            raise SessionFailed(Status.AUTH_REQUIRED, f"{msg.name}: no session")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        rids: list[int] = []

        def payload() -> bytes:
            rid, sealed = channel.seal_request(msg, fields or {})
            self._sealed[rid] = future
            rids.append(rid)
            return cbor_msgs.encode_request(SerialMsg.SECURE_DATA, {"data": sealed})

        await self._send(SerialMsg.SECURE_DATA, 0, payload)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            for rid in rids:
                if self._sealed.get(rid) is future:
                    del self._sealed[rid]

    async def send_raw_secure(self, data: bytes) -> None:
        """A ``SECURE_DATA`` frame with arbitrary ciphertext (a tampered or foreign message)."""
        body = cbor_msgs.encode_request(SerialMsg.SECURE_DATA, {"data": data})
        await self._send(SerialMsg.SECURE_DATA, 0, lambda: body)

    async def ok(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                 expect: Status = Status.OK, timeout: float = 5.0) -> dict[str, Any]:
        reply = await self.call(msg, fields, timeout=timeout)
        assert reply["status"] == expect, (msg.name, Status(reply["status"]).name, reply)
        return reply

    # -- receiving ------------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                for frame in self._frames.feed(data):
                    self._on_frame(frame)
        except (ConnectionError, OSError):
            pass
        finally:
            self._fail_sealed(Status.DISCONNECTED)

    def _on_frame(self, frame: Frame) -> None:
        if frame.flags & SerialFlag.RESPONSE and frame.type == SerialMsg.HELLO:
            if self._hello is not None and self._hello[0] == frame.request_id and not self._hello[1].done():
                fields = cbor_msgs.decode_response(SerialMsg.HELLO, frame.payload)
                self.credits = fields["caps"]["credits"] + frame.credits  # installed before the next frame
                self.owed = 0
                self.channel = None
                self._fail_sealed(Status.AUTH_REQUIRED)
                self._hello[1].set_result(frame)
            return
        self.credits += frame.credits
        self.owed += 1
        self._credit.set()
        if frame.flags & SerialFlag.RESPONSE:
            if frame.type == SerialMsg.SECURE_DATA and frame.request_id == 0:
                status = Status(cbor_msgs.decode_response(SerialMsg.SECURE_DATA, frame.payload)["status"])
                self.secure_failures.append(status)
                self.channel = None
                self._fail_sealed(status)
            else:
                channel = self._opening.get(frame.request_id)
                if channel is not None:
                    reply = cbor_msgs.decode_response(SerialMsg.SECURE_OPEN, frame.payload)
                    if reply["status"] == Status.OK:
                        channel.finish(reply["data"])
                        self.channel = channel
                future = self._plain.get(frame.request_id)
                if future is not None and not future.done():
                    future.set_result(frame)
                else:
                    self.stats["unmatched"] += 1
        elif frame.flags & SerialFlag.EVENT:
            self.plain_events.append(frame)  # a v2 device never sends a plaintext event
        elif frame.type == SerialMsg.SECURE_DATA:
            self._on_sealed(frame)
        else:
            self.stats["unmatched"] += 1
        self._changed.set()
        self._maybe_grant()

    def _on_sealed(self, frame: Frame) -> None:
        channel = self.channel
        if channel is None or not channel.open:
            self.stats["stray_sealed"] += 1
            return
        try:
            message = channel.unseal(cbor_msgs.decode_request(SerialMsg.SECURE_DATA, frame.payload)["data"])
        except (SecureChannelError, cbor_msgs.CborError):
            self.channel = None
            self._fail_sealed(Status.AUTH_FAILED)
            return
        fields = SecureChannel.decode(message)
        if message.flags & SerialFlag.RESPONSE:
            future = self._sealed.pop(message.request_id, None)
            if future is not None and not future.done():
                future.set_result(fields)
            else:
                self.stats["unmatched"] += 1
        elif message.flags & SerialFlag.EVENT:
            event = Event(SerialMsg(message.type), fields)
            self.events.append(event)
            if event.msg == SerialMsg.EVT_TUNNEL:
                self.tunnel_queue(fields["tunnel"]).put_nowait(fields)
            if self.auto_ack and "seq" in fields:
                self._spawn(self._ack(fields["seq"]))

    def _fail_sealed(self, status: Status) -> None:
        pending, self._sealed = self._sealed, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(SessionFailed(status))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _ack(self, seq: int) -> None:
        with contextlib.suppress(SessionFailed, TimeoutError, AssertionError):
            await self.call(SerialMsg.EVENT_ACK, {"seq": seq})

    def _maybe_grant(self) -> None:
        if self._granting or self.owed < GRANT_EVERY:
            return
        self._granting = True
        self.stats["grant_pings"] += 1

        async def ping() -> None:
            try:
                rid = next(self._rids) % 0xFFFF or next(self._rids)
                await self._send(SerialMsg.PING, rid, lambda: b"", internal=True)
            finally:
                self._granting = False

        self._spawn(ping())

    async def wait_event(self, msg: SerialMsg, *, timeout: float = 10.0, since: int = 0, **fields: Any) -> Event:
        """The first event of type ``msg`` (from index ``since``) whose fields include ``fields``."""

        async def scan() -> Event:
            while True:
                for event in self.events[since:]:
                    if event.matches(msg, **fields):
                        return event
                self._changed.clear()
                await self._changed.wait()

        return await asyncio.wait_for(scan(), timeout)

    def count(self, msg: SerialMsg, **fields: Any) -> int:
        return sum(1 for e in self.events if e.matches(msg, **fields))

    # -- tunnels ----------------------------------------------------------------------------------

    def tunnel_queue(self, tunnel: int) -> asyncio.Queue[dict[str, Any]]:
        return self._tunnels.setdefault(tunnel, asyncio.Queue())

    async def open_tunnel(self, bridge: int, tag_id: int = 0, duration_s: int = 60) -> HostTunnel:
        reply = await self.ok(SerialMsg.TUNNEL_OPEN, {"op_id": self.new_op_id(), "bridge": bridge, "tag_id": tag_id,
                                                      "duration_s": duration_s})
        return HostTunnel(self, reply["tunnel"], bridge, tag_id)


class HostTunnel:
    """The worker's end of one tunnel: ``EVT_TUNNEL`` in, ``TUNNEL_SEND`` out, a PAIR-endpoint session over it."""

    def __init__(self, host: V2Host, tunnel: int, bridge: int, tag_id: int) -> None:
        self.host = host
        self.tunnel = tunnel
        self.bridge = bridge
        self.tag_id = tag_id
        self.queue = host.tunnel_queue(tunnel)
        self.ident: Ident2 | None = None
        self.channel: SecureChannel | None = None

    async def event(self, timeout: float) -> dict[str, Any]:
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def wait_open(self, timeout: float = 30.0) -> Ident2:
        event = await self.event(timeout)
        if event["state"] == TunnelState.CLOSED:
            raise TunnelClosed(event["status"])
        assert event["state"] == TunnelState.OPEN and (event["bridge"], event["tag_id"]) == (self.bridge, self.tag_id)
        self.ident = Ident2.unpack(event["data"])
        return self.ident

    async def recv(self, timeout: float = 10.0) -> bytes:
        event = await self.event(timeout)
        if event["state"] == TunnelState.CLOSED:
            raise TunnelClosed(event["status"])
        assert event["state"] == TunnelState.DATA
        return bytes(event["data"])

    async def wait_closed(self, timeout: float = 30.0) -> Status:
        while True:
            event = await self.event(timeout)
            if event["state"] == TunnelState.CLOSED:
                return Status(event["status"])

    async def send(self, message: bytes) -> None:
        """TUNNEL_SEND; the gateway takes one message at a time (BUSY while the previous one is in flight)."""
        for _ in range(200):
            reply = await self.host.call(SerialMsg.TUNNEL_SEND, {"tunnel": self.tunnel, "data": message})
            if reply["status"] != Status.BUSY:
                assert reply["status"] == Status.OK, reply
                return
            await asyncio.sleep(0.005)
        raise AssertionError("the tunnel stayed busy")

    async def handshake(self, worker: Worker) -> SecureChannel:
        assert self.ident is not None
        channel = SecureChannel(worker.priv, self.ident.ik, self.ident.device_id, Link.TUNNEL)
        await self.send(pair_message(PairKind.HANDSHAKE, channel.message1()))
        kind, body = parse_pair_message(await self.recv())
        if kind == PairKind.CLOSE:
            raise SessionFailed(body[0] if body else Status.INVALID, "handshake")
        assert kind == PairKind.HANDSHAKE
        channel.finish(body)
        self.channel = channel
        return channel

    async def call(self, msg: SerialMsg, fields: dict[str, Any] | None = None, *,
                   timeout: float = 10.0) -> dict[str, Any]:
        channel = self.channel
        assert channel is not None and channel.open
        rid, sealed = channel.seal_request(msg, fields or {})
        await self.send(pair_message(PairKind.TRANSPORT, sealed))
        kind, body = parse_pair_message(await self.recv(timeout))
        if kind == PairKind.CLOSE:
            self.channel = None
            raise SessionFailed(body[0] if body else Status.INVALID, msg.name)
        assert kind == PairKind.TRANSPORT
        answer = channel.unseal(body)
        assert answer.request_id == rid and answer.flags & SerialFlag.RESPONSE
        return SecureChannel.decode(answer)

    async def close(self, *, say_goodbye: bool = True) -> None:
        """PairKind.CLOSE, then TUNNEL_CLOSE. The endpoint closes the tunnel itself after a CLOSE (a tag drops the
        link), so the TUNNEL_CLOSE may find it gone already: NOT_FOUND is fine here."""
        if say_goodbye:
            await self.send(pair_message(PairKind.CLOSE, bytes([Status.OK])))
        reply = await self.host.call(SerialMsg.TUNNEL_CLOSE, {"tunnel": self.tunnel})
        assert reply["status"] in (Status.OK, Status.NOT_FOUND), reply

    # -- v2 flows over the endpoint -------------------------------------------------------------

    async def challenge(self) -> tuple[bytes, dict[str, Any]]:
        status = await self.call(SerialMsg.STATUS)
        assert status["status"] == Status.OK, status
        return status["challenge"], status

    async def pair(self, auth: Authority, worker: Worker, secret: bytes, op_key: bytes) -> tuple[Status, bool]:
        """PAIR with the setup proof; returns the status and whether ``proof_d`` checked out."""
        channel, ident = self.channel, self.ident
        assert channel is not None and ident is not None
        challenge, status = await self.challenge()
        grant = auth.grant(GrantOp.PAIR, ident.device_id, NodeRole(ident.role), worker.pub, status["gen"], challenge)
        proof_s, k_set = channel.setup_proof(secret, grant["grant"])
        reply = await self.call(SerialMsg.PAIR, {**grant, "proof": proof_s, "op_key": op_key})
        result = Status(reply["status"])
        return result, result == Status.OK and channel.check_device_proof(k_set, proof_s, reply["proof"])

    async def grant_op(self, auth: Authority, worker: Worker, msg: SerialMsg, op: GrantOp,
                       **extra: Any) -> dict[str, Any]:
        """A grant-carrying request (REKEY, RELEASE) with a fresh challenge."""
        ident = self.ident
        assert ident is not None
        challenge, status = await self.challenge()
        grant = auth.grant(op, ident.device_id, NodeRole(ident.role), worker.pub, status["gen"], challenge)
        return await self.call(msg, {**grant, **extra})


# -- gateway flows ----------------------------------------------------------------------------------


async def claim(host: V2Host, auth: Authority, worker: Worker) -> dict[str, Any]:
    """connect-setup.md 8.1 step 5: IDENTIFY, SECURE_OPEN, CLAIM."""
    ident = await host.identify()
    await host.secure_open(worker, ident["ik"], ident["device_id"])
    grant = auth.grant(GrantOp.CLAIM, ident["device_id"], NodeRole.GATEWAY, worker.pub, ident["gen"],
                       ident["challenge"])
    return await host.call(SerialMsg.CLAIM, grant)


async def open_session(host: V2Host, worker: Worker) -> dict[str, Any]:
    """IDENTIFY + SECURE_OPEN; returns the IDENTIFY answer."""
    ident = await host.identify()
    await host.secure_open(worker, ident["ik"], ident["device_id"])
    return ident


async def provision(host: V2Host, uuid: bytes, static_oob: bytes | None, *, name: str = "") -> dict[str, Any]:
    op = host.new_op_id()
    fields: dict[str, Any] = {"op_id": op, "uuid": uuid}
    if name:
        fields["name"] = name
    if static_oob is not None:
        fields["static_oob"] = static_oob
    await host.ok(SerialMsg.PROVISION, fields, expect=Status.ACCEPTED)
    return (await host.wait_event(SerialMsg.EVT_PROVISIONED, op_id=op, timeout=15)).fields


async def configure(host: V2Host, addr: int) -> dict[str, Any]:
    op = host.new_op_id()
    await host.ok(SerialMsg.CONFIGURE_NODE, {"op_id": op, "addr": addr, "relay": True, "ttl": 5},
                  expect=Status.ACCEPTED)
    return (await host.wait_event(SerialMsg.EVT_NODE_CONFIGURED, op_id=op, timeout=15)).fields


def static_oob(payload: SetupPayload, device_id: bytes) -> bytes:
    return identity.static_oob(payload.secret, device_id)


async def add_bridge(host: V2Host, auth: Authority, worker: Worker, code: str, mk: bytes) -> tuple[int, bytes]:
    """connect-setup.md 8.2 from a setup code: scan for the beacon, provision with the static OOB, configure,
    tunnel to the bridge's own endpoint, PAIR (proof_d checked). Returns (mesh address, device_id)."""
    from cremind_tag.secure.codes import parse_code

    label = parse_code(code, role=NodeRole.BRIDGE)
    await host.ok(SerialMsg.SCAN_UNPROV, {"duration_s": 5})
    beacon = await host.wait_event(SerialMsg.EVT_UNPROV_BEACON, timeout=15)
    for _ in range(20):
        found = [e for e in host.events if e.msg == SerialMsg.EVT_UNPROV_BEACON
                 and identity.short_id(e.fields["uuid"]) == label.short_id]
        if found:
            beacon = found[0]
            break
        await asyncio.sleep(0.05)
    device_id = beacon.fields["uuid"]
    assert identity.short_id(device_id) == label.short_id
    provisioned = await provision(host, device_id, static_oob(label, device_id))
    assert provisioned["status"] == Status.OK, provisioned
    addr = provisioned["addr"]
    assert (await configure(host, addr))["status"] == Status.OK
    tunnel = await host.open_tunnel(addr)
    ident = await tunnel.wait_open()
    assert ident.device_id == device_id
    await tunnel.handshake(worker)
    status, proof_ok = await tunnel.pair(auth, worker, label.secret, mk)
    assert status == Status.OK and proof_ok
    await tunnel.close()
    return addr, device_id
