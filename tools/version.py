#!/usr/bin/env python3
"""The release version: one semver string in ``VERSION``, copied everywhere it is needed.

``VERSION`` at the repository root holds ``MAJOR.MINOR.PATCH`` or
``MAJOR.MINOR.PATCH-{alpha|beta|rc}.N`` (each number 0..255: Zephyr and the
tag's CAPS carry them as bytes). It is the only place a version is edited;
these copies are derived from it:

- ``apps/<app>/VERSION`` for every app in tools/targets.yaml, in Zephyr's
  format, so ``app_version.h`` gives ``APP_VERSION_MAJOR/MINOR/PATCHLEVEL`` and
  ``APP_VERSION_STRING`` (what the tag's CAPS and the gateway's HELLO report);
- ``__version__`` in companion/src/cremind_tag/__init__.py (PEP 440 form:
  ``0.2.0-rc.1`` becomes ``0.2.0rc1``), which names the wheel.

Examples::

    python tools/version.py                 # 0.1.0
    python tools/version.py --check         # exit 1 if a copy differs
    python tools/version.py --sync          # rewrite the copies from VERSION
    python tools/version.py --set 0.2.0     # new version, then sync
    python tools/version.py --expect-tag v0.1.0   # release CI: the tag must be v<VERSION>

tools/build.py records the version (and whether the app's copy matches) in
each target's metadata.json; tools/release.py refuses to package when a copy
differs. Exit status: 0 ok, 1 a check failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
TARGETS_FILE = REPO_ROOT / "tools" / "targets.yaml"
COMPANION_INIT = Path("companion") / "src" / "cremind_tag" / "__init__.py"

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:alpha|beta|rc)\.(?:0|[1-9]\d*)))?$"
)
_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}
_INIT_VERSION = re.compile(r'(?m)^__version__\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: str = ""
    """``alpha.N``, ``beta.N``, ``rc.N`` or empty."""

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (f"-{self.pre}" if self.pre else "")

    @property
    def tag(self) -> str:
        return f"v{self}"

    @property
    def pep440(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if not self.pre:
            return base
        kind, number = self.pre.split(".")
        return f"{base}{_PEP440_PRE[kind]}{number}"

    def zephyr_file(self) -> str:
        """The app VERSION file Zephyr's cmake/modules/version.cmake reads."""
        return (
            f"VERSION_MAJOR = {self.major}\n"
            f"VERSION_MINOR = {self.minor}\n"
            f"PATCHLEVEL = {self.patch}\n"
            "VERSION_TWEAK = 0\n"
            f"EXTRAVERSION ={' ' + self.pre if self.pre else ''}\n"
        )


def parse(text: str) -> Version:
    m = _SEMVER.match(text.strip())
    if m is None:
        raise ValueError(f"{text.strip()!r} is not MAJOR.MINOR.PATCH[-alpha.N|-beta.N|-rc.N]")
    version = Version(int(m[1]), int(m[2]), int(m[3]), m[4] or "")
    if max(version.major, version.minor, version.patch) > 255:
        raise ValueError(f"{version}: each number must fit in a byte (Zephyr VERSION, tag CAPS)")
    return version


def read_version(path: Path = VERSION_FILE) -> Version:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None


def parse_zephyr_version(text: str) -> Version | None:
    """The version in a Zephyr VERSION file, or None if a field is missing."""
    fields = dict(
        re.findall(
            r"(?m)^[ \t]*(VERSION_MAJOR|VERSION_MINOR|PATCHLEVEL|VERSION_TWEAK|EXTRAVERSION)[ \t]*=[ \t]*(\S*)", text
        )
    )
    try:
        major, minor, patch = (int(fields[k]) for k in ("VERSION_MAJOR", "VERSION_MINOR", "PATCHLEVEL"))
    except (KeyError, ValueError):
        return None
    if fields.get("VERSION_TWEAK", "0") not in ("", "0"):
        return None
    return Version(major, minor, patch, fields.get("EXTRAVERSION", ""))


