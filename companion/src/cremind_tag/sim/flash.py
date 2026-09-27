"""Simulated bridge external flash: font-pack slots, slot directory, installation, flash test
(docs/fontpack.md §3–§4, docs/protocol.md §1.6).

Layout (fontpack.md §3)::

    0            slot_size       2*slot_size                                  flash_size
    | slot 0     | slot 1        | dir A | dir B | pending layouts | spare     |

``slot_size = align_down_64K((flash_size - FONTPACK_WORKING_SPACE) / 2)``. Each
directory sector holds one 64-byte record (magic ``CTSL``, version, seq, slot,
pack id, size, content hash, CRC-32); the valid record with the highest ``seq``
names the active pack, and activation writes the *other* sector with ``seq + 1``.

The flash is sparse (untouched sectors read as erased ``0xFF``) and NOR-like:
programming can only clear bits, so a write to a sector that was not erased is
detected. Sectors listed in ``bad_sectors`` fail read-back, for flash-test
scenarios.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from ..fontpack.format import FontPack, FontPackError
from ..protocol.crc import crc32
from ..protocol.ids import FONTPACK_SLOT_DIR_MAGIC, FONTPACK_WORKING_SPACE, Status

SECTOR = 4096
BLOCK = 65536
MIB = 1 << 20
DIR_RECORD = struct.Struct("<IHHIB3s8sI32sI")
DIR_VERSION = 1
assert DIR_RECORD.size == 64


class FlashError(Exception):
    """A flash operation failed with a spec status."""

    def __init__(self, status: Status, message: str) -> None:
        super().__init__(message)
        self.status = status


class SimFlash:
    """Sparse NOR flash (erased = 0xFF, programming ANDs bits in)."""

    def __init__(self, size: int, bad_sectors: Iterable[int] = ()) -> None:
        if size % BLOCK:
            raise ValueError("flash size must be a multiple of 64 KiB")
        self.size = size
        self.bad_sectors = {s // SECTOR for s in bad_sectors}
        self._sectors: dict[int, bytearray] = {}

    def _check(self, offset: int, length: int) -> None:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise FlashError(Status.INVALID, f"flash access {offset:#x}+{length} outside {self.size:#x}")

    def read(self, offset: int, length: int) -> bytes:
        self._check(offset, length)
        out = bytearray()
        while length:
            index, start = divmod(offset, SECTOR)
            n = min(length, SECTOR - start)
            sector = self._sectors.get(index)
            chunk = bytes(sector[start : start + n]) if sector is not None else b"\xff" * n
            if index in self.bad_sectors:
                chunk = bytes(b ^ 0x01 for b in chunk)  # a stuck bit on read-back
            out += chunk
            offset += n
            length -= n
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        self._check(offset, len(data))
        pos = 0
        while pos < len(data):
            index, start = divmod(offset + pos, SECTOR)
            n = min(len(data) - pos, SECTOR - start)
            sector = self._sectors.setdefault(index, bytearray(b"\xff" * SECTOR))
            for i in range(n):
                sector[start + i] &= data[pos + i]
            pos += n

    def erase(self, offset: int, length: int) -> None:
        if offset % SECTOR or length % SECTOR:
            raise FlashError(Status.INVALID, "erase must be sector aligned")
        self._check(offset, length)
        for index in range(offset // SECTOR, (offset + length) // SECTOR):
            self._sectors.pop(index, None)


@dataclass(frozen=True, slots=True)
class DirRecord:
    seq: int
    slot: int
    pack_id: bytes
    size: int
    content_hash: bytes

    def pack(self) -> bytes:
        body = DIR_RECORD.pack(FONTPACK_SLOT_DIR_MAGIC, DIR_VERSION, 0, self.seq, self.slot, bytes(3), self.pack_id,
                               self.size, self.content_hash, 0)
        return body[:60] + crc32(body[:60]).to_bytes(4, "little")

    @classmethod
    def unpack(cls, data: bytes) -> DirRecord | None:
        magic, version, _, seq, slot, _, pack_id, size, content_hash, crc = DIR_RECORD.unpack(data[:64])
        if magic != FONTPACK_SLOT_DIR_MAGIC or version != DIR_VERSION or crc != crc32(data[:60]) or slot > 1:
            return None
        return cls(seq, slot, pack_id, size, content_hash)


@dataclass
class _Install:
    slot: int
    size: int
    digest: bytes
    pack_id: bytes
    written: int = 0
    erased_to: int = 0


class FontStore:
    """Two slots and the directory on one :class:`SimFlash` (see the module docstring)."""

    def __init__(self, flash: SimFlash) -> None:
        self.flash = flash
        self.slot_size = max(0, (flash.size - FONTPACK_WORKING_SPACE) // 2 // BLOCK * BLOCK)
        self.dir_offsets = (2 * self.slot_size, 2 * self.slot_size + SECTOR)
        self._install: _Install | None = None
        self._pack: FontPack | None = None
        self._pack_seq = -1
        self.installs = 0

    def slot_offset(self, slot: int) -> int:
        return slot * self.slot_size

    def active(self) -> DirRecord | None:
        if self.slot_size == 0:
            return None
        records = [DirRecord.unpack(self.flash.read(off, DIR_RECORD.size)) for off in self.dir_offsets]
        valid = [r for r in records if r is not None]
        return max(valid, key=lambda r: r.seq) if valid else None

    def active_pack(self) -> FontPack | None:
        """The active pack, validated as at boot (content hash not re-read)."""
        record = self.active()
        if record is None:
            return None
        if self._pack is None or self._pack_seq != record.seq:
            data = self.flash.read(self.slot_offset(record.slot), record.size)
            try:
                self._pack = FontPack(data, verify_content=False)
            except FontPackError:
                return None
            self._pack_seq = record.seq
        return self._pack

    # -- installation (FONT_BEGIN / FONT_DATA / FONT_COMMIT / FONT_ABORT) -------------

    def begin(self, size: int, digest: bytes, pack_id: bytes) -> int:
        if self.slot_size == 0:
            raise FlashError(Status.NO_RESOURCES, "flash too small for two slots and the working space")
        if size <= 0 or len(digest) != 32 or len(pack_id) != 8:
            raise FlashError(Status.INVALID, "bad FONT_BEGIN arguments")
        if size > self.slot_size:
            raise FlashError(Status.TOO_LARGE, f"pack of {size} bytes > slot of {self.slot_size}")
        active = self.active()
        slot = 1 - active.slot if active is not None else 0
        self._install = _Install(slot, size, digest, pack_id)
        return slot

    def data(self, offset: int, data: bytes) -> None:
        inst = self._install
        if inst is None:
            raise FlashError(Status.NOT_FOUND, "no installation in progress")
        if offset != inst.written or not data or offset + len(data) > inst.size:
            raise FlashError(Status.INVALID, f"FONT_DATA offset {offset}, expected {inst.written}")
        base = self.slot_offset(inst.slot)
        end = offset + len(data)
        while inst.erased_to < end:  # erase ahead, one 64 KiB block at a time
            self.flash.erase(base + inst.erased_to, min(BLOCK, self.slot_size - inst.erased_to))
            inst.erased_to += BLOCK
        self.flash.write(base + offset, data)
        inst.written = end

    def commit(self) -> bytes:
        inst = self._install
        if inst is None:
            raise FlashError(Status.NOT_FOUND, "no installation in progress")
        self._install = None
        if inst.written != inst.size:
            raise FlashError(Status.INCOMPLETE, f"{inst.written} of {inst.size} bytes written")
        data = self.flash.read(self.slot_offset(inst.slot), inst.size)
        if hashlib.sha256(data).digest() != inst.digest:
            raise FlashError(Status.DIGEST_MISMATCH, "SHA-256 of the slot differs from FONT_BEGIN.digest")
        try:
            pack = FontPack(data)  # install-time validation includes the content hash
        except FontPackError as exc:
            raise FlashError(exc.status, str(exc)) from None
        if pack.pack_id != inst.pack_id:
            raise FlashError(Status.INVALID, "pack id differs from FONT_BEGIN.fontpack_id")
        active = self.active()
        seq = active.seq + 1 if active is not None else 1
        # Write the sector that does NOT hold the active record: a reset mid-write leaves it valid.
        active_offset = self._dir_offset_of(active) if active is not None else None
        target = self.dir_offsets[1] if active_offset == self.dir_offsets[0] else self.dir_offsets[0]
        self.flash.erase(target, SECTOR)
        self.flash.write(target, DirRecord(seq, inst.slot, pack.pack_id, inst.size, pack.content_hash).pack())
        self.installs += 1
        return bytes(pack.pack_id)

    def _dir_offset_of(self, record: DirRecord) -> int:
        for off in self.dir_offsets:
            if DirRecord.unpack(self.flash.read(off, DIR_RECORD.size)) == record:
                return off
        return -1

    def abort(self) -> None:
        self._install = None

    def install(self, pack: bytes) -> bytes:
        """Direct installation (simulator setup): the same steps as over the maintenance port."""
        self.abort()
        self.begin(len(pack), hashlib.sha256(pack).digest(), FontPack(pack).pack_id)
        for offset in range(0, len(pack), 4096):
            self.data(offset, pack[offset : offset + 4096])
        return self.commit()

    # -- FLASH_TEST ----------------------------------------------------------------------

    def test_offsets(self) -> list[int]:
        """First sector of the inactive slot, both sectors at the 16 MiB boundary, the last sector."""
        active = self.active()
        inactive = 1 - active.slot if active is not None else 0
        offsets = [self.slot_offset(inactive), 16 * MIB - SECTOR, 16 * MIB, self.flash.size - SECTOR]
        return sorted({o for o in offsets if 0 <= o < self.flash.size})

    def _in_use(self, offset: int) -> bool:
        active = self.active()
        if active is not None:
            start = self.slot_offset(active.slot)
            if start <= offset < start + self.slot_size:
                return True
        return offset in self.dir_offsets or self._install is not None

    def flash_test(self) -> list[tuple[int, Status]]:
        """Erase / write a pattern / read back at each offset; in-use sectors report ``BUSY``."""
        results: list[tuple[int, Status]] = []
        for offset in self.test_offsets():
            if self._in_use(offset):
                results.append((offset, Status.BUSY))
                continue
            pattern = bytes((i * 37 + (offset >> 12)) & 0xFF for i in range(SECTOR))
            self.flash.erase(offset, SECTOR)
            self.flash.write(offset, pattern)
            ok = self.flash.read(offset, SECTOR) == pattern
            self.flash.erase(offset, SECTOR)
            results.append((offset, Status.OK if ok else Status.STORAGE_ERROR))
        return results
