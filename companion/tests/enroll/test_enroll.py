"""enroll_tag against a simulated target: order of operations, verification, failure cleanup, APPROTECT, dry runs."""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cremind_tag.enroll import (
    PROTECT_WARNING,
    EnrollError,
    PanelProfile,
    ReadbackError,
    SwdTool,
    ToolError,
    enroll_tag,
    make_tool,
    target_for_board,
    write_enrollment_hex,
)
from cremind_tag.protocol.enrollment import intel_hex, pack_blob
from cremind_tag.protocol.ids import Board, Panel

enroll_mod = importlib.import_module("cremind_tag.enroll.enroll")

ADDR = 0x10001080
FIX_ID = 0x1A2B3C4D
BOARD = Board.LAOWU_BWR_NRF51802
APP = bytes(range(0x80, 0xA0))  # the application already on the tag
OLD_BLOB = pack_blob(0x0BADCAFE, bytes(32), BOARD, Panel.UC8176_420_BWR)  # a previous enrollment


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch, enrollment_fixture: dict[str, Any]) -> bytes:
    value = bytes.fromhex(enrollment_fixture["fields"]["secret"])
    monkeypatch.setattr(enroll_mod, "_new_secret", lambda: value)
    return value


@pytest.fixture
def device(fakes: ModuleType) -> Any:
    memory = {i: b for i, b in enumerate(APP)}
    memory.update({ADDR + i: b for i, b in enumerate(OLD_BLOB)})
    return fakes.FakeDevice(memory=memory)


@pytest.fixture
def firmware(tmp_path: Path) -> Path:
    path = tmp_path / "build" / "zephyr.hex"
    path.parent.mkdir()
    path.write_text(intel_hex(bytes(range(64)), 0x0), encoding="ascii")
    return path


def tool_for(name: str, runner: Any, out_dir: Path, board: Board = BOARD) -> SwdTool:
    return make_tool(name, name, target_for_board(board), runner=runner, workdir=out_dir)


def run(db: Any, store: Any, out_dir: Path, tool: SwdTool | str | None, **kw: Any) -> Any:
    args: dict[str, Any] = {"board": "laowu_bwr", "panel": "bwr", "db": db, "secrets": store, "out_dir": out_dir,
                            "tool": tool, "tag_id": FIX_ID}
    args.update(kw)
    return enroll_tag(**args)


# -- real runs -------------------------------------------------------------------------------------


def test_real_run_rewrites_only_uicr(db: Any, store: Any, out_dir: Path, device: Any, fakes: ModuleType,
                                     secret: bytes, fixture_blob: bytes) -> None:
    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), name="kitchen")

    assert fakes.call_names(device.calls) == ["nrfjprog:eraseuicr", "nrfjprog:program", "nrfjprog:memrd",
                                              "nrfjprog:reset"]
    assert device.read(ADDR, 48) == fixture_blob
    assert device.read(0, len(APP)) == APP  # the application stays
    assert store.get_tag_secret(FIX_ID) == secret
    row = db.get_tag(FIX_ID)
    assert (row.board, row.panel, row.width, row.height, row.planes, row.plane_flags) == (17, 2, 400, 300, 2, 0x03)
    assert (row.secret_ref, row.name, row.fw, row.protected) == ("file:tag:1A2B3C4D", "kitchen", None, False)
    assert row.enrolled_at
    assert result.record == row and result.registered and result.secret_ref == row.secret_ref
    assert not result.hex_path.exists() and not result.hex_kept
    assert result.executed == result.plan
    assert [list(c.argv) for c in result.executed] == device.calls
    assert (result.hw_id, result.tool, result.dry_run, result.warnings) == ("1A2B3C4D", "nrfjprog", False, [])
    assert device.resets == 1


def test_uicr_must_be_erased_before_rewriting(fakes: ModuleType, device: Any, fixture_blob: bytes) -> None:
    """The fake behaves like NOR flash, so the erase in the plan is what makes the readback pass."""
    device.program({ADDR + i: b for i, b in enumerate(fixture_blob)})
    assert device.read(ADDR, 48) != fixture_blob


