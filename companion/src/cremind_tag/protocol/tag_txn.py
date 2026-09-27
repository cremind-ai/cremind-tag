"""Tag display-transaction decisions (docs/protocol.md §5.6 and §6).

Pure functions over the tag's persisted display record, shared as test
vectors with the firmware (protocol/fixtures/tag_txn.json).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

from .ids import Status


class StoredState(IntEnum):
    """Persisted ``state`` of the display record (§6)."""

    DISPLAYED = 0
    REFRESH_INTENT = 1


@dataclass(frozen=True, slots=True)
class DisplayRecord:
    """The tag's single persisted display record (§6)."""

    tag_id: int
    epoch: int
    revision: int
    update_id: int
    digest: bytes  # 32-byte frame digest
    status: Status
    state: StoredState


@dataclass(frozen=True, slots=True)
class FrameBeginDecision:
    """``accept`` -> begin_frame(); otherwise answer RESULT ``status`` now.

    ``duplicate`` means: re-send the stored RESULT OK with flags.bit0 set.
    """

    accept: bool
    status: Status | None = None
    duplicate: bool = False


def frame_begin_decision(
    stored: DisplayRecord | None,
    epoch: int,
    revision: int,
    digest: bytes,
    planes: int,
    plane_len: int,
    panel_planes: int,
    panel_plane_len: int,
) -> FrameBeginDecision:
    """Apply the FRAME_BEGIN table of §5.6 (first matching row wins)."""
    if stored is not None:
        if (epoch, revision) < (stored.epoch, stored.revision):
            return FrameBeginDecision(False, Status.STALE_REVISION)
        if (epoch, revision) == (stored.epoch, stored.revision):
            if digest != stored.digest:
                return FrameBeginDecision(False, Status.REVISION_CONFLICT)
            if stored.state == StoredState.DISPLAYED:
                return FrameBeginDecision(False, Status.OK, duplicate=True)
    if (planes, plane_len) != (panel_planes, panel_plane_len):
        return FrameBeginDecision(False, Status.INVALID)
    return FrameBeginDecision(True)


@dataclass(frozen=True, slots=True)
class BootRecovery:
    record: DisplayRecord | None
    persist: bool  # the record changed and must be written back
    unknown_pending: bool  # CHALLENGE flags.bit0


def boot_recover(stored: DisplayRecord | None) -> BootRecovery:
    """Apply the boot rule of §6 to the record read from storage."""
    if stored is None or stored.state == StoredState.DISPLAYED:
        return BootRecovery(stored, False, False)
    if stored.status == Status.DISPLAY_STATE_UNKNOWN:
        return BootRecovery(stored, False, True)
    return BootRecovery(replace(stored, status=Status.DISPLAY_STATE_UNKNOWN), True, True)
