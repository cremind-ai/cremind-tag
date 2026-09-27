#!/usr/bin/env python3
"""Verify that a Zephyr build links only the Zephyr Bluetooth controller.

Implements every check of docs/firmware-notes.md section 1 (resolved Kconfig,
devicetree, link map) plus:

- the exact memory geometry of the target SoC (devicetree flash/SRAM sizes and
  the linker's FLASH/RAM regions in zephyr.map), and that the image stays out
  of the storage partition;
- ``CONFIG_BT_CTLR_CRYPTO=y`` (firmware-notes correction 8);
- no legacy mesh advertiser on bridges and gateways (correction 4).

The devicetree is read with the build's own pickled edtlib (``edt.pickle``)
when ``devicetree.edtlib`` is importable (inside the NCS container, or with
``--zephyr-base``); otherwise the checks fall back to parsing
``devicetree_generated.h``. The report states which method ran.

Usage::

    verify_stack.py ARTIFACTS --target tag-laowu-bw [--json report.json]
    verify_stack.py ARTIFACTS --soc nrf51822_qfab --role tag

ARTIFACTS is a directory written by tools/build.py (flat files) or a Zephyr
build directory. Exit status: 0 = every check passed (warnings allowed),
1 = a check failed, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import struct
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS_FILE = REPO_ROOT / "tools" / "targets.yaml"

LL_SW_SPLIT_COMPAT = "zephyr,bt-hci-ll-sw-split"
SDC_COMPAT = "nordic,bt-hci-sdc"
REQUIRED_LL_SYMBOLS = ("lll_init", "ticker_init", "radio_isr_set", "ll_adv_enable")
FORBIDDEN_SYMBOL_PREFIXES = ("sdc_", "mpsl_")
MESH_ROLES = ("bridge", "gateway")
ROLES = ("gateway", "bridge", "tag")

# Flat names written by build.py, then the Zephyr build-directory layout.
ARTIFACT_PATHS: dict[str, tuple[str, ...]] = {
    "config": (".config", "zephyr/.config"),
    "map": ("zephyr.map", "zephyr/zephyr.map"),
    "dt_header": (
        "devicetree_generated.h",
        "zephyr/include/generated/zephyr/devicetree_generated.h",
    ),
    "edt": ("edt.pickle", "zephyr/edt.pickle"),
    "elf": ("zephyr.elf", "zephyr/zephyr.elf"),
}

Status = Literal["pass", "fail", "warn", "skip"]


class UsageError(Exception):
    """Bad arguments or configuration (exit status 2)."""


@dataclass(frozen=True)
class SocGeometry:
    name: str
    series: str
    flash: int
    ram: int


@dataclass(frozen=True)
class Region:
    name: str
    origin: int
    length: int

    @property
    def end(self) -> int:
        return self.origin + self.length

    def contains(self, address: int) -> bool:
        return self.origin <= address < self.end


@dataclass
class Check:
    id: str
    description: str
    status: Status
    detail: str = ""


@dataclass
class DtFacts:
    """The devicetree facts the checks need, from edtlib or the generated header."""

    method: str
    bt_hci_node: str | None = None
    bt_hci_compats: list[str] = field(default_factory=list)
    sdc_okay: list[str] = field(default_factory=list)
    flash_size: int | None = None
    sram_size: int | None = None
    storage: tuple[int, int] | None = None  # (absolute address, size)
    note: str = ""


@dataclass
class MemoryUsage:
    flash_region: int
    flash_used: int
    ram_region: int
    ram_used: int

    @property
    def flash_free(self) -> int:
        return self.flash_region - self.flash_used

    @property
    def ram_free(self) -> int:
        return self.ram_region - self.ram_used

    def as_dict(self) -> dict[str, int | float]:
        return {
            "flash_region": self.flash_region,
            "flash_used": self.flash_used,
            "flash_free": self.flash_free,
            "flash_used_pct": round(100.0 * self.flash_used / self.flash_region, 2) if self.flash_region else 0.0,
            "ram_region": self.ram_region,
            "ram_used": self.ram_used,
            "ram_free": self.ram_free,
        }


@dataclass
class Report:
    artifacts: str
    target: str | None
    soc: str
    role: str
    dt_method: str
    checks: list[Check]
    memory: dict[str, int | float] | None
    dt_note: str = ""

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in ("pass", "fail", "warn", "skip")}
        return {
            "tool": "verify_stack",
            "version": 1,
            "ok": self.ok,
            "target": self.target,
            "soc": self.soc,
            "role": self.role,
            "artifacts": self.artifacts,
            "dt_method": self.dt_method,
            "dt_note": self.dt_note,
            "counts": counts,
            "checks": [asdict(c) for c in self.checks],
            "memory": self.memory,
        }


# --------------------------------------------------------------------------
# Target matrix


def load_targets(path: Path = TARGETS_FILE) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "socs" not in data or "targets" not in data:
        raise UsageError(f"{path}: expected top-level 'socs' and 'targets'")
    return data


def soc_geometry(matrix: dict[str, Any], soc: str) -> SocGeometry:
    try:
        entry = matrix["socs"][soc]
    except KeyError:
        raise UsageError(f"unknown soc '{soc}' (known: {', '.join(matrix['socs'])})") from None
    return SocGeometry(name=soc, series=entry["series"], flash=int(entry["flash"]), ram=int(entry["ram"]))


# --------------------------------------------------------------------------
# Artifact parsing


def locate_artifacts(root: Path) -> dict[str, Path | None]:
    found: dict[str, Path | None] = {}
    for key, candidates in ARTIFACT_PATHS.items():
        found[key] = next((root / c for c in candidates if (root / c).is_file()), None)
    return found


def parse_kconfig(text: str) -> dict[str, str]:
    """Resolved .config as {CONFIG_NAME: value}; '# ... is not set' lines are absent."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(CONFIG_\w+)=(.*)$", line)
        if m:
            values[m[1]] = m[2]
    return values


