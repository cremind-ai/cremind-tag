"""``cremind-connect``: the Cremind Connect program (docs/connect-setup.md §11.1).

::

    cremind-connect service                 the per-user supervisor (started at logon)
    cremind-connect worker --dir D [--port P]   one worker (started by the service)
    cremind-connect open <link>             URL handler: hand the link to the service, which shows the window
    cremind-connect window [--link <link>]  the native setup window (started by the service; the link
                                            normally arrives in $CREMIND_CONNECT_LINK, never on a
                                            long-lived command line)
    cremind-connect status [--json]         this program, the installed copy, the service, its workers
    cremind-connect install [--from DIR] [--register-only]
    cremind-connect uninstall [--keep-data | --purge]
    cremind-connect version [--json]

A bare ``cremind-connect:`` link as the only argument means ``open <link>``
(macOS delivers links to the app as an Apple Event, which PyInstaller turns into
``argv[1]``). Without arguments a packaged copy registers itself if nobody did
(the macOS ``.dmg``), makes sure the service runs and exits.

``worker`` and ``window`` run :func:`cremind_tag.connect.worker.run_worker` and
:func:`cremind_tag.connect.setup_flow.run_setup`, imported lazily (they are
built separately); a build without them exits with a clear message (status 3).
Contracts: ``run_worker(directory: Path, port: str | None[, paths=ConnectPaths])``
and ``run_setup(link_url: str[, paths=ConnectPaths])`` return an exit status.

The Windows executable is windowed (no console window flashes at logon or
for links): ``status``/``version``/``install`` attach to the console they were
started from, and when there is none, output goes to ``<data>/logs``.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import logging
import logging.handlers
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .links import SCHEME, LinkError, parse_setup_link, redact
from .paths import ConnectPaths, default_paths
from .runtime import bundle_info, connect_version, executable, is_frozen, os_kind, self_command, spawn_detached

log = logging.getLogger("cremind_tag.connect")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_MISSING = 3
LINK_ENV = "CREMIND_CONNECT_LINK"
SERVICE_START_TIMEOUT_S = 15.0
INTERACTIVE = frozenset({"status", "version", "install", "uninstall"})


# ---------------------------------------------------------------------------
# Output and logging
# ---------------------------------------------------------------------------


def _attach_parent_console() -> bool:
    """Windows GUI executable started from a terminal: write to that terminal."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
        return True
    except OSError:
        return False


def _fix_stdio(paths: ConnectPaths, role: str) -> None:
    """A windowed executable has no ``sys.stdout``: use the parent console or a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    if role in INTERACTIVE and _attach_parent_console():
        return
    with contextlib.suppress(OSError):
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        stream: TextIO = open(paths.logs_dir / f"{role}-console.log", "a", encoding="utf-8",  # noqa: SIM115
                              errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def _setup_logging(paths: ConnectPaths, role: str, *, log_file: Path | None = None, verbose: bool = False) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_cremind_connect", False) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    target = log_file or paths.logs_dir / f"{role}.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3,
                                                                        encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler._cremind_connect = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if verbose and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

    def excepthook(kind: type[BaseException], value: BaseException, tb: Any) -> None:
        logging.getLogger("cremind_tag.connect").critical("unhandled error", exc_info=(kind, value, tb))
        if sys.stderr is not None:
            sys.__excepthook__(kind, value, tb)

    sys.excepthook = excepthook


def _print(text: str = "") -> None:
    if sys.stdout is not None:
        print(text)


def _print_json(data: Any) -> None:
    _print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------


def _window_env() -> dict[str, str]:
    from .service import WINDOW_ENV_KEYS

    return {k: os.environ[k] for k in WINDOW_ENV_KEYS if os.environ.get(k)}


def ensure_service(paths: ConnectPaths, timeout: float = SERVICE_START_TIMEOUT_S) -> bool:
    """The service answers ``ping`` — starting it (through the OS registration when there is one) if needed."""
    from . import ipc, startup
    from .plan import apply

    if ipc.ping(paths) is not None:
        return True
    paths.ensure()
    started = False
    with contextlib.suppress(Exception):
        if startup.status(paths).registered:
            started = apply(startup.start_plan(paths)).ok
    if not started:
        try:
            spawn_detached(self_command("service"), cwd=paths.data_dir,
                           log_path=paths.logs_dir / "service-console.log")
        except OSError as exc:
            log.error("open: cannot start the service: %s", exc)
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ipc.ping(paths, timeout=1.0) is not None:
            return True
        time.sleep(0.25)
    return False


def _spawn_window(paths: ConnectPaths, url: str) -> None:
    env = {**os.environ, LINK_ENV: url}
    spawn_detached(self_command("window"), env=env, cwd=paths.data_dir,
                   log_path=paths.logs_dir / "window-console.log")


def _allow_foreground() -> None:
    """Windows: let the window the service opens come to the front (this process was started by the browser)."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def _show_message(title: str, message: str) -> None:
    with contextlib.suppress(Exception):
        from .window import show_message

        show_message(title, message)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_service(paths: ConnectPaths, args: argparse.Namespace) -> int:
    _setup_logging(paths, "service", verbose=args.verbose)
    from .service import run_service

    return run_service(paths)


