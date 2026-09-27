"""tools/build.py pinning, workspace check, reproducibility flags and metadata.json (no Docker needed)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from ctag_tools_helpers import REPO_ROOT, small_elf

import build
import verify_stack as vs

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_pins_are_consistent():
    assert build.pin_problems() == []
    assert build.IMAGE == f"{build.IMAGE_NAME}@{build.IMAGE_DIGEST}"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", build.IMAGE_DIGEST)
    assert build.west_yml_revision() == build.NCS_REVISION


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_workflows_run_the_pinned_image(workflow):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    containers = [job["container"] for job in doc["jobs"].values() if "container" in job]
    assert containers, f"{workflow} has no container job"
    for container in containers:
        image = container if isinstance(container, str) else container["image"]
        assert image == build.IMAGE, f"{workflow}: {image}"
    # No job may fall back to the mutable tag.
    assert not re.search(r"sdk-nrf-toolchain:v\d", text)
    assert f"CTAG_TOOLCHAIN_IMAGE: {build.IMAGE}" in text


def test_release_flags_follow_the_hardware_matrix():
    targets, _ = build.load_matrix()
    hardware = build.load_hardware_status()
    marked = {name for name, t in targets.items() if t.release}
    assert marked == {"gateway-nrf52840dk", "gateway-nrf52dk", "bridge-nrf52840dk", "bridge-nrf52dk",
                      "tag-laowu-bw", "tag-laowu-bwr", "tag-nrf52dk"}
    for name in marked:  # never mark a blocked or merely documented board
        hw = targets[name].hardware
        assert hw is None or hardware[hw]["status"] not in ("blocked", "documented"), name


def test_repro_cmake_args_and_west_command():
    args = build.repro_cmake_args(Path("/src/x"), Path("/ncs"), Path("/b/deep/t"), "tag-laowu-bw", "33fa6a7aac6a4401d16a")
    cpp = next(a for a in args if a.startswith("-DEXTRA_CPPFLAGS="))
    maps = cpp.removeprefix("-DEXTRA_CPPFLAGS=").split()
    assert maps == ["-ffile-prefix-map=/ncs=/ncs", "-ffile-prefix-map=/src/x=/cremind-tag",
                    "-ffile-prefix-map=/b/deep/t=/build/tag-laowu-bw"]  # the build directory wins: last
    assert f"-DEXTRA_LDFLAGS={cpp.removeprefix('-DEXTRA_CPPFLAGS=')}" in args  # LTO code generation too
    assert "-DBUILD_VERSION=33fa6a7aac6a" in args
    assert not any(a.startswith("-DBUILD_VERSION") for a in build.repro_cmake_args(
        Path("/w"), Path("/n"), Path("/b"), "t", None))
    t = build.load_matrix()[0]["tag-laowu-bw"]
    cmd = build.west_command(t, Path("/work/apps/tag"), Path("/build/t"), Path("/work"), False, args)
    assert cmd[-len(args):] == args and cmd.index("-UCONFIG_*") < cmd.index(args[0])


def test_git_safe_env_appends_one_entry():
    env = build.git_safe_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"})
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("safe.directory", "*")
    assert build.git_safe_env({})["GIT_CONFIG_KEY_0"] == "safe.directory"


class _Git:
    def __init__(self, heads: dict[str, str | None]):
        self.heads = heads

    def __call__(self, repo: Path, *args: str) -> str | None:
        return self.heads.get(repo.name)


def _runner(rc: int, out: str = ""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, rc, out, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_check_workspace(monkeypatch):
    ncs = Path("/ncs")
    monkeypatch.setattr(build, "_git", _Git({"nrf": build.NCS_SDK_NRF_COMMIT, "zephyr": "33fa6a7aac6a" + "0" * 28}))
    run = _runner(0)
    info = build.check_workspace(ncs, runner=run)
    assert info["sdk_nrf_commit"] == build.NCS_SDK_NRF_COMMIT and info["compared"] and info["clean"]
    assert run.calls[0][0] == ["west", "compare", "--exit-code", "--ignore-branches"]
    assert run.calls[0][1]["cwd"] == ncs
    assert build.check_workspace(ncs, compare=False, runner=_runner(1))["compared"] is False

    with pytest.raises(build.WorkspaceError, match=r"(?s)west compare.*zephyr: HEAD differs.*west update"):
        build.check_workspace(ncs, runner=_runner(1, "=== zephyr (zephyr):\n--- zephyr: HEAD differs\n"))
    monkeypatch.setattr(build, "_git", _Git({"nrf": "0" * 40}))
    with pytest.raises(build.WorkspaceError, match=r"has sdk-nrf at 000000000000, but west.yml pins sdk-nrf v3\.4\.1"):
        build.check_workspace(ncs, runner=run)
    monkeypatch.setattr(build, "_git", _Git({}))
    with pytest.raises(build.WorkspaceError, match="not a git checkout"):
        build.check_workspace(ncs, runner=run)


def test_input_digests_ignore_path_comments():
    a = "#\n# cremind-tag (/work)\n#\nCONFIG_A=y\n# CONFIG_B is not set\n# end of cremind-tag (/work)\n"
    b = a.replace("/work", "/src/other")
    assert build.kconfig_digest(a) == build.kconfig_digest(b)
    assert build.kconfig_digest(a) != build.kconfig_digest(a.replace("CONFIG_A=y", "CONFIG_A=n"))
    dts = 'model = "x"; /* in ../work/boards/b.dts:16 */\nfoo;  /* in /work/x.dtsi:2 */\n'
    assert build.dts_digest(dts) == build.dts_digest(dts.replace("work", "src/alt"))
    assert build.dts_digest(dts) != build.dts_digest(dts.replace('"x"', '"y"'))
    hdr = "/*\n * DTS input file: /build/a/zephyr.dts.pre\n */\n#define DT_N_PATH \"/\" /* c */\n"
    assert build.dt_header_digest(hdr) == build.dt_header_digest(hdr.replace("/build/a", "/b/c"))


def test_metadata_json(tmp_path, monkeypatch, artifacts):
    art = artifacts("zephyr_ctlr")
    (art / "zephyr.elf").write_bytes(small_elf())
    (art / "zephyr.hex").write_bytes(b":00000001FF\n")
    matrix = vs.load_targets()
    targets, _ = build.load_matrix()
    t = targets["tag-laowu-bw"]
    report = vs.verify(art, vs.soc_geometry(matrix, t.soc), t.role, t.name)
    entry = {"built_at": "2026-09-28T00:00:00+00:00", "memory": report.memory, "status": "ok",
             "resources": build.evaluate_resources(t, report.memory)}
    monkeypatch.setenv("CTAG_TOOLCHAIN_IMAGE", build.IMAGE)
    ctx = build.BuildContext(
        version="0.1.0", version_error=None,
        git={"commit": "a" * 40, "dirty": False, "changed_files": [], "commit_time": 1790536279},
        ncs={"sdk_nrf_commit": build.NCS_SDK_NRF_COMMIT, "compared": True, "clean": True},
        source_date_epoch=1790536279, hardware=build.load_hardware_status(), pristine=True)
    meta = build.write_metadata(art, t, "apps/tag", entry, ctx, ["west", "build"], tmp_path / "nobuild", report)
    on_disk = json.loads((art / "metadata.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(meta))
    assert meta["schema"] == build.METADATA_SCHEMA
    assert (meta["target"], meta["board"], meta["soc"], meta["role"]) == ("tag-laowu-bw", "laowu_bw/nrf51822",
                                                                          "nrf51822_qfab", "tag")
    assert meta["hardware_status"] == "buildable" and meta["board_id"] == 16 and meta["release"] is True
    assert meta["version"] == "0.1.0" and meta["version_matches"] == (meta["app_version"] == "0.1.0")
    assert meta["toolchain"]["image"] == build.IMAGE and meta["toolchain"]["matches_pin"] is True
    assert meta["toolchain"]["digest"] == build.IMAGE_DIGEST
    assert meta["build"]["source_date_epoch"] == 1790536279 and meta["build"]["pristine"]
    assert meta["verify_stack"]["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", meta["inputs"]["kconfig_sha256"])
    assert meta["inputs"]["devicetree_header_sha256"]
    assert set(meta["artifacts"]) >= {"zephyr.hex", "zephyr.elf", "zephyr.map", ".config"}
    assert meta["artifacts"]["zephyr.hex"]["size"] == len(":00000001FF\n")


def test_container_script_and_docker_forward_the_new_options():
    args = argparse.Namespace(pristine=False, verbose=False, allow_resource_miss=False, app=None,
                              out_root="build/repro-out", skip_workspace_check=True)
    script = build.container_script(args, ["tag-laowu-bw"])
    assert "--out-root build/repro-out --skip-workspace-check" in script
    base = build._docker_base(False)
    assert base[base.index(build.IMAGE) - 1] == f"CTAG_TOOLCHAIN_IMAGE={build.IMAGE}"


def test_out_root_redirects_reports(tmp_path, monkeypatch):
    for name in ("OUT_ROOT", "REPORT_JSON", "REPORT_MD"):
        monkeypatch.setattr(build, name, getattr(build, name))
    build._set_out_root(tmp_path / "deep" / "out")
    build.write_reports({"tag-laowu-bw": {"app": "apps/tag", "board": "b", "status": "ok"}})
    assert (tmp_path / "deep" / "out" / "memory-report.json").is_file()
    assert build._display(REPO_ROOT / "build" / "x") == "build/x"


def test_cli_options(monkeypatch):
    seen = SimpleNamespace(args=None)
    monkeypatch.setattr(build, "run_docker", lambda script, interactive=False: setattr(seen, "args", script) or 0)
    assert build.main(["--out-root", str(REPO_ROOT / "build" / "o"), "--skip-workspace-check", "tag-laowu-bw"]) == 0
    assert "--out-root build/o --skip-workspace-check" in seen.args
    with pytest.raises(SystemExit):
        build.main(["--out-root", str(Path(REPO_ROOT.anchor) / "elsewhere-outside"), "tag-laowu-bw"])
