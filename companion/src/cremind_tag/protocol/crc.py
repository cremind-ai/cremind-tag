"""CRC-32/IEEE as used by serial frames, the enrollment blob and font packs.

Parameters (docs/protocol.md §1.1): polynomial 0x04C11DB7 reflected
(0xEDB88320), init 0xFFFFFFFF, xorout 0xFFFFFFFF, check value
``crc32(b"123456789") == 0xCBF43926``. This is exactly ``zlib.crc32``.
"""

from __future__ import annotations

import zlib

CHECK_INPUT = b"123456789"
CHECK_VALUE = 0xCBF43926


def crc32(data: bytes, crc: int = 0) -> int:
    """CRC-32/IEEE of ``data``; pass a previous result as ``crc`` to continue."""
    return zlib.crc32(data, crc) & 0xFFFFFFFF
