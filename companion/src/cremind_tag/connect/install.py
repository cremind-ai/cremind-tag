"""Install, upgrade, roll back and uninstall Cremind Connect for this OS user (docs/connect-packaging.md).

**Managed copies** live in ``<app_root>/versions/<version>/`` and run through the
``current`` link (a directory junction on Windows, a symlink elsewhere; on macOS
the link is ``~/Applications/Cremind Connect.app``). The OS registration
(:mod:`.startup`, :mod:`.urlhandler`) always points at ``<current>/<exe>``, so
switching versions never re-registers anything. An install or upgrade:

1. copies the bundle to ``versions/<version>`` (a hidden staging directory,
   renamed into place when complete);
2. asks the running service to stop (IPC ``stop``; the instance lock tells when
   it is gone);
3. switches ``current`` (POSIX: a new symlink renamed over the old one — atomic;
   Windows: junctions cannot be renamed over each other, so the old one is
   removed and the new one renamed in, while the service is stopped);
4. registers startup and the URL handler (Linux, frozen: the udev rule too), and
   starts the service;
5. waits up to 30 s for the service to answer ``ping`` with the new version;
6. on failure: stops it, switches ``current`` back, starts the previous
   version (a fresh install is unregistered and removed instead);
7. records the active copy in ``<data>/install.json`` and deletes every version
   except the new and the previous one.

**External copies** (the ``.deb`` in ``/opt/cremind-connect``, the ``.dmg`` copy
in ``/Applications``, a development run) are registered where they are
(``install --register-only``); an OS installer that unpacks into
``versions/<version>`` (the Windows installer) also uses ``--register-only``,
which then switches ``current`` to that version.

**Convergence**: the Cremind desktop app bundles Connect and runs ``install``
from its copy; the standalone installer does the same. Whichever is newer wins —
an older bundle never replaces a newer installed copy (``install.json``, the
managed ``current`` and the known external locations are all compared), and an
older service steps aside when a newer copy is installed (:mod:`.service`).

Everything touching the OS goes through :class:`Hooks`, so tests run the real
file operations with fake registration and a fake service.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Protocol

from .paths import ConnectPaths
from .runtime import (
    APP_BUNDLE_NAME,
    BUNDLE_INFO_FILE,
    connect_version,
    executable,
    exe_name,
    is_frozen,
    is_newer,
    resources_dir,
    self_command,
    version_key,
)

log = logging.getLogger(__name__)

RECORD_SCHEMA = "cremind-connect/install@1"
HEALTH_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 20.0
EXTERNAL_LOCATIONS = {
    "linux": (Path("/opt/cremind-connect"),),
    "macos": (Path("/Applications") / APP_BUNDLE_NAME,),
    "windows": (),
}


class InstallError(RuntimeError):
    """Installing, registering or uninstalling failed (the message says what to do)."""


# ---------------------------------------------------------------------------
# Bundles and the install record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bundle:
    """A built Cremind Connect program: a one-directory bundle, or a macOS ``.app``."""

    source: Path
    """What gets copied: the bundle directory, or the ``.app``."""
    version: str
    exe: PurePath
    """The executable, relative to ``source``."""
    app_name: str | None = None
    """``Cremind Connect.app`` when ``source`` is an app bundle."""
    info: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def executable(self) -> Path:
        return self.source / self.exe

    @property
    def exe_in_link(self) -> PurePath:
        """The executable relative to what ``current`` points at."""
        return self.exe


def _read_info(*candidates: Path) -> dict[str, Any]:
    for path in candidates:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    return {}


def read_bundle(path: Path) -> Bundle:
    """Describe the bundle at ``path`` (its ``connect.json`` names the version and the executable)."""
    path = Path(path)
    if path.suffix != ".app" and (path / APP_BUNDLE_NAME).is_dir():
        path = path / APP_BUNDLE_NAME
    if path.suffix == ".app":
        info = _read_info(path / "Contents" / "Resources" / BUNDLE_INFO_FILE,
                          path / "Contents" / "Frameworks" / BUNDLE_INFO_FILE)
        exe = PurePath(str(info.get("exe") or PurePath("Contents") / "MacOS" / "cremind-connect"))
        app_name: str | None = path.name
    else:
        info = _read_info(path / BUNDLE_INFO_FILE, path / "_internal" / BUNDLE_INFO_FILE)
        exe = PurePath(str(info.get("exe") or exe_name()))
        app_name = None
    version = info.get("version")
    if not isinstance(version, str) or not version:
        raise InstallError(f"{path} is not a Cremind Connect bundle (no {BUNDLE_INFO_FILE} with a version)")
    try:
        version_key(version)
    except ValueError:
        raise InstallError(f"{path}: {version!r} is not a version") from None
    if exe.is_absolute() or ".." in exe.parts:
        raise InstallError(f"{path}: {BUNDLE_INFO_FILE} names an executable outside the bundle")
    if not (path / exe).is_file():
        raise InstallError(f"{path}: the executable {exe} is missing")
    return Bundle(path, version, exe, app_name, info)


def running_bundle() -> Bundle | None:
    """The bundle this program runs from (``None`` from a source checkout)."""
    if not is_frozen():
        return None
    exe = executable()
    app = next((p for p in exe.parents if p.suffix == ".app"), None)
    source = app if app is not None else exe.parent
    info = _read_info(*(p / BUNDLE_INFO_FILE for p in (resources_dir(), exe.parent) if p is not None))
    return Bundle(source, connect_version(), exe.relative_to(source), app.name if app else None, info)


def read_record(paths: ConnectPaths) -> dict[str, Any] | None:
    """``install.json`` (``None`` when missing or unreadable)."""
    try:
        data = json.loads(paths.install_record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == RECORD_SCHEMA else None


def write_record(paths: ConnectPaths, **fields: Any) -> dict[str, Any]:
    record = {"schema": RECORD_SCHEMA, **fields, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.install_record.with_name(f".install.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, paths.install_record)
    return record


@dataclass(frozen=True)
class Installed:
    """A copy of Cremind Connect found on this computer."""

    version: str
    exe: Path
    layout: str
    """``managed`` or ``external``."""


def installed_copies(paths: ConnectPaths) -> list[Installed]:
    """Every copy that could be the active one: the record, the managed ``current``, known external locations."""
    found: dict[str, Installed] = {}

    def add(item: Installed) -> None:
        with contextlib.suppress(ValueError):
            version_key(item.version)
            if item.exe.is_file():
                found.setdefault(os.path.normcase(str(item.exe)), item)

    record = read_record(paths)
    if record is not None and isinstance(record.get("version"), str) and record.get("exe"):
        add(Installed(record["version"], Path(record["exe"]), str(record.get("layout", "managed"))))
    if _is_link(paths.current):
        with contextlib.suppress(InstallError, OSError):
            bundle = read_bundle(Path(os.path.realpath(paths.current)))
            add(Installed(bundle.version, paths.current / bundle.exe, "managed"))
    for location in EXTERNAL_LOCATIONS.get(paths.kind, ()):
        if location.exists():
            with contextlib.suppress(InstallError, OSError):
                bundle = read_bundle(location)
                add(Installed(bundle.version, bundle.executable, "external"))
    return list(found.values())


def newest(copies: list[Installed]) -> Installed | None:
    return max(copies, key=lambda c: version_key(c.version), default=None)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (sys.platform == "win32" and path.is_junction())


def link_target(path: Path) -> Path | None:
    """Where the ``current`` link points (resolved), or ``None`` when it is not a link."""
    if not _is_link(path):
        return None
    return Path(os.path.realpath(path))


def switch_link(link: Path, target: Path) -> None:
    """Point ``link`` at the directory ``target`` (see the module docstring for atomicity)."""
    target = Path(target)
    if not target.is_dir():
        raise InstallError(f"{target} is not a directory")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.new-{os.getpid()}")
    _remove_link(tmp)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(tmp))
        old = link_target(link)
        if link.exists() or _is_link(link):
            if not _is_link(link):
                raise InstallError(f"{link} is a real directory, not a link; move it away first")
            os.rmdir(link)  # removes the junction only, never its target
        try:
            os.rename(tmp, link)
        except OSError:
            if old is not None and not link.exists():
                with contextlib.suppress(OSError):
                    _winapi.CreateJunction(str(old), str(link))
            _remove_link(tmp)
            raise
        return
    os.symlink(str(target), str(tmp), target_is_directory=True)
    if link.exists() and not _is_link(link):
        _remove_link(tmp)
        raise InstallError(f"{link} is a real directory, not a link; move it away first")
    os.replace(tmp, link)


def _remove_link(path: Path) -> None:
    """Remove a link (never its target) or an empty leftover directory; anything else stays."""
    with contextlib.suppress(FileNotFoundError):
        if sys.platform == "win32" and path.is_junction():
            os.rmdir(path)
        elif path.is_symlink():
            path.unlink()
        elif path.is_dir():
            with contextlib.suppress(OSError):
                os.rmdir(path)


# ---------------------------------------------------------------------------
# The OS side (injectable)
# ---------------------------------------------------------------------------


class Hooks(Protocol):
    def register(self, command: list[str]) -> list[str]:
        """Register startup + URL handler for ``command``; return warnings, raise :class:`InstallError`."""
        ...

    def unregister(self) -> list[str]: ...

    def stop_service(self, timeout: float) -> bool:
        """Stop the running service, whichever copy it is; ``True`` when none runs afterwards."""
        ...

    def start_service(self, command: list[str]) -> None: ...

    def service_version(self) -> str | None:
        """The running service's version (IPC ``ping``), ``None`` when none answers."""
        ...


