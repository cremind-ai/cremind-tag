"""Startup, URL handler and udev plans: content only — nothing is registered on this machine."""

from __future__ import annotations

import configparser
import plistlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cremind_tag.connect import startup, udev, urlhandler
from cremind_tag.connect.paths import ConnectPaths, default_paths

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
WIN_EXE = r"C:\Users\Anna Lee\AppData\Local\Programs\Cremind Connect\current\cremind-connect.exe"
MAC_EXE = "/Users/anna/Applications/Cremind Connect.app/Contents/MacOS/cremind-connect"
LINUX_EXE = "/home/anna/.local/lib/cremind-connect/current/cremind-connect"
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def win_paths() -> ConnectPaths:
    return default_paths({"LOCALAPPDATA": r"C:\Users\Anna Lee\AppData\Local"}, "win32")


def mac_paths() -> ConnectPaths:
    return default_paths({"HOME": "/Users/anna"}, "darwin")


def linux_paths() -> ConnectPaths:
    return default_paths({"HOME": "/home/anna"}, "linux")


# ---------------------------------------------------------------------------- startup


def test_windows_scheduled_task() -> None:
    paths = win_paths()
    plan = startup.register_plan([WIN_EXE], paths, user_sid=SID)
    assert plan.kind == "schtasks" and plan.supported
    (task,) = plan.files
    assert task.encoding == "utf-16" and task.path.name == "cremind-connect-task.xml"
    assert plan.commands == (("schtasks", "/Create", "/TN", "Cremind Connect", "/XML", str(task.path), "/F"),)
    root = ET.fromstring(task.content.replace('encoding="UTF-16"', ""))

    def text(path: str) -> str:
        node = root.find("/".join(f"{TASK_NS}{part}" for part in path.split("/")))
        assert node is not None, path
        return node.text or ""

    assert text("Triggers/LogonTrigger/UserId") == SID
    assert text("Principals/Principal/UserId") == SID
    assert text("Principals/Principal/LogonType") == "InteractiveToken"
    assert text("Principals/Principal/RunLevel") == "LeastPrivilege"
    assert text("Settings/MultipleInstancesPolicy") == "IgnoreNew"
    assert text("Settings/ExecutionTimeLimit") == "PT0S"
    assert text("Settings/RestartOnFailure/Interval") == "PT1M"
    assert text("Settings/RestartOnFailure/Count") == "999"
    assert text("Actions/Exec/Command") == f'"{WIN_EXE}"'  # a path with spaces, quoted
    assert text("Actions/Exec/Arguments") == "service"
    assert text("Actions/Exec/WorkingDirectory") == str(paths.data_dir)


def test_windows_task_escapes_xml_and_runs_from_source() -> None:
    plan = startup.register_plan([r"C:\a&b\python.exe", "-m", "cremind_tag.connect"], win_paths(), user_sid=SID)
    content = plan.files[0].content
    assert "a&amp;b" in content and "<Arguments>-m cremind_tag.connect service</Arguments>" in content


def test_windows_unregister_and_start() -> None:
    plan = startup.unregister_plan(win_paths())
    assert plan.best_effort == (("schtasks", "/End", "/TN", "Cremind Connect"),
                                ("schtasks", "/Delete", "/TN", "Cremind Connect", "/F"))
    assert not plan.commands and plan.remove_files[0].name == "cremind-connect-task.xml"
    assert startup.start_plan(win_paths()).commands == (("schtasks", "/Run", "/TN", "Cremind Connect"),)
    assert startup.stop_plan(win_paths()).best_effort == (("schtasks", "/End", "/TN", "Cremind Connect"),)


def test_macos_launch_agent() -> None:
    paths = mac_paths()
    plan = startup.register_plan([MAC_EXE], paths, uid=501, env={"HOME": "/Users/anna"})
    (agent,) = plan.files
    assert agent.path == Path("/Users/anna/Library/LaunchAgents/io.cremind.connect.plist")
    doc = plistlib.loads(agent.content.encode("utf-8"))
    assert doc["Label"] == "io.cremind.connect" and doc["ProgramArguments"] == [MAC_EXE, "service"]
    assert doc["RunAtLoad"] is True and doc["KeepAlive"] is True and doc["ThrottleInterval"] == 5
    assert doc["StandardOutPath"] == str(paths.logs_dir / "launchd.log") == doc["StandardErrorPath"]
    assert plan.best_effort == (("launchctl", "bootout", "gui/501/io.cremind.connect"),)
    assert plan.commands == (("launchctl", "bootstrap", "gui/501", str(agent.path)),)
    start = startup.start_plan(paths, uid=501, env={"HOME": "/Users/anna"})
    assert start.commands == (("launchctl", "kickstart", "gui/501/io.cremind.connect"),)
    assert startup.stop_plan(paths, uid=501).best_effort == (("launchctl", "bootout", "gui/501/io.cremind.connect"),)
    gone = startup.unregister_plan(paths, uid=501, env={"HOME": "/Users/anna"})
    assert gone.remove_files == (agent.path,)


def test_linux_systemd_user_unit() -> None:
    paths = linux_paths()
    plan = startup.register_plan([LINUX_EXE], paths, env={"HOME": "/home/anna"})
    (unit,) = plan.files
    assert unit.path == Path("/home/anna/.config/systemd/user/cremind-connect.service")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(unit.content)
    assert parser["Service"]["ExecStart"] == f'"{LINUX_EXE}" "service"'
    assert parser["Service"]["Restart"] == "always" and parser["Service"]["RestartSec"] == "2"
    assert parser["Install"]["WantedBy"] == "default.target"
    assert plan.commands == (("systemctl", "--user", "daemon-reload"),
                             ("systemctl", "--user", "enable", "--now", "cremind-connect.service"))
    xdg = startup.register_plan([LINUX_EXE], paths, env={"HOME": "/home/anna", "XDG_CONFIG_HOME": "/cfg"})
    assert xdg.files[0].path == Path("/cfg/systemd/user/cremind-connect.service")
    gone = startup.unregister_plan(paths, env={"HOME": "/home/anna"})
    assert gone.best_effort == (("systemctl", "--user", "disable", "--now", "cremind-connect.service"),)
    assert gone.cleanup == (("systemctl", "--user", "daemon-reload"),)
    assert startup.stop_plan(paths).best_effort == (("systemctl", "--user", "stop", "cremind-connect.service"),)


