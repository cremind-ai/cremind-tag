"""The installation identity (Ed25519 key, owner-only)."""

from __future__ import annotations

import hashlib
import json
import sys
import threading

import pytest

from cremind_tag.connect import installation
from cremind_tag.connect.paths import ConnectPaths
from cremind_tag.private_files import access_problem
from cremind_tag.secure.identity import ed25519_verify


def test_created_once_then_loaded(paths: ConnectPaths) -> None:
    assert installation.load(paths) is None
    first = installation.load_or_create(paths)
    assert first.id == hashlib.sha256(first.public_key).digest()[:16].hex() and len(first.id) == 32
    assert len(first.public_key) == 32 and first.created_at.endswith("Z")
    again = installation.load_or_create(paths)
    assert again.id == first.id and again.created_at == first.created_at
    info = json.loads(paths.installation_json.read_text(encoding="utf-8"))
    assert info == {"schema": installation.INFO_SCHEMA, "id": first.id, "public_key": first.public_key.hex(),
                    "created_at": first.created_at}
    loaded = installation.load(paths)
    assert loaded is not None and loaded.id == first.id


def test_signatures_verify(paths: ConnectPaths) -> None:
    ident = installation.load_or_create(paths)
    message = b"cremind-connect/v1/bind\x00session"
    signature = ident.sign(message)
    assert len(signature) == 64 and ed25519_verify(ident.public_key, signature, message)
    assert ident.verify(signature, message) and not ident.verify(signature, message + b"!")


def test_the_key_file_is_owner_only_and_never_shown(paths: ConnectPaths) -> None:
    ident = installation.load_or_create(paths)
    assert access_problem(paths.installation_key) is None
    private_hex = json.loads(paths.installation_key.read_text(encoding="utf-8"))["private_key"]
    assert private_hex not in repr(ident)
    assert private_hex not in paths.installation_json.read_text(encoding="utf-8")


def test_a_lost_public_file_is_rewritten_from_the_key(paths: ConnectPaths) -> None:
    ident = installation.load_or_create(paths)
    paths.installation_json.unlink()
    again = installation.load_or_create(paths)
    assert again.id == ident.id and paths.installation_json.is_file()
    paths.installation_json.write_text(json.dumps({"public_key": "00" * 32, "created_at": "x"}), encoding="utf-8")
    assert installation.load_or_create(paths).id == ident.id
    assert json.loads(paths.installation_json.read_text(encoding="utf-8"))["public_key"] == ident.public_key.hex()


def test_a_corrupt_key_is_an_error_not_a_silent_new_identity(paths: ConnectPaths) -> None:
    installation.load_or_create(paths)
    paths.installation_key.write_text("{not json", encoding="utf-8")
    with pytest.raises(installation.InstallationError):
        installation.load_or_create(paths)


def test_concurrent_first_use_agrees_on_one_key(paths: ConnectPaths) -> None:
    ids: list[str] = []
    threads = [threading.Thread(target=lambda: ids.append(installation.load_or_create(paths).id)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1


def test_platform_and_computer_names() -> None:
    expected = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    assert installation.platform_name() == expected
    assert installation.computer_name()
