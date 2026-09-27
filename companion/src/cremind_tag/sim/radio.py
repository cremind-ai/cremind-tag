"""BLE model between simulated bridges (centrals) and tags (peripherals), docs/protocol.md §5.1–§5.3.

- :class:`Advert` is the tag's legacy advertising payload (Flags + Manufacturer
  Specific Data, §5.1), encoded to its on-air AD bytes and parsed back by the
  scanner.
- :class:`Air` hands every advertisement to the bridges that are scanning (a
  bridge whose mesh is suspended is not), and connects a central to a tag that is
  inside its advertising window: the connection completes at the tag's next
  advertising event, or fails after the attempt timeout.
- :class:`GattLink` is one connection: ATT values in each direction (the CAPS
  read, CTRL writes/indications, DATA writes without response, STATUS
  notifications) and a disconnect that either side can trigger. Values are at
  most ``ATT_VALUE_MAX`` bytes (ATT MTU 23), so the real fragmentation layer runs
  on every message. Connection-event pacing is applied by the bridge (at most 4
  records per connection event, §5.2 step 8).

Not modelled: RF propagation and collisions (every bridge hears every tag with a
fixed per-pair RSSI), channel maps, supervision timeouts, PHY/MTU negotiation.
"""

from __future__ import annotations

import asyncio
import random
import struct
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..protocol.ids import ATT_VALUE_MAX, MESH_COMPANY_ID, TAG_ADV_INTERVAL_MS, GattChr
from .core import SimClock

ADV_VERSION = 1
_MSD = struct.Struct("<HBIBH")  # company, ver, tag_id, flags, disp_rev

ADV_FLAG_RESULT_PENDING = 0x01
ADV_FLAG_LOW_BATTERY = 0x02
ADV_FLAG_UNKNOWN_STATE = 0x04


class LinkLost(ConnectionError):
    """The BLE connection dropped."""


class ConnectFailed(ConnectionError):
    """``bt_conn_le_create`` did not complete within the attempt timeout."""


@dataclass(frozen=True, slots=True)
class Advert:
    tag_id: int
    flags: int
    disp_rev: int  # low 16 bits of the displayed revision

    def to_bytes(self) -> bytes:
        msd = _MSD.pack(MESH_COMPANY_ID, ADV_VERSION, self.tag_id, self.flags, self.disp_rev & 0xFFFF)
        return bytes([2, 0x01, 0x06, len(msd) + 1, 0xFF]) + msd

    @classmethod
    def parse(cls, data: bytes) -> Advert | None:
        """Our advertisement, or ``None`` for anything else."""
        pos = 0
        while pos + 1 < len(data):
            length = data[pos]
            if length == 0 or pos + 1 + length > len(data):
                return None
            ad_type, body = data[pos + 1], data[pos + 2 : pos + 1 + length]
            if ad_type == 0xFF and len(body) == _MSD.size:
                company, version, tag_id, flags, disp_rev = _MSD.unpack(body)
                if company == MESH_COMPANY_ID and version == ADV_VERSION:
                    return cls(tag_id, flags, disp_rev)
            pos += 1 + length
        return None


class Peripheral(Protocol):
    tag_id: int

    def connectable(self) -> bool: ...

    def accept(self, link: GattLink) -> None: ...

    def read_characteristic(self, chr: GattChr) -> bytes: ...


class Scanner(Protocol):
    def scanning(self) -> bool: ...

    def on_advert(self, data: bytes, rssi: int) -> None: ...


