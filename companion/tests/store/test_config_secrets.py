"""Configuration file + overrides, and the secret store (file backend; the OS keyring is never touched)."""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from cremind_tag import secrets as secret_store
from cremind_tag.config import ConfigError, dumps_toml, load_config, register_section
from cremind_tag.protocol.session import derive_k_epoch


def test_defaults_and_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[hardware]\ngateway_url = "COM7"\njlink_tool = "nrfjprog"\n[paths]\ndata_dir = "~/ct"\n'
                    '[cremind]\nurl = "https://cremind.example"\n', encoding="utf-8")
    config = load_config(path, env={"CREMIND_TAG_GATEWAY_URL": "socket://127.0.0.1:7777",
                                    "CREMIND_TAG_SECRETS_BACKEND": "file"})
    assert config.hardware.gateway_url == "socket://127.0.0.1:7777"  # environment wins
    assert config.hardware.jlink_tool == "nrfjprog" and config.hardware.fontpack is None
    assert config.paths.data_dir == Path.home() / "ct"
    assert config.secrets.backend == "file"
    assert config.db_path == Path.home() / "ct" / "companion.sqlite3"
    assert config.raw_table("cremind") == {"url": "https://cremind.example"}


def test_edit_and_save_keeps_unknown_tables(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.toml"
    config = load_config(path, env={"CREMIND_TAG_BRIDGE_URL": "COM9"})
    config.set("hardware", "gateway_url", "COM7")
    config.set("hardware", "fontpack", tmp_path / "p.ctfp")
    config.set("other", "key", 'quote " and \\ back')
    config.save()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["hardware"] == {"gateway_url": "COM7", "fontpack": (tmp_path / "p.ctfp").as_posix()}
    assert "bridge_url" not in data["hardware"]  # environment overrides are never persisted
    assert data["other"]["key"] == 'quote " and \\ back'
    config.set("hardware", "gateway_url", None)
    config.save()
    assert "gateway_url" not in tomllib.loads(path.read_text(encoding="utf-8")).get("hardware", {})


def test_validation(tmp_path: Path) -> None:
    config = load_config(tmp_path / "none.toml", env={})
    with pytest.raises(ConfigError):
        config.set("hardware", "jlink_tool", "stlink")
    with pytest.raises(ConfigError):
        config.set("hardware", "nope", "x")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "none.toml", env={"CREMIND_TAG_SECRETS_BACKEND": "vault"})
    bad = tmp_path / "bad.toml"
    bad.write_text("[hardware\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad, env={})


def test_registered_section(tmp_path: Path) -> None:
    @register_section
    @dataclass
    class ServerSettings:
        SECTION: ClassVar[str] = "testserver"
        url: str = "https://localhost"
        poll_s: int = 25
        verify_tls: bool = True

    path = tmp_path / "c.toml"
    path.write_text("[testserver]\npoll_s = 10\n", encoding="utf-8")
    config = load_config(path, env={"CREMIND_TAG_TESTSERVER_VERIFY_TLS": "no"})
    settings = config.section(ServerSettings)
    assert (settings.url, settings.poll_s, settings.verify_tls) == ("https://localhost", 10, False)
    assert config.as_dict()["testserver"]["poll_s"] == 10


def test_toml_writer_round_trips() -> None:
    data = {"top": 1, "t": {"s": "a\tb\u0001", "b": True, "f": 1.5, "l": ["x", 2], "none": None}}
    parsed = tomllib.loads(dumps_toml(data))
    assert parsed == {"top": 1, "t": {"s": "a\tb\u0001", "b": True, "f": 1.5, "l": ["x", 2]}}


def test_file_secret_store(tmp_path: Path) -> None:
    store = secret_store.SecretStore.open(tmp_path, backend="file")
    assert store.backend_name == "file"
    secret = bytes(range(32))
    ref = store.set_tag_secret(0x1A2B3C4D, secret)
    assert ref == "file:tag:1A2B3C4D"
    assert store.get_tag_secret(0x1A2B3C4D, ref) == secret and store.has_tag_secret(0x1A2B3C4D)
    assert store.k_epoch(0x1A2B3C4D, 3) == derive_k_epoch(secret, 0x1A2B3C4D, 3)
    store.set_credential("tagc_abc", "s3cret")
    assert store.get_credential("tagc_abc") == "s3cret"
    path = tmp_path / secret_store.FILE_NAME
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # A second store instance sees the same file.
    again = secret_store.SecretStore.open(tmp_path, backend="file")
    assert again.get_tag_secret(0x1A2B3C4D) == secret
    assert again.delete_tag_secret(0x1A2B3C4D) and not again.delete_tag_secret(0x1A2B3C4D)
    with pytest.raises(secret_store.SecretNotFoundError):
        again.get_tag_secret(0x1A2B3C4D)
    with pytest.raises(secret_store.SecretStoreError, match="backend"):
        again.get_tag_secret(1, "keyring:tag:00000001")
    with pytest.raises(secret_store.SecretStoreError):
        again.set_tag_secret(1, b"short")


def test_secret_file_is_validated(tmp_path: Path) -> None:
    (tmp_path / secret_store.FILE_NAME).write_text('{"version": 9}', encoding="utf-8")
    store = secret_store.SecretStore.open(tmp_path, backend="file")
    with pytest.raises(secret_store.SecretStoreError):
        store.get_credential("x")


def test_auto_falls_back_to_the_file_without_a_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_store, "keyring_usable", lambda: (False, "no usable keyring backend"))
    store = secret_store.SecretStore.open(tmp_path)
    assert store.backend_name == "file"
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.SecretStore.open(tmp_path, backend="keyring")


def test_keyring_backend_with_a_fake_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 10  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self.data: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.data.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.data[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            if (service, username) not in self.data:
                raise keyring.errors.PasswordDeleteError(username)
            del self.data[(service, username)]

    memory = Memory()
    previous = keyring.get_keyring()
    keyring.set_keyring(memory)
    try:
        assert secret_store.keyring_usable()[0]
        store = secret_store.SecretStore.open(tmp_path)
        assert store.backend_name == "keyring"
        ref = store.set_tag_secret(5, bytes(32))
        assert ref == "keyring:tag:00000005" and ("cremind-tag", "tag:00000005") in memory.data
        assert store.delete_tag_secret(5) and not store.delete_tag_secret(5)
    finally:
        keyring.set_keyring(previous)
