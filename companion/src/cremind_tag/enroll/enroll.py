"""Tag enrollment over SWD (docs/enrollment.md, docs/protocol.md §9, docs/security.md).

:func:`enroll_tag` gives a tag its identity: a fresh tag id and 32-byte secret
packed into the enrollment blob, written to ``UICR.CUSTOMER[0..11]``, read back
and verified, then recorded in the local inventory with the secret in the
:class:`~cremind_tag.secrets.SecretStore`.

Real-run order and why:

1. store the secret first — a crash after the tag is programmed must never
   leave a tag whose secret exists nowhere else;
2. with ``firmware``: erase the whole chip and program the firmware (verified),
   then program the UICR image without erasing; without ``firmware``: erase
   only the UICR page and program the UICR image (the application stays);
3. read the 48 bytes back and require an exact match that also parses;
4. insert the inventory row (the tag is enrolled from here on);
5. optionally enable APPROTECT after an explicit confirmation;
6. reset.

A failure before step 4 deletes the stored secret and re-raises: the tag holds
no usable identity, and enrolling it again erases it again. The UICR image
holds the secret, so it is created with mode 0600 and deleted after a real run
unless ``keep_hex``. Neither the secret nor the image contents are ever logged.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets as _stdlib_secrets  # ``secrets`` below is the SecretStore parameter
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..protocol.enrollment import (
    BOARD_SOC,
    UICR_CUSTOMER_ADDR,
    EnrollmentError,
    enrollment_hex,
    pack_blob,
    unpack_blob,
)
from ..protocol.ids import TAG_SECRET_LEN, Board, Panel
from ..protocol.msgs import Enrollment
from ..store.db import TagRecord
from .hardware import BOARD_PANEL, PANEL_PROFILES, TAG_BOARDS, PanelProfile, format_tag_id, parse_board, parse_panel
from .tools import CommandRunner, EraseMode, SwdTool, ToolCommand, ToolError, choose_tool

if TYPE_CHECKING:
    from ..secrets import SecretStore
    from ..store.db import Database

log = logging.getLogger(__name__)

TAG_ID_MIN = 1
TAG_ID_MAX = 0xFFFFFFFE

PROTECT_WARNING = (
    "Enabling APPROTECT (readback protection) locks this tag's debug port: its memory, including the "
    "enrollment secret in UICR, can no longer be read or written over SWD. This is irreversible without a "
    "full chip erase (nrfutil device recover / nrfjprog --recover), which also erases the firmware and the "
    "enrollment secret, so the tag must then be reflashed and re-enrolled. Without APPROTECT, anyone with "
    "physical SWD access to the tag can read its secret and impersonate this tag (docs/security.md)."
)


class EnrollError(RuntimeError):
    """Enrollment could not be completed (tool failures are :class:`~.tools.ToolError`)."""


class ReadbackError(EnrollError):
    """The UICR read back does not hold the blob just written."""


@dataclass
class EnrollmentResult:
    """What :func:`enroll_tag` did or, for a dry run, would do."""

    tag_id: int
    board: Board
    panel: Panel
    geometry: PanelProfile
    tool: str
    hex_path: Path
    dry_run: bool
    plan: list[ToolCommand]
    """Every command of a real run, in order (``protect`` included when requested)."""
    executed: list[ToolCommand] = field(default_factory=list)
    registered: bool = False
    """The inventory row exists (always after a real run; after a dry run only with ``register``)."""
    protected: bool = False
    protect_declined: bool = False
    secret_ref: str | None = None
    record: TagRecord | None = None
    hex_kept: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def hw_id(self) -> str:
        return format_tag_id(self.tag_id)

    @property
    def commands(self) -> list[str]:
        """The plan as command lines quoted for this platform's shell."""
        return [command.display() for command in self.plan]

    def render_plan(self, platform: str = sys.platform) -> str:
        """The plan with J-Link command files listed under their command lines."""
        return "\n".join(command.render(platform) for command in self.plan)


# -- building blocks (public for the CLI and tests) ----------------------------------------------


def _new_secret() -> bytes:
    return _stdlib_secrets.token_bytes(TAG_SECRET_LEN)


def _random_tag_id() -> int:
    return TAG_ID_MIN + _stdlib_secrets.randbelow(TAG_ID_MAX - TAG_ID_MIN + 1)


