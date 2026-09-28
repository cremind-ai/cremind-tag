#!/usr/bin/env python3
"""Build Cremind Connect: the PyInstaller bundle, a smoke test and the OS installers (docs/connect-packaging.md).

Run from ``companion/`` in an environment with the ``package`` dependency group::

    uv sync --group package
    uv run python packaging/build_connect.py --smoke                        # bundle + smoke test
    uv run python packaging/build_connect.py --version 0.2.0 --smoke --installer
    uv run python packaging/build_connect.py --skip-build --installer       # installers from dist/ (after signing)
    uv run python packaging/build_connect.py --fontpack ../fonts/out/full --font-cache ../fonts/cache --smoke

Outputs (``--dist``, default ``companion/dist/connect``)::

    cremind-connect/                     the one-directory bundle (Windows, Linux) with connect.json beside the exe
    Cremind Connect.app                  the macOS app (connect.json in Contents/Resources)
    installers/Cremind-Connect-<v>-windows-x64.exe     Inno Setup, per user, no administrator rights
    installers/Cremind-Connect-<v>-macos-<arch>.dmg    the app and an Applications link
    installers/cremind-connect_<v>_<arch>.deb          /opt/cremind-connect, udev rule, systemd user unit
    installers/cremind-connect-<v>-linux-<arch>.tar.gz a per-user install with install.sh

PyInstaller's work files go to ``--work`` (default ``companion/dist/.build``).
Nothing is installed or registered on the build machine; the smoke test runs
the bundle with a private ``CREMIND_CONNECT_HOME``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
COMPANION = HERE.parent
REPO = COMPANION.parent
SPEC = HERE / "cremind-connect.spec"
APP_NAME = "Cremind Connect.app"
BUNDLE_NAME = "cremind-connect"
DEFAULT_MAINTAINER = "Cremind Tag maintainers <maintainers@cremind.invalid>"

sys.path.insert(0, str(COMPANION / "src"))


def kind() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)


def default_version() -> str:
    from cremind_tag import __version__

    return os.environ.get("CONNECT_VERSION") or __version__


def exe_relative(os_kind: str) -> str:
    """The executable relative to the bundle root (the one-dir folder, or the .app)."""
    return {"windows": "cremind-connect.exe", "macos": "Contents/MacOS/cremind-connect"}.get(os_kind,
                                                                                           "cremind-connect")


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def build_info(version: str) -> dict[str, Any]:
    from cremind_tag.connect.runtime import version_key

    version_key(version)  # refuse a version install.py could not compare
    return {"name": "cremind-connect", "version": version, "exe": exe_relative(kind()), "platform": kind(),
            "arch": arch(), "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "commit": git_commit(),
            "python": platform.python_version()}


def bundle_root(dist: Path) -> Path:
    return dist / APP_NAME if kind() == "macos" else dist / BUNDLE_NAME


def bundle_exe(dist: Path) -> Path:
    return bundle_root(dist) / exe_relative(kind())


def dir_size(path: Path) -> tuple[int, int]:
    """(bytes, files) under ``path`` (symlinks not followed)."""
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            item = Path(root) / name
            if not item.is_symlink():
                total += item.stat().st_size
                files += 1
    return total, files


def log(message: str) -> None:
    print(f"build_connect: {message}", flush=True)


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def prepare_assets(args: argparse.Namespace, work: Path) -> Path | None:
    from cremind_tag.resources import font_assets_in, make_font_assets, verify_font_assets

    if args.fontpack:
        root = work / "assets"
        shutil.rmtree(root, ignore_errors=True)
        assets = make_font_assets(Path(args.fontpack), Path(args.font_cache), root)
        log(f"font assets: pack {assets.pack_id} ({assets.profile}) assembled in {root}")
        return root
    if args.assets:
        root = Path(args.assets).resolve()
        packs = font_assets_in(root)
        if not packs:
            raise SystemExit(f"build_connect: {root} holds no fonts/<pack_id>/ directory")
        for assets in packs:
            verify_font_assets(assets)
            log(f"font assets: pack {assets.pack_id} verified")
        return root
    return None


def build_bundle(args: argparse.Namespace, dist: Path, work: Path) -> Path:
    info = build_info(args.version)
    work.mkdir(parents=True, exist_ok=True)
    info_path = work / "connect.json"
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    assets = prepare_assets(args, work)
    env = {**os.environ, "CONNECT_BUILD_INFO": str(info_path), "CONNECT_ASSETS": str(assets or "")}
    command = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--distpath", str(dist),
               "--workpath", str(work / "pyinstaller")]
    if args.clean:
        command.append("--clean")
    log(f"PyInstaller {args.version} for {kind()}-{arch()}")
    subprocess.run(command, cwd=COMPANION, env=env, check=True)
    root = bundle_root(dist)
    if kind() != "macos":  # for people and installers: the version beside the executable
        shutil.copy2(info_path, root / "connect.json")
    size, files = dir_size(root)
    log(f"bundle {root}: {size / 1e6:.1f} MB in {files} files")
    return root


def smoke(dist: Path, version: str) -> None:
    """Run the bundle's ``version`` and ``status`` with a private home; fail loudly on any surprise."""
    exe = bundle_exe(dist)
    if not exe.is_file():
        raise SystemExit(f"build_connect: {exe} is missing")
    with tempfile.TemporaryDirectory(prefix="cremind-connect-smoke-") as home:
        env = {**os.environ, "CREMIND_CONNECT_HOME": home}
        env.pop("CREMIND_TAG_ASSETS", None)
        for args in (["version"], ["version", "--json"], ["status", "--json"]):
            started = time.monotonic()
            done = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=300, env=env,
                                  stdin=subprocess.DEVNULL)
            took = time.monotonic() - started
            if done.returncode != 0:
                raise SystemExit(f"build_connect: `{' '.join(args)}` exited {done.returncode}\n{done.stdout}\n"
                                 f"{done.stderr}")
            out = done.stdout.strip()
            if args == ["version"]:
                if out != version:
                    raise SystemExit(f"build_connect: version says {out!r}, expected {version!r}")
            else:
                data = json.loads(out)
                if args[0] == "version" and (data.get("version") != version or data.get("frozen") is not True):
                    raise SystemExit(f"build_connect: unexpected version report {data}")
                if args[0] == "status" and (data.get("service", {}).get("running") is not False
                                            or data.get("version") != version):
                    raise SystemExit(f"build_connect: unexpected status report {data}")
            log(f"smoke: `cremind-connect {' '.join(args)}` ok in {took:.1f} s: {out[:160]!r}"
                + ("..." if len(out) > 160 else ""))


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


