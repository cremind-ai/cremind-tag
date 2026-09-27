"""The delivery daemon (docs/companion.md): Cremind feed -> durable SQLite queue -> screens -> gateway -> receipts.

- :mod:`.service` — :class:`DaemonService` / :class:`DaemonOptions`, the composition root;
- :mod:`.store` — :class:`QueueStore`, every state change as one transaction;
- :mod:`.schema` — the queue's migrations (companion database v2+) and :func:`open_database`;
- :mod:`.content`, :mod:`.hardware`, :mod:`.commands`, :mod:`.screens`, :mod:`.events`, :mod:`.outbox` — the loops;
- :mod:`.validator` — the card validator (defence in depth);
- :mod:`.crash` — crash injection at each durability boundary (tests).
"""

from .crash import BOUNDARIES, CrashPoints, SimulatedCrash
from .schema import QUEUE_MIGRATIONS, open_database
from .service import DaemonConfigError, DaemonOptions, DaemonService, read_status
from .settings import DaemonSettings
from .store import QueueStore

__all__ = [
    "BOUNDARIES", "QUEUE_MIGRATIONS", "CrashPoints", "DaemonConfigError", "DaemonOptions", "DaemonService",
    "DaemonSettings", "QueueStore", "SimulatedCrash", "open_database", "read_status",
]
