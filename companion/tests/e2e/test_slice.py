"""The first vertical slice against a real, throwaway Cremind (``tools/e2e_slice.py``).

Skipped unless ``CREMIND_E2E=1``. Needs the Cremind repository with its
``.venv`` (``CREMIND_REPO``, default ``C:\\Users\\lyntc\\DATA\\Personal\\Cremind\\cremind``),
a free port (``CREMIND_E2E_PORT``, default 1181), the dev font pack and the font
cache. Cremind boots from a fresh scratch system directory under
``build/e2e-slice/`` — never a copy of ``~/.cremind`` — and logs to its own
``logs/app.log``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.timeout(900),
    pytest.mark.skipif(os.environ.get("CREMIND_E2E") != "1", reason="set CREMIND_E2E=1 to boot a throwaway Cremind"),
]


def _slice_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_slice", REPO / "tools" / "e2e_slice.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_slice"] = module
    spec.loader.exec_module(module)
    return module


def test_first_vertical_slice() -> None:
    e2e = _slice_module()
    if not e2e.DEV_PACK.is_file() or not e2e.FONT_CACHE.is_dir():
        pytest.skip("the dev font pack or the font cache is missing (cremind-tag fonts fetch / fonts build)")
    repo = Path(os.environ.get("CREMIND_REPO", str(e2e.DEFAULT_CREMIND)))
    scratch = REPO / "build" / "e2e-slice" / ("pytest-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    scratch.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(e2e.run_slice(repo, int(os.environ.get("CREMIND_E2E_PORT", "1181")), scratch,
                                       time_scale=float(os.environ.get("CREMIND_E2E_TIME_SCALE", "10"))))
    assert report["ok"], report
    assert report["assign_tag"] == report["clear_tag"] == "succeeded"
    note = report["pinned_note"]
    assert note["stage"] == "displayed" and note["revision"] >= 1 and len(note["digest"]) == 16
    assert note["preview"]["png"] and note["preview"]["revision"] >= note["revision"]
    assert set(note["stages"]) >= {"companion_accepted", "gateway_received", "displayed"}
    assert report["device"]["displayed_revision"] >= note["revision"]
    print("e2e report:", report)
    shutil.rmtree(scratch, ignore_errors=True)
