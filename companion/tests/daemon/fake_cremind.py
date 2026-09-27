"""A stateful in-process Cremind connector for tests, served through ``httpx.MockTransport``.

It mirrors the real implementation (Cremind ``app/api/tag_connector.py`` and
``app/tags/storage.py``) closely enough for the daemon's contract: credential
checks (401 ``invalid_credential``/``credential_revoked``, 403
``wrong_credential_kind``), per-profile streams with ``stream_id``, delivery
``seq`` and pruning (410 ``cursor_expired`` with ``oldest_seq``/``head_seq``/
``stream_id``), ``events`` paging (``next_after`` = ``head_seq`` when the page is
not full), ``sync``'s ``outstanding``, ``accepted`` (queued ->
companion_accepted), receipts that never move a stage backwards and whose
terminal outcome is final (and whose ``epoch`` must match), previews, hardware
commands with claim/result, and ``replace_key``/``resolves`` retiring older
deliveries the way ``write_deliveries`` does. Timestamps are ISO strings with
second resolution, like the server's.

Every receipt received is logged in ``receipt_log`` so tests can check that no
delivery was ever reported with two different terminal outcomes.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import itertools
import json
import secrets as pysecrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

PREFIX = "/api/tag-connector/v1"
STAGES = ("queued", "companion_accepted", "gateway_received", "bridge_received", "transferring", "refreshing",
          "displayed")
STAGE_RANK = {s: i for i, s in enumerate(STAGES)}
TERMINAL = ("displayed", "superseded", "expired", "cancelled", "failed", "uncertain")
ACTIVE = STAGES[:-1]


def iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return dt.datetime.fromisoformat(text).timestamp() * 1000
    except ValueError:
        return None


@dataclass
class Cred:
    id: str
    secret: str
    kind: str
    profile: str | None
    revoked: bool = False

    @property
    def authorization(self) -> str:
        return f"CremindTag {self.id}.{self.secret}"

    @property
    def value(self) -> str:
        return f"{self.id}.{self.secret}"


@dataclass
class Stream:
    stream_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    head: int = 0
    pruned: int = 0
    settings: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True, "layout": "status", "show_excerpts": False, "qr_links": False, "progress_cadence_s": 300,
        "timezone": "UTC", "language": "en"})


class FakeCremind:
    """See the module docstring. ``transport`` is what ``DaemonOptions.transport`` takes."""

    def __init__(self, *, clock: Callable[[], float] = time.time, url: str = "https://cremind.test") -> None:
        self.clock = clock
        self.url = url
        self.companion_id = str(uuid.uuid4())
        self.credentials: dict[str, Cred] = {}
        self.streams: dict[str, Stream] = {}
        self.devices: dict[tuple[str, str], dict[str, Any]] = {}
        self.deliveries: dict[int, dict[str, Any]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.previews: dict[tuple[str, str], dict[str, Any]] = {}
        self.preview_log: list[dict[str, Any]] = []
        self.receipt_log: list[dict[str, Any]] = []
        self.accepted_log: list[int] = []
        self.requests: list[tuple[str, str, str | None]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.inventories: list[dict[str, Any]] = []
        self.command_results: list[dict[str, Any]] = []
        self.fail_next: dict[str, list[int]] = {}
        self.expire_cursor_once = False
        self._ids = itertools.count(500)
        self._command_event: asyncio.Event | None = None
        self.transport = httpx.MockTransport(self.handle)

    # -- setup helpers ---------------------------------------------------------------------

    def now_ms(self) -> float:
        return self.clock() * 1000

    def add_credential(self, kind: str, profile: str | None = None) -> Cred:
        cred = Cred("tagc_" + "".join(pysecrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(26)),
                    base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode(), kind, profile)
        self.credentials[cred.id] = cred
        if profile is not None:
            self.streams.setdefault(profile, Stream())
        return cred

    def revoke(self, credential_id: str) -> None:
        self.credentials[credential_id].revoked = True

    def add_bridge(self, hw_id: str) -> dict[str, Any]:
        return self.devices.setdefault(("bridge", hw_id), {"kind": "bridge", "hw_id": hw_id, "info": {}})

    def add_tag(self, hw_id: str, *, owner: str | None = None, epoch: int = 0, bridge_hw_id: str | None = None,
                width: int = 400, height: int = 300, planes: int = 1, rotation: int = 0, name: str = "Desk",
                clear_required: bool = False) -> dict[str, Any]:
        device = {"kind": "tag", "hw_id": hw_id, "name": name, "owner_profile": owner, "epoch": epoch,
                  "bridge_hw_id": bridge_hw_id, "width": width, "height": height, "planes": planes,
                  "rotation": rotation, "desired_revision": 0, "displayed_revision": 0, "displayed_digest": None,
                  "clear_required": clear_required, "info": {}}
        self.devices[("tag", hw_id)] = device
        if owner is not None:
            self.streams.setdefault(owner, Stream())
        return device

    def tag(self, hw_id: str) -> dict[str, Any]:
        return self.devices[("tag", hw_id)]

    def settings(self, profile: str, **values: Any) -> None:
        self.streams[profile].settings.update(values)

    def add_job(self, profile: str, tag_hw: str, *, kind: str = "notification", title: str = "Hello",
                body: str | None = None, ttl_s: float = 3600.0, priority: int | None = None,
                replace_key: str | None = None, resolves: str | None = None, card: dict[str, Any] | None = None,
                progress: dict[str, int] | None = None, cancel_first: bool = False) -> int:
        """Write one delivery the way ``write_deliveries`` does; returns its delivery id."""
        stream = self.streams[profile]
        device = self.tag(tag_hw)
        now = self.now_ms()
        if cancel_first:
            for d in self.deliveries.values():
                if d["tag"] == tag_hw and d["stage"] in ACTIVE:
                    self._end(d, "cancelled", "cleared")
        for key in (replace_key, resolves):
            if key:
                for d in self.deliveries.values():
                    if d["tag"] == tag_hw and d["replace_key"] == key and d["stage"] in ACTIVE:
                        self._end(d, "superseded", "replaced")
        stream.head += 1
        delivery_id = next(self._ids)
        self.deliveries[delivery_id] = {
            "id": delivery_id, "profile": profile, "seq": stream.head, "tag": tag_hw, "epoch": device["epoch"],
            "kind": kind, "priority": priority if priority is not None else {"needs_input": 90, "clear": 100,
                                                                              "resolved": 90}.get(kind, 40),
            "replace_key": replace_key, "resolves": resolves,
            "card": card or {"v": 1, "kind": kind, "severity": "info", "icon": "info", "title": title, "body": body,
                             "lang": "en", "ts": iso(now), "progress": progress, "link": None,
                             "source": {"type": "test", "id": None}},
            "stage": "queued", "outcome": None, "status_code": None, "revision": None, "digest": None,
            "detail": None, "timing": None, "stage_times": {"queued": now}, "created_at": now,
            "expires_at": now + ttl_s * 1000}
        self._wake_commands()
        return delivery_id

    def _end(self, d: dict[str, Any], outcome: str, detail: str) -> None:
        d.update(stage=outcome, outcome=outcome, detail=detail)

    def delivery(self, delivery_id: int) -> dict[str, Any]:
        return self.deliveries[delivery_id]

    def restore(self, profile: str) -> None:
        """A backup restore: active deliveries cancelled, a new stream_id, delivery ids jump."""
        for d in self.deliveries.values():
            if d["profile"] == profile and d["stage"] in ACTIVE:
                self._end(d, "cancelled", "restored")
        self.streams[profile].stream_id = str(uuid.uuid4())
        self._ids = itertools.count(next(self._ids) + (1 << 32))

    def prune(self, profile: str, through: int) -> None:
        self.streams[profile].pruned = through

    def add_command(self, kind: str, args: dict[str, Any], ttl_s: float = 3600.0) -> str:
        cid = str(uuid.uuid4())
        now = self.now_ms()
        self.commands[cid] = {"id": cid, "kind": kind, "args": args, "status": "queued", "result": None,
                              "error": None, "created_at": now, "expires_at": now + ttl_s * 1000}
        self._wake_commands()
        return cid

    def claim_tag(self, hw_id: str, owner: str, bridge_hw_id: str) -> tuple[str, str]:
        """Admin claim: epoch + 1, clear_required, cancel old work, queue assign_tag + clear_tag."""
        device = self.tag(hw_id)
        epoch = int(device["epoch"]) + 1
        device.update(owner_profile=owner, epoch=epoch, clear_required=True, bridge_hw_id=bridge_hw_id)
        self.streams.setdefault(owner, Stream())
        for d in self.deliveries.values():
            if d["tag"] == hw_id and d["stage"] in ACTIVE:
                self._end(d, "cancelled", "reassigned")
        assign = self.add_command("assign_tag", {"tag_id": hw_id, "bridge_hw_id": bridge_hw_id, "epoch": epoch},
                                  ttl_s=7 * 86400)
        clear = self.add_command("clear_tag", {"tag_id": hw_id, "epoch": epoch}, ttl_s=7 * 86400)
        return assign, clear

    def assign(self, hw_id: str, bridge_hw_id: str) -> str:
        device = self.tag(hw_id)
        epoch = int(device["epoch"]) + 1
        device.update(epoch=epoch, bridge_hw_id=bridge_hw_id)
        for d in self.deliveries.values():
            if d["tag"] == hw_id and d["stage"] in ACTIVE:
                d["epoch"] = epoch
        return self.add_command("assign_tag", {"tag_id": hw_id, "bridge_hw_id": bridge_hw_id, "epoch": epoch},
                                ttl_s=7 * 86400)

    def _wake_commands(self) -> None:
        if self._command_event is not None:
            self._command_event.set()

    # -- queries for assertions ---------------------------------------------------------------

    def terminal_outcomes(self) -> dict[int, set[str]]:
        """Terminal outcomes each delivery was ever reported with (should be one per delivery)."""
        out: dict[int, set[str]] = {}
        for r in self.receipt_log:
            if r.get("outcome") in TERMINAL:
                out.setdefault(int(r["delivery_id"]), set()).add(r["outcome"])
        return out

    # -- HTTP ----------------------------------------------------------------------------------

    def _json(self, status: int, body: Any) -> httpx.Response:
        return httpx.Response(status, json=body)

    def _error(self, status: int, code: str, message: str, **extra: Any) -> httpx.Response:
        return self._json(status, {"error": code, "message": message, "detail": message, **extra})

    def _auth(self, request: httpx.Request, kind: str | None) -> tuple[Cred | None, httpx.Response | None]:
        header = request.headers.get("authorization", "")
        if not header.startswith("CremindTag "):
            return None, self._error(401, "unsupported_scheme", "CremindTag only")
        value = header[len("CremindTag "):]
        cred_id, _, secret = value.partition(".")
        cred = self.credentials.get(cred_id)
        if cred is None or cred.secret != secret:
            return None, self._error(401, "invalid_credential", "The credential is not valid.")
        if cred.revoked:
            return None, self._error(401, "credential_revoked", "The credential has been revoked.")
        if kind is not None and cred.kind != kind:
            return None, self._error(403, "wrong_credential_kind", f"This endpoint needs a {kind} credential.")
        return cred, None

    def _expire(self) -> None:
        now = self.now_ms()
        for d in self.deliveries.values():
            if d["stage"] in ACTIVE and d["expires_at"] <= now:
                self._end(d, "expired", "expired")

    def _job(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"delivery_id": d["id"], "seq": d["seq"], "tag_id": d["tag"], "epoch": d["epoch"], "kind": d["kind"],
                "priority": d["priority"], "replace_key": d["replace_key"], "resolves": d["resolves"],
                "created_at": iso(d["created_at"]), "expires_at": iso(d["expires_at"]), "stage": d["stage"],
                "card": d["card"]}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(PREFIX):
            return self._error(404, "not_found", "no route")
        route = path[len(PREFIX):]
        self.requests.append((request.method, route, None))
        faults = self.fail_next.get(route.split("/")[1] if route.count("/") > 1 else route.strip("/"))
        if faults:
            status = faults.pop(0)
            return self._error(status, "injected", f"injected HTTP {status}")
        self._expire()
        body: Any = None
        if request.content:
            body = json.loads(request.content)
        if route == "/whoami" and request.method == "GET":
            cred, err = self._auth(request, None)
            if err:
                return err
            assert cred is not None
            return self._json(200, {"credential_id": cred.id, "kind": cred.kind, "companion_id": self.companion_id,
                                    "profile": cred.profile, "api_version": 1, "server_time": iso(self.now_ms())})
        if route == "/inventory":
            return self._inventory(request, body)
        if route == "/heartbeat":
            _, err = self._auth(request, "hardware")
            if err:
                return err
            self.heartbeats.append(body)
            pending = sum(1 for c in self.commands.values() if c["status"] == "queued")
            return self._json(200, {"server_time": iso(self.now_ms()), "commands_pending": pending})
        if route == "/commands":
            return await self._commands(request)
        if route.startswith("/commands/") and route.endswith("/claim"):
            return self._claim(request, route.split("/")[2])
        if route.startswith("/commands/") and route.endswith("/result"):
            return self._result(request, route.split("/")[2], body)
        if route == "/sync":
            return self._sync(request, body)
        if route == "/events":
            return self._events(request)
        if route == "/accepted":
            return self._accepted(request, body)
        if route == "/receipts":
            return self._receipts(request, body)
        if route == "/previews":
            return self._previews(request, body)
        return self._error(404, "not_found", "no route")

    def _inventory(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        _, err = self._auth(request, "hardware")
        if err:
            return err
        self.inventories.append(body)
        for item in body.get("bridges") or []:
            self.add_bridge(item["hw_id"])["info"].update(item)
        for item in body.get("tags") or []:
            device = self.devices.get(("tag", item["tag_id"]))
            if device is None:
                device = self.add_tag(item["tag_id"], width=item.get("width") or 400,
                                      height=item.get("height") or 300, planes=item.get("planes") or 1)
            reported = item.get("epoch")
            if isinstance(reported, int) and reported > device["epoch"]:
                device["epoch"] = reported
        assignments = [{"tag_id": d["hw_id"], "owner_profile": d["owner_profile"], "bridge_hw_id": d["bridge_hw_id"],
                        "epoch": d["epoch"], "rotation": d["rotation"]}
                       for (kind, _), d in self.devices.items() if kind == "tag"]
        return self._json(200, {"devices": list(self.devices.values()), "assignments": assignments})

    async def _commands(self, request: httpx.Request) -> httpx.Response:
        _, err = self._auth(request, "hardware")
        if err:
            return err
        wait = min(int(request.url.params.get("wait") or 0), 30)
        if self._command_event is None:
            self._command_event = asyncio.Event()
        deadline = time.monotonic() + wait
        while True:
            self._command_event.clear()
            now = self.now_ms()
            rows = [c for c in self.commands.values() if c["status"] == "queued" and c["expires_at"] > now]
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._command_event.wait(), min(remaining, 0.5))
            except TimeoutError:
                pass
        return self._json(200, {"commands": [self._command_json(c) for c in rows]})

    @staticmethod
    def _command_json(c: dict[str, Any]) -> dict[str, Any]:
        return {"id": c["id"], "kind": c["kind"], "args": c["args"], "status": c["status"],
                "created_at": iso(c["created_at"]), "expires_at": iso(c["expires_at"])}

    def _claim(self, request: httpx.Request, cid: str) -> httpx.Response:
        _, err = self._auth(request, "hardware")
        if err:
            return err
        c = self.commands.get(cid)
        if c is None:
            return self._error(404, "command_not_found", "No command with that id.")
        if c["status"] != "queued":
            return self._error(409, "already_claimed", f"The command is already {c['status']}.",
                               command=self._command_json(c))
        c["status"] = "claimed"
        return self._json(200, self._command_json(c))

    def _result(self, request: httpx.Request, cid: str, body: dict[str, Any]) -> httpx.Response:
        _, err = self._auth(request, "hardware")
        if err:
            return err
        c = self.commands.get(cid)
        if c is None:
            return self._error(404, "command_not_found", "No command with that id.")
        status = body.get("status")
        if status not in ("succeeded", "failed"):
            return self._error(422, "invalid_status", "bad status")
        self.command_results.append({"id": cid, **body})
        if c["status"] in ("succeeded", "failed", "expired", "cancelled"):
            if c["status"] == status:
                return self._json(200, {"ok": True, "command": self._command_json(c)})
            return self._error(409, "already_completed", "done", command=self._command_json(c))
        c.update(status=status, result=body.get("result"), error=body.get("error"))
        if status == "succeeded" and c["kind"] == "clear_tag":
            device = self.devices.get(("tag", c["args"]["tag_id"]))
            if device is not None and device["epoch"] == int(c["args"]["epoch"]):
                device["clear_required"] = False
        return self._json(200, {"ok": True, "command": self._command_json(c)})

    def _owned(self, profile: str) -> list[dict[str, Any]]:
        return [d for (kind, _), d in self.devices.items() if kind == "tag" and d["owner_profile"] == profile]

    def _sync(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        cred, err = self._auth(request, "content")
        if err:
            return err
        assert cred is not None and cred.profile is not None
        stream = self.streams[cred.profile]
        cursor = body.get("cursor")
        valid = isinstance(cursor, int) and stream.pruned <= cursor <= stream.head
        now = self.now_ms()
        owned = {d["hw_id"] for d in self._owned(cred.profile)}
        outstanding = [self._job(d) for d in sorted(self.deliveries.values(), key=lambda d: d["seq"])
                       if d["profile"] == cred.profile and d["stage"] in ACTIVE and d["expires_at"] > now
                       and d["tag"] in owned]
        tags = [{"tag_id": d["hw_id"], "name": d["name"], "epoch": d["epoch"], "bridge_hw_id": d["bridge_hw_id"],
                 "width": d["width"], "height": d["height"], "planes": d["planes"], "rotation": d["rotation"],
                 "desired_revision": d["desired_revision"], "displayed_revision": d["displayed_revision"],
                 "clear_required": d["clear_required"]} for d in self._owned(cred.profile)]
        return self._json(200, {"profile": cred.profile, "companion_id": self.companion_id,
                                "stream_id": stream.stream_id, "cursor_valid": valid, "oldest_seq": stream.pruned + 1,
                                "head_seq": stream.head, "outstanding": outstanding, "tags": tags,
                                "settings": dict(stream.settings)})

    def _events(self, request: httpx.Request) -> httpx.Response:
        cred, err = self._auth(request, "content")
        if err:
            return err
        assert cred is not None and cred.profile is not None
        stream = self.streams[cred.profile]
        after = int(request.url.params.get("after") or 0)
        limit = min(int(request.url.params.get("limit") or 100), 200)
        if self.expire_cursor_once or after > stream.head or after < stream.pruned:
            self.expire_cursor_once = False
            return self._error(410, "cursor_expired", "call sync", oldest_seq=stream.pruned + 1,
                               head_seq=stream.head, stream_id=stream.stream_id)
        rows = [d for d in sorted(self.deliveries.values(), key=lambda d: d["seq"])
                if d["profile"] == cred.profile and after < d["seq"] <= stream.head][:limit]
        next_after = rows[-1]["seq"] if len(rows) >= limit else max(stream.head, after)
        return self._json(200, {"stream_id": stream.stream_id, "jobs": [self._job(d) for d in rows],
                                "next_after": next_after, "head_seq": stream.head})

    def _accepted(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        cred, err = self._auth(request, "content")
        if err:
            return err
        assert cred is not None
        count = 0
        for did in body.get("delivery_ids") or []:
            d = self.deliveries.get(int(did))
            if d is None or d["profile"] != cred.profile:
                continue
            count += 1
            self.accepted_log.append(int(did))
            if d["stage"] == "queued":
                d["stage"] = "companion_accepted"
                d["stage_times"].setdefault("companion_accepted", self.now_ms())
        return self._json(200, {"accepted": count})

    def _receipts(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        cred, err = self._auth(request, "content")
        if err:
            return err
        assert cred is not None
        applied = 0
        for rec in body.get("receipts") or []:
            self.receipt_log.append(dict(rec))
            d = self.deliveries.get(int(rec["delivery_id"]))
            if d is None or d["profile"] != cred.profile or d["stage"] in TERMINAL:
                continue
            epoch = rec.get("epoch")
            if isinstance(epoch, int) and epoch != d["epoch"]:
                continue
            outcome, stage = rec.get("outcome"), rec.get("stage")
            target = d["stage"]
            if outcome in TERMINAL:
                target = outcome
            elif stage in STAGE_RANK and STAGE_RANK[stage] > STAGE_RANK.get(d["stage"], -1):
                target = stage
            changed = target != d["stage"]
            if changed:
                d["stage"] = target
                d["stage_times"].setdefault(target, parse_ts(rec.get("at")) or self.now_ms())
                if target in TERMINAL:
                    d["outcome"] = target
            for key in ("status_code", "revision", "digest", "detail", "timing"):
                if rec.get(key) is not None:
                    d[key] = rec[key]
                    changed = True
            if changed:
                applied += 1
            device = self.devices.get(("tag", d["tag"]))
            rev = d.get("revision")
            if device is not None and isinstance(rev, int):
                device["desired_revision"] = max(device["desired_revision"], rev)
                if d["stage"] == "displayed" and rev >= device["displayed_revision"]:
                    device["displayed_revision"] = rev
                    device["displayed_digest"] = d.get("digest")
        return self._json(200, {"applied": applied})

    def _previews(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        cred, err = self._auth(request, "content")
        if err:
            return err
        assert cred is not None
        if body.get("kind") not in ("desired", "displayed"):
            return self._error(422, "invalid_kind", "bad kind")
        png = base64.b64decode(body["png_base64"])
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 64 * 1024:
            return self._error(422, "invalid_preview", "not a PNG")
        device = self.devices.get(("tag", body.get("tag_id")))
        if device is None or device["owner_profile"] != cred.profile:
            return self._error(404, "tag_not_found", "No tag with that id belongs to this profile.")
        self.preview_log.append({k: body[k] for k in ("tag_id", "revision", "kind", "delivery_ids")})
        key = (body["tag_id"], body["kind"])
        current = self.previews.get(key)
        if current is not None and current["revision"] > body["revision"]:
            return self._json(200, {"stored": False})
        self.previews[key] = {"revision": body["revision"], "png": png, "delivery_ids": body["delivery_ids"]}
        if body["kind"] == "desired":
            device["desired_revision"] = max(device["desired_revision"], int(body["revision"]))
        return self._json(200, {"stored": True})


__all__ = ["Cred", "FakeCremind", "iso"]