def test_real_run_with_firmware_via_jlink(db: Any, store: Any, out_dir: Path, device: Any, fakes: ModuleType,
                                         secret: bytes, fixture_blob: bytes, firmware: Path) -> None:
    device.memory[0x2000] = 0x00  # beyond the new image: a full erase clears it
    result = run(db, store, out_dir, tool_for("jlink", device, out_dir), firmware=firmware)

    assert fakes.call_names(device.calls) == ["jlink:program", "jlink:program", "jlink:read", "jlink:reset"]
    fw_script, uicr_script = device.scripts[0].splitlines(), device.scripts[1].splitlines()
    assert "erase" in fw_script and f"loadfile {firmware}" in fw_script
    assert "erase" not in uicr_script and f"loadfile {result.hex_path}" in uicr_script
    assert device.read(0, 64) == bytes(range(64))
    assert device.read(0x2000, 1) == b"\xff"
    assert device.read(ADDR, 48) == fixture_blob
    assert db.tag_exists(FIX_ID) and store.has_tag_secret(FIX_ID)
    assert list(out_dir.glob("*.jlink")) == [] and not result.hex_path.exists()


def test_real_run_via_nrfutil_reads_back_json(db: Any, store: Any, out_dir: Path, device: Any, fakes: ModuleType,
                                             secret: bytes, fixture_blob: bytes) -> None:
    run(db, store, out_dir, tool_for("nrfutil", device, out_dir))
    assert fakes.call_names(device.calls) == ["nrfutil:program", "nrfutil:read", "nrfutil:reset"]
    assert device.calls[0][-1] == "chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE,verify=VERIFY_READ"
    assert device.read(ADDR, 48) == fixture_blob and db.tag_exists(FIX_ID)


@pytest.mark.parametrize("name", ["nrfutil", "nrfjprog", "jlink"])
@pytest.mark.parametrize("with_firmware", [False, True])
def test_every_tool_enrolls(name: str, with_firmware: bool, db: Any, store: Any, out_dir: Path, device: Any,
                            secret: bytes, fixture_blob: bytes, firmware: Path) -> None:
    result = run(db, store, out_dir, tool_for(name, device, out_dir),
                 firmware=firmware if with_firmware else None, protect=True)
    assert device.read(ADDR, 48) == fixture_blob
    assert device.read(0, 16) == (bytes(range(16)) if with_firmware else APP[:16])
    assert device.protected and result.protected and db.get_tag(FIX_ID).protected
    assert device.resets == 1


def test_secret_is_stored_before_the_first_command(db: Any, store: Any, out_dir: Path, device: Any,
                                                   secret: bytes) -> None:
    seen: list[bool] = []

    def runner(argv: list[str], *, input: str | None = None, timeout: float) -> Any:
        seen.append(store.has_tag_secret(FIX_ID))
        return device(argv, input=input, timeout=timeout)

    run(db, store, out_dir, tool_for("nrfjprog", runner, out_dir))
    assert seen and all(seen)


def test_readback_mismatch_forgets_the_secret(db: Any, store: Any, out_dir: Path, fakes: ModuleType,
                                              secret: bytes) -> None:
    device = fakes.FakeDevice(corrupt_readback=True)
    with pytest.raises(ReadbackError, match="does not match"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir))
    assert not store.has_tag_secret(FIX_ID)
    assert not db.tag_exists(FIX_ID)
    assert not (out_dir / "1A2B3C4D-uicr.hex").exists()
    assert fakes.call_names(device.calls)[-1] == "nrfjprog:memrd"  # no reset, no protect


def test_tool_failure_forgets_the_secret(db: Any, store: Any, out_dir: Path, fakes: ModuleType,
                                         secret: bytes) -> None:
    device = fakes.FakeDevice(fail_on="--eraseuicr")
    with pytest.raises(ToolError, match="erase UICR failed"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), keep_hex=True)
    assert not store.has_tag_secret(FIX_ID) and not db.tag_exists(FIX_ID)
    assert (out_dir / "1A2B3C4D-uicr.hex").exists()  # kept on request, for debugging


