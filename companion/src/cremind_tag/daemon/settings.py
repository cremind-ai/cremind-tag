"""The ``[daemon]`` configuration section: loop cadences, retry bounds, bridge maintenance ports.

::

    [daemon]
    active_poll_s = 2.0            # events poll while jobs keep arriving
    idle_poll_s = 10.0             # ... backing off to this when idle
    heartbeat_s = 30.0
    resync_s = 300.0               # periodic `sync` (reconciles cancellations)
    bridge_maintenance = ["br-0011…eeff=COM9"]   # install_fontpack: bridge hw id -> maintenance port

Every value has a ``CREMIND_TAG_DAEMON_<NAME>`` environment override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ..config import ENV_PREFIX, ConfigError, register_section


@register_section
@dataclass
class DaemonSettings:
    """Delivery daemon tuning (defaults suit real hardware; tests shrink them)."""

    SECTION: ClassVar[str] = "daemon"
    ENV_PREFIX: ClassVar[str] = ENV_PREFIX + "DAEMON_"

    active_poll_s: float = 2.0
    """Events poll interval right after jobs arrived."""
    idle_poll_s: float = 10.0
    """Events poll interval once the feed is idle (the interval doubles up to this)."""
    heartbeat_s: float = 30.0
    """Hardware heartbeat interval."""
    resync_s: float = 300.0
    """Periodic ``sync`` per content credential (reconciles deliveries Cremind cancelled)."""
    command_wait_s: int = 25
    """Long-poll wait of ``GET commands`` (at most 30)."""
    scan_interval_s: float = 2.0
    """How often the screen scheduler re-reads the queue for changes made by other processes (the CLI)."""
    retry_initial_s: float = 5.0
    """First delay before re-delivering after a link-level failure."""
    retry_max_s: float = 600.0
    """Longest delay between re-deliveries (the job's TTL bounds the total)."""
    connector_retry_max_s: float = 60.0
    """Longest back-off after a Cremind request failed transiently."""
    tls_retry_s: float = 300.0
    """Retry interval after a TLS configuration error (untrusted certificate, server moved to HTTPS)."""
    result_timeout_s: float = 1800.0
    """A delivery sent without any result for this long is sent again (the gateway may have lost it)."""
    identify_hold_s: float = 60.0
    """How long an ``identify`` screen stays before the regular screen returns."""
    uncertain_retries: int = 5
    """Re-deliveries after ``DISPLAY_STATE_UNKNOWN`` before waiting for the TTL at the retry cap."""
    bridge_maintenance: list[str] = field(default_factory=list)
    """``br-<uuid>=<port>`` entries: maintenance ports for ``install_fontpack``."""
    log_max_bytes: int = 5_000_000
    """Size of one daemon log file before it rotates (3 files are kept)."""

    def validate(self) -> None:
        for name in ("active_poll_s", "idle_poll_s", "heartbeat_s", "resync_s", "scan_interval_s",
                     "retry_initial_s", "retry_max_s", "connector_retry_max_s", "result_timeout_s",
                     "tls_retry_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"daemon.{name} must be positive")
        if self.idle_poll_s < self.active_poll_s:
            raise ConfigError("daemon.idle_poll_s must be >= daemon.active_poll_s")
        if not 0 <= self.command_wait_s <= 30:
            raise ConfigError("daemon.command_wait_s must be between 0 and 30")
        for entry in self.bridge_maintenance:
            hw_id, sep, port = entry.partition("=")
            if not sep or not hw_id.strip().startswith("br-") or not port.strip():
                raise ConfigError(f"daemon.bridge_maintenance entries look like 'br-<uuid>=COM9' (got {entry!r})")

    def maintenance_port(self, bridge_hw_id: str) -> str | None:
        for entry in self.bridge_maintenance:
            hw_id, _, port = entry.partition("=")
            if hw_id.strip().lower() == bridge_hw_id.strip().lower():
                return port.strip()
        return None


__all__ = ["DaemonSettings"]
