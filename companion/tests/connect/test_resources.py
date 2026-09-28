"""Resource resolution for a frozen bundle: the font asset bundle, and no guessing of a checkout."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from cremind_tag import resources
from cremind_tag.fontpack.format import FontPack
from cremind_tag.fonts import manifest

REPO = Path(__file__).resolve().parents[3]
FIXTURE_PACK = REPO / "protocol" / "fixtures" / "fontpack_test.ctfp"
FONT_BYTES = b"not really a font, but bytes with a SHA-256"


def write_pack_dir(directory: Path, *, font: bytes = FONT_BYTES) -> str:
    """A built pack directory (pack + sidecar + NOTICE + LICENSES); returns the pack id."""
    data = FIXTURE_PACK.read_bytes()
    pack_id = FontPack(data).pack_id.hex()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / resources.PACK_FILE).write_bytes(data)
    sidecar = {"schema": "cremind-tag/fontpack-faces@1", "pack_id": pack_id, "profile": "test", "faces": [
        {"face_id": 0, "role": "icons", "file": {"cache": "material/icons.ttf", "sha256": "0" * 64}},
        {"face_id": 1, "role": "primary", "file": {"cache": "noto/NotoSans-Regular.ttf",
                                                   "sha256": hashlib.sha256(font).hexdigest()}},
    ]}
    (directory / resources.SIDECAR_FILE).write_text(json.dumps(sidecar), encoding="utf-8")
    (directory / resources.NOTICE_FILE).write_text("Noto notices\n", encoding="utf-8")
    (directory / "LICENSES").mkdir(exist_ok=True)
    (directory / "LICENSES" / "OFL-1.1.txt").write_text("OFL\n", encoding="utf-8")
    return pack_id


def write_assets(root: Path, *, font: bytes = FONT_BYTES) -> resources.FontAssets:
    """``<root>/fonts/<pack_id>/`` with its source font in ``cache/``."""
    staging = root / "_build"
    pack_id = write_pack_dir(staging, font=font)
    target = root / "fonts" / pack_id
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(target)
    (target / "cache" / "noto").mkdir(parents=True)
    (target / "cache" / "noto" / "NotoSans-Regular.ttf").write_bytes(font)
    return resources.load_font_assets(target)


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to be a PyInstaller bundle whose resource directory is ``<tmp>/bundle/_internal``."""
    meipass = tmp_path / "bundle" / "_internal"
    meipass.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.delenv(resources.ASSETS_ENV, raising=False)
    return meipass


def test_frozen_bundle_assets_are_found(frozen: Path, tmp_path: Path) -> None:
    assets = write_assets(frozen / "assets")
    roots = resources.asset_roots(env={}, installed=tmp_path / "none")
    assert roots == [frozen / "assets"]
    found = resources.find_font_assets(roots=roots)
    assert found is not None and found.pack_id == assets.pack_id and found.profile == "test"
    assert found.pack_path.is_file() and found.cache_dir == found.root / "cache"
    assert resources.find_font_assets("ffffffffffffffff", roots=roots) is None


def test_resolution_order_env_bundle_installed(frozen: Path, tmp_path: Path) -> None:
    env_root, installed = tmp_path / "env", tmp_path / "installed"
    for root in (env_root, frozen / "assets", installed):
        root.mkdir(parents=True, exist_ok=True)
    roots = resources.asset_roots(env={resources.ASSETS_ENV: str(env_root)}, installed=installed)
    assert roots == [env_root, frozen / "assets", installed]


def test_from_source_there_is_no_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert resources.bundled_asset_dirs() == []
    assert resources.asset_roots(env={}, installed=tmp_path / "missing") == []


def test_verify_detects_tampering(tmp_path: Path) -> None:
    assets = write_assets(tmp_path)
    resources.verify_font_assets(assets)
    font = assets.cache_dir / "noto" / "NotoSans-Regular.ttf"
    font.write_bytes(b"changed")
    with pytest.raises(resources.FontAssetsError, match="does not match"):
        resources.verify_font_assets(assets)
    font.unlink()
    with pytest.raises(resources.FontAssetsError, match="missing"):
        resources.verify_font_assets(assets)


def test_layout_errors(tmp_path: Path) -> None:
    with pytest.raises(resources.FontAssetsError):
        resources.load_font_assets(tmp_path)
    pack_id = write_pack_dir(tmp_path / "wrong-name")
    with pytest.raises(resources.FontAssetsError, match="named after"):
        resources.load_font_assets(tmp_path / "wrong-name")
    assert pack_id


def test_install_copies_read_only_and_keeps_a_verified_copy(tmp_path: Path) -> None:
    source = write_assets(tmp_path / "bundle")
    installed = resources.install_font_assets(source, tmp_path / "data" / "assets")
    assert installed.root == tmp_path / "data" / "assets" / "fonts" / source.pack_id
    resources.verify_font_assets(installed)
    assert (installed.root / "LICENSES" / "OFL-1.1.txt").is_file() and installed.notice_path.is_file()
    assert not os.stat(installed.pack_path).st_mode & stat.S_IWRITE
    again = resources.install_font_assets(source, tmp_path / "data" / "assets")
    assert again.root == installed.root
    font = installed.cache_dir / "noto" / "NotoSans-Regular.ttf"
    os.chmod(font, stat.S_IWRITE | stat.S_IREAD)
    font.write_bytes(b"corrupted")
    repaired = resources.install_font_assets(source, tmp_path / "data" / "assets")
    resources.verify_font_assets(repaired)


def test_make_font_assets_from_a_build(tmp_path: Path) -> None:
    pack_dir = tmp_path / "fonts" / "out" / "full"
    pack_id = write_pack_dir(pack_dir)
    cache = tmp_path / "fonts" / "cache"
    (cache / "noto").mkdir(parents=True)
    (cache / "noto" / "NotoSans-Regular.ttf").write_bytes(FONT_BYTES)
    (cache / "noto" / "Unused.ttf").write_bytes(b"not in this pack")
    assets = resources.make_font_assets(pack_dir, cache, tmp_path / "assets")
    assert assets.root == tmp_path / "assets" / "fonts" / pack_id
    assert sorted(p.name for p in (assets.cache_dir / "noto").iterdir()) == ["NotoSans-Regular.ttf"]


def test_frozen_repo_root_never_walks_cwd_or_file(frozen: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREMIND_TAG_REPO", raising=False)
    monkeypatch.delenv("CREMIND_TAG_FONT_CACHE", raising=False)
    monkeypatch.chdir(REPO)  # a checkout right here would be found from source
    with pytest.raises(manifest.ManifestError, match="font asset bundle"):
        manifest.repo_root()
    with pytest.raises(manifest.ManifestError):
        manifest.default_cache_dir()  # callers pass the asset bundle's cache instead
    monkeypatch.setenv("CREMIND_TAG_REPO", str(REPO))
    assert manifest.repo_root() == REPO


def test_source_repo_root_still_finds_the_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("CREMIND_TAG_REPO", raising=False)
    monkeypatch.chdir(REPO / "companion")
    assert manifest.repo_root() == REPO