def test_inventory_failure_forgets_the_secret(db: Any, store: Any, out_dir: Path, device: Any,
                                              monkeypatch: pytest.MonkeyPatch, secret: bytes) -> None:
    def broken(record: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_tag", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir))
    assert not store.has_tag_secret(FIX_ID)


# -- APPROTECT ----------------------------------------------------------------------------------------


def test_protect_confirmed(db: Any, store: Any, out_dir: Path, device: Any, fakes: ModuleType,
                           secret: bytes) -> None:
    asked: list[str] = []

    def confirm(text: str) -> bool:
        asked.append(text)
        return True

    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), protect=True, confirm_protect=confirm)
    assert asked == [PROTECT_WARNING]
    assert fakes.call_names(device.calls) == ["nrfjprog:eraseuicr", "nrfjprog:program", "nrfjprog:memrd",
                                              "nrfjprog:rbp", "nrfjprog:reset"]
    assert result.protected and not result.protect_declined and device.protected
    assert db.get_tag(FIX_ID).protected and result.record is not None and result.record.protected


def test_protect_declined_skips_without_failing(db: Any, store: Any, out_dir: Path, device: Any,
                                                fakes: ModuleType, secret: bytes) -> None:
    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), protect=True,
                 confirm_protect=lambda text: False)
    assert "nrfjprog:rbp" not in fakes.call_names(device.calls)
    assert result.protect_declined and not result.protected and not device.protected
    assert not db.get_tag(FIX_ID).protected and device.resets == 1
    assert len(result.executed) == len(result.plan) - 1  # the protect command was planned, not run


def test_protect_warning_text() -> None:
    text = PROTECT_WARNING.lower()
    assert "irreversible" in text and "full chip erase" in text
    assert "erases" in text and "secret" in text and "re-enrolled" in text
    assert "swd" in text and "read its secret" in text


def test_protect_failure_keeps_the_enrolled_tag(db: Any, store: Any, out_dir: Path, fakes: ModuleType,
                                                secret: bytes) -> None:
    device = fakes.FakeDevice(fail_on="--rbp")
    with pytest.raises(EnrollError, match="enrolled, but enabling APPROTECT failed"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), protect=True)
    assert db.tag_exists(FIX_ID) and not db.get_tag(FIX_ID).protected
    assert store.has_tag_secret(FIX_ID)
    assert not (out_dir / "1A2B3C4D-uicr.hex").exists()


def test_reset_failure_is_a_warning(db: Any, store: Any, out_dir: Path, fakes: ModuleType, secret: bytes) -> None:
    device = fakes.FakeDevice(fail_reset=True)
    result = run(db, store, out_dir, tool_for("jlink", device, out_dir))
    assert db.tag_exists(FIX_ID) and result.registered
    assert len(result.warnings) == 1 and "power-cycle" in result.warnings[0]


# -- dry runs --------------------------------------------------------------------------------------


def test_write_enrollment_hex_matches_fixture(tmp_path: Path, enrollment_fixture: dict[str, Any],
                                              fixture_blob: bytes) -> None:
    path = write_enrollment_hex(tmp_path / "x" / "uicr.hex", fixture_blob, BOARD)
    assert path.read_bytes().decode("ascii").splitlines() == enrollment_fixture["intel_hex"]
    assert b"\r" not in path.read_bytes()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_hex_file_is_private(tmp_path: Path, fixture_blob: bytes) -> None:
    path = tmp_path / "uicr.hex"
    path.write_text("old")
    path.chmod(0o644)
    write_enrollment_hex(path, fixture_blob, BOARD)
    assert path.stat().st_mode & 0o777 == 0o600


