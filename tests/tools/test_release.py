"""tools/release.py: which targets ship, metadata checks, packaging, checksums, notices, archive."""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from ctag_tools_helpers import REPO_ROOT

import build
import release

TARGETS, SOCS = build.load_matrix()
COMMIT = "c" * 40


def test_soc_devices_cover_the_matrix():
    assert set(release.SOC_DEVICES) == set(SOCS)
    for soc, (device, family) in release.SOC_DEVICES.items():
        assert family == SOCS[soc].series.upper()
        assert device.lower().startswith(soc[:8])


def test_eligibility_rules():
    t = TARGETS["tag-laowu-bw"]
    hw = {"laowu_bw": {"status": "buildable", "board_id": 16}}

    def run(target, status):
        return release.eligible_targets({"x": target}, {"laowu_bw": hw["laowu_bw"] | {"status": status}})

    assert run(replace(t, release=False), "qualified")[0][0][1].release_status == "qualified"
    assert run(replace(t, release=True), "buildable")[0][0][1].release_status == "buildable"
    assert "not marked release: true" in run(replace(t, release=False), "buildable")[1][0]["reason"]
    for status in ("blocked", "documented"):
        assert run(replace(t, release=True), status)[1][0]["reason"] == f"hardware status {status}"
    dev = replace(t, hardware=None, release=True)
    assert release.eligible_targets({"x": dev}, {})[0][0][1].release_status == "development"
    assert "development target" in release.eligible_targets({"x": replace(dev, release=False)}, {})[1][0]["reason"]
    assert "not in hardware/matrix.yaml" in release.eligible_targets({"x": t}, {})[1][0]["reason"]


def test_repository_release_targets():
    ok, excluded = release.eligible_targets()
    names = [n for n, _ in ok]
    assert names == [n for n in TARGETS if n in names]  # matrix order
    assert {"gateway-nrf52840dk", "bridge-nrf52840dk", "tag-laowu-bw", "tag-laowu-bwr"} <= set(names)
    assert {e["target"] for e in excluded} >= {"tag-sifei-52810", "tag-hema-52811"}
    assert dict(ok)["tag-nrf52dk"].release_status == "development"


def _meta(name: str = "tag-laowu-bw", **over):
    t = TARGETS[name]
    meta = {
        "schema": build.METADATA_SCHEMA, "target": name, "app": t.app, "board": t.board, "soc": t.soc,
        "role": t.role, "status": "ok", "version": "0.1.0", "app_version": "0.1.0", "version_matches": True,
        "verify_stack": {"ok": True, "dt_method": "edtlib", "counts": {"pass": 15}, "failed": [], "warnings": []},
        "git": {"commit": COMMIT, "dirty": False, "changed_files": []},
        "ncs": {"sdk_nrf_commit": build.NCS_SDK_NRF_COMMIT, "compared": True},
        "toolchain": {"image": build.IMAGE}, "build": {"pristine": True},
        "memory": {"flash_region": 126976, "flash_used": 95000, "ram_region": 16384, "ram_used": 14316},
        "resources": {"flash_headroom_pct": 25.2, "flash_headroom_pct_min": 15, "ram_free": 2068,
                      "ram_free_min": 2048},
        "inputs": {"kconfig_sha256": "k" * 64, "note": "x"},
    }
    meta.update(over)
    return meta


def test_metadata_checks():
    git = {"commit": COMMIT}
    assert release.check_firmware_metadata(_meta(), "tag-laowu-bw", "0.1.0", git, False) == []
    bad = {
        "status": "resource-miss", "version": "0.0.9", "version_matches": False,
        "verify_stack": {"ok": False, "failed": ["map.no_sdc_libs"]},
        "git": {"commit": "d" * 40, "dirty": True, "changed_files": ["apps/tag/src/main.c"]},
        "ncs": {"sdk_nrf_commit": "e" * 40, "compared": False},
        "toolchain": {"image": "ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1"}, "build": {"pristine": False},
    }
    problems = " | ".join(release.check_firmware_metadata(_meta(**bad), "tag-laowu-bw", "0.1.0", git, False))
    for needle in ("build status 'resource-miss'", "verify_stack did not pass: map.no_sdc_libs",
                   "built as VERSION 0.0.9", "built from commit dddddddddddd", "dirty tree (apps/tag/src/main.c",
                   "sdk-nrf eeeeeeeeeeee", "not compared", "toolchain 'ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1'",
                   "not a pristine build"):
        assert needle in problems, needle
    dirty_ok = release.check_firmware_metadata(_meta(git={"commit": COMMIT, "dirty": True}), "tag-laowu-bw",
                                               "0.1.0", git, True)
    assert dirty_ok == []


