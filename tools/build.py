#!/usr/bin/env python3
"""Build the firmware target matrix with the pinned NCS toolchain.

On a host (Windows Git Bash or Linux) this script starts the NCS v3.4.1
toolchain container (pinned by digest, ``IMAGE``) with three mounts: the NCS
workspace volume at /ncs, the build volume at /build (building on the
repository bind mount is slow) and this repository at /work. Inside the
container (``--in-container``, also used by CI directly) it first checks that
the west workspace is sdk-nrf v3.4.1 exactly (``check_workspace``), then runs
``west build --no-sysbuild`` per target in ``<build-root>/<target>`` with the
reproducibility flags of ``repro_cmake_args``, copies the artifacts to
``build/<target>/`` in the repository, runs tools/verify_stack.py on them,
compares flash/RAM with the target's resource limits, writes
``build/<target>/metadata.json`` (git commit, VERSION, NCS and toolchain pins,
Kconfig/devicetree digests, verification, memory, artifact SHA-256s) and
build/memory-report.md and .json.

Examples::

    python tools/build.py --list
    python tools/build.py tag-laowu-bw bridge-nrf52840dk
    python tools/build.py --all --pristine
    python tools/build.py --app build/boardcheck tag-sifei-52810   # other app, same board setup
    python tools/build.py --shell
    python tools/build.py --setup                                   # create the NCS workspace volume
    python tools/build.py --out-root build/other tag-laowu-bw       # artifacts under build/other/

Exit status: 0 = every requested target built, verified and met its resource
limits; 1 = a build, verification or resource limit failed; 2 = usage error;
3 = nothing failed but a target was skipped because its app does not exist yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_stack as vs
import version as ctag_version

REPO_ROOT = vs.REPO_ROOT
OUT_ROOT = REPO_ROOT / "build"
REPORT_JSON = OUT_ROOT / "memory-report.json"
REPORT_MD = OUT_ROOT / "memory-report.md"
HARDWARE_FILE = REPO_ROOT / "hardware" / "matrix.yaml"
WEST_YML = REPO_ROOT / "west.yml"

NCS_REVISION = "v3.4.1"
NCS_MANIFEST_URL = "https://github.com/nrfconnect/sdk-nrf"
# The commit the annotated tag sdk-nrf v3.4.1 (tag object f34326924aaa) points
# at (`git ls-remote https://github.com/nrfconnect/sdk-nrf refs/tags/v3.4.1^{}`).
# The workspace's manifest repository must be checked out exactly here.
NCS_SDK_NRF_COMMIT = "b20f8619ba9a5530f8c34b0a130d829947cfe55d"
IMAGE_NAME = "ghcr.io/nrfconnect/sdk-nrf-toolchain"
IMAGE_TAG = NCS_REVISION
# Registry digest of IMAGE_NAME:v3.4.1 (`docker buildx imagetools inspect`,
# 2026-09-28; a single linux/amd64 manifest). The tag is never used to run a
# build: .github/workflows/ci.yml and release.yml name the same digest
# (tests/tools/test_build.py keeps them in step).
IMAGE_DIGEST = "sha256:45b97cad97a9967c52d77d1d1a0f7dd8fe027edd17c05c3eda2eeadc23729418"
IMAGE = f"{IMAGE_NAME}@{IMAGE_DIGEST}"
NCS_VOLUME = f"ncs-{NCS_REVISION}"
BUILD_VOLUME = "ctag-build"
CONTAINER_NCS = "/ncs"
CONTAINER_BUILD = "/build"
CONTAINER_REPO = "/work"
LL_SNIPPET = "bt-ll-sw-split"
METADATA_SCHEMA = "cremind-tag/build-metadata@1"
# Canonical prefixes the reproducibility flags map the build's real paths to.
CANONICAL_NCS = "/ncs"
CANONICAL_REPO = "/cremind-tag"
CANONICAL_BUILD = "/build"

# Published artifact name -> path inside the Zephyr build directory.
ARTIFACTS: dict[str, str] = {
    "zephyr.hex": "zephyr/zephyr.hex",
    "zephyr.bin": "zephyr/zephyr.bin",
    "zephyr.elf": "zephyr/zephyr.elf",
    "zephyr.map": "zephyr/zephyr.map",
    ".config": "zephyr/.config",
    "zephyr.dts": "zephyr/zephyr.dts",
    "devicetree_generated.h": "zephyr/include/generated/zephyr/devicetree_generated.h",
    "edt.pickle": "zephyr/edt.pickle",
}

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SKIPPED = 0, 1, 2, 3
NOTABLE = re.compile(
    r"(?i:\berror:|fatal error|overflowed|undefined reference)|^FAILED:|^CMake Error|^warning:|^\s*(FLASH|RAM):"
)


@dataclass(frozen=True)
class Target:
    name: str
    app: str
    board: str
    soc: str
    role: str
    hardware: str | None
    snippets: tuple[str, ...]
    extra_conf: tuple[str, ...]
    extra_overlay: tuple[str, ...]
    flash_headroom_pct: float
    ram_free_min: int | None
    release: bool = False
    """Explicitly marked for publishing although not qualified (tools/release.py)."""


def load_matrix(path: Path = vs.TARGETS_FILE) -> tuple[dict[str, Target], dict[str, vs.SocGeometry]]:
    data = vs.load_targets(path)
    socs = {name: vs.soc_geometry(data, name) for name in data["socs"]}
    targets: dict[str, Target] = {}
    for name, t in data["targets"].items():
        res = t.get("resources") or {}
        targets[name] = Target(
            name=name,
            app=t["app"],
            board=t["board"],
            soc=t["soc"],
            role=t["role"],
            hardware=t.get("hardware"),
            snippets=tuple(t.get("snippets") or ()),
            extra_conf=tuple(t.get("extra_conf") or ()),
            extra_overlay=tuple(t.get("extra_overlay") or ()),
            flash_headroom_pct=float(res.get("flash_headroom_pct", 15)),
            ram_free_min=res.get("ram_free_min"),
            release=bool(t.get("release", False)),
        )
    return targets, socs


def load_hardware_status(path: Path = HARDWARE_FILE) -> dict[str, dict[str, Any]]:
    """hardware/matrix.yaml entries by id: {"status", "board_id", "group"}."""
    import yaml

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, dict[str, Any]] = {}
    for group in ("gateways", "bridges", "tags"):
        for entry in data.get(group) or ():
            out[entry["id"]] = {"status": entry.get("status"), "board_id": entry.get("board_id"), "group": group}
    return out


def west_yml_revision(path: Path = WEST_YML) -> str | None:
    """The sdk-nrf revision this repository's west.yml pins (None if absent)."""
    import yaml

    with path.open(encoding="utf-8") as fh:
        manifest = (yaml.safe_load(fh) or {}).get("manifest") or {}
    for project in manifest.get("projects") or ():
        if project.get("name") == "sdk-nrf":
            return str(project.get("revision"))
    return None


