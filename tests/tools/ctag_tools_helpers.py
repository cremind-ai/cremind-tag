"""Helpers for the tools/ tests: synthetic ELF images, file edits, edtlib stand-ins."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PT_LOAD = 1
SHF_ALLOC = 0x2


def make_elf(segments: list[tuple[int, int, int, int]], sections: list[tuple[int, int, int]]) -> bytes:
    """Minimal ELF32 LE image: segments (vaddr, paddr, filesz, memsz), sections (flags, addr, size)."""
    ehsize, phentsize, shentsize = 52, 32, 40
    phoff = ehsize
    shoff = phoff + phentsize * len(segments)
    all_sections = [(0, 0, 0), *sections]  # index 0 is the null section
    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header += struct.pack(
        "<HHIIIIIHHHHHH",
        2,
        40,
        1,
        0,
        phoff,
        shoff,
        0,
        ehsize,
        phentsize,
        len(segments),
        shentsize,
        len(all_sections),
        0,
    )
    body = b"".join(struct.pack("<IIIIIIII", PT_LOAD, 0, v, p, fs, ms, 5, 4) for v, p, fs, ms in segments)
    body += b"".join(
        struct.pack("<IIIIIIIIII", 0, 1 if i else 0, flags, addr, 0, size, 0, 0, 4, 0)
        for i, (flags, addr, size) in enumerate(all_sections)
    )
    return header + body


def small_elf() -> bytes:
    """Image of 0x1040 flash bytes (.text + .data load image) and 0xa40 RAM bytes."""
    return make_elf(
        segments=[(0x0, 0x0, 0x1000, 0x1000), (0x20000000, 0x1000, 0x40, 0x40)],
        sections=[
            (SHF_ALLOC, 0x0, 0x1000),  # .text
            (SHF_ALLOC, 0x20000000, 0x40),  # .data
            (SHF_ALLOC, 0x20000040, 0x200),  # .bss
            (SHF_ALLOC, 0x20000240, 0x800),  # .noinit (stacks)
            (0, 0x0, 0x99),  # .comment, not allocated
        ],
    )


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# Stand-ins for edtlib objects: only the attributes verify_stack reads.


@dataclass
class StubReg:
    addr: int
    size: int


@dataclass
class StubNode:
    path: str
    compats: list[str] = field(default_factory=list)
    regs: list[StubReg] = field(default_factory=list)


@dataclass
class StubEdt:
    chosen: dict[str, StubNode]
    compat2okay: dict[str, list[StubNode]]
    label2node: dict[str, StubNode]

    def chosen_node(self, name: str) -> StubNode | None:
        return self.chosen.get(name)


def stub_edt(controller: str = "zephyr,bt-hci-ll-sw-split", flash: int = 131072, sram: int = 16384) -> StubEdt:
    node_name = "bt_hci_controller" if controller.startswith("zephyr") else "bt_hci_sdc"
    hci = StubNode(f"/soc/radio@40001000/{node_name}", [controller])
    storage = StubNode(
        "/soc/flash-controller@4001e000/flash@0/partitions/partition@1f000", regs=[StubReg(0x1F000, 0x1000)]
    )
    return StubEdt(
        chosen={
            "zephyr,bt-hci": hci,
            "zephyr,flash": StubNode("/soc/flash-controller@4001e000/flash@0", regs=[StubReg(0, flash)]),
            "zephyr,sram": StubNode("/soc/memory@20000000", regs=[StubReg(0x20000000, sram)]),
        },
        compat2okay={controller: [hci]},
        label2node={"storage_partition": storage},
    )
