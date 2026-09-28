"""The installation identity: Connect's Ed25519 key for this OS user (docs/connect-setup.md §1, §9.2).

One key per OS user, shared by every worker of the installation. It signs setup
requests (the bootstrap API's ``proof``), never ownership changes. Two files in
the data directory:

- ``installation.key`` — ``{"schema": ..., "private_key": "<64 hex>"}``,
  owner-only (mode ``0600``; on Windows a protected DACL for the user and
  SYSTEM, :mod:`cremind_tag.private_files`), written through an owner-only
  temporary file so the secret is never readable by anyone else, not even
  briefly;
- ``installation.json`` — the public part: ``{"id", "public_key", "created_at"}``
  with ``id = SHA-256(public_key)[0:16]`` in hex (32 characters).

:func:`load_or_create` runs under an inter-process lock, so the service and a
setup window starting at the same time agree on one key.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import platform as _platform
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..private_files import InterProcessLock, access_problem, replace_with_retry, restrict_to_owner
from ..secure.identity import ed25519_generate, ed25519_public, ed25519_sign, ed25519_verify
from .paths import ConnectPaths
from .runtime import os_kind

log = logging.getLogger(__name__)

KEY_SCHEMA = "cremind-connect/installation-key@1"
INFO_SCHEMA = "cremind-connect/installation@1"


class InstallationError(RuntimeError):
    """The installation identity is unreadable or inconsistent."""


def installation_id(public_key: bytes) -> str:
    """``SHA-256(public_key)[0:16]`` as 32 lower-case hex characters."""
    return hashlib.sha256(public_key).digest()[:16].hex()


@dataclass(frozen=True)
class Installation:
    """The identity (the private key stays inside; ``repr`` never shows it)."""

    id: str
    public_key: bytes
    created_at: str
    _private_key: bytes

    def __repr__(self) -> str:
        return f"Installation(id={self.id!r}, created_at={self.created_at!r})"

    def sign(self, message: bytes) -> bytes:
        """Ed25519 signature (64 bytes) over ``message``."""
        return ed25519_sign(self._private_key, message)

    def verify(self, signature: bytes, message: bytes) -> bool:
        return ed25519_verify(self.public_key, signature, message)

    def as_json(self) -> dict[str, str]:
        """The public part (``installation.json``)."""
        return {"schema": INFO_SCHEMA, "id": self.id, "public_key": self.public_key.hex(),
                "created_at": self.created_at}


def _write_private(path: Path, text: str) -> None:
    """Atomically write an owner-only file: restricted before a single secret byte is in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        try:
            restrict_to_owner(tmp)
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_public(path: Path, data: dict[str, str]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    replace_with_retry(tmp, path)


def _read_key(path: Path) -> bytes:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != KEY_SCHEMA:
            raise ValueError(f"schema is not {KEY_SCHEMA}")
        key = bytes.fromhex(doc["private_key"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InstallationError(f"{path} is not a usable installation key ({exc}); move it away to create a new "
                                "installation identity") from None
    if len(key) != 32:
        raise InstallationError(f"{path}: the private key must be 32 bytes")
    return key


def _created_at(paths: ConnectPaths, public: bytes) -> str | None:
    """``created_at`` from ``installation.json`` when that file describes this key."""
    try:
        info = json.loads(paths.installation_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("public_key") != public.hex() or not info.get("created_at"):
        return None
    return str(info["created_at"])


def load(paths: ConnectPaths) -> Installation | None:
    """The existing identity, or ``None`` when this user has none yet (read-only)."""
    if not paths.installation_key.is_file():
        return None
    private = _read_key(paths.installation_key)
    public = ed25519_public(private)
    return Installation(installation_id(public), public, _created_at(paths, public) or "", private)


def load_or_create(paths: ConnectPaths) -> Installation:
    """This user's identity, created on first use (see the module docstring)."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with InterProcessLock(paths.data_dir / "installation.lock").held():
        key_path = paths.installation_key
        if key_path.is_file():
            problem = access_problem(key_path)
            if problem is not None:
                restrict_to_owner(key_path)
                log.warning("installation: %s was not private (%s); access is now restricted to its owner",
                            key_path, problem)
            private = _read_key(key_path)
            public = ed25519_public(private)
            created = _created_at(paths, public)
            existing = Installation(installation_id(public), public, created or _now(), private)
            if created is None:  # installation.json is derived data: (re)write it from the key
                _write_public(paths.installation_json, existing.as_json())
            return existing
        private, public = ed25519_generate()
        created = Installation(installation_id(public), public, _now(), private)
        _write_private(key_path, json.dumps({"schema": KEY_SCHEMA, "private_key": private.hex()}) + "\n")
        _write_public(paths.installation_json, created.as_json())
        log.info("installation: created identity %s", created.id)
        return created


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def platform_name() -> str:
    """``windows``, ``macos`` or ``linux`` (what the bootstrap API's ``platform`` field carries)."""
    return os_kind()


def computer_name() -> str:
    """A name a person recognises for this computer ("Anna's MacBook Air", ``DESKTOP-4F2K``)."""
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
    if sys.platform == "win32" and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    name = _platform.node() or socket.gethostname() or "this computer"
    return name.removesuffix(".local")


__all__ = ["INFO_SCHEMA", "KEY_SCHEMA", "Installation", "InstallationError", "computer_name", "installation_id",
           "load", "load_or_create", "platform_name"]
