"""Secret storage for tag secrets and Cremind connector credentials.

docs/security.md: a tag's 32-byte enrollment secret lives only in the tag's
UICR and here; it is never sent to Cremind or to a bridge (bridges receive
``K_epoch``, derived on demand by :meth:`SecretStore.k_epoch`). Connector
credential secrets are kept here too.

Backends, chosen once when the store is opened:

- ``keyring`` — the OS credential store through ``keyring`` (Windows
  Credential Manager, macOS Keychain, Secret Service), service name
  ``cremind-tag``;
- ``file`` — a JSON file (``<data dir>/secrets.json``) only its owner can
  access (mode 0600; on Windows a protected DACL for the current user and
  SYSTEM, since Windows ignores the mode and a file inherits its folder's ACL),
  replaced atomically under an inter-process lock; used when no usable keyring
  backend exists (headless Linux, containers) or when configured explicitly.

The backend in use is logged; secret values never are. A reference such as
``keyring:tag:1A2B3C4D`` is what other components (the inventory database)
store instead of the secret.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .protocol.ids import TAG_SECRET_LEN
from .protocol.session import derive_k_epoch

log = logging.getLogger(__name__)

SERVICE = "cremind-tag"
FILE_NAME = "secrets.json"
_FILE_VERSION = 1


class SecretStoreError(RuntimeError):
    """The secret store is unusable or a secret is missing/corrupt."""


class SecretNotFoundError(SecretStoreError):
    """No secret under that key."""


class Backend(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> bool: ...

    def describe(self) -> str: ...


class KeyringBackend:
    """``keyring`` with service ``cremind-tag`` and the key as user name."""

    name = "keyring"

    def __init__(self, service: str = SERVICE) -> None:
        import keyring

        self._keyring = keyring
        self.service = service

    def get(self, key: str) -> str | None:
        try:
            return self._keyring.get_password(self.service, key)
        except self._keyring.errors.KeyringError as exc:
            raise SecretStoreError(f"keyring read failed for {key}: {exc}") from None

    def set(self, key: str, value: str) -> None:
        try:
            self._keyring.set_password(self.service, key, value)
        except self._keyring.errors.KeyringError as exc:
            raise SecretStoreError(f"keyring write failed for {key}: {exc}") from None

    def delete(self, key: str) -> bool:
        try:
            self._keyring.delete_password(self.service, key)
        except self._keyring.errors.PasswordDeleteError:
            return False
        except self._keyring.errors.KeyringError as exc:
            raise SecretStoreError(f"keyring delete failed for {key}: {exc}") from None
        return True

    def describe(self) -> str:
        backend = self._keyring.get_keyring()
        return f"keyring ({type(backend).__module__}.{type(backend).__name__}, service {self.service!r})"


class FileBackend:
    """An owner-only JSON file ``{"version": 1, "secrets": {key: value}}``, replaced atomically.

    Several processes may use the file at once (two ``tag enroll`` runs, the
    daemon and the CLI): every read-modify-write cycle holds an inter-process
    lock on ``secrets.json.lock``, and each write goes to its own temporary file
    (owner-only before any secret is written) that then replaces the file. On
    Windows "owner-only" is a protected DACL for the current user and SYSTEM
    (:mod:`cremind_tag.private_files`); elsewhere mode 0600.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        from .private_files import InterProcessLock

        self.path = Path(path)
        self._lock = threading.Lock()
        self._process_lock = InterProcessLock(self.path.with_name(self.path.name + ".lock"))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock, self._process_lock.held():
            yield

    def ensure_private(self) -> str | None:
        """Make an existing file owner-only again (e.g. created by an older version); returns what was wrong."""
        from .private_files import access_problem, restrict_to_owner

        with self._locked():
            problem = access_problem(self.path)
            if problem is not None:
                restrict_to_owner(self.path)
                log.warning("secrets: %s was not private (%s); access is now restricted to its owner", self.path,
                            problem)
        return problem

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SecretStoreError(f"cannot read {self.path}: {exc}") from None
        valid = isinstance(data, dict) and data.get("version") == _FILE_VERSION
        if not valid or not isinstance(data.get("secrets"), dict):
            raise SecretStoreError(f"{self.path} is not a cremind-tag secrets file")
        return {str(k): str(v) for k, v in data["secrets"].items()}

    def _store(self, secrets: dict[str, str]) -> None:
        from .private_files import replace_with_retry, restrict_to_owner

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": _FILE_VERSION, "secrets": secrets}, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            try:
                restrict_to_owner(tmp)  # before a single secret byte is in it
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            replace_with_retry(tmp, self.path)  # the owner-only DACL moves with the file
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def get(self, key: str) -> str | None:
        with self._locked():
            return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        with self._locked():
            secrets = self._load()
            secrets[key] = value
            self._store(secrets)

    def delete(self, key: str) -> bool:
        with self._locked():
            secrets = self._load()
            if key not in secrets:
                return False
            del secrets[key]
            self._store(secrets)
            return True

    def describe(self) -> str:
        from .private_files import protection_label

        return f"file {self.path} ({protection_label()})"


