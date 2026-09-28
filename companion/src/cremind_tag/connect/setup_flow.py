"""The setup window's controller: ``cremind-connect window`` (docs/connect-setup.md §8.1, §8.4).

:func:`run_setup` takes the ``cremind-connect://setup`` link the service handed
over and:

1. binds the session with this OS user's installation key — before any window
   exists, so a ``probe`` (the page checking that Connect is installed) ends
   right there, silently;
2. shows the native window (:mod:`.window`): the server, the profile, this
   computer, the four-word phrase Cremind shows too, and the gateways the
   service finds (``list_gateways``: a fresh ``IDENTIFY`` on every free port; a
   sole usable one preselected);
3. on **Approve**, sends the chosen gateway's identity (``approve``), then waits
   for the person to confirm the same words in Cremind;
4. generates the worker's controller key (X25519) and two connector credential
   secrets, stages them in ``workers/.staging-<session>/`` (so a crash cannot
   lose what Cremind already accepted), and redeems the session with their
   hashes and an idempotency key — repeated after a lost answer, the same ids
   come back;
5. turns the staging directory into ``workers/<companion_id>/`` (``worker.json``,
   ``controller.key``, ``secrets.json``, ``ca.pem`` when the link pinned a CA),
   asks the service to start its worker (``add_worker``) and waits until
   Cremind reports the gateway connected (the worker's claim and first
   heartbeat).

**Cancel** before the redemption tells Cremind (``fail``); afterwards the setup
continues in the background and the window only closes. Nothing here logs the
link's token, a credential secret or a key.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import queue
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .bootstrap_client import BootstrapClient, BootstrapError, Bound
from .links import LinkError, SetupLink, parse_setup_link, redact
from .paths import ConnectPaths, default_paths

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CANCELLED = 4
POLL_S = 2.0
GATEWAYS_EVERY_S = 3.0
LIST_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 240.0
CONTROLLER_KEY_FILE = "controller.key"
CONTROLLER_SCHEMA = "cremind-connect/controller-key@1"
STAGING_FILE = "staging.json"
ROLE_GATEWAY = "gateway"
OWNED = "owned"


class View(Protocol):
    """What the controller drives (:class:`.window.SetupWindow`; tests use a fake)."""

    def set_info(self, server_origin: str, profile_name: str, computer_name: str, operation: str) -> None: ...
    def set_phrase(self, words: list[str]) -> None: ...
    def set_gateways(self, choices: list[Any]) -> None: ...
    def set_progress(self, text: str) -> None: ...
    def show_error(self, title: str, message: str) -> None: ...
    def show_done(self, message: str) -> None: ...
    def close(self) -> None: ...
    def on_approve(self, callback: Callable[[str], None]) -> None: ...
    def on_cancel(self, callback: Callable[[], None]) -> None: ...


class ServiceApi(Protocol):
    def list_gateways(self) -> list[dict[str, Any]]: ...
    def add_worker(self, worker_id: str) -> None: ...


class IpcService:
    """The running service, over IPC (:mod:`.ipc`)."""

    def __init__(self, paths: ConnectPaths) -> None:
        self.paths = paths

    def list_gateways(self) -> list[dict[str, Any]]:
        from . import ipc

        answer = ipc.request(self.paths, "list_gateways", timeout=LIST_TIMEOUT_S)
        if not answer.get("ok"):
            raise RuntimeError(str(answer.get("message") or answer.get("error")))
        return list(answer.get("ports") or [])

    def add_worker(self, worker_id: str) -> None:
        from . import ipc

        answer = ipc.request(self.paths, "add_worker", worker_id=worker_id, timeout=10.0)
        if not answer.get("ok"):
            raise RuntimeError(str(answer.get("message") or answer.get("error")))


# ---------------------------------------------------------------------------
# Gateways the person can choose
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    choice: Any  # window.GatewayChoice
    identity: dict[str, Any] | None


_REASONS = {
    "v1_firmware": "Needs a firmware update before it can be used",
    "busy": "In use by another program",
    "no_access": "This computer does not allow Cremind Connect to open it yet",
    "error": "Did not answer as expected",
}


def gateway_candidates(ports: list[dict[str, Any]], bound: Bound) -> list[Candidate]:
    """The window's gateway list from ``list_gateways`` (advisory: Cremind checks again on approve)."""
    from .window import GatewayChoice, gateway_label

    out: list[Candidate] = []
    recover_id = str((bound.recover or {}).get("gateway_device_id") or "").lower() or None
    for port in ports:
        identity = port.get("identity")
        device = str(port.get("device") or "")
        if identity is None:
            reason = _REASONS.get(str(port.get("reason")))
            if reason is not None:  # "no_answer"/"gone": not a Cremind device, not shown
                label = "Gateway (older firmware)" if port.get("reason") == "v1_firmware" else f"Device on {device}"
                out.append(Candidate(GatewayChoice(f"port:{device}", label, usable=False, reason=reason), None))
            continue
        if identity.get("role") != ROLE_GATEWAY:
            continue  # a bridge's maintenance port, a tag on a programmer
        device_id = str(identity.get("device_id") or "").lower()
        label = gateway_label(device_id)
        fw = str(identity.get("fw") or "")
        reason = None
        detail = "Ready to move" if recover_id else "Ready to connect"
        if port.get("in_use"):
            reason = "Already connected on this computer"
        elif recover_id is not None and device_id != recover_id:
            reason = "Not the gateway being moved to this computer"
        elif identity.get("owner_state") == OWNED and identity.get("authority_id") != bound.authority_id.hex():
            reason = "Belongs to another Cremind server"
        elif recover_id is None and identity.get("owner_state") == OWNED:
            detail = "Connected to this Cremind before"
        if fw:
            detail = f"{detail} · firmware {fw}"
        out.append(Candidate(GatewayChoice(device_id, label, detail, usable=reason is None, reason=reason or ""),
                             identity if reason is None else None))
    return out


