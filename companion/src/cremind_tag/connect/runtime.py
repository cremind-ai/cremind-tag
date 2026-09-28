"""Facts about the running Cremind Connect program: version, frozen bundle, how to start itself.

A PyInstaller bundle carries ``connect.json`` (written by
``companion/packaging/build_connect.py``) in its resource directory
(``sys._MEIPASS``: ``_internal/`` of a one-directory bundle, ``Contents/Resources``
of the macOS app) and, for people and installers, next to the executable::

    {"name": "cremind-connect", "version": "0.1.0", "exe": "cremind-connect.exe",
     "platform": "windows", "arch": "x64", "built_at": "..."}

Run from a source checkout, the version is the companion's ``__version__``.
:func:`self_command` gives the argv that starts another role of *this* program
(``[exe, "service"]`` frozen, ``[python, "-m", "cremind_tag.connect", "service"]``
from source), so the service, its workers and the setup window always run the
same code.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

EXE_BASENAME = "cremind-connect"
BUNDLE_INFO_FILE = "connect.json"
APP_BUNDLE_NAME = "Cremind Connect.app"
MACOS_EXE_IN_APP = Path("Contents") / "MacOS" / EXE_BASENAME

# Windows process creation flags (subprocess exposes most of them only on Windows).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def os_kind(platform: str | None = None) -> str:
    """``windows``, ``macos`` or ``linux`` (every other POSIX system counts as ``linux``)."""
    platform = platform or sys.platform
    if platform == "win32":
        return "windows"
    if platform == "darwin":
        return "macos"
    return "linux"


def exe_name(kind: str | None = None) -> str:
    """The executable's file name on ``kind`` (default: this OS)."""
    return f"{EXE_BASENAME}.exe" if (kind or os_kind()) == "windows" else EXE_BASENAME


def is_frozen() -> bool:
    """Running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def resources_dir() -> Path | None:
    """The bundle's resource directory (``sys._MEIPASS``), ``None`` from source."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if is_frozen() and base else None


def executable() -> Path:
    """The running executable (the bundle's ``cremind-connect``, or the Python interpreter)."""
    return Path(sys.executable).resolve()


@functools.cache
def bundle_info() -> dict[str, Any]:
    """``connect.json`` of the running bundle (``{}`` from source or when missing/corrupt)."""
    candidates: list[Path] = []
    if (res := resources_dir()) is not None:
        candidates.append(res / BUNDLE_INFO_FILE)
    if is_frozen():
        candidates.append(executable().parent / BUNDLE_INFO_FILE)
    for path in candidates:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    return {}


def connect_version() -> str:
    """This program's version: the bundle's ``connect.json``, else the companion's ``__version__``."""
    version = bundle_info().get("version")
    if isinstance(version, str) and version:
        return version
    from cremind_tag import __version__

    return __version__


def self_command(*args: str) -> list[str]:
    """argv that runs ``cremind-connect <args>`` with this very program."""
    if is_frozen():
        return [str(executable()), *args]
    return [sys.executable, "-m", "cremind_tag.connect", *args]


# ---------------------------------------------------------------------------
# Versions (a PEP 440 subset; semver pre-releases such as 0.2.0-rc.1 are accepted too)
# ---------------------------------------------------------------------------

_VERSION = re.compile(
    r"""^v?(?P<release>\d+(?:\.\d+)*)
        (?:[-_.]?(?P<pre>a|alpha|b|beta|c|rc)[-_.]?(?P<pre_n>\d+)?)?
        (?:[-_.]?post[-_.]?(?P<post>\d+))?
        (?P<dev_seg>[-_.]?dev[-_.]?(?P<dev>\d+)?)?
        (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?$""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}
_INF = 1 << 62


def version_key(text: str) -> tuple[Any, ...]:
    """Sort key: ``1.0.dev1 < 1.0a1 < 1.0rc1 < 1.0 < 1.0.post1``; a ``+local`` part is ignored."""
    match = _VERSION.match(text.strip())
    if match is None:
        raise ValueError(f"not a version: {text!r}")
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre, post, dev = match["pre"], match["post"], match["dev"]
    has_dev = match["dev_seg"] is not None
    if pre is None and post is None and has_dev:
        pre_key: tuple[int, int] = (-1, 0)  # 1.0.dev1 sorts before 1.0a1
    elif pre is None:
        pre_key = (3, 0)
    else:
        pre_key = (_PRE_RANK[pre.lower()], int(match["pre_n"] or 0))
    post_key = int(post) if post is not None else -1
    dev_key = int(dev or 0) if has_dev else _INF
    return (tuple(release), pre_key, post_key, dev_key)


def compare_versions(a: str, b: str) -> int:
    """-1, 0 or 1 as ``a`` is older than, equal to or newer than ``b``."""
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def is_newer(a: str, b: str) -> bool:
    """``a`` is a strictly newer version than ``b`` (unparseable versions are never newer)."""
    try:
        return compare_versions(a, b) > 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Starting processes that outlive their parent
# ---------------------------------------------------------------------------


def spawn_detached(argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None,
                   log_path: Path | None = None) -> subprocess.Popen[bytes]:
    """Start ``argv`` in its own session/process group, detached from this console.

    Output goes to ``log_path`` (appended) or nowhere. On Windows the child
    leaves the parent's job object when the job allows it (a browser may start
    URL handlers inside one), so it survives the handler's exit.
    """
    out: IO[bytes] | int = subprocess.DEVNULL
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed below
    try:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": out, "close_fds": True,
                                  "env": env, "cwd": str(cwd) if cwd else None}
        if sys.platform == "win32":
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:  # ERROR_ACCESS_DENIED: the job forbids breakaway
                    raise
            return subprocess.Popen(argv, creationflags=flags, **kwargs)
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    finally:
        if not isinstance(out, int):
            out.close()


def no_window_flags() -> int:
    """``creationflags`` for a background child on Windows: its own process group, no console window."""
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists (best effort)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # exists, but belongs to someone else
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = ["APP_BUNDLE_NAME", "BUNDLE_INFO_FILE", "EXE_BASENAME", "MACOS_EXE_IN_APP", "bundle_info",
           "compare_versions", "connect_version", "executable", "exe_name", "is_frozen", "is_newer",
           "no_window_flags", "os_kind", "pid_alive", "resources_dir", "self_command", "spawn_detached",
           "version_key"]
