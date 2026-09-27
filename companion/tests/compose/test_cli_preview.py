"""`cremind-tag preview` with the local packs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from cremind_tag.cli.main import app

pytestmark = pytest.mark.fonts
runner = CliRunner()
CACHE = Path(__file__).resolve().parents[3] / "fonts" / "cache"


def _run(fonts: Any, *args: str) -> Any:
    return runner.invoke(app, ["preview", *args, "--pack", str(fonts.pack_path), "--cache", str(CACHE)])


def test_preview_text(fonts: Any, tmp_path: Path) -> None:
    out = tmp_path / "t.png"
    r = _run(fonts, "text", "Tiếng Việt مرحبا 123 שלום\\nสวัสดีครับ 你好", "--size", "24", "--width", "300",
             "--lang", "vi", "--out", str(out), "--scale", "2")
    assert r.exit_code == 0, r.output
    assert "faces [1, 5, 47, 151, 166]" in r.output and "unsupported" not in r.output
    assert Image.open(out).width == 2 * 308
    r = _run(fonts, "text", "A\U00020000", "--out", str(tmp_path / "u.png"))
    assert r.exit_code == 0 and "U+20000" in r.output
    r = _run(fonts, "text", "x", "--dir", "sideways")
    assert r.exit_code == 1


def test_preview_card_screen_identify(fonts: Any, tmp_path: Path) -> None:
    job = {"delivery_id": 501, "kind": "needs_input", "priority": 90, "created_at": "2026-09-27T07:00:00Z",
           "card": {"v": 1, "kind": "needs_input", "severity": "attention", "icon": "help",
                    "title": "Approve deployment?", "body": "Release planning", "lang": "en",
                    "ts": "2026-09-27T07:00:00Z", "progress": None, "link": None}}
    card_file = tmp_path / "card.json"
    card_file.write_text(json.dumps(job), encoding="utf-8")
    out = tmp_path / "card.png"
    r = _run(fonts, "card", str(card_file), "--panel", "bwr", "--now", "2026-09-27T07:05:00Z", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "shown deliveries [501]" in r.output and Image.open(out).mode == "P"

    doc = {"settings": {"language": "vi", "timezone": "Asia/Ho_Chi_Minh", "show_excerpts": True},
           "jobs": [job, {**job, "delivery_id": 502, "priority": 40, "kind": "notification",
                          "card": {**job["card"], "kind": "notification", "title": "Thông báo mới"}}]}
    screen_file = tmp_path / "screen.json"
    screen_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "screen.png"
    r = _run(fonts, "screen", str(screen_file), "--rotation", "1", "--now", "2026-09-27T07:05:00Z",
             "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "300x400 rotation 1" in r.output and "shown deliveries [501, 502]" in r.output
    assert Image.open(out).size == (300, 400)
    r = _run(fonts, "card", str(screen_file))
    assert r.exit_code == 1 and "2 cards" in r.output

    out = tmp_path / "id.png"
    r = _run(fonts, "identify", "--tag-id", "CAFE0001", "--out", str(out))
    assert r.exit_code == 0 and out.is_file()


def test_preview_samples_with_the_dev_pack(dev_fonts: Any, tmp_path: Path) -> None:
    r = _run(dev_fonts, "samples", "--out", str(tmp_path / "s"))
    assert r.exit_code == 0, r.output
    assert "5 faces (5 drawn by their own face)" in r.output
    summary = json.loads((tmp_path / "s" / "summary.json").read_text(encoding="utf-8"))
    assert [f["face_id"] for f in summary["faces"]] == [1, 5, 29, 47, 151]
    assert (tmp_path / "s" / "screen-landscape-bw.png").is_file()