def test_dry_run_touches_nothing(db: Any, store: Any, out_dir: Path, device: Any, fakes: ModuleType,
                                 secret: bytes, enrollment_fixture: dict[str, Any]) -> None:
    tool = tool_for("nrfjprog", device, out_dir)
    result = run(db, store, out_dir, tool, dry_run=True, protect=True, confirm_protect=lambda t: pytest.fail("asked"))

    assert device.calls == [] and tool.history == []
    assert db.list_tags() == [] and not store.has_tag_secret(FIX_ID)
    assert not (store.backend.path).exists()
    hex_path = out_dir / "1A2B3C4D-uicr.hex"
    assert result.hex_path == hex_path.resolve() and result.hex_kept
    assert hex_path.read_text(encoding="ascii").splitlines() == enrollment_fixture["intel_hex"]
    assert [c.description for c in result.plan] == [
        "erase UICR", "program 1A2B3C4D-uicr.hex (erase: none)", "read 48 bytes at 0x10001080",
        "enable readback protection (APPROTECT)", "reset"]
    assert result.commands == [c.display() for c in result.plan]
    assert result.commands[0].startswith("nrfjprog -f NRF51 --eraseuicr")
    assert str(hex_path.resolve()) in result.commands[1]
    assert not result.registered and result.record is None and result.secret_ref is None
    assert any("--register" in w for w in result.warnings)


def test_dry_run_jlink_writes_command_files(db: Any, store: Any, out_dir: Path, device: Any, secret: bytes,
                                            firmware: Path) -> None:
    result = run(db, store, out_dir, tool_for("jlink", device, out_dir), dry_run=True, firmware=firmware)
    assert device.calls == []
    for command in result.plan:
        assert command.script_path is not None and command.script_path.read_text(encoding="ascii") == command.script
    rendered = result.render_plan("linux")
    assert f"  loadfile {firmware}" in rendered and "  mem32 0x10001080, 0x0C" in rendered


def test_dry_run_register_stores_secret_and_row(db: Any, store: Any, out_dir: Path, device: Any,
                                                secret: bytes) -> None:
    result = run(db, store, out_dir, tool_for("nrfutil", device, out_dir), dry_run=True, register=True,
                 protect=True)
    assert device.calls == []
    assert store.get_tag_secret(FIX_ID) == secret
    row = db.get_tag(FIX_ID)
    assert row.secret_ref == "file:tag:1A2B3C4D" and not row.protected
    assert result.registered and result.record == row and result.hex_path.exists()
    assert any("unprotected" in w for w in result.warnings)


def test_tool_preference_is_resolved_with_allow_missing(db: Any, store: Any, out_dir: Path,
                                                        monkeypatch: pytest.MonkeyPatch, secret: bytes) -> None:
    calls: list[dict[str, Any]] = []
    real = enroll_mod.choose_tool

    def choose(preference: str, board: Board, **kw: Any) -> SwdTool:
        calls.append({"preference": preference, "board": board, **kw})
        return real(preference, board, which=lambda name: None, glob=lambda pattern: [], platform="linux", **kw)

    monkeypatch.setattr(enroll_mod, "choose_tool", choose)
    result = run(db, store, out_dir, None, dry_run=True, serial_number="682000123")
    assert result.tool == "nrfutil"
    assert result.commands[0].startswith("nrfutil device program")
    assert result.commands[0].endswith("--serial-number 682000123")
    assert calls[0]["preference"] == "auto" and calls[0]["allow_missing"] is True
    assert calls[0]["workdir"] == out_dir.resolve()

    with pytest.raises(ToolError, match="jlink not found"):
        run(db, store, out_dir, "jlink", tag_id=FIX_ID + 1)
    assert calls[1]["allow_missing"] is False
    assert not store.has_tag_secret(FIX_ID + 1) and not db.tag_exists(FIX_ID + 1)


# -- arguments --------------------------------------------------------------------------------------


def test_random_tag_id_skips_used_ids(db: Any, store: Any, out_dir: Path, device: Any,
                                      monkeypatch: pytest.MonkeyPatch, secret: bytes) -> None:
    run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), dry_run=True, register=True)  # FIX_ID is used
    store.set_tag_secret(0x00000002, bytes(32))  # an orphan secret
    ids: Iterator[int] = iter([FIX_ID, 0x00000002, 0x00000003])
    monkeypatch.setattr(enroll_mod, "_random_tag_id", lambda: next(ids))
    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), dry_run=True, tag_id=None)
    assert result.tag_id == 3 and result.hw_id == "00000003"


def test_random_tag_id_range() -> None:
    values = {enroll_mod._random_tag_id() for _ in range(200)}
    assert all(1 <= v <= 0xFFFFFFFE for v in values) and len(values) > 190


