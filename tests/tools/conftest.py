"""pytest setup for the tools/ tests (verify_stack.py, build.py, board files)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for path in (REPO_ROOT / "tools", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture
def artifacts(tmp_path: Path):
    """Copy a named directory from fixtures/ into tmp_path so a test can edit it."""

    def _copy(name: str) -> Path:
        dst = tmp_path / name
        shutil.copytree(HERE / "fixtures" / name, dst)
        return dst

    return _copy
