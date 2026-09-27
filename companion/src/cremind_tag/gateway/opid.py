"""Idempotency keys (``op_id``, u64) for side-effecting requests (docs/protocol.md §1.4).

The gateway remembers the last ``SERIAL_IDEMPOTENCY_SLOTS`` op ids and answers a
repeat with the remembered status, so an op id must never be reused for a
different operation — including by a companion restarted a second later. Ids are
``unix_ms << 20 | 20 random bits``, forced strictly increasing within a process:
unique across restarts (the clock moves on and the random part separates two
processes in the same millisecond) and valid until the year 2527.

Callers that must retry an operation after a crash (the delivery queue) persist
the op id with the job and pass it back explicitly.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

_RANDOM_BITS = 20
_U64 = (1 << 64) - 1


class OpIdGenerator:
    """Monotonic, random-suffixed u64 op ids."""

    def __init__(self, clock_ms: Callable[[], int] | None = None,
                 random_bits: Callable[[int], int] | None = None) -> None:
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._random_bits = random_bits or secrets.randbits
        self._last = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            candidate = ((self._clock_ms() << _RANDOM_BITS) | self._random_bits(_RANDOM_BITS)) & _U64
            if candidate <= self._last:
                candidate = self._last + 1
            if candidate == 0 or candidate > _U64:
                raise OverflowError("op id space exhausted")
            self._last = candidate
            return candidate


_default = OpIdGenerator()


def new_op_id() -> int:
    """A fresh op id from the process-wide generator."""
    return _default.next()