class SystemHooks:
    """The real registration, service control and health check."""

    def __init__(self, paths: ConnectPaths) -> None:
        self.paths = paths

    def register(self, command: list[str]) -> list[str]:
        from . import startup, urlhandler
        from .plan import apply

        warnings: list[str] = []
        result = apply(startup.register_plan(command, self.paths))
        warnings += result.warnings
        if not result.ok:
            raise InstallError(f"could not register Cremind Connect to start at logon: {result.error}")
        url = apply(urlhandler.register_plan(command, self.paths))
        warnings += url.warnings
        if not url.ok:
            warnings.append(f"cremind-connect: links will not open Cremind Connect: {url.error}")
        if self.paths.kind == "linux" and is_frozen():
            warnings += self._udev()
        return warnings

    def _udev(self) -> list[str]:
        from . import udev
        from .plan import apply

        if udev.installed_rules() is not None:
            return []
        staging = self.paths.data_dir / "udev"
        result = apply(udev.install_plan(staging))
        if result.ok:
            return result.warnings
        return [f"the USB device rule is not installed ({result.error}); without it Cremind Connect may not be "
                f"allowed to open the gateway. Install it with: {udev.manual_command(staging)}"]

    def unregister(self) -> list[str]:
        from . import startup, urlhandler
        from .plan import apply

        warnings = []
        for plan in (startup.unregister_plan(self.paths), urlhandler.unregister_plan(self.paths)):
            result = apply(plan)
            warnings += result.warnings + ([result.error] if result.error else [])
        return warnings

    def stop_service(self, timeout: float) -> bool:
        from . import ipc, startup
        from .instance import is_locked, read_info
        from .plan import apply
        from .runtime import pid_alive

        if self.paths.kind != "windows":
            apply(startup.stop_plan(self.paths))  # through launchd/systemd, or they would restart it
        with contextlib.suppress(ipc.IpcError):
            ipc.request(self.paths, "stop", timeout=5.0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not is_locked(self.paths.service_lock):
                return True
            time.sleep(0.2)
        if self.paths.kind == "windows":
            apply(startup.stop_plan(self.paths))
        pid = read_info(self.paths.service_info).get("pid")
        if isinstance(pid, int) and pid != os.getpid() and pid_alive(pid):
            log.warning("install: the service (pid %d) did not stop; terminating it", pid)
            _terminate(pid)
            time.sleep(1.0)
        return not is_locked(self.paths.service_lock)

    def start_service(self, command: list[str]) -> None:
        from . import startup
        from .plan import apply
        from .runtime import spawn_detached

        state = startup.status(self.paths)
        if state.registered:
            result = apply(startup.start_plan(self.paths))
            if result.ok:
                return
            log.warning("install: starting through %s failed (%s); starting directly", state.kind, result.error)
        spawn_detached([*command, "service"], cwd=self.paths.data_dir,
                       log_path=self.paths.logs_dir / "service-console.log")

    def service_version(self) -> str | None:
        from . import ipc

        answer = ipc.ping(self.paths, timeout=2.0)
        return str(answer.get("version")) if answer else None


def _terminate(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=0x08000000)
        return
    import signal

    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    action: str
    """``installed``, ``upgraded``, ``reinstalled``, ``registered``, ``kept_newer``, ``rolled_back``, ``failed``."""
    version: str
    exe: Path | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.action not in ("rolled_back", "failed")

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "action": self.action, "version": self.version,
                "exe": str(self.exe) if self.exe else None, "warnings": self.warnings, "error": self.error}


