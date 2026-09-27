"""Simulator plumbing: a time-scaled clock, seeded random streams, background tasks.

Simulated time runs ``time_scale`` times faster than real time, so a tag's 30 s
wake period takes 0.3 s at ``time_scale=100``. Every protocol timer of the
simulated devices (wake, advertising window, mesh latency, refresh, result
retries, back-off) goes through :class:`SimClock`; the companion's own timeouts
stay real. Simulated durations shorter than the host's timer period are
stretched to it, so reported timings are only faithful when ``time_scale`` keeps
most delays above ~1 ms of real time; on Windows the simulator requests 1 ms timer
resolution while it runs.

Determinism: each component draws from its own ``random.Random`` stream derived
from ``(seed, component name)``, so the *decisions* (ids, nonces, jitter, loss,
fault draws) do not depend on how asyncio interleaves the components. Absolute
timings still depend on the host's scheduler.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import logging
import random
import sys
import threading
import time
from collections.abc import Awaitable, Coroutine
from typing import Any

log = logging.getLogger(__name__)


class SimClock:
    """Simulated milliseconds since the clock was created."""

    def __init__(self, time_scale: float = 1.0) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be positive")
        self.time_scale = time_scale
        self._t0 = time.monotonic()

    def now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0 * self.time_scale

    def real_s(self, sim_ms: float) -> float:
        return max(0.0, sim_ms) / 1000.0 / self.time_scale

    async def sleep_ms(self, sim_ms: float) -> None:
        await asyncio.sleep(self.real_s(sim_ms))

    async def wait_for[T](self, aw: Awaitable[T], sim_ms: float) -> T:
        """``asyncio.wait_for`` with a simulated timeout (raises ``TimeoutError``)."""
        return await asyncio.wait_for(aw, self.real_s(sim_ms))


class Pacer:
    """Accumulates short simulated delays; sleeps once they are worth a real timer tick.

    Used for per-connection-event pacing, where hundreds of sub-millisecond
    sleeps would otherwise each cost a whole timer period of the host.
    """

    def __init__(self, clock: SimClock, min_real_s: float = 0.002) -> None:
        self.clock = clock
        self.min_real_s = min_real_s
        self._debt_ms = 0.0

    async def sleep_ms(self, sim_ms: float) -> None:
        self._debt_ms += sim_ms
        if self.clock.real_s(self._debt_ms) >= self.min_real_s:
            debt, self._debt_ms = self._debt_ms, 0.0
            await self.clock.sleep_ms(debt)
        else:
            await asyncio.sleep(0)


_timer_users = 0
_timer_lock = threading.Lock()


def acquire_timer_resolution() -> None:
    """Ask Windows for 1 ms timer resolution while a simulator runs (default is ~15.6 ms,
    which would stretch every short simulated delay). No-op elsewhere."""
    global _timer_users
    if sys.platform != "win32":
        return
    with _timer_lock:
        if _timer_users == 0:
            with contextlib.suppress(OSError, AttributeError):
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
        _timer_users += 1


def release_timer_resolution() -> None:
    global _timer_users
    if sys.platform != "win32":
        return
    with _timer_lock:
        if _timer_users == 0:
            return
        _timer_users -= 1
        if _timer_users == 0:
            with contextlib.suppress(OSError, AttributeError):
                ctypes.WinDLL("winmm").timeEndPeriod(1)


def rng_stream(seed: int, *parts: object) -> random.Random:
    """An independent, reproducible random stream for one component."""
    label = ":".join(str(p) for p in (seed, *parts)).encode()
    return random.Random(int.from_bytes(hashlib.sha256(label).digest()[:8], "little"))


class TaskSet:
    """Background tasks of one component: logged failures, cancelled together."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str = "") -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=f"{self.owner} {name}".strip())
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("%s: task %s failed", self.owner, task.get_name(), exc_info=task.exception())

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

    def cancel_others(self) -> int:
        """Cancel every task but the caller's (a device reset drops its RAM work and may run inside one of
        its own tasks); returns how many were cancelled."""
        current = asyncio.current_task()
        cancelled = 0
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    def __len__(self) -> int:
        return len(self._tasks)
