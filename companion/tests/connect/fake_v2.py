"""The fake Cremind of the daemon tests plus the connector v2 additions a private worker uses
(docs/setup-api.md §3): ``lease``, ``state``, ``operations`` (+ ``progress``), ``grants`` signed by an
in-memory authority, and a compare-and-set ``vault``. It follows Cremind's ``app/tags/operations.py``
closely enough for the worker's contract: grants only for an open operation of the worker that needs that
op, for the binding's current (or pending) generation; a ``pair`` grant creates the binding; a finished
pairing makes the binding ready; a finished ``release_gateway`` revokes the worker's credentials.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from cremind_tag.protocol.ids import GrantOp, NodeRole
from cremind_tag.secure import grants, identity

_DAEMON_TESTS = Path(__file__).resolve().parents[1] / "daemon"


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _DAEMON_TESTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FakeCremind = _load("fake_cremind").FakeCremind
PREFIX = "/api/tag-connector/v1"
OPEN = ("queued", "running", "pending_device")
FINAL = ("succeeded", "failed", "cancelled")
GRANTS_FOR = {"claim_gateway": {"claim", "recover"}, "pair_bridge": {"pair"}, "pair_tag": {"pair"},
              "unpair": {"release"}, "release_gateway": {"release"}, "recover_gateway": {"recover", "rekey"}}
GRANT_OPS = {"claim": GrantOp.CLAIM, "recover": GrantOp.RECOVER, "pair": GrantOp.PAIR, "rekey": GrantOp.REKEY,
             "release": GrantOp.RELEASE, "maint": GrantOp.MAINT}
ROLES = {"gateway": NodeRole.GATEWAY, "bridge": NodeRole.BRIDGE, "tag": NodeRole.TAG}


class FakeV2Cremind(FakeCremind):  # type: ignore[misc, valid-type]
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.authority_sk, self.authority_pub = identity.ed25519_generate()
        self.profile_id = str(uuid.uuid4())
        self.controller_pub: bytes | None = None
        self.worker_state = "active"
        self.paused = False
        self.operations: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}  # device_id hex -> binding
        self.vault: dict[str, list[dict[str, Any]]] = {}  # subject -> versions (newest last)
        self.grants_issued: list[dict[str, Any]] = []
        self.leases = 0
        self.progress_log: list[tuple[str, dict[str, Any]]] = []

    @property
    def owner(self) -> bytes:
        return uuid.UUID(self.profile_id).bytes

    # -- test helpers -------------------------------------------------------------------------

    def add_binding(self, device_id: bytes, role: str, ik: bytes, gen: int = 0, state: str = "pairing") -> None:
        self.bindings[device_id.hex()] = {"device_id": device_id.hex(), "role": role, "ik": ik.hex(),
                                          "generation": gen, "pending": None, "state": state}

    def queue_operation(self, kind: str, args: dict[str, Any], *, setup_secret: bytes | None = None) -> str:
        op_id = str(uuid.uuid4())
        self.operations[op_id] = {"id": op_id, "kind": kind, "state": "queued", "stage": "queued", "args": args,
                                  "setup_secret": setup_secret.hex() if setup_secret else None, "result": None,
                                  "error": None, "candidates": [], "devices": {}, "device": None}
        self.add_command("run_operation", {"operation_id": op_id, "kind": kind})
        return op_id

    def op(self, op_id: str) -> dict[str, Any]:
        return self.operations[op_id]

    def cancel_operation(self, op_id: str) -> None:
        self.operations[op_id].update(state="cancelled", setup_secret=None)

    def vault_latest(self, subject: str) -> dict[str, Any] | None:
        versions = self.vault.get(subject) or []
        return versions[-1] if versions else None

    # -- HTTP -------------------------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        route = path[len(PREFIX):] if path.startswith(PREFIX) else ""
        body: Any = json.loads(request.content) if request.content else None
        method = request.method
        if route == "/lease" and method == "POST":
            return self._hw(request) or self._lease()
        if route == "/state" and method == "GET":
            return self._hw(request) or self._state()
        if route.startswith("/operations/"):
            parts = route.split("/")
            op = self.operations.get(parts[2])
            if self._hw(request) is not None:
                return self._hw(request)  # type: ignore[return-value]
            if op is None:
                return self._error(404, "operation_not_found", "No such operation for this worker.")
            if len(parts) == 3 and method == "GET":
                return self._json(200, {"operation": self._op_json(op, worker=True)})
            if len(parts) == 4 and parts[3] == "progress" and method == "POST":
                return self._progress(op, body or {})
        if route == "/grants" and method == "POST":
            return self._hw(request) or self._grant(body or {})
        if route.startswith("/vault/") and method == "PUT":
            return self._hw(request) or self._vault_put(route.split("/")[2], body or {})
        if route == "/vault" and method == "GET":
            return self._hw(request) or self._vault_get()
        return await super().handle(request)

    def _hw(self, request: httpx.Request) -> httpx.Response | None:
        _, err = self._auth(request, "hardware")
        return err

    def _lease(self) -> httpx.Response:
        self.leases += 1
        return self._json(200, {"lease_id": "l", "expires_at": "", "ttl_s": 60, "renew_s": 20,
                                "state": self.worker_state, "paused": self.paused, "generation": 0})

    def _state(self) -> httpx.Response:
        return self._json(200, {"generation": 0, "state": self.worker_state, "paused": self.paused,
                                "bindings": [{"device_id": b["device_id"], "role": b["role"],
                                              "generation": b["generation"], "state": b["state"], "paused": False}
                                             for b in self.bindings.values()],
                                "revoked": [], "operations": [{"id": o["id"], "kind": o["kind"], "state": o["state"]}
                                                              for o in self.operations.values() if o["state"] in OPEN]})

    def _op_json(self, op: dict[str, Any], *, worker: bool = False) -> dict[str, Any]:
        out = {k: op[k] for k in ("id", "kind", "state", "stage", "args", "result", "error")}
        if worker:
            out["setup_secret"] = op["setup_secret"] if op["state"] in OPEN else None
            out["owner"] = self.owner.hex()
        return out

    def _progress(self, op: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        self.progress_log.append((op["id"], body))
        if op["state"] in FINAL:
            return self._json(200, {"operation": self._op_json(op)})
        if isinstance(body.get("stage"), str):
            op["stage"] = body["stage"]
            if op["state"] == "queued":
                op["state"] = "running"
        for cand in body.get("candidates") or []:
            op["candidates"].append(cand)
        for dev in body.get("devices") or []:
            op["devices"][dev["device_id"]] = dev
            binding = self.bindings.get(dev["device_id"])
            if binding is not None and isinstance(dev.get("gen"), int):
                binding.update(generation=dev["gen"], pending=None)
                if dev.get("state") == "rekeyed":
                    binding["state"] = "ready"
        if isinstance(body.get("device"), dict):
            op["device"] = body["device"]
            binding = self.bindings.get(str(op["args"].get("device_id") or op.get("binding") or ""))
            gen = body["device"].get("gen")
            if binding is not None and isinstance(gen, int) and gen in (binding["generation"], binding["pending"]):
                binding.update(generation=gen, pending=None)
        state = body.get("state")
        if state in ("succeeded", "failed", "pending_device"):
            op["state"] = state
            if isinstance(body.get("error"), dict):
                op["error"] = body["error"]
            if isinstance(body.get("result"), dict):
                op["result"] = body["result"]
            if state != "pending_device":
                op["setup_secret"] = None
            self._finish(op, state)
        return self._json(200, {"operation": self._op_json(op)})

    def _finish(self, op: dict[str, Any], state: str) -> None:
        binding = self.bindings.get(str(op.get("binding") or op["args"].get("device_id") or ""))
        if op["kind"] in ("claim_gateway", "pair_bridge", "pair_tag") and binding is not None:
            if state == "succeeded":
                binding["state"] = "ready"
            elif state == "failed" and binding["state"] == "pairing":
                del self.bindings[binding["device_id"]]
        if op["kind"] == "unpair" and state == "succeeded" and binding is not None:
            del self.bindings[binding["device_id"]]
        if op["kind"] == "release_gateway" and state == "succeeded":
            self.bindings.clear()
            for cred in self.credentials.values():
                cred.revoked = True

    def _grant(self, body: dict[str, Any]) -> httpx.Response:
        op = self.operations.get(str(body.get("operation_id")))
        name = body.get("op")
        if op is None or op["state"] not in OPEN or name not in GRANTS_FOR.get(op["kind"], set()):
            return self._error(403, "grant_refused", "No open operation of this worker needs that grant.")
        device = str(body.get("device_id"))
        role = str(body.get("role"))
        gen_from = int(body["gen_from"])
        binding = self.bindings.get(device)
        if name == "pair":
            ik = bytes.fromhex(str(body.get("ik")))
            if identity.device_id(ROLES[role], ik).hex() != device:
                return self._error(422, "invalid_grant_request", "ik")
            if identity.short_id(bytes.fromhex(device)) != int(op["args"].get("short_id") or -1):
                return self._error(403, "grant_refused", "not the labelled device")
            if binding is not None and binding.get("op") != op["id"]:
                return self._error(409, "device_owned", "That device is already set up.")
            if binding is None:
                self.add_binding(bytes.fromhex(device), role, ik, gen_from)
                binding = self.bindings[device]
                binding["op"] = op["id"]
                op["binding"] = device
        elif binding is None:
            return self._error(403, "grant_refused", "That device does not belong to this worker.")
        allowed = {binding["generation"]} | ({binding["pending"]} if binding["pending"] is not None else set())
        if gen_from not in allowed:
            return self._error(409, "stale_generation", "generation", expected=sorted(allowed))
        assert self.controller_pub is not None
        raw = grants.Grant(GRANT_OPS[name], bytes.fromhex(device), ROLES[role], self.authority_pub, self.owner,
                           self.controller_pub, gen_from, gen_from + 1, bytes.fromhex(str(body["challenge"]))).encode()
        binding["pending"] = gen_from + 1
        self.grants_issued.append({"op": name, "device_id": device, "gen_from": gen_from, "operation": op["id"]})
        return self._json(200, {"grant": raw.hex(), "sig": grants.sign(raw, self.authority_sk).hex(),
                                "authority_pub": self.authority_pub.hex()})

    def _vault_put(self, subject: str, body: dict[str, Any]) -> httpx.Response:
        if subject != "worker" and subject not in self.bindings:
            return self._error(403, "not_current_worker", "That device does not belong to this worker.")
        versions = self.vault.setdefault(subject, [])
        current = versions[-1]["version"] if versions else None
        if body.get("expected_version") != current:
            return self._error(409, "version_conflict", "Another version was saved meanwhile.", version=current)
        version = (current or 0) + 1
        versions.append({"device_id": subject, "version": version, "generation": body["generation"],
                         "stage": body["stage"], "state": body["state"]})
        return self._json(200, {"version": version})

    def _vault_get(self) -> httpx.Response:
        if not any(o["kind"] == "recover_gateway" and o["state"] in OPEN for o in self.operations.values()):
            return self._error(403, "no_recovery", "Recovery data is delivered only during a recovery.")
        entries = [v for versions in self.vault.values() for v in versions[-2:]]
        return self._json(200, {"entries": entries})
