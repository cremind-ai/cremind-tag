"""Identifying the device on a port: a fake v2 gateway, the simulator's v1 gateway, silent and busy ports."""

from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from cremind_tag.connect import probe
from cremind_tag.protocol.ids import PROTO_VERSION, SERIAL_MAX_FRAME, NodeRole, OwnerState, SerialMsg, Status
from cremind_tag.secure.device import DeviceKeys, OwnerRecord, SecureDevice
from cremind_tag.sim import SimConfig, SimulatorThread
from cremind_tag.sim.device import DeviceEndpoint

LoopThread = Any  # the conftest's loop_thread fixture


class FakeV2Gateway:
    """A serial endpoint answering HELLO and plaintext IDENTIFY like v2 gateway firmware."""

    def __init__(self, role: NodeRole = NodeRole.GATEWAY, record: OwnerRecord | None = None,
                 retained_events: int = 0) -> None:
        self.device = SecureDevice(DeviceKeys.generate(role), record)
        self.identifies = 0
        self.acks = 0
        self.endpoint = DeviceEndpoint("v2 gateway", self.handle, supported=(SerialMsg.IDENTIFY, SerialMsg.PING))
        self._retained = retained_events

    async def handle(self, msg: SerialMsg, fields: dict[str, Any]) -> dict[str, Any]:
        if msg == SerialMsg.HELLO:
            return {"status": Status.OK, "proto": PROTO_VERSION, "fw": "0.2.0", "build": "test", "boot_id": 7,
                    "caps": {"max_frame": SERIAL_MAX_FRAME, "credits": 4, "role": NodeRole.GATEWAY}}
        if msg == SerialMsg.IDENTIFY:
            self.identifies += 1
            return self.device.identify_fields(fw="0.2.0", build="test")
        return {"status": Status.UNSUPPORTED}

    async def start(self) -> str:
        url = await self.endpoint.start()
        for n in range(self._retained):  # retained events the probe must ignore (and never acknowledge)
            self.endpoint.emit(SerialMsg.EVT_NODE_REMOVED, {"op_id": n + 1, "addr": 2, "status": 0}, retained=True)
        return url


@pytest.fixture
def v2(loop_thread: LoopThread) -> Iterator[tuple[FakeV2Gateway, str]]:
    gateway = FakeV2Gateway(retained_events=16)
    url = loop_thread.run(gateway.start())
    yield gateway, url
    loop_thread.run(gateway.endpoint.stop())


def test_v2_gateway_identity(v2: tuple[FakeV2Gateway, str]) -> None:
    gateway, url = v2
    result = probe.probe_port(url)
    assert result.ok and result.reason is None and result.hello_fw == "0.2.0"
    identity = result.identity
    assert identity is not None
    assert identity.device_id == gateway.device.device_id and identity.ik == gateway.device.keys.ik_pub
    assert identity.is_gateway and identity.role_name == "gateway" and identity.proto == 2
    assert identity.owner_state == OwnerState.UNOWNED and identity.gen == 0 and identity.authority_id is None
    assert identity.challenge == gateway.device.challenge and len(identity.challenge) == 16
    assert gateway.endpoint.retained_seqs == list(range(1, 17))  # nothing acknowledged
    data = result.as_json(include_challenge=False)
    assert data["identity"]["device_id"] == identity.device_id_hex and "challenge" not in data["identity"]


def test_every_probe_draws_a_fresh_challenge(v2: tuple[FakeV2Gateway, str]) -> None:
    gateway, url = v2
    first = probe.probe_port(url).identity
    second = probe.probe_port(url).identity
    assert first is not None and second is not None and first.challenge != second.challenge
    assert gateway.identifies == 2


def test_owned_gateway_reports_its_authority(loop_thread: LoopThread) -> None:
    record = OwnerRecord(OwnerState.OWNED, 3, b"\x11" * 32, b"\x22" * 16, b"\x33" * 32)
    gateway = FakeV2Gateway(record=record)
    url = loop_thread.run(gateway.start())
    try:
        identity = probe.probe_port(url).identity
    finally:
        loop_thread.run(gateway.endpoint.stop())
    assert identity is not None and identity.owner_state == OwnerState.OWNED and identity.gen == 3
    assert identity.authority_id is not None and identity.owner_state_name == "owned"


