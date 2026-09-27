"""Owner-only files for secrets (tag secrets, connector credentials, UICR images) and a cross-process lock.

POSIX: mode ``0600`` does the job. Windows ignores the mode bits (``chmod``
only toggles read-only) and a new file inherits its directory's ACL — under
``C:\\`` that grants ``BUILTIN\\Users`` read and ``Authenticated Users`` modify.
:func:`restrict_to_owner` therefore gives the file a *protected* DACL (no
inherited entries) that grants full access to the current user and SYSTEM
only, through the Win32 security API (``ctypes``, no extra dependency).
:func:`access_problem` reports a file other local users can read, for
``cremind-tag doctor``.

:class:`InterProcessLock` serialises read-modify-write cycles on a shared file
between processes (``msvcrt.locking`` / ``fcntl.flock`` on a lock file).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
# Trusted besides the user: SYSTEM, Administrators (they can take ownership of any file anyway) and the
# owner-rights / creator-owner aliases.
_TRUSTED_ALIASES = frozenset({"SY", "BA", "OW", "CO"})
_ACE = re.compile(r"\(([A-Z]+);[^;]*;([^;]*);[^;]*;[^;]*;([^)]*)\)")


class PrivateFileError(OSError):
    """The file's access control could not be set or read."""


def _win() -> tuple[object, object]:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    advapi32.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    return advapi32, kernel32


def _winerror(what: str) -> PrivateFileError:
    import ctypes

    code = ctypes.get_last_error()  # type: ignore[attr-defined]
    return PrivateFileError(code, f"{what}: {ctypes.FormatError(code)}")  # type: ignore[attr-defined]


def current_user_sid() -> str:
    """The current process user's SID (``S-1-5-21-…``), Windows only."""
    import ctypes
    from ctypes import wintypes

    advapi32, kernel32 = _win()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):  # type: ignore[attr-defined]
        raise _winerror("OpenProcessToken")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))  # type: ignore[attr-defined]
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):  # type: ignore[attr-defined]
            raise _winerror("GetTokenInformation")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):  # type: ignore[attr-defined]
            raise _winerror("ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))  # type: ignore[attr-defined]
    finally:
        kernel32.CloseHandle(token)  # type: ignore[attr-defined]


def restrict_to_owner(path: Path | str) -> None:
    """Only the current user (and SYSTEM) may access ``path`` (Windows: a protected DACL; POSIX: 0600)."""
    path = Path(path)
    if not IS_WINDOWS:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return
    import ctypes

    advapi32, kernel32 = _win()
    sddl = f"D:P(A;;FA;;;{current_user_sid()})(A;;FA;;;SY)"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(  # type: ignore[attr-defined]
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        if not advapi32.SetFileSecurityW(  # type: ignore[attr-defined]
                str(path), _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION, descriptor):
            raise _winerror(f"SetFileSecurityW({path})")
    finally:
        kernel32.LocalFree(descriptor)  # type: ignore[attr-defined]


def dacl_sddl(path: Path | str) -> str:
    """The file's DACL in SDDL form (Windows only)."""
    import ctypes
    from ctypes import wintypes

    advapi32, kernel32 = _win()
    needed = wintypes.DWORD()
    advapi32.GetFileSecurityW(str(path), _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed))  # type: ignore[attr-defined]
    if not needed.value:
        raise _winerror(f"GetFileSecurityW({path})")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(str(path), _DACL_SECURITY_INFORMATION, buffer, needed,  # type: ignore[attr-defined]
                                     ctypes.byref(needed)):
        raise _winerror(f"GetFileSecurityW({path})")
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(  # type: ignore[attr-defined]
            buffer, _SDDL_REVISION_1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None):
        raise _winerror("ConvertSecurityDescriptorToStringSecurityDescriptorW")
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))  # type: ignore[attr-defined]


def access_problem(path: Path | str) -> str | None:
    """Why other local users could read or change ``path`` (``None`` when only its owner can)."""
    path = Path(path)
    if not path.exists():
        return None
    if not IS_WINDOWS:
        mode = path.stat().st_mode
        return f"mode {stat.S_IMODE(mode):04o} lets other users read it" if mode & 0o077 else None
    try:
        sddl = dacl_sddl(path)
        me = current_user_sid()
    except OSError as exc:
        return f"its access control could not be read: {exc}"
    if sddl.startswith("D:NO_ACCESS_CONTROL") or sddl == "D:":
        return "it has no access control list (everyone can access it)"
    others = sorted({sid for kind, _rights, sid in _ACE.findall(sddl)
                     if kind in ("A", "OA") and sid not in _TRUSTED_ALIASES and sid != me})
    return f"its ACL also grants access to {', '.join(others)}" if others else None


def protection_label() -> str:
    return "owner-only ACL" if IS_WINDOWS else "mode 0600"


class InterProcessLock:
    """An exclusive lock on ``<path>`` shared by every process (a lock file; its content is irrelevant)."""

    def __init__(self, path: Path | str, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = timeout

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            self._acquire(fd)
            try:
                yield
            finally:
                self._release(fd)
        finally:
            os.close(fd)

    def _acquire(self, fd: int) -> None:
        deadline = time.monotonic() + self.timeout
        if sys.platform == "win32":  # a literal check, so type checkers pick the right branch
            import msvcrt

            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{self.path} is locked by another process") from None
                    time.sleep(0.02)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{self.path} is locked by another process") from None
                    time.sleep(0.02)

    @staticmethod
    def _release(fd: int) -> None:
        if sys.platform == "win32":  # a literal check, so type checkers pick the right branch
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            with contextlib.suppress(OSError):
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


def replace_with_retry(source: Path | str, target: Path | str, attempts: int = 20) -> None:
    """``os.replace``, retried briefly on Windows sharing violations (an antivirus scanning the target)."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.05)


__all__ = ["IS_WINDOWS", "InterProcessLock", "PrivateFileError", "access_problem", "current_user_sid",
           "dacl_sddl", "protection_label", "replace_with_retry", "restrict_to_owner"]
