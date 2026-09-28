"""Protocol v2 on the simulated devices (docs/connect-setup.md; docs/simulator.md "Protocol v2").

The simulated v2 gateway, bridges and tags run the reference secure endpoint,
:class:`cremind_tag.secure.device.SecureDevice` (ownership record, single-use
challenges, the Noise IK responder, grants and proofs). This module adapts it
to the simulator's links; it implements no protocol rule of its own beyond the
glue the reference leaves to the device:

- :class:`LinkSessions`: one ``SecureDevice``, one Noise session per link. A
  bridge's USB maintenance port (``Link.SERIAL``) and its mesh tunnel endpoint
  (``Link.TUNNEL``) each keep their own session; the ownership record and the
  single-use challenge are the device's (an ``IDENT`` read on one link retires a
  challenge drawn on the other, as §5 says).
- :class:`SecureSerial`: the v2 layer of a serial port (§5): plaintext
  ``IDENTIFY`` and ``SECURE_OPEN``, the ``SECURE_DATA`` carrier, and which other
  plaintext requests the device still answers. Used by
  :class:`~cremind_tag.sim.device.DeviceEndpoint`.
- :class:`PairEndpoint`: a secure endpoint reached by ``kind | body`` messages
  (§6, §7.2): a bridge's own endpoint behind a mesh tunnel, a tag's ``PAIR``
  characteristic.
- :func:`generate_keys`: a device's factory identity drawn from the simulator
  seed, so ids and setup codes are reproducible.

``SecureDevice`` keeps one ``session`` attribute; :class:`LinkSessions` swaps
the right link's session in for the duration of a synchronous call. Never await
inside :meth:`LinkSessions.use`.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..protocol import cbor_msgs
from ..protocol.ids import Link, NodeRole, OwnerState, PairKind, SerialFlag, SerialMsg, Status
from ..secure.codes import SetupPayload
from ..secure.device import DeviceKeys, Outcome, OwnerRecord, SecureDevice
from ..secure.messages import SecureFrameError, SecureMessage, pair_message, parse_pair_message
from ..secure.noise import NoiseError
from .core import rng_stream

if TYPE_CHECKING:
    from .device import Reply

SECURE_MESSAGES = frozenset({SerialMsg.STATUS, SerialMsg.CLAIM, SerialMsg.RECOVER, SerialMsg.RELEASE, SerialMsg.PAIR,
                             SerialMsg.REKEY, SerialMsg.MAINT_AUTH, SerialMsg.RECOMMISSION})
"""The v2 secure messages ``SecureDevice.handle`` answers (connect-setup.md 5.1)."""

LINK_LEVEL = frozenset({SerialMsg.HELLO, SerialMsg.IDENTIFY, SerialMsg.SECURE_OPEN, SerialMsg.SECURE_DATA})
"""Serial link messages that never travel inside a session (answered ``UNSUPPORTED`` there)."""

PLAINTEXT_ALWAYS = frozenset({SerialMsg.HELLO, SerialMsg.PING, SerialMsg.IDENTIFY, SerialMsg.SECURE_OPEN,
                              SerialMsg.SECURE_DATA})
"""connect-setup.md 4.2, "Any, plaintext"."""

MESH_OOB_ON_BOX = 0x0800
"""Unprovisioned-beacon OOB information of a v2 bridge: static OOB "on box" (Mesh Profile 3.9.2, bit 11)."""

InnerHandler = Callable[[SerialMsg, dict[str, Any]], Awaitable["Reply | dict[str, Any]"]]


def generate_keys(seed: int, role: NodeRole, label: object, *, board: int, labelled: bool = True) -> DeviceKeys:
    """A device's factory identity (identity key, label secret) drawn from ``(seed, role, label)``.

    ``labelled=False`` gives a bridge that left the factory without a setup
    secret (``FACTORY_SETUP`` stores one over its maintenance port).
    """
    stream = rng_stream(seed, "v2-keys", role.name, label)
    keys = DeviceKeys.generate(role, board=board, rng=stream.randbytes)
    return keys if labelled else replace(keys, factory_secret=None)


def new_secure_device(keys: DeviceKeys, seed: int, label: object, record: OwnerRecord | None = None) -> SecureDevice:
    """A ``SecureDevice`` whose challenges, fresh setup secrets and Noise ephemerals come from a seeded stream
    (docs/simulator.md: a simulated session is predictable by design)."""
    stream = rng_stream(seed, "v2-device", keys.role.name, label)
    return SecureDevice(keys, record, rng=stream.randbytes, ephemeral=lambda: stream.randbytes(32))


def current_setup_secret(device: SecureDevice) -> bytes | None:
    """The secret the device pairs with now: the fresh one a release or recommission armed, else the label's."""
    record = device.record
    if record.state == OwnerState.RELEASED:
        return record.override_secret
    return device.keys.factory_secret