class Installer:
    """See the module docstring."""

    def __init__(self, paths: ConnectPaths, hooks: Hooks | None = None, *, health_timeout: float = HEALTH_TIMEOUT_S,
                 stop_timeout: float = STOP_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.paths = paths
        self.hooks = hooks or SystemHooks(paths)
        self.health_timeout = health_timeout
        self.stop_timeout = stop_timeout
        self._sleep = sleep
        self._clock = clock

    # -- queries -----------------------------------------------------------------------

    def managed_versions(self) -> list[str]:
        try:
            return sorted((p.name for p in self.paths.versions_dir.iterdir()
                           if p.is_dir() and not p.name.startswith(".")), key=_safe_key)
        except FileNotFoundError:
            return []

    def current_version(self) -> str | None:
        return _version_name(link_target(self.paths.current))

    # -- install / upgrade -----------------------------------------------------------------

    def install(self, bundle: Bundle) -> InstallResult:
        """Install ``bundle`` as the managed copy, unless a newer copy is installed already."""
        best = newest(installed_copies(self.paths))
        if best is not None and is_newer(best.version, bundle.version):
            return self._keep(best, f"Cremind Connect {best.version} is already installed; {bundle.version} "
                                    "is older and was not installed")
        previous_target = link_target(self.paths.current) or self._adopt()
        previous_version = _version_name(previous_target)
        version_dir = self.paths.versions_dir / bundle.version
        link_to = version_dir / bundle.app_name if bundle.app_name else version_dir
        reused = self._have(version_dir, bundle)
        if (reused and previous_target is not None and _same(previous_target, link_to)
                and self.hooks.service_version() == bundle.version):
            exe = self.paths.current / bundle.exe
            warnings = self.hooks.register([str(exe)])
            self._record(bundle.version, exe, "managed", previous=_record_previous(self.paths))
            return InstallResult("reinstalled", bundle.version, exe, warnings)
        if not reused:
            self._copy(bundle, version_dir)
        action = "installed" if previous_target is None else (
            "reinstalled" if previous_version == bundle.version else "upgraded")
        return self._activate(bundle.version, link_to, bundle.exe, previous_target, previous_version, action)

    def register_only(self, bundle: Bundle | None = None, command: list[str] | None = None) -> InstallResult:
        """Register the copy that is running (or ``bundle``) where it is (see the module docstring)."""
        bundle = bundle or running_bundle()
        version = bundle.version if bundle else connect_version()
        best = newest(installed_copies(self.paths))
        exe_path = bundle.executable if bundle else None
        if (best is not None and is_newer(best.version, version)
                and (exe_path is None or not _same(best.exe, exe_path))):
            return self._keep(best, f"Cremind Connect {best.version} is installed; this copy ({version}) is older "
                                    "and was not registered")
        if bundle is not None and _inside(bundle.source, self.paths.versions_dir):
            previous_target = link_target(self.paths.current) or self._adopt()
            version_dir = _version_dir_of(bundle.source, self.paths.versions_dir)
            link_to = version_dir / bundle.app_name if bundle.app_name else version_dir
            return self._activate(version, link_to, bundle.exe, previous_target, _version_name(previous_target),
                                  "registered")
        cmd = command or ([str(bundle.executable)] if bundle else self_command())
        previous = read_record(self.paths)
        self.hooks.stop_service(self.stop_timeout)
        warnings = self.hooks.register(cmd)
        self.hooks.start_service(cmd)
        if self._healthy(version):
            self._record(version, Path(cmd[0]), "external", command=cmd)
            return InstallResult("registered", version, Path(cmd[0]), warnings)
        error = f"the service did not answer within {self.health_timeout:.0f} s; see {self.paths.logs_dir}"
        old_exe = Path(str(previous.get("exe", ""))) if previous else None
        if previous is not None and old_exe is not None and old_exe.is_file() and not _same(old_exe, Path(cmd[0])):
            self.hooks.stop_service(self.stop_timeout)
            old_cmd = [str(c) for c in previous.get("command") or [old_exe]]
            warnings += self.hooks.register(old_cmd)
            self.hooks.start_service(old_cmd)
            return InstallResult("rolled_back", str(previous.get("version", "")), old_exe, warnings, error)
        return InstallResult("failed", version, Path(cmd[0]), warnings, error)

    def _adopt(self) -> Path | None:
        """A real directory where the ``current`` link belongs (an app dragged there by hand): keep it as a version.

        Returns what ``current`` should point at for that copy (``None`` when there was nothing to adopt).
        """
        current = self.paths.current
        if not current.exists() or _is_link(current):
            return None
        try:
            name = read_bundle(current).version
        except InstallError:
            name = f".adopted-{int(time.time())}"
        version_dir = self.paths.versions_dir / name
        if version_dir.exists():
            version_dir = self.paths.versions_dir / f".adopted-{int(time.time())}"
        destination = version_dir / current.name if current.suffix == ".app" else version_dir
        destination.parent.mkdir(parents=True, exist_ok=True)
        log.info("install: keeping the copy at %s as %s", current, destination)
        shutil.move(str(current), str(destination))
        return destination

    def _keep(self, best: Installed, note: str) -> InstallResult:
        """A newer copy is installed: make sure it is the registered, running one."""
        warnings = [note]
        record = read_record(self.paths)
        if record is None or not _same(Path(str(record.get("exe", ""))), best.exe):
            warnings += self.hooks.register([str(best.exe)])
            self._record(best.version, best.exe, best.layout)
        if self.hooks.service_version() != best.version:
            self.hooks.stop_service(self.stop_timeout)
            self.hooks.start_service([str(best.exe)])
            if not self._healthy(best.version):
                warnings.append(f"Cremind Connect {best.version} did not answer within "
                                f"{self.health_timeout:.0f} s; see {self.paths.logs_dir}")
        return InstallResult("kept_newer", best.version, best.exe, warnings)

    def _have(self, version_dir: Path, bundle: Bundle) -> bool:
        """``versions/<v>`` already holds this version, complete."""
        if not version_dir.is_dir():
            return False
        try:
            existing = read_bundle(version_dir / bundle.app_name if bundle.app_name else version_dir)
        except InstallError:
            return False
        return existing.version == bundle.version and existing.exe == bundle.exe

    def _copy(self, bundle: Bundle, version_dir: Path) -> None:
        self.paths.versions_dir.mkdir(parents=True, exist_ok=True)
        staging = self.paths.versions_dir / f".partial-{bundle.version}-{os.getpid()}"
        _rmtree(staging)
        try:
            shutil.copytree(bundle.source, staging / bundle.app_name if bundle.app_name else staging, symlinks=True)
            if version_dir.exists():  # an incomplete or foreign copy of the same version
                broken = self.paths.versions_dir / f".broken-{bundle.version}-{os.getpid()}"
                os.rename(version_dir, broken)
                _rmtree(broken)
            os.rename(staging, version_dir)
        except BaseException:
            _rmtree(staging)
            raise
        log.info("install: copied %s to %s", bundle.source, version_dir)

    def _activate(self, version: str, link_to: Path, exe_rel: PurePath, previous_target: Path | None,
                  previous_version: str | None, action: str) -> InstallResult:
        exe = self.paths.current / exe_rel
        command = [str(exe)]
        if not self.hooks.stop_service(self.stop_timeout):
            log.warning("install: the running service did not stop; continuing")
        switch_link(self.paths.current, link_to)
        try:
            warnings = self.hooks.register(command)
        except InstallError as exc:
            restored = self._restore(previous_target, previous_version, command)
            return InstallResult("rolled_back" if restored else "failed", previous_version or version, exe, [],
                                 str(exc))
        self.hooks.start_service(command)
        if self._healthy(version):
            previous = previous_version if previous_version != version else _record_previous(self.paths)
            self._record(version, exe, "managed", previous=previous)
            warnings += self._prune({link_to, *([previous_target] if previous_target else [])})
            return InstallResult(action, version, exe, warnings)
        error = (f"Cremind Connect {version} did not answer within {self.health_timeout:.0f} s; "
                 f"see {self.paths.logs_dir}")
        log.error("install: %s", error)
        self.hooks.stop_service(self.stop_timeout)
        if self._restore(previous_target, previous_version, command):
            return InstallResult("rolled_back", previous_version or "", exe, warnings, error)
        return InstallResult("failed", version, exe, warnings, error)

    def _restore(self, previous_target: Path | None, previous_version: str | None, command: list[str]) -> bool:
        """Go back to the previous version (``True``), or undo a fresh install (``False``)."""
        if previous_target is not None and previous_target.is_dir():
            switch_link(self.paths.current, previous_target)
            self.hooks.start_service(command)
            if previous_version and not self._healthy(previous_version):
                log.error("install: the previous version %s does not answer either", previous_version)
            if previous_version:
                self._record(previous_version, Path(command[0]), "managed")
            return True
        for warning in self.hooks.unregister():
            log.warning("install: %s", warning)
        _remove_link(self.paths.current)
        return False

    def _healthy(self, version: str) -> bool:
        deadline = self._clock() + self.health_timeout
        while True:
            if self.hooks.service_version() == version:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(0.5)

    def _record(self, version: str, exe: Path, layout: str, **extra: Any) -> None:
        write_record(self.paths, version=version, exe=str(exe), layout=layout,
                     **{k: v for k, v in extra.items() if v is not None})

    def _prune(self, keep: set[Path]) -> list[str]:
        """Delete every version directory but ``keep`` (the new and the previous one)."""
        keep_dirs = {_version_dir_of(p, self.paths.versions_dir) for p in keep
                     if _inside(p, self.paths.versions_dir)}
        running = executable()
        warnings = []
        try:
            entries = list(self.paths.versions_dir.iterdir())
        except FileNotFoundError:
            return warnings
        for entry in entries:
            if not entry.is_dir() or any(_same(entry, k) for k in keep_dirs) or _inside(running, entry):
                continue
            try:
                _rmtree(entry)
                log.info("install: removed %s", entry)
            except OSError as exc:
                warnings.append(f"could not remove the old version {entry.name}: {exc}")
        return warnings

    # -- uninstall -----------------------------------------------------------------------

    def uninstall(self, *, keep_data: bool = True) -> InstallResult:
        """Unregister, stop the service, remove managed program files (and data unless ``keep_data``)."""
        warnings = self.hooks.unregister()
        if not self.hooks.stop_service(self.stop_timeout):
            warnings.append("the service did not stop; some files may remain until the next restart")
        running = executable()
        leftovers: list[Path] = []
        _remove_link(self.paths.current)
        if self.paths.versions_dir.is_dir():
            for entry in self.paths.versions_dir.iterdir():
                if _inside(running, entry):
                    leftovers.append(entry)
                    continue
                try:
                    _rmtree(entry)
                except OSError as exc:
                    warnings.append(f"could not remove {entry}: {exc}")
                    leftovers.append(entry)
        if not leftovers:
            for directory in (self.paths.versions_dir, self.paths.app_root):
                with contextlib.suppress(OSError):
                    directory.rmdir()
        elif sys.platform == "win32" and any(_inside(running, e) for e in leftovers):
            _remove_after_exit(self.paths.app_root)
            warnings.append(f"{self.paths.app_root} is removed once this program exits")
        with contextlib.suppress(FileNotFoundError):
            self.paths.install_record.unlink()
        if not keep_data:
            if _inside(running, self.paths.data_dir):
                warnings.append(f"{self.paths.data_dir} holds the running program and was kept")
            else:
                try:
                    _rmtree(self.paths.data_dir)
                except OSError as exc:
                    warnings.append(f"could not remove {self.paths.data_dir}: {exc}")
        return InstallResult("uninstalled", connect_version(), None, warnings)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_key(version: str) -> tuple[Any, ...]:
    try:
        return (0, version_key(version))
    except ValueError:
        return (1, version)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def _inside(path: Path, directory: Path) -> bool:
    try:
        real, base = Path(os.path.realpath(path)), Path(os.path.realpath(directory))
    except OSError:
        return False
    return real == base or base in real.parents


def _version_dir_of(path: Path, versions_dir: Path) -> Path:
    real, base = Path(os.path.realpath(path)), Path(os.path.realpath(versions_dir))
    relative = real.relative_to(base)
    return base / relative.parts[0]


def _version_name(target: Path | None) -> str | None:
    """The version a ``current`` target belongs to: ``versions/<v>`` or ``versions/<v>/<name>.app``."""
    if target is None:
        return None
    return (target.parent if target.suffix == ".app" else target).name


def _record_previous(paths: ConnectPaths) -> str | None:
    record = read_record(paths)
    previous = record.get("previous") if record else None
    return previous if isinstance(previous, str) else None


def _rmtree(path: Path, attempts: int = 10) -> None:
    def onexc(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)  # read-only files (Windows)
            func(target)

    for attempt in range(attempts):
        try:
            if _is_link(path):
                _remove_link(path)
            else:
                shutil.rmtree(path, onexc=onexc)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)


def _remove_after_exit(path: Path) -> None:
    """Windows: delete ``path`` a few seconds after this process exits (its own files are in use until then)."""
    with contextlib.suppress(OSError):
        subprocess.Popen(["cmd.exe", "/d", "/c", f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{path}"'],
                         creationflags=0x00000008 | 0x00000200, close_fds=True,  # detached, own process group
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_registered(paths: ConnectPaths) -> InstallResult | None:
    """First launch of a copy nobody registered (the macOS ``.dmg``): register it in place. Best effort.

    Never under ``CREMIND_CONNECT_HOME`` (tests, side-by-side runs): the OS registration is per
    user, not per home, and an implicit one would take over the real installation's.
    """
    if not is_frozen() or paths.overridden or read_record(paths) is not None:
        return None
    try:
        return Installer(paths).register_only()
    except (InstallError, OSError) as exc:
        log.warning("install: could not register this copy: %s", exc)
        return None


__all__ = ["HEALTH_TIMEOUT_S", "RECORD_SCHEMA", "Bundle", "Hooks", "InstallError", "InstallResult", "Installed",
           "Installer", "SystemHooks", "ensure_registered", "installed_copies", "link_target", "newest",
           "read_bundle", "read_record", "running_bundle", "switch_link", "write_record"]
