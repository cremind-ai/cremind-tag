"""Local IPC over the real named pipe (Windows) or Unix socket (elsewhere)."""

from __future__ import annotations

import os
import sys
import threading
import time
from multiprocessing.connection import Client
from typing import Any

import pytest

from cremind_tag.connect import ipc
from cremind_tag.connect.paths import ConnectPaths, default_paths
from cremind_tag.private_files import access_problem


def echo(request: dict[str, Any]) -> dict[str, Any]:
    if request["op"] == "boom":
        raise RuntimeError("kaput")
    if request["op"] == "slow":
        time.sleep(float(request.get("s", 1)))
    return {"ok": True, "echo": request}


@pytest.fixture
def server(paths: ConnectPaths) -> ipc.IpcServer:
    srv = ipc.IpcServer(paths, echo)
    srv.start()
    yield srv
    srv.stop()


def test_round_trip(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    answer = ipc.request(paths, "hello", value=[1, "two", {"three": 3}])
    assert answer == {"ok": True, "echo": {"op": "hello", "value": [1, "two", {"three": 3}]}}
    assert ipc.ping(paths) is not None


def test_several_requests_on_one_connection(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    with ipc.connect(paths) as conn:
        for n in range(5):
            assert ipc.call(conn, "n", n=n)["echo"]["n"] == n


def test_address_is_a_private_pipe_or_socket(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    address, family = ipc.address(paths)
    if sys.platform == "win32":
        assert family == "AF_PIPE" and address.startswith(r"\\.\pipe\cremind-connect-")
        assert len(address.rsplit("-", 1)[1]) == 16
    else:
        assert family == "AF_UNIX" and os.stat(os.path.dirname(address)).st_mode & 0o077 == 0
    assert access_problem(paths.ipc_key) is None
    assert len(ipc.load_authkey(paths) or b"") == ipc.AUTHKEY_LEN


def test_the_override_home_gets_its_own_pipe(tmp_path: Any) -> None:
    a = default_paths({"CREMIND_CONNECT_HOME": str(tmp_path / "a")})
    b = default_paths({"CREMIND_CONNECT_HOME": str(tmp_path / "b")})
    assert ipc.address(a) != ipc.address(b)


def test_errors_are_answers(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    answer = ipc.request(paths, "boom")
    assert answer["ok"] is False and answer["error"] == "internal" and "kaput" in answer["message"]


def test_a_wrong_key_is_refused(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    paths.ipc_key.write_text("11" * 32 + "\n", encoding="ascii")
    with pytest.raises(ipc.IpcAuthError):
        ipc.request(paths, "hello")


def test_a_silent_client_does_not_block_others(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    address, family = ipc.address(paths)
    lurker = Client(address, family)  # connects, never answers the challenge
    try:
        assert ipc.request(paths, "hello", timeout=5.0)["ok"]
    finally:
        lurker.close()


def test_timeouts(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    with pytest.raises(ipc.IpcTimeout):
        ipc.request(paths, "slow", s=2, timeout=0.3)


def test_no_service(paths: ConnectPaths) -> None:
    with pytest.raises(ipc.IpcUnavailable):
        ipc.request(paths, "ping")  # no ipc.key yet
    ipc.ensure_authkey(paths)
    with pytest.raises(ipc.IpcUnavailable):
        ipc.request(paths, "ping")  # a key, but nobody listens
    assert ipc.ping(paths) is None


def test_stop_releases_the_address(paths: ConnectPaths) -> None:
    for _ in range(2):  # the second server can bind the same name
        srv = ipc.IpcServer(paths, echo)
        srv.start()
        assert ipc.request(paths, "x")["ok"]
        srv.stop()
    with pytest.raises(ipc.IpcUnavailable):
        ipc.request(paths, "x")


def test_concurrent_clients(paths: ConnectPaths, server: ipc.IpcServer) -> None:
    results: list[bool] = []

    def one(n: int) -> None:
        results.append(ipc.request(paths, "n", n=n)["echo"]["n"] == n)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [True] * 8
