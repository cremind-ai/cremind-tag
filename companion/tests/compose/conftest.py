"""Fixtures: the locally built font packs and a realistic card set (see tests/layout/conftest.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 27, 7, 5, tzinfo=UTC)


def load_pack(profile: str) -> Any:
    from cremind_tag.fonts.fontset import FontSet
    from cremind_tag.fonts.manifest import ManifestError

    pack = REPO / "fonts" / "out" / profile / "fontpack.ctfp"
    cache = REPO / "fonts" / "cache"
    if not pack.is_file() or not cache.is_dir():
        pytest.skip(f"{pack} or {cache} is missing (run `cremind-tag fonts fetch` and `fonts build`)")
    try:
        return FontSet.load(pack, cache)
    except (OSError, ValueError, ManifestError) as exc:
        pytest.skip(f"font pack {profile} not usable: {exc}")


@pytest.fixture(scope="session")
def fonts() -> Any:
    return load_pack("full")


@pytest.fixture(scope="session")
def dev_fonts() -> Any:
    return load_pack("dev")


@pytest.fixture
def now() -> datetime:
    return NOW
