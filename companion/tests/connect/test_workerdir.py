"""worker.json: validation, round trip, unknown keys preserved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cremind_tag.connect.workerdir import WorkerDirError, WorkerSpec, list_workers, load_worker, write_worker

DEVICE = "0f1e2d3c4b5a69788796a5b4c3d2a1b2"


def spec(worker_id: str = "w1", **overrides: object) -> WorkerSpec:
    data = {"worker_id": worker_id, "server_origin": "https://Cremind.Example.org:443", "profile": "Anna",
            "companion_id": "comp-1", "gateway_device_id": DEVICE.upper(), "enabled": True, **overrides}
    return WorkerSpec.from_json(data)


def test_round_trip_normalises_and_keeps_unknown_keys(tmp_path: Path) -> None:
    item = spec(future_field={"x": 1})
    assert item.server_origin == "https://cremind.example.org" and item.gateway_device_id == DEVICE
    write_worker(tmp_path / "w1", item)
    loaded = load_worker(tmp_path / "w1")
    assert loaded == item and loaded.extra == {"future_field": {"x": 1}}
    assert json.loads((tmp_path / "w1" / "worker.json").read_text(encoding="utf-8"))["future_field"] == {"x": 1}


@pytest.mark.parametrize("bad", [
    {"gateway_device_id": "xyz"}, {"server_origin": "https://h/path"}, {"profile": ""}, {"enabled": "yes"},
    {"worker_id": "../evil"}, {"companion_id": None},
])
def test_invalid_specs(bad: dict[str, object]) -> None:
    with pytest.raises(WorkerDirError):
        spec(**bad)


def test_directory_name_must_match(tmp_path: Path) -> None:
    write_worker(tmp_path / "other", spec("w1"))
    with pytest.raises(WorkerDirError):
        load_worker(tmp_path / "other")


def test_list_workers_reports_broken_directories(tmp_path: Path) -> None:
    write_worker(tmp_path / "a", spec("a"))
    (tmp_path / "b").mkdir()
    (tmp_path / ".staging").mkdir()
    listed = dict((d.name, s) for d, s in list_workers(tmp_path))
    assert set(listed) == {"a", "b"}
    assert isinstance(listed["a"], WorkerSpec) and isinstance(listed["b"], WorkerDirError)
    assert list_workers(tmp_path / "missing") == []