def new_tag_id(db: Database, secrets: SecretStore | None = None, *, attempts: int = 64) -> int:
    """A random tag id in 1..0xFFFFFFFE unused in the inventory (and without a stored secret)."""
    for _ in range(attempts):
        candidate = _random_tag_id()
        if db.tag_exists(candidate):
            continue
        if secrets is not None and secrets.has_tag_secret(candidate):
            continue
        return candidate
    raise EnrollError("could not find an unused tag id")  # pragma: no cover - 2^32 space


def resolve_geometry(panel: Panel, geometry: tuple[int, int, int, int] | PanelProfile | None) -> PanelProfile:
    """Panel geometry and plane encoding: the panel profile, or ``geometry`` for UNVERIFIED panels."""
    given: PanelProfile | None = None
    if geometry is not None:
        given = geometry if isinstance(geometry, PanelProfile) else PanelProfile(*geometry)
        if not (1 <= given.width <= 0xFFFF and 1 <= given.height <= 0xFFFF):
            raise ValueError(f"panel size {given.width}x{given.height} out of range")
        if given.planes not in (1, 2):
            raise ValueError(f"planes must be 1 or 2, not {given.planes}")
        if not 0 <= given.plane_flags <= 0x03 or (given.plane_flags & 0x02 and given.planes < 2):
            raise ValueError(f"plane flags 0x{given.plane_flags:02X} do not fit {given.planes} plane(s)")
    profile = PANEL_PROFILES.get(panel)
    if profile is None:
        if given is None:
            raise ValueError(f"panel {panel.name.lower()} has no verified profile: give its geometry "
                             "(width, height, planes, plane flags)")
        return given
    if given is not None and given != profile:
        raise ValueError(f"panel {panel.name.lower()} is {profile.width}x{profile.height}, {profile.planes} plane(s), "
                         f"flags 0x{profile.plane_flags:02X}; a different geometry needs panel 'unverified'")
    return profile


def enrollment_hex_path(out_dir: Path, tag_id: int) -> Path:
    return Path(out_dir) / f"{format_tag_id(tag_id)}-uicr.hex"


