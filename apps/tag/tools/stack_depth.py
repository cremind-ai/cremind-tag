#!/usr/bin/env python3
"""Static worst-case stack estimate from a linked Cortex-M image.

puncover-style, but on the final binary (so it sees what LTO inlined): every
function's frame is read from its disassembly (push/stmdb/vpush, sub sp, and
literal-pool adjustments of sp), call edges from bl/blx and tail branches, and
the deepest call chain below each root is reported with its frames. Functions
are identified by address: static functions of different files may share a
name.

Indirect calls (blx rN through device API tables, work handlers, GATT and
HCI event callbacks) cannot be followed statically. A blx whose register was
loaded from a literal holding a code address is a direct (long) call. For the
others the caller supplies the targets it knows with --edge or --edges-file
lines ``CALLER=TARGET[,TARGET...]`` (fnmatch globs on names); every indirect
call left unresolved below a root is listed.

The results are ESTIMATES: interrupts nest on the ISR stack (not included),
each thread stack additionally takes the exception frame pushed on interrupt
entry (32 bytes without FPU context), recursion is cut, and hardware
measurement with CONFIG_THREAD_ANALYZER remains the reference.

Usage (inside the NCS toolchain container, where objdump and pyelftools are):

    python3 apps/tag/tools/stack_depth.py build/tag-laowu-bw/zephyr.elf \\
        --edges-file apps/tag/tools/stack_edges.txt --root work_queue_main --json out.json
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

FUNC_RE = re.compile(r"^([0-9a-f]+) <(.+)>:$")
INSN_RE = re.compile(r"^\s+([0-9a-f]+):\s+(\S+)\s*(.*)$")
TARGET_RE = re.compile(r"^([0-9a-f]+) <([^>+]+)(\+0x[0-9a-f]+)?>")
REGLIST_RE = re.compile(r"\{([^}]*)\}")
IMM_RE = re.compile(r"#(-?\d+)")
LITERAL_RE = re.compile(r"\[pc, #\d+\]\s*@ \(?(0x[0-9a-f]+)")


@dataclass
class Func:
    name: str
    addr: int
    frame: int = 0
    calls: set[int] = field(default_factory=set)
    indirect: int = 0  # unresolved blx rN sites
    notes: list[str] = field(default_factory=list)
    long_calls: list[int] = field(default_factory=list)


def reg_count(operands: str) -> int:
    m = REGLIST_RE.search(operands)
    if not m:
        return 0
    n = 0
    for part in m.group(1).split(","):
        part = part.strip()
        if "-" in part:  # r4-r7 / d8-d15
            lo, hi = part.split("-")
            n += int(re.sub(r"\D", "", hi)) - int(re.sub(r"\D", "", lo)) + 1
        elif part:
            n += 1
    return n


def find_objdump() -> str:
    hits = sorted(glob.glob("/opt/ncs/toolchains/*/opt/zephyr-sdk/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-objdump"))
    return hits[0] if hits else "arm-zephyr-eabi-objdump"


def read_word(elf, addr: int) -> int | None:
    for seg in elf.iter_segments():
        if seg["p_type"] != "PT_LOAD":
            continue
        start = seg["p_vaddr"]
        if start <= addr < start + seg["p_filesz"] - 3:
            return int.from_bytes(seg.data()[addr - start : addr - start + 4], "little")
    return None


def parse(elf_path: str, objdump: str) -> dict[int, Func]:
    from elftools.elf.elffile import ELFFile

    text = subprocess.run([objdump, "-d", "--no-show-raw-insn", elf_path], check=True, capture_output=True,
                          text=True).stdout
    elf = ELFFile(open(elf_path, "rb"))
    funcs: dict[int, Func] = {}
    cur: Func | None = None
    literals: dict[str, int] = {}
    for line in text.splitlines():
        m = FUNC_RE.match(line)
        if m:
            cur = Func(m.group(2), int(m.group(1), 16))
            funcs[cur.addr] = cur
            literals = {}
            continue
        m = INSN_RE.match(line)
        if not m or cur is None:
            continue
        mnem, ops = m.group(2), m.group(3)
        base = mnem.split(".")[0]
        if base in ("push", "stmdb", "vpush") and (base != "stmdb" or ops.startswith("sp!")):
            cur.frame += (8 if base == "vpush" else 4) * reg_count(ops)
        elif base in ("sub", "subw") and re.match(r"sp,\s*(sp,\s*)?#", ops):
            imm = IMM_RE.search(ops)
            if imm:
                cur.frame += int(imm.group(1))
        elif base == "ldr" and "[pc" in ops:
            lit = LITERAL_RE.search(ops)
            if lit:
                value = read_word(elf, int(lit.group(1), 16))
                if value is not None:
                    literals[ops.split(",")[0].strip()] = value
        elif base in ("add", "sub") and re.match(r"sp,\s*(sp,\s*)?r\d+$", ops):
            value = literals.get(ops.split(",")[-1].strip())
            if value is None:
                cur.notes.append(f"unknown sp adjustment '{mnem} {ops}'")
            else:
                signed = value - (1 << 32) if value & 0x80000000 else value
                cur.frame += -signed if base == "add" else signed
        if base in ("bl", "blx", "b", "bx") or mnem in ("b.n", "b.w"):
            t = TARGET_RE.match(ops)
            if t:
                # bl: a call; b to another function's entry: a tail call
                # (counted as a call: conservative).
                if t.group(2) != cur.name and (base in ("bl", "blx") or t.group(3) is None):
                    cur.calls.add(int(t.group(1), 16))
            elif base == "blx":
                target = literals.get(ops.strip())
                if target is not None:
                    cur.long_calls.append(target & ~1)
                else:
                    cur.indirect += 1
    for f in funcs.values():
        for addr in f.long_calls:
            if addr in funcs:
                f.calls.add(addr)
            else:
                f.indirect += 1
        f.calls &= funcs.keys()
    return funcs


class Graph:
    def __init__(self, funcs: dict[int, Func], edges: dict[str, list[str]]) -> None:
        self.funcs = funcs
        self.by_name: dict[str, list[int]] = {}
        for f in funcs.values():
            self.by_name.setdefault(f.name, []).append(f.addr)
        names = list(self.by_name)
        self.extra: dict[int, set[int]] = {}
        self.resolved: set[int] = set()
        for pattern, targets in edges.items():
            addrs = {a for t in targets for n in fnmatch.filter(names, t) for a in self.by_name[n]}
            missing = [t for t in targets if not fnmatch.filter(names, t)]
            if missing:
                print(f"note: edge targets not in the image: {', '.join(missing)}", file=sys.stderr)
            for n in fnmatch.filter(names, pattern):
                for a in self.by_name[n]:
                    self.extra.setdefault(a, set()).update(addrs - {a})
                    self.resolved.add(a)
        self.memo: dict[int, tuple[int, list[int]]] = {}
        self.cycles: set[str] = set()

    def callees(self, addr: int) -> set[int]:
        return self.funcs[addr].calls | self.extra.get(addr, set())

    def depth(self, addr: int, stack: tuple[int, ...] = ()) -> tuple[int, list[int]]:
        if addr in self.memo:
            return self.memo[addr]
        if addr in stack:
            self.cycles.add(self.funcs[addr].name)
            return 0, []
        best, best_path = 0, []
        for c in sorted(self.callees(addr)):
            d, path = self.depth(c, stack + (addr,))
            if d > best:
                best, best_path = d, path
        result = (self.funcs[addr].frame + best, [addr, *best_path])
        self.memo[addr] = result
        return result

    def reachable(self, root: int) -> set[int]:
        seen, todo = set(), [root]
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(self.callees(n))
        return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--objdump", default=find_objdump())
    ap.add_argument("--root", action="append", default=[], help="root function name or glob (repeat)")
    ap.add_argument("--edge", action="append", default=[], help="CALLER=T1,T2 known indirect targets (globs)")
    ap.add_argument("--edges-file", help="file of CALLER=T1,T2 lines (# comments)")
    ap.add_argument("--json", help="write the results as JSON")
    args = ap.parse_args(argv)

    funcs = parse(args.elf, args.objdump)
    lines = list(args.edge)
    if args.edges_file:
        with open(args.edges_file, encoding="utf-8") as fh:
            lines += [ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()]
    edges: dict[str, list[str]] = {}
    for e in lines:
        caller, _, targets = e.partition("=")
        edges.setdefault(caller, []).extend(t for t in targets.split(",") if t)
    g = Graph(funcs, edges)
    out = {}
    for pattern in args.root:
        names = fnmatch.filter(list(g.by_name), pattern)
        if not names:
            print(f"{pattern}: not in the image (inlined?)")
            continue
        for name in names:
            for root in g.by_name[name]:
                depth, path = g.depth(root)
                reach = g.reachable(root)
                unresolved = sorted({funcs[a].name for a in reach if funcs[a].indirect and a not in g.resolved})
                notes = sorted({f"{funcs[a].name}: {x}" for a in reach for x in funcs[a].notes})
                label = name if len(g.by_name[name]) == 1 else f"{name}@{root:#x}"
                print(f"\n{label}: {depth} B (estimate)")
                for a in path:
                    print(f"  {funcs[a].frame:5d}  {funcs[a].name}")
                if unresolved:
                    print(f"  indirect calls not followed in: {', '.join(unresolved)}")
                for note in notes:
                    print(f"  note: {note}")
                out[label] = {"depth": depth, "path": [[funcs[a].name, funcs[a].frame] for a in path],
                              "unresolved_indirect": unresolved, "notes": notes}
    if g.cycles:
        print(f"\nrecursion cut at: {', '.join(sorted(g.cycles))}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