def parse_map_regions(text: str) -> dict[str, Region]:
    regions: dict[str, Region] = {}
    start = text.find("Memory Configuration")
    if start < 0:
        return regions
    end = text.find("Linker script and memory map", start)
    block = text[start : end if end >= 0 else len(text)]
    for line in block.splitlines():
        m = re.match(r"^(\S+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)", line)
        if m and m[1] != "*default*":
            regions[m[1]] = Region(m[1], int(m[2], 16), int(m[3], 16))
    return regions


def map_symbols(text: str) -> set[str]:
    """Linked symbols plus function/data names from linked input section names.

    Only the part after "Linker script and memory map" counts: the earlier
    "Discarded input sections" block lists what --gc-sections removed.
    """
    start = text.find("Linker script and memory map")
    linked = text[start:] if start >= 0 else text
    names = set(re.findall(r"(?m)^\s+0x[0-9a-fA-F]+\s+([A-Za-z_][\w$]*)\s*$", linked))
    names.update(re.findall(r"(?m)^\s*\.(?:text|rodata|data|bss|noinit)\.([A-Za-z_]\w*)", linked))
    return names


def map_forbidden_libraries(text: str) -> list[str]:
    return sorted(set(re.findall(r"lib(?:softdevice_controller|mpsl)[\w.+-]*\.a", text)))


def elf_memory_usage(data: bytes, flash: Region, ram: Region) -> MemoryUsage:
    """Used bytes per region, as GNU ld --print-memory-usage counts them.

    FLASH: highest load address (LMA) of loadable file content; RAM: highest
    end of any allocated section (includes .bss and .noinit stacks).
    """
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a 32-bit little-endian ELF file")
    (_, _, _, _, phoff, shoff, _, _, phentsize, phnum, shentsize, shnum, _) = struct.unpack_from(
        "<HHIIIIIHHHHHH", data, 16
    )
    flash_end = flash.origin
    for i in range(phnum):
        p_type, _, _, p_paddr, p_filesz, _, _, _ = struct.unpack_from("<IIIIIIII", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz and flash.contains(p_paddr):
            flash_end = max(flash_end, p_paddr + p_filesz)
    ram_end = ram.origin
    for i in range(shnum):
        _, _, sh_flags, sh_addr, _, sh_size, _, _, _, _ = struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize)
        if sh_flags & 0x2 and sh_size and ram.contains(sh_addr):
            ram_end = max(ram_end, sh_addr + sh_size)
    return MemoryUsage(
        flash_region=flash.length,
        flash_used=flash_end - flash.origin,
        ram_region=ram.length,
        ram_used=ram_end - ram.origin,
    )