def pin_problems() -> list[str]:
    """Static consistency of the pins (west.yml, image digest, commit format)."""
    problems: list[str] = []
    revision = west_yml_revision()
    if revision != NCS_REVISION:
        problems.append(f"west.yml pins sdk-nrf {revision!r}, tools/build.py NCS_REVISION is {NCS_REVISION!r}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", IMAGE_DIGEST):
        problems.append(f"IMAGE_DIGEST {IMAGE_DIGEST!r} is not a sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", NCS_SDK_NRF_COMMIT):
        problems.append(f"NCS_SDK_NRF_COMMIT {NCS_SDK_NRF_COMMIT!r} is not a full commit id")
    return problems


def validate_target(t: Target, socs: dict[str, vs.SocGeometry]) -> list[str]:
    """Static rules every target must satisfy (independent of the app)."""
    errors: list[str] = []
    soc = socs.get(t.soc)
    if soc is None:
        return [f"{t.name}: unknown soc '{t.soc}'"]
    if t.role not in vs.ROLES:
        errors.append(f"{t.name}: unknown role '{t.role}'")
    # firmware-notes correction 1: the snippet's overlay needs &bt_hci_sdc, absent on nRF51.
    if soc.series == "nrf51" and LL_SNIPPET in t.snippets:
        errors.append(f"{t.name}: {LL_SNIPPET} must never be applied on nRF51")
    if soc.series == "nrf52" and LL_SNIPPET not in t.snippets:
        errors.append(f"{t.name}: nRF52 targets must apply the {LL_SNIPPET} snippet")
    return errors


