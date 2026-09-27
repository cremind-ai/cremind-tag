"""`cremind-tag firmware`: release/build discovery, verification, flashing and FICR reads with mocked tools."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from cremind_tag.cli import firmware as fw
from cremind_tag.cli.main import app
from cremind_tag.enroll.tools import CompletedResult, ToolInfo

REPO = Path(__file__).resolve().parents[3]
VERSION = "0.1.0"


# -- fixtures ----------------------------------------------------------------------------------------


def to_hex(data: bytes, base: int = 0) -> str:
    lines = []
    if base >> 16:
        rec = bytes([2, 0, 0, 4]) + (base >> 16).to_bytes(2, "big")
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    for off in range(0, len(data), 16):
        chunk, addr = data[off:off + 16], (base + off) & 0xFFFF
        rec = bytes([len(chunk), addr >> 8, addr & 0xFF, 0]) + chunk
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    return "\n".join([*lines, ":00000001FF"]) + "\n"


def _meta(target: str, role: str, board: str, soc: str, hex_sha: str, **over: Any) -> dict[str, Any]:
    meta = {"schema": "cremind-tag/build-metadata@1", "target": target, "role": role, "board": board, "soc": soc,
            "hardware": {"gateway": "nrf52840_gateway", "tag": "laowu_bw"}.get(role), "version": VERSION,
            "hardware_status": "buildable", "status": "ok",
            "verify_stack": {"ok": True, "counts": {"pass": 15}},
            "artifacts": {"zephyr.hex": {"sha256": hex_sha}}}
    meta.update(over)
    return meta


TARGETS = {
    "gateway-nrf52840dk": ("gateway", "nrf52840dk/nrf52840", "nrf52840_qiaa"),
    "tag-laowu-bw": ("tag", "laowu_bw/nrf51822", "nrf51822_qfab"),
}


@pytest.fixture()
def release(tmp_path: Path) -> Path:
    """dist/0.1.0 as tools/release.py writes it (two targets, SHA256SUMS)."""
    root = tmp_path / "dist" / VERSION
    firmware = []
    for name, (role, board, soc) in TARGETS.items():
        d = root / "firmware" / name
        d.mkdir(parents=True)
        hex_path = d / f"{name}-{VERSION}.hex"
        hex_path.write_bytes(to_hex(bytes(range(64))).encode())
        sha = hashlib.sha256(hex_path.read_bytes()).hexdigest()
        meta_path = d / f"{name}-{VERSION}.metadata.json"
        meta_path.write_text(json.dumps(_meta(name, role, board, soc, sha)), encoding="utf-8")
        firmware.append({"target": name, "role": role, "board": board, "soc": soc, "version": VERSION,
                         "hardware": _meta(name, role, board, soc, sha)["hardware"], "release_status": "buildable",
                         "files": {"hex": hex_path.relative_to(root).as_posix(),
                                   "metadata": meta_path.relative_to(root).as_posix()},
                         "sha256": {"hex": sha}, "verify_stack": {"ok": True}})
    (root / "release.json").write_text(json.dumps({"schema": "cremind-tag/release@1", "version": VERSION,
                                                   "firmware": firmware}), encoding="utf-8")
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}\n"
                   for p in sorted(root.rglob("*")) if p.is_file())
    (root / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return root


@pytest.fixture()
def build_dir(tmp_path: Path) -> Path:
    """build/ as tools/build.py writes it: <target>/zephyr.hex + metadata.json."""
    d = tmp_path / "build" / "gateway-nrf52840dk"
    d.mkdir(parents=True)
    (d / "zephyr.hex").write_bytes(to_hex(bytes(range(32))).encode())
    sha = hashlib.sha256((d / "zephyr.hex").read_bytes()).hexdigest()
    (d / "metadata.json").write_text(json.dumps(_meta("gateway-nrf52840dk", *TARGETS["gateway-nrf52840dk"], sha)),
                                     encoding="utf-8")
    return d.parent


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREMIND_TAG_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CREMIND_TAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fw, "_detect", lambda **_: [])
    monkeypatch.setattr(fw, "_RUNNER", None)


class Recorder:
    """A runner that answers per command substring and records every argv."""

    def __init__(self, answers: dict[str, CompletedResult] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
        self.calls.append(argv)
        joined = " ".join(argv)
        for key, result in self.answers.items():
            if key in joined:
                return result
        return CompletedResult(0, "", "")


def use_tool(monkeypatch: pytest.MonkeyPatch, name: str, runner: Recorder) -> None:
    exe = {"nrfutil": "/opt/nrfutil", "nrfjprog": "/opt/nrfjprog", "jlink": "/opt/SEGGER/JLinkExe"}[name]
    monkeypatch.setattr(fw, "_detect", lambda **_: [ToolInfo(name, exe)])
    monkeypatch.setattr(fw, "_RUNNER", runner)


def cli(*args: str, code: int = 0, input: str | None = None) -> Any:
    result = CliRunner().invoke(app, ["firmware", *args], input=input, catch_exceptions=False)
    assert result.exit_code == code, result.output
    return result


# -- tables --------------------------------------------------------------------------------------------


def test_soc_table_matches_the_build_matrix_and_the_release_tool() -> None:
    socs = yaml.safe_load((REPO / "tools" / "targets.yaml").read_text(encoding="utf-8"))["socs"]
    assert set(fw.SOC_DEVICES) == set(socs)
    for key, (_, family, flash) in fw.SOC_DEVICES.items():
        assert flash == socs[key]["flash"] and family == socs[key]["series"].upper()
    sys.path.insert(0, str(REPO / "tools"))
    try:
        spec = importlib.util.spec_from_file_location("_ctag_release_tool", REPO / "tools" / "release.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve the module by name
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(REPO / "tools"))
        sys.modules.pop("_ctag_release_tool", None)
    assert {k: v[:2] for k, v in fw.SOC_DEVICES.items()} == module.SOC_DEVICES


# -- list / verify ---------------------------------------------------------------------------------------


def test_list_release_and_build(release: Path, build_dir: Path) -> None:
    rows = json.loads(cli("list", str(release.parent), "--json").stdout)
    assert [r["target"] for r in rows] == list(TARGETS)
    assert rows[0]["release_status"] == "buildable" and rows[0]["verify_stack"] is True
    rows = json.loads(cli("list", str(build_dir), "--json").stdout)
    assert [r["target"] for r in rows] == ["gateway-nrf52840dk"] and rows[0]["source"] == "metadata.json"
    assert "gateway-nrf52840dk" in cli("list", str(release / "release.json")).stdout
    cli("list", str(release / "firmware"), code=1)


def test_verify_release_image(release: Path) -> None:
    hex_path = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    doc = json.loads(cli("verify", "--hex", str(hex_path), "--target", "gateway-nrf52840dk", "--json").stdout)
    assert doc["ok"] is True
    checks = {c["id"]: c for c in doc["checks"]}
    assert checks["sha256"]["ok"] and checks["sha256sums"]["ok"] and checks["board"]["ok"]
    assert checks["range"]["ok"] and checks["verify_stack"]["ok"]
    assert checks["qualified"]["level"] == "warning" and not checks["qualified"]["ok"]

    out = cli("verify", "--hex", str(hex_path), "--target", "gateway-nrf52dk", code=1).stdout
    assert "image is gateway-nrf52840dk, not gateway-nrf52dk" in out
    hex_path.write_bytes(to_hex(bytes(range(65))).encode())  # tampered
    doc = json.loads(cli("verify", "--hex", str(hex_path), "--json", code=1).stdout)
    failed = {c["id"] for c in doc["checks"] if not c["ok"] and c["level"] == "error"}
    assert failed == {"sha256", "sha256sums"}


def test_verify_refuses_unknown_images_and_bad_builds(tmp_path: Path, build_dir: Path) -> None:
    stray = tmp_path / "stray.hex"
    stray.write_bytes(to_hex(b"\x00").encode())
    assert "no release.json entry or metadata.json" in cli("verify", "--hex", str(stray), code=1).stdout
    d = build_dir / "gateway-nrf52840dk"
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    meta["verify_stack"] = {"ok": False}
    meta["status"] = "verify-failed"
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    out = cli("verify", "--hex", str(d / "zephyr.hex"), code=1).stdout
    assert "verify_stack" in out and "FAILED" in out and "build status verify-failed" in out


def test_hex_range_must_fit_the_soc(build_dir: Path) -> None:
    d = build_dir / "gateway-nrf52840dk"
    (d / "zephyr.hex").write_bytes(to_hex(b"\x00" * 16, base=0x100000).encode())  # past 1 MiB
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    meta["artifacts"]["zephyr.hex"]["sha256"] = hashlib.sha256((d / "zephyr.hex").read_bytes()).hexdigest()
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    assert "exceeds the 1024 KiB of nrf52840_qiaa" in cli("verify", "--hex", str(d / "zephyr.hex"), code=1).stdout
    info = fw.hex_info(to_hex(b"\x01" * 4).replace(":00000001FF\n", "") + to_hex(b"\x02" * 4, base=0x10001080))
    assert info.uicr and (info.start, info.end) == (0, 4)


# -- flash -------------------------------------------------------------------------------------------------


def test_flash_dry_run_prints_the_exact_commands(release: Path, tmp_path: Path) -> None:
    hex_path = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    out = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), "--tool", "nrfjprog",
              "--snr", "683012345", "--dry-run").stdout
    assert f"nrfjprog -f NRF52 --program {hex_path} --sectoranduicrerase --verify --snr 683012345" in out
    assert "nrfjprog -f NRF52 --reset --snr 683012345" in out

    out = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), "--tool", "nrfutil",
              "--dry-run").stdout
    assert "chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE,verify=VERIFY_READ" in out

    scripts = tmp_path / "scripts"
    out = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), "--tool", "jlink",
              "--dry-run", "--erase", "all", "--script-dir", str(scripts)).stdout
    assert "-device nRF52840_xxAA" in out and "erase" in out and "loadfile" in out
    written = sorted(p.name for p in scripts.iterdir())
    assert len(written) == 2 and all(n.endswith(".jlink") for n in written)
    assert "loadfile" in (scripts / next(n for n in written if "program" in n)).read_text(encoding="ascii")


def test_flash_runs_program_then_reset(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hex_path = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    runner = Recorder()
    use_tool(monkeypatch, "nrfjprog", runner)
    out = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path)).stdout
    assert [c[3] for c in runner.calls] == ["--program", "--reset"]
    assert runner.calls[0][:3] == ["/opt/nrfjprog", "-f", "NRF52"]
    assert "gateway-nrf52840dk 0.1.0 flashed" in out

    runner = Recorder({"--program": CompletedResult(33, "", "ERROR: no debugger")})
    use_tool(monkeypatch, "nrfjprog", runner)
    err = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), code=1)
    assert "program" in err.output and len(runner.calls) == 1  # no reset after a failed program

    runner = Recorder()
    use_tool(monkeypatch, "nrfjprog", runner)
    cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), "--erase", "all", code=1, input="n\n")
    assert runner.calls == []
    cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(hex_path), "--erase", "all", "--yes")
    assert "--chiperase" in runner.calls[0]


def test_flash_refuses_tags_mismatches_and_missing_tools(release: Path) -> None:
    tag_hex = release / "firmware" / "tag-laowu-bw" / f"tag-laowu-bw-{VERSION}.hex"
    out = cli("flash", "--target", "tag-laowu-bw", "--hex", str(tag_hex), "--dry-run", code=1).output
    assert "cremind-tag tag enroll --board laowu_bw --firmware" in out
    gw_hex = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    out = cli("flash", "--target", "tag-laowu-bw", "--hex", str(gw_hex), "--dry-run", code=1).output
    assert "nothing was flashed" in out
    out = cli("flash", "--target", "gateway-nrf52840dk", "--hex", str(gw_hex), code=1).output
    assert "no SWD tool found" in out


# -- info ---------------------------------------------------------------------------------------------------


def nrfjprog_dump(data: bytes, address: int) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        words = " ".join(f"{int.from_bytes(chunk[i:i + 4], 'little'):08X}" for i in range(0, len(chunk), 4))
        lines.append(f"0x{address + off:08X}: {words}")
    return "\n".join(lines) + "\n"


def _words(*values: int) -> bytes:
    return b"".join(v.to_bytes(4, "little") for v in values)


def test_info_reads_ficr_and_uicr(release: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ficr = bytearray(b"\xff" * 0x58)
    ficr[0:8] = _words(4096, 256)  # CODEPAGESIZE, CODESIZE
    ficr[0x50:0x58] = _words(0x89ABCDEF, 0x01234567)  # DEVICEID[0..1] at 0x10000060
    info_block = _words(0x52840, 0x41414430, 0x2004, 256, 1024)
    uicr = _words(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFF5A, 0xFFFFFFFF)
    runner = Recorder({
        "0x10000010": CompletedResult(0, nrfjprog_dump(bytes(ficr), 0x10000010), ""),
        "0x10000100": CompletedResult(0, nrfjprog_dump(info_block, 0x10000100), ""),
        "0x10001200": CompletedResult(0, nrfjprog_dump(uicr, 0x10001200), ""),
    })
    use_tool(monkeypatch, "nrfjprog", runner)
    hex_path = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    doc = json.loads(cli("info", "--hex", str(hex_path), "--json").stdout)
    assert (doc["part"], doc["variant"], doc["package"], doc["ram_kib"], doc["flash_kib"]) == (
        "nRF52840", "AAD0", "QI", 256, 1024)
    assert doc["device_id"] == "0123456789ABCDEF" and doc["flash_bytes"] == 1048576
    assert doc["approtect"] == "HwDisabled (open)" and doc["pselreset0"] == "not set"
    assert doc["image"] == {"hex": str(hex_path), "matches": True, "target": "gateway-nrf52840dk",
                            "version": VERSION}
    assert ["--verify", str(hex_path)] == runner.calls[-1][3:5]
    assert not any("0x10001080" in " ".join(c) for c in runner.calls)  # UICR.CUSTOMER is never read

    runner = Recorder({"--verify": CompletedResult(55, "", "ERROR: verify failed")})
    use_tool(monkeypatch, "nrfjprog", runner)
    doc = json.loads(cli("info", "--hex", str(hex_path), "--json", code=1).stdout)
    assert doc["image"]["matches"] is False


def test_info_dry_run_per_tool(tmp_path: Path, release: Path) -> None:
    out = cli("info", "--soc", "nrf51822_qfab", "--tool", "nrfjprog", "--dry-run").stdout
    assert "nrfjprog -f NRF51 --memrd 0x10000010 --n 88 --w 32" in out
    assert "--memrd 0x10001000 --n 20" in out
    hex_path = release / "firmware" / "gateway-nrf52840dk" / f"gateway-nrf52840dk-{VERSION}.hex"
    out = cli("info", "--hex", str(hex_path), "--tool", "nrfutil", "--dry-run").stdout
    assert f"nrfutil device fw-verify --firmware {hex_path}" in out
    scripts = tmp_path / "s"
    out = cli("info", "--hex", str(hex_path), "--tool", "jlink", "--dry-run", "--script-dir", str(scripts)).stdout
    assert "verifybin" in out and (scripts / f"{hex_path.stem}.verify.bin").read_bytes() == bytes(range(64))
    cli("info", "--dry-run", code=1)


def test_decode_nrf51_identity() -> None:
    mem: dict[int, int] = {}

    def put(addr: int, *values: int) -> None:
        for i, b in enumerate(_words(*values)):
            mem[addr + i] = b

    put(0x10000010, 1024, 128)
    put(0x10000034, 4, 4096)
    put(0x1000005C, 0x0000008F, 0x11223344, 0x55667788)
    put(0x10001004, 0xFFFF00FF)
    fields = fw.decode_identity("NRF51", mem)
    assert fields["flash_bytes"] == 131072 and fields["ram_bytes"] == 16384 and fields["hwid"] == "0x008F"
    assert fields["device_id"] == "5566778811223344"
    assert fields["rbpconf"] == "PALL ENABLED, PR0 off"


def test_firmware_help_lists_every_command() -> None:
    out = cli("--help").stdout
    for command in ("list", "verify", "flash", "info"):
        assert command in out
    assert sys.modules["cremind_tag.cli.firmware"] is fw