def test_bridge_maintenance_port_is_identified_as_a_bridge(loop_thread: LoopThread) -> None:
    bridge = FakeV2Gateway(role=NodeRole.BRIDGE)
    url = loop_thread.run(bridge.start())
    try:
        identity = probe.probe_port(url).identity
    finally:
        loop_thread.run(bridge.endpoint.stop())
    assert identity is not None and not identity.is_gateway and identity.role_name == "bridge"


def test_v1_simulator_gateway_is_v1_firmware() -> None:
    with SimulatorThread(SimConfig(bridges=[])) as sim:
        result = probe.probe_port(sim.gateway_url)
    assert result.identity is None and result.reason == probe.V1_FIRMWARE
    assert result.hello_fw == "0.1.0"


def test_inconsistent_identity_is_an_error(loop_thread: LoopThread) -> None:
    gateway = FakeV2Gateway()
    original = gateway.device.identify_fields

    def lying(**kwargs: Any) -> dict[str, Any]:
        fields = original(**kwargs)
        fields["device_id"] = bytes(16)
        return fields

    gateway.device.identify_fields = lying  # type: ignore[method-assign]
    url = loop_thread.run(gateway.start())
    try:
        result = probe.probe_port(url)
    finally:
        loop_thread.run(gateway.endpoint.stop())
    assert result.reason == probe.ERROR and "device id" in result.detail


@pytest.fixture
def silent_server() -> Iterator[str]:
    """Accepts connections and never answers (a port with some other device behind it)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    conns: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conns.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"socket://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join(2)
    for conn in conns:
        conn.close()
    server.close()


def test_no_answer(silent_server: str) -> None:
    started = time.monotonic()
    result = probe.probe_port(silent_server, timeout=0.3)
    assert result.reason == probe.NO_ANSWER and "HELLO" in result.detail
    assert time.monotonic() - started < 3


def test_nothing_listening_is_no_answer() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert probe.probe_port(f"socket://127.0.0.1:{free}").reason == probe.NO_ANSWER


class _Exc(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.errno = code


@pytest.mark.parametrize(("message", "code", "expected"), [
    ("Could not exclusively lock port /dev/ttyACM0: [Errno 11]", errno.EAGAIN, probe.BUSY),
    ("could not open port /dev/ttyACM0: [Errno 16] Device or resource busy", errno.EBUSY, probe.BUSY),
    ("could not open port /dev/ttyACM0: [Errno 13] Permission denied", errno.EACCES,
     probe.BUSY if sys.platform == "win32" else probe.NO_ACCESS),
    ("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.')", None,
     probe.GONE),
])
def test_open_failures_are_classified(message: str, code: int | None, expected: str) -> None:
    def opener(_device: str) -> Any:
        raise _Exc(message, code)

    result = probe.probe_port("/dev/ttyACM0", opener=opener)
    assert result.reason == expected and result.identity is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows wording")
def test_windows_access_denied_means_in_use() -> None:
    exc = _Exc("could not open port 'COM7': PermissionError(13, 'Access is denied.', None, 5)")
    assert probe.classify_open_error("COM7", exc) == probe.BUSY


def test_the_port_is_always_closed(v2: tuple[FakeV2Gateway, str]) -> None:
    _gateway, url = v2
    closed: list[bool] = []

    def opener(device: str) -> Any:
        port = probe.open_port(device)
        original = port.close

        def close() -> None:
            closed.append(True)
            original()

        port.close = close
        return port

    assert probe.probe_port(url, opener=opener).ok and closed == [True]

    def failing_opener(device: str) -> Any:
        port = opener(device)
        port.write = lambda _data: (_ for _ in ()).throw(OSError(errno.EIO, os.strerror(errno.EIO)))
        return port

    result = probe.probe_port(url, opener=failing_opener)
    assert result.reason == probe.NO_ANSWER and closed == [True, True]
