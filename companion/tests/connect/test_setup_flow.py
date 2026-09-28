"""The setup window's controller and the bootstrap client (docs/connect-setup.md §8.1, docs/setup-api.md §2),
against a fake Cremind that checks every proof the way Cremind does, a fake window and a fake service."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

import httpx
import pytest

from cremind_tag.connect.bootstrap_client import BootstrapClient, BootstrapError, canonical_body, proof_message
from cremind_tag.connect.installation import load_or_create
from cremind_tag.connect.links import SetupLink
from cremind_tag.connect.setup_flow import (
    EXIT_CANCELLED,
    EXIT_OK,
    SetupFlow,
    approve_body,
    gateway_candidates,
    read_controller_key,
    secret_sha256,
)
from cremind_tag.connect.workerdir import load_worker
from cremind_tag.secrets import FileBackend, SecretStore
from cremind_tag.secure import identity

SESSION = str(uuid.uuid4())
TOKEN = "t" * 43
ORIGIN = "https://cremind.test"


class FakeBootstrap:
    """Cremind's /api/tag-setup/v1 for one session: bind, poll, approve, redeem, fail (proofs checked)."""

    def __init__(self, operation: str = "connect_gateway") -> None:
        self.operation = operation
        self.authority_sk, self.authority_pub = identity.ed25519_generate()
        self.state = "waiting_for_connect"
        self.installation_pub: str | None = None
        self.nonce: str | None = None
        self.gateway: dict[str, Any] | None = None
        self.redeemed: dict[str, Any] | None = None
        self.redeem_key: str | None = None
        self.redeem_bodies: list[dict[str, Any]] = []
        self.failed: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def confirm(self) -> None:  # the browser confirms the phrase
        assert self.state == "waiting_for_confirmation"
        self.state = "redeeming"

    def _error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": code, "message": code})

    def _proof_ok(self, action: str, body: dict[str, Any], proof: str | None, pub: str | None = None) -> bool:
        key = pub or self.installation_pub
        if key is None or proof is None:
            return False
        message = proof_message(action, SESSION, "" if action == "bind" else (self.nonce or ""), body)
        return identity.ed25519_verify(bytes.fromhex(key), bytes.fromhex(proof), message)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"CremindSetup {SESSION}.{TOKEN}":
            return self._error(401, "invalid_setup_credential")
        action = request.url.path.rsplit("/", 1)[-1]
        action = "poll" if action == SESSION else action
        self.calls.append(action)
        body = json.loads(request.content) if request.content else {}
        if action == "bind":
            inst = body["installation"]
            if not self._proof_ok("bind", body, body.get("proof"), inst["public_key"]):
                return self._error(401, "invalid_proof")
            if hashlib.sha256(bytes.fromhex(inst["public_key"])).digest()[:16].hex() != inst["id"]:
                return self._error(422, "invalid_installation")
            self.installation_pub, self.nonce = inst["public_key"], "ab" * 16
            self.state = "completed" if self.operation == "probe" else "waiting_for_approval"
            return httpx.Response(200, json={
                "session": {"id": SESSION, "operation": self.operation, "state": self.state, "expires_at": None},
                "server": {"installation_id": "srv", "name": "Cremind", "origin": ORIGIN,
                           "authority_pub": self.authority_pub.hex(),
                           "authority_id": identity.authority_id(self.authority_pub).hex()},
                "profile": {"name": "anna", "id": str(uuid.uuid4())}, "verification_phrase": "amber river candle orbit",
                "server_nonce": self.nonce, "recover": None})
        if action == "poll":
            if not self._proof_ok("poll", {}, request.headers.get("x-cremind-connect-proof")):
                return self._error(401, "invalid_proof")
            return httpx.Response(200, json={"state": self.state, "native_approved": self.gateway is not None,
                                             "browser_confirmed": self.state in ("redeeming", "connecting",
                                                                                 "completed"),
                                             "error": None})
        if not self._proof_ok(action, body, body.get("proof")):
            return self._error(401, "invalid_proof")
        if action == "approve":
            self.gateway = body["gateway"]
            self.state = "waiting_for_confirmation"
            return httpx.Response(200, json={"state": self.state})
        if action == "redeem":
            self.redeem_bodies.append(body)
            if self.redeemed is not None:
                if body["idempotency_key"] != self.redeem_key:
                    return self._error(409, "already_redeemed")
                return httpx.Response(200, json=self.redeemed)
            if self.state != "redeeming":
                return self._error(409, "not_confirmed")
            self.redeem_key = body["idempotency_key"]
            self.redeemed = {"companion_id": str(uuid.uuid4()),
                             "credentials": {"hardware_id": "tagc_" + "a" * 26, "content_id": "tagc_" + "b" * 26},
                             "operation_id": str(uuid.uuid4()), "profile": {"name": "anna", "id": "p"},
                             "server": {"installation_id": "srv", "origin": ORIGIN,
                                        "authority_pub": self.authority_pub.hex(),
                                        "authority_id": identity.authority_id(self.authority_pub).hex()}}
            self.state = "connecting"
            return httpx.Response(200, json=self.redeemed)
        if action == "fail":
            self.failed = body
            self.state = "failed"
            return httpx.Response(200, json={"state": "failed"})
        return self._error(404, "not_found")