def test_systemd_quoting_escapes_specifiers_and_variables() -> None:
    assert startup.systemd_quote('/opt/50% "odd" $HOME\\x') == '"/opt/50%% \\"odd\\" $$HOME\\\\x"'


def test_status_reads_the_registration() -> None:
    xml = startup.task_xml([WIN_EXE], SID, Path("C:/data"))
    state = startup.status(win_paths(), runner=lambda argv: (0, xml))
    assert state.registered and state.target == f'"{WIN_EXE}" service'
    assert not startup.status(win_paths(), runner=lambda argv: (1, "ERROR: not found")).registered


# ---------------------------------------------------------------------------- URL handler


def test_windows_url_handler_registry() -> None:
    plan = urlhandler.register_plan([WIN_EXE], win_paths())
    values = {(v.key, v.name): v.value for v in plan.registry}
    key = r"Software\Classes\cremind-connect"
    assert values[(key, "")] == "URL:Cremind Connect" and values[(key, "URL Protocol")] == ""
    assert values[(key + r"\shell\open\command", "")] == f'"{WIN_EXE}" open "%1"'
    assert values[(key + r"\DefaultIcon", "")] == f'"{WIN_EXE}",0'
    assert urlhandler.unregister_plan(win_paths()).remove_registry == (key,)
    source = urlhandler.windows_open_command([r"C:\py\python.exe", "-m", "cremind_tag.connect"])
    assert source == '"C:\\py\\python.exe" -m cremind_tag.connect open "%1"'


def test_macos_url_handler_is_bundle_managed() -> None:
    plan = urlhandler.register_plan([MAC_EXE], mac_paths())
    assert plan.kind == "bundle" and plan.supported and not plan.files
    app = str(Path("/Users/anna/Applications/Cremind Connect.app"))  # (rendered with this OS's separators)
    assert plan.best_effort == ((urlhandler.LSREGISTER, "-f", app),)
    outside = urlhandler.register_plan(["/usr/bin/python3", "-m", "cremind_tag.connect"], mac_paths())
    assert not outside.supported and "app bundle" in (outside.reason or "")


def test_linux_desktop_file() -> None:
    plan = urlhandler.register_plan(["/opt/Cremind Connect/cremind-connect"], linux_paths(),
                                    env={"HOME": "/home/anna"})
    (desktop,) = plan.files
    assert desktop.path == Path("/home/anna/.local/share/applications/cremind-connect.desktop")
    lines = dict(line.split("=", 1) for line in desktop.content.splitlines() if "=" in line)
    assert lines["Exec"] == '"/opt/Cremind Connect/cremind-connect" open %u'
    assert lines["MimeType"] == "x-scheme-handler/cremind-connect;" and lines["NoDisplay"] == "true"
    assert lines["Type"] == "Application" and lines["Terminal"] == "false"
    assert plan.best_effort == (
        ("xdg-mime", "default", "cremind-connect.desktop", "x-scheme-handler/cremind-connect"),
        ("update-desktop-database", str(Path("/home/anna/.local/share/applications"))))
    gone = urlhandler.unregister_plan(linux_paths(), env={"HOME": "/home/anna"})
    assert gone.remove_files == (desktop.path,)


@pytest.mark.parametrize(("arg", "expected"), [
    ("/usr/bin/cremind-connect", "/usr/bin/cremind-connect"),
    ("/a b/c", '"/a b/c"'),
    ("/x/100%", "/x/100%%"),
    ('/q"uote', '"/q\\\\"uote"'),
    ("/$HOME", '"/\\\\$HOME"'),
])
def test_desktop_exec_quoting(arg: str, expected: str) -> None:
    assert urlhandler.desktop_exec_arg(arg) == expected


# ---------------------------------------------------------------------------- udev


def test_udev_rules() -> None:
    text = udev.rules_text()
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == [
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="0002", TAG+="uaccess"',
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="0001", TAG+="uaccess"',
    ]
    assert udev.RULES_NAME == "70-cremind-tag.rules"


def test_udev_pkexec_install_keeps_paths_out_of_the_script(tmp_path: Path) -> None:
    plan = udev.install_plan(tmp_path / "staging it")
    (staged,) = plan.files
    assert staged.content == udev.rules_text() and staged.mode == 0o644
    ((pkexec, shell, flag, script, name, src, dst),) = plan.commands
    assert (pkexec, shell, flag, name) == ("pkexec", "/bin/sh", "-c", "sh")
    assert src == str(staged.path) and dst == str(Path("/etc/udev/rules.d/70-cremind-tag.rules"))
    assert str(tmp_path) not in script and "udevadm control --reload-rules" in script
    assert re.search(r'install -m 0644 "\$1" "\$2"', script)
    assert "sudo install" in udev.manual_command(tmp_path)


def test_udev_installed_rules(tmp_path: Path) -> None:
    assert udev.installed_rules([tmp_path]) is None
    (tmp_path / udev.RULES_NAME).write_text(udev.rules_text(), encoding="utf-8")
    assert udev.installed_rules([tmp_path]) == tmp_path / udev.RULES_NAME
