"""Crash injection at the daemon's durability boundaries (tests only; a no-op in production).

Every place where the daemon moves from "decided" to "durable" to "told
someone" calls :meth:`CrashPoints.hit` with a name from :data:`BOUNDARIES`.
A test arms one boundary; reaching it raises :class:`SimulatedCrash` — a
``BaseException``, so no ``except Exception`` in the daemon (or in the gateway
client's handler pipeline) can swallow it — and the service tears everything
down without any graceful flush, like a killed process. The test then starts
a new service on the same database and checks nothing was lost.
"""

from __future__ import annotations

from collections.abc import Callable

BOUNDARIES = (
    "events_fetched",        # an events page arrived; nothing committed yet
    "events_committed",      # jobs + cursor + `accepted` committed; not POSTed yet
    "revision_persisted",    # a revision (with its op_id) committed; DELIVER_LAYOUT not sent yet
    "deliver_sent",          # DELIVER_LAYOUT answered; the answer not recorded, no result yet
    "handler_before_commit", # a retained gateway event is being handled; its transaction not committed
    "result_committed",      # a result's receipts committed to the outbox; not POSTed yet
    "before_receipt_post",   # the outbox is about to POST receipts
    "command_claimed",       # a hardware command is claimed; nothing executed yet
)


class SimulatedCrash(BaseException):
    """Raised at an armed boundary (never caught by the daemon's error handling)."""


class CrashPoints:
    """Armed boundaries; ``hit(name)`` raises once the boundary's countdown reaches zero."""

    def __init__(self) -> None:
        self._armed: dict[str, int] = {}
        self.hits: dict[str, int] = {}
        self.fired: list[str] = []
        self.on_crash: Callable[[SimulatedCrash], None] | None = None

    def arm(self, name: str, *, after: int = 0) -> None:
        """Crash at the ``after + 1``-th time ``name`` is reached."""
        if name not in BOUNDARIES:
            raise ValueError(f"unknown crash boundary {name!r}")
        self._armed[name] = after

    def disarm(self) -> None:
        self._armed.clear()

    def hit(self, name: str) -> None:
        self.hits[name] = self.hits.get(name, 0) + 1
        remaining = self._armed.get(name)
        if remaining is None:
            return
        if remaining > 0:
            self._armed[name] = remaining - 1
            return
        del self._armed[name]
        self.fired.append(name)
        crash = SimulatedCrash(name)
        if self.on_crash is not None:
            self.on_crash(crash)
        raise crash


__all__ = ["BOUNDARIES", "CrashPoints", "SimulatedCrash"]