def approve_body(identity: dict[str, Any]) -> dict[str, Any]:
    """``list_gateways``' identity (names, hex) as ``approve`` sends it (numbers, hex)."""
    from ..protocol.ids import NodeRole, OwnerState

    return {"device_id": identity["device_id"], "ik": identity["ik"],
            "role": int(NodeRole[str(identity["role"]).upper()]), "proto": int(identity["proto"]),
            "fw": str(identity.get("fw") or ""), "board": int(identity.get("board") or 0),
            "owner_state": int(OwnerState[str(identity["owner_state"]).upper()]), "gen": int(identity["gen"]),
            "authority_id": identity.get("authority_id"), "challenge": identity.get("challenge")}


# ---------------------------------------------------------------------------
# Secrets and the worker directory
# ---------------------------------------------------------------------------


def new_credential_secret() -> str:
    """32 random bytes, base64url without padding — the shape Cremind's credentials have."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def secret_sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def write_private(path: Path, text: str) -> None:
    """Atomically write an owner-only file (restricted before a secret byte is in it)."""
    from ..private_files import replace_with_retry, restrict_to_owner

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


def read_controller_key(directory: Path) -> bytes:
    doc = json.loads((Path(directory) / CONTROLLER_KEY_FILE).read_text(encoding="utf-8"))
    if doc.get("schema") != CONTROLLER_SCHEMA:
        raise ValueError(f"{CONTROLLER_KEY_FILE}: unknown schema")
    key = bytes.fromhex(doc["private_key"])
    if len(key) != 32:
        raise ValueError(f"{CONTROLLER_KEY_FILE}: the key must be 32 bytes")
    return key


@dataclass
class Staged:
    """What a redemption needs, kept on disk until it is done."""

    directory: Path
    idempotency_key: str
    controller_priv: bytes
    controller_pub: bytes
    hardware_secret: str
    content_secret: str

    @classmethod
    def load_or_create(cls, workers_dir: Path, session_id: str) -> Staged:
        from ..secure.identity import x25519_generate, x25519_public

        directory = Path(workers_dir) / f".staging-{session_id}"
        path = directory / STAGING_FILE
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            priv = read_controller_key(directory)
            return cls(directory, doc["idempotency_key"], priv, x25519_public(priv), doc["hardware_secret"],
                       doc["content_secret"])
        priv, pub = x25519_generate()
        staged = cls(directory, secrets.token_urlsafe(24), priv, pub, new_credential_secret(), new_credential_secret())
        write_private(directory / CONTROLLER_KEY_FILE,
                      json.dumps({"schema": CONTROLLER_SCHEMA, "private_key": priv.hex()}) + "\n")
        write_private(path, json.dumps({"idempotency_key": staged.idempotency_key,
                                        "hardware_secret": staged.hardware_secret,
                                        "content_secret": staged.content_secret}) + "\n")
        return staged


def finish_worker_dir(paths: ConnectPaths, staged: Staged, bound: Bound, redeemed: dict[str, Any],
                      gateway: dict[str, Any], ca_pem: str | None) -> tuple[str, Path]:
    """Turn the staging directory into ``workers/<companion_id>`` (see the module docstring)."""
    from ..secrets import FileBackend, SecretStore
    from .workerdir import WorkerSpec, write_worker

    companion_id = str(redeemed["companion_id"])
    creds = redeemed.get("credentials") or {}
    worker_id = companion_id
    target = paths.worker_dir(worker_id)
    store = SecretStore(FileBackend(staged.directory / "secrets.json"))
    store.set_credential("hardware", f"{creds['hardware_id']}.{staged.hardware_secret}")
    store.set_credential("content", f"{creds['content_id']}.{staged.content_secret}")
    if ca_pem:
        (staged.directory / "ca.pem").write_text(ca_pem, encoding="ascii")
    server = redeemed.get("server") or {}
    profile = redeemed.get("profile") or {}
    spec = WorkerSpec(worker_id, bound.server_origin, str(profile.get("name") or bound.profile_name), companion_id,
                      str(gateway["device_id"]).lower(), True, extra={
                          "schema": "cremind-connect/worker@1",
                          "profile_id": str(profile.get("id") or bound.profile_id),
                          "installation_id": str(server.get("installation_id") or bound.installation_id),
                          "authority_pub": str(server.get("authority_pub") or bound.authority_pub.hex()),
                          "authority_id": str(server.get("authority_id") or bound.authority_id.hex()),
                          "gateway_ik": str(gateway["ik"]).lower(),
                          "operation": bound.operation,
                          "operation_id": redeemed.get("operation_id"),
                          "credentials": {"hardware_id": creds.get("hardware_id"),
                                          "content_id": creds.get("content_id")},
                          "ca_file": "ca.pem" if ca_pem else None,
                      })
    write_worker(staged.directory, spec)
    (staged.directory / STAGING_FILE).unlink(missing_ok=True)
    if target.exists():
        # The same connection set up again on this computer (a recovery): the old directory is replaced.
        old = target.with_name(f".replaced-{worker_id}-{int(time.time())}")
        os.replace(target, old)
        shutil.rmtree(old, ignore_errors=True)
    os.replace(staged.directory, target)
    return worker_id, target


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


class Cancelled(Exception):
    pass


class SetupFlow:
    """Runs one setup on a worker thread while the window runs on the main thread."""

    def __init__(self, link: SetupLink, bound: Bound, client: BootstrapClient, view: View, service: ServiceApi,
                 paths: ConnectPaths, *, computer: str, poll_s: float = POLL_S,
                 gateways_every_s: float = GATEWAYS_EVERY_S, connect_timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        self.link = link
        self.bound = bound
        self.client = client
        self.view = view
        self.service = service
        self.paths = paths
        self.computer = computer
        self.poll_s = poll_s
        self.gateways_every_s = gateways_every_s
        self.connect_timeout_s = connect_timeout_s
        self._inbox: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.redeemed: dict[str, Any] | None = None
        self.worker_id: str | None = None
        self.gateway_device_id: str | None = None
        self.outcome = EXIT_ERROR
        view.on_approve(lambda gateway_id: self._inbox.put(("approve", gateway_id)))
        view.on_cancel(lambda: self._inbox.put(("cancel", None)))

    # -- helpers --------------------------------------------------------------------------------

    def _wait_input(self, timeout: float) -> tuple[str, Any] | None:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def _check_session(self) -> dict[str, Any]:
        state = self.client.poll()
        if state.get("state") in ("cancelled", "expired", "failed"):
            raise BootstrapError(f"session_{state['state']}", _session_message(state))
        return state

    # -- run ------------------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self.outcome = self._run()
        except Cancelled:
            self.outcome = EXIT_CANCELLED
            self.view.close()
        except BootstrapError as exc:
            log.warning("setup %s: %s (%s)", self.link.session, exc.code, exc)
            self.view.show_error("The setup stopped", str(exc))
            self.outcome = EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - shown to the person, logged with its traceback
            log.exception("setup %s failed", self.link.session)
            with contextlib.suppress(BootstrapError):
                if self.redeemed is None:
                    self.client.fail("connect_failed", "Cremind Connect could not finish the setup.")
            self.view.show_error("The setup stopped", f"Something went wrong: {exc}\n\nDetails are in "
                                                      f"{self.paths.logs_dir}.")
            self.outcome = EXIT_ERROR
        return self.outcome

    def _run(self) -> int:
        bound = self.bound
        self.view.set_info(bound.server_origin, bound.profile_name, self.computer, bound.operation)
        self.view.set_phrase(bound.phrase_words)
        self.view.set_progress("Looking for gateways…")
        identity = self._choose_gateway()
        self.gateway_device_id = str(identity["device_id"]).lower()
        self.view.set_progress("Approved. Now confirm the same words in Cremind…")
        self._wait_confirmed()
        self.view.set_progress("Connecting…")
        staged = Staged.load_or_create(self.paths.workers_dir, self.link.session)
        self.redeemed = self._redeem(staged)
        self.worker_id, directory = finish_worker_dir(self.paths, staged, bound, self.redeemed, identity,
                                                      self.client.ca_pem)
        log.info("setup %s: worker %s ready in %s", self.link.session, self.worker_id, directory)
        try:
            self.service.add_worker(self.worker_id)
        except Exception as exc:  # noqa: BLE001 - the service picks the directory up on its next scan anyway
            log.warning("setup %s: the service did not take worker %s now (%s)", self.link.session, self.worker_id,
                        exc)
        self.view.set_progress("Setting up the gateway…")
        return self._wait_connected()

    def _choose_gateway(self) -> dict[str, Any]:
        candidates: list[Candidate] = []
        next_list = 0.0
        next_poll = time.monotonic() + self.poll_s
        while True:
            now = time.monotonic()
            if now >= next_list:
                try:
                    candidates = gateway_candidates(self.service.list_gateways(), self.bound)
                    self.view.set_gateways([c.choice for c in candidates])
                    usable = sum(1 for c in candidates if c.identity is not None)
                    self.view.set_progress("Choose the gateway, then Approve." if usable > 1 else
                                           "Check the words match Cremind, then Approve." if usable else
                                           "Plug in your gateway. It will appear here in a few seconds.")
                except Exception as exc:  # noqa: BLE001 - the service restarts; keep trying
                    log.info("setup %s: gateway list unavailable: %s", self.link.session, exc)
                next_list = time.monotonic() + self.gateways_every_s
            if now >= next_poll:
                self._check_session()
                next_poll = time.monotonic() + self.poll_s
            event = self._wait_input(0.2)
            if event is None:
                continue
            kind, value = event
            if kind == "cancel":
                self._cancel()
            chosen = next((c for c in candidates if c.choice.id == value and c.identity is not None), None)
            if chosen is None:
                self.view.set_gateways([c.choice for c in candidates])  # re-enables the buttons
                continue
            assert chosen.identity is not None
            self.view.set_progress("Checking with Cremind…")
            try:
                self.client.approve(approve_body(chosen.identity))
            except BootstrapError as exc:
                if exc.final and exc.code not in ("device_owned", "wrong_gateway", "v1_firmware", "not_a_gateway",
                                                  "invalid_gateway", "device_rejected"):
                    raise
                self.view.set_progress(str(exc))
                next_list = 0.0
                continue
            return chosen.identity

    def _cancel(self) -> None:
        with contextlib.suppress(BootstrapError):
            self.client.fail("cancelled", "The setup was cancelled in Cremind Connect.")
        raise Cancelled()

    def _wait_confirmed(self) -> None:
        while True:
            state = self._check_session()
            if state.get("browser_confirmed") or state.get("state") in ("redeeming", "connecting", "completed"):
                return
            event = self._wait_input(self.poll_s)
            if event is not None and event[0] == "cancel":
                self._cancel()

    def _redeem(self, staged: Staged) -> dict[str, Any]:
        delay = 1.0
        for _attempt in range(6):
            try:
                return self.client.redeem(idempotency_key=staged.idempotency_key,
                                          controller_pub=staged.controller_pub,
                                          hardware_sha256=secret_sha256(staged.hardware_secret),
                                          content_sha256=secret_sha256(staged.content_secret))
            except BootstrapError as exc:
                if exc.status is not None and exc.status < 500:
                    raise
                log.info("setup %s: redeem not answered (%s); again in %.0f s", self.link.session, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise BootstrapError("unreachable", "Cremind stopped answering while connecting. Try again from Cremind.")

    def _wait_connected(self) -> int:
        deadline = time.monotonic() + self.connect_timeout_s
        gateway = self._gateway_name()
        while time.monotonic() < deadline:
            try:
                state = self.client.poll()
            except BootstrapError as exc:
                if exc.final:
                    break  # the session ended (e.g. expired): the worker carries on regardless
                state = {}
            if state.get("state") == "completed":
                self.view.show_done(f"{gateway} is connected to Cremind. You can close this window: "
                                    "Cremind Connect keeps it running in the background.")
                return EXIT_OK
            if state.get("state") == "failed":
                raise BootstrapError("claim_failed", _session_message(state))
            event = self._wait_input(self.poll_s)
            if event is not None and event[0] == "cancel":
                self.view.close()  # already redeemed: the worker keeps going; remove it in Cremind instead
                return EXIT_OK
        self.view.show_done(f"{gateway} is still being set up. You can close this window; Cremind shows when it "
                            "is ready.")
        return EXIT_OK

    def _gateway_name(self) -> str:
        from .window import gateway_label

        recover = self.bound.recover or {}
        if recover.get("gateway_name"):
            return str(recover["gateway_name"])
        return gateway_label(self.gateway_device_id or "")


def _session_message(state: dict[str, Any]) -> str:
    error = state.get("error") if isinstance(state.get("error"), dict) else {}
    if error.get("message"):
        return str(error["message"])
    return {"cancelled": "The setup was cancelled in Cremind.",
            "expired": "The setup took too long and expired. Start again from Cremind.",
            "failed": "The setup failed. Start again from Cremind."}.get(str(state.get("state")),
                                                                         "The setup stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_setup(link_url: str, paths: ConnectPaths | None = None, *, view_factory: Callable[[], Any] | None = None,
              service: ServiceApi | None = None, transport: Any = None) -> int:
    """``cremind-connect window``: returns the exit status (see the module docstring)."""
    from .installation import computer_name, load_or_create, platform_name
    from .runtime import connect_version

    paths = paths or default_paths()
    try:
        link = parse_setup_link(link_url)
    except LinkError as exc:
        _message("This link cannot be used", str(exc), view_factory)
        return EXIT_ERROR
    log.info("setup: %s", redact(link.to_url()))
    installation = load_or_create(paths)
    computer = computer_name()
    client = BootstrapClient(link, installation, transport=transport)
    try:
        bound = client.bind(computer=computer, platform=platform_name(), version=connect_version())
    except BootstrapError as exc:
        log.warning("setup %s: bind refused: %s (%s)", link.session, exc.code, exc)
        _message("The setup could not start", str(exc), view_factory)
        client.close()
        return EXIT_ERROR
    if bound.operation == "probe":
        log.info("setup %s: probe answered", link.session)
        client.close()
        return EXIT_OK
    try:
        view = (view_factory or _window)()
    except Exception as exc:  # noqa: BLE001 - no display: tell Cremind so the page can say so
        log.error("setup %s: cannot show the window: %s", link.session, exc)
        with contextlib.suppress(BootstrapError):
            client.fail("no_display", "Cremind Connect could not show its window on this computer.")
        client.close()
        return EXIT_ERROR
    flow = SetupFlow(link, bound, client, view, service or IpcService(paths), paths, computer=computer)
    worker = threading.Thread(target=flow.run, name="setup flow", daemon=True)
    worker.start()
    run = getattr(view, "run", None)
    if callable(run):
        run()  # the Tk main loop, until the window closes
    worker.join(timeout=0 if callable(run) else None)
    client.close()
    return flow.outcome if not worker.is_alive() else EXIT_OK


def _window() -> Any:
    from .window import SetupWindow

    return SetupWindow()


def _message(title: str, message: str, view_factory: Callable[[], Any] | None) -> None:
    if view_factory is not None:
        with contextlib.suppress(Exception):
            view = view_factory()
            view.show_error(title, message)
            run = getattr(view, "run", None)
            if callable(run):
                run()
        return
    from .window import show_message

    show_message(title, message)


__all__ = ["Candidate", "IpcService", "SetupFlow", "Staged", "approve_body", "finish_worker_dir",
           "gateway_candidates", "new_credential_secret", "read_controller_key", "run_setup", "secret_sha256"]
