"""Coverage: Unicode script data, per-script report, check_text, face candidates."""

from __future__ import annotations

from typing import Any

from cremind_tag.fonts.coverage import (
    CmapIndex,
    candidate_faces,
    check_text,
    coverage_report,
    format_ranges,
    load_unicode,
    needs_glyph,
)
from cremind_tag.fonts.fontset import FontSet


def test_needs_glyph() -> None:
    assert needs_glyph("A") and needs_glyph(" ") and needs_glyph("一") and needs_glyph("\U00020000")
    assert not any(needs_glyph(c) for c in "\n\t‍‌‏️\U000e0100­")
    assert needs_glyph("؀")  # Arabic number sign: a format character that is drawn


def test_load_unicode(repo: Any) -> None:
    u = load_unicode(repo.cache / "unicode" / "17.0.0", "17.0.0")
    assert u.names["Hani"] == "Han" and u.scripts["Latn"] == frozenset({0x41, 0x42, 0x43})
    assert u.script_of(0x4E01) == "Hani" and u.script_of(0x20000) == "Hani" and u.script_of(0x0378) == "Zzzz"
    assert u.extensions["Hani"] == frozenset({0x3001})


def test_coverage_report(repo: Any, built: Any) -> None:
    fs = FontSet.load(built.pack_path, repo.cache)
    u = load_unicode(repo.cache / "unicode" / "17.0.0", "17.0.0")
    report = coverage_report(u, [(f.face_id, f.key, f.scripts, f.path) for f in fs.faces if f.path])
    s = report["scripts"]
    assert (s["Latn"]["covered"], s["Latn"]["total"], s["Latn"]["status"]) == (2, 3, "partial")
    assert s["Latn"]["missing"] == ["U+0043"] and s["Latn"]["faces"] == [1]
    assert (s["Hani"]["covered"], s["Hani"]["status"], s["Hani"]["faces"]) == (4, "full", [2, 3])
    assert s["Thai"]["status"] == "none" and s["Hani"]["extensions_total"] == 1
    assert report["summary"] == {"scripts": 3, "full": 1, "partial": 1, "none": 1, "partial_scripts": ["Latn"],
                                 "missing_scripts": ["Thai"], "code_points_covered": 8}
    assert report["faces"]["test-han-jp"]["declared"]["Hani"] == [2, 4]


def test_check_text(repo: Any, built: Any) -> None:
    fs = FontSet.load(built.pack_path, repo.cache)
    assert check_text("AB 一丂\U00020000\n‍", fs) == ()
    assert check_text("ACAก七C", fs) == ("C", "ก", "七")
    index = CmapIndex.for_fontset(fs)
    assert index.faces_for(0x4E00) == (2, 3) and index.faces_for(0x4E02) == (2,) and index.covers(0x2014)
    assert check_text("", fs) == ("",)  # icon codepoints are not text


def test_candidate_faces(repo: Any, built: Any) -> None:
    fs = FontSet.load(built.pack_path, repo.cache)
    keys = lambda script, lang="": [f.key for f in candidate_faces(fs, script, lang)]  # noqa: E731
    assert keys("Hani", "ja") == ["test-han-jp", "test-han-sc"]
    assert keys("Hani", "ja-JP") == ["test-han-jp", "test-han-sc"]
    assert keys("Hani") == ["test-han-sc", "test-han-jp"]  # no language: zh-Hans face
    assert keys("Hani", "en") == ["test-han-sc", "test-han-jp"]
    assert keys("Latn", "ja") == ["test-sans"] and keys("Thai") == []


def test_format_ranges() -> None:
    assert format_ranges([0x41, 0x42, 0x43, 0x45, 0x20000]) == ["U+0041..U+0043", "U+0045", "U+20000"]
