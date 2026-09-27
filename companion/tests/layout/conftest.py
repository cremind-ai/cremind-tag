"""Fixtures: the locally built font packs (fonts/out/<profile>) and the verified font cache.

Tests that need them are marked ``fonts`` and skip cleanly when
``cremind-tag fonts fetch`` / ``fonts build`` have not been run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


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
    """The full pack (171 faces, 16/24/32 px)."""
    return load_pack("full")


@pytest.fixture(scope="session")
def dev_fonts() -> Any:
    """The dev pack (6 faces, 16/24 px — no 32 px strikes)."""
    return load_pack("dev")
