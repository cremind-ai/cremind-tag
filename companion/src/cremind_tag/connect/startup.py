"""Start Cremind Connect at logon and keep it running (docs/connect-setup.md §11.5).

=========  ==========================================================================================
OS         Mechanism
=========  ==========================================================================================
Windows    Scheduled Task ``Cremind Connect`` created with ``schtasks /Create /XML``: a logon trigger
           for this user's SID, ``InteractiveToken`` + ``LeastPrivilege`` (normal user rights, the
           user's desktop, so the setup window can appear), restart on failure every minute up to
           999 times, no execution time limit, ``IgnoreNew`` for a second instance. It runs the
           windowed executable with ``service``.
macOS      LaunchAgent ``io.cremind.connect`` (``RunAtLoad``, ``KeepAlive``, ``ThrottleInterval`` 5,
           output to ``<data>/logs/launchd.log``), loaded with ``launchctl bootstrap gui/<uid>``.
Linux      systemd user unit ``cremind-connect.service`` (``Restart=always``, ``RestartSec=2``,
           ``WantedBy=default.target``); ``daemon-reload`` then ``enable --now``.
=========  ==========================================================================================

Everything here *plans* (:class:`~cremind_tag.connect.plan.Plan`); nothing touches
the machine until :func:`~cremind_tag.connect.plan.apply` runs the plan, so
tests assert the rendered task XML, plist and unit. ``command`` is the program
prefix (``[exe]`` for a bundle, ``[python, "-m", "cremind_tag.connect"]`` from
source); ``service`` is appended.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .paths import ConnectPaths
from .plan import Plan, PlanFile, Runner, run_command

TASK_NAME = "Cremind Connect"
LAUNCHD_LABEL = "io.cremind.connect"
UNIT_NAME = "cremind-connect.service"
SERVICE_ARG = "service"


@dataclass(frozen=True)
class StartupStatus:
    kind: str
    registered: bool | None
    """True/False where the OS can say, ``None`` when it cannot."""
    target: str | None = None
    """The command line the registration runs."""
    loaded: bool | None = None
    """macOS/Linux: the service manager has the job loaded / enabled."""
    detail: str | None = None

    def as_json(self) -> dict[str, object]:
        return {"kind": self.kind, "registered": self.registered, "target": self.target, "loaded": self.loaded,
                "detail": self.detail}


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


def _xml(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def windows_quote(arg: str) -> str:
    """One argument for a Windows command line (``subprocess.list2cmdline`` rules)."""
    return subprocess.list2cmdline([arg])


def systemd_quote(arg: str) -> str:
    """One word of ``ExecStart=`` (quoted; ``%`` and ``$`` escaped for systemd's specifier/variable expansion)."""
    text = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def task_xml(command: Sequence[str], user_sid: str, workdir: Path) -> str:
    """The Scheduled Task definition (UTF-16 when written: what ``schtasks /XML`` expects)."""
    program, *args = [*command, SERVICE_ARG]
    arguments = " ".join(windows_quote(a) for a in args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keeps Cremind Connect running for this user: it connects the Cremind Tag gateways plugged into this computer to Cremind. Managed by "cremind-connect install" and "cremind-connect uninstall".</Description>
    <URI>\\{_xml(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml(user_sid)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml(user_sid)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml(windows_quote(program))}</Command>
      <Arguments>{_xml(arguments)}</Arguments>
      <WorkingDirectory>{_xml(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _task_xml_path(paths: ConnectPaths) -> Path:
    return paths.data_dir / "startup" / "cremind-connect-task.xml"


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def launchd_plist(command: Sequence[str], paths: ConnectPaths) -> str:
    log = str(paths.logs_dir / "launchd.log")
    doc = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*command, SERVICE_ARG],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "WorkingDirectory": str(paths.data_dir),
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _home(env: Mapping[str, str] | None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("HOME") or Path.home())


def launchd_plist_path(env: Mapping[str, str] | None = None) -> Path:
    return _home(env) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _uid(uid: int | None) -> int:
    if uid is not None:
        return uid
    return os.getuid() if hasattr(os, "getuid") else 0


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def systemd_unit(command: Sequence[str], paths: ConnectPaths) -> str:
    exec_start = " ".join(systemd_quote(a) for a in [*command, SERVICE_ARG])
    return f"""# Written by "cremind-connect install"; "cremind-connect uninstall" removes it.
[Unit]
Description=Cremind Connect (Cremind Tag gateways for this user)

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={systemd_quote(str(paths.data_dir))}
Restart=always
RestartSec=2
# Stop the workers gracefully before systemd escalates.
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""


def systemd_unit_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else _home(env) / ".config"
    return base / "systemd" / "user" / UNIT_NAME


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def register_plan(command: Sequence[str], paths: ConnectPaths, *, user_sid: str | None = None,
                  uid: int | None = None, env: Mapping[str, str] | None = None) -> Plan:
    """Register ``command service`` to start at logon (does not start it on Windows; see :func:`start_plan`)."""
    command = [str(c) for c in command]
    if paths.kind == "windows":
        if user_sid is None:
            from ..private_files import current_user_sid

            user_sid = current_user_sid()
        xml_path = _task_xml_path(paths)
        return Plan("schtasks", f'Scheduled Task "{TASK_NAME}" at logon',
                    files=(PlanFile(xml_path, task_xml(command, user_sid, paths.data_dir), encoding="utf-16"),),
                    commands=(("schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"),))
    if paths.kind == "macos":
        plist = launchd_plist_path(env)
        domain = f"gui/{_uid(uid)}"
        return Plan("launchd", f"LaunchAgent {LAUNCHD_LABEL}",
                    files=(PlanFile(plist, launchd_plist(command, paths), mode=0o644),),
                    # bootout first: bootstrap refuses a job that is already loaded ("service already loaded")
                    best_effort=(("launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"),),
                    commands=(("launchctl", "bootstrap", domain, str(plist)),))
    unit = systemd_unit_path(env)
    return Plan("systemd", f"systemd user unit {UNIT_NAME}",
                files=(PlanFile(unit, systemd_unit(command, paths), mode=0o644),),
                commands=(("systemctl", "--user", "daemon-reload"),
                          ("systemctl", "--user", "enable", "--now", UNIT_NAME)))


def unregister_plan(paths: ConnectPaths, *, uid: int | None = None, env: Mapping[str, str] | None = None) -> Plan:
    """Remove the registration and stop the registered job (idempotent: every command is best effort)."""
    if paths.kind == "windows":
        return Plan("schtasks", f'remove Scheduled Task "{TASK_NAME}"',
                    best_effort=(("schtasks", "/End", "/TN", TASK_NAME),
                                 ("schtasks", "/Delete", "/TN", TASK_NAME, "/F")),
                    remove_files=(_task_xml_path(paths),))
    if paths.kind == "macos":
        return Plan("launchd", f"remove LaunchAgent {LAUNCHD_LABEL}",
                    best_effort=(("launchctl", "bootout", f"gui/{_uid(uid)}/{LAUNCHD_LABEL}"),),
                    remove_files=(launchd_plist_path(env),))
    return Plan("systemd", f"remove systemd user unit {UNIT_NAME}",
                best_effort=(("systemctl", "--user", "disable", "--now", UNIT_NAME),),
                remove_files=(systemd_unit_path(env),),
                cleanup=(("systemctl", "--user", "daemon-reload"),))


def start_plan(paths: ConnectPaths, *, uid: int | None = None, env: Mapping[str, str] | None = None) -> Plan:
    """Start the registered service now (a no-op when it is already running)."""
    if paths.kind == "windows":
        return Plan("schtasks", "start now", commands=(("schtasks", "/Run", "/TN", TASK_NAME),))
    if paths.kind == "macos":
        domain = f"gui/{_uid(uid)}"
        # bootstrap loads the job again after a stop (bootout); it fails harmlessly when already loaded
        return Plan("launchd", "start now",
                    best_effort=(("launchctl", "bootstrap", domain, str(launchd_plist_path(env))),),
                    commands=(("launchctl", "kickstart", f"{domain}/{LAUNCHD_LABEL}"),))
    return Plan("systemd", "start now", commands=(("systemctl", "--user", "start", UNIT_NAME),))


def stop_plan(paths: ConnectPaths, *, uid: int | None = None) -> Plan:
    """Stop the registered service without the service manager restarting it (registration kept).

    ``KeepAlive`` (launchd) and ``Restart=always`` (systemd) would restart a service
    that merely exits, so an upgrade stops it through the manager: ``systemctl
    --user stop`` and ``launchctl bootout`` (SIGTERM: a graceful stop). On Windows
    ``schtasks /End`` terminates the task's process, so the installer asks over IPC
    first and uses this only for a service that does not stop.
    """
    if paths.kind == "windows":
        return Plan("schtasks", "stop now", best_effort=(("schtasks", "/End", "/TN", TASK_NAME),))
    if paths.kind == "macos":
        return Plan("launchd", "stop now", best_effort=(("launchctl", "bootout", f"gui/{_uid(uid)}/{LAUNCHD_LABEL}"),))
    return Plan("systemd", "stop now", best_effort=(("systemctl", "--user", "stop", UNIT_NAME),))


# ---------------------------------------------------------------------------
# Status (read-only)
# ---------------------------------------------------------------------------


def _task_target(xml_text: str) -> str | None:
    command = re.search(r"<Command>(.*?)</Command>", xml_text, re.S)
    if command is None:
        return None
    arguments = re.search(r"<Arguments>(.*?)</Arguments>", xml_text, re.S)
    from xml.sax.saxutils import unescape

    text = unescape(command.group(1), {"&quot;": '"'})
    if arguments is not None:
        text += " " + unescape(arguments.group(1), {"&quot;": '"'})
    return text


def status(paths: ConnectPaths, *, runner: Runner | None = None, uid: int | None = None,
           env: Mapping[str, str] | None = None) -> StartupStatus:
    """What the OS says about the registration (runs read-only queries only)."""
    run = runner or run_command
    if paths.kind == "windows":
        code, out = run(("schtasks", "/Query", "/TN", TASK_NAME, "/XML"))
        if code != 0:
            return StartupStatus("schtasks", False)
        return StartupStatus("schtasks", True, _task_target(out))
    if paths.kind == "macos":
        plist = launchd_plist_path(env)
        target = None
        if plist.is_file():
            try:
                target = " ".join(plistlib.loads(plist.read_bytes()).get("ProgramArguments", [])) or None
            except (OSError, plistlib.InvalidFileException, ValueError):
                target = None
        code, _ = run(("launchctl", "print", f"gui/{_uid(uid)}/{LAUNCHD_LABEL}"))
        return StartupStatus("launchd", plist.is_file(), target, code == 0)
    unit = systemd_unit_path(env)
    target = None
    for candidate in (unit, Path("/usr/lib/systemd/user") / UNIT_NAME, Path("/lib/systemd/user") / UNIT_NAME):
        if candidate.is_file():
            match = re.search(r"(?m)^ExecStart=(.*)$", candidate.read_text(encoding="utf-8", errors="replace"))
            target = match.group(1).strip() if match else None
            break
    code, out = run(("systemctl", "--user", "is-enabled", UNIT_NAME))
    return StartupStatus("systemd", code == 0 or unit.is_file(), target, code == 0, (out or "").strip() or None)


__all__ = ["LAUNCHD_LABEL", "SERVICE_ARG", "TASK_NAME", "UNIT_NAME", "StartupStatus", "launchd_plist",
           "launchd_plist_path", "register_plan", "start_plan", "status", "stop_plan", "systemd_quote",
           "systemd_unit", "systemd_unit_path", "task_xml", "unregister_plan", "windows_quote"]