# --------------------------------------------------------------------------
# Devicetree


class EdtUnavailable(Exception):
    pass


def load_edt(path: Path, zephyr_base: str | None) -> Any:
    """Unpickle edt.pickle; needs Zephyr's python-devicetree package importable."""
    for base in (zephyr_base, os.environ.get("ZEPHYR_BASE")):
        if base:
            src = Path(base) / "scripts" / "dts" / "python-devicetree" / "src"
            if src.is_dir() and str(src) not in sys.path:
                sys.path.insert(0, str(src))
    try:
        import devicetree.edtlib  # noqa: F401  (needed by pickle.load)
    except ImportError as exc:
        raise EdtUnavailable(f"devicetree.edtlib not importable ({exc}); pass --zephyr-base") from exc
    try:
        with path.open("rb") as fh:
            return pickle.load(fh)
    except Exception as exc:  # a stale or foreign pickle: report and fall back
        raise EdtUnavailable(f"cannot unpickle {path.name}: {exc}") from exc


def dt_facts_from_edt(edt: Any) -> DtFacts:
    facts = DtFacts(method="edtlib")
    node = edt.chosen_node("zephyr,bt-hci")
    if node is not None:
        facts.bt_hci_node = node.path
        facts.bt_hci_compats = list(node.compats)
    facts.sdc_okay = [n.path for n in edt.compat2okay.get(SDC_COMPAT, [])]
    flash = edt.chosen_node("zephyr,flash")
    if flash is not None and flash.regs:
        facts.flash_size = flash.regs[0].size
    sram = edt.chosen_node("zephyr,sram")
    if sram is not None and sram.regs:
        facts.sram_size = sram.regs[0].size
    storage = edt.label2node.get("storage_partition")
    if storage is not None and storage.regs:
        facts.storage = (storage.regs[0].addr, storage.regs[0].size)
    return facts


def _header_defines(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?m)^#define\s+(\w+)[ \t]+(.*?)[ \t]*(?:/\*.*\*/)?[ \t]*$", text))


def _header_int(defines: dict[str, str], name: str) -> int | None:
    value = defines.get(name)
    return int(value, 0) if value is not None and re.fullmatch(r"-?\w+", value) else None


def dt_facts_from_header(text: str) -> DtFacts:
    defines = _header_defines(text)
    facts = DtFacts(method="devicetree_generated.h")
    node = defines.get("DT_CHOSEN_zephyr_bt_hci")
    if node:
        facts.bt_hci_node = node
        facts.bt_hci_compats = re.findall(r'"([^"]*)"', defines.get(f"{node}_P_compatible", ""))
    m = re.search(r"(?m)^#define\s+DT_FOREACH_OKAY_nordic_bt_hci_sdc\(fn\)\s+(.*)$", text)
    if m:
        facts.sdc_okay = re.findall(r"fn\((\w+)\)", m[1])
    elif defines.get("DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc") == "1":
        facts.sdc_okay = ["(unnamed nordic,bt-hci-sdc node)"]
    for chosen, attr in (("zephyr_flash", "flash_size"), ("zephyr_sram", "sram_size")):
        chosen_node = defines.get(f"DT_CHOSEN_{chosen}")
        if chosen_node:
            setattr(facts, attr, _header_int(defines, f"{chosen_node}_REG_IDX_0_VAL_SIZE"))
    storage = defines.get("DT_N_NODELABEL_storage_partition")
    if storage:
        addr = _header_int(defines, f"{storage}_REG_IDX_0_VAL_ADDRESS")
        size = _header_int(defines, f"{storage}_REG_IDX_0_VAL_SIZE")
        if addr is not None and size is not None:
            facts.storage = (addr, size)
    return facts


def resolve_dt_facts(art: dict[str, Path | None], zephyr_base: str | None) -> DtFacts | None:
    notes: list[str] = []
    if art["edt"] is not None:
        try:
            return dt_facts_from_edt(load_edt(art["edt"], zephyr_base))
        except EdtUnavailable as exc:
            notes.append(str(exc))
    else:
        notes.append("edt.pickle not found")
    if art["dt_header"] is not None:
        facts = dt_facts_from_header(art["dt_header"].read_text(encoding="utf-8", errors="replace"))
        facts.note = "; ".join(notes)
        return facts
    return None


