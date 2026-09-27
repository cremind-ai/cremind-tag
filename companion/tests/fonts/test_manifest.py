"""Manifest, icon map and lock validation (fonts/manifest.yaml, fonts/icons.yaml, manifest.lock.json)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from cremind_tag.fonts.fetch import VerificationError, git_blob_sha1, lock, verify_cached
from cremind_tag.fonts.manifest import (
    ManifestError,
    check_lock_current,
    load_icon_map,
    load_lock,
    load_manifest,
    parse_manifest,
)
from cremind_tag.protocol.ids import Icon

REPO = Path(__file__).resolve().parents[3]


def test_repository_manifest_is_valid() -> None:
    m = load_manifest(REPO / "fonts" / "manifest.yaml")
    assert m.icon_face.face_id == 0 and m.icon_face.key == "material-icons"
    assert [f.face_id for f in m.faces] == sorted({f.face_id for f in m.faces})
    assert len(m.faces) == 172
    assert {f.role for f in m.faces} == {"icons", "primary", "supplement", "cjk-region", "optional", "emoji"}
    cjk = {f.key: f.languages for f in m.faces if f.role == "cjk-region"}
    assert set(cjk) == {"noto-sans-sc", "noto-sans-tc", "noto-sans-hk", "noto-sans-jp", "noto-sans-kr"}
    assert all("Hani" in m.face(k).scripts for k in cjk)
    assert m.face("noto-emoji").variations == (("wght", 400.0),)
    assert m.face("noto-sans-arabic").rtl and m.face("noto-sans-hebrew").rtl and not m.face("noto-sans").rtl
    assert m.face("noto-nastaliq-urdu").role == "optional"
    assert {f.license for f in m.faces} == {"OFL-1.1", "Apache-2.0"}
    assert m.profiles["dev"].faces is not None and "noto-sans-thai" in m.profiles["dev"].faces


def test_repository_icon_map_matches_the_spec() -> None:
    icons = load_icon_map(load_manifest(REPO / "fonts" / "manifest.yaml"))
    assert [(i.id, i.name) for i in icons] == [(i.value, i.name.lower()) for i in Icon]
    by_name = {i.name: i for i in icons}
    assert by_name["battery_low"].material == "battery_alert" and by_name["battery_low"].note
    assert by_name["hourglass"].material == "hourglass_empty"


def test_repository_lock_is_current() -> None:
    m = load_manifest(REPO / "fonts" / "manifest.yaml")
    lk = load_lock(m.lock_path)
    check_lock_current(m, lk)
    assert lk.manifest_id == hashlib.sha256(m.lock_path.read_bytes()).digest()[:8]
    assert b"\r" not in lk.raw


def test_git_blob_sha1() -> None:
    assert git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def _doc(repo: Any) -> dict[str, Any]:
    return yaml.safe_load(repo.manifest.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(schema="other"), "schema"),
    (lambda d: d["faces"][1].update(face_id=0), "face_id"),
    (lambda d: d["faces"][0].update(face_id=9), "face_id 0"),
    (lambda d: d["faces"][1].update(key="test-sans-x", git_blob_sha1="abc"), "git_blob_sha1"),
    (lambda d: d["faces"][1].update(scripts=["latin"]), "ISO 15924"),
    (lambda d: d["faces"][1].update(role="main"), "role"),
    (lambda d: d["faces"][1].update(hinting="light"), "hinting"),
    (lambda d: d["faces"][1].update(license="GPL"), "license"),
    (lambda d: d["faces"][2].update(path=d["faces"][3]["path"]), "cache file"),
    (lambda d: d["profiles"]["dev"].update(text_sizes=[20]), "sizes"),
    (lambda d: d["profiles"]["dev"].update(faces=["test-sans"]), "icon face"),
    (lambda d: d["profiles"]["dev"].update(faces=["test-icons", "nope"]), "unknown faces"),
    (lambda d: d["sources"]["test"].update(commit="main"), "commit"),
    (lambda d: d["render"]["hinting"].update(cff="light"), "render.hinting"),
])
def test_invalid_manifests(repo: Any, mutate: Any, message: str) -> None:
    doc = copy.deepcopy(_doc(repo))
    mutate(doc)
    with pytest.raises(ManifestError, match=message):
        parse_manifest(doc, repo.manifest)


def test_lock_is_deterministic_and_detects_staleness(repo: Any) -> None:
    m = load_manifest(repo.manifest)
    first = m.lock_path.read_bytes()
    assert lock(m, repo.cache).raw == first
    doc = _doc(repo)
    doc["faces"][1]["size"] += 1
    stale = parse_manifest(doc, repo.manifest)
    with pytest.raises(ManifestError, match="out of date"):
        check_lock_current(stale, load_lock(m.lock_path))


def test_lock_rejects_a_file_that_is_not_the_pinned_blob(repo: Any, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc(repo)
    doc["faces"][1]["git_blob_sha1"] = "0" * 40
    m = parse_manifest(doc, tmp_path / "manifest.yaml")
    fetched: list[str] = []

    def fake_download(url: str, **_: Any) -> bytes:
        fetched.append(url)
        return (repo.cache / "test" / "TestSans-Regular.ttf").read_bytes()

    # The cached copy no longer matches the pin, so lock downloads it again; the download fails the pin too.
    monkeypatch.setattr("cremind_tag.fonts.fetch.download", fake_download)
    with pytest.raises(VerificationError, match="git blob"):
        lock(m, repo.cache, workers=1)
    assert fetched == ["https://example.invalid/0123456789abcdef0123456789abcdef01234567/sans/TestSans-Regular.ttf"]


def test_lock_rejects_wrong_font_facts(repo: Any, tmp_path: Path) -> None:
    doc = _doc(repo)
    doc["faces"][1]["num_glyphs"] += 1
    doc["faces"][1]["copyright"] = "someone else"
    m = parse_manifest(doc, tmp_path / "manifest.yaml")
    with pytest.raises(VerificationError, match="num_glyphs.*\n.*copyright"):
        lock(m, repo.cache)


def test_verify_cached_detects_tampering(repo: Any, tmp_path: Path) -> None:
    m = load_manifest(repo.manifest)
    lk = load_lock(m.lock_path)
    assert set(verify_cached(lk, repo.cache, ["test-sans"])) == {"test-sans"}
    bad = tmp_path / "cache"
    (bad / "test").mkdir(parents=True)
    src = repo.cache / lk.file("test-sans").cache
    (bad / lk.file("test-sans").cache).write_bytes(src.read_bytes() + b"\0")
    with pytest.raises(VerificationError, match="SHA-256"):
        verify_cached(lk, bad, ["test-sans"])
    with pytest.raises(VerificationError, match="missing"):
        verify_cached(lk, bad, ["test-icons"])
