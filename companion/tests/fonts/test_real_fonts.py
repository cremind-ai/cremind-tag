"""The pinned fonts themselves: needs the verified cache (`cremind-tag fonts fetch`) or the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from cremind_tag.fonts.build import build, plan_build
from cremind_tag.fonts.coverage import candidate_faces, check_text
from cremind_tag.fonts.fetch import VerificationError, check_font_facts, download, git_blob_sha1
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.fonts.manifest import (
    check_icon_codepoints,
    load_icon_map,
    load_lock,
    load_manifest,
    parse_codepoints,
)
from cremind_tag.fonts.rasterize import freetype_version

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "fonts" / "manifest.yaml"
SAMPLES = "Tiếng Việt có dấu, Ελληνικά, Русский, مرحبا بالعالم, שלום עולם, สวัสดีครับ, नमस्ते दुनिया"


@pytest.mark.fonts
def test_pinned_files_match_the_manifest(real_cache: Path) -> None:
    m = load_manifest(MANIFEST)
    lk = load_lock(m.lock_path)
    assert check_font_facts(m, {f.key: real_cache / lk.file(f.key).cache for f in m.faces}) == []
    codepoints = parse_codepoints((real_cache / lk.file("material-icons.codepoints").cache).read_text("utf-8"))
    check_icon_codepoints(load_icon_map(m), codepoints)


@pytest.mark.fonts
def test_dev_pack_is_reproducible_and_loads(real_cache: Path, tmp_path: Path) -> None:
    m = load_manifest(MANIFEST)
    if freetype_version() != m.render.freetype:
        pytest.skip(f"FreeType {freetype_version()} is not the pinned {m.render.freetype}")
    plan = plan_build(m, "dev")
    a = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "a", jobs=1)
    b = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "b", jobs=4)
    assert a.pack_id == b.pack_id and a.pack_path.read_bytes() == b.pack_path.read_bytes()
    fs = FontSet.load(a.pack_path, real_cache)
    assert [f.face_id for f in fs.faces] == [0, 1, 5, 29, 47, 151]
    assert check_text(SAMPLES, fs) == ()
    assert check_text("日本 😀", fs) == ("日", "本", "😀")
    assert fs.strike(0, 48).glyph_count == 25 and not fs.has_strike(1, 32)


@pytest.mark.fonts
def test_cjk_regional_faces_share_bitmaps(real_cache: Path, tmp_path: Path) -> None:
    m = load_manifest(MANIFEST)
    plan = plan_build(m, "full", faces=["noto-sans-sc", "noto-sans-tc", "noto-sans-hk"], sizes=[16])
    r = build(m, plan, cache_dir=real_cache, out_dir=tmp_path / "cjk", strict_freetype=False)
    assert r.bitmap_area_size < 0.75 * r.undeduplicated_bitmap_size  # TC and HK share ~88 % of their glyphs
    fs = FontSet.load(r.pack_path, real_cache)
    assert [f.key for f in candidate_faces(fs, "Hani", "zh-Hant-HK")][0] == "noto-sans-hk"
    assert [f.key for f in candidate_faces(fs, "Hani", "zh-TW")][0] == "noto-sans-tc"


@pytest.mark.network
def test_download_verifies_against_the_pinned_blob() -> None:
    m = load_manifest(MANIFEST)
    face = min((f for f in m.faces if f.role == "primary"), key=lambda f: f.file.size)
    try:
        data = download(m.url(face.file), retries=1, timeout=20)
    except VerificationError as exc:
        pytest.skip(f"offline: {exc}")
    assert len(data) == face.file.size and git_blob_sha1(data) == face.file.git_blob_sha1