def test_explicit_tag_id_must_be_unused(db: Any, store: Any, out_dir: Path, device: Any, secret: bytes) -> None:
    run(db, store, out_dir, tool_for("nrfjprog", device, out_dir))
    with pytest.raises(EnrollError, match="already enrolled"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir))
    for bad in (0, 0xFFFFFFFF):
        with pytest.raises(ValueError, match="out of range"):
            run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), tag_id=bad)


def test_unverified_panel_needs_geometry(db: Any, store: Any, out_dir: Path, fakes: ModuleType,
                                         secret: bytes) -> None:
    device = fakes.FakeDevice()
    tool = tool_for("nrfjprog", device, out_dir, board=Board.SIFEI_NRF52810)
    with pytest.raises(ValueError, match="no verified profile"):
        run(db, store, out_dir, tool, board="sifei_52810", panel=None)
    result = run(db, store, out_dir, tool, board="sifei_52810", panel=None, geometry=(296, 128, 2, 0x03))
    assert result.panel is Panel.UNVERIFIED and result.geometry == PanelProfile(296, 128, 2, 0x03)
    row = db.get_tag(FIX_ID)
    assert (row.panel, row.width, row.height, row.planes, row.plane_flags) == (255, 296, 128, 2, 0x03)
    assert device.calls[0][:3] == ["nrfjprog", "-f", "NRF52"]


def test_geometry_rules(db: Any, store: Any, out_dir: Path, device: Any, secret: bytes) -> None:
    tool = tool_for("nrfjprog", device, out_dir)
    with pytest.raises(ValueError, match="different geometry"):
        run(db, store, out_dir, tool, geometry=(296, 128, 2, 3), dry_run=True)
    result = run(db, store, out_dir, tool, geometry=(400, 300, 2, 3), dry_run=True)  # equal to the profile: fine
    assert result.geometry == PanelProfile(400, 300, 2, 3)
    with pytest.raises(ValueError, match="plane flags"):
        run(db, store, out_dir, tool, panel="unverified", geometry=(400, 300, 1, 3), dry_run=True)
    with pytest.raises(ValueError, match="not a tag board"):
        run(db, store, out_dir, tool, board="nrf52840_bridge", dry_run=True)
    with pytest.raises(ValueError, match="targets laowu_bwr_nrf51802"):
        run(db, store, out_dir, tool, board="laowu_bw", panel="bw", dry_run=True)


def test_unusual_panel_for_board_is_a_warning(db: Any, store: Any, out_dir: Path, device: Any,
                                              secret: bytes) -> None:
    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), panel="bw", dry_run=True)
    assert any("ships with panel uc8176_420_bwr" in w for w in result.warnings)


def test_firmware_is_checked_before_anything_happens(db: Any, store: Any, out_dir: Path, device: Any,
                                                     tmp_path: Path, secret: bytes) -> None:
    with pytest.raises(ValueError, match="firmware not found"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), firmware=tmp_path / "missing.hex")
    with pytest.raises(ValueError, match="Intel HEX"):
        run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), firmware=tmp_path / "zephyr.bin")
    assert device.calls == [] and not store.has_tag_secret(FIX_ID) and not out_dir.exists()
    result = run(db, store, out_dir, tool_for("nrfjprog", device, out_dir), firmware=tmp_path / "missing.hex",
                 dry_run=True)
    assert any("does not exist yet" in w for w in result.warnings)


def test_secret_and_image_never_logged(db: Any, store: Any, out_dir: Path, device: Any, secret: bytes,
                                       caplog: pytest.LogCaptureFixture, enrollment_fixture: dict[str, Any]) -> None:
    caplog.set_level(logging.DEBUG)
    run(db, store, out_dir, tool_for("jlink", device, out_dir), protect=True)
    text = caplog.text
    assert "1A2B3C4D" in text  # the tag id is logged
    assert secret.hex() not in text.lower()
    for word in ("43424140", "47464544", "4B4A4948", "5F5E5D5C"):  # little-endian words of the secret
        assert word not in text.upper()
    for line in enrollment_fixture["intel_hex"][1:4]:
        assert line not in text
