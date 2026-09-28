"""Ask the device on a serial port who it is, without the host link layer (docs/connect-setup.md §5, §11.6).

The supervisor maps each attached port to a ``device_id`` before any worker
opens it. A probe:

1. opens the port **exclusively** with ``serial.serial_for_url`` (so simulator
   ``socket://`` endpoints work too) — a port another program holds is ``busy``;
2. sends ``HELLO {proto: 1, name: "cremind-connect probe"}`` (granting the device
   a generous send window: it may re-send retained events first; they are
   ignored and **never acknowledged** — they belong to the gateway's worker);
3. sends plaintext ``IDENTIFY {}`` and reads the answer;
4. closes the port — always.

Each answer gets ``timeout`` seconds (2 s, protocol.md §1.3); HELLO is tried
twice, since a device that has just enumerated may miss the first one.

Outcomes: a :class:`GatewayIdentity` (any v2 role — the caller decides whether
a bridge's maintenance port or an owned gateway is usable), or a ``reason``:

``v1_firmware``  HELLO works but ``IDENTIFY`` answers ``UNSUPPORTED`` or ``proto < 2``
``busy``         the port cannot be opened (in use; on Windows "access denied" means in use)
``no_access``    POSIX permission denied (Linux: the udev rule is missing, see udev.py)
``gone``         the port disappeared
``no_answer``    nothing answered in time (not a Cremind Tag device, or it is hung)
``error``        an answer that breaks the protocol (malformed, inconsistent identity)
"""

from __future__ import annotations

import errno
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..protocol import cbor_msgs
from ..protocol.ids import PROTO_VERSION, SECURE_PROTO_VERSION, NodeRole, OwnerState, SerialFlag, SerialMsg, Status
from ..protocol.serial_frame import Frame, FrameReader, frame_to_wire
from ..secure.identity import device_id as derive_device_id

log = logging.getLogger(__name__)

PROBE_NAME = "cremind-connect probe"
ANSWER_TIMEOUT_S = 2.0
HELLO_ATTEMPTS = 2
HOST_GRANT = 60
BAUDRATE = 115200

V1_FIRMWARE = "v1_firmware"
BUSY = "busy"
NO_ACCESS = "no_access"
GONE = "gone"
NO_ANSWER = "no_answer"
ERROR = "error"

Opener = Callable[[str], Any]


@dataclass(frozen=True)
class GatewayIdentity:
    """A v2 device's plaintext ``IDENTIFY`` answer (docs/connect-setup.md §5)."""

    device_id: bytes
    ik: bytes
    role: int
    proto: int
    fw: str
    build: str
    board: int
    owner_state: int
    gen: int
    authority_id: bytes | None
    challenge: bytes

    @property
    def device_id_hex(self) -> str:
        return self.device_id.hex()

    @property
    def is_gateway(self) -> bool:
        return self.role == NodeRole.GATEWAY

    @property
    def role_name(self) -> str:
        try:
            return NodeRole(self.role).name.lower()
        except ValueError:
            return f"role-{self.role}"

    @property
    def owner_state_name(self) -> str:
        try:
            return OwnerState(self.owner_state).name.lower()
        except ValueError:
            return f"state-{self.owner_state}"

    def as_json(self, *, include_challenge: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "device_id": self.device_id.hex(), "ik": self.ik.hex(), "role": self.role_name, "proto": self.proto,
            "fw": self.fw, "build": self.build, "board": self.board, "owner_state": self.owner_state_name,
            "gen": self.gen, "authority_id": self.authority_id.hex() if self.authority_id else None,
        }
        if include_challenge:
            out["challenge"] = self.challenge.hex()
        return out


@dataclass(frozen=True)
class ProbeResult:
    device: str
    identity: GatewayIdentity | None = None
    reason: str | None = None
    detail: str = ""
    hello_fw: str | None = None
    """The firmware version HELLO reported (v1 devices included)."""

    @property
    def ok(self) -> bool:
        return self.identity is not None

    def as_json(self, *, include_challenge: bool = True) -> dict[str, Any]:
        return {"device": self.device, "reason": self.reason, "detail": self.detail, "hello_fw": self.hello_fw,
                "identity": self.identity.as_json(include_challenge=include_challenge) if self.identity else None}


def open_port(device: str) -> Any:
    """Open ``device`` exclusively (``COM7``, ``/dev/ttyACM0`` or a pyserial URL)."""
    import serial

    return serial.serial_for_url(device, baudrate=BAUDRATE, timeout=0.05, write_timeout=ANSWER_TIMEOUT_S,
                                 exclusive=True)


def classify_open_error(device: str, exc: BaseException) -> str:
    """The probe reason for a port that could not be opened."""
    text = str(exc).lower()
    code = getattr(exc, "errno", None)
    if device.lower().startswith("socket://"):
        return NO_ANSWER  # nothing listens there (a simulator that is not running)
    if "exclusively lock" in text or code in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK) or "busy" in text:
        return BUSY
    if sys.platform == "win32" and (code in (errno.EACCES, errno.EPERM) or "access is denied" in text
                                    or "permissionerror" in text or "permission denied" in text):
        return BUSY  # Windows opens COM ports exclusively: "access denied" = someone has it open
    if code in (errno.EACCES, errno.EPERM) or "permission denied" in text:
        return NO_ACCESS
    if code == errno.ENOENT or "filenotfound" in text or "no such file" in text or "cannot find" in text:
        return GONE
    return BUSY