def keyring_usable() -> tuple[bool, str]:
    """Whether ``keyring`` has a real backend (not the fail/null/plaintext ones)."""
    try:
        import keyring
        from keyring.backends import chainer, fail
    except Exception as exc:  # pragma: no cover - keyring is a dependency
        return False, f"keyring unavailable: {exc}"
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # pragma: no cover - broken environment
        return False, f"keyring backend error: {exc}"
    module = type(backend).__module__
    if isinstance(backend, fail.Keyring) or module.startswith("keyring.backends.null"):
        return False, f"no usable keyring backend ({module})"
    if isinstance(backend, chainer.ChainerBackend) and not backend.backends:
        return False, "keyring chainer has no backends"
    if module.startswith("keyrings.alt"):  # plaintext/obfuscated file backends: no better than ours
        return False, f"keyring backend {module} is not an OS credential store"
    priority = getattr(backend, "priority", 1)
    if priority is not None and priority <= 0:
        return False, f"keyring backend {module} has priority {priority}"
    return True, module


def tag_key(tag_id: int) -> str:
    return f"tag:{tag_id:08X}"


TAG_ROOT_PREFIX = "tagroot:"
V2_KEY_LEN = 32


def tag_root_key(tag_id: int) -> str:
    return f"{TAG_ROOT_PREFIX}{tag_id:08X}"


