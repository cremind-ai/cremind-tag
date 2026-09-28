"""The cremind-connect command line (nothing here registers or starts anything on this machine)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connect import ipc, main
from cremind_tag.connect.paths import ConnectPaths

LINK = ("cremind-connect://setup?v=1&server=https%3A%2F%2Fcremind.example.org&"
        "session=3f2b8c1e-5a4d-4e6f-9a0b-1c2d3e4f5a6b&token=tok_Ab-cd_ef0123456789XYZ")


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "_show_message", lambda title, message: shown.append((title, message)))
    return shown


def test_a_bare_link_means_open() -> None:
    assert main.normalize_argv([LINK]) == ["open", LINK]
    assert main.normalize_argv(["-psn_0_12345", LINK]) == ["open", LINK]
    assert main.normalize_argv(["status", "--json"]) == ["status", "--json"]


def test_version(capsys: pytest.CaptureFixture[str], connect_home: Path) -> None:
    from cremind_tag import __version__

    assert main.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert main.main(["version", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "cremind-connect" and data["version"] == __version__ and data["frozen"] is False


def test_status_without_a_service_is_read_only(capsys: pytest.CaptureFixture[str], connect_home: Path) -> None:
    assert main.main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["service"]["running"] is False and report["installed"] is None
    assert {"startup", "url_handler", "version", "data_dir"} <= set(report)
    assert not connect_home.exists()  # status created nothing
    assert main.main(["status"]) == 0
    assert "Service: not running" in capsys.readouterr().out


def test_status_with_a_service(capsys: pytest.CaptureFixture[str], paths: ConnectPaths) -> None:
    def handler(request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "version": "9.9.9", "pid": 1, "workers": [
            {"worker_id": "w1", "state": "running", "gateway_device_id": "ab" * 16, "profile": "Anna",
             "server": "https://c.example"}],
            "ports": [{"device": "COM7", "identity": {"role": "gateway", "device_id": "ab" * 16},
                       "held_by": "w1"}]}

    server = ipc.IpcServer(paths, handler)
    server.start()
    try:
        assert main.main(["status"]) == 0
    finally:
        server.stop()
    out = capsys.readouterr().out
    assert "Service: running (version 9.9.9" in out and "w1: running — gateway …ABAB" in out
    assert "port COM7: gateway …ABAB, used by w1" in out


def test_worker_without_the_worker_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                          capsys: pytest.CaptureFixture[str], connect_home: Path) -> None:
    monkeypatch.setitem(sys.modules, "cremind_tag.connect.worker", None)
    assert main.main(["worker", "--dir", str(tmp_path / "w1"), "--port", "COM7"]) == main.EXIT_MISSING
    assert "no worker" in capsys.readouterr().err


def test_worker_runs_the_worker_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, connect_home: Path) -> None:
    import types

    calls: list[tuple[Any, ...]] = []
    fake = types.ModuleType("cremind_tag.connect.worker")
    fake.run_worker = lambda directory, port, *, paths: calls.append((directory, port, paths)) or 7  # type: ignore
    monkeypatch.setitem(sys.modules, "cremind_tag.connect.worker", fake)
    assert main.main(["worker", "--dir", str(tmp_path / "w1"), "--port", "COM7"]) == 7
    ((directory, port, paths),) = calls
    assert directory == tmp_path / "w1" and port == "COM7" and paths.overridden


def test_window_without_the_setup_flow(monkeypatch: pytest.MonkeyPatch, messages: list[tuple[str, str]],
                                       connect_home: Path) -> None:
    monkeypatch.setitem(sys.modules, "cremind_tag.connect.setup_flow", None)
    monkeypatch.setenv(main.LINK_ENV, LINK)
    assert main.main(["window"]) == main.EXIT_MISSING
    assert messages and "incomplete" in messages[0][0]


def test_window_takes_the_link_from_the_environment(monkeypatch: pytest.MonkeyPatch, connect_home: Path) -> None:
    import os
    import types

    seen: list[str] = []
    fake = types.ModuleType("cremind_tag.connect.setup_flow")
    fake.run_setup = lambda url: seen.append(url) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cremind_tag.connect.setup_flow", fake)
    monkeypatch.setenv(main.LINK_ENV, LINK)
    assert main.main(["window"]) == 0
    assert seen == [LINK] and main.LINK_ENV not in os.environ  # not passed on to anything the flow starts


def test_window_refuses_bad_links(messages: list[tuple[str, str]], connect_home: Path) -> None:
    assert main.main(["window", "--link", "cremind-connect://setup?v=7"]) == main.EXIT_USAGE
    assert messages[0][0] == "This link cannot be used"


def test_open_refuses_a_bad_link(messages: list[tuple[str, str]], connect_home: Path) -> None:
    assert main.main(["open", "cremind-connect://setup?v=1&server=ftp%3A%2F%2Fx"]) == main.EXIT_USAGE
    assert messages and "cannot be used" in messages[0][0]


def test_open_hands_the_link_to_the_service(monkeypatch: pytest.MonkeyPatch, paths: ConnectPaths) -> None:
    received: list[dict[str, Any]] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        received.append(request)
        return {"ok": True, "version": "9.9.9"} if request["op"] == "ping" else {"ok": True, "pid": 5}

    monkeypatch.setattr(main, "_spawn_window", lambda *a: pytest.fail("the service should open the window"))
    server = ipc.IpcServer(paths, handler)
    server.start()
    try:
        assert main.main([LINK]) == 0
    finally:
        server.stop()
    (open_link,) = [r for r in received if r["op"] == "open_link"]
    assert open_link["url"] == LINK and isinstance(open_link["env"], dict)


def test_open_falls_back_to_a_window_when_no_service_starts(monkeypatch: pytest.MonkeyPatch,
                                                            connect_home: Path) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(main, "ensure_service", lambda paths, timeout=0: False)
    monkeypatch.setattr(main, "_spawn_window", lambda paths, url: spawned.append(url))
    assert main.main(["open", LINK]) == 0
    assert spawned == [LINK]


def test_install_needs_a_bundle_from_source(capsys: pytest.CaptureFixture[str], connect_home: Path) -> None:
    assert main.main(["install"]) == main.EXIT_USAGE
    assert "--from" in capsys.readouterr().out


def test_install_and_uninstall_with_fake_os_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str], paths: ConnectPaths) -> None:
    from cremind_tag.connect import install

    registered: list[list[str]] = []

    class Hooks:
        def __init__(self, _paths: ConnectPaths) -> None:
            self.running: str | None = None

        def register(self, command: list[str]) -> list[str]:
            registered.append(command)
            return []

        def unregister(self) -> list[str]:
            registered.append([])
            return []

        def stop_service(self, timeout: float) -> bool:
            return True

        def start_service(self, command: list[str]) -> None:
            pass

        def service_version(self) -> str | None:
            return "0.7.0"

    monkeypatch.setattr(install, "SystemHooks", Hooks)
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "connect.json").write_text(json.dumps({"version": "0.7.0", "exe": "cremind-connect.exe"}),
                                         encoding="utf-8")
    (bundle / "cremind-connect.exe").write_bytes(b"x")
    assert main.main(["install", "--from", str(bundle), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["action"] == "installed" and result["version"] == "0.7.0"
    assert registered == [[str(paths.current / "cremind-connect.exe")]]
    assert main.main(["uninstall"]) == 0
    assert registered[-1] == [] and not paths.current.exists()
