"""Fixtures for the daemon tests: the dev font pack, the fake Cremind and the simulator rig."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fake_cremind = _load("fake_cremind")
rig = _load("rig")
fake_server = _load("fake_server")


def load_dev_fonts() -> Any:
    from cremind_tag.fonts.fontset import FontSet
    from cremind_tag.fonts.manifest import ManifestError

    pack = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp"
    cache = REPO / "fonts" / "cache"
    if not pack.is_file() or not cache.is_dir():
        pytest.skip(f"{pack} or {cache} is missing (run `cremind-tag fonts fetch` and `fonts build --profile dev`)")
    try:
        return FontSet.load(pack, cache)
    except (OSError, ValueError, ManifestError) as exc:
        pytest.skip(f"dev font pack not usable: {exc}")


@pytest.fixture(scope="session")
def dev_fonts() -> Any:
    return load_dev_fonts()


@pytest.fixture
def make_rig(tmp_path: Path, dev_fonts: Any) -> Any:
    def factory(**options: Any) -> Any:
        return rig.Rig(tmp_path, dev_fonts, **options)

    return factory