def _fake_build(root: Path, name: str, **over) -> Path:
    d = root / name
    d.mkdir(parents=True)
    files = {"zephyr.hex": ":00000001FF\n", "zephyr.bin": "\x00", "zephyr.elf": "ELF", "verify.json": "{}",
             "zephyr.map": "libzephyr.a modules/hal_nordic liboberon_3.0.20.a", ".config": "CONFIG_BT=y\n"}
    for fname, text in files.items():
        (d / fname).write_text(text, encoding="utf-8", newline="\n")
    artifacts = {f: {"sha256": hashlib.sha256((d / f).read_bytes()).hexdigest()} for f in files}
    (d / "metadata.json").write_text(json.dumps(_meta(name, artifacts=artifacts, **over)), encoding="utf-8")
    return d


def test_package_firmware(tmp_path: Path):
    src, dist = tmp_path / "build", tmp_path / "dist" / "0.1.0"
    _fake_build(src, "tag-laowu-bw")
    elig = dict(release.eligible_targets()[0])
    entry = release.package_firmware("tag-laowu-bw", elig["tag-laowu-bw"], src, dist, "0.1.0",
                                     {"commit": COMMIT}, False)
    assert entry["files"]["hex"] == "firmware/tag-laowu-bw/tag-laowu-bw-0.1.0.hex"
    assert (dist / entry["files"]["metadata"]).name == "tag-laowu-bw-0.1.0.metadata.json"
    assert (dist / entry["files"]["memory"]).is_file() and entry["files"]["config"].endswith(".config")
    assert entry["sha256"]["hex"] == hashlib.sha256(b":00000001FF\n").hexdigest()
    assert (entry["jlink_device"], entry["family"]) == ("nRF51822_xxAB", "NRF51")
    assert entry["flash"] == ("cremind-tag tag enroll --board laowu_bw --firmware "
                              "firmware/tag-laowu-bw/tag-laowu-bw-0.1.0.hex")
    assert entry["release_status"] == "buildable" and entry["qualified"] is False
    gw = _fake_build(src, "gateway-nrf52840dk")
    gw_entry = release.package_firmware("gateway-nrf52840dk", elig["gateway-nrf52840dk"], src, dist, "0.1.0",
                                        {"commit": COMMIT}, False)
    assert gw_entry["flash"].startswith("cremind-tag firmware flash --target gateway-nrf52840dk --hex ")

    (gw / "zephyr.hex").write_text(":00000001FF\n:00000001FF\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="zephyr.hex changed after the build"):
        release.package_firmware("gateway-nrf52840dk", elig["gateway-nrf52840dk"], src, dist, "0.1.0",
                                 {"commit": COMMIT}, False)
    with pytest.raises(release.ReleaseError, match="metadata.json is missing"):
        release.package_firmware("tag-laowu-bwr", elig["tag-laowu-bwr"], src, dist, "0.1.0", {"commit": COMMIT},
                                 False)
    comps = release.firmware_components(src, ["tag-laowu-bw"])
    assert [c[0].split(" ")[0] for c in comps] == ["Zephyr", "nrfx", "nrf_oberon", "CMSIS"]


def test_checksums_and_archive(tmp_path: Path):
    dist = tmp_path / "out" / "1.2.3"
    (dist / "a" / "b").mkdir(parents=True)
    (dist / "a" / "b" / "x.bin").write_bytes(b"\x01\x02")
    (dist / "release.json").write_bytes(b"{}\n")
    sums = release.write_sha256sums(dist)
    lines = sums.read_text(encoding="utf-8").splitlines()
    bin_digest = hashlib.sha256(bytes([1, 2])).hexdigest()
    json_digest = hashlib.sha256("{}\n".encode()).hexdigest()
    assert lines == [f"{bin_digest}  a/b/x.bin", f"{json_digest}  release.json"]
    assert release.verify_sha256sums(dist) == []
    (dist / "a" / "b" / "x.bin").write_bytes(b"\x00")
    assert release.verify_sha256sums(dist) == ["a/b/x.bin"]

    first = release.write_archive(dist, "cremind-tag-1.2.3", 1790536279).read_bytes()
    (dist / "a" / "b" / "x.bin").touch()
    second = release.write_archive(dist, "cremind-tag-1.2.3", 1790536279).read_bytes()
    assert first == second  # mtimes, owners and gzip header do not leak in
    with tarfile.open(tmp_path / "out" / "cremind-tag-1.2.3.tar.gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members][:2] == ["cremind-tag-1.2.3/SHA256SUMS", "cremind-tag-1.2.3/a"]
    assert all(m.uid == 0 and m.mtime == 1790536279 for m in members)
    assert (tmp_path / "out" / "cremind-tag-1.2.3.tar.gz.sha256").read_text(encoding="utf-8").endswith(
        "  cremind-tag-1.2.3.tar.gz\n")


def test_notices(tmp_path: Path):
    deps = [{"name": "cbor2", "version": "5.6.5", "licence": "MIT", "marker": None},
            {"name": "pywin32-ctypes", "version": "0.2.3", "licence": "BSD-3-Clause",
             "marker": "sys_platform == 'win32'"}]
    fonts = {"manifest_id": "f2c67d9125acaf06", "packs": {"full": {}, "dev": {}}}
    comps = [("Zephyr RTOS (sdk-zephyr)", "Apache-2.0", ["tag-laowu-bw"])]
    text = release.write_notices(tmp_path, [{"target": "tag-laowu-bw"}], comps, fonts, deps).read_text(
        encoding="utf-8")
    assert "Nayuki QR Code generator v1.8.0 — MIT" in text and "Copyright © 2022 Project Nayuki" in text
    assert "SIL Open Font License 1.1" in text and "fonts/full/, fonts/dev/" in text
    assert "SIL OPEN FONT LICENSE Version 1.1" in text
    assert "Material Icons — Apache License 2.0" in text and "Apache License" in text
    assert "Zephyr RTOS (sdk-zephyr): Apache-2.0 (all targets)" in text
    assert "pywin32-ctypes" in text and "[sys_platform == 'win32']" in text
    bare = release.write_notices(tmp_path, [], [], None, None).read_text(encoding="utf-8")
    assert "Open Font License" not in bare and "Companion runtime" not in bare


def test_release_notes():
    manifest = {
        "version": "0.1.0", "git": {"commit": COMMIT},
        "ncs": {"revision": "v3.4.1", "sdk_nrf_commit": build.NCS_SDK_NRF_COMMIT},
        "toolchain": {"digest": build.IMAGE_DIGEST},
        "firmware": [{"target": "tag-laowu-bw", "board": "laowu_bw/nrf51822", "release_status": "buildable",
                      "sha256": {"hex": "ab" * 32}}],
        "excluded_targets": [{"target": "bridge-nrf52dk", "reason": "hardware status blocked"}],
        "fonts": {"manifest_id": "f2c67d9125acaf06", "packs": {"dev": {"pack_id": "c1d7af1ec9fc564e", "size": 470296}}},
        "companion": {"wheel": {"file": "companion/cremind_tag-0.1.0-py3-none-any.whl"}},
    }
    notes = release.release_notes(manifest)
    assert "| `tag-laowu-bw` | `laowu_bw/nrf51822` | buildable |" in notes
    assert "`bridge-nrf52dk` (hardware status blocked)" in notes and "dev `c1d7af1ec9fc564e`" in notes
    assert "sha256sum -c SHA256SUMS" in notes


def test_main_packages_a_trial_release(tmp_path: Path, monkeypatch, capsys):
    src = tmp_path / "build"
    _fake_build(src, "tag-laowu-bw")
    monkeypatch.setattr(build, "git_info", lambda repo=None: {"commit": COMMIT, "describe": "v0.1.0", "dirty": False,
                                                              "changed_files": [], "commit_time": 1790536279})
    monkeypatch.setattr(release.ctag_version, "check", lambda version, root=None: [])
    rc = release.main(["--targets", "tag-laowu-bw", "--skip-fonts", "--skip-wheel", "--no-network",
                       "--firmware-dir", str(src), "--out", str(tmp_path / "dist")])
    assert rc == 0, capsys.readouterr().err
    version = str(release.ctag_version.read_version())
    dist = tmp_path / "dist" / version
    manifest = json.loads((dist / "release.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == release.MANIFEST_SCHEMA and manifest["version"] == version
    assert manifest["toolchain"]["digest"] == build.IMAGE_DIGEST
    assert manifest["ncs"]["sdk_nrf_commit"] == build.NCS_SDK_NRF_COMMIT
    assert [f["target"] for f in manifest["firmware"]] == ["tag-laowu-bw"]
    assert manifest["fonts"] is None and manifest["companion"] is None
    assert manifest["protocol"]["spec"] == "protocol/spec.yaml" and manifest["protocol"]["protocol_version"] == 1
    reasons = {e["target"]: e["reason"] for e in manifest["excluded_targets"]}
    assert reasons["tag-laowu-bwr"] == "not selected (--targets)" and "tag-sifei-52810" in reasons
    assert release.verify_sha256sums(dist) == []
    assert (dist / "LICENSE").is_file() and (dist / "protocol" / "fixtures" / "crc32.json").is_file()
    assert (tmp_path / "dist" / f"cremind-tag-{version}.tar.gz").is_file()
    assert (tmp_path / "dist" / f"RELEASE_NOTES-{version}.md").is_file()

    with pytest.raises(SystemExit):
        release.main(["--targets", "tag-sifei-52810", "--skip-fonts", "--skip-wheel"])
    assert "not publishable: tag-sifei-52810" in capsys.readouterr().err


def test_list_targets(capsys):
    assert release.main(["--list-targets"]) == 0
    assert capsys.readouterr().out.split() == [n for n, _ in release.eligible_targets()[0]]


def test_repository_licence_texts_exist():
    for path in ("LICENSE", "lib/third_party/qrcodegen/LICENSE", "fonts/LICENSES/OFL-1.1.txt",
                 "fonts/LICENSES/Apache-2.0.txt", "docs/protocol.md", "docs/fontpack.md", "protocol/spec.yaml"):
        assert (REPO_ROOT / path).is_file(), path
