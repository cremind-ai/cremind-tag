"""External-flash sizing for font packs (docs/fontpack.md §3).

A bridge keeps two pack slots plus a working space, so a flash part is
acceptable only when ``flash_size ≥ 2 × erase_aligned(P) + FONTPACK_WORKING_SPACE``
(16 MiB). ``slot_size = align_down_64K((flash_size − working_space) / 2)``.
Development boards may use a smaller working space; such results are labelled
development-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from cremind_tag.protocol.ids import FONTPACK_WORKING_SPACE

ERASE_BLOCK = 64 * 1024
MIB = 1024 * 1024
STANDARD_NOR_DENSITIES = tuple(MIB << i for i in range(9))
"""Standard serial NOR densities, 8 Mbit (1 MiB) to 2 Gbit (256 MiB)."""
FOUR_BYTE_ADDRESSING_ABOVE = 16 * MIB


def erase_aligned(size: int) -> int:
    """``size`` rounded up to the 64 KiB erase block."""
    return -(-size // ERASE_BLOCK) * ERASE_BLOCK


def required_flash(pack_size: int, working_space: int = FONTPACK_WORKING_SPACE) -> int:
    return 2 * erase_aligned(pack_size) + working_space


def smallest_density(required: int) -> int | None:
    """Smallest standard NOR density ≥ ``required``, or None above 2 Gbit."""
    return next((d for d in STANDARD_NOR_DENSITIES if d >= required), None)


def slot_size(flash_size: int, working_space: int = FONTPACK_WORKING_SPACE) -> int:
    """Pack slot size on a part, or 0 when the working space leaves no room."""
    return max(0, (flash_size - working_space) // 2 // ERASE_BLOCK * ERASE_BLOCK)


def density_label(size: int) -> str:
    mbit = size * 8 // MIB
    gbit = f"{mbit // 1024} Gbit" if mbit >= 1024 and mbit % 1024 == 0 else f"{mbit} Mbit"
    return f"{gbit} ({size // MIB} MiB)"


@dataclass(frozen=True)
class Sizing:
    pack_size: int
    working_space: int
    flash_size: int | None
    """The part checked: given, else the smallest standard density that satisfies the rule."""
    development_only: bool

    @property
    def erase_aligned(self) -> int:
        return erase_aligned(self.pack_size)

    @property
    def required(self) -> int:
        return required_flash(self.pack_size, self.working_space)

    @property
    def slot_size(self) -> int:
        return slot_size(self.flash_size, self.working_space) if self.flash_size else 0

    @property
    def fits(self) -> bool:
        return self.flash_size is not None and self.flash_size >= self.required

    @property
    def headroom(self) -> int:
        """Slot bytes left beyond the pack (how much the pack can still grow)."""
        return self.slot_size - self.pack_size

    @property
    def four_byte_addressing(self) -> bool:
        return bool(self.flash_size and self.flash_size > FOUR_BYTE_ADDRESSING_ABOVE)


def size_pack(pack_size: int, *, flash_size: int | None = None, working_space: int = FONTPACK_WORKING_SPACE) -> Sizing:
    """Apply the capacity rule; without ``flash_size`` pick the smallest standard part."""
    if flash_size is None:
        flash_size = smallest_density(required_flash(pack_size, working_space))
    return Sizing(pack_size, working_space, flash_size, working_space < FONTPACK_WORKING_SPACE)


def parse_size(text: str) -> int:
    """``8MiB``, ``512KiB``, ``16777216`` or ``0x1000000`` -> bytes."""
    t = text.strip().replace(" ", "")
    for suffix, mult in (("GiB", 1 << 30), ("MiB", MIB), ("KiB", 1024), ("M", MIB), ("K", 1024)):
        if t.endswith(suffix):
            return int(float(t[: -len(suffix)]) * mult)
    return int(t, 0)
