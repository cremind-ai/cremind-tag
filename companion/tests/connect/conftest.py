"""Shared fixtures for Cremind Connect tests: every test runs under its own ``CREMIND_CONNECT_HOME``.

Nothing here registers anything with this machine: plans are asserted as data,
and the OS side of installs is a fake (tests/connect/test_install.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.connect.paths import HOME_ENV, ConnectPaths, default_paths


@pytest.fixture
def connect_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "connect-home"
    monkeypatch.setenv(HOME_ENV, str(home))
    return home


@pytest.fixture
def paths(connect_home: Path) -> ConnectPaths:
    return default_paths().ensure()


class LoopThread:
    """An asyncio loop in a daemon thread, for simulated devices the synchronous code under test talks to."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="test loop", daemon=True)
        self._thread.start()

    def run[T](self, coro: Coroutine[Any, Any, T], timeout: float = 10.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call[T](self, fn: Callable[[], T]) -> T:
        async def wrapper() -> T:
            return fn()

        return self.run(wrapper())

    def stop(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5.0)


@pytest.fixture
def loop_thread() -> Iterator[LoopThread]:
    thread = LoopThread()
    try:
        yield thread
    finally:
        thread.stop()
