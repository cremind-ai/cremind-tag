"""Where a packaged cremind-tag finds its resources: the font asset bundle (docs/connect-setup.md §11.7).

A development checkout builds packs from ``fonts/manifest.yaml`` and the font
cache (:mod:`cremind_tag.fonts`). A packaged program (Cremind Connect) never
builds fonts and never looks for a checkout (``fonts.manifest.repo_root`` refuses
to guess one when frozen): it uses the published **font asset bundle**, one
directory per pack::

    <assets root>/fonts/<pack_id>/
        fontpack.ctfp        the binary pack the bridges have active
        fontpack.json        its sidecar: face metadata, and the SHA-256 of every source font
        cache/<source>/<file>   the exact source fonts HarfBuzz shapes with (the sidecar's ``file.cache``)
        NOTICE, LICENSES/    the pack's notices and licence texts
        (anything else the build adds, e.g. coverage metadata, is carried along)

Asset roots, in order (:func:`asset_roots`):

1. ``$CREMIND_TAG_ASSETS``;
2. inside a frozen bundle: ``<sys._MEIPASS>/assets`` (``_internal/assets`` of a
   one-directory bundle, ``Contents/Resources/assets`` of the macOS app), then
   ``assets/`` next to the executable;
3. the installed, verified copies: Cremind Connect's ``<data>/assets``.

:func:`verify_font_assets` checks a pack against its sidecar (the pack parses and
its id matches; every source font has the recorded SHA-256), and
:func:`install_font_assets` copies a verified pack read-only into
``<data>/assets/fonts/<pack_id>/``, where every worker shares it. A consumer
loads fonts with ``cremind_tag.fonts.build.load_fontset(a.pack_path, a.cache_dir)``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ASSETS_ENV = "CREMIND_TAG_ASSETS"
FONTS_DIR = "fonts"
PACK_FILE = "fontpack.ctfp"
SIDECAR_FILE = "fontpack.json"
CACHE_DIR = "cache"
NOTICE_FILE = "NOTICE"
LICENSES_DIR = "LICENSES"


class FontAssetsError(ValueError):
    """A font asset directory is incomplete, inconsistent or corrupt."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_asset_dirs() -> list[Path]:
    """``assets`` inside the running frozen bundle (empty from source)."""
    if not is_frozen():
        return []
    out = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        out.append(Path(base) / "assets")
    out.append(Path(sys.executable).resolve().parent / "assets")
    return out


def asset_roots(env: Mapping[str, str] | None = None, installed: Path | None = None) -> list[Path]:
    """Candidate asset roots in priority order (see the module docstring); missing ones are skipped."""
    env = os.environ if env is None else env
    roots: list[Path] = []
    if env.get(ASSETS_ENV):
        roots.append(Path(env[ASSETS_ENV]))
    roots += bundled_asset_dirs()
    if installed is None:
        with contextlib.suppress(Exception):
            from cremind_tag.connect.paths import default_paths

            installed = default_paths(env).assets_dir
    if installed is not None:
        roots.append(installed)
    seen: set[str] = set()
    out = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen and root.is_dir():
            seen.add(key)
            out.append(root)
    return out


@dataclass(frozen=True)
class FontAssets:
    """One pack's asset directory."""

    root: Path
    pack_id: str
    profile: str | None
    sidecar: dict[str, Any]

    @property
    def pack_path(self) -> Path:
        return self.root / PACK_FILE

    @property
    def sidecar_path(self) -> Path:
        return self.root / SIDECAR_FILE

    @property
    def cache_dir(self) -> Path:
        return self.root / CACHE_DIR

    @property
    def notice_path(self) -> Path:
        return self.root / NOTICE_FILE

    def face_files(self) -> list[tuple[Path, str]]:
        """``(path, sha256)`` of every source font the sidecar names (the icon face has none)."""
        out = []
        for face in self.sidecar.get("faces", []):
            info = face.get("file") if face.get("role") != "icons" else None
            if isinstance(info, dict) and info.get("cache") and info.get("sha256"):
                out.append((self.cache_dir / Path(*str(info["cache"]).split("/")), str(info["sha256"])))
        return out


