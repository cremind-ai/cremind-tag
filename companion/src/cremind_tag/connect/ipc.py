"""Local IPC between ``cremind-connect`` processes (docs/connect-setup.md §11.3).

Transport: :mod:`multiprocessing.connection` over a named pipe on Windows
(``\\\\.\\pipe\\cremind-connect-<SHA-256(user SID)[:16]>``) or a Unix socket
``<runtime>/cremind-connect.sock`` in a ``0700`` directory elsewhere — no TCP
listener, no HTTP. Only this OS user can open either (the pipe's default DACL
gives other users read access at most, a two-way client needs write; the
socket's directory is private), and ``multiprocessing``'s first pipe instance
flag makes squatting the pipe name fail loudly.

Authentication: both sides prove they hold the 32-byte key in the owner-only
``ipc.key`` (the mutual HMAC challenge of ``multiprocessing.connection``, run
with a deadline so a stalled peer cannot hang anyone).

Messages: one JSON object per ``send_bytes`` — never pickle — with an ``op``
field; every answer is ``{"ok": true, ...}`` or ``{"ok": false, "error":
<code>, "message": <text>}``. A connection may carry several requests.
With ``CREMIND_CONNECT_HOME`` set, the pipe name also covers that directory, so
tests never meet a real service.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Connection, Listener, answer_challenge, deliver_challenge
from pathlib import Path
from typing import Any

from ..private_files import InterProcessLock, replace_with_retry, restrict_to_owner
from .paths import ConnectPaths, ensure_private_dir

log = logging.getLogger(__name__)

AUTHKEY_LEN = 32
MAX_MESSAGE = 1 << 20
HANDSHAKE_TIMEOUT_S = 5.0
IDLE_TIMEOUT_S = 30.0

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class IpcError(RuntimeError):
    """An IPC request failed."""


class IpcUnavailable(IpcError):
    """No service is listening (or it never ran: no ``ipc.key``)."""


class IpcTimeout(IpcError):
    """The service did not answer in time."""


class IpcAuthError(IpcError):
    """The peer does not hold ``ipc.key``."""


# ---------------------------------------------------------------------------
# Address and key
# ---------------------------------------------------------------------------


def _user_identity() -> str:
    if sys.platform == "win32":
        from ..private_files import current_user_sid

        return current_user_sid()
    return str(os.getuid())


def pipe_name(paths: ConnectPaths) -> str:
    """``\\\\.\\pipe\\cremind-connect-<16 hex>`` for this user (and override directory)."""
    identity = _user_identity()
    if paths.overridden:
        identity += "|" + str(paths.data_dir)
    return r"\\.\pipe\cremind-connect-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def address(paths: ConnectPaths) -> tuple[str, str]:
    """``(address, family)`` of the service's listener."""
    if sys.platform == "win32":
        return pipe_name(paths), "AF_PIPE"
    return str(paths.socket_path), "AF_UNIX"


