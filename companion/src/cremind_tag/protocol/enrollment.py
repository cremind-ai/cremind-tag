"""Tag enrollment blob and its Intel HEX image (docs/protocol.md §9).

The 48-byte blob (spec ``enrollment``) goes to UICR.CUSTOMER[0..11]. On both
SoC families UICR starts at 0x10001000 and CUSTOMER[0] is at offset 0x080:
nRF51 series reference manual (UICR, CUSTOMER[0..31] at 0x080) and the
nRF52810/52811/52832/52840 product specifications (UICR, CUSTOMER[n] at
0x080 + 4n).
"""

from __future__ import annotations

from .crc import crc32
from .ids import ENROLLMENT_MAGIC, TAG_SECRET_LEN, Board, Status
from .msgs import Enrollment

ENROLLMENT_VERSION = 1
CRC_OFFSET = Enrollment.LEN - 4

UICR_CUSTOMER_ADDR: dict[str, int] = {"nrf51": 0x10001080, "nrf52": 0x10001080}

# SoC family of each tag board.
BOARD_SOC: dict[Board, str] = {
    Board.LAOWU_BW_NRF51822: "nrf51",
    Board.LAOWU_BWR_NRF51802: "nrf51",
    Board.SIFEI_NRF52810: "nrf52",
    Board.HEMA_NRF52811: "nrf52",
    Board.NRF52DK_TAG: "nrf52",
}


class EnrollmentError(ValueError):
    """Missing or corrupt blob; firmware reports SECURITY_CONFIG."""

    status = Status.SECURITY_CONFIG


def pack_blob(tag_id: int, secret: bytes, board: int, panel: int, flags: int = 0) -> bytes:
    """Build the blob, computing its CRC-32 over the first 44 bytes."""
    if len(secret) != TAG_SECRET_LEN:
        raise ValueError(f"secret must be {TAG_SECRET_LEN} bytes")
    body = Enrollment(ENROLLMENT_MAGIC, ENROLLMENT_VERSION, board, panel, flags, tag_id, secret, 0).pack()
    return body[:CRC_OFFSET] + crc32(body[:CRC_OFFSET]).to_bytes(4, "little")


def unpack_blob(blob: bytes) -> Enrollment:
    """Parse and verify a blob read back from UICR."""
    try:
        enrollment = Enrollment.unpack(blob)
    except ValueError as exc:
        raise EnrollmentError(str(exc)) from None
    if enrollment.magic != ENROLLMENT_MAGIC:
        raise EnrollmentError("bad magic")
    if enrollment.crc32 != crc32(blob[:CRC_OFFSET]):
        raise EnrollmentError("CRC mismatch")
    if enrollment.version != ENROLLMENT_VERSION:
        raise EnrollmentError(f"unsupported version {enrollment.version}")
    return enrollment


def _record(address: int, record_type: int, data: bytes) -> str:
    raw = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type]) + data
    return f":{raw.hex().upper()}{(-sum(raw)) & 0xFF:02X}"


def intel_hex(data: bytes, address: int, record_size: int = 16) -> str:
    """Intel HEX (I32HEX) text placing ``data`` at ``address``; LF line ends."""
    lines: list[str] = []
    upper = None
    pos = 0
    while pos < len(data):
        addr = address + pos
        if addr >> 16 != upper:
            upper = addr >> 16
            lines.append(_record(0, 0x04, upper.to_bytes(2, "big")))
        count = min(record_size, len(data) - pos, 0x10000 - (addr & 0xFFFF))
        lines.append(_record(addr & 0xFFFF, 0x00, data[pos : pos + count]))
        pos += count
    lines.append(_record(0, 0x01, b""))
    return "\n".join(lines) + "\n"


def enrollment_hex(blob: bytes, board: Board) -> str:
    """HEX image of a blob for the board's UICR.CUSTOMER[0]."""
    return intel_hex(blob, UICR_CUSTOMER_ADDR[BOARD_SOC[board]])
