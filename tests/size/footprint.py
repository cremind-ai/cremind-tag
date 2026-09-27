#!/usr/bin/env python3
"""Footprint of the ctag libraries in a Zephyr build of tests/size.

Usage: footprint.py <build dir> [<size tool>]

Per libctag_* archive and object: .text/.data/.bss (the cost when all of it
is used); per archive, the bytes the link kept (zephyr.map: flash = text +
rodata + data, RAM = data + bss); per library, the largest stack frame of its
functions (CONFIG_STACK_USAGE=y .su files; callees not included).
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def objects(lib: Path, size_tool: str) -> list[tuple[str, int, int, int]]:
    text = subprocess.run([size_tool, str(lib)], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in text.splitlines()[1:]:
        f = line.split()
        rows.append((f[5], int(f[0]), int(f[1]), int(f[2])))
    return rows


def linked(build: Path) -> dict[str, int]:
    text = next(build.rglob("zephyr.map")).read_text()
    text = text[text.index("Linker script and memory map"):]
    kept: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    section = ""
    for line in text.splitlines():
        m = re.match(r"^ (\.[\w.]+)(\s+0x|\s*$)", line)
        if m:
            section = m.group(1)
        m = re.search(r"0x[0-9a-f]+\s+0x([0-9a-f]+)\s+\S*lib(ctag_\w+)\.a\(", line)
        kind = next((k for k in ("text", "rodata", "data", "bss") if section.startswith("." + k)), None)
        if m and kind is not None:
            kept[m.group(2)][kind] += int(m.group(1), 16)
    return {lib: k["text"] + k["rodata"] + k["data"] for lib, k in kept.items()}


def frames(build: Path) -> dict[str, tuple[int, str]]:
    worst: dict[str, tuple[int, str]] = {}
    for su in build.rglob("*.su"):
        lib = next((p.removeprefix("CMakeFiles/").removesuffix(".dir") for p in su.parts
                    if p.startswith("ctag_") and p.endswith(".dir")), None)
        if lib is None:
            continue
        for line in su.read_text().splitlines():
            fields = line.split("\t")
            size, func = int(fields[1]), fields[0].rsplit(":", 1)[-1]
            if size > worst.get(lib, (0, ""))[0]:
                worst[lib] = (size, func)
    return worst


def main() -> None:
    build = Path(sys.argv[1])
    size_tool = sys.argv[2] if len(sys.argv) > 2 else "arm-zephyr-eabi-size"
    kept = linked(build)
    stack = frames(build)
    print(f"{'library / object':28} {'text':>6} {'data':>5} {'bss':>5} {'linked':>7}  largest frame")
    for lib in sorted(build.rglob("libctag_*.a")):
        name = lib.stem.removeprefix("lib")
        rows = objects(lib, size_tool)
        total = [sum(r[i] for r in rows) for i in (1, 2, 3)]
        frame = stack.get(name, (0, "-"))
        print(f"{name:28} {total[0]:6} {total[1]:5} {total[2]:5} {kept.get(name, 0):7}  "
              f"{frame[0]} B ({frame[1]})")
        if len(rows) > 1:
            for obj, t, d, b in rows:
                print(f"  {obj.removesuffix('.obj'):26} {t:6} {d:5} {b:5}")


if __name__ == "__main__":
    main()
