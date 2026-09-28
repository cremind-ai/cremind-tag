"""One Cremind Connect worker: ``cremind-connect worker --dir <dir> --port <device>`` (docs/connect-setup.md §11.6).

The service starts one worker per worker directory whose gateway is attached,
with that gateway's port. The worker is the companion daemon for exactly one
*(Cremind server, profile, gateway)*:

- its gateway link is a protocol v2 secure session with the worker's
  controller key, pinned to the gateway's ``device_id`` and identity key (a
  different device on the port is refused before anything is sent);
- its connector credentials, controller key and device keys live in the
  directory's owner-only files; the database is ``companion.sqlite3``;
- fonts are the verified, shared font asset bundle (never built here);
- the agent (:mod:`.agent`) holds the lease and runs Cremind's operations.

It stops when its stdin closes (the service asks it to), on SIGTERM/SIGINT, or
when Cremind removes it — then it disables its directory (``enabled: false``)
and deletes its keys, so the service does not start it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..connector.client import ConnectorClient, parse_credential
from ..protocol.ids import NodeRole
from .paths import ConnectPaths, default_paths
from .workerdir import WorkerDirError, WorkerSpec, load_worker, write_worker

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
SUPERVISED_ENV = "CREMIND_CONNECT_SUPERVISED"
DB_FILE = "companion.sqlite3"
SECRETS_FILE = "secrets.json"


class WorkerConfigError(RuntimeError):
    """The worker directory cannot run (missing key, credential, or a malformed ``worker.json``)."""


def load_identity(spec: WorkerSpec) -> Any:
    from .agent import WorkerIdentity

    extra = spec.extra
    try:
        return WorkerIdentity(companion_id=spec.companion_id, profile_id=str(extra["profile_id"]),
                              authority_pub=bytes.fromhex(str(extra["authority_pub"])),
                              gateway_device_id=bytes.fromhex(spec.gateway_device_id),
                              gateway_ik=bytes.fromhex(str(extra["gateway_ik"])),
                              operation=str(extra.get("operation") or "connect_gateway"))
    except (KeyError, ValueError) as exc:
        raise WorkerConfigError(f"worker.json lacks {exc}") from None


def build(directory: Path, port: str, paths: ConnectPaths, *, transport: Any = None,
          fonts: Any = None, gateway_options: dict[str, Any] | None = None) -> tuple[Any, Any]:
    """The daemon and its agent for ``directory`` (not started)."""
    from ..daemon.service import DaemonOptions, DaemonService
    from ..gateway.link import SecureOptions
    from ..resources import find_font_assets
    from ..secrets import FileBackend, SecretStore
    from .agent import ConnectAgent
    from .setup_flow import read_controller_key

    spec = load_worker(directory)
    ident = load_identity(spec)
    try:
        controller = read_controller_key(directory)
    except (OSError, ValueError, KeyError) as exc:
        raise WorkerConfigError(f"the controller key is unusable: {exc}") from None
    store = SecretStore(FileBackend(directory / SECRETS_FILE))
    store.backend.ensure_private()  # type: ignore[attr-defined]
    credentials = []
    for name in ("hardware", "content"):
        value = store.get_credential(name)
        if value is None:
            raise WorkerConfigError(f"the {name} credential is missing from {directory / SECRETS_FILE}")
        credentials.append(parse_credential(value))
    ca = spec.extra.get("ca_file")
    ca_file = directory / str(ca) if ca else None
    pack = cache = None
    if fonts is None:
        assets = find_font_assets(roots=None)
        if assets is not None:
            pack, cache = assets.pack_path, assets.cache_dir
        else:
            log.warning("worker: no verified font pack is installed; screens are held until one is")
    secure = SecureOptions(controller, expect_device_id=ident.gateway_device_id, expect_ik=ident.gateway_ik,
                           role=NodeRole.GATEWAY)
    options = DaemonOptions(
        db_path=directory / DB_FILE, data_dir=directory, cremind_url=spec.server_origin, ca_file=ca_file,
        hardware_credential=credentials[0], content_credentials=[credentials[1]], gateway_url=port,
        fontpack=pack, font_cache=cache, fonts=fonts, secrets=store, transport=transport,
        gateway_options={"secure": secure, **(gateway_options or {})}, gateway_hw_id=ident.gateway_hw_id)
    svc = DaemonService(options)
    client = ConnectorClient(spec.server_origin, credentials[0], ca_file=ca_file, transport=transport)
    agent = ConnectAgent(svc, ident, controller, client, directory)

    async def removed() -> None:
        await asyncio.to_thread(retire, directory)
        svc.stop()

    agent.on_removed = removed
    svc.agent = agent
    return svc, agent


def retire(directory: Path) -> None:
    """Cremind removed this worker: disable the directory and delete its keys (logs stay)."""
    try:
        spec = load_worker(directory)
        write_worker(directory, replace(spec, enabled=False, extra={**spec.extra, "removed": True}))
    except WorkerDirError as exc:
        log.warning("worker: cannot disable %s: %s", directory, exc)
    for name in ("controller.key", SECRETS_FILE, f"{SECRETS_FILE}.lock", "ca.pem"):
        with contextlib.suppress(OSError):
            (directory / name).unlink()
    log.info("worker: %s retired (disabled, keys deleted)", directory)


async def serve(svc: Any, agent: Any, *, stop: asyncio.Event | None = None) -> None:
    """Run the daemon and the agent until ``stop`` is set or the daemon ends."""
    stop = stop or asyncio.Event()
    await svc.start()
    lease = asyncio.create_task(agent.run(), name="agent lease")
    try:
        runner = asyncio.create_task(svc.run(), name="daemon")
        waiter = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            svc.stop()
        await runner
        waiter.cancel()
    finally:
        lease.cancel()
        with contextlib.suppress(BaseException):
            await lease
        with contextlib.suppress(Exception):
            await agent.client.aclose()


def _watch_stdin(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """The service closes our stdin to ask us to stop (EOF also comes when it dies)."""

    def run() -> None:
        try:
            stream = sys.stdin.buffer if sys.stdin is not None else None
            while stream is not None and stream.read(1):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=run, name="stdin watch", daemon=True).start()


def run_worker(directory: Path, port: str | None, paths: ConnectPaths | None = None) -> int:
    """``cremind-connect worker``: returns the exit status (see the module docstring)."""
    paths = paths or default_paths()
    directory = Path(directory)
    if not port:
        log.error("worker %s: no gateway port given", directory.name)
        return EXIT_CONFIG
    try:
        spec = load_worker(directory)
    except WorkerDirError as exc:
        log.error("worker: %s", exc)
        return EXIT_CONFIG
    if not spec.enabled:
        log.info("worker %s is disabled; not starting", spec.worker_id)
        return EXIT_OK
    try:
        svc, agent = build(directory, port, paths)
    except (WorkerConfigError, WorkerDirError) as exc:
        log.error("worker %s: %s", spec.worker_id, exc)
        return EXIT_CONFIG

    async def main() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        if os.environ.get(SUPERVISED_ENV):
            _watch_stdin(loop, stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
        log.info("worker %s: %s for %s at %s on %s", spec.worker_id, spec.gateway_device_id[-4:].upper(),
                  spec.profile, spec.server_origin, port)
        await serve(svc, agent, stop=stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:
        log.exception("worker %s stopped with an error", spec.worker_id)
        return EXIT_ERROR
    return EXIT_OK


__all__ = ["WorkerConfigError", "build", "load_identity", "retire", "run_worker", "serve"]
