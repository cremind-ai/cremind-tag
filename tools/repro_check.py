#!/usr/bin/env python3
"""Prove that firmware images are reproducible.

The checkout is snapshotted once and copied to a second, differently
named path; each copy is built pristine with tools/build.py into its own build
root (a different, deeper directory). ``zephyr.hex`` and ``zephyr.bin`` of the
two builds must be byte-identical; ``zephyr.elf`` and the Kconfig/devicetree
digests of metadata.json are compared and reported too. Building both from one
snapshot means edits made to the working tree meanwhile cannot fake a
difference, and the two paths prove that no checkout or build path leaks into
the image (CI builds in $GITHUB_WORKSPACE, a local build in /work).

When images differ the report says where: the differing address ranges, the
ELF section and symbol at each, path strings found in only one image, date or
time strings, GNU build ids, and the fix to apply in tools/build.py
(``repro_cmake_args``).

Font packs are the host software's: Cremind builds, pins and proves them
(its scripts/tags/build_font_bundle.py --check).

Examples::

    python tools/repro_check.py tag-laowu-bw bridge-nrf52840dk
    python tools/repro_check.py --release-targets             # every target tools/release.py publishes
    python3 tools/repro_check.py --in-container --all         # CI, inside the toolchain container

The report goes to build/repro/report.json; for a target that differs both
builds' images are kept in build/repro/<target>/{a,b}/. Exit status: 0 every
checked item is reproducible, 1 something differs or a build failed, 2 usage
error, 3 nothing differed but a target was skipped (its app does not exist).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

REPO_ROOT = build.REPO_ROOT
REPORT_DIR = REPO_ROOT / "build" / "repro"
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SKIPPED = 0, 1, 2, 3

IMAGE_FILES = ("zephyr.hex", "zephyr.bin")
COMPARED_FILES = (*IMAGE_FILES, "zephyr.elf")
INPUT_DIGESTS = ("kconfig_sha256", "dts_sha256", "devicetree_header_sha256")
# Never copied into the snapshot: build output, caches, environments.
SNAPSHOT_IGNORE_PATHS = (
    "build",
    "build-*",
    "dist",
    "twister-out*",
    "tests/host/build",
)
SNAPSHOT_IGNORE_NAMES = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
)
# Second checkout and build root: other names, other depths.
ALT_CHECKOUT = Path("b-alternate") / "second" / "checkout-copy"
BUILD_A = Path("build-a")
BUILD_B = Path("build-b") / "a" / "deeper" / "build-root"


# --------------------------------------------------------------------------
# Intel HEX and ELF


def parse_intel_hex(text: str) -> dict[int, int]:
    """Address -> byte for the data records of an Intel HEX file (types 00, 01, 02, 04)."""
    memory: dict[int, int] = {}
    base = 0
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {number}: not an Intel HEX record")
        raw = bytes.fromhex(line[1:])
        if len(raw) < 5 or sum(raw) & 0xFF:
            raise ValueError(f"line {number}: bad record or checksum")
        count, addr, rtype, data = raw[0], int.from_bytes(raw[1:3], "big"), raw[3], raw[4:-1]
        if len(data) != count:
            raise ValueError(f"line {number}: length mismatch")
        if rtype == 0x00:
            for i, byte in enumerate(data):
                memory[base + addr + i] = byte
        elif rtype == 0x01:
            break
        elif rtype == 0x02:
            base = int.from_bytes(data, "big") << 4
        elif rtype == 0x04:
            base = int.from_bytes(data, "big") << 16
    return memory


def diff_ranges(a: dict[int, int], b: dict[int, int]) -> list[tuple[int, int]]:
    """Merged [start, end) address ranges where the images differ (a byte present in one only counts)."""
    addresses = sorted(addr for addr in a.keys() | b.keys() if a.get(addr) != b.get(addr))
    ranges: list[tuple[int, int]] = []
    for addr in addresses:
        if ranges and addr == ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], addr + 1)
        else:
            ranges.append((addr, addr + 1))
    return ranges


@dataclass
class Section:
    name: str
    addr: int
    size: int
    flags: int
    type: int
    offset: int


@dataclass
class Symbol:
    name: str
    value: int
    size: int


@dataclass
class Elf:
    sections: list[Section] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    segments: list[tuple[int, int, int]] = field(default_factory=list)
    """PT_LOAD (vaddr, paddr, filesz)."""
    data: bytes = b""

    def section_bytes(self, name: str) -> bytes | None:
        for s in self.sections:
            if s.name == name and s.type != 8:  # SHT_NOBITS
                return self.data[s.offset : s.offset + s.size]
        return None

    def vma(self, lma: int) -> int:
        """The run address of a load address (.data is loaded from flash)."""
        for vaddr, paddr, filesz in self.segments:
            if paddr <= lma < paddr + filesz:
                return vaddr + (lma - paddr)
        return lma

    def locate(self, lma: int) -> tuple[str | None, str | None]:
        """(section, symbol+offset) holding the byte loaded at ``lma``."""
        vma = self.vma(lma)
        section = next(
            (s.name for s in self.sections if s.flags & 0x2 and s.addr <= vma < s.addr + s.size), None
        )
        best: Symbol | None = None
        for sym in self.symbols:
            if sym.value <= vma < sym.value + max(sym.size, 1) and (best is None or sym.size < best.size):
                best = sym
        symbol = f"{best.name}+0x{vma - best.value:x}" if best else None
        return section, symbol


def read_elf(data: bytes) -> Elf:
    """Sections, PT_LOAD segments and sized symbols of a 32-bit little-endian ELF."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a 32-bit little-endian ELF file")
    (_, _, _, _, phoff, shoff, _, _, phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from(
        "<HHIIIIIHHHHHH", data, 16
    )
    raw: list[tuple[int, ...]] = [struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)]
    strtab_hdr = raw[shstrndx] if 0 < shstrndx < len(raw) else None

    def cstr(table_off: int, idx: int) -> str:
        end = data.index(b"\0", table_off + idx)
        return data[table_off + idx : end].decode("utf-8", "replace")

    elf = Elf(data=data)
    for hdr in raw:
        name = cstr(strtab_hdr[4], hdr[0]) if strtab_hdr else ""
        elf.sections.append(Section(name, hdr[3], hdr[5], hdr[2], hdr[1], hdr[4]))
    for i in range(phnum):
        p_type, _, p_vaddr, p_paddr, p_filesz, *_ = struct.unpack_from("<IIIIIIII", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            elf.segments.append((p_vaddr, p_paddr, p_filesz))
    for hdr in raw:
        if hdr[1] != 2:  # SHT_SYMTAB
            continue
        strtab = raw[hdr[6]]
        for off in range(hdr[4], hdr[4] + hdr[5], 16):
            st_name, st_value, st_size, st_info, _, _ = struct.unpack_from("<IIIBBH", data, off)
            if st_name and st_size and (st_info & 0xF) in (1, 2):  # OBJECT, FUNC
                elf.symbols.append(Symbol(cstr(strtab[4], st_name), st_value & ~1, st_size))
    return elf


# --------------------------------------------------------------------------
# Diagnosis

_DATE = re.compile(rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ 0-3]\d \d{4}")
_TIME = re.compile(rb"(?<![\d:])[0-2]\d:[0-5]\d:[0-5]\d(?![\d:])")
_ISO = re.compile(rb"20\d\d-[01]\d-[0-3]\d[T ][0-2]\d:[0-5]\d")
_PATH = re.compile(rb"/(?:[A-Za-z0-9._+-]+/)+[A-Za-z0-9._+-]*")
_GITISH = re.compile(rb"(?:-dirty|-g[0-9a-f]{7,40})\b")


def _strings(blob: bytes, pattern: re.Pattern[bytes]) -> set[str]:
    return {m.group(0).decode("latin-1") for m in pattern.finditer(blob)}


def diagnose(dir_a: Path, dir_b: Path, roots: list[str]) -> dict[str, Any]:
    """Explain why two builds' images differ (see the module docstring)."""
    diag: dict[str, Any] = {"ranges": [], "differing_bytes": 0, "hints": []}
    hex_a, hex_b = dir_a / "zephyr.hex", dir_b / "zephyr.hex"
    if not (hex_a.is_file() and hex_b.is_file()):
        diag["hints"].append("zephyr.hex missing on one side; compare zephyr.bin by hand")
        return diag
    mem_a = parse_intel_hex(hex_a.read_text(encoding="ascii"))
    mem_b = parse_intel_hex(hex_b.read_text(encoding="ascii"))
    ranges = diff_ranges(mem_a, mem_b)
    diag["differing_bytes"] = sum(end - start for start, end in ranges)
    diag["size"] = {"a": len(mem_a), "b": len(mem_b)}
    elf_a = elf_b = None
    try:
        elf_a = read_elf((dir_a / "zephyr.elf").read_bytes())
        elf_b = read_elf((dir_b / "zephyr.elf").read_bytes())
    except (OSError, ValueError, struct.error, IndexError) as exc:
        diag["hints"].append(f"ELF not readable ({exc}); ranges are not mapped to symbols")
    for start, end in ranges[:12]:
        item: dict[str, Any] = {"start": f"0x{start:08x}", "length": end - start}
        if elf_a is not None:
            item["section"], item["symbol_a"] = elf_a.locate(start)
        if elf_b is not None:
            item["symbol_b"] = elf_b.locate(start)[1]
        item["a"] = bytes(mem_a.get(x, 0xFF) for x in range(start, min(end, start + 32))).hex()
        item["b"] = bytes(mem_b.get(x, 0xFF) for x in range(start, min(end, start + 32))).hex()
        diag["ranges"].append(item)
    diag["range_count"] = len(ranges)

    blob_a = (dir_a / "zephyr.bin").read_bytes() if (dir_a / "zephyr.bin").is_file() else b""
    blob_b = (dir_b / "zephyr.bin").read_bytes() if (dir_b / "zephyr.bin").is_file() else b""
    paths_a, paths_b = _strings(blob_a, _PATH), _strings(blob_b, _PATH)
    only = sorted((paths_a ^ paths_b))[:20]
    leaked = sorted(p for p in paths_a | paths_b if any(root and root in p for root in roots))[:20]
    if only or leaked:
        diag["path_strings"] = {"only_in_one_image": only, "build_or_checkout_paths": leaked}
        diag["hints"].append(
            "absolute paths reach the image (__FILE__ in asserts/logging): extend -ffile-prefix-map in "
            "tools/build.py repro_cmake_args to the directory that leaks"
        )
    dates = sorted((_strings(blob_a, _DATE) | _strings(blob_a, _TIME) | _strings(blob_a, _ISO))
                   ^ (_strings(blob_b, _DATE) | _strings(blob_b, _TIME) | _strings(blob_b, _ISO)))
    if dates:
        diag["timestamps"] = dates[:20]
        diag["hints"].append(
            "__DATE__/__TIME__ or a generated timestamp differs: build.py sets SOURCE_DATE_EPOCH (GCC honours "
            "it); a generator that ignores it must be given a fixed value"
        )
    git = sorted(_strings(blob_a, _GITISH) ^ _strings(blob_b, _GITISH))
    if git:
        diag["git_strings"] = git
        diag["hints"].append("a git describe string differs (tags or dirty state differ between the builds)")
    if elf_a is not None and elf_b is not None:
        ids = (elf_a.section_bytes(".note.gnu.build-id"), elf_b.section_bytes(".note.gnu.build-id"))
        if ids[0] is not None and ids[0] != ids[1]:
            diag["build_id"] = [x.hex() if x else None for x in ids]
            diag["hints"].append("GNU build ids differ: link with -Wl,--build-id=none (EXTRA_LDFLAGS)")
    if not diag["hints"]:
        diag["hints"].append(
            "no path, timestamp, git or build-id string explains it: compare the symbols listed above "
            "(arm-zephyr-eabi-objdump -d on both ELFs) — LTO partitioning or an unordered generator"
        )
    return diag


# --------------------------------------------------------------------------
# Firmware check (inside the toolchain container)


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def snapshot_ignore(src: Path) -> Any:
    """copytree ``ignore``: SNAPSHOT_IGNORE_PATHS relative to ``src``, SNAPSHOT_IGNORE_NAMES anywhere."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).resolve().relative_to(src.resolve()).as_posix()
        skipped = set()
        for name in names:
            path = name if rel == "." else f"{rel}/{name}"
            if name in SNAPSHOT_IGNORE_NAMES or any(fnmatch.fnmatchcase(path, p) for p in SNAPSHOT_IGNORE_PATHS):
                skipped.add(name)
        return skipped

    return ignore


def snapshot(src: Path, dst: Path) -> None:
    """Copy the checkout (with .git and untracked files, without build output and caches)."""
    shutil.copytree(src, dst, symlinks=True, ignore=snapshot_ignore(src))


def _build(checkout: Path, build_root: Path, targets: list[str], skip_workspace_check: bool, log: Path) -> int:
    cmd = [
        sys.executable,
        str(checkout / "tools" / "build.py"),
        "--in-container",
        "--pristine",
        "--allow-resource-miss",
        "--build-root",
        str(build_root),
        "--out-root",
        "build/repro-out",
        *targets,
    ]
    if skip_workspace_check:
        cmd.insert(3, "--skip-workspace-check")
    print(f"$ {shlex.join(cmd)}", flush=True)
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            if line.startswith(("BUILD", "OK", "FAIL", "SKIP", "WARN", "NCS", "error")):
                print("    " + line.rstrip(), flush=True)
        return proc.wait()


def compare_target(name: str, out_a: Path, out_b: Path, roots: list[str]) -> dict[str, Any]:
    dir_a, dir_b = out_a / name, out_b / name
    result: dict[str, Any] = {"target": name}
    if not (dir_a / "zephyr.hex").is_file() and not (dir_b / "zephyr.hex").is_file():
        skipped = not (dir_a / "build.log").is_file()
        result["status"] = "skipped" if skipped else "build-failed"
        return result
    if not (dir_a / "zephyr.hex").is_file() or not (dir_b / "zephyr.hex").is_file():
        result["status"] = "build-failed"
        result["detail"] = "only one of the two builds produced zephyr.hex"
        return result
    files: dict[str, Any] = {}
    for fname in COMPARED_FILES:
        a, b = _sha256(dir_a / fname), _sha256(dir_b / fname)
        files[fname] = {"a": a, "b": b, "same": a is not None and a == b}
    result["files"] = files
    meta_a = json.loads((dir_a / "metadata.json").read_text(encoding="utf-8")) if (dir_a / "metadata.json").is_file() else {}
    meta_b = json.loads((dir_b / "metadata.json").read_text(encoding="utf-8")) if (dir_b / "metadata.json").is_file() else {}
    result["inputs"] = {
        key: {"same": (meta_a.get("inputs") or {}).get(key) == (meta_b.get("inputs") or {}).get(key),
              "value": (meta_a.get("inputs") or {}).get(key)}
        for key in INPUT_DIGESTS
    }
    result["verify_stack_ok"] = [(meta_a.get("verify_stack") or {}).get("ok"), (meta_b.get("verify_stack") or {}).get("ok")]
    images_same = all(files[f]["same"] for f in IMAGE_FILES)
    if images_same and files["zephyr.elf"]["same"]:
        result["status"] = "reproducible"
    elif images_same:
        result["status"] = "image-reproducible"
        result["detail"] = "zephyr.hex/.bin identical; zephyr.elf differs (debug information)"
    else:
        result["status"] = "DIFFERENT"
        result["diagnosis"] = diagnose(dir_a, dir_b, roots)
    result["sha256"] = {f: files[f]["a"] for f in COMPARED_FILES}
    return result


def run_firmware(args: argparse.Namespace, targets: list[str]) -> tuple[list[dict[str, Any]], int]:
    work = Path(args.work_root or Path(os.environ.get("CTAG_BUILD_ROOT", build.CONTAINER_BUILD)) / "repro")
    shutil.rmtree(work, ignore_errors=True)
    src_a = work / "a" / "cremind-tag"
    src_b = work / ALT_CHECKOUT
    print(f"snapshot {REPO_ROOT.as_posix()} -> {src_a.as_posix()} and {src_b.as_posix()}", flush=True)
    snapshot(REPO_ROOT, src_a)
    snapshot(src_a, src_b)
    logs = REPORT_DIR
    logs.mkdir(parents=True, exist_ok=True)
    rc_a = _build(src_a, work / BUILD_A, targets, False, logs / "build-a.log")
    rc_b = _build(src_b, work / BUILD_B, targets, True, logs / "build-b.log")
    out_a, out_b = src_a / "build" / "repro-out", src_b / "build" / "repro-out"
    roots = [src_a.as_posix(), src_b.as_posix(), (work / BUILD_A).as_posix(), (work / BUILD_B).as_posix()]
    results = [compare_target(name, out_a, out_b, roots) for name in targets]
    for result in results:
        if result["status"] in ("DIFFERENT", "build-failed"):
            for side, out in (("a", out_a), ("b", out_b)):
                dst = REPORT_DIR / result["target"] / side
                shutil.rmtree(dst, ignore_errors=True)
                dst.mkdir(parents=True)
                for fname in (*COMPARED_FILES, "zephyr.map", "metadata.json", "build.log"):
                    if (out / result["target"] / fname).is_file():
                        shutil.copy2(out / result["target"] / fname, dst / fname)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    print(f"builds exited {rc_a} / {rc_b} (logs: {build._display(logs)}/build-a.log, build-b.log)", flush=True)
    return results, 0


# --------------------------------------------------------------------------


def _print_summary(firmware: list[dict[str, Any]]) -> None:
    if firmware:
        print(f"\n{'target':20} {'status':20} {'zephyr.hex':14} {'zephyr.bin':14} {'zephyr.elf':14} kconfig/dts")
        for r in firmware:
            files = r.get("files") or {}

            def cell(f: str) -> str:
                entry = files.get(f)
                return "-" if not entry else ("same " + (entry["a"] or "")[:8]) if entry["same"] else "DIFFERS"

            inputs = r.get("inputs") or {}
            same_inputs = "-" if not inputs else "same" if all(v["same"] for v in inputs.values()) else "DIFFER"
            print(f"{r['target']:20} {r['status']:20} {cell('zephyr.hex'):14} {cell('zephyr.bin'):14} "
                  f"{cell('zephyr.elf'):14} {same_inputs}")
            for hint in (r.get("diagnosis") or {}).get("hints", []):
                print(f"    hint: {hint}")
            for item in (r.get("diagnosis") or {}).get("ranges", [])[:5]:
                print(f"    {item['start']} +{item['length']}: {item.get('section')} {item.get('symbol_a')}")


def _exit_status(firmware: list[dict[str, Any]]) -> int:
    statuses = [r["status"] for r in firmware]
    if any(s in ("DIFFERENT", "build-failed") for s in statuses):
        return EXIT_FAILED
    return EXIT_SKIPPED if "skipped" in statuses else EXIT_OK


def release_targets() -> list[str]:
    import release

    return [t for t, _ in release.eligible_targets()[0]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="*", help="firmware targets from tools/targets.yaml")
    parser.add_argument("--all", action="store_true", help="every target")
    parser.add_argument("--release-targets", action="store_true", help="the targets tools/release.py publishes")
    parser.add_argument("--in-container", action="store_true", help="already inside the NCS toolchain (CI)")
    parser.add_argument("--work-root", help="scratch directory for the two checkouts and builds "
                                            "(default: $CTAG_BUILD_ROOT/repro)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch checkouts and builds")
    parser.add_argument("--json", type=Path, help=f"report path (default: {build._display(REPORT_DIR)}/report.json)")
    parser.add_argument("--chown", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    targets_all, _ = build.load_matrix()
    names = list(targets_all) if args.all else release_targets() if args.release_targets else args.targets
    unknown = [n for n in names if n not in targets_all]
    if unknown or not names:
        parser.error(f"unknown target(s): {', '.join(unknown)}" if unknown
                     else "give target names, --all or --release-targets")

    if args.in_container:
        firmware_results, _ = run_firmware(args, names)
    else:
        inner = ["python3", f"{build.CONTAINER_REPO}/tools/repro_check.py", "--in-container", "--work-root",
                 f"{build.CONTAINER_BUILD}/repro", *names]
        if args.keep:
            inner.append("--keep")
        if hasattr(os, "getuid"):
            inner += ["--chown", f"{os.getuid()}:{os.getgid()}"]
        rc = build.run_docker(shlex.join(inner))
        report = REPORT_DIR / "report.json"
        if rc == EXIT_USAGE or not report.is_file():
            return rc if rc else EXIT_USAGE
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, args.json)
        return rc

    doc = {
        "tool": "repro_check",
        "version": 1,
        "toolchain": build.IMAGE,
        "ncs": build.NCS_REVISION,
        "firmware": firmware_results,
    }
    out = args.json or REPORT_DIR / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if args.chown:
        uid, gid = (int(x) for x in args.chown.split(":"))
        for path in [REPORT_DIR, *REPORT_DIR.rglob("*")]:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass
    _print_summary(firmware_results)
    print(f"report: {build._display(out)}")
    return _exit_status(firmware_results)


if __name__ == "__main__":
    sys.exit(main())
