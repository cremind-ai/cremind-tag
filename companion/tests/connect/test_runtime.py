"""Versions and how Connect starts itself."""

from __future__ import annotations

import sys

import pytest

from cremind_tag.connect import runtime


@pytest.mark.parametrize(("older", "newer"), [
    ("0.1.0", "0.1.1"), ("0.1.9", "0.1.10"), ("0.2.0.dev3", "0.2.0a1"), ("0.2.0a1", "0.2.0b1"),
    ("0.2.0b2", "0.2.0rc1"), ("0.2.0rc1", "0.2.0"), ("0.2.0-rc.1", "0.2.0"), ("0.2.0", "0.2.0.post1"),
    ("0.2.0.dev1", "0.2.0.dev2"), ("1.0", "1.0.1"), ("v1.2.3", "1.2.4"),
])
def test_version_order(older: str, newer: str) -> None:
    assert runtime.compare_versions(older, newer) == -1
    assert runtime.compare_versions(newer, older) == 1
    assert runtime.is_newer(newer, older) and not runtime.is_newer(older, newer)


def test_equal_versions_and_local_parts() -> None:
    assert runtime.compare_versions("1.0", "1.0.0") == 0
    assert runtime.compare_versions("1.0+ci.5", "1.0") == 0
    assert runtime.version_key("0.2.0-rc.1") == runtime.version_key("0.2.0rc1")


def test_garbage_is_never_newer() -> None:
    with pytest.raises(ValueError):
        runtime.version_key("latest")
    assert not runtime.is_newer("latest", "0.1.0")


def test_self_command_from_source() -> None:
    assert runtime.self_command("service") == [sys.executable, "-m", "cremind_tag.connect", "service"]


def test_self_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert runtime.self_command("worker", "--dir", "d") == [str(runtime.executable()), "worker", "--dir", "d"]


def test_os_kind_and_exe_name() -> None:
    assert runtime.os_kind("win32") == "windows" and runtime.os_kind("darwin") == "macos"
    assert runtime.os_kind("linux") == "linux" and runtime.os_kind("freebsd14") == "linux"
    assert runtime.exe_name("windows") == "cremind-connect.exe" and runtime.exe_name("macos") == "cremind-connect"


def test_version_from_source_is_the_companion_version() -> None:
    from cremind_tag import __version__

    assert runtime.connect_version() == __version__


def test_pid_alive() -> None:
    import os

    assert runtime.pid_alive(os.getpid())
    assert not runtime.pid_alive(0)
