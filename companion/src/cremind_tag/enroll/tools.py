"""SWD programmers for tag enrollment: detection, command builders and runners (docs/enrollment.md).

Three tools drive a SEGGER J-Link probe (or the on-board J-Link of a Nordic DK):

- ``nrfutil device`` (nRF Util) — preferred, but only when its ``device``
  command is installed (``nrfutil install device``);
- ``nrfjprog`` (nRF Command Line Tools, legacy);
- SEGGER J-Link Commander (``JLinkExe``; ``JLink.exe`` on Windows) driven by a
  command file.

Every operation is first *built* as a :class:`ToolCommand` — a pure value
holding the argv and, for J-Link, the command-file text — and then *run*
through a :class:`CommandRunner`. A dry run therefore prints exactly what a
real run executes, and tests substitute the runner. The command lines follow
the vendors' documentation; none of the tools could be exercised on real
hardware when this was written, so docs/enrollment.md lists what the first
physical sample must confirm.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ..protocol.ids import Board

log = logging.getLogger(__name__)

TOOL_NAMES: tuple[str, ...] = ("nrfutil", "nrfjprog", "jlink")
"""Tool names in order of preference for ``jlink_tool = "auto"``."""

TOOL_PREFERENCES: tuple[str, ...] = ("auto", *TOOL_NAMES)

JLINK_SPEED_KHZ = 4000

# Timeouts (seconds). A chip erase plus a full-flash program of a 256 KiB part
# takes well under a minute; the margin covers slow USB hubs and first-run
# tool start-up (nrfutil unpacks itself on first use).
TIMEOUT_PROGRAM = 180.0
TIMEOUT_SHORT = 60.0
TIMEOUT_PROBE = 30.0


class ToolError(RuntimeError):
    """An SWD tool is missing, failed, or printed something we cannot parse."""

    def __init__(self, message: str, *, argv: list[str] | tuple[str, ...] | None = None,
                 returncode: int | None = None, output: str = "") -> None:
        self.argv = list(argv) if argv is not None else None
        self.returncode = returncode
        self.output = output
        lines = [message]
        if self.argv is not None:
            lines.append(f"  command: {format_command(self.argv)}")
        if returncode is not None:
            lines.append(f"  exit status: {returncode}")
        if output.strip():
            tail = output.strip().splitlines()[-20:]
            lines.append("  output:")
            lines.extend(f"    {line}" for line in tail)
        super().__init__("\n".join(lines))


# -- running commands ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompletedResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs one command without a shell; must not raise on a non-zero exit status."""

    def __call__(self, argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult: ...


def subprocess_runner(argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
    """Default runner: :func:`subprocess.run`, text mode, no shell, stdin closed unless ``input``."""
    kwargs: dict[str, Any] = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, shell=False, **kwargs)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]} not found", argv=argv) from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {timeout:.0f} s", argv=argv) from None
    except OSError as exc:
        raise ToolError(f"cannot run {argv[0]}: {exc}", argv=argv) from None
    return CompletedResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def format_command(argv: list[str] | tuple[str, ...], platform: str = sys.platform) -> str:
    """One command line quoted for the platform's usual shell (cmd.exe rules on Windows, POSIX elsewhere)."""
    if platform == "win32":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


@dataclass(frozen=True, slots=True)
class ToolCommand:
    """One tool invocation, fully determined before anything runs."""

    argv: tuple[str, ...]
    description: str
    timeout: float = TIMEOUT_SHORT
    script: str | None = None
    """J-Link Commander command file text, written to ``script_path`` before the run."""
    script_path: Path | None = None
    sensitive_output: bool = False
    """stdout holds secret material (a UICR readback): never logged or put into errors."""

    def display(self, platform: str = sys.platform) -> str:
        return format_command(self.argv, platform)

    def render(self, platform: str = sys.platform) -> str:
        """The command line, followed by the command file for J-Link."""
        text = self.display(platform)
        if self.script is not None:
            text += f"\n  # {self.script_path}:\n" + "".join(f"  {line}\n" for line in self.script.splitlines())
        return text.rstrip("\n")