def setup_payload(device: SecureDevice) -> SetupPayload | None:
    secret = current_setup_secret(device)
    if secret is None or device.role not in (NodeRole.BRIDGE, NodeRole.TAG):
        return None
    return SetupPayload(device.role, device.keys.short_id, secret)


def device_state(device: SecureDevice) -> dict[str, Any]:
    """What a v2 device keeps in flash, for the simulator's state file."""
    return {"keys": device.keys.to_json(), "owner": device.record.to_json()}


def load_device_state(device: SecureDevice, data: dict[str, Any] | None, *, adopt_identity: bool = False) -> bool:
    """Restore the ownership record (and a label secret stored later) from :func:`device_state` output.

    The stored identity must be the device's own unless ``adopt_identity`` (the
    gateway: its flash wins over the seed). Returns whether anything was loaded.
    """
    if not data:
        return False
    keys = DeviceKeys.from_json(data["keys"]) if data.get("keys") else None
    if keys is not None:
        if keys.role != device.role:
            return False
        if keys.ik_priv != device.keys.ik_priv:
            if not adopt_identity:
                return False
            device.keys = keys
        elif keys.factory_secret != device.keys.factory_secret:
            device.keys = replace(device.keys, factory_secret=keys.factory_secret)
    if data.get("owner"):
        device.record = OwnerRecord.from_json(data["owner"])
    device.challenge = None
    device.session = None
    return True


def outcome_fields(outcome: Outcome) -> dict[str, Any]:
    return {"status": int(outcome.status), **outcome.fields}


class LinkSessions:
    """One :class:`SecureDevice`, one Noise session per link (see the module docstring)."""

    def __init__(self, device: SecureDevice) -> None:
        self.device = device
        self._slots: dict[int, Any] = {}

    @contextlib.contextmanager
    def use(self, link: int) -> Iterator[SecureDevice]:
        """The device with ``link``'s session in place (synchronous use only)."""
        dev = self.device
        dev.session = self._slots.get(int(link))
        try:
            yield dev
        finally:
            self._slots[int(link)] = dev.session
            dev.session = None

    def has_session(self, link: int) -> bool:
        return self._slots.get(int(link)) is not None

    def drop(self, link: int) -> None:
        self._slots.pop(int(link), None)

    def clear(self) -> None:
        """A reboot: every session is RAM."""
        self._slots.clear()
        self.device.session = None
        self.device.challenge = None


class SecureSerial:
    """The v2 layer of one serial port (connect-setup.md 5): one session per connection over ``Link.SERIAL``.

    ``plaintext(msg)`` says which requests the device still answers without a
    session (the rest answer ``AUTH_REQUIRED``); ``handler`` answers the
    messages that arrive inside a session (after the endpoint unsealed and
    decoded them); ``events()`` says whether events may flow into the session.
    ``session_id`` changes whenever a session is opened or dropped, so sealed
    messages queued for an older session are never sent in a newer one.
    """

    def __init__(self, sessions: LinkSessions, *, fw: str, build: str = "sim",
                 plaintext: Callable[[SerialMsg], bool], handler: InnerHandler,
                 events: Callable[[], bool] = lambda: False) -> None:
        self.sessions = sessions
        self.fw = fw
        self.build = build
        self._plaintext = plaintext
        self.handle = handler
        self._events = events
        self.session_id = 0
        self.counters: Counter[str] = Counter()

    @property
    def device(self) -> SecureDevice:
        return self.sessions.device

    @property
    def is_open(self) -> bool:
        return self.sessions.has_session(Link.SERIAL)

    def _bump(self) -> None:
        self.session_id += 1

    def plaintext_allowed(self, msg: SerialMsg) -> bool:
        return msg in PLAINTEXT_ALWAYS or self._plaintext(msg)

    def events_allowed(self) -> bool:
        return self.is_open and self._events()

    def identify(self) -> dict[str, Any]:
        """The ``IDENTIFY`` answer; draws a fresh challenge."""
        return self.device.identify_fields(fw=self.fw, build=self.build)

    def open(self, message1: bytes) -> dict[str, Any]:
        """``SECURE_OPEN``: replaces any session; answers Noise message 2."""
        self._bump()
        with self.sessions.use(Link.SERIAL) as dev:
            try:
                message2 = dev.open_session(Link.SERIAL, message1)
            except NoiseError:
                dev.close_session()
                self.counters["secure_open_failed"] += 1
                return {"status": Status.AUTH_FAILED}
        self.counters["secure_opens"] += 1
        return {"status": Status.OK, "data": message2}

    def drop(self) -> None:
        """``HELLO``, a reopened port or a failed decryption: the session ends."""
        if self.is_open:
            self.counters["sessions_dropped"] += 1
        self.sessions.drop(Link.SERIAL)
        self._bump()

    def unseal(self, ciphertext: bytes) -> bytes | None:
        """One incoming transport message; ``None`` (and the session dropped) when it does not decrypt."""
        with self.sessions.use(Link.SERIAL) as dev:
            if dev.session is None:
                self.counters["no_session"] += 1
                return None
            try:
                return dev.session.noise.decrypt(ciphertext)
            except NoiseError:
                dev.close_session()
        self.counters["decrypt_failures"] += 1
        self._bump()
        return None

    def seal(self, plaintext: bytes) -> bytes | None:
        with self.sessions.use(Link.SERIAL) as dev:
            if dev.session is None:
                return None
            return dev.session.noise.encrypt(plaintext)