def write_enrollment_hex(path: Path, blob: bytes, board: Board) -> Path:
    """Write the UICR image for ``blob`` with mode 0600 (it holds the secret)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(enrollment_hex(blob, board).encode("ascii"))
    if sys.platform != "win32":  # an existing file keeps its old mode through O_TRUNC
        os.chmod(path, 0o600)
    return path


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("enroll: could not delete %s (it holds a tag secret): %s", path, exc)


def _forget_secret(secrets: SecretStore, tag_id: int) -> None:
    try:
        secrets.delete_tag_secret(tag_id)
        log.info("enroll: deleted the secret of tag %s (enrollment failed)", format_tag_id(tag_id))
    except Exception as exc:  # never mask the enrollment failure
        log.error("enroll: could not delete the secret of failed tag %s: %s", format_tag_id(tag_id), exc)


def verify_readback(data: bytes, blob: bytes) -> Enrollment:
    """The UICR contents must equal the written blob and parse (docs/protocol.md §9)."""
    if len(data) != len(blob) or not hmac.compare_digest(data, blob):
        raise ReadbackError("UICR.CUSTOMER read back does not match the enrollment blob just written")
    try:
        return unpack_blob(data)
    except EnrollmentError as exc:
        raise ReadbackError(f"UICR.CUSTOMER read back is not a valid enrollment blob: {exc}") from None


def plan_commands(tool: SwdTool, *, hex_path: Path, firmware: Path | None, protect: bool) -> list[ToolCommand]:
    """The enrollment command sequence for ``tool`` (see the module docstring for the order)."""
    soc = tool.target.soc
    commands: list[ToolCommand] = []
    if firmware is not None:
        commands.append(tool.program_command(firmware, erase=EraseMode.ALL, verify=True))
        commands.append(tool.program_command(hex_path, erase=EraseMode.NONE, verify=True))
    else:
        commands.extend(tool.program_uicr_commands(hex_path))
    commands.append(tool.read_memory_command(UICR_CUSTOMER_ADDR[soc], Enrollment.LEN))
    if protect:
        commands.append(tool.protect_command())
    commands.append(tool.reset_command())
    return commands


# -- enrollment ------------------------------------------------------------------------------------


def enroll_tag(
    *,
    board: Board | int | str,
    panel: Panel | int | str | None,
    db: Database,
    secrets: SecretStore,
    out_dir: Path,
    tool: SwdTool | str | None = None,
    serial_number: str | None = None,
    firmware: Path | None = None,
    protect: bool = False,
    confirm_protect: Callable[[str], bool] | None = None,
    dry_run: bool = False,
    register: bool = False,
    tag_id: int | None = None,
    name: str = "",
    geometry: tuple[int, int, int, int] | PanelProfile | None = None,
    keep_hex: bool = False,
    runner: CommandRunner | None = None,
) -> EnrollmentResult:
    """Enroll one tag over SWD, or with ``dry_run`` only show how.

    ``board``/``panel`` accept enum members, numbers or CLI names
    (:func:`~.hardware.parse_board`); ``panel=None`` means the board's stock
    panel. ``geometry`` = ``(width, height, planes, plane_flags)`` is required
    for an UNVERIFIED panel and must match the profile otherwise.

    ``tool`` is an :class:`~.tools.SwdTool`, or a preference (``auto`` |
    ``nrfutil`` | ``nrfjprog`` | ``jlink``; ``None`` = ``auto``) resolved with
    ``serial_number`` and ``runner``. ``firmware`` (an Intel HEX) is flashed
    after a full chip erase; without it only the UICR is rewritten.

    ``protect`` enables APPROTECT after enrollment. ``confirm_protect`` is
    called with :data:`PROTECT_WARNING` first; a ``False`` answer skips
    protection without failing (``None`` = no question, e.g. ``--yes``).

    ``dry_run`` writes the UICR image and any J-Link command files to
    ``out_dir`` and returns the plan without running anything; it touches
    neither ``db`` nor ``secrets`` unless ``register`` (then the secret is
    stored and the row inserted, unprotected, so the printed commands can be
    run by hand). ``register`` has no effect on a real run, which always
    registers.

    Raises ``ValueError`` for bad arguments, :class:`EnrollError` (including
    :class:`ReadbackError`) and :class:`~.tools.ToolError`.
    """
    board = parse_board(board) if isinstance(board, str) else Board(board)
    if board not in TAG_BOARDS:
        raise ValueError(f"board {board.name.lower()} is not a tag board")
    if panel is None:
        panel = BOARD_PANEL[board]
    panel = parse_panel(panel) if isinstance(panel, str) else Panel(panel)
    profile = resolve_geometry(panel, geometry)
    warnings: list[str] = []
    if panel != BOARD_PANEL[board]:
        warnings.append(f"board {board.name.lower()} ships with panel {BOARD_PANEL[board].name.lower()}, "
                        f"not {panel.name.lower()}")

    if firmware is not None:
        firmware = Path(firmware).resolve()
        if firmware.suffix.lower() != ".hex":
            raise ValueError(f"firmware must be an Intel HEX file (.hex): {firmware}")
        if not firmware.is_file():
            if not dry_run:
                raise ValueError(f"firmware not found: {firmware}")
            warnings.append(f"firmware {firmware} does not exist yet")

    if tag_id is None:
        tag_id = new_tag_id(db, secrets)
    else:
        if not TAG_ID_MIN <= tag_id <= TAG_ID_MAX:
            raise ValueError(f"tag id {tag_id} out of range 1..0xFFFFFFFE")
        if db.tag_exists(tag_id):
            raise EnrollError(f"tag {format_tag_id(tag_id)} is already enrolled; remove it first")
        if secrets.has_tag_secret(tag_id) and (register or not dry_run):
            warnings.append(f"replacing a stored secret of tag {format_tag_id(tag_id)} that had no inventory row")
    hw_id = format_tag_id(tag_id)

    out_dir = Path(out_dir).resolve()
    if isinstance(tool, SwdTool):
        swd = tool
        if swd.target.board != board:
            raise ValueError(f"the SWD tool targets {swd.target.board.name.lower()}, not {board.name.lower()}")
    else:
        swd = choose_tool(tool or "auto", board, serial_number=serial_number, runner=runner, workdir=out_dir,
                          allow_missing=dry_run)
    if BOARD_SOC[board] != swd.target.soc:  # pragma: no cover - tables out of sync
        raise EnrollError(f"SoC family mismatch for {board.name}")

    secret = _new_secret()
    blob = pack_blob(tag_id, secret, board, panel)
    hex_path = write_enrollment_hex(enrollment_hex_path(out_dir, tag_id), blob, board)
    plan = plan_commands(swd, hex_path=hex_path, firmware=firmware, protect=protect)
    result = EnrollmentResult(tag_id=tag_id, board=board, panel=panel, geometry=profile, tool=swd.name,
                              hex_path=hex_path, dry_run=dry_run, plan=plan, warnings=warnings)
    record = TagRecord(tag_id=tag_id, board=int(board), panel=int(panel), width=profile.width,
                       height=profile.height, planes=profile.planes, plane_flags=profile.plane_flags,
                       secret_ref="", name=name, fw=None, protected=False)
    log.info("enroll: tag %s, board %s, panel %s, via %s%s", hw_id, board.name.lower(), panel.name.lower(),
             swd.label, " (dry run)" if dry_run else "")

    if dry_run:
        for command in plan:
            swd.prepare(command)
        result.hex_kept = True
        if register:
            result.secret_ref = secrets.set_tag_secret(tag_id, secret)
            try:
                result.record = db.insert_tag(replace(record, secret_ref=result.secret_ref))
            except BaseException:
                _forget_secret(secrets, tag_id)
                raise
            result.registered = True
            if protect:
                warnings.append("registered as unprotected: the inventory is not updated when you enable "
                                "protection by hand")
        else:
            warnings.append(f"{hex_path.name} holds a secret that is not stored: flashing it by hand leaves a "
                            "tag the companion cannot talk to (use --register)")
        return result

    return _run(swd, result, record, secret=secret, blob=blob, secrets=secrets, db=db,
                protect=protect, confirm_protect=confirm_protect, keep_hex=keep_hex)


def _run(swd: SwdTool, result: EnrollmentResult, record: TagRecord, *, secret: bytes, blob: bytes,
         secrets: SecretStore, db: Database, protect: bool, confirm_protect: Callable[[str], bool] | None,
         keep_hex: bool) -> EnrollmentResult:
    tag_id, hw_id = result.tag_id, result.hw_id
    plan = list(result.plan)
    reset_command = plan.pop()
    protect_command = plan.pop() if protect else None
    read_command = plan.pop()
    start = len(swd.history)
    try:
        # 1. The secret first: a crash after programming must not lose it.
        result.secret_ref = secrets.set_tag_secret(tag_id, secret)
        try:
            for command in plan:  # 2. program
                swd.run(command)
            address, length = UICR_CUSTOMER_ADDR[swd.target.soc], len(blob)
            if swd.read_memory_command(address, length) != read_command:  # pragma: no cover - builders are pure
                raise EnrollError("readback command changed between planning and running")
            verify_readback(swd.read_memory(address, length), blob)  # 3. verify
            result.record = db.insert_tag(replace(record, secret_ref=result.secret_ref))  # 4. enrolled
            result.registered = True
        except BaseException:
            _forget_secret(secrets, tag_id)
            raise
        log.info("enroll: tag %s programmed, verified and added to the inventory", hw_id)

        if protect_command is not None:  # 5. APPROTECT
            if confirm_protect is not None and not confirm_protect(PROTECT_WARNING):
                result.protect_declined = True
                log.info("enroll: APPROTECT declined for tag %s", hw_id)
            else:
                try:
                    swd.run(protect_command)
                except ToolError as exc:
                    raise EnrollError(f"tag {hw_id} is enrolled, but enabling APPROTECT failed; its secret stays "
                                      f"readable over SWD:\n{exc}") from exc
                result.record = db.update_tag(tag_id, protected=True)
                result.protected = True
                log.info("enroll: APPROTECT enabled on tag %s", hw_id)

        try:  # 6. reset
            swd.run(reset_command)
        except ToolError as exc:
            first = str(exc).splitlines()[0]
            result.warnings.append(f"reset failed ({first}); power-cycle the tag")
            log.warning("enroll: reset of tag %s failed: %s", hw_id, first)
    finally:
        result.executed = swd.history[start:]
        if keep_hex:
            result.hex_kept = True
        else:
            _remove(result.hex_path)
    return result


__all__ = [
    "PROTECT_WARNING",
    "TAG_ID_MAX",
    "TAG_ID_MIN",
    "EnrollError",
    "EnrollmentResult",
    "ReadbackError",
    "enroll_tag",
    "enrollment_hex_path",
    "new_tag_id",
    "plan_commands",
    "resolve_geometry",
    "verify_readback",
    "write_enrollment_hex",
]