# --------------------------------------------------------------------------
# Checks


def _kconfig_checks(cfg: dict[str, str] | None, role: str) -> list[Check]:
    if cfg is None:
        return [Check("kconfig", "resolved .config present", "fail", ".config not found")]

    def is_y(sym: str) -> bool:
        return cfg.get(sym) == "y"

    checks = [
        Check(
            "kconfig.ll_sw_split",
            "CONFIG_BT_LL_SW_SPLIT=y",
            "pass" if is_y("CONFIG_BT_LL_SW_SPLIT") else "fail",
            f"CONFIG_BT_LL_SW_SPLIT={cfg.get('CONFIG_BT_LL_SW_SPLIT', 'unset')}",
        )
    ]
    sdc_syms = sorted(s for s, v in cfg.items() if v == "y" and re.fullmatch(r"CONFIG_BT_LL_SOFTDEVICE\w*", s))
    checks.append(
        Check(
            "kconfig.no_softdevice",
            "no CONFIG_BT_LL_SOFTDEVICE*=y",
            "fail" if sdc_syms else "pass",
            ", ".join(sdc_syms) or "none set",
        )
    )
    # Anchored: integer CONFIG_MPSL_* symbols remain with the Zephyr controller.
    checks.append(
        Check(
            "kconfig.no_mpsl",
            "no exact CONFIG_MPSL=y",
            "fail" if is_y("CONFIG_MPSL") else "pass",
            f"CONFIG_MPSL={cfg.get('CONFIG_MPSL', 'unset')}",
        )
    )
    if is_y("CONFIG_SOC_FLASH_NRF_RADIO_SYNC_MPSL"):
        status: Status = "fail"
        detail = "CONFIG_SOC_FLASH_NRF_RADIO_SYNC_MPSL=y"
    elif is_y("CONFIG_SOC_FLASH_NRF"):
        ticker = is_y("CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER")
        status = "pass" if ticker else "fail"
        detail = "CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER=" + (
            "y" if ticker else cfg.get("CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER", "unset")
        )
    else:
        status, detail = "skip", "flash driver (CONFIG_SOC_FLASH_NRF) not enabled"
    checks.append(Check("kconfig.flash_sync", "flash radio sync via the controller ticker", status, detail))
    checks.append(
        Check(
            "kconfig.ctlr_crypto",
            "CONFIG_BT_CTLR_CRYPTO=y",
            "pass" if is_y("CONFIG_BT_CTLR_CRYPTO") else "fail",
            f"CONFIG_BT_CTLR_CRYPTO={cfg.get('CONFIG_BT_CTLR_CRYPTO', 'unset')}",
        )
    )
    if role in MESH_ROLES:
        legacy = is_y("CONFIG_BT_MESH_ADV_LEGACY")
        checks.append(
            Check(
                "kconfig.mesh_adv",
                "no legacy mesh advertiser on bridge/gateway",
                "fail" if legacy else "pass",
                "CONFIG_BT_MESH_ADV_LEGACY=y"
                if legacy
                else f"CONFIG_BT_MESH_ADV_EXT={cfg.get('CONFIG_BT_MESH_ADV_EXT', 'unset')}",
            )
        )
    else:
        checks.append(Check("kconfig.mesh_adv", "no legacy mesh advertiser on bridge/gateway", "skip", f"role {role}"))
    cc3xx = is_y("CONFIG_ENTROPY_CC3XX")
    checks.append(
        Check(
            "kconfig.entropy",
            "no CryptoCell entropy driver (firmware-notes correction 9)",
            "warn" if cc3xx else "pass",
            "CONFIG_ENTROPY_CC3XX=y" if cc3xx else "CONFIG_ENTROPY_CC3XX not set",
        )
    )
    return checks


