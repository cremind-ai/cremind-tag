"""`cremind-tag firmware` — list, verify and flash released firmware; read a device's identity.

Firmware comes from a release (``dist/<version>/release.json``, docs/releasing.md)
or from a local build (``build/<target>/`` with the ``metadata.json`` that
tools/build.py writes). Before anything is flashed the image is verified: its
SHA-256 must be the recorded one, it must be the requested target (same board
and SoC), its address range must fit the SoC, and the build must have passed
tools/verify_stack.py.

Gateways and bridges are flashed here, erasing only the pages the image covers
(the mesh network and settings survive). Tags are flashed by ``cremind-tag tag
enroll --firmware``, which also gives them their identity.

The programmers are the enrollment ones (cremind_tag.enroll.tools: nRF Util,
nrfjprog, J-Link Commander); every command is built first, so ``--dry-run``
prints exactly what a real run executes (and writes J-Link command files).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from cremind_tag.cli._hardware import console, err_console, fail, print_json, table

if TYPE_CHECKING:
    from cremind_tag.enroll.tools import CommandRunner, SwdTool, Target, ToolCommand, ToolInfo

app = typer.Typer(name="firmware", help="List, verify and flash released firmware (gateways, bridges).",
                  no_args_is_help=True)

# SoC key (tools/targets.yaml) -> (J-Link device, nrfjprog family, flash bytes). Mirrors
# SOC_DEVICES in tools/release.py (tests/firmware checks both against targets.yaml).
SOC_DEVICES: dict[str, tuple[str, str, int]] = {
    "nrf51822_qfaa": ("nRF51822_xxAA", "NRF51", 262144),
    "nrf51822_qfab": ("nRF51822_xxAB", "NRF51", 131072),
    "nrf52810_qfaa": ("nRF52810_xxAA", "NRF52", 196608),
    "nrf52811_qfaa": ("nRF52811_xxAA", "NRF52", 196608),
    "nrf52832_qfaa": ("nRF52832_xxAA", "NRF52", 524288),
    "nrf52840_qiaa": ("nRF52840_xxAA", "NRF52", 1048576),
}
FLASHABLE_ROLES = ("gateway", "bridge")
UICR_BASE = 0x10001000

# Hooks for tests: tool detection and the command runner (None = subprocess).
_detect: Any = None
_RUNNER: CommandRunner | None = None


# -- images ---------------------------------------------------------------------------------------


@dataclass
class FirmwareImage:
    """One firmware target as a release manifest or a build's metadata.json describes it."""

    target: str
    role: str
    board: str
    soc: str
    version: str | None
    hex_path: Path | None
    expected_sha256: str | None
    source: str
    """``release.json`` or ``metadata.json``."""
    release_status: str | None = None
    hardware: str | None = None
    metadata: dict[str, Any] | None = None
    release_root: Path | None = None
    manifest_entry: dict[str, Any] | None = None

    @property
    def verify_stack_ok(self) -> bool | None:
        vs = (self.metadata or {}).get("verify_stack") or (self.manifest_entry or {}).get("verify_stack")
        return None if vs is None else bool(vs.get("ok"))

    @property
    def build_status(self) -> str | None:
        return (self.metadata or {}).get("status")

    def row(self) -> dict[str, Any]:
        return {"target": self.target, "role": self.role, "board": self.board, "soc": self.soc,
                "version": self.version, "release_status": self.release_status,
                "verify_stack": self.verify_stack_ok, "hex": str(self.hex_path) if self.hex_path else None,
                "sha256": self.expected_sha256, "source": self.source}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError) as exc:
        fail(f"cannot read {path}: {exc}")


def _from_metadata(meta: dict[str, Any], directory: Path, source: str = "metadata.json") -> FirmwareImage:
    hex_path = directory / "zephyr.hex"
    return FirmwareImage(
        target=meta.get("target", directory.name), role=meta.get("role", "?"), board=meta.get("board", "?"),
        soc=meta.get("soc", "?"), version=meta.get("version"), hex_path=hex_path if hex_path.is_file() else None,
        expected_sha256=((meta.get("artifacts") or {}).get("zephyr.hex") or {}).get("sha256"), source=source,
        release_status=meta.get("hardware_status"), hardware=meta.get("hardware"), metadata=meta)