def west_command(
    t: Target,
    app_dir: Path,
    build_dir: Path,
    repo_dir: Path,
    pristine: bool,
    extra_cmake_args: tuple[str, ...] | list[str] = (),
) -> list[str]:
    cmd = ["west", "build", "--no-sysbuild", "-p", "always" if pristine else "auto"]
    cmd += ["-d", build_dir.as_posix(), "-b", t.board]
    for snippet in t.snippets:
        cmd += ["-S", snippet]
    cmd += [app_dir.as_posix(), "--", f"-DZEPHYR_EXTRA_MODULES={repo_dir.as_posix()}"]
    # Without sysbuild, Zephyr's kconfig.cmake treats every cached CONFIG_* entry as a
    # command-line Kconfig assignment. nrf_security caches CONFIG_MBEDTLS_CONFIG_FILE and
    # CONFIG_TF_PSA_CRYPTO_*CONFIG_FILE (nrf/subsys/nrf_security/configs/config_extra.cmake.in),
    # so a re-configure feeds them back unquoted and Kconfig aborts. This script never
    # passes -DCONFIG_*, so every cached CONFIG_* entry is stale and is dropped.
    cmd.append("-UCONFIG_*")
    if t.extra_conf:
        cmd.append("-DEXTRA_CONF_FILE=" + ";".join((app_dir / f).as_posix() for f in t.extra_conf))
    if t.extra_overlay:
        cmd.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join((app_dir / f).as_posix() for f in t.extra_overlay))
    cmd += list(extra_cmake_args)
    return cmd


def repro_cmake_args(
    repo_dir: Path, ncs_dir: Path, build_dir: Path, target: str, zephyr_commit: str | None
) -> list[str]:
    """CMake arguments that make an image independent of where it was built.

    - ``-ffile-prefix-map`` (compile and LTO link) rewrites the workspace,
      repository and build directory to fixed prefixes in ``__FILE__`` and the
      DWARF of zephyr.elf. Zephyr's own BUILD_OUTPUT_STRIP_PATHS maps only the
      app, ZEPHYR_BASE and the west top directory, not this repository's
      lib/ (a Zephyr module) nor the build directory. The build directory
      comes last: with overlapping prefixes GCC applies the last match.
    - ``BUILD_VERSION`` is the sdk-zephyr commit (12 digits) instead of
      ``git describe`` in the workspace, whose result depends on whether the
      clone has tags (a narrow ``--depth=1`` workspace has none).

    SOURCE_DATE_EPOCH (the commit time) is set in the build environment by
    build_one. tools/repro_check.py proves the result: two pristine builds from
    different checkout paths and build directories must give byte-identical
    zephyr.hex, zephyr.bin and zephyr.elf.
    """
    maps = [
        f"-ffile-prefix-map={ncs_dir.as_posix()}={CANONICAL_NCS}",
        f"-ffile-prefix-map={repo_dir.as_posix()}={CANONICAL_REPO}",
        f"-ffile-prefix-map={build_dir.as_posix()}={CANONICAL_BUILD}/{target}",
    ]
    flags = shlex.join(maps)
    args = [f"-DEXTRA_CPPFLAGS={flags}", f"-DEXTRA_LDFLAGS={flags}"]
    if zephyr_commit:
        args.append(f"-DBUILD_VERSION={zephyr_commit[:12]}")
    return args


def evaluate_resources(t: Target, memory: dict[str, Any]) -> dict[str, Any]:
    headroom = 100.0 * memory["flash_free"] / memory["flash_region"] if memory["flash_region"] else 0.0
    flash_ok = headroom >= t.flash_headroom_pct
    ram_ok = t.ram_free_min is None or memory["ram_free"] >= t.ram_free_min
    return {
        "flash_headroom_pct": round(headroom, 2),
        "flash_headroom_pct_min": t.flash_headroom_pct,
        "flash_ok": flash_ok,
        "ram_free": memory["ram_free"],
        "ram_free_min": t.ram_free_min,
        "ram_ok": ram_ok,
        "ok": flash_ok and ram_ok,
    }


# --------------------------------------------------------------------------
# Provenance: git state, west workspace, toolchain, input digests


class WorkspaceError(Exception):
    """The west workspace is not exactly the pinned sdk-nrf revision."""


def _git(repo: Path, *args: str) -> str | None:
    """``git -C repo args`` (any owner: the container's uid differs), stripped stdout or None."""
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_info(repo: Path = REPO_ROOT) -> dict[str, Any]:
    """Commit, describe, dirty flag (tracked changes or untracked files) and commit time."""
    commit = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    changed = [line[3:] for line in (status or "").splitlines() if line.strip()]
    commit_time = _git(repo, "log", "-1", "--format=%ct")
    return {
        "commit": commit,
        "describe": _git(repo, "describe", "--tags", "--always", "--dirty", "--abbrev=12"),
        "dirty": bool(changed) if status is not None else None,
        "changed_files": changed[:20],
        "changed_count": len(changed),
        "commit_time": int(commit_time) if commit_time and commit_time.isdigit() else None,
    }


