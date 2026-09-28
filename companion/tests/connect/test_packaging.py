"""packaging/build_connect.py helpers: versions, .deb metadata, the tar.gz layout, the Inno Setup script."""

from __future__ import annotations

import configparser
import importlib.util
import json
import re
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_connect", PACKAGING / "build_connect.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_connect"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("version", "expected"), [
    ("0.1.0", "0.1.0"), ("0.2.0rc1", "0.2.0~rc1"), ("0.2.0-rc.1", "0.2.0~rc1"), ("0.2.0.dev7", "0.2.0~dev7"),
    ("1.0.0b2", "1.0.0~b2"), ("v1.2.3", "1.2.3"),
])
def test_debian_versions_sort_pre_releases_first(build: ModuleType, version: str, expected: str) -> None:
    assert build.deb_version(version) == expected


def test_build_info_refuses_versions_install_cannot_compare(build: ModuleType) -> None:
    info = build.build_info("0.3.0rc2")
    assert info["name"] == "cremind-connect" and info["version"] == "0.3.0rc2" and info["exe"]
    with pytest.raises(ValueError):
        build.build_info("latest")


def test_deb_files(build: ModuleType) -> None:
    files = build.deb_files("0.2.0rc1", "Someone <someone@example.org>", 123456)
    control = files["DEBIAN/control"][0]
    fields = dict(line.split(": ", 1) for line in control.splitlines() if ": " in line and not line.startswith(" "))
    assert fields["Package"] == "cremind-connect" and fields["Version"] == "0.2.0~rc1"
    assert fields["Maintainer"] == "Someone <someone@example.org>" and fields["Installed-Size"] == "123456"
    assert re.fullmatch(r"libc6 \(>= \d+\.\d+\), libx11-6", fields["Depends"])
    for script in ("DEBIAN/postinst", "DEBIAN/prerm", "DEBIAN/postrm"):
        text, mode = files[script]
        assert text.startswith("#!/bin/sh") and mode == 0o755
    postinst = files["DEBIAN/postinst"][0]
    assert "udevadm control --reload-rules" in postinst and "systemctl --global enable cremind-connect.service" in postinst
    rules, _ = files["lib/udev/rules.d/70-cremind-tag.rules"]
    assert 'ATTRS{idProduct}=="0002", TAG+="uaccess"' in rules
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(files["usr/lib/systemd/user/cremind-connect.service"][0])
    assert unit["Service"]["ExecStart"] == "/opt/cremind-connect/cremind-connect service"
    assert unit["Service"]["Restart"] == "always" and unit["Install"]["WantedBy"] == "default.target"
    desktop = files["usr/share/applications/cremind-connect.desktop"][0]
    assert "Exec=/opt/cremind-connect/cremind-connect open %u" in desktop
    assert "MimeType=x-scheme-handler/cremind-connect;" in desktop


def test_tarball_layout(build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    bundle = dist / "cremind-connect"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "cremind-connect").write_bytes(b"exe")
    (bundle / "connect.json").write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
    monkeypatch.setattr(build, "kind", lambda: "linux")
    monkeypatch.setattr(build, "arch", lambda: "x64")
    tarball = build.linux_tarball(dist, "0.1.0", tmp_path / "out")
    assert tarball.name == "cremind-connect-0.1.0-linux-x64.tar.gz"
    with tarfile.open(tarball) as archive:
        names = set(archive.getnames())
        install_sh = archive.getmember("cremind-connect-0.1.0-linux-x64/install.sh")
        script = archive.extractfile(install_sh).read().decode()  # type: ignore[union-attr]
    assert {"cremind-connect-0.1.0-linux-x64/cremind-connect", "cremind-connect-0.1.0-linux-x64/connect.json"} <= names
    assert install_sh.mode == 0o755 and 'install --from "$HERE"' in script


def test_inno_setup_script_installs_per_user_and_registers(build: ModuleType) -> None:
    iss = (PACKAGING / "windows" / "cremind-connect.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss
    assert r"DefaultDirName={localappdata}\Programs\Cremind Connect" in iss
    assert r'DestDir: "{app}\versions\{#AppVersion}"' in iss
    assert 'Parameters: "install --register-only"' in iss and 'Parameters: "uninstall --keep-data"' in iss
    assert re.search(r"AppId=\{\{[0-9A-F-]{36}\}", iss)


def test_spec_declares_the_url_scheme_and_windowed_build() -> None:
    spec = (PACKAGING / "cremind-connect.spec").read_text(encoding="utf-8")
    assert '"CFBundleURLSchemes": ["cremind-connect"]' in spec and '"LSUIElement": True' in spec
    assert "console=False" in spec and "argv_emulation=IS_MAC" in spec
    assert 'collect_submodules("serial.urlhandler")' in spec and 'copy_metadata' in spec