def images_from_release(manifest_path: Path) -> list[FirmwareImage]:
    doc = _read_json(manifest_path)
    if not str(doc.get("schema", "")).startswith("cremind-tag/release@"):
        fail(f"{manifest_path} is not a Cremind Tag release manifest")
    root = manifest_path.parent
    images = []
    for entry in doc.get("firmware") or []:
        files = entry.get("files") or {}
        meta_file = root / files["metadata"] if files.get("metadata") else None
        images.append(FirmwareImage(
            target=entry["target"], role=entry["role"], board=entry["board"], soc=entry["soc"],
            version=entry.get("version"), hex_path=root / files["hex"] if files.get("hex") else None,
            expected_sha256=(entry.get("sha256") or {}).get("hex"), source="release.json",
            release_status=entry.get("release_status"), hardware=entry.get("hardware"),
            metadata=_read_json(meta_file) if meta_file and meta_file.is_file() else None,
            release_root=root, manifest_entry=entry))
    return images


def find_manifest(source: Path) -> Path | None:
    if source.is_file() and source.name.endswith(".json"):
        return source if "release" in source.name else None
    if (source / "release.json").is_file():
        return source / "release.json"
    candidates = sorted(source.glob("*/release.json"))  # dist/ with dist/<version>/release.json
    return candidates[-1] if candidates else None


def images_from_source(source: Path) -> list[FirmwareImage]:
    """A release manifest, a dist/ or dist/<version>/ directory, or a build/ directory."""
    manifest = find_manifest(source)
    if manifest is not None:
        return images_from_release(manifest)
    if source.is_dir() and (source / "metadata.json").is_file():
        return [_from_metadata(_read_json(source / "metadata.json"), source)]
    if source.is_dir():
        return [_from_metadata(_read_json(p), p.parent) for p in sorted(source.glob("*/metadata.json"))]
    return []


def image_for_hex(hex_path: Path, manifest: Path | None, target: str | None) -> FirmwareImage | None:
    """The recorded description of ``hex_path``: from ``manifest``, a sibling metadata file or a parent release."""
    hex_path = hex_path.resolve()
    manifests = [manifest] if manifest else [p / "release.json" for p in list(hex_path.parents)[:4]
                                              if (p / "release.json").is_file()]
    for m in manifests:
        for image in images_from_release(m):
            if image.hex_path is not None and image.hex_path.resolve() == hex_path:
                return image
        if target is not None and manifest is not None:
            match = next((i for i in images_from_release(m) if i.target == target), None)
            if match is not None:
                return match  # verify_image reports the checksum difference
    for sibling in (hex_path.with_name(hex_path.stem + ".metadata.json"), hex_path.with_name("metadata.json")):
        if sibling.is_file():
            image = _from_metadata(_read_json(sibling), hex_path.parent, source=sibling.name)
            image.hex_path = hex_path
            return image
    return None


# -- verification ----------------------------------------------------------------------------------


@dataclass
class Check:
    id: str
    ok: bool
    detail: str
    level: str = "error"
    """``error`` blocks flashing, ``warning`` does not."""


@dataclass
class HexInfo:
    start: int
    end: int
    size: int
    uicr: bool
    segments: list[tuple[int, int]] = field(default_factory=list)


