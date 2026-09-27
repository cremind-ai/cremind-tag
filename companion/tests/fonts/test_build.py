"""Pack builds from the synthetic repository: determinism, de-duplication, validation, FontSet.load."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from cremind_tag.fontpack.format import FACE_FLAG_CJK, FACE_FLAG_ICON, FontPack
from cremind_tag.fonts.build import (
    BuildError,
    bitmap_area_size,
    build,
    check_freetype,
    load_fontset,
    plan_build,
)
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.fonts.manifest import ManifestError, load_manifest
from cremind_tag.protocol.ids import Icon


def test_plan_profiles_and_filters(repo: Any) -> None:
    m = load_manifest(repo.manifest)
    full = plan_build(m, "full")
    assert [f.key for f in full.faces] == ["test-icons", "test-sans", "test-han-sc", "test-han-jp"]
    assert (full.name, full.text_sizes, full.icon_sizes) == ("full", (16, 24, 32), (16, 24, 32, 48))
    dev = plan_build(m, "dev")
    assert [f.key for f in dev.faces] == ["test-icons", "test-sans"] and dev.text_sizes == (16,)
    custom = plan_build(m, "full", faces=["3"], sizes=[16, 48])
    assert [f.key for f in custom.faces] == ["test-icons", "test-han-jp"]
    assert (custom.name, custom.text_sizes, custom.icon_sizes) == ("full-custom", (16,), (16, 48))
    with pytest.raises(BuildError, match="neither"):
        plan_build(m, "full", sizes=[20])
    with pytest.raises(BuildError, match="unknown profile"):
        plan_build(m, "tiny")
    with pytest.raises(ManifestError, match="no face"):
        plan_build(m, "full", faces=["nope"])


def test_pack_round_trips_through_the_format(built: Any) -> None:
    data = built.pack_path.read_bytes()
    pack = FontPack(data)
    assert pack.pack_id == built.pack_id and pack.total_size == len(data) == built.total_size
    faces = {f.face_id: f for f in pack.faces}
    assert faces[0].flags & FACE_FLAG_ICON and faces[0].glyph_count == len(Icon) + 1
    assert faces[0].name == "Test Icons 1.000" and faces[1].scripts == "Latn"
    assert faces[2].flags & FACE_FLAG_CJK and faces[2].scripts == "Hans,Hani"
    assert [(s.face_id, s.size_px) for s in pack.strikes] == (
        [(0, s) for s in (16, 24, 32, 48)] + [(f, s) for f in (1, 2, 3) for s in (16, 24, 32)])
    bar = pack.glyph(1, 16, 2)
    assert bar is not None and (bar.width, bar.height, bar.bearing_x, bar.bearing_y, bar.advance) == (6, 10, 2, 10, 10)
    assert pack.glyph(1, 16, 1) is not None and pack.glyph(1, 16, 1).width == 0  # type: ignore[union-attr]
    icon = pack.glyph(0, 48, int(Icon.PERSON))
    assert icon is not None and (icon.width, icon.height, icon.bearing_x, icon.bearing_y) == (48, 48, 0, 0)
    assert built.face_stats["test-sans"].clipped == 3  # the 280-px bar at 16, 24 and 32 px


def test_builds_are_deterministic(repo: Any, built: Any, tmp_path: Path) -> None:
    m = load_manifest(repo.manifest)
    again = build(m, plan_build(m, "full"), cache_dir=repo.cache, out_dir=tmp_path / "again", jobs=2,
                  strict_freetype=False)
    assert again.pack_id == built.pack_id
    assert again.pack_path.read_bytes() == built.pack_path.read_bytes()
    assert again.sidecar_path.read_bytes() == built.sidecar_path.read_bytes()
    assert (tmp_path / "again" / "NOTICE").read_bytes() == (built.pack_path.parent / "NOTICE").read_bytes()


def test_identical_bitmaps_are_stored_once(built: Any) -> None:
    data = built.pack_path.read_bytes()
    pack = FontPack(data)
    assert bitmap_area_size(data) == built.bitmap_area_size < built.undeduplicated_bitmap_size
    # Regional faces share glyph outlines: the JP strike reuses the SC bitmaps.
    for size in (16, 24, 32):
        index = {}
        for face in (2, 3):
            strike = pack.strike(face, size)
            assert strike is not None
            index[face] = data[strike.index_off: strike.index_off + strike.glyph_count * 12]
        assert index[3] == index[2][: len(index[3])]
    assert built.cjk_unique_bitmap_size < built.cjk_bitmap_size
    sans = pack.strike(1, 16)
    assert sans is not None
    a, b = (data[sans.index_off + 12 * g: sans.index_off + 12 * g + 4] for g in (2, 3))
    assert a == b  # 'A' and 'B' share one bitmap


def test_sidecar_and_notice(built: Any) -> None:
    doc = json.loads(built.sidecar_path.read_text(encoding="utf-8"))
    assert doc["pack_id"] == built.pack_id.hex() and doc["manifest_id"] == built.manifest_id.hex()
    assert [f["face_id"] for f in doc["faces"]] == [0, 1, 2, 3]
    sc = doc["faces"][2]
    assert sc["languages"] == ["zh-Hans", "zh"] and sc["file"]["cache"] == "test/TestHanSC-Regular.ttf"
    assert len(sc["file"]["sha256"]) == 64 and sc["render_mode"] == "none"
    notice = (built.pack_path.parent / "NOTICE").read_text(encoding="utf-8")
    assert "Test Glyph Pack" in notice and "derived from Noto" in notice and "Apache License" in notice
    assert "Copyright Test Han" in notice and sc["file"]["sha256"] in notice
    assert {p.name for p in (built.pack_path.parent / "LICENSES").iterdir()} == {"OFL-1.1.txt", "Apache-2.0.txt"}


def test_fontset_load(repo: Any, built: Any) -> None:
    fs = FontSet.load(built.pack_path, repo.cache)
    assert fs.pack_id == built.pack_id and [f.face_id for f in fs.faces] == [0, 1, 2, 3]
    icons, sans, sc, jp = fs.faces
    assert icons.path is None and icons.role == "icons"
    assert sans.path == repo.cache / "test" / "TestSans-Regular.ttf" and sans.scripts == ("Latn",)
    assert (sc.role, sc.languages, jp.languages) == ("cjk-region", ("zh-Hans", "zh"), ("ja",))
    assert fs.strike(1, 16).ascent == 14 and fs.strike(1, 16).glyph_count == 5
    assert fs.has_strike(0, 48) and not fs.has_strike(1, 48)


def test_fontset_load_verifies_font_files(repo: Any, built: Any, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    shutil.copytree(repo.cache, cache)
    target = cache / "test" / "TestSans-Regular.ttf"
    target.write_bytes(target.read_bytes() + b"\0")
    with pytest.raises(ManifestError, match="does not match"):
        load_fontset(built.pack_path, cache)
    target.unlink()
    with pytest.raises(FileNotFoundError, match="fonts fetch"):
        load_fontset(built.pack_path, cache)


def test_fontset_load_without_sidecar_uses_the_manifest(repo: Any, built: Any, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    pack = tmp_path / "fontpack.ctfp"
    shutil.copyfile(built.pack_path, pack)
    monkeypatch.setenv("CREMIND_TAG_REPO", str(repo.root))
    fs = load_fontset(pack, repo.cache)
    assert [f.key for f in fs.faces] == ["test-icons", "test-sans", "test-han-sc", "test-han-jp"]


def test_build_refuses_an_unpinned_freetype(repo: Any) -> None:
    m = load_manifest(repo.manifest)  # pins FreeType "0.0.0"
    with pytest.raises(BuildError, match="FreeType"):
        check_freetype(m)
    with pytest.raises(BuildError, match="FreeType"):
        build(m, plan_build(m, "dev"), cache_dir=repo.cache)