# -- targets --------------------------------------------------------------------


class Family(StrEnum):
    NRF51 = "NRF51"
    NRF52 = "NRF52"


class EraseMode(StrEnum):
    """What a program operation erases first."""

    NONE = "none"
    TOUCHED = "touched"  # the flash pages (and UICR) the image writes to
    ALL = "all"  # the whole chip, UICR included


@dataclass(frozen=True, slots=True)
class Target:
    board: Board
    family: Family
    jlink_device: str

    @property
    def soc(self) -> str:
        """Key of ``protocol.enrollment.UICR_CUSTOMER_ADDR``/``BOARD_SOC``."""
        return self.family.value.lower()


# J-Link device names as docs/building.md "Flashing with J-Link" lists them.
TARGETS: dict[Board, Target] = {
    Board.LAOWU_BW_NRF51822: Target(Board.LAOWU_BW_NRF51822, Family.NRF51, "nRF51822_xxAB"),
    Board.LAOWU_BWR_NRF51802: Target(Board.LAOWU_BWR_NRF51802, Family.NRF51, "nRF51822_xxAA"),
    Board.SIFEI_NRF52810: Target(Board.SIFEI_NRF52810, Family.NRF52, "nRF52810_xxAA"),
    Board.HEMA_NRF52811: Target(Board.HEMA_NRF52811, Family.NRF52, "nRF52811_xxAA"),
    Board.NRF52DK_TAG: Target(Board.NRF52DK_TAG, Family.NRF52, "nRF52832_xxAA"),
}


def target_for_board(board: Board | int) -> Target:
    try:
        return TARGETS[Board(board)]
    except (KeyError, ValueError):
        raise ValueError(f"board {board!r} is not a tag board") from None


# -- memory dumps ------------------------------------------------------------------

# ``0x10001080: 47415443 ...`` (nrfjprog), ``10001080 = 47415443 ...`` (J-Link mem32),
# ``0x10001080: 43 54 41 47 ...`` (byte dumps). Register lines J-Link prints
# after ``h`` (``R0 = ...``, ``PC = ...``) never start with a pure hex token.
_DUMP_LINE = re.compile(r"^\s*(?:0[xX])?([0-9A-Fa-f]{4,16})\s*[:=]\s*(.*)$")
_HEX = re.compile(r"^[0-9A-Fa-f]+$")


def _hex_token(token: str) -> str | None:
    digits = token[2:] if token[:2].lower() == "0x" else token
    return digits if len(digits) in (2, 4, 8) and _HEX.match(digits) else None