def hex_info(text: str) -> HexInfo:
    """Address range of an Intel HEX image; ``uicr`` = it writes the UICR page."""
    base = 0
    addresses: list[tuple[int, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {number} is not an Intel HEX record")
        data = bytes.fromhex(line[1:])
        if len(data) < 5 or sum(data) & 0xFF or len(data) != data[0] + 5:
            raise ValueError(f"line {number}: bad record")
        count, addr, rtype = data[0], int.from_bytes(data[1:3], "big"), data[3]
        if rtype == 0x00 and count:
            addresses.append((base + addr, base + addr + count))
        elif rtype == 0x02:
            base = int.from_bytes(data[4:6], "big") << 4
        elif rtype == 0x04:
            base = int.from_bytes(data[4:6], "big") << 16
        elif rtype == 0x01:
            break
    if not addresses:
        raise ValueError("no data records")
    segments: list[tuple[int, int]] = []
    for start, end in sorted(addresses):
        if segments and start <= segments[-1][1]:
            segments[-1] = (segments[-1][0], max(end, segments[-1][1]))
        else:
            segments.append((start, end))
    flash = [s for s in segments if s[0] < UICR_BASE]
    return HexInfo(start=min(s[0] for s in segments), end=max(s[1] for s in flash) if flash else 0,
                   size=sum(e - s for s, e in segments), uicr=any(s[0] >= UICR_BASE for s in segments),
                   segments=segments)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_image(hex_path: Path, image: FirmwareImage | None, target: str | None) -> list[Check]:
    """Everything that must hold before ``hex_path`` is flashed as ``target``."""
    checks: list[Check] = []
    if not hex_path.is_file():
        return [Check("file", False, f"{hex_path} does not exist")]
    if image is None:
        return [Check("metadata", False, f"no release.json entry or metadata.json describes {hex_path.name}; "
                                         "pass --manifest, or keep the build's metadata.json beside it")]
    actual = sha256_file(hex_path)
    if image.expected_sha256 is None:
        checks.append(Check("sha256", False, "no SHA-256 recorded for this image"))
    else:
        checks.append(Check("sha256", actual == image.expected_sha256,
                            f"{actual[:16]}... " + ("matches" if actual == image.expected_sha256 else
                                                    f"!= recorded {image.expected_sha256[:16]}... ({image.source})")))
    if image.release_root is not None and (image.release_root / "SHA256SUMS").is_file():
        rel = hex_path.resolve().relative_to(image.release_root.resolve()).as_posix() \
            if hex_path.resolve().is_relative_to(image.release_root.resolve()) else None
        sums: dict[str, str] = {}
        for line in (image.release_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, sep, name = line.partition("  ")
            if sep:
                sums[name] = digest
        if rel is not None:
            listed = sums.get(rel)
            checks.append(Check("sha256sums", listed == actual,
                                f"SHA256SUMS lists {rel}" + ("" if listed == actual else " with another digest"
                                                             if listed else " not at all")))
    if target is not None:
        checks.append(Check("target", image.target == target,
                            f"image is {image.target}" + ("" if image.target == target else f", not {target}")))
    meta = image.metadata or {}
    if image.manifest_entry is not None and meta:
        same = all(meta.get(k) == image.manifest_entry.get(k) for k in ("target", "board", "soc", "role"))
        checks.append(Check("board", same, f"{image.board} ({image.soc})" + ("" if same else
                            f"; metadata says {meta.get('board')} ({meta.get('soc')})")))
    else:
        checks.append(Check("board", image.soc in SOC_DEVICES, f"{image.board} ({image.soc})"))
    try:
        info = hex_info(hex_path.read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        checks.append(Check("range", False, f"not a valid Intel HEX file: {exc}"))
    else:
        flash = SOC_DEVICES.get(image.soc, (None, None, 0))[2]
        fits = info.end <= flash and info.start >= 0
        checks.append(Check("range", fits, f"0x{info.start:08x}..0x{info.end:08x} ({info.size:,} B)"
                            + ("" if fits else f" exceeds the {flash // 1024} KiB of {image.soc}")))
        if info.uicr and image.role in FLASHABLE_ROLES:
            checks.append(Check("uicr", True, "the image writes UICR", level="warning"))
    vs_ok = image.verify_stack_ok
    checks.append(Check("verify_stack", bool(vs_ok),
                        "passed" if vs_ok else "no verify_stack result" if vs_ok is None else "FAILED"))
    if image.build_status is not None:
        checks.append(Check("build", image.build_status == "ok", f"build status {image.build_status}"))
    status = image.release_status
    if status != "qualified":
        checks.append(Check("qualified", False, f"release status {status or 'unknown'} (not qualified: first "
                                                "samples, see docs/hardware/matrix.md)", level="warning"))
    return checks


def _errors(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.level == "error"]


def _print_checks(checks: list[Check]) -> None:
    for c in checks:
        mark = "[green]ok[/green]  " if c.ok else ("[red]FAIL[/red]" if c.level == "error" else "[yellow]warn[/yellow]")
        console.print(f"  {mark} {c.id:13} {c.detail}", highlight=False, soft_wrap=True)


# -- tools ---------------------------------------------------------------------------------------


def _config_defaults() -> tuple[str, str | None]:
    try:
        from cremind_tag.config import load_config

        hw = load_config().hardware
        return hw.jlink_tool, hw.jlink_serial
    except Exception:  # no or broken config: the command-line defaults apply
        return "auto", None


def swd_target(soc: str, hardware: str | None = None) -> Target:
    from cremind_tag.enroll.hardware import BOARD_ALIASES
    from cremind_tag.enroll.tools import Family, Target

    try:
        device, family, _ = SOC_DEVICES[soc]
    except KeyError:
        fail(f"unknown SoC {soc!r} (known: {', '.join(SOC_DEVICES)})")
    board = BOARD_ALIASES.get(hardware or "")
    return Target(board, Family(family), device)  # type: ignore[arg-type]


def pick_tool(preference: str | None, target: Target, serial: str | None, *, allow_missing: bool,
              workdir: Path | None = None) -> SwdTool:
    """The first detected tool for ``auto``, else the one named; with ``allow_missing`` a bare one for dry runs."""
    from cremind_tag.enroll.tools import (
        INSTALL_HINT,
        TOOL_NAMES,
        TOOL_PREFERENCES,
        ToolError,
        detect_tools,
        jlink_executable_name,
        make_tool,
    )

    preference = (preference or "auto").strip().lower()
    if preference not in TOOL_PREFERENCES:
        fail(f"unknown tool {preference!r} (choose from {', '.join(TOOL_PREFERENCES)})")
    detect = _detect or detect_tools
    found: list[ToolInfo] = detect(runner=_RUNNER) if _RUNNER is not None else detect()
    if preference != "auto":
        found = [t for t in found if t.name == preference]
    if found:
        return make_tool(found[0].name, found[0].path, target, serial_number=serial, runner=_RUNNER,
                         workdir=workdir)
    wanted = TOOL_NAMES[0] if preference == "auto" else preference
    if not allow_missing:
        raise ToolError(f"{'no SWD tool found' if preference == 'auto' else wanted + ' not found'}: {INSTALL_HINT}")
    exe = jlink_executable_name() if wanted == "jlink" else wanted
    return make_tool(wanted, exe, target, serial_number=serial, runner=_RUNNER, workdir=workdir)


def verify_device_command(tool: SwdTool, hex_path: Path, bin_path: Path | None, base: int) -> ToolCommand:
    """Compare the device's memory with an image without programming it."""
    from cremind_tag.enroll.tools import TIMEOUT_PROGRAM, JLinkTool, NrfjprogTool, NrfutilTool

    if isinstance(tool, NrfutilTool):
        return tool._command(f"compare the device with {hex_path.name}", "fw-verify", "--firmware", str(hex_path),
                             timeout=TIMEOUT_PROGRAM)
    if isinstance(tool, NrfjprogTool):
        return tool._command(f"compare the device with {hex_path.name}", "--verify", str(hex_path),
                             timeout=TIMEOUT_PROGRAM)
    assert isinstance(tool, JLinkTool) and bin_path is not None
    path = str(bin_path)
    quoted = f'"{path}"' if any(c.isspace() for c in path) else path
    return tool._script("verify", f"compare the device with {bin_path.name}",
                        ["h", f"verifybin {quoted}, 0x{base:08X}"], timeout=TIMEOUT_PROGRAM)


def hex_to_bin(text: str, out: Path) -> int:
    """Write the flash part of an Intel HEX image as a raw binary (gaps 0xFF); returns its base address."""
    memory: dict[int, int] = {}
    base = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        data = bytes.fromhex(line[1:])
        count, addr, rtype = data[0], int.from_bytes(data[1:3], "big"), data[3]
        if rtype == 0x00:
            for i, b in enumerate(data[4:4 + count]):
                if base + addr + i < UICR_BASE:
                    memory[base + addr + i] = b
        elif rtype == 0x02:
            base = int.from_bytes(data[4:6], "big") << 4
        elif rtype == 0x04:
            base = int.from_bytes(data[4:6], "big") << 16
        elif rtype == 0x01:
            break
    start, end = min(memory), max(memory) + 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(memory.get(a, 0xFF) for a in range(start, end)))
    return start


# -- FICR / UICR ----------------------------------------------------------------------------------

# (label, address, length) per family; UICR.CUSTOMER (the tag secret) is never read.
IDENTITY_READS: dict[str, list[tuple[str, int, int]]] = {
    "NRF52": [("FICR", 0x10000010, 0x58), ("FICR.INFO", 0x10000100, 0x14), ("UICR", 0x10001200, 0x10)],
    "NRF51": [("FICR", 0x10000010, 0x58), ("UICR", 0x10001000, 0x14)],
}
_PACKAGES = {0x2000: "QF", 0x2001: "CH", 0x2002: "CI", 0x2003: "QC", 0x2004: "QI", 0x2005: "CK"}


def _word(mem: dict[int, int], addr: int) -> int | None:
    try:
        return int.from_bytes(bytes(mem[addr + i] for i in range(4)), "little")
    except KeyError:
        return None


def decode_identity(family: str, mem: dict[int, int]) -> dict[str, Any]:
    """FICR/UICR words (address -> byte) as readable fields."""
    out: dict[str, Any] = {}
    page, pages = _word(mem, 0x10000010), _word(mem, 0x10000014)
    if page is not None and pages is not None:
        out["flash_bytes"] = page * pages
        out["code_page_size"] = page
    lo, hi = _word(mem, 0x10000060), _word(mem, 0x10000064)
    if lo is not None and hi is not None:
        out["device_id"] = f"{(hi << 32) | lo:016X}"
    if family == "NRF52":
        part, variant, package = _word(mem, 0x10000100), _word(mem, 0x10000104), _word(mem, 0x10000108)
        ram, flash = _word(mem, 0x1000010C), _word(mem, 0x10000110)
        if part is not None:
            out["part"] = f"nRF{part:X}"
        if variant is not None:
            out["variant"] = variant.to_bytes(4, "big").decode("ascii", "replace") if variant != 0xFFFFFFFF \
                else "unspecified"
        if package is not None:
            out["package"] = _PACKAGES.get(package, f"0x{package:X}")
        if ram is not None:
            out["ram_kib"] = ram
        if flash is not None:
            out["flash_kib"] = flash
        prot = _word(mem, 0x10001208)
        if prot is not None:
            low = prot & 0xFF
            out["approtect"] = ("erased (open; newer revisions protect it by hardware)" if prot == 0xFFFFFFFF
                                else "HwDisabled (open)" if low == 0x5A else "ENABLED (protected)" if low == 0x00
                                else f"0x{prot:08X}")
        for i, addr in enumerate((0x10001200, 0x10001204)):
            value = _word(mem, addr)
            if value is not None:
                out[f"pselreset{i}"] = "not set" if value == 0xFFFFFFFF else \
                    f"P0.{value & 0x1F}{'' if value >> 31 == 0 else ' (disconnected)'}"
    else:
        config = _word(mem, 0x1000005C)
        blocks, size = _word(mem, 0x10000034), _word(mem, 0x10000038)
        if config is not None:
            out["hwid"] = f"0x{config & 0xFFFF:04X}"
        if blocks is not None and size is not None:
            out["ram_bytes"] = blocks * size
        rbp = _word(mem, 0x10001004)
        if rbp is not None:
            pall = (rbp >> 8) & 0xFF
            out["rbpconf"] = "disabled (open)" if pall == 0xFF and (rbp & 0xFF) == 0xFF else \
                f"PALL {'ENABLED' if pall == 0 else 'off'}, PR0 {'ENABLED' if rbp & 0xFF == 0 else 'off'}"
    return out


# -- commands -------------------------------------------------------------------------------------


@app.command("list")
def list_(
    source: Path = typer.Argument(Path("dist"), help="release.json, dist/ or dist/<version>/, or a build/ directory."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Firmware targets in a release (or a local build)."""
    images = images_from_source(source)
    if not images:
        fail(f"no release.json and no <target>/metadata.json under {source}")
    if as_json:
        print_json([i.row() for i in images])
        return
    t = table(None, "Target", "Role", "Board", "Version", "Status", "verify_stack")
    t.columns[0].no_wrap = True
    for i in images:
        t.add_row(i.target, i.role, i.board, i.version or "-", i.release_status or "-",
                  "pass" if i.verify_stack_ok else "FAIL" if i.verify_stack_ok is False else "-")
    console.print(f"Firmware in {source} ({images[0].source}):", highlight=False, soft_wrap=True)
    console.print(t)
    for i in images:
        console.print(f"  {i.target}: {i.hex_path or '-'}  sha256 {i.expected_sha256 or '-'}", highlight=False,
                      soft_wrap=True)


@app.command()
def verify(
    hex_path: Path = typer.Option(..., "--hex", help="Intel HEX image to check."),
    target: str | None = typer.Option(None, "--target", help="Target it must be (e.g. gateway-nrf52840dk)."),
    manifest: Path | None = typer.Option(None, "--manifest", help="release.json (default: found beside the hex)."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Checksum and metadata checks of an image (what `flash` runs first). Exit 1 on a failure."""
    image = image_for_hex(hex_path, manifest, target)
    checks = verify_image(hex_path, image, target)
    errors = _errors(checks)
    if as_json:
        print_json({"hex": str(hex_path), "image": image.row() if image else None, "ok": not errors,
                    "checks": [asdict(c) for c in checks]})
    else:
        console.print(f"{hex_path}: {image.target if image else 'unknown image'} "
                      f"{image.version if image else ''}".rstrip(), highlight=False, soft_wrap=True)
        _print_checks(checks)
        console.print("[green]verified[/green]" if not errors else f"[red]{len(errors)} check(s) failed[/red]")
    if errors:
        raise typer.Exit(1)


ToolOpt = typer.Option(None, "--tool", help="auto | nrfutil | nrfjprog | jlink (default: config hardware.jlink_tool).")
SnrOpt = typer.Option(None, "--snr", "--serial-number", help="J-Link probe serial number (several probes attached).")


@app.command()
def flash(
    target: str = typer.Option(..., "--target", help="Gateway or bridge target, e.g. gateway-nrf52840dk."),
    hex_path: Path = typer.Option(..., "--hex", help="Intel HEX image from a release or build/<target>/."),
    manifest: Path | None = typer.Option(None, "--manifest", help="release.json (default: found beside the hex)."),
    tool: str | None = ToolOpt,
    snr: str | None = SnrOpt,
    erase: str = typer.Option("touched", "--erase", help="touched: only the pages the image covers (keeps the "
                                                         "mesh network and settings); all: the whole chip."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before --erase all."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify and print the exact commands; run nothing."),
    script_dir: Path | None = typer.Option(None, "--script-dir", help="Where J-Link command files go."),
) -> None:
    """Verify, then program a gateway or bridge over SWD (J-Link) and reset it."""
    from cremind_tag.enroll.tools import EraseMode, ToolError

    image = image_for_hex(hex_path, manifest, target)
    checks = verify_image(hex_path, image, target)
    console.print(f"{hex_path}: {image.target if image else 'unknown image'}", highlight=False, soft_wrap=True)
    _print_checks(checks)
    if _errors(checks):
        fail("verification failed: nothing was flashed")
    assert image is not None
    if image.role not in FLASHABLE_ROLES:
        board = image.hardware or ("nrf52dk_tag" if image.board.startswith("nrf52dk/") else image.board)
        fail(f"{image.target} is a {image.role}: tags are flashed with their identity by "
             f"`cremind-tag tag enroll --board {board} --firmware {hex_path}`")
    modes = {"touched": EraseMode.TOUCHED, "all": EraseMode.ALL}
    if erase not in modes:
        fail("--erase must be touched or all")
    default_tool, default_snr = _config_defaults()
    try:
        swd = pick_tool(tool or default_tool, swd_target(image.soc, image.hardware), snr or default_snr,
                        allow_missing=dry_run, workdir=script_dir)
    except ToolError as exc:
        fail(str(exc))
    commands = [swd.program_command(hex_path, erase=modes[erase], verify=True), swd.reset_command()]
    if dry_run:
        typer.echo(f"\nDry run ({swd.label}, {swd.target.jlink_device}):")
        for command in commands:
            swd.prepare(command)  # J-Link command files, so the commands can be run by hand
            typer.echo(command.render())
        return
    if modes[erase] is EraseMode.ALL and not yes and not typer.confirm(
            f"Erase the whole {image.soc} (mesh network, settings and keys are lost)?", default=False):
        fail("cancelled")
    try:
        swd.run(commands[0])
        console.print(f"programmed {hex_path.name} with {swd.label} ({swd.target.jlink_device})")
        try:
            swd.run(commands[1])
        except ToolError as exc:
            err_console.print(f"[yellow]warning:[/yellow] reset failed, power-cycle the board: {exc}")
    except ToolError as exc:
        fail(str(exc))
    console.print(f"[green]{image.target} {image.version or ''} flashed[/green]")


@app.command()
def info(
    target: str | None = typer.Option(None, "--target", help="Target (its SoC from --manifest or the hex's metadata)."),
    soc: str | None = typer.Option(None, "--soc", help=f"SoC instead of a target: {', '.join(SOC_DEVICES)}."),
    hex_path: Path | None = typer.Option(None, "--hex", help="Also compare the device's flash with this image."),
    manifest: Path | None = typer.Option(None, "--manifest", help="release.json naming the target."),
    tool: str | None = ToolOpt,
    snr: str | None = SnrOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the exact commands; run nothing."),
    script_dir: Path | None = typer.Option(None, "--script-dir", help="Where J-Link command files go."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Read the device's FICR (part, variant, memory, device id) and UICR protection over SWD."""
    from cremind_tag.enroll.tools import ToolError, parse_memory_dump

    hardware = None
    image = image_for_hex(hex_path, manifest, target) if hex_path else None
    if image is None and target and manifest:
        image = next((i for i in images_from_release(manifest) if i.target == target), None)
    if soc is None and image is not None:
        soc, hardware = image.soc, image.hardware
    if soc is None:
        fail("give --soc, or --target with --manifest (or --hex with its metadata)")
    default_tool, default_snr = _config_defaults()
    try:
        swd = pick_tool(tool or default_tool, swd_target(soc, hardware), snr or default_snr, allow_missing=dry_run,
                        workdir=script_dir)
    except ToolError as exc:
        fail(str(exc))
    family = swd.target.family.value
    reads = [(label, swd.read_memory_command(addr, length), addr, length)
             for label, addr, length in IDENTITY_READS[family]]
    compare = None
    if hex_path is not None:
        bin_path = None
        base = 0
        if swd.name == "jlink":
            bin_path = swd.workdir / f"{hex_path.stem}.verify.bin"
            base = hex_to_bin(hex_path.read_text(encoding="ascii"), bin_path)
        compare = verify_device_command(swd, hex_path, bin_path, base)
    if dry_run:
        typer.echo(f"Dry run ({swd.label}, {swd.target.jlink_device}):")
        for command in [c for _, c, _, _ in reads] + ([compare] if compare else []):
            swd.prepare(command)
            typer.echo(command.render())
        return

    memory: dict[int, int] = {}
    errors: list[str] = []
    for label, command, addr, length in reads:
        try:
            result = swd.run(command)
            data = parse_memory_dump(result.stdout, addr, length)
        except (ToolError, ValueError) as exc:
            errors.append(f"{label}: {str(exc).splitlines()[0]} (readback protection?)")
            continue
        memory.update({addr + i: b for i, b in enumerate(data)})
    fields = decode_identity(family, memory)
    doc: dict[str, Any] = {"tool": swd.name, "device": swd.target.jlink_device, "soc": soc, **fields,
                           "errors": errors}
    if compare is not None:
        try:
            swd.run(compare)
            doc["image"] = {"hex": str(hex_path), "matches": True}
        except ToolError as exc:
            doc["image"] = {"hex": str(hex_path), "matches": False, "detail": str(exc).splitlines()[0]}
        if image is not None:
            doc["image"].update(target=image.target, version=image.version)
    if as_json:
        print_json(doc)
    else:
        t = table(f"{swd.target.jlink_device} via {swd.label}", "Field", "Value")
        for key, value in doc.items():
            if key not in ("errors",):
                t.add_row(key, json.dumps(value) if isinstance(value, dict) else str(value))
        console.print(t)
        for e in errors:
            err_console.print(f"[yellow]warning:[/yellow] {e}")
    if errors and not fields:
        raise typer.Exit(1)
    if compare is not None and not doc["image"]["matches"]:
        raise typer.Exit(1)


__all__ = ["SOC_DEVICES", "FirmwareImage", "app", "decode_identity", "hex_info", "images_from_source",
           "verify_image"]
