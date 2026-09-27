"""Fixtures for the simulated scale and fault test: the tool (tools/sim_scale.py) and the dev font pack."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


def load_tool() -> ModuleType:
    name = "sim_scale"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / "sim_scale.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sim_scale() -> ModuleType:
    return load_tool()


@pytest.fixture(scope="session")
def scale_fonts(sim_scale: ModuleType) -> Any:
    try:
        return sim_scale.load_fonts()
    except sim_scale.ScaleSetupError as exc:
        pytest.skip(str(exc))
