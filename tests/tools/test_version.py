"""tools/version.py: the one VERSION and its derived copies (the apps' Zephyr VERSION files)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ctag_tools_helpers import REPO_ROOT

import version as v


def test_parse_forms():
    assert v.parse("0.1.0") == v.Version(0, 1, 0)
    rc = v.parse("1.2.3-rc.4\n")
    assert (str(rc), rc.tag) == ("1.2.3-rc.4", "v1.2.3-rc.4")
    assert str(v.parse("2.0.0-alpha.1")) == "2.0.0-alpha.1"
    for bad in ("1.2", "01.2.3", "1.2.3-dev", "1.2.3-rc1", "1.2.3+build", "v1.2.3", "256.0.0"):
        with pytest.raises(ValueError):
            v.parse(bad)


def test_zephyr_file_round_trip():
    for text in ("0.1.0", "3.14.255-rc.2"):
        version = v.parse(text)
        body = version.zephyr_file()
        assert "VERSION_TWEAK = 0\n" in body
        assert v.parse_zephyr_version(body) == version
    assert v.parse_zephyr_version("VERSION_MAJOR = 1\n") is None
    assert v.parse_zephyr_version(v.parse("1.0.0").zephyr_file().replace("TWEAK = 0", "TWEAK = 3")) is None


def _repo(tmp_path: Path, apps: dict[str, str | None]) -> Path:
    (tmp_path / "tools").mkdir()
    targets = "targets:\n" + "".join(f"  t-{i}:\n    app: {app}\n" for i, app in enumerate(apps))
    (tmp_path / "tools" / "targets.yaml").write_text(targets, encoding="utf-8")
    for app, content in apps.items():
        d = tmp_path / app
        d.mkdir(parents=True)
        (d / "CMakeLists.txt").write_text("", encoding="utf-8")
        if content is not None:
            (d / "VERSION").write_text(content, encoding="utf-8")
    return tmp_path


def test_check_and_sync(tmp_path: Path):
    version = v.parse("0.2.0-rc.1")
    root = _repo(tmp_path, {"apps/a": v.parse("0.1.0").zephyr_file(), "apps/b": None, "apps/c": "garbage"})
    (root / "apps" / "ghost").mkdir()  # no CMakeLists: not an app yet
    problems = v.check(version, root)
    assert any("apps/a/VERSION says 0.1.0" in p for p in problems)
    assert any("apps/b/VERSION is missing" in p for p in problems)
    assert any("apps/c/VERSION is not a Zephyr VERSION file" in p for p in problems)
    changed = v.sync(version, root)
    assert {p.relative_to(root).as_posix() for p in changed} == {"apps/a/VERSION", "apps/b/VERSION", "apps/c/VERSION"}
    assert v.check(version, root) == []
    assert v.sync(version, root) == []
    assert (root / "apps" / "a" / "VERSION").read_text(encoding="utf-8") == version.zephyr_file()


def test_repository_versions_are_in_sync():
    version = v.read_version()
    assert v.check(version, REPO_ROOT) == [], "run: python tools/version.py --sync"
    assert set(v.app_dirs()) == {"apps/gateway", "apps/bridge", "apps/tag"}


def test_cli(capsys, monkeypatch):
    assert v.main([]) == 0
    assert capsys.readouterr().out.strip() == str(v.read_version())
    assert v.main(["--expect-tag", v.read_version().tag]) == 0
    assert v.main(["--expect-tag", "v9.9.9"]) == 1
    assert "does not match" in capsys.readouterr().err
    assert v.main(["--json"]) == 0
    assert capsys.readouterr().out.strip() == f'{{"version": "{v.read_version()}", "tag": "{v.read_version().tag}"}}'
