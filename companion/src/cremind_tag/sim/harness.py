"""A simulator + connected :class:`GatewayClient` for tests (this repo's, and the daemon's).

::

    async with SimHarness(fontpack=pack, tags=2) as h:
        tag = h.tag_ids[0]
        ack = await h.deliver(tag, layout, revision=1)
        result = await h.wait_result(ack.update_id)       # ResultEvent
        assert result.status == Status.OK

The harness registers one ACK-gating handler that records every event
(``h.events``, ``h.results`` by ``update_id``, ``h.assign_results`` by
``op_id``), so retained events are acknowledged exactly as a daemon's would be.
Tags are generated from the seed and, by default, assigned round-robin to the
bridges at epoch 1 (``K_epoch`` derived from each tag's secret).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ..gateway.client import GatewayClient
from ..gateway.events import AssignResult, GatewayEvent, ResultEvent, StageEvent
from ..gateway.results import Ack
from ..protocol.ids import Panel
from ..protocol.session import derive_k_epoch
from .tag import TagSpec
from .world import Assign, BridgeSpec, SimConfig, Simulator


def run_scenario[T](coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
    """Run one async test scenario on a fresh event loop, bounded by ``timeout`` seconds."""

    async def bounded() -> T:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(bounded())


def make_config(*, fontpack: bytes | None, tags: int = 1, bridges: int = 1, seed: int = 1, time_scale: float = 200.0,
                assign: bool = True, panel: int = Panel.UC8176_420_BW, **overrides: Any) -> SimConfig:
    tag_specs = [TagSpec.generate(seed, i, panel=panel) for i in range(tags)]
    assignments = [Assign(t.tag_id, i % max(1, bridges), 1) for i, t in enumerate(tag_specs)] if assign else []
    return SimConfig(seed=seed, time_scale=time_scale, fontpack=fontpack,
                     bridges=[BridgeSpec() for _ in range(bridges)], tags=tag_specs, assignments=assignments,
                     **overrides)


class SimHarness:
    """Simulator + client + an event recorder (see the module docstring)."""

    def __init__(self, config: SimConfig | None = None, *, fontpack: bytes | None = None,
                 client_options: dict[str, Any] | None = None, handler: Callable[[GatewayEvent], Awaitable[None]]
                 | None = None, **config_options: Any) -> None:
        self.config = config or make_config(fontpack=fontpack, **config_options)
        self.sim = Simulator(self.config)
        self.client_options = {"reconnect": True, "request_timeout": 2.0, **(client_options or {})}
        self.client: GatewayClient | None = None
        self.events: list[GatewayEvent] = []
        self.results: dict[int, ResultEvent] = {}
        self.assign_results: dict[int, AssignResult] = {}
        self.stages: list[StageEvent] = []
        self._extra_handler = handler
        self._changed = asyncio.Event()
        self._update_ids = itertools.count(1000)

    @property
    def tag_ids(self) -> list[int]:
        return [t.tag_id for t in self.config.tags]

    async def __aenter__(self) -> SimHarness:
        await self.sim.start()
        self.client = GatewayClient(self.sim.gateway_url, **self.client_options)
        self.client.add_event_handler(self._record)
        await self.client.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.client is not None:
            await self.client.close()
        await self.sim.stop()

    async def _record(self, event: GatewayEvent) -> None:
        if self._extra_handler is not None:
            await self._extra_handler(event)
        self.events.append(event)
        if isinstance(event, ResultEvent):
            self.results.setdefault(event.update_id, event)
        elif isinstance(event, AssignResult):
            self.assign_results.setdefault(event.op_id, event)
        elif isinstance(event, StageEvent):
            self.stages.append(event)
        self._changed.set()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 30.0) -> None:
        async def loop() -> None:
            while not predicate():
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(loop(), timeout)

    async def wait_result(self, update_id: int, timeout: float = 30.0) -> ResultEvent:
        await self.wait_until(lambda: update_id in self.results, timeout)
        return self.results[update_id]

    async def wait_assign(self, op_id: int, timeout: float = 30.0) -> AssignResult:
        await self.wait_until(lambda: op_id in self.assign_results, timeout)
        return self.assign_results[op_id]

    def tag_bridge(self, tag_id: int) -> int:
        """Mesh address of the bridge the tag was assigned to in the configuration."""
        assign = next(a for a in self.config.assignments if a.tag_id == tag_id)
        addr = self.sim.bridges[assign.bridge].addr
        assert addr is not None
        return addr

    def secret(self, tag_id: int) -> bytes:
        return self.sim.tag(tag_id).spec.secret

    def k_epoch(self, tag_id: int, epoch: int) -> bytes:
        return derive_k_epoch(self.secret(tag_id), tag_id, epoch)

    def new_update_id(self) -> int:
        return next(self._update_ids)

    async def deliver(self, tag_id: int, layout: bytes, revision: int, *, epoch: int = 1, bridge: int | None = None,
                      update_id: int | None = None, op_id: int | None = None) -> DeliveryAck:
        """``DELIVER_LAYOUT`` with the bridge's active font pack id."""
        assert self.client is not None
        addr = bridge if bridge is not None else self.tag_bridge(tag_id)
        pack_id = self.sim.bridge_at(addr).fontpack_id or bytes(8)
        update_id = update_id if update_id is not None else self.new_update_id()
        ack = await self.client.deliver_layout(bridge=addr, tag_id=tag_id, epoch=epoch, revision=revision,
                                               update_id=update_id, fontpack_id=pack_id, layout=layout, op_id=op_id)
        return DeliveryAck(ack, update_id)


class DeliveryAck:
    """``DELIVER_LAYOUT``'s answer plus the ``update_id`` that was used."""

    def __init__(self, ack: Ack, update_id: int) -> None:
        self.ack = ack
        self.update_id = update_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ack, name)

    def __repr__(self) -> str:
        return f"DeliveryAck({self.ack!r}, update_id={self.update_id})"