def _accepts(function: Any, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def cmd_worker(paths: ConnectPaths, args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    _setup_logging(paths, "worker", log_file=directory / "logs" / "worker.log", verbose=args.verbose)
    try:
        from .worker import run_worker  # built separately (see the module docstring)
    except ImportError as exc:
        if exc.name != "cremind_tag.connect.worker":
            raise
        message = "this Cremind Connect build has no worker (cremind_tag.connect.worker is missing)"
        log.error("worker: %s", message)
        if sys.stderr is not None:
            print(f"cremind-connect: {message}", file=sys.stderr)
        return EXIT_MISSING
    kwargs = {"paths": paths} if _accepts(run_worker, "paths") else {}
    return int(run_worker(directory, args.port, **kwargs) or 0)


def cmd_open(paths: ConnectPaths, args: argparse.Namespace) -> int:
    from . import ipc

    _setup_logging(paths, "open", verbose=args.verbose)
    url = args.url
    try:
        parse_setup_link(url)
    except LinkError as exc:
        log.warning("open: refused %s: %s", redact(url or ""), exc)
        _show_message("This link cannot be used", f"{exc}\n\nStart again from Cremind: Settings → Tags.")
        return EXIT_USAGE
    log.info("open: %s", redact(url))
    _allow_foreground()
    if os_kind() == "macos" and is_frozen():
        from .install import ensure_registered

        ensure_registered(paths)  # the .dmg copy has no installer that registers it
    if ensure_service(paths):
        try:
            answer = ipc.request(paths, "open_link", url=url, env=_window_env(), timeout=10.0)
        except ipc.IpcError as exc:
            log.warning("open: the service did not take the link (%s); opening the window directly", exc)
        else:
            if answer.get("ok"):
                return EXIT_OK
            if answer.get("error") == "invalid_link":
                _show_message("This link cannot be used", str(answer.get("message")))
                return EXIT_USAGE
            log.warning("open: the service answered %s; opening the window directly", answer)
    else:
        log.warning("open: the service did not start; opening the window directly")
    try:
        _spawn_window(paths, url)
    except OSError as exc:
        log.error("open: cannot open the setup window: %s", exc)
        _show_message("Cremind Connect could not start", f"{exc}\n\nDetails are in {paths.logs_dir}.")
        return EXIT_ERROR
    return EXIT_OK


def cmd_window(paths: ConnectPaths, args: argparse.Namespace) -> int:
    _setup_logging(paths, "window", verbose=args.verbose)
    url = args.link or os.environ.pop(LINK_ENV, None)
    if not url:
        _show_message("Cremind Connect", "Start the setup from Cremind: Settings → Tags → Connect gateway.")
        return EXIT_USAGE
    try:
        parse_setup_link(url)
    except LinkError as exc:
        _show_message("This link cannot be used", str(exc))
        return EXIT_USAGE
    try:
        from .setup_flow import run_setup  # built separately (see the module docstring)
    except ImportError as exc:
        if exc.name != "cremind_tag.connect.setup_flow":
            raise
        log.error("window: this build has no setup flow (cremind_tag.connect.setup_flow is missing)")
        _show_message("Cremind Connect is incomplete",
                      "This version of Cremind Connect cannot set up gateways yet. Install the latest version.")
        return EXIT_MISSING
    kwargs = {"paths": paths} if _accepts(run_setup, "paths") else {}
    return int(run_setup(url, **kwargs) or 0)


def status_report(paths: ConnectPaths) -> dict[str, Any]:
    """Everything ``status`` shows (read-only: creates nothing)."""
    from . import ipc, startup, urlhandler
    from .install import read_record
    from .workerdir import WorkerDirError, list_workers

    report: dict[str, Any] = {
        "version": connect_version(), "frozen": is_frozen(), "executable": str(executable()),
        "data_dir": str(paths.data_dir), "app_root": str(paths.app_root), "installed": read_record(paths),
    }
    try:
        answer = ipc.request(paths, "status", timeout=5.0)
        report["service"] = {"running": True, **{k: v for k, v in answer.items() if k != "ok"}}
    except ipc.IpcError as exc:
        workers = []
        for directory, spec in list_workers(paths.workers_dir):
            if isinstance(spec, WorkerDirError):
                workers.append({"worker_id": directory.name, "state": "invalid", "last_error": str(spec)})
            else:
                workers.append({"worker_id": spec.worker_id, "server": spec.server_origin, "profile": spec.profile,
                                "gateway_device_id": spec.gateway_device_id,
                                "state": "stopped" if spec.enabled else "disabled"})
        report["service"] = {"running": False, "detail": str(exc), "workers": workers}
    for key, query in (("startup", startup.status), ("url_handler", urlhandler.status)):
        try:
            report[key] = query(paths).as_json()
        except Exception as exc:  # a missing tool must not break status
            report[key] = {"registered": None, "detail": f"{type(exc).__name__}: {exc}"}
    return report


def _short(device_id: str | None) -> str:
    return f"…{device_id[-4:].upper()}" if device_id else "?"


def cmd_status(paths: ConnectPaths, args: argparse.Namespace) -> int:
    report = status_report(paths)
    if args.json:
        _print_json(report)
        return EXIT_OK
    installed = report["installed"]
    service = report["service"]
    _print(f"Cremind Connect {report['version']} ({report['executable']})")
    _print(f"Installed: {installed['version']} at {installed['exe']} ({installed.get('layout')})" if installed
           else "Installed: no")
    if service["running"]:
        _print(f"Service: running (version {service.get('version')}, pid {service.get('pid')})")
    else:
        _print(f"Service: not running ({service.get('detail')})")
    startup_state = report["startup"]
    _print(f"Starts at logon: {_yes_no(startup_state.get('registered'))} ({startup_state.get('kind', '?')})")
    _print(f"Opens {SCHEME}: links: {_yes_no(report['url_handler'].get('registered'))}")
    workers = service.get("workers") or []
    _print("Workers:" if workers else "Workers: none")
    for worker in workers:
        _print(f"  {worker.get('worker_id')}: {worker.get('state')} — gateway {_short(worker.get('gateway_device_id'))}"
               f", {worker.get('profile') or '?'} at {worker.get('server') or '?'}"
               + (f" ({worker['last_error']})" if worker.get("last_error") else ""))
    for port in service.get("ports") or []:
        identity = port.get("identity")
        what = f"{identity['role']} {_short(identity['device_id'])}" if identity else port.get("reason") or "unknown"
        _print(f"  port {port.get('device')}: {what}" + (f", used by {port['held_by']}" if port.get("held_by") else ""))
    return EXIT_OK


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def cmd_install(paths: ConnectPaths, args: argparse.Namespace) -> int:
    from .install import InstallError, Installer, read_bundle, running_bundle

    _setup_logging(paths, "install", verbose=args.verbose)
    paths.ensure()
    installer = Installer(paths)
    try:
        if args.register_only:
            result = installer.register_only()
        else:
            bundle = read_bundle(Path(args.from_dir)) if args.from_dir else running_bundle()
            if bundle is None:
                _print("cremind-connect install: run it from a packaged Cremind Connect, or pass --from <bundle>")
                return EXIT_USAGE
            result = installer.install(bundle)
    except InstallError as exc:
        log.error("install: %s", exc)
        _print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"cremind-connect install: {exc}")
        return EXIT_ERROR
    log.info("install: %s", result.as_json())
    if args.json:
        _print_json(result.as_json())
    else:
        _print(f"{result.action}: Cremind Connect {result.version}" + (f" ({result.exe})" if result.exe else ""))
        for warning in result.warnings:
            _print(f"  note: {warning}")
        if result.error:
            _print(f"  error: {result.error}")
    return EXIT_OK if result.ok else EXIT_ERROR


def cmd_uninstall(paths: ConnectPaths, args: argparse.Namespace) -> int:
    from .install import Installer

    _setup_logging(paths, "install", verbose=args.verbose)
    result = Installer(paths).uninstall(keep_data=not args.purge)
    if args.json:
        _print_json(result.as_json())
    else:
        _print("Cremind Connect was removed" + ("" if args.purge else f"; its data stays in {paths.data_dir}"))
        for warning in result.warnings:
            _print(f"  note: {warning}")
    return EXIT_OK


def cmd_version(paths: ConnectPaths, args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"name": "cremind-connect", "version": connect_version(), "frozen": is_frozen(),
                     "executable": str(executable()), "python": sys.version.split()[0], "platform": sys.platform,
                     "bundle": bundle_info() or None})
    else:
        _print(connect_version())
    return EXIT_OK


