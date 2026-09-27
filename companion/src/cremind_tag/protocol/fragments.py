"""GATT fragmentation of CTRL, DATA and STATUS messages (docs/protocol.md §5.3).

Every ATT value is ``header(1) | payload``; the header holds START (0x80),
END (0x40) and a 6-bit SEQ that each sender increments per fragment, per
characteristic and direction, from 0 on connect. Senders fill every fragment
but the last to the maximum payload. Any violation aborts the session with
``INVALID``; the reassembler is unusable afterwards.
"""

from __future__ import annotations

from .ids import (
    FRAG_END,
    FRAG_PAYLOAD_MAX,
    FRAG_SEQ_MASK,
    FRAG_START,
    TAG_CTRL_MSG_MAX,
    TAG_RECORD_WIRE_MAX,
    GattChr,
    Status,
)

# Longest reassembled message per characteristic.
MAX_MESSAGE: dict[GattChr, int] = {
    GattChr.CTRL: TAG_CTRL_MSG_MAX,
    GattChr.DATA: TAG_RECORD_WIRE_MAX,
    GattChr.STATUS: TAG_RECORD_WIRE_MAX,
}


class FragmentError(ValueError):
    """Fragmentation rule violated; the session aborts with ``status``."""

    status = Status.INVALID


class Fragmenter:
    """Splits messages into ATT values for one characteristic and direction."""

    def __init__(self, max_message: int, max_payload: int = FRAG_PAYLOAD_MAX) -> None:
        if max_payload < 1:
            raise ValueError("max_payload must be at least 1")
        self.max_message = max_message
        self.max_payload = max_payload
        self.seq = 0

    def split(self, message: bytes) -> list[bytes]:
        if not message:
            raise FragmentError("a message carries at least its type byte")
        if len(message) > self.max_message:
            raise FragmentError(f"message of {len(message)} bytes exceeds {self.max_message}")
        chunks = [message[i : i + self.max_payload] for i in range(0, len(message), self.max_payload)]
        values = []
        for i, chunk in enumerate(chunks):
            header = self.seq
            if i == 0:
                header |= FRAG_START
            if i == len(chunks) - 1:
                header |= FRAG_END
            values.append(bytes([header]) + chunk)
            self.seq = (self.seq + 1) & FRAG_SEQ_MASK
        return values


class Reassembler:
    """Reassembles ATT values received on one characteristic and direction."""

    def __init__(self, max_message: int, seq: int = 0) -> None:
        self.max_message = max_message
        self._seq = seq  # SEQ expected next; 0 right after connect
        self._buf: bytearray | None = None

    def feed(self, value: bytes) -> bytes | None:
        """Consume one ATT value; return the message it completes, if any."""
        if len(value) < 2:
            raise FragmentError("empty fragment")
        header = value[0]
        seq = header & FRAG_SEQ_MASK
        if seq != self._seq:
            raise FragmentError(f"sequence gap: expected {self._seq}, got {seq}")
        self._seq = (seq + 1) & FRAG_SEQ_MASK
        if header & FRAG_START:
            if self._buf is not None:
                raise FragmentError("START in the middle of a message")
            self._buf = bytearray()
        elif self._buf is None:
            raise FragmentError("continuation without START")
        self._buf += value[1:]
        if len(self._buf) > self.max_message:
            raise FragmentError(f"message longer than {self.max_message} bytes")
        if not header & FRAG_END:
            return None
        message, self._buf = bytes(self._buf), None
        return message