def find_iscc() -> str | None:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for sub in ("Inno Setup 6", os.path.join("Programs", "Inno Setup 6")):
            candidate = Path(base or "") / sub / "ISCC.exe"
            if base and candidate.is_file():
                return str(candidate)
    return None


def windows_installer(dist: Path, version: str, out: Path) -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("build_connect: Inno Setup 6 (ISCC.exe) is not installed (choco install innosetup)")
    out.mkdir(parents=True, exist_ok=True)
    numeric = ".".join((re.findall(r"\d+", version) + ["0"] * 4)[:4])
    subprocess.run([iscc, f"/DAppVersion={version}", f"/DAppVersionNumeric={numeric}",
                    f"/DSourceDir={bundle_root(dist)}", f"/DOutputDir={out}", f"/DArch={arch()}",
                    str(HERE / "windows" / "cremind-connect.iss")], check=True)
    installer = out / f"Cremind-Connect-{version}-windows-{arch()}.exe"
    log(f"installer {installer} ({installer.stat().st_size / 1e6:.1f} MB)")
    return installer


def macos_dmg(dist: Path, version: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    dmg = out / f"Cremind-Connect-{version}-macos-{arch()}.dmg"
    with tempfile.TemporaryDirectory(prefix="cremind-connect-dmg-") as staging:
        subprocess.run(["ditto", str(bundle_root(dist)), str(Path(staging) / APP_NAME)], check=True)
        os.symlink("/Applications", Path(staging) / "Applications")
        subprocess.run(["hdiutil", "create", "-volname", "Cremind Connect", "-srcfolder", staging, "-ov",
                        "-format", "UDZO", str(dmg)], check=True)
    log(f"disk image {dmg} ({dmg.stat().st_size / 1e6:.1f} MB)")
    return dmg


def deb_version(version: str) -> str:
    """Debian ordering: 0.2.0rc1 -> 0.2.0~rc1, 0.2.0.dev3 -> 0.2.0~dev3 (both sort before 0.2.0)."""
    text = re.sub(r"^v", "", version)
    text = re.sub(r"[-_.]?(a|alpha|b|beta|rc|c)[-_.]?(\d+)", lambda m: f"~{m[1]}{m[2]}", text)
    return re.sub(r"[-_.]?dev[-_.]?(\d*)", lambda m: f"~dev{m[1]}", text)


def deb_arch() -> str:
    return {"x64": "amd64", "arm64": "arm64"}.get(arch(), arch())


def glibc_floor() -> str:
    """The build machine's glibc: the bundle needs at least that version (build on the oldest supported distro)."""
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, ValueError, OSError):
        text = ""
    match = re.search(r"(\d+\.\d+)", text)
    return match.group(1) if match else "2.35"


