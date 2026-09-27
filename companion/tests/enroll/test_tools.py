"""SWD tool detection, argv builders, J-Link command files, running and memory-dump parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from cremind_tag.enroll.hardware import TAG_BOARDS
from cremind_tag.enroll.tools import (
    TARGETS,
    CompletedResult,
    EraseMode,
    Family,
    JLinkTool,
    NrfjprogTool,
    NrfutilTool,
    ToolCommand,
    ToolError,
    choose_tool,
    detect_tools,
    format_command,
    jlink_executable_name,
    make_tool,
    parse_memory_dump,
    subprocess_runner,
    target_for_board,
)
from cremind_tag.protocol.enrollment import BOARD_SOC, UICR_CUSTOMER_ADDR
from cremind_tag.protocol.ids import Board

ADDR = UICR_CUSTOMER_ADDR["nrf52"]
HEX = Path("/work/tag") / "1A2B3C4D-uicr.hex"
FW = Path("/work/build") / "zephyr.hex"


class Recorder:
    """A runner that answers from a table and records every argv."""

    def __init__(self, answers: dict[str, CompletedResult] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
        self.calls.append(argv)
        for key, result in self.answers.items():
            if key in " ".join(argv):
                return result
        return CompletedResult(0, "", "")


def fake_which(available: dict[str, str]):  # type: ignore[no-untyped-def]
    return lambda name: available.get(name)


def no_glob(pattern: str) -> list[str]:
    return []


# -- targets ----------------------------------------------------------------------------------


def test_targets_cover_every_tag_board_with_the_right_family() -> None:
    assert set(TARGETS) == set(TAG_BOARDS)
    for board, target in TARGETS.items():
        assert target.soc == BOARD_SOC[board]
    assert target_for_board(Board.LAOWU_BW_NRF51822).jlink_device == "nRF51822_xxAB"
    assert target_for_board(Board.LAOWU_BWR_NRF51802).jlink_device == "nRF51822_xxAA"
    assert target_for_board(Board.SIFEI_NRF52810).jlink_device == "nRF52810_xxAA"
    assert target_for_board(Board.HEMA_NRF52811).jlink_device == "nRF52811_xxAA"
    assert target_for_board(20).jlink_device == "nRF52832_xxAA"
    with pytest.raises(ValueError, match="not a tag board"):
        target_for_board(Board.NRF52840DK_GATEWAY)


# -- detection ---------------------------------------------------------------------------------


def test_detect_prefers_nrfutil_with_device_command() -> None:
    runner = Recorder({"device --version": CompletedResult(0, "nrfutil-device 2.7.10\n", "")})
    which = fake_which({"nrfutil": "/usr/bin/nrfutil", "nrfjprog": "/usr/bin/nrfjprog",
                        "JLinkExe": "/usr/bin/JLinkExe"})
    tools = detect_tools(which, runner, platform="linux", glob=no_glob)
    assert [(t.name, t.path) for t in tools] == [
        ("nrfutil", "/usr/bin/nrfutil"), ("nrfjprog", "/usr/bin/nrfjprog"), ("jlink", "/usr/bin/JLinkExe")]
    assert tools[0].detail == "nrfutil-device 2.7.10"
    # Only nrfutil is probed; J-Link Commander would wait for interactive input.
    assert runner.calls == [["/usr/bin/nrfutil", "device", "--version"]]


def test_detect_skips_nrfutil_without_device_command() -> None:
    runner = Recorder({"device --version": CompletedResult(1, "", "error: unrecognized subcommand 'device'\n")})
    which = fake_which({"nrfutil": "/usr/bin/nrfutil", "nrfjprog": "/usr/bin/nrfjprog"})
    assert [t.name for t in detect_tools(which, runner, platform="linux", glob=no_glob)] == ["nrfjprog"]


def test_detect_skips_nrfutil_that_cannot_start() -> None:
    def runner(argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
        raise ToolError("nrfutil not found", argv=argv)

    which = fake_which({"nrfutil": "/usr/bin/nrfutil"})
    assert detect_tools(which, runner, platform="linux", glob=no_glob) == []


def test_detect_finds_jlink_in_default_windows_dirs() -> None:
    installs = [r"C:\Program Files\SEGGER\JLink_V794e\JLink.exe", r"C:\Program Files\SEGGER\JLink_V810a\JLink.exe",
                r"C:\Program Files\SEGGER\JLink_V810\JLink.exe"]
    seen: list[str] = []

    def glob(pattern: str) -> list[str]:
        seen.append(pattern)
        return installs if pattern.startswith(r"C:\Program Files\SEGGER") else []

    tools = detect_tools(fake_which({}), Recorder(), platform="win32", glob=glob)
    assert [(t.name, t.path) for t in tools] == [("jlink", r"C:\Program Files\SEGGER\JLink_V810a\JLink.exe")]
    assert r"C:\Program Files\SEGGER\JLink*\JLink.exe" in seen
    assert r"C:\Program Files (x86)\SEGGER\JLink*\JLink.exe" in seen


def test_detect_uses_path_before_default_dirs() -> None:
    which = fake_which({"JLink.exe": r"D:\tools\JLink.exe"})
    default = [r"C:\Program Files\SEGGER\JLink\JLink.exe"]
    tools = detect_tools(which, Recorder(), platform="win32", glob=lambda p: default if "SEGGER" in p else [])
    assert [t.name for t in tools] == ["jlink"]
    assert tools[-1].path == r"D:\tools\JLink.exe"
    assert jlink_executable_name("win32") == "JLink.exe"
    assert jlink_executable_name("linux") == "JLinkExe"
    assert jlink_executable_name("darwin") == "JLinkExe"


def test_choose_tool_auto_and_forced() -> None:
    runner = Recorder({"device --version": CompletedResult(0, "2.7.10\n", "")})
    which = fake_which({"nrfutil": "/bin/nrfutil", "nrfjprog": "/bin/nrfjprog", "JLinkExe": "/bin/JLinkExe"})
    kw = {"runner": runner, "which": which, "platform": "linux", "glob": no_glob}
    auto = choose_tool("auto", Board.SIFEI_NRF52810, **kw)  # type: ignore[arg-type]
    assert isinstance(auto, NrfutilTool) and auto.executable == "/bin/nrfutil"
    assert auto.target.jlink_device == "nRF52810_xxAA"
    forced = choose_tool("jlink", Board.SIFEI_NRF52810, serial_number="682000123", **kw)  # type: ignore[arg-type]
    assert isinstance(forced, JLinkTool) and forced.executable == "/bin/JLinkExe"
    assert forced.serial_number == "682000123"
    assert isinstance(choose_tool("NRFJPROG", Board.SIFEI_NRF52810, **kw), NrfjprogTool)  # type: ignore[arg-type]


def test_choose_tool_errors() -> None:
    kw = {"runner": Recorder(), "which": fake_which({"nrfjprog": "/bin/nrfjprog"}), "platform": "linux",
          "glob": no_glob}
    with pytest.raises(ToolError, match="jlink not found"):
        choose_tool("jlink", Board.LAOWU_BW_NRF51822, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown SWD tool"):
        choose_tool("openocd", Board.LAOWU_BW_NRF51822, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a tag board"):
        choose_tool("auto", Board.NRF52832_BRIDGE, **kw)  # type: ignore[arg-type]
    nothing = {**kw, "which": fake_which({})}
    with pytest.raises(ToolError, match="no SWD tool found"):
        choose_tool("auto", Board.LAOWU_BW_NRF51822, **nothing)  # type: ignore[arg-type]


def test_choose_tool_allow_missing_shows_bare_executable() -> None:
    kw = {"runner": Recorder(), "which": fake_which({}), "glob": no_glob, "allow_missing": True}
    tool = choose_tool("auto", Board.LAOWU_BW_NRF51822, platform="linux", **kw)  # type: ignore[arg-type]
    assert isinstance(tool, NrfutilTool) and tool.executable == "nrfutil"
    tool = choose_tool("jlink", Board.LAOWU_BW_NRF51822, platform="win32", **kw)  # type: ignore[arg-type]
    assert isinstance(tool, JLinkTool) and tool.executable == "JLink.exe"


# -- argv builders ----------------------------------------------------------------------------


def test_nrfutil_argv() -> None:
    tool = make_tool("nrfutil", "nrfutil", target_for_board(Board.SIFEI_NRF52810))
    assert list(tool.program_command(FW, erase=EraseMode.ALL).argv) == [
        "nrfutil", "device", "program", "--firmware", str(FW),
        "--options", "chip_erase_mode=ERASE_ALL,verify=VERIFY_READ"]
    assert list(tool.program_command(HEX).argv)[-1] == "chip_erase_mode=ERASE_NONE,verify=VERIFY_READ"
    assert list(tool.program_command(HEX, erase=EraseMode.TOUCHED, verify=False).argv)[-1] == (
        "chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE,verify=VERIFY_NONE")
    [uicr] = tool.program_uicr_commands(HEX)
    assert list(uicr.argv)[-1] == "chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE,verify=VERIFY_READ"
    assert list(tool.erase_all_command().argv) == ["nrfutil", "device", "erase", "--all"]
    read = tool.read_memory_command(ADDR, 48)
    assert list(read.argv) == ["nrfutil", "device", "read", "--address", "0x10001080", "--bytes", "48"]
    assert read.sensitive_output
    assert list(tool.protect_command().argv) == ["nrfutil", "device", "protection-set", "All"]
    assert list(tool.reset_command().argv) == ["nrfutil", "device", "reset"]


def test_nrfutil_serial_number() -> None:
    tool = make_tool("nrfutil", "nrfutil", target_for_board(Board.SIFEI_NRF52810), serial_number="682000123")
    for command in (tool.program_command(FW), tool.read_memory_command(ADDR, 48), tool.protect_command(),
                    tool.reset_command(), tool.erase_all_command()):
        assert list(command.argv[-2:]) == ["--serial-number", "682000123"]


def test_nrfjprog_argv() -> None:
    nrf51 = make_tool("nrfjprog", "nrfjprog", target_for_board(Board.LAOWU_BW_NRF51822))
    nrf52 = make_tool("nrfjprog", "nrfjprog", target_for_board(Board.HEMA_NRF52811), serial_number="682000123")
    assert list(nrf51.program_command(FW, erase=EraseMode.ALL).argv) == [
        "nrfjprog", "-f", "NRF51", "--program", str(FW), "--chiperase", "--verify"]
    assert list(nrf51.program_command(HEX).argv) == ["nrfjprog", "-f", "NRF51", "--program", str(HEX), "--verify"]
    assert "--sectorerase" in nrf51.program_command(HEX, erase=EraseMode.TOUCHED).argv
    assert list(nrf52.program_command(HEX, erase=EraseMode.TOUCHED, verify=False).argv) == [
        "nrfjprog", "-f", "NRF52", "--program", str(HEX), "--sectoranduicrerase", "--snr", "682000123"]
    assert [list(c.argv) for c in nrf51.program_uicr_commands(HEX)] == [
        ["nrfjprog", "-f", "NRF51", "--eraseuicr"],
        ["nrfjprog", "-f", "NRF51", "--program", str(HEX), "--verify"]]
    assert list(nrf51.erase_all_command().argv) == ["nrfjprog", "-f", "NRF51", "--eraseall"]
    assert list(nrf51.read_memory_command(ADDR, 48).argv) == [
        "nrfjprog", "-f", "NRF51", "--memrd", "0x10001080", "--n", "48", "--w", "32"]
    assert nrf51.read_memory_command(ADDR, 46).argv[6] == "48"  # whole words
    assert list(nrf52.protect_command().argv) == ["nrfjprog", "-f", "NRF52", "--rbp", "ALL", "--snr", "682000123"]
    assert list(nrf52.reset_command().argv) == ["nrfjprog", "-f", "NRF52", "--reset", "--snr", "682000123"]


def _script_lines(command: ToolCommand) -> list[str]:
    assert command.script is not None
    return command.script.splitlines()


def test_jlink_argv_and_command_files(tmp_path: Path) -> None:
    tool = make_tool("jlink", "JLinkExe", target_for_board(Board.SIFEI_NRF52810), serial_number="682000123",
                     workdir=tmp_path)
    program = tool.program_command(FW, erase=EraseMode.ALL)
    assert program.script_path is not None and program.script_path.parent == tmp_path
    assert list(program.argv) == [
        "JLinkExe", "-SelectEmuBySN", "682000123", "-device", "nRF52810_xxAA", "-if", "SWD", "-speed", "4000",
        "-autoconnect", "1", "-NoGui", "1", "-ExitOnError", "1", "-CommanderScript", str(program.script_path)]
    assert _script_lines(program) == ["r", "h", "erase", f"loadfile {FW}", "qc"]
    assert _script_lines(tool.program_command(FW)) == ["r", "h", f"loadfile {FW}", "qc"]
    [uicr] = tool.program_uicr_commands(HEX)
    assert _script_lines(uicr) == ["r", "h", "w4 0x4001E504 2", "w4 0x4001E514 1", "Sleep 200",
                                   "w4 0x4001E504 0", f"loadfile {HEX}", "qc"]
    assert _script_lines(tool.erase_all_command()) == ["r", "h", "erase", "qc"]
    read = tool.read_memory_command(ADDR, 48)
    assert _script_lines(read) == ["h", "mem32 0x10001080, 0x0C", "qc"] and read.sensitive_output
    assert _script_lines(tool.protect_command()) == [
        "r", "h", "w4 0x4001E504 1", "w4 0x10001208 0xFFFFFF00", "w4 0x4001E504 0", "qc"]
    assert _script_lines(tool.reset_command()) == ["r", "g", "qc"]
    # Pure builders: the same operation gives the same command file path.
    assert tool.program_command(FW, erase=EraseMode.ALL) == program
    assert tool.reset_command().script_path != tool.protect_command().script_path


def test_jlink_nrf51_protect_and_quoting(tmp_path: Path) -> None:
    tool = make_tool("jlink", "JLinkExe", target_for_board(Board.LAOWU_BWR_NRF51802), workdir=tmp_path)
    assert "-SelectEmuBySN" not in tool.reset_command().argv
    assert tool.reset_command().argv[2] == "nRF51822_xxAA"
    # nRF51 UICR.RBPCONF: PALL (bits 15:8) = 0x00 protects everything.
    assert "w4 0x10001004 0xFFFF00FF" in _script_lines(tool.protect_command())
    spaced = Path("C:/Users/Jane Doe/AppData/Local/cremind-tag/enroll/1A2B3C4D-uicr.hex")
    assert f'loadfile "{spaced}"' in _script_lines(tool.program_command(spaced))


def test_render_lists_the_command_file(tmp_path: Path) -> None:
    tool = make_tool("jlink", "JLinkExe", target_for_board(Board.NRF52DK_TAG), workdir=tmp_path)
    text = tool.reset_command().render("linux")
    assert text.splitlines()[0].startswith("JLinkExe -device nRF52832_xxAA")
    assert text.splitlines()[1:] == [f"  # {tool.reset_command().script_path}:", "  r", "  g", "  qc"]
    assert make_tool("nrfjprog", "nrfjprog", tool.target).reset_command().render("linux") == (
        "nrfjprog -f NRF52 --reset")


def test_format_command_quotes_per_platform() -> None:
    argv = ["nrfutil", "device", "program", "--firmware", r"C:\My Tags\uicr.hex"]
    assert format_command(argv, "win32") == r'nrfutil device program --firmware "C:\My Tags\uicr.hex"'
    assert format_command(["nrfjprog", "--program", "/tmp/my tags/u.hex"], "linux") == (
        "nrfjprog --program '/tmp/my tags/u.hex'")


# -- running ------------------------------------------------------------------------------------


def test_run_records_history_and_raises_with_command_and_stderr() -> None:
    runner = Recorder({"--eraseuicr": CompletedResult(33, "", "ERROR: Unable to connect to a debugger.\n")})
    tool = make_tool("nrfjprog", "nrfjprog", target_for_board(Board.LAOWU_BW_NRF51822), runner=runner)
    tool.reset()
    with pytest.raises(ToolError) as info:
        tool.program_uicr(HEX)
    message = str(info.value)
    assert "erase UICR failed" in message
    assert "nrfjprog -f NRF51 --eraseuicr" in message
    assert "exit status: 33" in message and "Unable to connect to a debugger" in message
    assert info.value.returncode == 33
    assert [c.description for c in tool.history] == ["reset", "erase UICR"]
    assert runner.calls == [list(c.argv) for c in tool.history]


def test_readback_output_never_reaches_errors(fixture_blob: bytes, fakes: ModuleType) -> None:
    dump = fakes.nrfjprog_dump(fixture_blob, ADDR)
    tool = make_tool("nrfjprog", "nrfjprog", target_for_board(Board.LAOWU_BWR_NRF51802),
                     runner=Recorder({"--memrd": CompletedResult(1, dump, "ERROR: read failed\n")}))
    with pytest.raises(ToolError) as info:
        tool.read_memory(ADDR, 48)
    assert "read failed" in str(info.value)
    assert "4D3C2B1A" not in str(info.value) and "404142" not in str(info.value)
    # A dump that does not cover the range: the parse error names no data either.
    tool.runner = Recorder({"--memrd": CompletedResult(0, dump.splitlines()[0] + "\n", "")})
    with pytest.raises(ToolError, match="cannot parse") as info:
        tool.read_memory(ADDR, 48)
    assert "47415443" not in str(info.value)


def test_jlink_script_exists_during_run_and_is_removed(tmp_path: Path) -> None:
    seen: list[str] = []

    def runner(argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
        seen.append(Path(argv[-1]).read_text(encoding="ascii"))
        return CompletedResult(0, "Reset delay: 0 ms\n", "")

    tool = make_tool("jlink", "JLinkExe", target_for_board(Board.SIFEI_NRF52810), runner=runner, workdir=tmp_path)
    tool.reset()
    assert seen == ["r\ng\nqc\n"]
    assert list(tmp_path.glob("*.jlink")) == []
    assert tool.history[0].script == "r\ng\nqc\n"


def test_jlink_errors_reported_only_in_output(tmp_path: Path) -> None:
    runner = Recorder({"-CommanderScript": CompletedResult(0, "Connecting to target...\nCannot connect to target.\n",
                                                          "")})
    tool = make_tool("jlink", "JLinkExe", target_for_board(Board.SIFEI_NRF52810), runner=runner, workdir=tmp_path)
    with pytest.raises(ToolError, match="Cannot connect to target"):
        tool.reset()


def test_subprocess_runner_runs_without_a_shell() -> None:
    result = subprocess_runner([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
                               timeout=60)
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (0, "out", "err")
    result = subprocess_runner([sys.executable, "-c", "import sys; sys.exit(sys.stdin.read() == 'x' and 3)"],
                               input="x", timeout=60)
    assert result.returncode == 3
    with pytest.raises(ToolError, match="not found"):
        subprocess_runner(["cremind-tag-no-such-tool-xyz"], timeout=10)


# -- memory dumps -------------------------------------------------------------------------------


def test_parse_nrfjprog_word_dump(fixture_blob: bytes, fakes: ModuleType) -> None:
    text = "Reading 48 bytes\n" + fakes.nrfjprog_dump(fixture_blob, ADDR)
    assert text.splitlines()[1] == (
        "0x10001080: 47415443 00021101 1A2B3C4D 43424140   |CTAG....M<+.@ABC|")
    assert parse_memory_dump(text, ADDR, 48) == fixture_blob
    # The ASCII column holds '[', '\\' and ']' (secret bytes 0x5B..0x5D): not JSON.
    assert "[" in text


def test_parse_jlink_mem32_dump(fixture_blob: bytes, fakes: ModuleType) -> None:
    text = fakes.jlink_dump(fixture_blob, ADDR)
    assert "10001080 = 47415443 00021101 1A2B3C4D 43424140 " in text.splitlines()
    assert parse_memory_dump(text, ADDR, 48) == fixture_blob


def test_parse_byte_dump(fixture_blob: bytes, fakes: ModuleType) -> None:
    text = fakes.byte_dump(fixture_blob, ADDR)
    assert text.startswith("0x10001080: 43 54 41 47 01 11 02 00")
    assert parse_memory_dump(text, ADDR, 48) == fixture_blob


def test_parse_sub_range_and_mixed_case(fixture_blob: bytes, fakes: ModuleType) -> None:
    text = fakes.nrfjprog_dump(fixture_blob, ADDR).lower()
    assert parse_memory_dump(text, ADDR + 8, 8) == fixture_blob[8:16]
    halfwords = "10001080: " + " ".join(f"{int.from_bytes(fixture_blob[i:i + 2], 'little'):04X}"
                                        for i in range(0, 16, 2))
    assert parse_memory_dump(halfwords, ADDR, 16) == fixture_blob[:16]


def test_parse_json_dumps(fixture_blob: bytes) -> None:
    assert parse_memory_dump(json.dumps({"data": list(fixture_blob)}), ADDR, 48) == fixture_blob
    assert parse_memory_dump(json.dumps({"result": {"memory": fixture_blob.hex()}}), ADDR, 48) == fixture_blob
    words = [int.from_bytes(fixture_blob[i:i + 4], "little") for i in range(0, 48, 4)]
    assert parse_memory_dump(json.dumps([{"address": ADDR, "words": words}]), ADDR, 48) == fixture_blob
    ndjson = (json.dumps({"type": "task_begin", "data": {"task": {"name": "read"}}}) + "\n"
              + json.dumps({"type": "task_end", "data": {"data": list(fixture_blob)}}) + "\n")
    assert parse_memory_dump(ndjson, ADDR, 48) == fixture_blob


def test_parse_json_without_data_falls_back_to_text(fixture_blob: bytes, fakes: ModuleType) -> None:
    text = json.dumps({"type": "info", "serial": "682000123"}) + "\n" + fakes.nrfjprog_dump(fixture_blob, ADDR)
    assert parse_memory_dump(text, ADDR, 48) == fixture_blob


def test_parse_incomplete_dump_fails(fixture_blob: bytes, fakes: ModuleType) -> None:
    lines = fakes.nrfjprog_dump(fixture_blob, ADDR).splitlines()
    with pytest.raises(ValueError, match="first missing byte 0x100010A0"):
        parse_memory_dump("\n".join(lines[:2]), ADDR, 48)
    with pytest.raises(ValueError):
        parse_memory_dump("ERROR: no debugger\n", ADDR, 48)


def test_family_values_match_nrfjprog() -> None:
    assert [f.value for f in Family] == ["NRF51", "NRF52"]
    assert NrfjprogTool.name == "nrfjprog" and NrfutilTool.name == "nrfutil" and JLinkTool.name == "jlink"
