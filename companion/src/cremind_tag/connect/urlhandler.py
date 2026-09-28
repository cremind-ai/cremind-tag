"""Make the OS open ``cremind-connect:`` links with Cremind Connect (docs/connect-setup.md §8.1, §11.4).

=========  ==============================================================================================
OS         Mechanism
=========  ==============================================================================================
Windows    ``HKCU\\Software\\Classes\\cremind-connect``: default value ``URL:Cremind Connect``,
           ``URL Protocol`` = "", ``DefaultIcon``, ``shell\\open\\command`` = ``"<exe>" open "%1"``
           (per user: no administrator rights).
macOS      The app's ``Info.plist`` owns the scheme (``CFBundleURLTypes``, written by the PyInstaller
           spec); registering only asks LaunchServices to look at the app again (``lsregister -f``).
           The link reaches the app as an Apple Event, which PyInstaller's argv emulation turns into
           ``argv[1]`` (``main`` accepts a bare link).
Linux      ``~/.local/share/applications/cremind-connect.desktop`` (``Exec=<exe> open %u``,
           ``MimeType=x-scheme-handler/cremind-connect;``, ``NoDisplay=true``), then
           ``xdg-mime default`` and ``update-desktop-database`` (both best effort: minimal desktops
           lack them, and the desktop file alone is often enough).
=========  ==============================================================================================

Like :mod:`.startup`, everything here only *plans*; :func:`~cremind_tag.connect.plan.apply` acts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .links import SCHEME
from .paths import ConnectPaths
from .plan import Plan, PlanFile, RegistryValue, Runner, read_registry_value, run_command
from .startup import windows_quote

REGISTRY_KEY = rf"Software\Classes\{SCHEME}"
DESKTOP_FILE = "cremind-connect.desktop"
MIME_TYPE = f"x-scheme-handler/{SCHEME}"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/"
              "Support/lsregister")


@dataclass(frozen=True)
class UrlHandlerStatus:
    kind: str
    registered: bool | None
    target: str | None = None
    detail: str | None = None

    def as_json(self) -> dict[str, object]:
        return {"kind": self.kind, "registered": self.registered, "target": self.target, "detail": self.detail}


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def windows_open_command(command: Sequence[str]) -> str:
    """``"<exe>" [args] open "%1"`` — the program always quoted, ``%1`` (the link) always quoted."""
    program, *args = command
    parts = [f'"{program}"', *(windows_quote(a) for a in args), "open", '"%1"']
    return " ".join(parts)


def windows_registry_values(command: Sequence[str]) -> tuple[RegistryValue, ...]:
    return (
        RegistryValue(REGISTRY_KEY, "", "URL:Cremind Connect"),
        RegistryValue(REGISTRY_KEY, "URL Protocol", ""),
        RegistryValue(REGISTRY_KEY + r"\DefaultIcon", "", f'"{command[0]}",0'),
        RegistryValue(REGISTRY_KEY + r"\shell\open\command", "", windows_open_command(command)),
    )


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def app_bundle_of(executable: Path) -> Path | None:
    """The ``.app`` directory an executable lives in (``None`` outside a bundle)."""
    for parent in Path(executable).parents:
        if parent.suffix == ".app":
            return parent
    return None


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_arg(arg: str) -> str:
    """One ``Exec=`` argument (Desktop Entry spec: quoting rules, then string escaping; ``%`` doubled)."""
    arg = arg.replace("%", "%%")
    if arg and not (_EXEC_RESERVED & set(arg)):
        return arg
    quoted = '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg) + '"'
    return quoted.replace("\\", "\\\\")  # the value is also a desktop-file string: backslashes doubled


def desktop_entry(command: Sequence[str]) -> str:
    exec_line = " ".join(desktop_exec_arg(a) for a in command) + " open %u"
    return f"""[Desktop Entry]
Type=Application
Name=Cremind Connect
Comment=Connects the Cremind Tag gateways plugged into this computer to Cremind
Exec={exec_line}
Terminal=false
NoDisplay=true
MimeType={MIME_TYPE};
Categories=Network;
"""


def _applications_dir(env: Mapping[str, str] | None) -> Path:
    env = os.environ if env is None else env
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else Path(env.get("HOME") or Path.home()) / \
        ".local" / "share"
    return base / "applications"


def desktop_file_path(env: Mapping[str, str] | None = None) -> Path:
    return _applications_dir(env) / DESKTOP_FILE


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def register_plan(command: Sequence[str], paths: ConnectPaths, *, env: Mapping[str, str] | None = None) -> Plan:
    """Make ``cremind-connect:`` links run ``command open <link>``."""
    command = [str(c) for c in command]
    if paths.kind == "windows":
        return Plan("winreg", rf"HKCU\{REGISTRY_KEY}", registry=windows_registry_values(command))
    if paths.kind == "macos":
        app = app_bundle_of(Path(command[0]))
        if app is None:
            return Plan("bundle", supported=False,
                        reason="only an app bundle can own the cremind-connect: scheme on macOS "
                               "(this program is not running from Cremind Connect.app)")
        return Plan("bundle", f"{app.name} declares {SCHEME}: in its Info.plist (bundle-managed)",
                    best_effort=((LSREGISTER, "-f", str(app)),))
    desktop = desktop_file_path(env)
    return Plan("xdg", f"{desktop} handles {MIME_TYPE}",
                files=(PlanFile(desktop, desktop_entry(command), mode=0o644),),
                best_effort=(("xdg-mime", "default", DESKTOP_FILE, MIME_TYPE),
                             ("update-desktop-database", str(desktop.parent))))


def unregister_plan(paths: ConnectPaths, *, app: Path | None = None, env: Mapping[str, str] | None = None) -> Plan:
    if paths.kind == "windows":
        return Plan("winreg", rf"remove HKCU\{REGISTRY_KEY}", remove_registry=(REGISTRY_KEY,))
    if paths.kind == "macos":
        return Plan("bundle", "the app bundle owns the scheme; nothing to remove",
                    best_effort=((LSREGISTER, "-u", str(app)),) if app is not None else ())
    desktop = desktop_file_path(env)
    return Plan("xdg", f"remove {desktop}", remove_files=(desktop,),
                cleanup=(("update-desktop-database", str(desktop.parent)),))


def status(paths: ConnectPaths, *, runner: Runner | None = None,
           env: Mapping[str, str] | None = None) -> UrlHandlerStatus:
    """Read-only: is the scheme registered, and to what?"""
    if paths.kind == "windows":
        target = read_registry_value(REGISTRY_KEY + r"\shell\open\command")
        return UrlHandlerStatus("winreg", target is not None, target)
    if paths.kind == "macos":
        return UrlHandlerStatus("bundle", None, None, "declared by the app bundle's Info.plist")
    desktop = desktop_file_path(env)
    code, out = (runner or run_command)(("xdg-mime", "query", "default", MIME_TYPE))
    default = out.strip() if code == 0 else None
    return UrlHandlerStatus("xdg", desktop.is_file(), str(desktop) if desktop.is_file() else None,
                            f"default handler: {default or 'unknown'}")


__all__ = ["DESKTOP_FILE", "LSREGISTER", "MIME_TYPE", "REGISTRY_KEY", "UrlHandlerStatus", "app_bundle_of",
           "desktop_entry", "desktop_exec_arg", "desktop_file_path", "register_plan", "status", "unregister_plan",
           "windows_open_command", "windows_registry_values"]
