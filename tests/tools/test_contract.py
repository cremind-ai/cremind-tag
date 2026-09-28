"""tools/contract.py: the protocol contract artifact host software pins."""

from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from ctag_tools_helpers import REPO_ROOT

import contract as c


def _repo(tmp_path: Path) -> Path:
    """A tiny git repository with the contract's inputs."""
    root = tmp_path / "repo"
    (root / "protocol" / "fixtures").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "VERSION").write_text("1.2.3\n", encoding="ascii")
    (root / "protocol" / "spec.yaml").write_text(
        "spec_version: 2\nconstants:\n  PROTO_VERSION: {value: 1}\n  SECURE_PROTO_VERSION: {value: 2}\n"
        "fontpack:\n  version: 1\n", encoding="utf-8")
    (root / "protocol" / "fixtures" / "a.json").write_text('{"x": 1}\n', encoding="utf-8")
    (root / "protocol" / "fixtures" / "pack.ctfp").write_bytes(b"\x00\x01\r\n\x02")
    (root / "docs" / "protocol.md").write_bytes(b"# Protocol\r\n")  # as a CRLF checkout has it
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def test_builds_a_verifiable_reproducible_artifact(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target, archive = c.build(tmp_path / "out", root=root)
    meta = c.check(target)
    assert meta["version"] == "1.2.3" and meta["schema"] == c.SCHEMA
    assert meta["protocol"] == {"spec_version": 2, "proto_version": 1, "secure_proto_version": 2,
                                "fontpack_version": 1}
    assert set(meta["files"]) == {"spec.yaml", "fixtures/a.json", "fixtures/pack.ctfp", "docs/protocol.md"}
    assert meta["source"]["revision"] == subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                                        capture_output=True, text=True).stdout.strip()
    assert (target / "docs" / "protocol.md").read_bytes() == b"# Protocol\n"  # text normalised to LF
    assert (target / "fixtures" / "pack.ctfp").read_bytes() == b"\x00\x01\r\n\x02"  # binary kept as is
    first = archive.read_bytes()
    _, again = c.build(tmp_path / "out2", root=root)
    assert again.read_bytes() == first
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert names == sorted(names) and all(n.startswith("cremind-tag-contract-1.2.3/") for n in names)
    sha = (archive.parent / (archive.name + ".sha256")).read_text(encoding="ascii").split()[0]
    assert sha == c.sha256_bytes(first)


def test_check_refuses_changed_missing_and_unlisted_files(tmp_path: Path) -> None:
    target, _ = c.build(tmp_path / "out", root=_repo(tmp_path))
    (target / "fixtures" / "a.json").write_text('{"x": 2}\n', encoding="utf-8")
    (target / "extra.txt").write_text("?", encoding="ascii")
    (target / "docs" / "protocol.md").unlink()
    with pytest.raises(c.ContractError) as err:
        c.check(target)
    assert "changed fixtures/a.json" in str(err.value)
    assert "missing docs/protocol.md" in str(err.value)
    assert "unlisted extra.txt" in str(err.value)


def test_a_tampered_digest_is_refused(tmp_path: Path) -> None:
    target, _ = c.build(tmp_path / "out", root=_repo(tmp_path))
    meta = json.loads((target / c.META).read_text(encoding="utf-8"))
    meta["digest"] = "0" * 64
    (target / c.META).write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(c.ContractError, match="digest"):
        c.check(target)


def test_uncommitted_inputs_are_refused_unless_allowed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "protocol" / "spec.yaml").write_text(
        (root / "protocol" / "spec.yaml").read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
    with pytest.raises(c.ContractError, match="uncommitted"):
        c.build(tmp_path / "out", root=root)
    target, _ = c.build(tmp_path / "out", root=root, allow_dirty=True)
    assert json.loads((target / c.META).read_text(encoding="utf-8"))["source"]["dirty"] is True


def test_this_repository_builds_its_contract(tmp_path: Path) -> None:
    target, _ = c.build(tmp_path / "out", root=REPO_ROOT, allow_dirty=True)
    meta = c.check(target)
    assert meta["protocol"]["spec_version"] >= 2
    assert "fixtures/v2_secure.json" in meta["files"]
