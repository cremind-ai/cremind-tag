"""Fixtures for the enrollment tests: the committed enrollment fixture, a fake target, a throwaway inventory."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "protocol" / "fixtures"


def _fakes() -> ModuleType:
    name = "_enroll_fakes"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("fakes.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def fakes() -> ModuleType:
    return _fakes()


@pytest.fixture(scope="session")
def enrollment_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "enrollment.json").read_text(encoding="ascii"))  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def fixture_blob(enrollment_fixture: dict[str, Any]) -> bytes:
    return bytes.fromhex(enrollment_fixture["blob"])


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Any]:
    from cremind_tag.store.db import Database

    database = Database.open(tmp_path / "companion.sqlite3")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """A file-backed secret store; the OS keyring is never touched."""
    from cremind_tag.secrets import FileBackend, SecretStore

    return SecretStore(FileBackend(tmp_path / "secrets.json"))


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "enroll"