def load_authkey(paths: ConnectPaths) -> bytes | None:
    """The key from ``ipc.key`` (``None`` when missing or malformed)."""
    try:
        key = bytes.fromhex(paths.ipc_key.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return key if len(key) == AUTHKEY_LEN else None


def ensure_authkey(paths: ConnectPaths) -> bytes:
    """The key, created (owner-only) when missing."""
    key = load_authkey(paths)
    if key is not None:
        return key
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with InterProcessLock(paths.data_dir / "ipc.key.lock").held():
        key = load_authkey(paths)
        if key is not None:
            return key
        key = os.urandom(AUTHKEY_LEN)
        tmp = paths.ipc_key.with_name(f".ipc.key.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        try:
            try:
                restrict_to_owner(tmp)
                os.write(fd, key.hex().encode("ascii") + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            replace_with_retry(tmp, paths.ipc_key)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        return key


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------


class _Deadline:
    """A connection whose ``recv_bytes`` gives up after ``timeout`` (for the challenge functions)."""

    def __init__(self, conn: Connection, timeout: float) -> None:
        self._conn = conn
        self._timeout = timeout

    def send_bytes(self, data: bytes) -> None:
        self._conn.send_bytes(data)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        if not self._conn.poll(self._timeout):
            raise AuthenticationError("no answer to the authentication challenge")
        return self._conn.recv_bytes(maxlength)


def _encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _decode(data: bytes) -> dict[str, Any]:
    message = json.loads(data.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("an IPC message is a JSON object")
    return message


def error(error_code: str, message: str, /, **extra: Any) -> dict[str, Any]:
    """An error answer."""
    return {"ok": False, "error": error_code, "message": message, **extra}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class IpcServer:
    """Accepts local connections and answers each request with ``handler(request)``.

    ``handler`` runs on a per-connection thread and must be thread-safe; an
    exception becomes ``{"ok": false, "error": "internal"}``.
    """

    def __init__(self, paths: ConnectPaths, handler: Handler, *, authkey: bytes | None = None,
                 max_clients: int = 16, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
        self.paths = paths
        self.handler = handler
        self.authkey = authkey
        self.max_clients = max_clients
        self.idle_timeout = idle_timeout
        self.address, self.family = address(paths)
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._clients = threading.BoundedSemaphore(max_clients)

    def start(self) -> None:
        """Listen (the caller holds the instance lock, so a leftover socket file is stale)."""
        if self.authkey is None:
            self.authkey = ensure_authkey(self.paths)
        if self.family == "AF_UNIX":
            ensure_private_dir(Path(self.address).parent)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.address)
        self._listener = Listener(self.address, self.family, backlog=16)
        if self.family == "AF_UNIX":
            os.chmod(self.address, 0o600)
        self._thread = threading.Thread(target=self._accept_loop, name="ipc accept", daemon=True)
        self._thread.start()
        log.info("ipc: listening on %s", self.address)

    def stop(self) -> None:
        if self._listener is None or self._stopping.is_set():
            return
        self._stopping.set()
        with contextlib.suppress(Exception):  # wake the blocked accept()
            Client(self.address, self.family).close()
        if self._thread is not None:
            self._thread.join(5.0)
        with contextlib.suppress(Exception):
            self._listener.close()
        if self.family == "AF_UNIX":
            with contextlib.suppress(OSError):
                os.unlink(self.address)
        log.info("ipc: stopped")

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                conn = self._listener.accept()
            except OSError as exc:
                if self._stopping.is_set():
                    break
                log.warning("ipc: accept failed: %s", exc)
                continue
            if self._stopping.is_set():
                conn.close()
                break
            if not self._clients.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), name="ipc client", daemon=True).start()

    def _serve(self, conn: Connection) -> None:
        try:
            with conn:
                assert self.authkey is not None
                deliver_challenge(_Deadline(conn, HANDSHAKE_TIMEOUT_S), self.authkey)  # type: ignore[arg-type]
                answer_challenge(_Deadline(conn, HANDSHAKE_TIMEOUT_S), self.authkey)  # type: ignore[arg-type]
                while not self._stopping.is_set() and conn.poll(self.idle_timeout):
                    data = conn.recv_bytes(MAX_MESSAGE)
                    conn.send_bytes(_encode(self._answer(data)))
        except AuthenticationError as exc:
            log.warning("ipc: refused a client: %s", exc)
        except (EOFError, OSError):
            pass
        finally:
            self._clients.release()

    def _answer(self, data: bytes) -> dict[str, Any]:
        try:
            request = _decode(data)
        except ValueError as exc:
            return error("bad_request", f"not a JSON object: {exc}")
        if not isinstance(request.get("op"), str):
            return error("bad_request", "the request has no op")
        try:
            answer = self.handler(request)
        except Exception as exc:
            log.exception("ipc: %s failed", request.get("op"))
            return error("internal", f"{type(exc).__name__}: {exc}")
        return answer if isinstance(answer, dict) else error("internal", "the handler returned no object")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def connect(paths: ConnectPaths, *, timeout: float = HANDSHAKE_TIMEOUT_S) -> Connection:
    """An authenticated connection to the service."""
    key = load_authkey(paths)
    if key is None:
        raise IpcUnavailable("Cremind Connect has not run for this user yet (no ipc.key)")
    addr, family = address(paths)
    try:
        conn = Client(addr, family)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise IpcUnavailable(f"the Cremind Connect service is not running ({exc})") from None
    except OSError as exc:
        raise IpcUnavailable(f"cannot reach the Cremind Connect service: {exc}") from None
    try:
        answer_challenge(_Deadline(conn, timeout), key)  # type: ignore[arg-type]
        deliver_challenge(_Deadline(conn, timeout), key)  # type: ignore[arg-type]
    except AuthenticationError as exc:
        conn.close()
        raise IpcAuthError(f"the service did not accept this program's key: {exc}") from None
    except (EOFError, OSError) as exc:
        conn.close()
        raise IpcUnavailable(f"the service closed the connection: {exc}") from None
    return conn


def call(conn: Connection, op: str, *, timeout: float = 5.0, **fields: Any) -> dict[str, Any]:
    """One request on an open connection."""
    try:
        conn.send_bytes(_encode({"op": op, **fields}))
        if not conn.poll(timeout):
            raise IpcTimeout(f"no answer to {op} within {timeout:g} s")
        return _decode(conn.recv_bytes(MAX_MESSAGE))
    except (EOFError, OSError) as exc:
        raise IpcUnavailable(f"the service closed the connection: {exc}") from None
    except ValueError as exc:
        raise IpcError(f"malformed answer to {op}: {exc}") from None


def request(paths: ConnectPaths, op: str, *, timeout: float = 5.0, **fields: Any) -> dict[str, Any]:
    """Connect, send ``{"op": op, **fields}``, return the answer (raises :class:`IpcError`)."""
    conn = connect(paths, timeout=min(timeout, HANDSHAKE_TIMEOUT_S))
    with conn:
        return call(conn, op, timeout=timeout, **fields)


def ping(paths: ConnectPaths, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """The service's ``ping`` answer, or ``None`` when it does not answer."""
    try:
        answer = request(paths, "ping", timeout=timeout)
    except IpcError:
        return None
    return answer if answer.get("ok") else None


__all__ = ["AUTHKEY_LEN", "IpcAuthError", "IpcError", "IpcServer", "IpcTimeout", "IpcUnavailable", "address",
           "call", "connect", "ensure_authkey", "error", "load_authkey", "pipe_name", "ping", "request"]
