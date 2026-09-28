"""Worker directories: ``<data>/workers/<worker_id>/`` (docs/connect-setup.md §11.2, §11.6).

A worker is one isolated companion daemon for exactly one *(Cremind server,
profile, gateway)*. Its directory holds ``worker.json`` (below; the supervisor
reads only this file), ``controller.key``, ``config.toml``, ``companion.sqlite3``,
``secrets.json`` and ``logs/`` — written by the setup flow and the worker.

``worker.json``::

    {"worker_id": "…", "server_origin": "https://cremind.example.org", "profile": "Anna",
     "companion_id": "…", "gateway_device_id": "<32 hex>", "enabled": true}

Unknown keys are preserved when the file is rewritten. ``enabled: false``
tells the supervisor to leave the worker stopped (Pause / Remove in Cremind,
or a worker that disabled itself).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .links import LinkError, validate_origin

WORKER_FILE = "worker.json"
_DEVICE_ID = re.compile(r"[0-9a-f]{32}")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_KNOWN = ("worker_id", "server_origin", "profile", "companion_id", "gateway_device_id", "enabled")


class WorkerDirError(ValueError):
    """A worker directory without a usable ``worker.json``."""


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    server_origin: str
    profile: str
    companion_id: str
    gateway_device_id: str
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_json(cls, data: Any, *, where: str = WORKER_FILE) -> WorkerSpec:
        if not isinstance(data, dict):
            raise WorkerDirError(f"{where}: not a JSON object")
        values: dict[str, Any] = {}
        for key in ("worker_id", "server_origin", "profile", "companion_id", "gateway_device_id"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise WorkerDirError(f"{where}: {key} is missing")
            values[key] = value
        if not _WORKER_ID.fullmatch(values["worker_id"]):
            raise WorkerDirError(f"{where}: worker_id {values['worker_id']!r} is not a plain name")
        device_id = values["gateway_device_id"].lower()
        if not _DEVICE_ID.fullmatch(device_id):
            raise WorkerDirError(f"{where}: gateway_device_id must be 32 hex characters")
        try:
            origin = validate_origin(values["server_origin"])
        except LinkError as exc:
            raise WorkerDirError(f"{where}: server_origin: {exc}") from None
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise WorkerDirError(f"{where}: enabled must be true or false")
        extra = {k: v for k, v in data.items() if k not in _KNOWN}
        return cls(values["worker_id"], origin, values["profile"], values["companion_id"], device_id, enabled, extra)

    def as_json(self) -> dict[str, Any]:
        return {**self.extra, "worker_id": self.worker_id, "server_origin": self.server_origin,
                "profile": self.profile, "companion_id": self.companion_id,
                "gateway_device_id": self.gateway_device_id, "enabled": self.enabled}


def load_worker(directory: Path) -> WorkerSpec:
    """The spec in ``<directory>/worker.json``; its ``worker_id`` must be the directory's name."""
    path = Path(directory) / WORKER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorkerDirError(f"{path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise WorkerDirError(f"{path}: {exc}") from None
    spec = WorkerSpec.from_json(data, where=str(path))
    if spec.worker_id != Path(directory).name:
        raise WorkerDirError(f"{path}: worker_id {spec.worker_id!r} does not match its directory")
    return spec


def write_worker(directory: Path, spec: WorkerSpec) -> Path:
    """Write ``worker.json`` atomically."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / WORKER_FILE
    tmp = directory / f".{WORKER_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(spec.as_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def list_workers(workers_dir: Path) -> list[tuple[Path, WorkerSpec | WorkerDirError]]:
    """Every worker directory with its spec, or the error that makes it unusable (sorted by name)."""
    out: list[tuple[Path, WorkerSpec | WorkerDirError]] = []
    try:
        entries = sorted(p for p in Path(workers_dir).iterdir() if p.is_dir() and not p.name.startswith("."))
    except FileNotFoundError:
        return out
    for directory in entries:
        try:
            out.append((directory, load_worker(directory)))
        except WorkerDirError as exc:
            out.append((directory, exc))
    return out


__all__ = ["WORKER_FILE", "WorkerDirError", "WorkerSpec", "list_workers", "load_worker", "write_worker"]