def git_safe_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` plus safe.directory=* as command-line git config (GIT_CONFIG_COUNT).

    Zephyr's and NCS's own ``git describe``/``rev-parse`` calls then work on a
    checkout owned by another uid, so the version headers never depend on it.
    """
    env = dict(env)
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    env[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
    env[f"GIT_CONFIG_VALUE_{n}"] = "*"
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def check_workspace(ncs_dir: Path, compare: bool = True, runner: Any = subprocess.run) -> dict[str, Any]:
    """Prove the workspace is sdk-nrf NCS_REVISION exactly, as west.yml pins it.

    The manifest repository (``nrf``) must be at NCS_SDK_NRF_COMMIT, and with
    ``compare`` ``west compare`` must report no project off its manifest
    revision and no local changes. Raises WorkspaceError with the fix.
    """
    fix = (
        f"recreate it: docker volume rm {NCS_VOLUME} && python tools/build.py --setup "
        "(in CI: change the NCS cache key)"
    )
    head = _git(ncs_dir / "nrf", "rev-parse", "HEAD")
    if head is None:
        raise WorkspaceError(f"{ncs_dir.as_posix()}/nrf is not a git checkout of sdk-nrf; {fix}")
    if head != NCS_SDK_NRF_COMMIT:
        raise WorkspaceError(
            f"the NCS workspace at {ncs_dir.as_posix()} has sdk-nrf at {head[:12]}, but west.yml pins sdk-nrf "
            f"{NCS_REVISION} = {NCS_SDK_NRF_COMMIT[:12]}; {fix}"
        )
    info: dict[str, Any] = {
        "revision": NCS_REVISION,
        "manifest_url": NCS_MANIFEST_URL,
        "sdk_nrf_commit": head,
        "zephyr_commit": _git(ncs_dir / "zephyr", "rev-parse", "HEAD"),
        "workspace": ncs_dir.as_posix(),
        "compared": False,
        "clean": None,
    }
    if compare:
        try:
            proc = runner(
                ["west", "compare", "--exit-code", "--ignore-branches"],
                cwd=ncs_dir,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                env=git_safe_env(dict(os.environ)),
            )
        except OSError as exc:
            raise WorkspaceError(f"cannot run `west compare` in {ncs_dir.as_posix()}: {exc}") from None
        if proc.returncode != 0:
            detail = "\n".join(
                "    " + line for line in ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[:40]
            )
            raise WorkspaceError(
                f"the NCS workspace at {ncs_dir.as_posix()} differs from the sdk-nrf {NCS_REVISION} manifest "
                f"(`west compare`: a project is off its manifest revision or has local changes):\n{detail}\n"
                f"  fix: revert the local changes and run `west update` in {ncs_dir.as_posix()}, or {fix}"
            )
        info["compared"] = True
        info["clean"] = True
    return info


def toolchain_info(build_dir: Path | None = None) -> dict[str, Any]:
    """The toolchain image (named by the caller in CTAG_TOOLCHAIN_IMAGE) and the compiler that ran."""
    image = os.environ.get("CTAG_TOOLCHAIN_IMAGE")
    used = image or IMAGE
    info: dict[str, Any] = {
        "image": used,
        "digest": used.split("@", 1)[1] if "@" in used else None,
        "pinned": IMAGE,
        "source": "CTAG_TOOLCHAIN_IMAGE" if image else "assumed: CTAG_TOOLCHAIN_IMAGE not set",
        "matches_pin": (image == IMAGE) if image else None,
        "compiler": None,
    }
    cache = build_dir / "CMakeCache.txt" if build_dir else None
    if cache is not None and cache.is_file():
        m = re.search(r"(?m)^CMAKE_C_COMPILER:\w+=(.+)$", cache.read_text(encoding="utf-8", errors="replace"))
        if m:
            try:
                out = subprocess.run([m[1].strip(), "--version"], capture_output=True, text=True, check=False)
                info["compiler"] = (out.stdout.splitlines() or [""])[0].strip() or None
            except OSError:
                pass
    return info


def _sha256_text(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def kconfig_digest(text: str) -> str:
    """SHA-256 of the effective configuration: CONFIG_ lines and '# CONFIG_X is not set'.

    Menu comments name the checkout path (``# cremind-tag (/work)``) and are left out.
    """
    return _sha256_text(
        [
            line
            for line in text.splitlines()
            if line.startswith("CONFIG_") or re.fullmatch(r"# CONFIG_\w+ is not set", line)
        ]
    )


def dts_digest(text: str) -> str:
    """SHA-256 of zephyr.dts without comments (each property's ``/* in <file>:<line> */``)."""
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return _sha256_text([line.rstrip() for line in stripped.splitlines() if line.strip()])


def dt_header_digest(text: str) -> str:
    """SHA-256 of the #define lines of devicetree_generated.h (comments name paths)."""
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return _sha256_text([line.rstrip() for line in stripped.splitlines() if line.startswith("#define")])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BuildContext:
    """What is common to every target of one build.py run (metadata.json)."""

    version: str | None
    version_error: str | None
    git: dict[str, Any]
    ncs: dict[str, Any]
    source_date_epoch: int | None
    hardware: dict[str, dict[str, Any]]
    pristine: bool


def make_context(ncs_info: dict[str, Any], pristine: bool) -> BuildContext:
    try:
        version: str | None = str(ctag_version.read_version())
        version_error = None
    except ValueError as exc:
        version, version_error = None, str(exc)
    git = git_info(REPO_ROOT)
    env_epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
    epoch = int(env_epoch) if env_epoch.isdigit() else git["commit_time"]
    try:
        hardware = load_hardware_status()
    except (OSError, KeyError, TypeError):
        hardware = {}
    return BuildContext(version, version_error, git, ncs_info, epoch, hardware, pristine)


def write_metadata(
    out_dir: Path,
    t: Target,
    app_rel: str,
    entry: dict[str, Any],
    ctx: BuildContext,
    cmd: list[str],
    build_dir: Path,
    report: vs.Report,
) -> dict[str, Any]:
    """build/<target>/metadata.json: everything needed to trust, trace and reproduce the image."""

    def text(name: str) -> str | None:
        path = out_dir / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    app_version = ctag_version.app_version(REPO_ROOT / app_rel)
    hw = ctx.hardware.get(t.hardware) if t.hardware else None
    config, dts, header = text(".config"), text("zephyr.dts"), text("devicetree_generated.h")
    as_dict = report.as_dict()
    doc = {
        "schema": METADATA_SCHEMA,
        "target": t.name,
        "app": app_rel,
        "board": t.board,
        "soc": t.soc,
        "role": t.role,
        "hardware": t.hardware,
        "hardware_status": hw["status"] if hw else None,
        "board_id": hw["board_id"] if hw else None,
        "release": t.release,
        "version": ctx.version,
        "version_error": ctx.version_error,
        "app_version": str(app_version) if app_version else None,
        "version_matches": app_version is not None and str(app_version) == ctx.version,
        "git": ctx.git,
        "ncs": ctx.ncs,
        "toolchain": toolchain_info(build_dir),
        "build": {
            "built_at": entry["built_at"],
            "pristine": ctx.pristine,
            "snippets": list(t.snippets),
            "command": cmd,
            "source_date_epoch": ctx.source_date_epoch,
        },
        "inputs": {
            "kconfig_sha256": kconfig_digest(config) if config is not None else None,
            "dts_sha256": dts_digest(dts) if dts is not None else None,
            "devicetree_header_sha256": dt_header_digest(header) if header is not None else None,
            "note": "digests of the effective content: .config CONFIG_ lines, zephyr.dts and "
            "devicetree_generated.h without comments (comments name checkout paths)",
        },
        "verify_stack": {
            "ok": report.ok,
            "dt_method": report.dt_method,
            "counts": as_dict["counts"],
            "failed": [c.id for c in report.checks if c.status == "fail"],
            "warnings": [c.id for c in report.checks if c.status == "warn"],
        },
        "memory": entry.get("memory"),
        "resources": entry.get("resources"),
        "status": entry.get("status"),
        "artifacts": {
            name: {"sha256": file_sha256(out_dir / name), "size": (out_dir / name).stat().st_size}
            for name in (*ARTIFACTS, "verify.json")
            if (out_dir / name).is_file()
        },
    }
    (out_dir / "metadata.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


# --------------------------------------------------------------------------
# In-container build


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set_out_root(path: Path) -> None:
    """Write artifacts and reports under ``path`` instead of build/ (tools/repro_check.py)."""
    global OUT_ROOT, REPORT_JSON, REPORT_MD
    OUT_ROOT = path
    REPORT_JSON = path / "memory-report.json"
    REPORT_MD = path / "memory-report.md"


def _display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _run_logged(
    cmd: list[str], cwd: Path, log_path: Path, verbose: bool, env: dict[str, str] | None = None
) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + shlex.join(cmd) + "\n")
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if verbose or NOTABLE.search(line):
                print("    " + line.rstrip(), flush=True)
        return proc.wait()


def build_one(
    t: Target,
    soc: vs.SocGeometry,
    app_rel: str,
    ncs_dir: Path,
    build_root: Path,
    pristine: bool,
    verbose: bool,
    ctx: BuildContext | None = None,
) -> dict[str, Any] | None:
    """Build, collect, verify and measure one target. None = skipped (no app).

    With ``ctx`` the build gets the reproducibility flags and SOURCE_DATE_EPOCH
    and writes metadata.json next to the artifacts.
    """
    app_dir = REPO_ROOT / app_rel
    if not (app_dir / "CMakeLists.txt").is_file():
        print(f"SKIP {t.name}: application '{app_rel}' does not exist yet", flush=True)
        return None
    entry: dict[str, Any] = {
        "built_at": _now(),
        "app": app_rel,
        "board": t.board,
        "soc": t.soc,
        "role": t.role,
        "hardware": t.hardware,
        "snippets": list(t.snippets),
    }
    missing = [f for f in (*t.extra_conf, *t.extra_overlay) if not (app_dir / f).is_file()]
    if missing:
        print(f"FAIL {t.name}: missing extra files in {app_rel}: {', '.join(missing)}", flush=True)
        return entry | {"status": "build-failed", "error": "missing extra files: " + ", ".join(missing)}

    out_dir = OUT_ROOT / t.name
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    build_dir = build_root / t.name
    extra: list[str] = []
    env: dict[str, str] | None = None
    if ctx is not None:
        extra = repro_cmake_args(REPO_ROOT, ncs_dir, build_dir, t.name, ctx.ncs.get("zephyr_commit"))
        env = git_safe_env(dict(os.environ))
        if ctx.source_date_epoch is not None:
            env["SOURCE_DATE_EPOCH"] = str(ctx.source_date_epoch)
    cmd = west_command(t, app_dir, build_dir, REPO_ROOT, pristine, extra)
    print(f"BUILD {t.name}: {t.board} {' '.join('-S ' + s for s in t.snippets)} <- {app_rel}", flush=True)
    rc = _run_logged(cmd, ncs_dir, out_dir / "build.log", verbose, env)
    if rc != 0:
        print(f"FAIL {t.name}: west build exited {rc} (log: {_display(out_dir / 'build.log')})", flush=True)
        return entry | {"status": "build-failed", "error": f"west build exited {rc}"}

    for name, rel in ARTIFACTS.items():
        src = build_dir / rel
        if src.is_file():
            shutil.copy2(src, out_dir / name)

    report = vs.verify(out_dir, soc, t.role, t.name, zephyr_base=str(ncs_dir / "zephyr"))
    (out_dir / "verify.json").write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    print("\n".join("    " + line for line in vs.format_report(report).splitlines()), flush=True)
    entry["verify"] = {
        "ok": report.ok,
        "dt_method": report.dt_method,
        "failed": [c.id for c in report.checks if c.status == "fail"],
        "warnings": [c.id for c in report.checks if c.status == "warn"],
    }
    entry["memory"] = report.memory
    if report.memory is not None:
        entry["resources"] = evaluate_resources(t, report.memory)

    if not report.ok:
        entry["status"] = "verify-failed"
    elif "resources" not in entry:
        entry["status"] = "verify-failed"
        entry["error"] = "memory usage unavailable (zephyr.elf or map regions missing)"
    elif not entry["resources"]["ok"]:
        entry["status"] = "resource-miss"
    else:
        entry["status"] = "ok"
    if ctx is not None:
        meta = write_metadata(out_dir, t, app_rel, entry, ctx, cmd, build_dir, report)
        if not meta["version_matches"]:
            print(
                f"WARN {t.name}: {app_rel}/VERSION is {meta['app_version'] or 'missing'}, VERSION is "
                f"{ctx.version} (run: python tools/version.py --sync); tools/release.py will refuse it",
                flush=True,
            )
    print(f"{'OK  ' if entry['status'] == 'ok' else 'FAIL'} {t.name}: {entry['status']}", flush=True)
    return entry


def _fmt_bytes(n: int | None) -> str:
    return "-" if n is None else f"{n:,}"


def write_reports(results: dict[str, dict[str, Any]]) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    previous: dict[str, Any] = {}
    if REPORT_JSON.is_file():
        try:
            previous = json.loads(REPORT_JSON.read_text(encoding="utf-8")).get("targets", {})
        except (OSError, ValueError):
            previous = {}
    merged = previous | results
    doc = {"generated": _now(), "ncs": NCS_REVISION, "targets": dict(sorted(merged.items()))}
    REPORT_JSON.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Firmware memory report",
        "",
        f"Generated {doc['generated']} by `tools/build.py` (NCS {NCS_REVISION}, `--no-sysbuild`).",
        "Flash is measured against the linker FLASH region (the code partition);",
        "RAM free is what remains after every static allocation including stacks.",
        "",
        "| Target | App | Board | Flash used / region | Headroom (min) | RAM used / region | RAM free (min) | Stack check | Status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, e in doc["targets"].items():
        mem = e.get("memory") or {}
        res = e.get("resources") or {}
        verify = e.get("verify") or {}
        headroom = f"{res['flash_headroom_pct']:.1f} % ({res['flash_headroom_pct_min']:g} %)" if res else "-"
        ram_min = res.get("ram_free_min")
        ram_free = (
            f"{_fmt_bytes(res.get('ram_free'))} ({_fmt_bytes(ram_min) if ram_min is not None else 'none'})"
            if res
            else "-"
        )
        stack = "-" if not verify else ("pass" if verify["ok"] else "FAIL: " + ", ".join(verify["failed"]))
        lines.append(
            f"| {name} | `{e.get('app', '-')}` | `{e.get('board', '-')}` "
            f"| {_fmt_bytes(mem.get('flash_used'))} / {_fmt_bytes(mem.get('flash_region'))} | {headroom} "
            f"| {_fmt_bytes(mem.get('ram_used'))} / {_fmt_bytes(mem.get('ram_region'))} | {ram_free} "
            f"| {stack} | {e.get('status', '-')} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_in_container(args: argparse.Namespace, names: list[str]) -> int:
    targets, socs = load_matrix()
    ncs_dir = Path(args.ncs_dir)
    if not (ncs_dir / ".west").is_dir():
        print(f"error: no west workspace at {ncs_dir} (run tools/build.py --setup on the host)", file=sys.stderr)
        return EXIT_USAGE
    build_root = Path(args.build_root)
    build_root.mkdir(parents=True, exist_ok=True)
    if getattr(args, "out_root", None):
        _set_out_root(REPO_ROOT / args.out_root)
    problems = pin_problems()
    if problems:
        print("error: " + "; ".join(problems), file=sys.stderr)
        return EXIT_USAGE
    try:
        ncs_info = check_workspace(ncs_dir, compare=not getattr(args, "skip_workspace_check", False))
    except WorkspaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    ctx = make_context(ncs_info, args.pristine)
    git = ctx.git
    print(
        f"NCS {NCS_REVISION}: sdk-nrf {ncs_info['sdk_nrf_commit'][:12]}, sdk-zephyr "
        f"{(ncs_info['zephyr_commit'] or '?')[:12]}, "
        + ("west compare clean" if ncs_info["compared"] else "west compare SKIPPED")
        + f"; toolchain {toolchain_info()['image']}; VERSION {ctx.version}; "
        f"commit {(git['commit'] or 'unknown')[:12]}{' (dirty)' if git['dirty'] else ''}; "
        f"SOURCE_DATE_EPOCH {ctx.source_date_epoch}",
        flush=True,
    )

    results: dict[str, dict[str, Any]] = {}
    failed = skipped = False
    for name in names:
        t = targets[name]
        errors = validate_target(t, socs)
        if errors:
            print("FAIL " + "; ".join(errors), flush=True)
            failed = True
            continue
        entry = build_one(
            t, socs[t.soc], args.app or t.app, ncs_dir, build_root, args.pristine, args.verbose, ctx
        )
        if entry is None:
            skipped = True
            continue
        results[name] = entry
        if entry["status"] == "resource-miss" and args.allow_resource_miss:
            continue
        failed |= entry["status"] != "ok"

    if results:
        write_reports(results)
        print(f"Report: {_display(REPORT_MD)}", flush=True)
    if args.chown:
        uid, gid = (int(x) for x in args.chown.split(":"))
        for path in [OUT_ROOT, *OUT_ROOT.rglob("*")]:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass
    if failed:
        return EXIT_FAILED
    return EXIT_SKIPPED if skipped else EXIT_OK


# --------------------------------------------------------------------------
# Host side: Docker


def _docker_base(interactive: bool) -> list[str]:
    cmd = ["docker", "run", "--rm"]
    if interactive:
        cmd.append("-it")
    elif sys.stdout.isatty():
        cmd.append("-t")
    return cmd + [
        "-v",
        f"{NCS_VOLUME}:{CONTAINER_NCS}",
        "-v",
        f"{BUILD_VOLUME}:{CONTAINER_BUILD}",
        "-v",
        f"{REPO_ROOT.as_posix()}:{CONTAINER_REPO}",
        "-e",
        f"CTAG_TOOLCHAIN_IMAGE={IMAGE}",
        IMAGE,
        "-c",
    ]


def _docker_env() -> dict[str, str]:
    # Git Bash would otherwise rewrite the container paths into Windows paths.
    return os.environ | {"MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}


def run_docker(script: str, interactive: bool = False) -> int:
    if shutil.which("docker") is None:
        print("error: docker not found on PATH (see docs/building.md)", file=sys.stderr)
        return EXIT_USAGE
    return subprocess.call(_docker_base(interactive) + [script], env=_docker_env())


def container_script(args: argparse.Namespace, names: list[str]) -> str:
    inner = ["python3", f"{CONTAINER_REPO}/tools/build.py", "--in-container"]
    if args.pristine:
        inner.append("--pristine")
    if args.verbose:
        inner.append("--verbose")
    if args.allow_resource_miss:
        inner.append("--allow-resource-miss")
    if args.app:
        inner += ["--app", args.app]
    if getattr(args, "out_root", None):
        inner += ["--out-root", args.out_root]
    if getattr(args, "skip_workspace_check", False):
        inner.append("--skip-workspace-check")
    if hasattr(os, "getuid"):
        inner += ["--chown", f"{os.getuid()}:{os.getgid()}"]
    inner += names
    check = (
        f"test -d {CONTAINER_NCS}/.west || "
        f"{{ echo 'error: volume {NCS_VOLUME} holds no NCS workspace; run: python tools/build.py --setup' >&2; exit {EXIT_USAGE}; }}"
    )
    return f"{check} && {shlex.join(inner)}"


def setup_script() -> str:
    return (
        f"cd {CONTAINER_NCS} && "
        f"{{ test -d .west || west init -m {NCS_MANIFEST_URL} --mr {NCS_REVISION}; }} && "
        "west update --narrow -o=--depth=1"
    )


# --------------------------------------------------------------------------


def list_targets(targets: dict[str, Target], socs: dict[str, vs.SocGeometry]) -> None:
    print(f"{'target':20} {'app':14} {'board':22} {'soc':15} {'snippets':16} {'RAM free min':>12}  app present")
    for t in targets.values():
        present = "yes" if (REPO_ROOT / t.app / "CMakeLists.txt").is_file() else "no"
        ram = str(t.ram_free_min) if t.ram_free_min is not None else "-"
        print(f"{t.name:20} {t.app:14} {t.board:22} {t.soc:15} {','.join(t.snippets) or '-':16} {ram:>12}  {present}")
        for err in validate_target(t, socs):
            print(f"  ! {err}")


def _app_relative(app: str, option: str = "--app") -> str:
    path = Path(app)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            raise SystemExit(f"error: {option} must be inside the repository ({REPO_ROOT})") from None
    return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="*", help="target names from tools/targets.yaml")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    parser.add_argument("--all", action="store_true", help="build every target")
    parser.add_argument("--pristine", action="store_true", help="rebuild from scratch (west build -p always)")
    parser.add_argument("--app", help="build this app directory (repository-relative) instead of each target's app")
    parser.add_argument(
        "--allow-resource-miss", action="store_true", help="report resource limit misses without failing"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="stream the full build output")
    parser.add_argument("--shell", action="store_true", help="open a shell in the toolchain container")
    parser.add_argument(
        "--setup", action="store_true", help=f"initialise/update the NCS workspace in volume {NCS_VOLUME}"
    )
    parser.add_argument("--in-container", action="store_true", help="already inside the NCS toolchain (CI)")
    parser.add_argument(
        "--ncs-dir", default=os.environ.get("NCS_DIR", CONTAINER_NCS), help="west workspace (--in-container)"
    )
    parser.add_argument(
        "--build-root",
        default=os.environ.get("CTAG_BUILD_ROOT", CONTAINER_BUILD),
        help="build directories (--in-container)",
    )
    parser.add_argument(
        "--out-root",
        help="write artifacts and reports under this repository-relative directory instead of build/",
    )
    parser.add_argument(
        "--skip-workspace-check",
        action="store_true",
        help="skip `west compare` (the sdk-nrf commit is still checked; metadata records the skip)",
    )
    parser.add_argument("--chown", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        targets, socs = load_matrix()
    except (vs.UsageError, OSError, KeyError) as exc:
        print(f"error: tools/targets.yaml: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.list:
        list_targets(targets, socs)
        return EXIT_OK
    if args.shell:
        return run_docker(f"cd {CONTAINER_NCS} && exec bash", interactive=True)
    if args.setup:
        return run_docker(setup_script())

    names = list(targets) if args.all else args.targets
    unknown = [n for n in names if n not in targets]
    if unknown or not names:
        parser.error(f"unknown target(s): {', '.join(unknown)}" if unknown else "give target names, --all or --list")
    if args.app:
        args.app = _app_relative(args.app)
    if args.out_root:
        args.out_root = _app_relative(args.out_root, "--out-root")

    if args.in_container:
        return run_in_container(args, names)
    return run_docker(container_script(args, names))


if __name__ == "__main__":
    sys.exit(main())