def _parse_text_dump(text: str, address: int, length: int) -> bytes:
    memory: dict[int, int] = {}
    for raw in text.splitlines():
        line = raw.split("|", 1)[0]  # drop the ASCII column
        match = _DUMP_LINE.match(line)
        if match is None:
            continue
        pos = int(match.group(1), 16)
        width = 0
        for token in match.group(2).split():
            digits = _hex_token(token)
            if digits is None or (width and len(digits) // 2 != width):
                break  # end of the data columns
            width = len(digits) // 2
            # Words and half-words are printed as values; memory is little-endian.
            for i, byte in enumerate(int(digits, 16).to_bytes(width, "little")):
                memory[pos + i] = byte
            pos += width
    missing = next((a for a in range(address, address + length) if a not in memory), None)
    if missing is not None:
        raise ValueError(f"memory dump does not cover 0x{address:08X}+{length} (first missing byte 0x{missing:08X})")
    return bytes(memory[a] for a in range(address, address + length))


def _json_values(text: str) -> Iterator[Any]:
    try:
        yield json.loads(text)
        return
    except ValueError:
        pass
    for line in text.splitlines():  # newline-delimited JSON (``--json`` progress + result)
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _bytes_in_json(value: Any, length: int) -> bytes | None:
    if isinstance(value, list):
        if value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            if all(0 <= v <= 0xFF for v in value) and len(value) >= length:
                return bytes(value[:length])
            if all(0 <= v <= 0xFFFFFFFF for v in value) and len(value) * 4 >= length:
                return b"".join(v.to_bytes(4, "little") for v in value)[:length]
            return None
        for item in value:
            if (found := _bytes_in_json(item, length)) is not None:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            if (found := _bytes_in_json(item, length)) is not None:
                return found
    elif isinstance(value, str):
        digits = re.sub(r"\s+", "", value.removeprefix("0x"))
        if len(digits) >= 2 * length and len(digits) % 2 == 0 and _HEX.match(digits):
            return bytes.fromhex(digits)[:length]
    return None


def parse_memory_dump(text: str, address: int, length: int) -> bytes:
    """``length`` bytes at ``address`` from a tool's memory dump.

    Accepts nrfjprog ``--memrd`` output, J-Link ``mem32``/``mem8`` output and
    byte dumps (32-bit words are little-endian values; an ASCII column after
    ``|`` is ignored), and JSON holding a byte list, a word list or a hex
    string. Raises ``ValueError`` when the dump does not cover the range.
    """
    if "{" in text or "[" in text:
        for value in _json_values(text):
            if (found := _bytes_in_json(value, length)) is not None:
                return found
    return _parse_text_dump(text, address, length)


# -- tools ------------------------------------------------------------------------------


class SwdTool(ABC):
    """One programmer for one target: builds :class:`ToolCommand` values and runs them.

    Builders (``*_command``) are pure. :meth:`run` records every command it
    runs in :attr:`history` (J-Link command files included).
    """

    name: ClassVar[str]
    label: ClassVar[str]

    def __init__(self, executable: str, target: Target, *, serial_number: str | None = None,
                 runner: CommandRunner | None = None, workdir: Path | None = None) -> None:
        self.executable = executable
        self.target = target
        self.serial_number = serial_number or None
        self.runner: CommandRunner = runner or subprocess_runner
        self.workdir = Path(workdir) if workdir is not None else Path(tempfile.gettempdir()) / "cremind-tag"
        self.keep_scripts = False
        self.history: list[ToolCommand] = []

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.executable!r}, {self.target.jlink_device})"

    # -- builders -----------------------------------------------------------------

    @abstractmethod
    def erase_all_command(self) -> ToolCommand:
        """Erase the whole chip, UICR included."""

    @abstractmethod
    def program_command(self, hex_path: Path, *, erase: EraseMode = EraseMode.NONE, verify: bool = True) -> ToolCommand:
        """Program an Intel HEX image."""

    @abstractmethod
    def program_uicr_commands(self, hex_path: Path) -> list[ToolCommand]:
        """Erase only the UICR page, then program a UICR-only image (flash stays untouched)."""

    @abstractmethod
    def read_memory_command(self, address: int, length: int) -> ToolCommand:
        """Dump ``length`` bytes at ``address`` to stdout (see :func:`parse_memory_dump`)."""

    @abstractmethod
    def protect_command(self) -> ToolCommand:
        """Enable readback protection of everything (nRF52 APPROTECT, nRF51 RBPCONF.PALL)."""

    @abstractmethod
    def reset_command(self) -> ToolCommand:
        """Reset the target and let it run."""

    # -- running ------------------------------------------------------------------------

    def prepare(self, command: ToolCommand) -> None:
        """Write the command's J-Link command file (no secrets in it: paths and register writes only)."""
        if command.script is not None and command.script_path is not None:
            command.script_path.parent.mkdir(parents=True, exist_ok=True)
            command.script_path.write_text(command.script, encoding="ascii", newline="\n")

    def run(self, command: ToolCommand) -> CompletedResult:
        self.history.append(command)
        self.prepare(command)
        log.info("swd: %s (%s)", command.description, self.label)
        log.debug("swd: %s", command.display())
        try:
            result = self.runner(list(command.argv), timeout=command.timeout)
        finally:
            if command.script_path is not None and not self.keep_scripts:
                command.script_path.unlink(missing_ok=True)
        output = result.stderr if command.sensitive_output else (result.stderr + "\n" + result.stdout)
        if result.returncode != 0:
            raise ToolError(f"{command.description} failed", argv=command.argv, returncode=result.returncode,
                            output=output)
        if (problem := self._output_problem(result)) is not None:
            raise ToolError(f"{command.description} failed: {problem}", argv=command.argv, output=output)
        return result

    def _output_problem(self, result: CompletedResult) -> str | None:
        """A failure the tool reports only in its output (exit status 0)."""
        return None

    def erase_all(self) -> None:
        self.run(self.erase_all_command())

    def program(self, hex_path: Path, *, erase: EraseMode = EraseMode.NONE, verify: bool = True) -> None:
        self.run(self.program_command(hex_path, erase=erase, verify=verify))

    def program_uicr(self, hex_path: Path) -> None:
        for command in self.program_uicr_commands(hex_path):
            self.run(command)

    def read_memory(self, address: int, length: int) -> bytes:
        command = self.read_memory_command(address, length)
        result = self.run(command)
        try:
            return parse_memory_dump(result.stdout, address, length)
        except ValueError as exc:
            # The output may hold the secret: report the parse problem only.
            raise ToolError(f"cannot parse the {self.label} memory dump: {exc}", argv=command.argv) from None

    def protect(self) -> None:
        self.run(self.protect_command())

    def reset(self) -> None:
        self.run(self.reset_command())