def _key_bytes(value: str, what: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise SecretStoreError(f"the {what} is corrupt") from None
    if len(raw) != V2_KEY_LEN:
        raise SecretStoreError(f"the {what} has the wrong length")
    return raw


def credential_key(name: str) -> str:
    return f"credential:{name}"


class SecretStore:
    """Tag secrets and connector credentials on one backend."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    @classmethod
    def open(cls, data_dir: Path, backend: str = "auto") -> SecretStore:
        """Pick the backend: ``keyring`` when usable (or forced), else the owner-only file."""
        if backend not in ("auto", "keyring", "file"):
            raise SecretStoreError(f"unknown secrets backend {backend!r}")
        chosen: Backend
        if backend == "file":
            chosen = FileBackend(Path(data_dir) / FILE_NAME)
        else:
            usable, why = keyring_usable()
            if usable:
                chosen = KeyringBackend()
            elif backend == "keyring":
                raise SecretStoreError(f"keyring backend requested but {why}")
            else:
                log.warning("secrets: %s; falling back to an owner-only file", why)
                chosen = FileBackend(Path(data_dir) / FILE_NAME)
        if isinstance(chosen, FileBackend):
            chosen.ensure_private()
        log.info("secrets: using %s", chosen.describe())
        return cls(chosen)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def describe(self) -> str:
        return self.backend.describe()

    def ref(self, key: str) -> str:
        return f"{self.backend.name}:{key}"

    def _check_ref(self, ref: str) -> str:
        backend, sep, key = ref.partition(":")
        if not sep or not key:
            raise SecretStoreError(f"malformed secret reference {ref!r}")
        if backend != self.backend.name:
            raise SecretStoreError(
                f"secret {key} is stored in the {backend!r} backend but this store uses {self.backend.name!r}"
                " (set [secrets] backend accordingly)")
        return key

    # -- tag secrets ----------------------------------------------------------

    def set_tag_secret(self, tag_id: int, secret: bytes) -> str:
        """Store a tag secret; returns the reference to keep in the inventory."""
        if len(secret) != TAG_SECRET_LEN:
            raise SecretStoreError(f"tag secret must be {TAG_SECRET_LEN} bytes")
        key = tag_key(tag_id)
        self.backend.set(key, secret.hex())
        log.info("secrets: stored the secret of tag %08X", tag_id)
        return self.ref(key)

    def get_tag_secret(self, tag_id: int, ref: str | None = None) -> bytes:
        key = self._check_ref(ref) if ref is not None else tag_key(tag_id)
        value = self.backend.get(key)
        if value is None:
            raise SecretNotFoundError(f"no secret for tag {tag_id:08X} in {self.backend.describe()}")
        try:
            secret = bytes.fromhex(value)
        except ValueError:
            raise SecretStoreError(f"secret of tag {tag_id:08X} is corrupt") from None
        if len(secret) != TAG_SECRET_LEN:
            raise SecretStoreError(f"secret of tag {tag_id:08X} has the wrong length")
        return secret

    def has_tag_secret(self, tag_id: int) -> bool:
        return self.backend.get(tag_key(tag_id)) is not None

    def delete_tag_secret(self, tag_id: int) -> bool:
        return self.backend.delete(tag_key(tag_id))

    def k_epoch(self, tag_id: int, epoch: int, ref: str | None = None) -> bytes:
        """``K_epoch`` for ``(tag, epoch)`` (docs/protocol.md §5.4); never persisted. A v2 tag's reference
        names its operational root, and the key is ``K_epoch`` v2 (docs/connect-setup.md §3.5)."""
        if ref is not None and self._check_ref(ref).startswith(TAG_ROOT_PREFIX):
            from .secure.identity import k_epoch_v2

            return k_epoch_v2(self.get_tag_root(tag_id, ref), tag_id, epoch)
        return derive_k_epoch(self.get_tag_secret(tag_id, ref), tag_id, epoch)

    # -- v2 keys (docs/connect-setup.md §3.5) -------------------------------------------

    def set_tag_root(self, tag_id: int, root: bytes) -> str:
        """Store a v2 tag's operational root (32 bytes); returns the reference kept as its ``secret_ref``."""
        if len(root) != V2_KEY_LEN:
            raise SecretStoreError(f"a tag root must be {V2_KEY_LEN} bytes")
        key = tag_root_key(tag_id)
        self.backend.set(key, root.hex())
        log.info("secrets: stored the root of tag %08X", tag_id)
        return self.ref(key)

    def get_tag_root(self, tag_id: int, ref: str | None = None) -> bytes:
        key = self._check_ref(ref) if ref is not None else tag_root_key(tag_id)
        value = self.backend.get(key)
        if value is None:
            raise SecretNotFoundError(f"no root for tag {tag_id:08X} in {self.backend.describe()}")
        return _key_bytes(value, f"root of tag {tag_id:08X}")

    def delete_tag_root(self, tag_id: int) -> bool:
        return self.backend.delete(tag_root_key(tag_id))

    def set_key(self, name: str, value: bytes) -> str:
        """A named 32-byte key (``mk:<device_id>``, ``staged:<…>``); returns its reference."""
        if len(value) != V2_KEY_LEN:
            raise SecretStoreError(f"key {name} must be {V2_KEY_LEN} bytes")
        key = f"key:{name}"
        self.backend.set(key, value.hex())
        return self.ref(key)

    def get_key(self, name: str) -> bytes | None:
        value = self.backend.get(f"key:{name}")
        return None if value is None else _key_bytes(value, f"key {name}")

    def delete_key(self, name: str) -> bool:
        return self.backend.delete(f"key:{name}")

    # -- connector credentials -------------------------------------------------

    def set_credential(self, name: str, value: str) -> str:
        key = credential_key(name)
        self.backend.set(key, value)
        log.info("secrets: stored credential %s", name)
        return self.ref(key)

    def get_credential(self, name: str) -> str | None:
        return self.backend.get(credential_key(name))

    def delete_credential(self, name: str) -> bool:
        return self.backend.delete(credential_key(name))
