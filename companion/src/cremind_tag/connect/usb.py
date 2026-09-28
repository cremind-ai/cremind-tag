"""Serial ports that may be a Cremind Tag gateway (or a bridge's maintenance port).

Candidates are USB serial ports whose VID:PID is a Cremind Tag device's:

========  ===========  ============================================================
VID:PID   role hint    source
========  ===========  ============================================================
1209:0002 ``gateway``  docs/gateway-firmware.md (pid.codes **test** pair: replace
                       before shipping, together with udev.py)
1209:0001 ``bridge``   docs/bridge-firmware.md (maintenance port, test pair)
========  ===========  ============================================================

The role is only a hint: :mod:`.probe` asks the device itself (``IDENTIFY``).
A COM port or ``/dev`` path is an *observation*, never an identity — the
device id is (docs/connect-setup.md §2.1).

``CREMIND_CONNECT_EXTRA_PORTS`` adds candidates, comma separated: ``VID:PID``
pairs in hex (development boards with other USB ids) and ``socket://host:port``
simulator endpoints, e.g. ``2fe3:0004,socket://127.0.0.1:7777``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

EXTRA_PORTS_ENV = "CREMIND_CONNECT_EXTRA_PORTS"
GATEWAY_USB_ID = (0x1209, 0x0002)
BRIDGE_MAINT_USB_ID = (0x1209, 0x0001)
KNOWN_USB_IDS: dict[tuple[int, int], str] = {GATEWAY_USB_ID: "gateway", BRIDGE_MAINT_USB_ID: "bridge"}
_USB_ID = re.compile(r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})")


@dataclass(frozen=True)
class PortInfo:
    """One candidate port."""

    device: str
    """What pyserial opens: ``COM7``, ``/dev/ttyACM0``, ``/dev/cu.usbmodem14101``, ``socket://127.0.0.1:7777``."""
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str
    role_hint: str
    """``gateway``, ``bridge`` or ``unknown`` (extra ids, simulators)."""

    @property
    def key(self) -> tuple[str, str | None]:
        """What identifies "the same port with the same device" between scans."""
        return (self.device, self.serial_number)

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}" if self.vid is not None and self.pid is not None else ""

    def as_json(self) -> dict[str, Any]:
        return {"device": self.device, "vid": self.vid, "pid": self.pid, "serial_number": self.serial_number,
                "description": self.description, "role_hint": self.role_hint}


def parse_extra_ports(text: str | None) -> tuple[dict[tuple[int, int], str], list[str]]:
    """``(extra USB ids, simulator URLs)`` from ``CREMIND_CONNECT_EXTRA_PORTS``; bad entries are logged and skipped."""
    usb_ids: dict[tuple[int, int], str] = {}
    urls: list[str] = []
    for raw in re.split(r"[,;]", text or ""):
        entry = raw.strip()
        if not entry:
            continue
        if match := _USB_ID.fullmatch(entry):
            usb_ids[(int(match[1], 16), int(match[2], 16))] = "unknown"
        elif entry.lower().startswith("socket://") and len(entry) > len("socket://"):
            urls.append(entry)
        else:
            log.warning("usb: ignoring %s entry %r (expected VID:PID or socket://host:port)", EXTRA_PORTS_ENV, entry)
    return usb_ids, urls


def _system_ports() -> Iterable[Any]:
    from serial.tools import list_ports

    return list_ports.comports()


def list_candidate_ports(env: Mapping[str, str] | None = None,
                         comports: Callable[[], Iterable[Any]] | None = None) -> list[PortInfo]:
    """Candidate ports, sorted by device (``comports`` is for tests: objects shaped like pyserial's)."""
    env = os.environ if env is None else env
    extra_ids, urls = parse_extra_ports(env.get(EXTRA_PORTS_ENV))
    wanted = {**KNOWN_USB_IDS, **{k: v for k, v in extra_ids.items() if k not in KNOWN_USB_IDS}}
    ports: list[PortInfo] = []
    try:
        system = list((comports or _system_ports)())
    except Exception as exc:  # a broken driver must not stop the service
        log.warning("usb: cannot list serial ports: %s", exc)
        system = []
    for port in system:
        vid, pid = getattr(port, "vid", None), getattr(port, "pid", None)
        if vid is None or pid is None or (vid, pid) not in wanted:
            continue
        ports.append(PortInfo(str(port.device), int(vid), int(pid), getattr(port, "serial_number", None) or None,
                              str(getattr(port, "description", "") or ""), wanted[(vid, pid)]))
    ports += [PortInfo(url, None, None, None, "simulator", "unknown") for url in urls]
    return sorted(ports, key=lambda p: p.device)


__all__ = ["BRIDGE_MAINT_USB_ID", "EXTRA_PORTS_ENV", "GATEWAY_USB_ID", "KNOWN_USB_IDS", "PortInfo",
           "list_candidate_ports", "parse_extra_ports"]