class NrfutilTool(SwdTool):
    """``nrfutil device`` (nRF Util 7+ with the ``device`` command v2)."""

    name = "nrfutil"
    label = "nrfutil device"

    def _command(self, description: str, *args: str, timeout: float = TIMEOUT_SHORT,
                 sensitive: bool = False) -> ToolCommand:
        argv = [self.executable, "device", *args]
        if self.serial_number:
            argv += ["--serial-number", self.serial_number]
        return ToolCommand(tuple(argv), description, timeout, sensitive_output=sensitive)

    def erase_all_command(self) -> ToolCommand:
        return self._command("erase the whole chip", "erase", "--all", timeout=TIMEOUT_PROGRAM)

    def program_command(self, hex_path: Path, *, erase: EraseMode = EraseMode.NONE, verify: bool = True) -> ToolCommand:
        mode = {EraseMode.NONE: "ERASE_NONE", EraseMode.TOUCHED: "ERASE_RANGES_TOUCHED_BY_FIRMWARE",
                EraseMode.ALL: "ERASE_ALL"}[erase]
        options = f"chip_erase_mode={mode},verify={'VERIFY_READ' if verify else 'VERIFY_NONE'}"
        return self._command(f"program {Path(hex_path).name} (erase: {erase.value})",
                             "program", "--firmware", str(hex_path), "--options", options, timeout=TIMEOUT_PROGRAM)

    def program_uicr_commands(self, hex_path: Path) -> list[ToolCommand]:
        # The image touches only the UICR page, so "ranges touched" erases exactly UICR.
        command = self.program_command(hex_path, erase=EraseMode.TOUCHED)
        return [replace(command, description=f"erase UICR and program {Path(hex_path).name}")]

    def read_memory_command(self, address: int, length: int) -> ToolCommand:
        return self._command(f"read {length} bytes at 0x{address:08X}", "read", "--address", f"0x{address:08X}",
                             "--bytes", str(length), sensitive=True)

    def protect_command(self) -> ToolCommand:
        return self._command("enable readback protection (APPROTECT)", "protection-set", "All")

    def reset_command(self) -> ToolCommand:
        return self._command("reset", "reset")