class GattLink:
    """One BLE connection between a bridge (central) and a tag (peripheral)."""

    def __init__(self, central: str, peripheral: Peripheral) -> None:
        self.central = central
        self.peripheral = peripheral
        self.connected = True
        self.reason: str | None = None
        self._to_tag: asyncio.Queue[tuple[GattChr, bytes] | None] = asyncio.Queue()
        self._to_bridge: asyncio.Queue[tuple[GattChr, bytes] | None] = asyncio.Queue()
        self._on_disconnect: list[Callable[[str], None]] = []

    def on_disconnect(self, callback: Callable[[str], None]) -> None:
        self._on_disconnect.append(callback)

    def disconnect(self, reason: str) -> None:
        if not self.connected:
            return
        self.connected = False
        self.reason = reason
        self._to_tag.put_nowait(None)
        self._to_bridge.put_nowait(None)
        for callback in self._on_disconnect:
            callback(reason)

    def _check(self, value: bytes) -> None:
        if not self.connected:
            raise LinkLost(self.reason or "disconnected")
        if not 1 <= len(value) <= ATT_VALUE_MAX:
            raise ValueError(f"ATT value of {len(value)} bytes")

    # central side
    async def read(self, chr: GattChr) -> bytes:
        if not self.connected:
            raise LinkLost(self.reason or "disconnected")
        return self.peripheral.read_characteristic(chr)

    async def write(self, chr: GattChr, value: bytes) -> None:
        self._check(value)
        self._to_tag.put_nowait((chr, bytes(value)))

    async def central_recv(self, clock: SimClock, timeout_ms: float) -> tuple[GattChr, bytes]:
        item = await clock.wait_for(self._to_bridge.get(), timeout_ms)
        if item is None:
            self._to_bridge.put_nowait(None)
            raise LinkLost(self.reason or "disconnected")
        return item

    # peripheral side
    def notify(self, chr: GattChr, value: bytes) -> None:
        self._check(value)
        self._to_bridge.put_nowait((chr, bytes(value)))

    async def peripheral_recv(self, clock: SimClock, timeout_ms: float) -> tuple[GattChr, bytes]:
        item = await clock.wait_for(self._to_tag.get(), timeout_ms)
        if item is None:
            self._to_tag.put_nowait(None)
            raise LinkLost(self.reason or "disconnected")
        return item


@dataclass
class AirFaults:
    connect_fail: float = 0.0  # probability that a connection attempt fails although the tag advertises


class Air:
    """Advertising and connection establishment (see the module docstring)."""

    def __init__(self, clock: SimClock, rng: random.Random, faults: AirFaults | None = None) -> None:
        self.clock = clock
        self.rng = rng
        self.faults = faults or AirFaults()
        self.tags: dict[int, Peripheral] = {}
        self.scanners: list[Scanner] = []
        self._rssi: dict[tuple[int, int], int] = {}
        self.counters: Counter[str] = Counter()

    def rssi(self, scanner: Scanner, tag_id: int) -> int:
        key = (id(scanner), tag_id)
        if key not in self._rssi:
            self._rssi[key] = self.rng.randint(-85, -45)
        return self._rssi[key]

    def advertise(self, tag_id: int, data: bytes) -> None:
        self.counters["adverts"] += 1
        for scanner in list(self.scanners):
            if scanner.scanning():
                scanner.on_advert(data, self.rssi(scanner, tag_id))

    async def connect(self, central: str, tag_id: int, timeout_ms: float) -> GattLink:
        """Initiate towards ``tag_id``; the connection completes at its next advertising event."""
        self.counters["connect_attempts"] += 1
        deadline = self.clock.now_ms() + timeout_ms
        tag = self.tags.get(tag_id)
        while tag is not None and self.clock.now_ms() < deadline:
            if tag.connectable():
                await self.clock.sleep_ms(min(self.rng.uniform(0, TAG_ADV_INTERVAL_MS),
                                              max(0.0, deadline - self.clock.now_ms())))
                if self.faults.connect_fail > 0 and self.rng.random() < self.faults.connect_fail:
                    break
                if tag.connectable():
                    link = GattLink(central, tag)
                    tag.accept(link)
                    self.counters["connections"] += 1
                    return link
            else:
                await self.clock.sleep_ms(min(50.0, max(0.0, deadline - self.clock.now_ms())))
        self.counters["connect_failed"] += 1
        raise ConnectFailed(f"tag {tag_id:08X} did not accept a connection")