def _dt_checks(facts: DtFacts | None, header_text: str | None) -> list[Check]:
    checks: list[Check] = []
    if facts is None:
        checks.append(
            Check("dt", "devicetree artifacts present", "fail", "neither edt.pickle nor devicetree_generated.h found")
        )
    else:
        compats_ok = facts.bt_hci_compats == [LL_SW_SPLIT_COMPAT]
        checks.append(
            Check(
                "dt.chosen_bt_hci",
                f"chosen zephyr,bt-hci compatibles == ['{LL_SW_SPLIT_COMPAT}']",
                "pass" if compats_ok else "fail",
                f"{facts.bt_hci_node}: {facts.bt_hci_compats}" if facts.bt_hci_node else "no zephyr,bt-hci chosen node",
            )
        )
        checks.append(
            Check(
                "dt.no_sdc_okay",
                f"no okay '{SDC_COMPAT}' node",
                "fail" if facts.sdc_okay else "pass",
                ", ".join(facts.sdc_okay) or "none",
            )
        )
    if header_text is None:
        checks.append(
            Check(
                "dt.generated_header", "devicetree_generated.h compat flags", "fail", "devicetree_generated.h not found"
            )
        )
    else:
        has_ll = re.search(r"(?m)^#define\s+DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split\s+1\b", header_text)
        has_sdc = re.search(r"(?m)^#define\s+DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc\s+1\b", header_text)
        ok = bool(has_ll) and not has_sdc
        checks.append(
            Check(
                "dt.generated_header",
                "DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split 1 and no DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc",
                "pass" if ok else "fail",
                f"ll_sw_split={'1' if has_ll else 'absent'}, sdc={'1' if has_sdc else 'absent'}",
            )
        )
    return checks


def _map_checks(map_text: str | None) -> list[Check]:
    if map_text is None:
        return [Check("map", "zephyr.map present", "fail", "zephyr.map not found")]
    libs = map_forbidden_libraries(map_text)
    symbols = map_symbols(map_text)
    forbidden = sorted(s for s in symbols if s.startswith(FORBIDDEN_SYMBOL_PREFIXES))
    missing = [s for s in REQUIRED_LL_SYMBOLS if s not in symbols]
    shown = ", ".join(forbidden[:8]) + (f" (+{len(forbidden) - 8} more)" if len(forbidden) > 8 else "")
    return [
        Check(
            "map.no_sdc_libs",
            "no libsoftdevice_controller*.a / libmpsl*.a linked",
            "fail" if libs else "pass",
            ", ".join(libs) or "none",
        ),
        Check("map.no_sdc_symbols", "no sdc_* / mpsl_* symbols", "fail" if forbidden else "pass", shown or "none"),
        Check(
            "map.ll_symbols",
            "Zephyr controller symbols " + ", ".join(REQUIRED_LL_SYMBOLS),
            "fail" if missing else "pass",
            ("missing: " + ", ".join(missing)) if missing else "all present",
        ),
    ]


def _geometry_checks(
    soc: SocGeometry,
    facts: DtFacts | None,
    regions: dict[str, Region] | None,
    usage: MemoryUsage | None,
) -> list[Check]:
    checks: list[Check] = []
    if facts is None or facts.flash_size is None or facts.sram_size is None:
        checks.append(
            Check(
                "geometry.dt",
                "devicetree flash/SRAM sizes match the SoC",
                "fail",
                "flash or SRAM size not found in the devicetree",
            )
        )
    else:
        ok = facts.flash_size == soc.flash and facts.sram_size == soc.ram
        checks.append(
            Check(
                "geometry.dt",
                "devicetree flash/SRAM sizes match the SoC",
                "pass" if ok else "fail",
                f"flash {facts.flash_size} (expected {soc.flash}), sram {facts.sram_size} (expected {soc.ram})",
            )
        )
    flash = regions.get("FLASH") if regions else None
    ram = regions.get("RAM") if regions else None
    if flash is None or ram is None:
        checks.append(
            Check(
                "geometry.map",
                "zephyr.map FLASH/RAM regions match the SoC",
                "fail",
                "FLASH/RAM regions not found in zephyr.map",
            )
        )
        return checks
    ok = ram.length == soc.ram and flash.end <= soc.flash
    checks.append(
        Check(
            "geometry.map",
            "zephyr.map RAM region == SoC RAM and FLASH region inside SoC flash",
            "pass" if ok else "fail",
            f"FLASH 0x{flash.origin:x}+0x{flash.length:x} (SoC flash 0x{soc.flash:x}), "
            f"RAM 0x{ram.length:x} (SoC RAM 0x{soc.ram:x})",
        )
    )
    storage = facts.storage if facts else None
    if storage is None:
        checks.append(
            Check("geometry.storage", "image stays out of the storage partition", "skip", "no storage_partition node")
        )
    else:
        start = storage[0]
        if flash.end <= start:
            status: Status = "pass"
            detail = f"FLASH region ends 0x{flash.end:x} <= storage 0x{start:x}"
        elif usage is not None and flash.origin + usage.flash_used <= start:
            status = "warn"
            detail = (
                f"FLASH region (to 0x{flash.end:x}) overlaps storage at 0x{start:x}; the image "
                f"(to 0x{flash.origin + usage.flash_used:x}) fits today but the linker would not stop growth"
            )
        else:
            status = "fail"
            detail = f"FLASH region (to 0x{flash.end:x}) overlaps storage at 0x{start:x}"
        checks.append(Check("geometry.storage", "image stays out of the storage partition", status, detail))
    return checks


