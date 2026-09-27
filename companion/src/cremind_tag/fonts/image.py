"""External-flash images for factory programming of a bridge (docs/fontpack.md §3).

The image holds a pack in slot 0 and an active slot-directory record at the
start of the working space, so a bridge boots with that pack without a
maintenance-port install. ``fonts image`` writes:

- ``flash.bin`` — raw bytes from flash offset 0 to the end of the two
  directory sectors (slot 1 and every gap erased, 0xFF),
- ``flash.hex`` — Intel HEX at the nRF52840 QSPI XIP base (0x12000000) with
  only the sectors that matter (the pack's and both directory sectors), for
  ``nrfjprog --program flash.hex --qspisectorerase --verify``,
- ``flash.json`` — the layout and pack identity.

Directory sector B is written erased so no stale record can outrank the new one.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

from cremind_tag.fontpack.format import FontPack
from cremind_tag.fonts.sizing import ERASE_BLOCK, slot_size
from cremind_tag.protocol.crc import crc32
from cremind_tag.protocol.ids import FONTPACK_SLOT_DIR_MAGIC, FONTPACK_WORKING_SPACE

SECTOR = 4096
SLOT_DIR_VERSION = 1
QSPI_XIP_BASE = 0x12000000
_DIR = struct.Struct("<IHHIB3s8sI32sI")  # magic, version, reserved, seq, slot, pad, pack_id, size, hash, crc
assert _DIR.size == 64


def slot_dir_record(seq: int, slot: int, pack_id: bytes, size: int, content_hash: bytes) -> bytes:
    """One 64-byte slot-directory record; CRC-32 covers the first 60 bytes."""
    body = _DIR.pack(FONTPACK_SLOT_DIR_MAGIC, SLOT_DIR_VERSION, 0, seq, slot, bytes(3), pack_id, size,
                     content_hash, 0)[:-4]
    return body + crc32(body).to_bytes(4, "little")


def parse_slot_dir_record(data: bytes) -> dict[str, object] | None:
    """The record at the start of a directory sector, or None when erased/invalid."""
    magic, version, _, seq, slot, _, pack_id, size, content_hash, crc = _DIR.unpack_from(data)
    if magic != FONTPACK_SLOT_DIR_MAGIC or crc != crc32(data[:60]):
        return None
    return {"version": version, "seq": seq, "slot": slot, "pack_id": pack_id, "size": size,
            "content_hash": content_hash}


@dataclass(frozen=True)
class FlashImage:
    flash_size: int
    working_space: int
    slot_size: int
    dir_offset: int
    data: bytes
    """Flash bytes from offset 0 to ``dir_offset + 2 sectors``."""
    pack_id: bytes
    pack_size: int

    @property
    def segments(self) -> list[tuple[int, bytes]]:
        """(offset, bytes) that must be programmed: the pack's sectors and both directory sectors."""
        pack_end = -(-self.pack_size // SECTOR) * SECTOR
        return [(0, self.data[:pack_end]), (self.dir_offset, self.data[self.dir_offset:])]


def flash_image(pack_bytes: bytes, flash_size: int, working_space: int = FONTPACK_WORKING_SPACE,
                seq: int = 1) -> FlashImage:
    pack = FontPack(pack_bytes)
    slot = slot_size(flash_size, working_space)
    if slot < ERASE_BLOCK:
        raise ValueError(f"a {flash_size:,}-byte part with a {working_space:,}-byte working space has no pack slots")
    if pack.total_size > slot:
        raise ValueError(f"the pack ({pack.total_size:,} B) does not fit a {slot:,}-byte slot")
    dir_offset = 2 * slot
    image = bytearray(b"\xff" * (dir_offset + 2 * SECTOR))
    image[: pack.total_size] = pack.data
    record = slot_dir_record(seq, 0, pack.pack_id, pack.total_size, pack.content_hash)
    image[dir_offset: dir_offset + len(record)] = record
    return FlashImage(flash_size, working_space, slot, dir_offset, bytes(image), pack.pack_id, pack.total_size)


def intel_hex(segments: list[tuple[int, bytes]], base: int, record_len: int = 32) -> str:
    """Intel HEX (type 04 extended linear addresses + type 00 data + EOF)."""
    lines = []
    upper = None

    def rec(kind: int, addr: int, payload: bytes) -> str:
        raw = bytes([len(payload), (addr >> 8) & 0xFF, addr & 0xFF, kind]) + payload
        return ":" + (raw + bytes([-sum(raw) & 0xFF])).hex().upper()

    for start, data in segments:
        off = 0
        while off < len(data):
            addr = base + start + off
            if addr >> 16 != upper:
                upper = addr >> 16
                lines.append(rec(0x04, 0, upper.to_bytes(2, "big")))
            chunk = data[off: off + min(record_len, 0x10000 - (addr & 0xFFFF))]  # never cross a 64 KiB page
            lines.append(rec(0x00, addr & 0xFFFF, chunk))
            off += len(chunk)
    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def write_image(image: FlashImage, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"bin": out_dir / "flash.bin", "hex": out_dir / "flash.hex", "json": out_dir / "flash.json"}
    paths["bin"].write_bytes(image.data)
    paths["hex"].write_bytes(intel_hex(image.segments, QSPI_XIP_BASE).encode("ascii"))
    meta = {
        "flash_size": image.flash_size, "working_space": image.working_space, "slot_size": image.slot_size,
        "slot0_offset": 0, "slot1_offset": image.slot_size, "dir_offset": image.dir_offset,
        "dir_sectors": [image.dir_offset, image.dir_offset + SECTOR], "image_size": len(image.data),
        "pack_id": image.pack_id.hex(), "pack_size": image.pack_size,
        "image_sha256": hashlib.sha256(image.data).hexdigest(), "qspi_xip_base": QSPI_XIP_BASE,
        "program": f"nrfjprog -f NRF52 --program {paths['hex'].name} --qspisectorerase --verify",
    }
    paths["json"].write_bytes((json.dumps(meta, indent=2) + "\n").encode("utf-8"))
    return paths