class NrfjprogTool(SwdTool):
    """``nrfjprog`` from the nRF Command Line Tools (legacy, still widely installed)."""

    name = "nrfjprog"
    label = "nrfjprog"

    def _command(self, description: str, *args: str, timeout: float = TIMEOUT_SHORT,
                 sensitive: bool = False) -> ToolCommand:
        argv = [self.executable, "-f", self.target.family.value, *args]
        if self.serial_number:
            argv += ["--snr", self.serial_number]
        return ToolCommand(tuple(argv), description, timeout, sensitive_output=sensitive)

    def erase_all_command(self) -> ToolCommand:
        return self._command("erase the whole chip", "--eraseall", timeout=TIMEOUT_PROGRAM)

    def program_command(self, hex_path: Path, *, erase: EraseMode = EraseMode.NONE, verify: bool = True) -> ToolCommand:
        args = ["--program", str(hex_path)]
        if erase is EraseMode.ALL:
            args.append("--chiperase")
        elif erase is EraseMode.TOUCHED:
            # Zephyr's nrfjprog runner: --sectoranduicrerase on nRF52, --sectorerase elsewhere.
            args.append("--sectoranduicrerase" if self.target.family is Family.NRF52 else "--sectorerase")
        if verify:
            args.append("--verify")
        return self._command(f"program {Path(hex_path).name} (erase: {erase.value})", *args, timeout=TIMEOUT_PROGRAM)

    def program_uicr_commands(self, hex_path: Path) -> list[ToolCommand]:
        return [self._command("erase UICR", "--eraseuicr"), self.program_command(hex_path, erase=EraseMode.NONE)]

    def read_memory_command(self, address: int, length: int) -> ToolCommand:
        count = (length + 3) // 4 * 4
        return self._command(f"read {length} bytes at 0x{address:08X}", "--memrd", f"0x{address:08X}",
                             "--n", str(count), "--w", "32", sensitive=True)

    def protect_command(self) -> ToolCommand:
        return self._command("enable readback protection (APPROTECT)", "--rbp", "ALL")

    def reset_command(self) -> ToolCommand:
        return self._command("reset", "--reset")


# nRF51/nRF52 NVMC (base 0x4001E000): CONFIG at +0x504 (0 = read only, 1 = write
# enable, 2 = erase enable), ERASEUICR at +0x514 (nRF51 reference manual v3.0
# §6 NVMC; nRF52810/52811/52832 product specifications, NVMC registers).
_NVMC_CONFIG = 0x4001E504
_NVMC_ERASEUICR = 0x4001E514
# nRF52 UICR.APPROTECT (0x10001208): PALL[7:0] 0xFF = disabled, 0x00 = enabled
# (0x5A = HwDisabled on newer revisions). nRF51 UICR.RBPCONF (0x10001004):
# PR0[7:0] protects code region 0, PALL[15:8] protects everything; 0x00 =
# enabled. Flash bits only go 1 -> 0 without an erase, so writing the value
# with just the PALL field cleared is enough on an erased or partly set UICR.
_PROTECT_WRITE: dict[Family, tuple[int, int]] = {
    Family.NRF52: (0x10001208, 0xFFFFFF00),
    Family.NRF51: (0x10001004, 0xFFFF00FF),
}
# The UICR page erase takes at most ~90 ms (nRF52 t_ERASEPAGE) / ~22 ms (nRF51).
_UICR_ERASE_WAIT_MS = 200

# Failures J-Link Commander reports in its output; with -ExitOnError 1 it also
# exits non-zero, these catch versions that do not.
_JLINK_ERROR = re.compile(
    r"^\*{4,} Error|Cannot connect to target|Could not connect to target|FAILED|Error while programming|"
    r"Verify failed|Could not find|Unknown command", re.MULTILINE)