def app_dirs(targets_file: Path = TARGETS_FILE) -> list[str]:
    """Every app directory of the target matrix, repository-relative, in first-use order."""
    with targets_file.open(encoding="utf-8") as fh:
        targets = (yaml.safe_load(fh) or {}).get("targets") or {}
    seen: dict[str, None] = {}
    for entry in targets.values():
        seen.setdefault(entry["app"], None)
    return list(seen)


def app_version(app_dir: Path) -> Version | None:
    path = app_dir / "VERSION"
    return parse_zephyr_version(path.read_text(encoding="utf-8")) if path.is_file() else None


def companion_version(root: Path = REPO_ROOT) -> str | None:
    path = root / COMPANION_INIT
    if not path.is_file():
        return None
    m = _INIT_VERSION.search(path.read_text(encoding="utf-8"))
    return m[1] if m else None


def check(version: Version, root: Path = REPO_ROOT, targets_file: Path | None = None) -> list[str]:
    """Every derived copy that differs from ``version`` (empty = all in sync).

    Apps whose directory has no CMakeLists.txt yet are skipped.
    """
    problems: list[str] = []
    for app in app_dirs(targets_file or root / "tools" / "targets.yaml"):
        app_dir = root / app
        if not (app_dir / "CMakeLists.txt").is_file():
            continue
        found = app_version(app_dir)
        if not (app_dir / "VERSION").is_file():
            problems.append(f"{app}/VERSION is missing (VERSION says {version})")
        elif found is None:
            problems.append(f"{app}/VERSION is not a Zephyr VERSION file")
        elif found != version:
            problems.append(f"{app}/VERSION says {found}, VERSION says {version}")
    companion = companion_version(root)
    if companion is not None and companion != version.pep440:
        problems.append(f"{COMPANION_INIT.as_posix()} __version__ is {companion}, VERSION says {version.pep440}")
    return problems


def sync(version: Version, root: Path = REPO_ROOT, targets_file: Path | None = None) -> list[Path]:
    """Rewrite every derived copy from ``version``; returns the files changed."""
    changed: list[Path] = []
    for app in app_dirs(targets_file or root / "tools" / "targets.yaml"):
        app_dir = root / app
        if not (app_dir / "CMakeLists.txt").is_file():
            continue
        path = app_dir / "VERSION"
        if app_version(app_dir) != version:  # formatting alone never rewrites a file
            path.write_text(version.zephyr_file(), encoding="utf-8", newline="\n")
            changed.append(path)
    init = root / COMPANION_INIT
    if init.is_file():
        text = init.read_text(encoding="utf-8")
        new = _INIT_VERSION.sub(f'__version__ = "{version.pep440}"', text, count=1)
        if new != text:
            init.write_text(new, encoding="utf-8", newline="\n")
            changed.append(init)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="exit 1 if an app VERSION or the companion differs")
    group.add_argument("--sync", action="store_true", help="rewrite the derived copies from VERSION")
    group.add_argument("--set", metavar="VERSION", help="write VERSION, then sync")
    parser.add_argument("--expect-tag", metavar="TAG", help="also require TAG == v<VERSION> (release CI)")
    parser.add_argument("--json", action="store_true", help="print the version forms as JSON")
    args = parser.parse_args(argv)

    try:
        if args.set:
            version = parse(args.set)
            VERSION_FILE.write_text(f"{version}\n", encoding="utf-8", newline="\n")
        else:
            version = read_version()
    except ValueError as exc:
        print(f"version: error: {exc}", file=sys.stderr)
        return 2

    status = 0
    if args.sync or args.set:
        for path in sync(version):
            print(f"updated {path.relative_to(REPO_ROOT).as_posix()}")
    if args.check:
        problems = check(version)
        for problem in problems:
            print(f"version: {problem} (run: python tools/version.py --sync)", file=sys.stderr)
        status = 1 if problems else 0
    if args.expect_tag is not None and args.expect_tag != version.tag:
        print(f"version: tag {args.expect_tag!r} does not match VERSION {version} (expected {version.tag})",
              file=sys.stderr)
        status = 1
    if args.json:
        print(json.dumps({"version": str(version), "tag": version.tag, "pep440": version.pep440}))
    elif status == 0 and not (args.sync or args.set):
        print(version)
    return status


if __name__ == "__main__":
    sys.exit(main())