def deb_files(version: str, maintainer: str, installed_kb: int) -> dict[str, tuple[str, int]]:
    """Package metadata and system files: path -> (content, mode)."""
    from cremind_tag.connect.udev import rules_text
    from cremind_tag.connect.urlhandler import desktop_entry

    exe = "/opt/cremind-connect/cremind-connect"
    control = f"""Package: cremind-connect
Version: {deb_version(version)}
Section: utils
Priority: optional
Architecture: {deb_arch()}
Maintainer: {maintainer}
Installed-Size: {installed_kb}
Depends: libc6 (>= {glibc_floor()}), libx11-6
Description: Cremind Connect: Cremind Tag gateways on this computer
 A per-user background service that connects the Cremind Tag gateways
 plugged into this computer to Cremind, so e-paper tags keep receiving
 updates after every browser window is closed. Opens cremind-connect: links
 from Cremind's Settings, Tags.
"""
    postinst = """#!/bin/sh
# Cremind Connect: reload udev (the gateway's USB rule), enable the per-user service, register the link handler.
set -e
if [ "$1" = "configure" ]; then
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=tty --action=add >/dev/null 2>&1 || true
    systemctl --global enable cremind-connect.service >/dev/null 2>&1 || true
    update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
"""
    prerm = """#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    systemctl --global disable cremind-connect.service >/dev/null 2>&1 || true
fi
exit 0
"""
    postrm = """#!/bin/sh
set -e
udevadm control --reload-rules >/dev/null 2>&1 || true
update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
exit 0
"""
    unit = f"""# Installed by the cremind-connect package: one service per logged-in user (systemctl --global enable).
[Unit]
Description=Cremind Connect (Cremind Tag gateways for this user)

[Service]
Type=simple
ExecStart={exe} service
WorkingDirectory=%h
Restart=always
RestartSec=2
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""
    return {
        "DEBIAN/control": (control, 0o644),
        "DEBIAN/postinst": (postinst, 0o755),
        "DEBIAN/prerm": (prerm, 0o755),
        "DEBIAN/postrm": (postrm, 0o755),
        "lib/udev/rules.d/70-cremind-tag.rules": (rules_text(), 0o644),
        "usr/lib/systemd/user/cremind-connect.service": (unit, 0o644),
        "usr/share/applications/cremind-connect.desktop": (desktop_entry([exe]), 0o644),
    }


def linux_deb(dist: Path, version: str, out: Path, maintainer: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    deb = out / f"cremind-connect_{deb_version(version)}_{deb_arch()}.deb"
    with tempfile.TemporaryDirectory(prefix="cremind-connect-deb-") as staging_dir:
        staging = Path(staging_dir) / "root"
        shutil.copytree(bundle_root(dist), staging / "opt" / "cremind-connect", symlinks=True)
        size, _files = dir_size(staging)
        for relative, (content, mode) in deb_files(version, maintainer, size // 1024 + 1).items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            os.chmod(path, mode)
        (staging / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        os.symlink("/opt/cremind-connect/cremind-connect", staging / "usr" / "bin" / "cremind-connect")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(staging), str(deb)], check=True)
    log(f"package {deb} ({deb.stat().st_size / 1e6:.1f} MB)")
    return deb


INSTALL_SH = """#!/bin/sh
# Install Cremind Connect for this user: ~/.local/lib/cremind-connect, a systemd user unit, the
# cremind-connect: link handler and (asking for an administrator password once) the USB rule.
# Uninstall: ~/.local/lib/cremind-connect/current/cremind-connect uninstall
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
exec "$HERE/cremind-connect" install --from "$HERE" "$@"
"""


def linux_tarball(dist: Path, version: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    name = f"cremind-connect-{version}-linux-{arch()}"
    tarball = out / f"{name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(bundle_root(dist), arcname=name)
        data = INSTALL_SH.encode("utf-8")
        member = tarfile.TarInfo(f"{name}/install.sh")
        member.size, member.mode, member.mtime = len(data), 0o755, int(time.time())
        archive.addfile(member, io.BytesIO(data))
    log(f"archive {tarball} ({tarball.stat().st_size / 1e6:.1f} MB)")
    return tarball


def installers(args: argparse.Namespace, dist: Path) -> list[Path]:
    out = dist / "installers"
    if kind() == "windows":
        return [windows_installer(dist, args.version, out)]
    if kind() == "macos":
        return [macos_dmg(dist, args.version, out)]
    return [linux_deb(dist, args.version, out, args.maintainer), linux_tarball(dist, args.version, out)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", default=None, help="the Connect version (default: CONNECT_VERSION or the "
                                                        "companion's __version__)")
    parser.add_argument("--dist", default=str(COMPANION / "dist" / "connect"), help="output directory")
    parser.add_argument("--work", default=str(COMPANION / "dist" / ".build"), help="PyInstaller work directory")
    parser.add_argument("--assets", help="a font asset root (<root>/fonts/<pack_id>/) to embed")
    parser.add_argument("--fontpack", help="a built pack directory (fonts/out/<profile>) to embed ...")
    parser.add_argument("--font-cache", help="... with its source fonts from this font cache (fonts/cache)")
    parser.add_argument("--clean", action="store_true", help="PyInstaller --clean")
    parser.add_argument("--skip-build", action="store_true", help="use the bundle already in --dist")
    parser.add_argument("--smoke", action="store_true", help="run version/status from the bundle")
    parser.add_argument("--installer", action="store_true", help="build this OS's installer(s)")
    parser.add_argument("--maintainer", default=os.environ.get("CONNECT_DEB_MAINTAINER", DEFAULT_MAINTAINER),
                        help="the .deb Maintainer field")
    args = parser.parse_args(argv)
    args.version = args.version or default_version()
    if args.fontpack and not args.font_cache:
        parser.error("--fontpack needs --font-cache")
    dist, work = Path(args.dist).resolve(), Path(args.work).resolve()
    if not args.skip_build:
        build_bundle(args, dist, work)
    if args.smoke:
        smoke(dist, args.version)
    if args.installer:
        installers(args, dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
