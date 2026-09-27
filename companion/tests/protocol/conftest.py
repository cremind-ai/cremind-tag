"""Shared fixtures: repository paths, the fixture generator and the test font pack."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "protocol" / "fixtures"


def _load_tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def gen_fixtures() -> ModuleType:
    return _load_tool("gen_fixtures")


@pytest.fixture(scope="session")
def codegen() -> ModuleType:
    return _load_tool("codegen")


@pytest.fixture(scope="session")
def fixture() -> Any:
    """Loader for a committed JSON fixture by file name."""

    def load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="ascii"))

    return load


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def pack() -> Any:
    from cremind_tag.fontpack.format import FontPack

    return FontPack((FIXTURES / "fontpack_test.ctfp").read_bytes())
