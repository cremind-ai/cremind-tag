"""The fake Cremind of the daemon tests, shared with the connector client tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

DAEMON_TESTS = Path(__file__).resolve().parents[1] / "daemon"


def _load(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DAEMON_TESTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fake_cremind = _load("fake_cremind")
