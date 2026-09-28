"""One Cremind Connect service per OS user.

The service takes an exclusive, non-blocking lock on ``<runtime>/service.lock``
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere) and keeps it for its
whole life; the OS drops it when the process ends, however it ends, so a crash
never leaves a stale lock. ``service.json`` beside it says who holds the lock
(pid, version, executable, start time) for ``status`` and ``install``. File
descriptors are not inheritable (PEP 446), so workers never keep the lock alive.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .runtime import pid_alive


class InstanceLock:
    """The per-user service lock (see the module docstring)."""

    def __init__(self, lock_path: Path, info_path: Path | None = None) -> None:
        self.lock_path = Path(lock_path)
        self.info_path = Path(info_path) if info_path else self.lock_path.with_suffix(".json")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock now; ``False`` when another process holds it."""
        if self._fd is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        return True

    def write_info(self, **info: Any) -> None:
        """Record who holds the lock (only while holding it)."""
        if self._fd is None:
            raise RuntimeError("the instance lock is not held")
        data = {"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **info}
        tmp = self.info_path.with_name(f".{self.info_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.info_path)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            if read_info(self.info_path).get("pid") == os.getpid():
                self.info_path.unlink()
        _unlock(fd)
        os.close(fd)

    def __enter__(self) -> InstanceLock:
        if not self.acquire():
            raise RuntimeError(f"{self.lock_path} is held by another process")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":  # a literal check, so type checkers pick the right branch
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def is_locked(lock_path: Path) -> bool:
    """Whether some process holds the lock right now (probes by taking and dropping it)."""
    if not Path(lock_path).exists():
        return False
    probe = InstanceLock(lock_path)
    if probe.acquire():
        _unlock(probe._fd)  # type: ignore[arg-type]
        os.close(probe._fd)  # type: ignore[arg-type]
        probe._fd = None
        return False
    return True


def read_info(info_path: Path) -> dict[str, Any]:
    """``service.json`` (``{}`` when missing or unreadable)."""
    try:
        data = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def running_service(lock_path: Path, info_path: Path) -> dict[str, Any] | None:
    """The holder's ``service.json`` when a live process holds the lock, else ``None``."""
    if not is_locked(lock_path):
        return None
    info = read_info(info_path)
    pid = info.get("pid")
    if isinstance(pid, int) and not pid_alive(pid):
        info = {}
    return info or {"pid": None}


__all__ = ["InstanceLock", "is_locked", "read_info", "running_service"]