class JLinkTool(SwdTool):
    """SEGGER J-Link Commander running one command file per operation."""

    name = "jlink"
    label = "J-Link Commander"

    def _script(self, slug: str, description: str, lines: list[str], *, timeout: float = TIMEOUT_SHORT,
                sensitive: bool = False) -> ToolCommand:
        text = "\n".join([*lines, "qc"]) + "\n"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        path = self.workdir / f"jlink-{slug}-{digest}.jlink"
        argv = [self.executable]
        if self.serial_number:
            argv += ["-SelectEmuBySN", self.serial_number]
        argv += ["-device", self.target.jlink_device, "-if", "SWD", "-speed", str(JLINK_SPEED_KHZ),
                 "-autoconnect", "1", "-NoGui", "1", "-ExitOnError", "1", "-CommanderScript", str(path)]
        return ToolCommand(tuple(argv), description, timeout, script=text, script_path=path,
                           sensitive_output=sensitive)

    @staticmethod
    def _loadfile(hex_path: Path) -> str:
        path = str(hex_path)
        return f'loadfile "{path}"' if any(c.isspace() for c in path) else f"loadfile {path}"

    def erase_all_command(self) -> ToolCommand:
        return self._script("erase", "erase the whole chip", ["r", "h", "erase"], timeout=TIMEOUT_PROGRAM)

    def program_command(self, hex_path: Path, *, erase: EraseMode = EraseMode.NONE, verify: bool = True) -> ToolCommand:
        # loadfile erases the sectors it writes and verifies them itself, so
        # TOUCHED and verify need no extra commands.
        lines = ["r", "h"]
        if erase is EraseMode.ALL:
            lines.append("erase")
        lines.append(self._loadfile(hex_path))
        return self._script("program", f"program {Path(hex_path).name} (erase: {erase.value})", lines,
                            timeout=TIMEOUT_PROGRAM)

    def program_uicr_commands(self, hex_path: Path) -> list[ToolCommand]:
        lines = ["r", "h",
                 f"w4 0x{_NVMC_CONFIG:08X} 2", f"w4 0x{_NVMC_ERASEUICR:08X} 1", f"Sleep {_UICR_ERASE_WAIT_MS}",
                 f"w4 0x{_NVMC_CONFIG:08X} 0",
                 self._loadfile(hex_path)]
        return [self._script("uicr", f"erase UICR and program {Path(hex_path).name}", lines, timeout=TIMEOUT_PROGRAM)]

    def read_memory_command(self, address: int, length: int) -> ToolCommand:
        words = (length + 3) // 4
        return self._script("read", f"read {length} bytes at 0x{address:08X}",
                            ["h", f"mem32 0x{address:08X}, 0x{words:02X}"], sensitive=True)

    def protect_command(self) -> ToolCommand:
        register, value = _PROTECT_WRITE[self.target.family]
        lines = ["r", "h", f"w4 0x{_NVMC_CONFIG:08X} 1", f"w4 0x{register:08X} 0x{value:08X}",
                 f"w4 0x{_NVMC_CONFIG:08X} 0"]
        return self._script("protect", "enable readback protection (APPROTECT)", lines)

    def reset_command(self) -> ToolCommand:
        return self._script("reset", "reset", ["r", "g"])

    def _output_problem(self, result: CompletedResult) -> str | None:
        match = _JLINK_ERROR.search(result.stdout)
        return None if match is None else f"J-Link reported {match.group(0)!r}"


TOOL_CLASSES: dict[str, type[SwdTool]] = {"nrfutil": NrfutilTool, "nrfjprog": NrfjprogTool, "jlink": JLinkTool}


# -- detection ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolInfo:
    name: str
    """``nrfutil`` | ``nrfjprog`` | ``jlink``."""
    path: str
    detail: str = ""


Which = Callable[[str], str | None]
Glob = Callable[[str], list[str]]

_WINDOWS_GLOBS: dict[str, tuple[str, ...]] = {
    "nrfjprog": (r"C:\Program Files\Nordic Semiconductor\nrf-command-line-tools\bin\nrfjprog.exe",),
    "jlink": (r"C:\Program Files\SEGGER\JLink*\JLink.exe", r"C:\Program Files (x86)\SEGGER\JLink*\JLink.exe"),
}
_POSIX_GLOBS: dict[str, tuple[str, ...]] = {
    "jlink": ("/opt/SEGGER/JLink*/JLinkExe", "/Applications/SEGGER/JLink*/JLinkExe"),
}


def jlink_executable_name(platform: str = sys.platform) -> str:
    return "JLink.exe" if platform == "win32" else "JLinkExe"


def _natural_key(path: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path)]


def _find(name: str, executable: str, which: Which, platform: str, glob: Glob) -> str | None:
    if found := which(executable):
        return found
    patterns = (_WINDOWS_GLOBS if platform == "win32" else _POSIX_GLOBS).get(name, ())
    matches = [m for pattern in patterns for m in glob(pattern)]
    return max(matches, key=_natural_key) if matches else None  # newest install


def _default_executable(name: str, platform: str) -> str:
    return jlink_executable_name(platform) if name == "jlink" else name


