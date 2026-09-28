"""Candidate serial ports by USB id, plus extra ids and simulators from the environment."""

from __future__ import annotations

from types import SimpleNamespace

from cremind_tag.connect.usb import EXTRA_PORTS_ENV, list_candidate_ports, parse_extra_ports


def port(device: str, vid: int | None, pid: int | None, serial: str | None = None, description: str = "") -> object:
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial, description=description)


SYSTEM = [
    port("COM3", 0x1366, 0x1015, "000683", "JLink CDC UART Port"),
    port("COM7", 0x1209, 0x0002, "A1B2C3D4", "Cremind Tag gateway"),
    port("COM9", 0x1209, 0x0001, "0011AABB", "Cremind Tag bridge"),
    port("COM4", None, None, None, "Communications Port"),
    port("COM12", 0x2FE3, 0x0004, "DEV1", "USB-DEV"),
]


def test_known_ids_only() -> None:
    ports = list_candidate_ports(env={}, comports=lambda: SYSTEM)
    assert [(p.device, p.role_hint) for p in ports] == [("COM7", "gateway"), ("COM9", "bridge")]
    gateway = ports[0]
    assert gateway.vid == 0x1209 and gateway.pid == 0x0002 and gateway.serial_number == "A1B2C3D4"
    assert gateway.usb_id == "1209:0002" and gateway.key == ("COM7", "A1B2C3D4")
    assert gateway.as_json()["description"] == "Cremind Tag gateway"


def test_extra_ids_and_simulators() -> None:
    env = {EXTRA_PORTS_ENV: " 2fe3:0004 ; socket://127.0.0.1:7777, bogus, 1209:0002"}
    ports = list_candidate_ports(env=env, comports=lambda: SYSTEM)
    by_device = {p.device: p for p in ports}
    assert set(by_device) == {"COM7", "COM9", "COM12", "socket://127.0.0.1:7777"}
    assert by_device["COM12"].role_hint == "unknown"
    assert by_device["COM7"].role_hint == "gateway"  # a known id keeps its role
    sim = by_device["socket://127.0.0.1:7777"]
    assert sim.vid is None and sim.serial_number is None and sim.description == "simulator"


def test_parse_extra_ports() -> None:
    ids, urls = parse_extra_ports("ABCD:0001,socket://h:1;;socket://,xyz")
    assert ids == {(0xABCD, 0x0001): "unknown"} and urls == ["socket://h:1"]
    assert parse_extra_ports(None) == ({}, [])


def test_a_broken_enumeration_is_survived() -> None:
    def broken() -> list[object]:
        raise OSError("driver on fire")

    assert list_candidate_ports(env={}, comports=broken) == []