class FakeView:
    def __init__(self, *, approve: bool = True, cancel: bool = False) -> None:
        self.auto_approve, self.auto_cancel = approve, cancel
        self.events: list[tuple[str, Any]] = []
        self._approve: Any = None
        self._cancel: Any = None
        self.done = threading.Event()

    def set_info(self, *args: Any) -> None:
        self.events.append(("info", args))

    def set_phrase(self, words: list[str]) -> None:
        self.events.append(("phrase", list(words)))

    def set_gateways(self, choices: list[Any]) -> None:
        self.events.append(("gateways", list(choices)))
        usable = [c for c in choices if c.usable]
        if self.auto_cancel:
            self.auto_cancel = False
            self._cancel()
        elif self.auto_approve and len(usable) == 1:
            self.auto_approve = False
            self._approve(usable[0].id)

    def set_progress(self, text: str) -> None:
        self.events.append(("progress", text))

    def show_error(self, title: str, message: str) -> None:
        self.events.append(("error", (title, message)))
        self.done.set()

    def show_done(self, message: str) -> None:
        self.events.append(("done", message))
        self.done.set()

    def close(self) -> None:
        self.events.append(("close", None))
        self.done.set()

    def on_approve(self, callback: Any) -> None:
        self._approve = callback

    def on_cancel(self, callback: Any) -> None:
        self._cancel = callback


def gateway_port(device_id: bytes, ik: bytes, **identity_fields: Any) -> dict[str, Any]:
    ident = {"device_id": device_id.hex(), "ik": ik.hex(), "role": "gateway", "proto": 2, "fw": "0.2.0",
             "build": "b", "board": 1, "owner_state": "unowned", "gen": 0, "authority_id": None,
             "challenge": "cc" * 16, **identity_fields}
    return {"device": "COM9", "identity": ident, "reason": None, "held_by": None, "in_use": False}


class FakeService:
    def __init__(self, ports: list[dict[str, Any]]) -> None:
        self.ports = ports
        self.added: list[str] = []

    def list_gateways(self) -> list[dict[str, Any]]:
        return list(self.ports)

    def add_worker(self, worker_id: str) -> None:
        self.added.append(worker_id)


def _gateway() -> tuple[bytes, bytes]:
    _, ik = identity.x25519_generate()
    return identity.device_id(1, ik), ik


