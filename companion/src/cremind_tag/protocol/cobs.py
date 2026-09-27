"""Consistent Overhead Byte Stuffing for the serial link (docs/protocol.md §1.1).

Standard Cheshire–Baker COBS: the data is split at zero bytes into blocks of
at most 254 non-zero bytes, each preceded by a code byte ``len + 1``. A code
below 0xFF implies a zero after its block (except after the last block); code
0xFF implies none. The encoder never emits a trailing 0x01 block after a final
0xFF block, so 254 non-zero bytes encode to 255 bytes; decoders accept either
form. Encoded data contains no 0x00, which delimits frames on the wire.
"""

from __future__ import annotations

from .ids import SERIAL_MAX_FRAME


class CobsError(ValueError):
    """Malformed COBS data (a zero byte inside, or a truncated block)."""


def encode(data: bytes) -> bytes:
    """COBS-encode ``data`` (without the 0x00 delimiter)."""
    out = bytearray()
    start = 0
    size = len(data)
    while True:
        end = start
        while end < size and data[end] != 0 and end - start < 254:
            end += 1
        out.append(end - start + 1)
        out += data[start:end]
        if end == size:
            return bytes(out)
        if end - start == 254:
            start = end  # a full block implies no zero: the next byte starts a block
        else:
            start = end + 1  # the zero that ended the block is implied by its code


def decode(data: bytes) -> bytes:
    """Decode one COBS-encoded frame (without its delimiter)."""
    if not data:
        raise CobsError("empty input has no code byte")
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            raise CobsError(f"zero byte at offset {i}")
        block = data[i + 1 : i + code]
        if len(block) != code - 1:
            raise CobsError(f"block at offset {i} truncated")
        if 0 in block:
            raise CobsError(f"zero byte inside the block at offset {i}")
        out += block
        i += code
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


class StreamDecoder:
    """Incremental decoder for a 0x00-delimited COBS byte stream.

    ``feed`` returns the frames completed by a chunk. A frame whose decoded size
    would exceed ``max_frame`` is discarded while it is received and the
    decoder resynchronises on the next 0x00 (``oversize`` counts them);
    malformed frames (a delimiter inside a block) count as ``errors``. Empty
    frames (consecutive delimiters) are ignored.
    """

    def __init__(self, max_frame: int = SERIAL_MAX_FRAME) -> None:
        self.max_frame = max_frame
        self.oversize = 0
        self.errors = 0
        self._out = bytearray()
        self._remaining = 0  # data bytes left in the current block
        self._zero_pending = False  # the current block implies a zero if another follows
        self._started = False
        self._discarding = False

    def _reset(self) -> None:
        self._out.clear()
        self._remaining = 0
        self._zero_pending = False
        self._started = False
        self._discarding = False

    def feed(self, chunk: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for byte in chunk:
            if byte == 0:
                if self._discarding:
                    self.oversize += 1
                elif self._remaining:
                    self.errors += 1
                elif self._started:
                    frames.append(bytes(self._out))
                self._reset()
                continue
            if self._discarding:
                continue
            if self._remaining == 0:
                if self._zero_pending:
                    self._out.append(0)
                self._started = True
                self._remaining = byte - 1
                self._zero_pending = byte != 0xFF
            else:
                self._out.append(byte)
                self._remaining -= 1
            if len(self._out) > self.max_frame:
                self._reset()
                self._discarding = True
        return frames