def detect_tools(which: Which = shutil.which, runner: CommandRunner | None = None, *,
                 platform: str = sys.platform, glob: Glob = _glob.glob) -> list[ToolInfo]:
    """Usable SWD tools in order of preference (nrfutil > nrfjprog > J-Link Commander).

    nrfutil counts only when ``nrfutil device --version`` succeeds, i.e. the
    ``device`` command is installed. J-Link Commander is never started here:
    without a command file it waits for interactive input.
    """
    run = runner or subprocess_runner
    found: list[ToolInfo] = []
    for name in TOOL_NAMES:
        path = _find(name, _default_executable(name, platform), which, platform, glob)
        if path is None:
            continue
        if name == "nrfutil":
            try:
                result = run([path, "device", "--version"], timeout=TIMEOUT_PROBE)
            except ToolError as exc:
                log.info("swd: nrfutil at %s is not usable: %s", path, exc)
                continue
            if result.returncode != 0:
                log.info("swd: nrfutil at %s has no device command (run: nrfutil install device)", path)
                continue
            version = (result.stdout.strip().splitlines() or [""])[0]
            found.append(ToolInfo(name, path, version))
        else:
            found.append(ToolInfo(name, path))
    return found


INSTALL_HINT = ("install nRF Util and run `nrfutil install device`, the nRF Command Line Tools (nrfjprog), "
                "or the SEGGER J-Link Software Pack (see docs/enrollment.md)")


def make_tool(name: str, executable: str, target: Target, *, serial_number: str | None = None,
              runner: CommandRunner | None = None, workdir: Path | None = None) -> SwdTool:
    try:
        cls = TOOL_CLASSES[name]
    except KeyError:
        raise ValueError(f"unknown SWD tool {name!r} (choose from {', '.join(TOOL_PREFERENCES)})") from None
    return cls(executable, target, serial_number=serial_number, runner=runner, workdir=workdir)


def choose_tool(preference: str, board: Board | int, *, serial_number: str | None = None,
                runner: CommandRunner | None = None, workdir: Path | None = None, allow_missing: bool = False,
                which: Which = shutil.which, platform: str = sys.platform, glob: Glob = _glob.glob) -> SwdTool:
    """The SWD tool for ``board``: the first detected one for ``auto``, else exactly the one named.

    ``allow_missing`` (dry runs) returns a tool with the bare executable name
    when nothing is installed, so the commands can still be shown.
    """
    preference = (preference or "auto").strip().lower()
    if preference not in TOOL_PREFERENCES:
        raise ValueError(f"unknown SWD tool {preference!r} (choose from {', '.join(TOOL_PREFERENCES)})")
    target = target_for_board(board)
    tools = detect_tools(which, runner, platform=platform, glob=glob)
    if preference != "auto":
        tools = [t for t in tools if t.name == preference]
    if tools:
        chosen = tools[0]
        log.info("swd: using %s at %s", chosen.name, chosen.path)
        return make_tool(chosen.name, chosen.path, target, serial_number=serial_number, runner=runner, workdir=workdir)
    wanted = TOOL_NAMES[0] if preference == "auto" else preference
    if not allow_missing:
        what = "no SWD tool found" if preference == "auto" else f"{preference} not found"
        raise ToolError(f"{what}: {INSTALL_HINT}")
    log.warning("swd: %s; showing %s commands", "no SWD tool found" if preference == "auto" else f"{wanted} not found",
                wanted)
    return make_tool(wanted, _default_executable(wanted, platform), target, serial_number=serial_number,
                     runner=runner, workdir=workdir)


__all__ = [
    "INSTALL_HINT",
    "JLINK_SPEED_KHZ",
    "TARGETS",
    "TOOL_CLASSES",
    "TOOL_NAMES",
    "TOOL_PREFERENCES",
    "CommandRunner",
    "CompletedResult",
    "EraseMode",
    "Family",
    "JLinkTool",
    "NrfjprogTool",
    "NrfutilTool",
    "SwdTool",
    "Target",
    "ToolCommand",
    "ToolError",
    "ToolInfo",
    "choose_tool",
    "detect_tools",
    "format_command",
    "jlink_executable_name",
    "make_tool",
    "parse_memory_dump",
    "subprocess_runner",
    "target_for_board",
]
