"""One service per user: the instance lock."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from cremind_tag.connect.instance import InstanceLock, is_locked, read_info, running_service

HOLDER = textwrap.dedent("""
    import sys, time
    from cremind_tag.connect.instance import InstanceLock
    lock = InstanceLock(sys.argv[1])
    if lock.acquire():
        lock.write_info(version="test")
        print("held", flush=True)
        sys.stdin.readline()
    else:
        print("busy", flush=True)
""")


def test_exclusive_within_and_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "run" / "service.lock"
    first = InstanceLock(lock_path)
    assert first.acquire() and first.held
    assert not InstanceLock(lock_path).acquire()
    assert is_locked(lock_path)
    child = subprocess.run([sys.executable, "-c", HOLDER, str(lock_path)], capture_output=True, text=True,
                           timeout=60, input="")
    assert child.stdout.strip() == "busy"
    first.release()
    assert not is_locked(lock_path)
    assert InstanceLock(lock_path).acquire()


def test_released_when_the_holder_dies(tmp_path: Path) -> None:
    lock_path = tmp_path / "service.lock"
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        assert is_locked(lock_path)
        info = running_service(lock_path, lock_path.with_suffix(".json"))
        # (a venv's python.exe on Windows may be a launcher: the holder's pid is its child's)
        assert info is not None and isinstance(info["pid"], int) and info["version"] == "test"
        child.kill()
        child.wait(30)
    finally:
        if child.poll() is None:
            child.kill()
    deadline = time.monotonic() + 15
    while is_locked(lock_path) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not is_locked(lock_path)
    assert running_service(lock_path, lock_path.with_suffix(".json")) is None


def test_info_is_written_and_removed(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "service.lock", tmp_path / "service.json")
    with lock:
        lock.write_info(version="1.2.3", exe="x")
        info = read_info(tmp_path / "service.json")
        assert info["pid"] == os.getpid() and info["version"] == "1.2.3" and info["started_at"].endswith("Z")
    assert not (tmp_path / "service.json").exists()
    assert read_info(tmp_path / "missing.json") == {}
