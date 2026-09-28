"""Where Cremind Connect keeps its data and its program files (docs/connect-setup.md §11.2).

=========  ==============================================  ==============================================
OS         Data (``data_dir``)                             Program (``app_root``, managed copies)
=========  ==============================================  ==============================================
Windows    ``%LOCALAPPDATA%\\Cremind\\Connect``              ``%LOCALAPPDATA%\\Programs\\Cremind Connect``
macOS      ``~/Library/Application Support/Cremind Connect``  ``<data>/app``, shown as
                                                           ``~/Applications/Cremind Connect.app``
Linux      ``~/.local/share/cremind-connect``               ``~/.local/lib/cremind-connect``
=========  ==============================================  ==============================================

A *managed* copy (installed by ``cremind-connect install``, e.g. from the Windows
installer or from the copy bundled with Cremind desktop) lives in
``<app_root>/versions/<version>/`` and runs through the ``current`` link
(:attr:`ConnectPaths.current`: ``<app_root>/current`` on Windows and Linux; on
macOS the ``~/Applications/Cremind Connect.app`` symlink, so the app keeps one
path in Finder and in its LaunchAgent). Copies placed by an OS package — the
``.deb`` in ``/opt/cremind-connect``, the ``.dmg`` dragged to
``/Applications/Cremind Connect.app`` — are *external*: registered where they
are (docs/connect-packaging.md).

Inside ``data_dir``: ``installation.json``/``installation.key`` (owner-only),
``ipc.key``, ``install.json`` (which copy is active), ``logs/``, ``assets/``
(verified font assets) and ``workers/<worker_id>/``. ``runtime_dir`` (a ``0700``
directory: ``$XDG_RUNTIME_DIR/cremind-connect`` on Linux, else ``<data>/run``)
holds the instance lock and, on POSIX, the IPC socket.

``CREMIND_CONNECT_HOME`` moves everything under one directory (tests, side-by-side
development): ``<home>/data/…``, ``<home>/app``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .runtime import APP_BUNDLE_NAME, os_kind

HOME_ENV = "CREMIND_CONNECT_HOME"
SOCKET_NAME = "cremind-connect.sock"
# sockaddr_un.sun_path is 104 bytes on macOS, 108 on Linux; keep a margin.
_MAX_SOCKET_PATH = 100
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class PathsError(OSError):
    """A directory Cremind Connect needs is unsafe or unusable."""


@dataclass(frozen=True)
class ConnectPaths:
    """Every location Cremind Connect uses for one OS user (see the module docstring)."""

    data_dir: Path
    logs_dir: Path
    workers_dir: Path
    assets_dir: Path
    runtime_dir: Path
    app_root: Path
    kind: str = "windows"
    """OS these paths are for: ``windows``, ``macos`` or ``linux``."""
    current_link: Path | None = None
    """The link to the active managed version (default ``<app_root>/current``)."""
    overridden: bool = False
    """``CREMIND_CONNECT_HOME`` is in effect."""

    # -- program files -------------------------------------------------------------

    @property
    def versions_dir(self) -> Path:
        return self.app_root / "versions"

    @property
    def current(self) -> Path:
        return self.current_link or self.app_root / "current"

    # -- data files --------------------------------------------------------------------

    @property
    def installation_json(self) -> Path:
        return self.data_dir / "installation.json"

    @property
    def installation_key(self) -> Path:
        return self.data_dir / "installation.key"

    @property
    def ipc_key(self) -> Path:
        return self.data_dir / "ipc.key"

    @property
    def install_record(self) -> Path:
        """``install.json``: the active copy (version, executable, managed or external)."""
        return self.data_dir / "install.json"

    @property
    def service_lock(self) -> Path:
        return self.runtime_dir / "service.lock"

    @property
    def service_info(self) -> Path:
        return self.runtime_dir / "service.json"

    @property
    def socket_path(self) -> Path:
        """The service's Unix socket (POSIX). A short private directory in ``/tmp`` when the natural path is too long."""
        path = self.runtime_dir / SOCKET_NAME
        if len(os.fsencode(str(path))) <= _MAX_SOCKET_PATH:
            return path
        uid = os.getuid() if hasattr(os, "getuid") else 0
        digest = hashlib.sha256(os.fsencode(str(self.runtime_dir))).hexdigest()[:8]
        return Path("/tmp") / f"cremind-connect-{uid}-{digest}" / SOCKET_NAME

    def worker_dir(self, worker_id: str) -> Path:
        """``workers/<worker_id>`` (the id must be a plain path component)."""
        if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError(f"invalid worker id {worker_id!r}")
        return self.workers_dir / worker_id

    def ensure(self) -> ConnectPaths:
        """Create the directories (the runtime directory private: ``0700``, owned by this user)."""
        for directory in (self.data_dir, self.logs_dir, self.workers_dir, self.assets_dir):
            directory.mkdir(parents=True, exist_ok=True)
        ensure_private_dir(self.runtime_dir)
        if self.kind != "windows" and self.socket_path.parent != self.runtime_dir:
            ensure_private_dir(self.socket_path.parent)
        return self


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (mode ``0700``) or check an existing one is ours and private; fix a loose mode."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform == "win32":
        return path  # %LOCALAPPDATA% is private to the user by its inherited ACL
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PathsError(f"{path} is not a plain directory")
    if st.st_uid != os.getuid():
        raise PathsError(f"{path} belongs to another user (uid {st.st_uid})")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def default_paths(env: Mapping[str, str] | None = None, platform: str | None = None) -> ConnectPaths:
    """The paths for this user (``env`` and ``platform`` are for tests)."""
    env = os.environ if env is None else env
    kind = os_kind(platform)
    override = env.get(HOME_ENV)
    if override:
        root = Path(override)
        data = root / "data"
        app_root = root / "app"
        current = root / "Applications" / APP_BUNDLE_NAME if kind == "macos" else app_root / "current"
        return ConnectPaths(data, data / "logs", data / "workers", data / "assets", data / "run", app_root, kind,
                            current, True)
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if kind == "windows":
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        data = local / "Cremind" / "Connect"
        app_root = local / "Programs" / "Cremind Connect"
        return ConnectPaths(data, data / "logs", data / "workers", data / "assets", data / "run", app_root, kind,
                            app_root / "current")
    if kind == "macos":
        data = home / "Library" / "Application Support" / "Cremind Connect"
        return ConnectPaths(data, data / "logs", data / "workers", data / "assets", data / "run", data / "app", kind,
                            home / "Applications" / APP_BUNDLE_NAME)
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "cremind-connect"
    xdg_runtime = env.get("XDG_RUNTIME_DIR")
    runtime = Path(xdg_runtime) / "cremind-connect" if xdg_runtime else data / "run"
    app_root = home / ".local" / "lib" / "cremind-connect"
    return ConnectPaths(data, data / "logs", data / "workers", data / "assets", runtime, app_root, kind,
                        app_root / "current")


__all__ = ["HOME_ENV", "SOCKET_NAME", "ConnectPaths", "PathsError", "default_paths", "ensure_private_dir"]
