"""tools/build.py: target matrix rules, west command lines and reports (no Docker needed)."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from ctag_tools_helpers import REPO_ROOT

import build

TARGETS, SOCS = build.load_matrix()
HARDWARE = yaml.safe_load((REPO_ROOT / "hardware" / "matrix.yaml").read_text(encoding="utf-8"))
EXPECTED_TARGETS = {
    "gateway-nrf52840dk",
    "gateway-nrf52dk",
    "bridge-nrf52840dk",
    "bridge-nrf52dk",
    "tag-laowu-bw",
    "tag-laowu-bwr",
    "tag-sifei-52810",
    "tag-hema-52811",
    "tag-nrf52dk",
}


def test_matrix_has_every_target():
    assert set(TARGETS) == EXPECTED_TARGETS


@pytest.mark.parametrize("name", sorted(EXPECTED_TARGETS))
def test_target_obeys_static_rules(name):
    assert build.validate_target(TARGETS[name], SOCS) == []


def test_snippet_rules_per_series():
    for t in TARGETS.values():
        series = SOCS[t.soc].series
        assert (build.LL_SNIPPET in t.snippets) == (series == "nrf52"), t.name


def test_validate_rejects_snippet_on_nrf51_and_missing_snippet_on_nrf52():
    laowu = TARGETS["tag-laowu-bw"]
    assert build.validate_target(replace(laowu, snippets=("bt-ll-sw-split",)), SOCS)
    sifei = TARGETS["tag-sifei-52810"]
    assert build.validate_target(replace(sifei, snippets=()), SOCS)
    assert build.validate_target(replace(sifei, soc="nrf99"), SOCS)
    assert build.validate_target(replace(sifei, role="relay"), SOCS)


def test_resource_targets_follow_the_plan():
    ram = {name: t.ram_free_min for name, t in TARGETS.items()}
    assert ram["tag-laowu-bw"] == ram["tag-laowu-bwr"] == 2048
    assert ram["tag-sifei-52810"] == ram["tag-hema-52811"] == 3072
    assert ram["bridge-nrf52dk"] == 8192
    assert all(t.flash_headroom_pct == 15 for t in TARGETS.values())


def test_hardware_ids_and_boards_match_the_hardware_matrix():
    entries = {e["id"]: e for group in ("gateways", "bridges", "tags") for e in HARDWARE[group]}
    for t in TARGETS.values():
        if t.hardware is None:
            continue
        entry = entries[t.hardware]
        assert entry["zephyr_board"] == t.board, t.name
        soc = SOCS[t.soc]
        assert entry["flash_kib"] * 1024 == soc.flash, t.name
        assert entry["ram_kib"] * 1024 == soc.ram, t.name


def test_west_command_nrf51_has_no_snippet():
    t = TARGETS["tag-laowu-bw"]
    cmd = build.west_command(t, Path("/work/apps/tag"), Path("/build/tag-laowu-bw"), Path("/work"), pristine=False)
    assert cmd[:5] == ["west", "build", "--no-sysbuild", "-p", "auto"]
    assert "-S" not in cmd
    assert cmd[cmd.index("-b") + 1] == "laowu_bw/nrf51822"
    assert cmd[cmd.index("-d") + 1] == "/build/tag-laowu-bw"
    # Stale nrf_security CONFIG_* cache entries break a non-sysbuild re-configure.
    assert cmd[-3:] == ["--", "-DZEPHYR_EXTRA_MODULES=/work", "-UCONFIG_*"]


def test_west_command_nrf52_extra_files():
    t = replace(TARGETS["tag-sifei-52810"], extra_conf=("a.conf", "b.conf"), extra_overlay=("x.overlay",))
    cmd = build.west_command(t, Path("/work/apps/tag"), Path("/build/t"), Path("/work"), pristine=True)
    assert cmd[cmd.index("-p") + 1] == "always"
    assert cmd[cmd.index("-S") + 1] == "bt-ll-sw-split"
    assert "-DEXTRA_CONF_FILE=/work/apps/tag/a.conf;/work/apps/tag/b.conf" in cmd
    assert "-DEXTRA_DTC_OVERLAY_FILE=/work/apps/tag/x.overlay" in cmd
    assert cmd.index("/work/apps/tag") < cmd.index("--")


def test_evaluate_resources():
    t = TARGETS["tag-laowu-bw"]
    memory = {"flash_region": 126976, "flash_free": 22660, "ram_free": 2068}
    res = build.evaluate_resources(t, memory)
    assert res["ok"] and res["flash_ok"] and res["ram_ok"]
    assert res["flash_headroom_pct"] == pytest.approx(17.85, abs=0.01)
    assert not build.evaluate_resources(t, memory | {"ram_free": 2047})["ok"]
    assert not build.evaluate_resources(t, memory | {"flash_free": 19000})["flash_ok"]
    assert build.evaluate_resources(TARGETS["gateway-nrf52dk"], memory | {"ram_free": 1})["ram_ok"]


def test_container_script():
    args = argparse.Namespace(pristine=True, verbose=False, allow_resource_miss=False, app="build/boardcheck")
    script = build.container_script(args, ["tag-laowu-bw"])
    assert script.startswith("test -d /ncs/.west ||")
    assert "python3 /work/tools/build.py --in-container --pristine --app build/boardcheck" in script
    assert script.endswith("tag-laowu-bw")
    assert "west update --narrow" in build.setup_script()


def test_reports_merge_and_render(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(build, "REPORT_JSON", tmp_path / "memory-report.json")
    monkeypatch.setattr(build, "REPORT_MD", tmp_path / "memory-report.md")
    entry = {
        "app": "apps/tag",
        "board": "laowu_bw/nrf51822",
        "status": "ok",
        "verify": {"ok": True, "failed": [], "warnings": [], "dt_method": "edtlib"},
        "memory": {"flash_region": 126976, "flash_used": 104316, "ram_region": 16384, "ram_used": 14316},
        "resources": {
            "flash_headroom_pct": 17.85,
            "flash_headroom_pct_min": 15,
            "ram_free": 2068,
            "ram_free_min": 2048,
        },
    }
    build.write_reports({"tag-laowu-bw": entry})
    build.write_reports({"tag-hema-52811": entry | {"status": "resource-miss"}})
    doc = json.loads((tmp_path / "memory-report.json").read_text(encoding="utf-8"))
    assert list(doc["targets"]) == ["tag-hema-52811", "tag-laowu-bw"]
    md = (tmp_path / "memory-report.md").read_text(encoding="utf-8")
    assert "| tag-laowu-bw | `apps/tag` | `laowu_bw/nrf51822` | 104,316 / 126,976 | 17.9 % (15 %) |" in md
    assert "2,068 (2,048)" in md
    assert "resource-miss" in md


def test_cli_list_and_usage(capsys):
    assert build.main(["--list"]) == 0
    assert "tag-laowu-bw" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        build.main(["no-such-target"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        build.main([])
