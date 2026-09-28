#!/usr/bin/env python3
"""Package a firmware release into dist/<version>/ (docs/releasing.md).

Inputs: the firmware artifacts tools/build.py wrote to build/<target>/ (each
with its metadata.json) and the protocol contract built from this commit
(tools/contract.py). Output::

    dist/<version>/
      release.json                 the manifest tying everything together
      SHA256SUMS                   every other file (sha256sum -c SHA256SUMS)
      THIRD_PARTY_NOTICES.txt      QR generator, firmware SDK components
      LICENSE
      firmware/<target>/<target>-<version>.{hex,bin,elf,map,config,dts}
                                   and .metadata.json .verify.json .memory.json
      firmware/memory-report.{md,json}
      contract/cremind-tag-contract-<version>.tar.gz (+ .sha256)
                                   the protocol contract host software pins
    dist/cremind-tag-<version>.tar.gz (+ .sha256)   the directory above, deterministic archive

A release holds firmware and the contract only: host software (Cremind)
ships separately, with its own version, and builds the font packs itself.

Only publishable targets are packaged: a board ``qualified`` in
hardware/matrix.yaml, or one explicitly marked ``release: true`` in
tools/targets.yaml whose board is not blocked/documented (development targets
without a hardware entry need the mark too). Each target's status is recorded.

A firmware target is refused unless its metadata.json shows a pristine build
of this commit and VERSION, a clean sdk-nrf v3.4.1 workspace, the pinned
toolchain digest, verify_stack passed and resources met, and its files still
hash as recorded.

    python tools/build.py --pristine $(python tools/release.py --list-targets)
    uv run python tools/release.py
    uv run python tools/release.py --targets tag-laowu-bw --out /tmp/dist

Exit status: 0 packaged, 1 a check or step failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import contract as ctag_contract  # noqa: E402
import version as ctag_version  # noqa: E402

REPO_ROOT = build.REPO_ROOT
MANIFEST_SCHEMA = "cremind-tag/release@2"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

# SoC key (tools/targets.yaml) -> (J-Link device, nrfjprog family). Mirrored in
# Cremind's `cremind tags tools firmware` (app/tags/runtime/cli/firmware.py), which
# checks its copy against the contract's hardware/targets.yaml.
SOC_DEVICES: dict[str, tuple[str, str]] = {
    "nrf51822_qfaa": ("nRF51822_xxAA", "NRF51"),
    "nrf51822_qfab": ("nRF51822_xxAB", "NRF51"),
    "nrf52810_qfaa": ("nRF52810_xxAA", "NRF52"),
    "nrf52811_qfaa": ("nRF52811_xxAA", "NRF52"),
    "nrf52832_qfaa": ("nRF52832_xxAA", "NRF52"),
    "nrf52840_qiaa": ("nRF52840_xxAA", "NRF52"),
}
# build.py artifact -> suffix in dist (the file is <target>-<version><suffix>).
FIRMWARE_FILES: dict[str, str] = {
    "zephyr.hex": ".hex",
    "zephyr.bin": ".bin",
    "zephyr.elf": ".elf",
    "zephyr.map": ".map",
    ".config": ".config",
    "zephyr.dts": ".dts",
    "verify.json": ".verify.json",
    "metadata.json": ".metadata.json",
}
REQUIRED_FIRMWARE = ("zephyr.hex", "zephyr.bin", "zephyr.elf", "zephyr.map", "verify.json", "metadata.json")
PUBLISHED_STATUSES = ("qualified", "functional", "buildable")
NEVER_PUBLISHED = ("blocked", "documented")


class ReleaseError(Exception):
    pass


# --------------------------------------------------------------------------
# Which targets


@dataclass(frozen=True)
class Eligibility:
    target: build.Target
    hardware_status: str | None
    release_status: str
    """qualified | functional | buildable | development"""
    board_id: int | None


def eligible_targets(
    targets: dict[str, build.Target] | None = None, hardware: dict[str, dict[str, Any]] | None = None
) -> tuple[list[tuple[str, Eligibility]], list[dict[str, str]]]:
    """(publishable targets in matrix order, excluded targets with the reason)."""
    if targets is None:
        targets, _ = build.load_matrix()
    if hardware is None:
        hardware = build.load_hardware_status()
    ok: list[tuple[str, Eligibility]] = []
    excluded: list[dict[str, str]] = []
    for name, t in targets.items():
        hw = hardware.get(t.hardware) if t.hardware else None
        status = hw["status"] if hw else None
        board_id = hw["board_id"] if hw else None
        if t.hardware and hw is None:
            excluded.append({"target": name, "reason": f"hardware id {t.hardware!r} is not in hardware/matrix.yaml"})
        elif status in NEVER_PUBLISHED:
            excluded.append({"target": name, "reason": f"hardware status {status}"})
        elif status == "qualified":
            ok.append((name, Eligibility(t, status, "qualified", board_id)))
        elif not t.release:
            what = f"hardware status {status}" if status else "no hardware entry (development target)"
            excluded.append({"target": name, "reason": f"not qualified ({what}) and not marked release: true"})
        elif status is None:
            ok.append((name, Eligibility(t, None, "development", board_id)))
        elif status in PUBLISHED_STATUSES:
            ok.append((name, Eligibility(t, status, status, board_id)))
        else:
            excluded.append({"target": name, "reason": f"unknown hardware status {status!r}"})
    return ok, excluded


# --------------------------------------------------------------------------
# Helpers


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# --------------------------------------------------------------------------
# Firmware


def check_firmware_metadata(
    meta: dict[str, Any], name: str, version: str, git: dict[str, Any], allow_dirty: bool
) -> list[str]:
    """Why this build must not be published (empty = publishable)."""
    problems: list[str] = []
    if meta.get("schema") != build.METADATA_SCHEMA:
        problems.append(f"metadata schema {meta.get('schema')!r}, expected {build.METADATA_SCHEMA}")
    if meta.get("target") != name:
        problems.append(f"metadata is for {meta.get('target')!r}")
    if meta.get("status") != "ok":
        problems.append(f"build status {meta.get('status')!r}")
    if not (meta.get("verify_stack") or {}).get("ok"):
        problems.append("verify_stack did not pass: " + ", ".join((meta.get("verify_stack") or {}).get("failed") or ["?"]))
    if meta.get("version") != version:
        problems.append(f"built as VERSION {meta.get('version')}, VERSION is {version}")
    if not meta.get("version_matches"):
        problems.append(f"{meta.get('app')}/VERSION was {meta.get('app_version')} (python tools/version.py --sync)")
    mgit = meta.get("git") or {}
    if git.get("commit") and mgit.get("commit") != git["commit"]:
        problems.append(f"built from commit {str(mgit.get('commit'))[:12]}, HEAD is {git['commit'][:12]} (rebuild)")
    if mgit.get("dirty") and not allow_dirty:
        problems.append("built from a dirty tree (" + ", ".join(mgit.get("changed_files") or [])[:200] + ")")
    ncs = meta.get("ncs") or {}
    if ncs.get("sdk_nrf_commit") != build.NCS_SDK_NRF_COMMIT:
        problems.append(f"sdk-nrf {str(ncs.get('sdk_nrf_commit'))[:12]}, pinned {build.NCS_SDK_NRF_COMMIT[:12]}")
    if not ncs.get("compared"):
        problems.append("the west workspace was not compared against the manifest (--skip-workspace-check)")
    tool = meta.get("toolchain") or {}
    if tool.get("image") != build.IMAGE:
        problems.append(f"toolchain {tool.get('image')!r}, pinned {build.IMAGE}")
    if not (meta.get("build") or {}).get("pristine"):
        problems.append("not a pristine build (python tools/build.py --pristine)")
    return problems


def package_firmware(
    name: str,
    elig: Eligibility,
    src_root: Path,
    dist: Path,
    version: str,
    git: dict[str, Any],
    allow_dirty: bool,
) -> dict[str, Any]:
    src = src_root / name
    meta_path = src / "metadata.json"
    if not meta_path.is_file():
        raise ReleaseError(
            f"{name}: {build._display(meta_path)} is missing; build it: python tools/build.py --pristine {name}"
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    problems = check_firmware_metadata(meta, name, version, git, allow_dirty)
    missing = [f for f in REQUIRED_FIRMWARE if not (src / f).is_file()]
    if missing:
        problems.append("missing " + ", ".join(missing))
    for fname, rec in (meta.get("artifacts") or {}).items():
        path = src / fname
        if path.is_file() and sha256_file(path) != rec.get("sha256"):
            problems.append(f"{fname} changed after the build (SHA-256 differs from metadata.json)")
    if problems:
        raise ReleaseError(f"{name}: " + "; ".join(problems))

    dst = dist / "firmware" / name
    dst.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for fname, suffix in FIRMWARE_FILES.items():
        if not (src / fname).is_file():
            continue
        out = dst / f"{name}-{version}{suffix}"
        shutil.copyfile(src / fname, out)
        kind = suffix.lstrip(".").replace(".json", "")
        files[kind] = _rel(out, dist)
        hashes[kind] = sha256_file(out)
    memory = {"target": name, "memory": meta.get("memory"), "resources": meta.get("resources")}
    mem_path = dst / f"{name}-{version}.memory.json"
    mem_path.write_text(json.dumps(memory, indent=2) + "\n", encoding="utf-8", newline="\n")
    files["memory"] = _rel(mem_path, dist)
    hashes["memory"] = sha256_file(mem_path)

    t = elig.target
    device, family = SOC_DEVICES[t.soc]
    hex_rel = files["hex"]
    if t.role == "tag":
        board = t.hardware or ("nrf52dk_tag" if t.board.startswith("nrf52dk/") else t.board)
        flash = f"cremind tags tools tag enroll --board {board} --firmware {hex_rel}"
    else:
        flash = f"cremind tags tools firmware flash --target {name} --hex {hex_rel}"
    return {
        "target": name,
        "app": meta["app"],
        "role": t.role,
        "board": t.board,
        "soc": t.soc,
        "jlink_device": device,
        "family": family,
        "hardware": t.hardware,
        "board_id": elig.board_id,
        "hardware_status": elig.hardware_status,
        "release_status": elig.release_status,
        "qualified": elig.release_status == "qualified",
        "version": meta["version"],
        "files": files,
        "sha256": hashes,
        "verify_stack": {k: meta["verify_stack"][k] for k in ("ok", "dt_method", "counts", "warnings")},
        "memory": meta.get("memory"),
        "resources": meta.get("resources"),
        "inputs": {k: v for k, v in (meta.get("inputs") or {}).items() if k != "note"},
        "flash": flash,
    }


def write_memory_report(dist: Path, entries: list[dict[str, Any]]) -> None:
    doc = {e["target"]: {"memory": e["memory"], "resources": e["resources"]} for e in entries}
    # Every text file of a release has LF line ends, so one packaged on Windows hashes as on Linux.
    (dist / "firmware" / "memory-report.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8",
                                                          newline="\n")
    lines = [
        "# Firmware memory report",
        "",
        "| Target | Status | Flash used / region | Headroom (min) | RAM used / region | RAM free (min) |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        m, r = e["memory"] or {}, e["resources"] or {}
        ram_min = r.get("ram_free_min")
        lines.append(
            f"| {e['target']} | {e['release_status']} | {m.get('flash_used', 0):,} / {m.get('flash_region', 0):,} "
            f"| {r.get('flash_headroom_pct', 0):.1f} % ({r.get('flash_headroom_pct_min', 0):g} %) "
            f"| {m.get('ram_used', 0):,} / {m.get('ram_region', 0):,} "
            f"| {r.get('ram_free', 0):,} ({'none' if ram_min is None else f'{ram_min:,}'}) |"
        )
    (dist / "firmware" / "memory-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# The protocol contract


def package_contract(dist: Path, allow_dirty: bool) -> dict[str, Any]:
    """The contract artifact of this commit (tools/contract.py), as host software downloads and pins it."""
    out = dist / "contract"
    out.mkdir(parents=True, exist_ok=True)
    try:
        unpacked, archive = ctag_contract.build(out, allow_dirty=allow_dirty)
        meta = ctag_contract.check(unpacked)
    except ctag_contract.ContractError as exc:
        raise ReleaseError(f"contract: {exc}") from None
    shutil.rmtree(unpacked)  # the archive is what is published (and pinned); SHA256SUMS covers it
    return {
        "name": meta["name"],
        "version": meta["version"],
        "digest": meta["digest"],
        "protocol": meta["protocol"],
        "archive": _rel(archive, dist),
        "sha256": sha256_file(archive),
    }


# --------------------------------------------------------------------------
# Notices


# Firmware SDK components: (name, licence, marker found in zephyr.map when linked).
FIRMWARE_COMPONENTS: list[tuple[str, str, str | None]] = [
    ("Zephyr RTOS (sdk-zephyr)", "Apache-2.0", "libzephyr.a"),
    ("nRF Connect SDK (sdk-nrf)", "LicenseRef-Nordic-5-Clause", "modules/nrf/"),
    ("nrfx (hal_nordic)", "BSD-3-Clause", "hal_nordic"),
    ("nrf_oberon PSA crypto library (binary, nrfxlib)", "LicenseRef-Nordic-5-Clause", "liboberon"),
    ("nrf_security / PSA core (sdk-nrf, TF-PSA-Crypto sources)", "Apache-2.0 / LicenseRef-Nordic-5-Clause",
     "nrf_security"),
    ("zcbor", "Apache-2.0", "zcbor"),
    ("CMSIS (headers)", "Apache-2.0", None),
]


def firmware_components(firmware_root: Path, names: list[str]) -> list[tuple[str, str, list[str]]]:
    found: list[tuple[str, str, list[str]]] = []
    maps = {n: (firmware_root / n / "zephyr.map").read_text(encoding="utf-8", errors="replace")
            for n in names if (firmware_root / n / "zephyr.map").is_file()}
    for comp, licence, marker in FIRMWARE_COMPONENTS:
        users = sorted(n for n, text in maps.items() if marker is None or marker in text)
        if users:
            found.append((comp, licence, users))
    return found


def write_notices(
    dist: Path,
    firmware: list[dict[str, Any]],
    components: list[tuple[str, str, list[str]]],
) -> Path:
    qr_license = (REPO_ROOT / "lib" / "third_party" / "qrcodegen" / "LICENSE").read_text(encoding="utf-8")
    parts = [
        "THIRD-PARTY NOTICES — Cremind Tag release",
        "=" * 44,
        "",
        "Cremind Tag itself is licensed under the MIT licence (LICENSE). This release also",
        "contains or links the third-party components below.",
        "",
        "1. Nayuki QR Code generator v1.8.0 — MIT",
        "   Vendored in the firmware (lib/third_party/qrcodegen, C). Source:",
        "   https://github.com/nayuki/QR-Code-generator (tag v1.8.0).",
        "",
        *("   " + line if line else "" for line in qr_license.strip().splitlines()),
        "",
    ]
    n = 2
    if components:
        parts += [
            f"{n}. Firmware SDK components (nRF Connect SDK {build.NCS_REVISION}, sdk-nrf "
            f"{build.NCS_SDK_NRF_COMMIT[:12]})",
            "   Linked into the firmware images as found in each image's zephyr.map. Licence",
            "   texts are in each project at the pinned revision of the NCS west workspace.",
            "",
        ]
        for comp, licence, users in components:
            everyone = len(users) == len(firmware)
            parts.append(f"   - {comp}: {licence} ({'all targets' if everyone else ', '.join(users)})")
        parts.append("")
    path = dist / "THIRD_PARTY_NOTICES.txt"
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
# Checksums and archive


def write_sha256sums(dist: Path) -> Path:
    path = dist / "SHA256SUMS"
    # Sorted by the POSIX path string: Path ordering is case-insensitive on Windows.
    files = sorted((p for p in dist.rglob("*") if p.is_file() and p != path), key=lambda p: _rel(p, dist))
    path.write_text("".join(f"{sha256_file(p)}  {_rel(p, dist)}\n" for p in files), encoding="utf-8", newline="\n")
    return path


def verify_sha256sums(dist: Path) -> list[str]:
    """Files whose checksum no longer matches SHA256SUMS (or are missing)."""
    bad = []
    for line in (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        path = dist / rel
        if not path.is_file() or sha256_file(path) != digest:
            bad.append(rel)
    return bad


def write_archive(dist: Path, prefix: str, epoch: int | None) -> Path:
    """dist/../<prefix>.tar.gz: sorted members, fixed owner/mode/mtime, gzip without a timestamp."""
    out = dist.parent / f"{prefix}.tar.gz"
    mtime = epoch or 0
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(dist.rglob("*"), key=lambda p: _rel(p, dist)):
            info = tar.gettarinfo(str(path), arcname=f"{prefix}/{_rel(path, dist)}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = mtime
            info.mode = 0o755 if path.is_dir() else 0o644
            if path.is_file():
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    with out.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue())
    out.with_name(out.name + ".sha256").write_text(f"{sha256_file(out)}  {out.name}\n", encoding="utf-8",
                                                   newline="\n")
    return out


def release_notes(manifest: dict[str, Any]) -> str:
    """Markdown for the GitHub release body (the draft; a maintainer edits and publishes it)."""
    v = manifest["version"]
    git = manifest["git"]
    lines = [
        f"Cremind Tag {v} — commit `{(git['commit'] or '?')[:12]}`, nRF Connect SDK {manifest['ncs']['revision']} "
        f"(sdk-nrf `{manifest['ncs']['sdk_nrf_commit'][:12]}`), toolchain `{manifest['toolchain']['digest']}`.",
        "",
        "No board is qualified yet unless its status says so: `buildable` means the firmware links within the",
        "board's exact memory geometry and passed the controller checks, not that it was measured on hardware.",
        "",
        "| Firmware | Board | Status | SHA-256 (hex) |",
        "|---|---|---|---|",
    ]
    for f in manifest["firmware"]:
        lines.append(f"| `{f['target']}` | `{f['board']}` | {f['release_status']} | `{f['sha256']['hex']}` |")
    if manifest.get("excluded_targets"):
        lines += ["", "Not in this release: " + "; ".join(
            f"`{e['target']}` ({e['reason']})" for e in manifest["excluded_targets"]) + "."]
    contract = manifest["contract"]
    capabilities = ", ".join(f"{k} {v}" for k, v in contract["protocol"].items())
    lines += ["", f"Protocol contract `{contract['archive'].split('/')[-1]}` ({capabilities}), digest "
                  f"`{contract['digest']}`: what host software pins (Cremind: scripts/tags/pin_contract.py)."]
    lines += [
        "",
        "Verify before use (docs/releasing.md, \"Verifying a download\"):",
        "",
        "```sh",
        f"sha256sum -c cremind-tag-{v}.tar.gz.sha256 && tar xzf cremind-tag-{v}.tar.gz",
        f"cd cremind-tag-{v} && sha256sum -c SHA256SUMS",
        "cremind tags tools firmware verify --hex firmware/<target>/<target>-" + v + ".hex",
        "```",
        "",
        "Flash gateways and bridges with `cremind tags tools firmware flash`, tags with "
        "`cremind tags tools tag enroll --firmware` (Cremind's hardware tools).",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--targets", help="comma-separated targets (default: every publishable target)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist", help="output root (dist/<version>/ inside)")
    parser.add_argument("--firmware-dir", type=Path, default=REPO_ROOT / "build",
                        help="where tools/build.py wrote the artifacts (default: build/)")
    parser.add_argument("--build", action="store_true", help="run tools/build.py --pristine for the targets first")
    parser.add_argument("--allow-dirty", action="store_true", help="package a dirty tree (never for a real release)")
    parser.add_argument("--no-archive", action="store_true", help="do not write dist/cremind-tag-<version>.tar.gz")
    parser.add_argument("--list-targets", action="store_true", help="print the publishable targets and exit")
    args = parser.parse_args(argv)

    eligible, excluded = eligible_targets()
    by_name = dict(eligible)
    if args.list_targets:
        print(" ".join(by_name))
        return EXIT_OK
    targets_all, _ = build.load_matrix()
    if args.targets:
        names = [n.strip() for n in args.targets.split(",") if n.strip()]
        unknown = [n for n in names if n not in targets_all]
        refused = [e for e in excluded if e["target"] in names]
        if unknown:
            parser.error(f"unknown target(s): {', '.join(unknown)}")
        if refused:
            parser.error("not publishable: " + "; ".join(f"{e['target']}: {e['reason']}" for e in refused))
    else:
        names = list(by_name)

    try:
        version = ctag_version.read_version()
    except ValueError as exc:
        print(f"release: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    problems = ctag_version.check(version) + build.pin_problems()
    if problems:
        for p in problems:
            print(f"release: error: {p}", file=sys.stderr)
        print("release: fix with: python tools/version.py --sync", file=sys.stderr)
        return EXIT_FAILED
    git = build.git_info(REPO_ROOT)
    if git["dirty"] and not args.allow_dirty:
        print("release: error: the working tree is dirty (" + ", ".join(git["changed_files"][:8]) +
              "); commit or stash, or pass --allow-dirty for a trial run", file=sys.stderr)
        return EXIT_FAILED
    epoch = git["commit_time"]

    if args.build:
        rc = build.main(["--pristine", *names])
        if rc != build.EXIT_OK:
            print(f"release: error: tools/build.py exited {rc}", file=sys.stderr)
            return EXIT_FAILED

    dist = args.out / str(version)
    shutil.rmtree(dist, ignore_errors=True)
    dist.mkdir(parents=True)
    print(f"release {version} (commit {(git['commit'] or '?')[:12]}{', DIRTY' if git['dirty'] else ''}) -> {dist}")
    try:
        firmware = [
            package_firmware(n, by_name[n], args.firmware_dir, dist, str(version), git, args.allow_dirty)
            for n in names
        ]
        for entry in firmware:
            print(f"  firmware {entry['target']:20} {entry['release_status']:12} {entry['sha256']['hex'][:16]}")
        if firmware:
            write_memory_report(dist, firmware)
        contract = package_contract(dist, args.allow_dirty)
        print(f"  contract {contract['archive']} (digest {contract['digest'][:16]})")
        shutil.copyfile(REPO_ROOT / "LICENSE", dist / "LICENSE")
        components = firmware_components(args.firmware_dir, names)
        write_notices(dist, firmware, components)
    except (ReleaseError, OSError, KeyError) as exc:
        print(f"release: error: {exc}", file=sys.stderr)
        shutil.rmtree(dist, ignore_errors=True)  # never leave a partial release behind
        return EXIT_FAILED

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": "cremind-tag",
        "version": str(version),
        "tag": version.tag,
        "git": {k: git[k] for k in ("commit", "describe", "dirty", "commit_time")},
        "source_date_epoch": epoch,
        "ncs": {
            "revision": build.NCS_REVISION,
            "manifest_url": build.NCS_MANIFEST_URL,
            "sdk_nrf_commit": build.NCS_SDK_NRF_COMMIT,
        },
        "toolchain": {"image": build.IMAGE, "digest": build.IMAGE_DIGEST},
        "firmware": firmware,
        "excluded_targets": excluded
        + [{"target": n, "reason": "not selected (--targets)"} for n in by_name if n not in names],
        "contract": contract,
        "notices": "THIRD_PARTY_NOTICES.txt",
        "licence": "LICENSE",
        "checksums": "SHA256SUMS",
    }
    (dist / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    sums = write_sha256sums(dist)
    print(f"  {sum(1 for _ in sums.read_text(encoding='utf-8').splitlines())} files in SHA256SUMS")
    if not args.no_archive:
        archive = write_archive(dist, f"cremind-tag-{version}", epoch)
        print(f"  archive  {archive}")
    notes = args.out / f"RELEASE_NOTES-{version}.md"
    notes.write_text(release_notes(manifest), encoding="utf-8", newline="\n")
    print(f"release.json: {dist / 'release.json'}")
    print(f"notes:        {notes}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
