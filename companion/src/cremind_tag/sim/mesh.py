"""Bluetooth Mesh model between the simulated gateway and bridges (docs/protocol.md §2–§3).

Messages are real vendor-model PDUs: the 3-octet opcode ``(0xC0 | op),
MESH_COMPANY_ID`` followed by parameters packed with the generated
``protocol.msgs`` codecs (never more than ``MESH_MAX_VENDOR_PARAMS``); the
receiver parses them back, so every hop exercises the wire format.

What is modelled: per-message latency growing with the number of lower-transport
segments; the sender's ``end`` callback (``send`` returns whether the segments
were acknowledged); a destination whose mesh is suspended for a BLE connection
(§5.2) receives the message once it resumes, or the send fails if the window is
longer than the segment retransmission budget; access-layer loss of
``LAYOUT_CHUNK`` (probability or explicit chunk indices, the lower transport
still acknowledges — which is how ``LAYOUT_STATUS INCOMPLETE`` arises) and of
results/acks; failed sends. Not modelled: radio propagation, relaying, TTL,
network/IV-index/sequence-number handling, provisioning PDUs, keys.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from ..protocol.ids import MESH_COMPANY_ID, MESH_MAX_VENDOR_PARAMS, MeshOp
from ..protocol.msgs import MESH_MESSAGES, FixedMessage, MeshDeliveryResult, MeshLayoutChunk, MeshResultAck
from .core import SimClock

log = logging.getLogger(__name__)

GATEWAY_ADDR = 0x0001
UNSEGMENTED_MAX = 11  # access PDU bytes that fit one unsegmented message (4-byte TransMIC)
SEGMENT_PAYLOAD = 12


class MeshPduError(ValueError):
    """A PDU that is not one of our vendor messages."""


def pack_pdu(msg: FixedMessage) -> bytes:
    op: int = msg.TYPE  # type: ignore[attr-defined]
    params = msg.pack()
    if len(params) > MESH_MAX_VENDOR_PARAMS:
        raise MeshPduError(f"{MeshOp(op).name}: {len(params)} parameter bytes > {MESH_MAX_VENDOR_PARAMS}")
    return bytes([0xC0 | op]) + MESH_COMPANY_ID.to_bytes(2, "little") + params


def parse_pdu(pdu: bytes) -> FixedMessage:
    if len(pdu) < 3 or pdu[0] & 0xC0 != 0xC0 or int.from_bytes(pdu[1:3], "little") != MESH_COMPANY_ID:
        raise MeshPduError("not a vendor opcode of this company")
    try:
        op = MeshOp(pdu[0] & 0x3F)
    except ValueError:
        raise MeshPduError(f"unknown vendor op {pdu[0] & 0x3F:#04x}") from None
    return MESH_MESSAGES[op].unpack(pdu[3:])


def segments(pdu_len: int) -> int:
    return 1 if pdu_len <= UNSEGMENTED_MAX else math.ceil(pdu_len / SEGMENT_PAYLOAD)


class MeshNode(Protocol):
    """A node the network can deliver to."""

    @property
    def mesh_suspended(self) -> bool: ...

    async def wait_mesh_resumed(self) -> None: ...

    def mesh_accepts(self, msg: FixedMessage) -> bool: ...

    def mesh_receive(self, src: int, msg: FixedMessage) -> None: ...


@dataclass
class MeshFaults:
    """Access-layer loss and send failures (probabilities are per message)."""

    chunk_loss: float = 0.0
    drop_chunks: set[int] = field(default_factory=set)  # chunk indices dropped once each
    result_loss: float = 0.0  # DELIVERY_RESULT and RESULT_ACK
    send_fail: float = 0.0  # end callback reports failure


@dataclass
class MeshTiming:
    base_ms: float = 15.0
    segment_ms: float = 12.0
    suspend_tolerance_ms: float = 3000.0  # lower-transport retransmissions cover a suspend window this long
    unreachable_ms: float = 2000.0


class MeshNetwork:
    """Delivers vendor messages between attached nodes (see the module docstring)."""

    def __init__(self, clock: SimClock, rng: random.Random, faults: MeshFaults | None = None,
                 timing: MeshTiming | None = None) -> None:
        self.clock = clock
        self.rng = rng
        self.faults = faults or MeshFaults()
        self.timing = timing or MeshTiming()
        self.nodes: dict[int, MeshNode] = {}
        self.counters: Counter[str] = Counter()

    def attach(self, addr: int, node: MeshNode) -> None:
        self.nodes[addr] = node

    def detach(self, addr: int) -> None:
        self.nodes.pop(addr, None)

    def _lost(self, msg: FixedMessage) -> bool:
        faults = self.faults
        if isinstance(msg, MeshLayoutChunk):
            if msg.index in faults.drop_chunks:
                faults.drop_chunks.discard(msg.index)
                return True
            return faults.chunk_loss > 0 and self.rng.random() < faults.chunk_loss
        if isinstance(msg, MeshDeliveryResult | MeshResultAck):
            return faults.result_loss > 0 and self.rng.random() < faults.result_loss
        return False

    async def send(self, src: int, dst: int, msg: FixedMessage) -> bool:
        """Send one message; ``True`` when the destination's lower transport acknowledged it."""
        pdu = pack_pdu(msg)
        name = type(msg).__name__
        self.counters["sent"] += 1
        self.counters[f"sent_{name}"] += 1
        await self.clock.sleep_ms(self.timing.base_ms + segments(len(pdu)) * self.timing.segment_ms)
        node = self.nodes.get(dst)
        if node is None:
            self.counters["unreachable"] += 1
            await self.clock.sleep_ms(self.timing.unreachable_ms)
            return False
        if node.mesh_suspended:
            self.counters["waited_for_resume"] += 1
            try:
                await self.clock.wait_for(node.wait_mesh_resumed(), self.timing.suspend_tolerance_ms)
            except TimeoutError:
                self.counters["suspend_timeouts"] += 1
                return False
        if self.faults.send_fail > 0 and self.rng.random() < self.faults.send_fail:
            self.counters["send_failed"] += 1
            return False
        if not node.mesh_accepts(msg):
            self.counters["rejected"] += 1
            return False
        if self._lost(msg):
            self.counters[f"lost_{name}"] += 1
            return True  # segments were acknowledged; the access layer dropped it
        decoded = parse_pdu(pdu)
        node.mesh_receive(src, decoded)
        return True

    async def settle(self) -> None:
        """Yield once so freshly delivered messages start being handled."""
        await asyncio.sleep(0)
