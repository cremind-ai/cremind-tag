"""Serial frames between the companion and a gateway/bridge (docs/protocol.md §1).

Decoded frame: ``header(8) | payload(length) | crc32(4)`` with the CRC over
header and payload; on the wire it is COBS-encoded and followed by 0x00.
Receivers check, in this order: decoded size (≥ 12 and ≤ SERIAL_MAX_FRAME),
CRC, ``length`` = size − 12, ``version`` — each failure has its own error
type and counter.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import cobs
from .crc import crc32
from .ids import PROTO_VERSION, SERIAL_CRC_LEN, SERIAL_HEADER_LEN, SERIAL_MAX_FRAME, SERIAL_MAX_PAYLOAD
from .msgs import SerialHeader

MIN_FRAME = SERIAL_HEADER_LEN + SERIAL_CRC_LEN


class SerialFrameError(ValueError):
    """A decoded frame was rejected."""


class FrameLengthError(SerialFrameError):
    """Decoded size out of range or ``length`` inconsistent (``len_errors``)."""


class FrameCrcError(SerialFrameError):
    """CRC-32 mismatch (``crc_errors``)."""


class FrameVersionError(SerialFrameError):
    """Unknown ``version`` (``version_errors``)."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One serial frame; ``payload`` is a CBOR map or empty."""

    type: int
    request_id: int
    payload: bytes = b""
    flags: int = 0
    credits: int = 0
    version: int = PROTO_VERSION


def encode_frame(frame: Frame) -> bytes:
    """Decoded frame bytes: header, payload, CRC-32."""
    if len(frame.payload) > SERIAL_MAX_PAYLOAD:
        raise FrameLengthError(f"payload {len(frame.payload)} > {SERIAL_MAX_PAYLOAD}")
    header = SerialHeader(frame.version, frame.type, frame.request_id, len(frame.payload),
                          frame.flags, frame.credits).pack()
    body = header + frame.payload
    return body + crc32(body).to_bytes(SERIAL_CRC_LEN, "little")


def decode_frame(data: bytes) -> Frame:
    """Validate and parse decoded frame bytes (checks in the order of §1.1)."""
    if not MIN_FRAME <= len(data) <= SERIAL_MAX_FRAME:
        raise FrameLengthError(f"decoded size {len(data)} outside {MIN_FRAME}..{SERIAL_MAX_FRAME}")
    body, trailer = data[:-SERIAL_CRC_LEN], data[-SERIAL_CRC_LEN:]
    if crc32(body) != int.from_bytes(trailer, "little"):
        raise FrameCrcError("CRC-32 mismatch")
    header = SerialHeader.unpack(body[:SERIAL_HEADER_LEN])
    if header.length != len(body) - SERIAL_HEADER_LEN:
        raise FrameLengthError(f"length {header.length} but {len(body) - SERIAL_HEADER_LEN} payload bytes")
    if header.version != PROTO_VERSION:
        raise FrameVersionError(f"version {header.version}")
    return Frame(header.type, header.request_id, bytes(body[SERIAL_HEADER_LEN:]), header.flags,
                 header.credits, header.version)


def frame_to_wire(frame: Frame) -> bytes:
    """COBS-encoded frame followed by the 0x00 delimiter."""
    return cobs.encode(encode_frame(frame)) + b"\x00"


class FrameReader:
    """Turns a received byte stream into frames, counting what it drops."""

    def __init__(self) -> None:
        self._cobs = cobs.StreamDecoder(SERIAL_MAX_FRAME)
        self.crc_errors = 0
        self.len_errors = 0
        self.version_errors = 0

    @property
    def cobs_errors(self) -> int:
        return self._cobs.errors

    @property
    def oversize(self) -> int:
        return self._cobs.oversize

    def feed(self, chunk: bytes) -> list[Frame]:
        frames: list[Frame] = []
        for raw in self._cobs.feed(chunk):
            try:
                frames.append(decode_frame(raw))
            except FrameCrcError:
                self.crc_errors += 1
            except FrameLengthError:
                self.len_errors += 1
            except FrameVersionError:
                self.version_errors += 1
        return frames