def load_font_assets(directory: Path) -> FontAssets:
    """Describe ``<root>/fonts/<pack_id>`` (layout only; :func:`verify_font_assets` checks content)."""
    directory = Path(directory)
    for name in (PACK_FILE, SIDECAR_FILE):
        if not (directory / name).is_file():
            raise FontAssetsError(f"{directory} has no {name}")
    try:
        sidecar = json.loads((directory / SIDECAR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FontAssetsError(f"{directory / SIDECAR_FILE}: {exc}") from None
    pack_id = sidecar.get("pack_id") if isinstance(sidecar, dict) else None
    if not isinstance(pack_id, str) or not pack_id:
        raise FontAssetsError(f"{directory / SIDECAR_FILE} names no pack_id")
    if directory.name != pack_id:
        raise FontAssetsError(f"{directory} holds pack {pack_id}; the directory must be named after it")
    return FontAssets(directory, pack_id, sidecar.get("profile"), sidecar)


def font_assets_in(root: Path) -> list[FontAssets]:
    """Every well-formed pack directory under ``<root>/fonts`` (malformed ones are skipped)."""
    out = []
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        for directory in sorted((Path(root) / FONTS_DIR).iterdir()):
            if directory.is_dir() and not directory.name.startswith("."):
                with contextlib.suppress(FontAssetsError):
                    out.append(load_font_assets(directory))
    return out


def find_font_assets(pack_id: str | None = None, *, roots: Sequence[Path] | None = None) -> FontAssets | None:
    """The pack ``pack_id`` (or, without one, the first pack) from the first root that has it."""
    for root in asset_roots() if roots is None else roots:
        for assets in font_assets_in(root):
            if pack_id is None or assets.pack_id == pack_id:
                return assets
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_font_assets(assets: FontAssets) -> None:
    """Raise :class:`FontAssetsError` unless the pack and every source font are exactly what the sidecar says."""
    from cremind_tag.fontpack.format import FontPack, FontPackError

    try:
        pack = FontPack(assets.pack_path.read_bytes())
    except (OSError, FontPackError) as exc:
        raise FontAssetsError(f"{assets.pack_path}: {exc}") from None
    if pack.pack_id.hex() != assets.pack_id:
        raise FontAssetsError(f"{assets.pack_path} is pack {pack.pack_id.hex()}, the sidecar says {assets.pack_id}")
    for path, expected in assets.face_files():
        try:
            actual = _sha256(path)
        except OSError:
            raise FontAssetsError(f"{path} is missing") from None
        if actual != expected:
            raise FontAssetsError(f"{path} does not match the font the pack was rendered from")


def make_font_assets(pack_dir: Path, cache_dir: Path, out_root: Path) -> FontAssets:
    """Assemble ``<out_root>/fonts/<pack_id>`` from a built pack directory and the font cache (release builds)."""
    pack_dir, cache_dir = Path(pack_dir), Path(cache_dir)
    sidecar = json.loads((pack_dir / SIDECAR_FILE).read_text(encoding="utf-8"))
    pack_id = str(sidecar["pack_id"])
    target = Path(out_root) / FONTS_DIR / pack_id
    if target.exists():
        _rmtree(target)
    target.mkdir(parents=True)
    for name in (PACK_FILE, SIDECAR_FILE, NOTICE_FILE):
        if (pack_dir / name).is_file():
            shutil.copy2(pack_dir / name, target / name)
    if (pack_dir / LICENSES_DIR).is_dir():
        shutil.copytree(pack_dir / LICENSES_DIR, target / LICENSES_DIR)
    assets = FontAssets(target, pack_id, sidecar.get("profile"), sidecar)
    for path, _sha in assets.face_files():
        relative = path.relative_to(assets.cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_dir / relative, path)
    verify_font_assets(assets)
    return assets


def install_font_assets(source: FontAssets, assets_dir: Path) -> FontAssets:
    """Verify ``source`` and copy it (read-only) to ``<assets_dir>/fonts/<pack_id>``; an existing verified copy stays."""
    target = Path(assets_dir) / FONTS_DIR / source.pack_id
    if target.is_dir():
        with contextlib.suppress(FontAssetsError):
            existing = load_font_assets(target)
            verify_font_assets(existing)
            return existing
        _rmtree(target)
    verify_font_assets(source)
    staging = target.with_name(f".{source.pack_id}.partial-{os.getpid()}")
    _rmtree(staging)
    shutil.copytree(source.root, staging)
    try:
        verify_font_assets(FontAssets(staging, source.pack_id, source.profile, source.sidecar))
        for path in staging.rglob("*"):
            if path.is_file():
                os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        os.rename(staging, target)
    except BaseException:
        _rmtree(staging)
        raise
    return load_font_assets(target)


def _rmtree(path: Path) -> None:
    def onexc(func: Any, target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path, onexc=onexc)


__all__ = ["ASSETS_ENV", "FontAssets", "FontAssetsError", "asset_roots", "bundled_asset_dirs", "find_font_assets",
           "font_assets_in", "install_font_assets", "is_frozen", "load_font_assets", "make_font_assets",
           "verify_font_assets"]