@dataclass
class PairResult:
    """What one ``kind | body`` message did at a :class:`PairEndpoint`."""

    replies: list[bytes]  # messages to send back, in order
    outcome: Outcome | None = None  # a secure message's outcome (the device model acts on it)
    request: SerialMsg | None = None
    record_changed: bool = False  # persist before sending the replies
    closed: bool = False  # the worker closed the session (PairKind.CLOSE)


PairHandler = Callable[[SecureDevice, SerialMsg, dict[str, Any]], Outcome]


def _close(status: Status) -> bytes:
    return pair_message(PairKind.CLOSE, bytes([int(status)]))


class PairEndpoint:
    """A v2 secure endpoint reached by ``kind | body`` messages over ``Link.TUNNEL`` (connect-setup.md 6, 7.2).

    ``HANDSHAKE`` opens (or replaces) the session and answers Noise message 2;
    ``TRANSPORT`` carries one sealed secure message and gets the sealed answer;
    ``CLOSE`` from the worker ends the session. A handshake that fails answers
    ``CLOSE{AUTH_FAILED}``, a transport message without a session or that fails
    to decrypt ``CLOSE{AUTH_REQUIRED}`` (the session is gone: the worker opens a
    new one), a malformed message ``CLOSE{INVALID}``.
    """

    def __init__(self, sessions: LinkSessions, handler: PairHandler | None = None) -> None:
        self.sessions = sessions
        self.handler: PairHandler = handler or (lambda dev, msg, fields: dev.handle(msg, fields))
        self.counters: Counter[str] = Counter()

    @property
    def device(self) -> SecureDevice:
        return self.sessions.device

    def ident(self) -> bytes:
        """The endpoint's ``ident2`` (the first message up a tunnel, the ``IDENT`` value); a fresh challenge."""
        return self.device.ident2().pack()

    def drop(self) -> None:
        self.sessions.drop(Link.TUNNEL)

    def receive(self, message: bytes) -> PairResult:
        try:
            kind, body = parse_pair_message(message)
        except SecureFrameError:
            self.counters["malformed"] += 1
            return PairResult([_close(Status.INVALID)])
        if kind == PairKind.CLOSE:
            self.counters["closed_by_worker"] += 1
            self.drop()
            return PairResult([], closed=True)
        if kind == PairKind.HANDSHAKE:
            with self.sessions.use(Link.TUNNEL) as dev:
                try:
                    message2 = dev.open_session(Link.TUNNEL, body)
                except NoiseError:
                    dev.close_session()
                    self.counters["handshake_failed"] += 1
                    return PairResult([_close(Status.AUTH_FAILED)])
            self.counters["handshakes"] += 1
            return PairResult([pair_message(PairKind.HANDSHAKE, message2)])
        with self.sessions.use(Link.TUNNEL) as dev:
            if dev.session is None:
                self.counters["no_session"] += 1
                return PairResult([_close(Status.AUTH_REQUIRED)])
            try:
                plain = dev.session.noise.decrypt(body)
            except NoiseError:
                dev.close_session()
                self.counters["decrypt_failures"] += 1
                return PairResult([_close(Status.AUTH_REQUIRED)])
            try:
                request = SecureMessage.unpack(plain)
            except SecureFrameError:
                self.counters["malformed"] += 1
                return PairResult([_close(Status.INVALID)])
            before = dev.record
            payload, outcome, msg = self._answer(dev, request)
            answer = dev.session.noise.encrypt(
                SecureMessage(request.type, int(SerialFlag.RESPONSE), request.request_id, payload).pack())
            changed = dev.record is not before
        return PairResult([pair_message(PairKind.TRANSPORT, answer)], outcome, msg, changed)

    def _answer(self, dev: SecureDevice, request: SecureMessage) -> tuple[bytes, Outcome | None, SerialMsg | None]:
        try:
            msg = SerialMsg(request.type)
        except ValueError:
            return cbor_msgs.encode_map({"status": Status.UNSUPPORTED}), None, None
        if request.flags & (SerialFlag.RESPONSE | SerialFlag.EVENT):
            return cbor_msgs.encode_map({"status": Status.INVALID}), None, msg
        try:
            fields = cbor_msgs.decode_request(msg, request.payload)
        except cbor_msgs.CborError as exc:
            return cbor_msgs.encode_response(msg, {"status": Status.INVALID, "text": str(exc)[:120]}), None, msg
        outcome = self.handler(dev, msg, fields)
        self.counters[f"{msg.name.lower()}_{outcome.status.name.lower()}"] += 1
        return cbor_msgs.encode_response(msg, outcome_fields(outcome)), outcome, msg
