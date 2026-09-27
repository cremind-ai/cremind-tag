"""tools/gen_hardware_docs.py renders the matrix page and keeps qualification reports' hand-written parts."""

from __future__ import annotations

import re

import gen_hardware_docs as gen
import pytest

MATRIX, TARGETS, _ = gen.load_inputs(gen.REPO_ROOT / "does-not-exist.json")
BOARDS = [(group, b) for group in gen.GROUPS for b in MATRIX[group]]
REPORT = {
    "generated": "2026-09-27T00:00:00+00:00",
    "ncs": "v3.4.1",
    "targets": {
        "tag-laowu-bw": {
            "app": "apps/tag",
            "status": "ok",
            "built_at": "2026-09-27T00:00:00+00:00",
            "verify": {"ok": True, "failed": [], "dt_method": "edtlib"},
            "memory": {"flash_used": 104316, "flash_region": 126976, "ram_used": 14316, "ram_region": 16384},
            "resources": {
                "flash_headroom_pct": 17.85,
                "flash_headroom_pct_min": 15,
                "ram_free": 2068,
                "ram_free_min": 2048,
            },
        }
    },
}


def test_matrix_page_lists_every_board_and_target():
    page = gen.render_matrix(MATRIX, TARGETS, None)
    for _, board in BOARDS:
        assert f"[{board['id']}](../qualification/{board['id']}.md)" in page
    for name in TARGETS["targets"]:
        assert f"| {name} |" in page
    assert "No `build/memory-report.json` yet" in page


def test_matrix_page_build_facts():
    page = gen.render_matrix(MATRIX, TARGETS, REPORT)
    assert "| tag-laowu-bw | `apps/tag` | 104,316 / 126,976 | 17.9 % (15 %) |" in page
    assert "| tag-hema-52811 | not built yet |" in page


def test_board_check_builds_are_labelled():
    report = {**REPORT, "targets": {"tag-laowu-bw": {**REPORT["targets"]["tag-laowu-bw"], "app": "build/boardcheck"}}}
    assert "board-check app, not the product firmware" in gen.render_matrix(MATRIX, TARGETS, report)


@pytest.mark.parametrize(("group", "board"), BOARDS, ids=[b["id"] for _, b in BOARDS])
def test_qualification_template(group, board):
    block = gen.build_facts_block(board, TARGETS, None)
    text = gen.render_qualification(group, board, MATRIX, TARGETS, block)
    assert f"(`{board['id']}`)" in text
    assert f"Status: **{board['status']}**" in text
    assert "**TODO:**" in text and "is not built yet" in text
    assert gen.BEGIN in text and gen.END in text
    headings = re.findall(r"(?m)^## \d+\. (.+)$", text)
    assert headings == [
        "Identity",
        "Hardware verification checklist",
        "Build facts",
        "Functional tests",
        "Power",
        "Endurance",
        "Faults",
        "Status history",
    ]


def test_refresh_replaces_only_the_generated_block():
    board = MATRIX["tags"][0]
    old = f"# Report\n\nhand-written notes\n\n{gen.BEGIN}\nstale\n{gen.END}\n\nmore notes\n"
    new = gen.refresh_block(old, gen.build_facts_block(board, TARGETS, REPORT))
    assert "stale" not in new
    assert new.startswith("# Report\n\nhand-written notes\n\n") and new.endswith("\n\nmore notes\n")
    assert "104,316 / 126,976" in new
    assert gen.refresh_block("no markers here", "x") == "no markers here"


@pytest.mark.parametrize(("group", "board"), BOARDS, ids=[b["id"] for _, b in BOARDS])
def test_committed_reports_exist_with_markers(group, board):
    path = gen.QUALIFICATION_DIR / f"{board['id']}.md"
    text = path.read_text(encoding="utf-8")
    assert gen.BEGIN in text and gen.END in text
