"""Secure-message framing and tunnel fragmentation (connect-setup.md 3.2, 6, 7.2).

A **secure message** is the plaintext of one Noise transport message::

    type u8 | flags u8 | request_id u16le | CBOR map (serial.cbor_keys)

``type`` and ``flags`` come from the serial catalogue (``SerialMsg``,
``SerialFlag``), so a gateway's secure session carries the whole serial
protocol and a bridge's or tag's secure endpoint the v2 subset.

A **tunnel / PAIR message** is ``kind u8 | body`` (``PairKind``): a Noise
handshake message, a transport message, or a close ``{status u8}``. On the
mesh it travels as ``TUNNEL_DATA`` / ``TUNNEL_UP`` fragments of at most
``TUNNEL_DATA_MAX`` bytes (``seq`` from 0, ``START`` on the first, ``END`` on the
last); on BLE as ``PAIR`` characteristic fragments.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from cremind_tag.protocol.ids import TUNNEL_DATA_MAX, TUNNEL_MSG_MAX, PairKind

_HEAD = struct.Struct("<BBH")
FRAG_START = 0x01
FRAG_END = 0x02
FRAG_CLOSE = 0x80


class SecureFrameError(ValueError):
    """A secure message or tunnel fragment was malformed."""


@dataclass(frozen=True, slots=True)
class SecureMessage:
    type: int
    flags: int
    request_id: int
    payload: bytes = b""

    def pack(self) -> bytes:
        return _HEAD.pack(self.type, self.flags, self.request_id) + bytes(self.payload)

    @classmethod
    def unpack(cls, raw: bytes) -> SecureMessage:
        if len(raw) < _HEAD.size:
            raise SecureFrameError("secure message shorter than its header")
        mtype, flags, request_id = _HEAD.unpack_from(raw)
        return cls(mtype, flags, request_id, bytes(raw[_HEAD.size:]))


def pair_message(kind: PairKind | int, body: bytes = b"") -> bytes:
    return bytes([int(kind)]) + bytes(body)


def parse_pair_message(raw: bytes) -> tuple[PairKind, bytes]:
    if not raw:
        raise SecureFrameError("empty tunnel message")
    try:
        kind = PairKind(raw[0])
    except ValueError:
        raise SecureFrameError(f"unknown tunnel message kind {raw[0]}") from None
    return kind, bytes(raw[1:])


def fragments(message: bytes, max_data: int = TUNNEL_DATA_MAX) -> list[tuple[int, int, bytes]]:
    """``(seq, flags, data)`` fragments of one tunnel message."""
    if not message or len(message) > TUNNEL_MSG_MAX:
        raise SecureFrameError(f"a tunnel message is 1..{TUNNEL_MSG_MAX} bytes")
    parts = [message[i:i + max_data] for i in range(0, len(message), max_data)]
    out = []
    for seq, part in enumerate(parts):
        flags = (FRAG_START if seq == 0 else 0) | (FRAG_END if seq == len(parts) - 1 else 0)
        out.append((seq, flags, bytes(part)))
    return out


class Reassembler:
    """Rebuild tunnel messages from in-order fragments; a gap drops the message."""

    def __init__(self, limit: int = TUNNEL_MSG_MAX):
        self._limit = limit
        self._buf: bytearray | None = None
        self._next = 0

    def reset(self) -> None:
        self._buf = None
        self._next = 0

    def feed(self, seq: int, flags: int, data: bytes) -> bytes | None:
        if flags & FRAG_START:
            self._buf = bytearray()
            self._next = 0
        if self._buf is None or seq != self._next:
            self.reset()
            raise SecureFrameError("tunnel fragment out of order")
        if len(self._buf) + len(data) > self._limit:
            self.reset()
            raise SecureFrameError("tunnel message too long")
        self._buf += data
        self._next += 1
        if flags & FRAG_END:
            out = bytes(self._buf)
            self.reset()
            return out
        return None