def cmd_launch(paths: ConnectPaths, args: argparse.Namespace) -> int:
    """No arguments (a double-click): register a copy nobody registered, make sure the service runs."""
    _setup_logging(paths, "open", verbose=args.verbose)
    from .install import ensure_registered

    ensure_registered(paths)
    return EXIT_OK if ensure_service(paths) else EXIT_ERROR


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cremind-connect",
                                     description="Cremind Connect: connects Cremind Tag gateways on this computer to "
                                                 "Cremind.")
    parser.add_argument("-v", "--verbose", action="store_true", help="more detailed logs")
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("service", help="run the per-user service (normally started at logon)")
    p.set_defaults(func=cmd_service)
    p = sub.add_parser("worker", help="run one worker (started by the service)")
    p.add_argument("--dir", required=True, help="the worker directory")
    p.add_argument("--port", help="the gateway's serial port")
    p.set_defaults(func=cmd_worker)
    p = sub.add_parser("open", help=f"open a {SCHEME}: link (the URL handler)")
    p.add_argument("url")
    p.set_defaults(func=cmd_open)
    p = sub.add_parser("window", help="show the setup window (started by the service)")
    p.add_argument("--link", help=f"the {SCHEME}: link (default: ${LINK_ENV})")
    p.set_defaults(func=cmd_window)
    p = sub.add_parser("status", help="show this program, the installed copy, the service and its workers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("install", help="install or upgrade for this user, register startup and links")
    p.add_argument("--from", dest="from_dir", help="the bundle to install (default: this program)")
    p.add_argument("--register-only", action="store_true",
                   help="register this copy where it is (after an OS installer placed it)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("uninstall", help="unregister, stop and remove the program")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--keep-data", action="store_true", default=True, help="keep workers and keys (default)")
    group.add_argument("--purge", action="store_true", help="also delete all data (workers, keys, logs)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_uninstall)
    p = sub.add_parser("version", help="print the version")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_version)
    return parser


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """A bare link means ``open <link>``; macOS's ``-psn_…`` launch argument is dropped."""
    out = [a for a in argv if not a.startswith("-psn_")]
    if out and out[0].lower().startswith(f"{SCHEME}:"):
        out = ["open", *out]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    args_list = normalize_argv(sys.argv[1:] if argv is None else argv)
    paths = default_paths()
    command = args_list[0] if args_list and not args_list[0].startswith("-") else ("launch" if not args_list
                                                                                   else "")
    _fix_stdio(paths, command or "cli")
    parser = build_parser()
    if not args_list:
        if is_frozen():
            return cmd_launch(paths, parser.parse_args([]))
        parser.print_help()
        return EXIT_USAGE
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return int(args.func(paths, args) or 0)


__all__ = ["build_parser", "ensure_service", "main", "normalize_argv", "status_report"]
