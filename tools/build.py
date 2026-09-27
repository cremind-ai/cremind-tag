#!/usr/bin/env python3
"""Build the firmware target matrix with the pinned NCS toolchain.

On a host (Windows Git Bash or Linux) this script starts the NCS v3.4.1
toolchain container with three mounts: the NCS workspace volume at /ncs, the
build volume at /build (building on the repository bind mount is slow) and this
repository at /work. Inside the container (``--in-container``, also used by CI
directly) it runs ``west build --no-sysbuild`` per target in
``<build-root>/<target>``, copies the artifacts to ``build/<target>/`` in the
repository, runs tools/verify_stack.py on them, compares flash/RAM with the
target's resource limits and writes build/memory-report.md and .json.

Examples::

    python tools/build.py --list
    python tools/build.py tag-laowu-bw bridge-nrf52840dk
    python tools/build.py --all --pristine
    python tools/build.py --app build/boardcheck tag-sifei-52810   # other app, same board setup
    python tools/build.py --shell
    python tools/build.py --setup                                   # create the NCS workspace volume

Exit status: 0 = every requested target built, verified and met its resource
limits; 1 = a build, verification or resource limit failed; 2 = usage error;
3 = nothing failed but a target was skipped because its app does not exist yet.
"""

from __future__ import annotations

import argparse
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

REPO_ROOT = vs.REPO_ROOT
OUT_ROOT = REPO_ROOT / "build"
REPORT_JSON = OUT_ROOT / "memory-report.json"
REPORT_MD = OUT_ROOT / "memory-report.md"

NCS_REVISION = "v3.4.1"
NCS_MANIFEST_URL = "https://github.com/nrfconnect/sdk-nrf"
IMAGE = f"ghcr.io/nrfconnect/sdk-nrf-toolchain:{NCS_REVISION}"
NCS_VOLUME = f"ncs-{NCS_REVISION}"
BUILD_VOLUME = "ctag-build"
CONTAINER_NCS = "/ncs"
CONTAINER_BUILD = "/build"
CONTAINER_REPO = "/work"
LL_SNIPPET = "bt-ll-sw-split"

# Published artifact name -> path inside the Zephyr build directory.
ARTIFACTS: dict[str, str] = {
    "zephyr.hex": "zephyr/zephyr.hex",
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
        )
    return targets, socs


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


def west_command(t: Target, app_dir: Path, build_dir: Path, repo_dir: Path, pristine: bool) -> list[str]:
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
    return cmd


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
# In-container build


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_logged(cmd: list[str], cwd: Path, log_path: Path, verbose: bool) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + shlex.join(cmd) + "\n")
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
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
) -> dict[str, Any] | None:
    """Build, collect, verify and measure one target. None = skipped (no app)."""
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
    cmd = west_command(t, app_dir, build_dir, REPO_ROOT, pristine)
    print(f"BUILD {t.name}: {t.board} {' '.join('-S ' + s for s in t.snippets)} <- {app_rel}", flush=True)
    rc = _run_logged(cmd, ncs_dir, out_dir / "build.log", verbose)
    if rc != 0:
        print(f"FAIL {t.name}: west build exited {rc} (log: build/{t.name}/build.log)", flush=True)
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
    print(f"{'OK  ' if entry['status'] == 'ok' else 'FAIL'} {t.name}: {entry['status']}", flush=True)
    return entry


def _fmt_bytes(n: int | None) -> str:
    return "-" if n is None else f"{n:,}"


def write_reports(results: dict[str, dict[str, Any]]) -> None:
    OUT_ROOT.mkdir(exist_ok=True)
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

    results: dict[str, dict[str, Any]] = {}
    failed = skipped = False
    for name in names:
        t = targets[name]
        errors = validate_target(t, socs)
        if errors:
            print("FAIL " + "; ".join(errors), flush=True)
            failed = True
            continue
        entry = build_one(t, socs[t.soc], args.app or t.app, ncs_dir, build_root, args.pristine, args.verbose)
        if entry is None:
            skipped = True
            continue
        results[name] = entry
        if entry["status"] == "resource-miss" and args.allow_resource_miss:
            continue
        failed |= entry["status"] != "ok"

    if results:
        write_reports(results)
        print(f"Report: {REPORT_MD.relative_to(REPO_ROOT).as_posix()}", flush=True)
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


def _app_relative(app: str) -> str:
    path = Path(app)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            raise SystemExit(f"error: --app must be inside the repository ({REPO_ROOT})") from None
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

    if args.in_container:
        return run_in_container(args, names)
    return run_docker(container_script(args, names))


if __name__ == "__main__":
    sys.exit(main())