def _await_response(port: Any, reader: FrameReader, msg: SerialMsg, request_id: int, timeout: float) -> Frame | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = getattr(port, "in_waiting", 0) or 0
        chunk = port.read(max(1, waiting))
        if not chunk:
            continue
        for frame in reader.feed(chunk):
            if frame.flags & SerialFlag.RESPONSE and frame.type == msg and frame.request_id == request_id:
                return frame
    return None


def _identity(fields: dict[str, Any]) -> GatewayIdentity:
    missing = [k for k in ("proto", "role", "device_id", "ik", "owner_state", "gen", "challenge") if k not in fields]
    if missing:
        raise ValueError(f"IDENTIFY answer lacks {', '.join(missing)}")
    identity = GatewayIdentity(
        device_id=bytes(fields["device_id"]), ik=bytes(fields["ik"]), role=int(fields["role"]),
        proto=int(fields["proto"]), fw=str(fields.get("fw", "")), build=str(fields.get("build", "")),
        board=int(fields.get("board", 0)), owner_state=int(fields["owner_state"]), gen=int(fields["gen"]),
        authority_id=bytes(fields["authority_id"]) if fields.get("authority_id") else None,
        challenge=bytes(fields["challenge"]))
    if derive_device_id(identity.role, identity.ik) != identity.device_id:
        raise ValueError("the device id does not belong to the identity key it reported")
    return identity


def probe_port(device: str, *, timeout: float = ANSWER_TIMEOUT_S, opener: Opener | None = None) -> ProbeResult:
    """Identify the device on ``device`` (see the module docstring); never leaves the port open."""
    try:
        port = (opener or open_port)(device)
    except Exception as exc:  # serial.SerialException, OSError, ValueError (bad URL)
        reason = classify_open_error(device, exc)
        log.debug("probe %s: cannot open (%s): %s", device, reason, exc)
        return ProbeResult(device, reason=reason, detail=str(exc))
    try:
        return _probe_open(device, port, timeout)
    except Exception as exc:  # the port vanished mid-probe, a write timed out, ...
        log.debug("probe %s: failed: %s", device, exc)
        return ProbeResult(device, reason=NO_ANSWER, detail=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            port.close()
        except Exception as exc:  # noqa: BLE001 - closing must never raise out of a probe
            log.debug("probe %s: close failed: %s", device, exc)


def _probe_open(device: str, port: Any, timeout: float) -> ProbeResult:
    reader = FrameReader()
    try:
        port.reset_input_buffer()
    except Exception:  # noqa: BLE001 - optional on some URL handlers
        pass
    hello_payload = cbor_msgs.encode_request(SerialMsg.HELLO, {"proto": PROTO_VERSION, "name": PROBE_NAME})
    hello: Frame | None = None
    rid = 0
    for _attempt in range(HELLO_ATTEMPTS):
        rid += 1
        port.write(frame_to_wire(Frame(SerialMsg.HELLO, rid, hello_payload, 0, HOST_GRANT)))
        hello = _await_response(port, reader, SerialMsg.HELLO, rid, timeout)
        if hello is not None:
            break
    if hello is None:
        return ProbeResult(device, reason=NO_ANSWER, detail="no answer to HELLO")
    try:
        hello_fields = cbor_msgs.decode_response(SerialMsg.HELLO, hello.payload)
    except cbor_msgs.CborError as exc:
        return ProbeResult(device, reason=ERROR, detail=f"malformed HELLO answer: {exc}")
    hello_fw = hello_fields.get("fw")
    if hello_fields.get("status") != Status.OK:
        return ProbeResult(device, reason=ERROR, detail=f"HELLO answered {_status(hello_fields.get('status'))}",
                           hello_fw=hello_fw)
    rid += 1
    port.write(frame_to_wire(Frame(SerialMsg.IDENTIFY, rid, b"", 0, 0)))
    answer = _await_response(port, reader, SerialMsg.IDENTIFY, rid, timeout)
    if answer is None:
        return ProbeResult(device, reason=NO_ANSWER, detail="no answer to IDENTIFY", hello_fw=hello_fw)
    try:
        fields = cbor_msgs.decode_response(SerialMsg.IDENTIFY, answer.payload)
    except cbor_msgs.CborError as exc:
        return ProbeResult(device, reason=ERROR, detail=f"malformed IDENTIFY answer: {exc}", hello_fw=hello_fw)
    status = fields.get("status")
    if status == Status.UNSUPPORTED:
        return ProbeResult(device, reason=V1_FIRMWARE, detail="IDENTIFY is not supported (protocol v1 firmware)",
                           hello_fw=hello_fw)
    if status != Status.OK:
        return ProbeResult(device, reason=ERROR, detail=f"IDENTIFY answered {_status(status)}", hello_fw=hello_fw)
    if int(fields.get("proto", 0)) < SECURE_PROTO_VERSION:
        return ProbeResult(device, reason=V1_FIRMWARE, detail=f"IDENTIFY reports protocol {fields.get('proto')}",
                           hello_fw=hello_fw)
    try:
        identity = _identity(fields)
    except (ValueError, TypeError) as exc:
        return ProbeResult(device, reason=ERROR, detail=str(exc), hello_fw=hello_fw)
    return ProbeResult(device, identity=identity, hello_fw=hello_fw)


def _status(value: Any) -> str:
    try:
        return Status(value).name
    except (ValueError, TypeError):
        return str(value)


__all__ = ["ANSWER_TIMEOUT_S", "BUSY", "ERROR", "GONE", "NO_ACCESS", "NO_ANSWER", "PROBE_NAME", "V1_FIRMWARE",
           "GatewayIdentity", "ProbeResult", "classify_open_error", "open_port", "probe_port"]
