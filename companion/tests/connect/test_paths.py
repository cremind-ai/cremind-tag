"""Where Connect keeps things on each OS (docs/connect-setup.md §11.2)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path, PurePath

import pytest

from cremind_tag.connect.paths import HOME_ENV, PathsError, default_paths, ensure_private_dir
from cremind_tag.connect.runtime import APP_BUNDLE_NAME


def _parts(path: Path) -> tuple[str, ...]:
    return PurePath(str(path).replace("\\", "/")).parts


def test_windows_defaults() -> None:
    env = {"LOCALAPPDATA": r"C:\Users\anna\AppData\Local", "USERPROFILE": r"C:\Users\anna"}
    p = default_paths(env, "win32")
    assert p.kind == "windows" and not p.overridden
    assert _parts(p.data_dir)[-3:] == ("Local", "Cremind", "Connect")
    assert _parts(p.app_root)[-3:] == ("Local", "Programs", "Cremind Connect")
    assert p.logs_dir == p.data_dir / "logs" and p.workers_dir == p.data_dir / "workers"
    assert p.assets_dir == p.data_dir / "assets" and p.runtime_dir == p.data_dir / "run"
    assert p.current == p.app_root / "current" and p.versions_dir == p.app_root / "versions"


def test_macos_defaults() -> None:
    p = default_paths({"HOME": "/Users/anna"}, "darwin")
    assert p.kind == "macos"
    assert p.data_dir == Path("/Users/anna/Library/Application Support/Cremind Connect")
    assert p.app_root == p.data_dir / "app"
    assert p.current == Path("/Users/anna/Applications") / APP_BUNDLE_NAME  # what Finder and the LaunchAgent see
    assert p.runtime_dir == p.data_dir / "run"


def test_linux_defaults_follow_xdg() -> None:
    p = default_paths({"HOME": "/home/anna"}, "linux")
    assert p.data_dir == Path("/home/anna/.local/share/cremind-connect")
    assert p.app_root == Path("/home/anna/.local/lib/cremind-connect")
    assert p.runtime_dir == p.data_dir / "run"
    q = default_paths({"HOME": "/home/anna", "XDG_DATA_HOME": "/data/anna", "XDG_RUNTIME_DIR": "/run/user/1000"},
                      "linux")
    assert q.data_dir == Path("/data/anna/cremind-connect")
    assert q.runtime_dir == Path("/run/user/1000/cremind-connect")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_home_override_puts_everything_under_one_directory(tmp_path: Path, platform: str) -> None:
    p = default_paths({HOME_ENV: str(tmp_path), "HOME": "/elsewhere"}, platform)
    assert p.overridden
    for directory in (p.data_dir, p.logs_dir, p.workers_dir, p.assets_dir, p.runtime_dir, p.app_root, p.current):
        assert tmp_path in directory.parents


def test_files_live_in_the_data_directory(tmp_path: Path) -> None:
    p = default_paths({HOME_ENV: str(tmp_path)})
    assert p.installation_key.parent == p.data_dir and p.installation_json.parent == p.data_dir
    assert p.ipc_key.parent == p.data_dir and p.install_record.name == "install.json"
    assert p.service_lock.parent == p.runtime_dir


def test_worker_dir_accepts_plain_names_only(tmp_path: Path) -> None:
    p = default_paths({HOME_ENV: str(tmp_path)})
    assert p.worker_dir("w-1_a") == p.workers_dir / "w-1_a"
    for bad in ("", "..", "../x", "a/b", "a\\b", ".hidden", "x" * 65):
        with pytest.raises(ValueError):
            p.worker_dir(bad)


def test_ensure_creates_the_directories(tmp_path: Path) -> None:
    p = default_paths({HOME_ENV: str(tmp_path)}).ensure()
    for directory in (p.data_dir, p.logs_dir, p.workers_dir, p.assets_dir, p.runtime_dir):
        assert directory.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p.runtime_dir).st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_private_dir_is_tightened_and_foreign_dirs_refused(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    ensure_private_dir(loose)
    assert stat.S_IMODE(os.stat(loose).st_mode) == 0o700
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PathsError):
        ensure_private_dir(link)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_long_socket_paths_fall_back_to_a_short_private_directory(tmp_path: Path) -> None:
    deep = tmp_path / ("d" * 120)
    p = default_paths({HOME_ENV: str(deep)})
    assert len(os.fsencode(str(p.socket_path))) <= 100
    assert str(p.socket_path).startswith("/tmp/cremind-connect-")
