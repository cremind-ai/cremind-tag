"""`cremind-tag fonts` against the synthetic repository."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from cremind_tag.cli.main import app

runner = CliRunner()


def _run(*args: str) -> Any:
    return runner.invoke(app, ["fonts", *args])


def test_cli_pipeline(repo: Any, tmp_path: Path) -> None:
    common = ["--manifest", str(repo.manifest)]
    cache = ["--cache", str(repo.cache)]
    r = _run("list", *common)
    assert r.exit_code == 0 and "test-han-jp" in r.output and "4 faces" in r.output

    r = _run("lock", *common, *cache)
    assert r.exit_code == 0, r.output
    assert "manifest id" in r.output

    r = _run("build", "--profile", "dev", "--out", str(tmp_path / "dev"), "--jobs", "1", *common, *cache)
    assert r.exit_code == 1 and "FreeType" in r.output  # the synthetic manifest pins FreeType 0.0.0
    r = _run("build", "--profile", "dev", "--out", str(tmp_path / "dev"), "--jobs", "1",
             "--allow-freetype-mismatch", *common, *cache)
    assert r.exit_code == 0, r.output
    pack = tmp_path / "dev" / "fontpack.ctfp"
    assert pack.is_file() and "pack id" in r.output and "flash rule" in r.output

    r = _run("size", str(pack), *common)
    assert r.exit_code == 0 and "256 Mbit (32 MiB)" in r.output and "DEVELOPMENT ONLY" in r.output
    r = _run("size", str(pack), "--flash-size", "8MiB", *common)
    assert r.exit_code == 1 and "DOES NOT SATISFY" in r.output

    r = _run("coverage", "--pack", str(pack), "--text", "AB A", *common, *cache)
    assert r.exit_code == 0 and "covered" in r.output
    r = _run("coverage", "--pack", str(pack), "--text", "ABC", *common, *cache)
    assert r.exit_code == 1 and "U+0043" in r.output
    r = _run("coverage", "--pack", str(pack), *common, *cache)
    assert r.exit_code == 0 and (tmp_path / "dev" / "coverage.json").is_file()

    r = _run("image", "--pack", str(pack), "--out", str(tmp_path / "img"), *common)
    assert r.exit_code == 0 and "DEVELOPMENT ONLY" in r.output and (tmp_path / "img" / "flash.hex").is_file()

    r = _run("notice", "--pack", str(pack), "--out", str(tmp_path / "n"), *common)
    assert r.exit_code == 0 and (tmp_path / "n" / "NOTICE").is_file()
