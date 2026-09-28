"""Linux: let the logged-in user open Cremind Tag USB serial ports (docs/connect-setup.md §11.5).

``/dev/ttyACM*`` belongs to ``root:dialout`` on most distributions, and adding
the user to ``dialout`` needs a new login. The udev rule ``70-cremind-tag.rules``
tags the gateway and the bridge maintenance port ``uaccess`` instead:
systemd-logind then grants the user sitting at the machine access (ACL) at
once. Its number keeps it before ``73-seat-late.rules``, which applies
``uaccess``. One line per product id works with every udev version::

    SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="0002", TAG+="uaccess"
    SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="0001", TAG+="uaccess"

The ``.deb`` ships the rule in ``/lib/udev/rules.d`` and reloads udev from its
``postinst``. A ``tar.gz`` install has no package manager: :func:`install_plan`
copies the rule to ``/etc/udev/rules.d`` through ``pkexec`` (one password
prompt), reloads the rules and re-triggers attached ttys.

The ids are pid.codes **test** pairs (usb.py); allocate real ones before shipping.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .plan import Plan, PlanFile
from .usb import BRIDGE_MAINT_USB_ID, GATEWAY_USB_ID

RULES_NAME = "70-cremind-tag.rules"
PACKAGE_RULES_DIR = Path("/lib/udev/rules.d")
LOCAL_RULES_DIR = Path("/etc/udev/rules.d")
RULE_DIRS = (LOCAL_RULES_DIR, Path("/usr/lib/udev/rules.d"), PACKAGE_RULES_DIR)


def rules_text(usb_ids: Iterable[tuple[int, int]] = (GATEWAY_USB_ID, BRIDGE_MAINT_USB_ID)) -> str:
    lines = ["# Cremind Tag gateway and bridge maintenance ports: access for the logged-in user (uaccess).",
             "# Installed by Cremind Connect; the ids are pid.codes test pairs until real ones are allocated."]
    for vid, pid in usb_ids:
        lines.append(f'SUBSYSTEM=="tty", ATTRS{{idVendor}}=="{vid:04x}", ATTRS{{idProduct}}=="{pid:04x}", '
                     'TAG+="uaccess"')
    return "\n".join(lines) + "\n"


def installed_rules(dirs: Iterable[Path] = RULE_DIRS) -> Path | None:
    """The installed rule file with the current content, if any."""
    expected = rules_text()
    for directory in dirs:
        path = directory / RULES_NAME
        try:
            if path.read_text(encoding="utf-8") == expected:
                return path
        except OSError:
            continue
    return None


# The shell only sees fixed text; the two paths travel as positional parameters ("$1", "$2").
_INSTALL_SCRIPT = ('install -m 0644 "$1" "$2" && udevadm control --reload-rules && '
                   'udevadm trigger --subsystem-match=tty --action=add')


def install_plan(staging_dir: Path, *, target_dir: Path = LOCAL_RULES_DIR) -> Plan:
    """Install the rule with administrator approval (``pkexec``); ``staging_dir`` is a user-writable directory."""
    staged = Path(staging_dir) / RULES_NAME
    target = Path(target_dir) / RULES_NAME
    return Plan("udev", f"install {target} (asks for an administrator password)",
                files=(PlanFile(staged, rules_text(), mode=0o644),),
                commands=(("pkexec", "/bin/sh", "-c", _INSTALL_SCRIPT, "sh", str(staged), str(target)),))


def manual_command(staging_dir: Path, *, target_dir: Path = LOCAL_RULES_DIR) -> str:
    """What to run by hand when ``pkexec`` is missing or refused."""
    staged = Path(staging_dir) / RULES_NAME
    return (f"sudo install -m 0644 '{staged}' '{Path(target_dir) / RULES_NAME}' && "
            "sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty --action=add")


__all__ = ["LOCAL_RULES_DIR", "PACKAGE_RULES_DIR", "RULES_NAME", "install_plan", "installed_rules",
           "manual_command", "rules_text"]