def run_flow(paths: Any, server: FakeBootstrap, view: FakeView, service: FakeService,
             confirm: bool = True) -> tuple[SetupFlow, int]:
    link = SetupLink(1, ORIGIN, SESSION, TOKEN)
    client = BootstrapClient(link, load_or_create(paths), transport=server.transport)
    bound = client.bind(computer="Test PC", platform="windows", version="0.2.0")
    flow = SetupFlow(link, bound, client, view, service, paths, computer="Test PC", poll_s=0.02,
                     gateways_every_s=0.05, connect_timeout_s=5)
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(flow.run()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while confirm and server.state != "waiting_for_confirmation" and time.monotonic() < deadline:
        time.sleep(0.01)
    if confirm and server.state == "waiting_for_confirmation":
        server.confirm()
        while server.state != "connecting" and time.monotonic() < deadline:
            time.sleep(0.01)
        server.state = "completed"  # the worker claimed the gateway and sent its first heartbeat
    thread.join(10)
    assert not thread.is_alive()
    return flow, result[0]


def test_connect_gateway_end_to_end(paths: Any) -> None:
    server = FakeBootstrap()
    device, ik = _gateway()
    service = FakeService([gateway_port(device, ik)])
    view = FakeView()
    flow, outcome = run_flow(paths, server, view, service)
    assert outcome == EXIT_OK, view.events
    assert ("phrase", ["amber", "river", "candle", "orbit"]) in view.events
    # Approved with the identity the service probed (numbers, not names).
    assert server.gateway == approve_body(service.ports[0]["identity"]) and server.gateway["role"] == 1
    # Redeemed with the hashes of the secrets now stored for the worker, and the controller's public key.
    assert flow.worker_id is not None and service.added == [flow.worker_id]
    directory = paths.worker_dir(flow.worker_id)
    spec = load_worker(directory)
    assert spec.gateway_device_id == device.hex() and spec.extra["gateway_ik"] == ik.hex()
    assert spec.extra["authority_pub"] == server.authority_pub.hex()
    store = SecretStore(FileBackend(directory / "secrets.json"))
    hardware, content = store.get_credential("hardware"), store.get_credential("content")
    assert hardware is not None and content is not None
    body = server.redeem_bodies[0]
    assert body["credentials"] == {"hardware_sha256": secret_sha256(hardware.split(".", 1)[1]),
                                   "content_sha256": secret_sha256(content.split(".", 1)[1])}
    assert hardware.startswith("tagc_aaaa") and content.startswith("tagc_bbbb")
    assert body["controller_pub"] == identity.x25519_public(read_controller_key(directory)).hex()
    assert not any(p.name.startswith(".staging") for p in paths.workers_dir.iterdir())
    assert view.events[-1][0] == "done"


def test_a_lost_redeem_answer_is_repeated_with_the_same_key(paths: Any) -> None:
    server = FakeBootstrap()
    lost = [True]
    handle = server.handle

    def flaky(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.url.path.endswith("/redeem") and lost[0]:
            lost[0] = False
            return httpx.Response(503, json={"error": "unavailable", "message": "try again"})  # committed, lost
        return response

    server.transport = httpx.MockTransport(flaky)
    device, ik = _gateway()
    flow, outcome = run_flow(paths, server, FakeView(), FakeService([gateway_port(device, ik)]))
    assert outcome == EXIT_OK
    keys = {b["idempotency_key"] for b in server.redeem_bodies}
    assert len(server.redeem_bodies) == 2 and len(keys) == 1
    assert flow.redeemed is not None and flow.redeemed["companion_id"] == server.redeemed["companion_id"]


def test_cancel_before_redeem_tells_cremind(paths: Any) -> None:
    server = FakeBootstrap()
    device, ik = _gateway()
    view = FakeView(approve=False, cancel=True)
    service = FakeService([gateway_port(device, ik)])
    _, outcome = run_flow(paths, server, view, service, confirm=False)
    assert outcome == EXIT_CANCELLED
    assert server.failed is not None and server.failed["code"] == "cancelled" and service.added == []


def test_a_probe_link_binds_and_needs_no_window(paths: Any) -> None:
    from cremind_tag.connect.setup_flow import run_setup

    server = FakeBootstrap(operation="probe")
    link = SetupLink(1, ORIGIN, SESSION, TOKEN).to_url()

    def no_window() -> Any:
        raise AssertionError("a probe must not open a window")

    assert run_setup(link, paths, view_factory=no_window, transport=server.transport) == EXIT_OK
    assert server.state == "completed" and server.calls == ["bind"]


def test_requests_are_proven_with_the_installation_key(paths: Any) -> None:
    server = FakeBootstrap()
    link = SetupLink(1, ORIGIN, SESSION, TOKEN)
    client = BootstrapClient(link, load_or_create(paths), transport=server.transport)
    client.bind(computer="PC", platform="linux", version="1")
    # Another installation cannot speak for this session: its proofs fail.
    other = BootstrapClient(link, _other_installation(), transport=server.transport)
    other.server_nonce = client.server_nonce
    with pytest.raises(BootstrapError) as err:
        other.poll()
    assert err.value.code == "invalid_proof" and err.value.status == 401
    assert client.poll()["state"] == "waiting_for_approval"
    assert canonical_body({"b": 1, "a": "é", "proof": "x"}) == '{"a":"é","b":1}'.encode()


def _other_installation() -> Any:
    from cremind_tag.connect.installation import Installation, installation_id

    priv, pub = identity.ed25519_generate()
    return Installation(installation_id(pub), pub, "", priv)


def test_gateway_choices(paths: Any) -> None:
    from cremind_tag.connect.bootstrap_client import Bound

    authority = identity.ed25519_generate()[1]
    bound = Bound(SESSION, "connect_gateway", "waiting_for_approval", None, ORIGIN, "Cremind", "srv", authority,
                  identity.authority_id(authority), "anna", "p", "a b c d", "n", None)
    ok_id, ok_ik = _gateway()
    mine_id, mine_ik = _gateway()
    foreign_id, foreign_ik = _gateway()
    busy_id, busy_ik = _gateway()
    ports = [gateway_port(ok_id, ok_ik),
             gateway_port(mine_id, mine_ik, owner_state="owned", authority_id=identity.authority_id(authority).hex()),
             gateway_port(foreign_id, foreign_ik, owner_state="owned", authority_id="11" * 16),
             {**gateway_port(busy_id, busy_ik), "in_use": True},
             {"device": "COM3", "identity": None, "reason": "v1_firmware"},
             {"device": "COM4", "identity": None, "reason": "no_answer"},
             {**gateway_port(ok_id, ok_ik), "identity": {**gateway_port(ok_id, ok_ik)["identity"], "role": "bridge"}}]
    choices = {c.choice.id: c for c in gateway_candidates(ports, bound)}
    assert choices[ok_id.hex()].choice.usable and choices[ok_id.hex()].identity is not None
    assert choices[mine_id.hex()].choice.usable  # owned by this server before: Cremind decides (re-adopt)
    assert not choices[foreign_id.hex()].choice.usable and "another Cremind" in choices[foreign_id.hex()].choice.reason
    assert not choices[busy_id.hex()].choice.usable
    assert not choices["port:COM3"].choice.usable and "firmware" in choices["port:COM3"].choice.reason
    assert "port:COM4" not in choices and len(choices) == 5  # not a Cremind device; a bridge is not a gateway
    # A recovery offers only the gateway being moved.
    moving = Bound(**{**bound.__dict__, "operation": "recover",
                      "recover": {"companion_id": "c", "gateway_device_id": mine_id.hex(), "gateway_name": "Hall"}})
    usable = [c.choice.id for c in gateway_candidates(ports, moving) if c.choice.usable]
    assert usable == [mine_id.hex()]