def verify(
    artifacts_dir: Path,
    soc: SocGeometry,
    role: str,
    target: str | None = None,
    zephyr_base: str | None = None,
    edt: Any = None,
) -> Report:
    """Run every check. ``edt`` injects an already-loaded EDT (tests, callers)."""
    art = locate_artifacts(artifacts_dir)

    def read(key: str) -> str | None:
        path = art[key]
        return path.read_text(encoding="utf-8", errors="replace") if path is not None else None

    cfg_text = read("config")
    map_text = read("map")
    header_text = read("dt_header")
    facts = dt_facts_from_edt(edt) if edt is not None else resolve_dt_facts(art, zephyr_base)
    regions = parse_map_regions(map_text) if map_text is not None else None

    usage: MemoryUsage | None = None
    if art["elf"] is not None and regions and "FLASH" in regions and "RAM" in regions:
        usage = elf_memory_usage(art["elf"].read_bytes(), regions["FLASH"], regions["RAM"])

    checks = (
        _kconfig_checks(parse_kconfig(cfg_text) if cfg_text is not None else None, role)
        + _dt_checks(facts, header_text)
        + _map_checks(map_text)
        + _geometry_checks(soc, facts, regions, usage)
    )
    return Report(
        artifacts=str(artifacts_dir),
        target=target,
        soc=soc.name,
        role=role,
        dt_method=facts.method if facts else "none",
        dt_note=facts.note if facts else "",
        checks=checks,
        memory=usage.as_dict() if usage else None,
    )


def format_report(report: Report) -> str:
    head = report.target or report.artifacts
    lines = [f"verify_stack: {head} ({report.soc}, {report.role}); devicetree via {report.dt_method}"]
    if report.dt_note:
        lines.append(f"  note: {report.dt_note}")
    for c in report.checks:
        lines.append(f"  {c.status.upper():4} {c.id:22} {c.detail}")
    counts = report.as_dict()["counts"]
    verdict = "PASS" if report.ok else "FAIL"
    lines.append(
        f"RESULT: {verdict} ({counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['warn']} warnings, {counts['skip']} skipped)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("artifacts", type=Path, help="build.py artifact directory or Zephyr build directory")
    parser.add_argument("--target", help="target name in tools/targets.yaml (sets --soc and --role)")
    parser.add_argument("--soc", help="SoC geometry key in tools/targets.yaml 'socs'")
    parser.add_argument("--role", choices=ROLES, help="firmware role")
    parser.add_argument("--targets-file", type=Path, default=TARGETS_FILE)
    parser.add_argument("--zephyr-base", help="Zephyr tree for importing edtlib (default: $ZEPHYR_BASE)")
    parser.add_argument("--json", type=Path, help="write the machine-readable report here")
    parser.add_argument("--quiet", action="store_true", help="print only the result line")
    args = parser.parse_args(argv)

    try:
        matrix = load_targets(args.targets_file)
        soc_name, role = args.soc, args.role
        if args.target:
            if args.target not in matrix["targets"]:
                raise UsageError(f"unknown target '{args.target}'")
            entry = matrix["targets"][args.target]
            soc_name = soc_name or entry["soc"]
            role = role or entry["role"]
        if not soc_name or not role:
            raise UsageError("give --target, or both --soc and --role")
        if not args.artifacts.is_dir():
            raise UsageError(f"{args.artifacts} is not a directory")
        report = verify(args.artifacts, soc_geometry(matrix, soc_name), role, args.target, args.zephyr_base)
    except UsageError as exc:
        print(f"verify_stack: error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    text = format_report(report)
    print(text.splitlines()[-1] if args.quiet else text)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
