"""Async client for Cremind's connector API: ``/api/tag-connector/v1/*`` (docs/connector-api.md).

One :class:`ConnectorClient` per credential; every request carries
``Authorization: CremindTag <credential-id>.<secret>``. The companion always
connects outbound, so this is the only way it talks to Cremind.

Errors are typed so the daemon can decide what to do without looking at HTTP
details:

- :class:`ConnectorAuthError` (401, 403): the credential is revoked, invalid or
  of the wrong kind — stop the loop that uses it and report;
- :class:`CursorExpired` (410 ``cursor_expired``): call ``sync``;
- :class:`ConnectorConflict` (409), :class:`ConnectorNotFound` (404),
  :class:`ConnectorRejected` (400/422): the request itself was refused — retrying
  the same request cannot succeed;
- :class:`ConnectorUnavailable` (5xx, 429, network, timeouts): transient — retry
  with :class:`Backoff`;
- :class:`ConnectorTlsError`: the server's certificate is not trusted, an
  ``https://`` URL points at a plain-HTTP server, a plain-HTTP URL points at a
  server that moved to HTTPS, or the CA file is unusable — a configuration
  problem the operator must fix (``cremind-tag connect server``); the daemon
  retries it slowly. An EOF or reset during the TLS handshake (a restarting
  server, a proxy dropping the connection) is :class:`ConnectorUnavailable`.

TLS: the system trust store (``ssl.create_default_context()``, which reads the
Windows certificate store / the OS bundle), plus an optional CA bundle file for
a Cremind with a private CA (``--ca-file``). Secrets never appear in logs or in
``repr``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import __version__
from .models import (
    Command,
    EventsPage,
    HeartbeatResult,
    InventoryResult,
    MalformedResponse,
    ReceiptsResult,
    SyncResult,
    WhoAmI,
)

log = logging.getLogger(__name__)

SCHEME = "CremindTag"
API_PREFIX = "/api/tag-connector/v1"
CREDENTIAL_PREFIX = "tagc_"
MAX_COMMAND_WAIT_S = 30
DEFAULT_TIMEOUT_S = 30.0
EVENTS_PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class CredentialError(ValueError):
    """A connector credential could not be parsed."""


@dataclass(frozen=True, slots=True)
class Credential:
    """``<credential-id>.<secret>`` — the public id and the secret shown once at creation."""

    credential_id: str
    secret: str

    @property
    def authorization(self) -> str:
        return f"{SCHEME} {self.credential_id}.{self.secret}"

    @property
    def value(self) -> str:
        """What the secret store keeps: ``<id>.<secret>``."""
        return f"{self.credential_id}.{self.secret}"

    def __repr__(self) -> str:
        return f"Credential({self.credential_id!r}, secret=***)"

    __str__ = __repr__


def parse_credential(text: str) -> Credential:
    """Accept ``CremindTag <id>.<secret>``, ``Authorization: CremindTag …`` or ``<id>.<secret>``."""
    value = (text or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith(SCHEME.lower() + " "):
        value = value[len(SCHEME) + 1:].strip()
    cred_id, sep, secret = value.partition(".")
    if not sep or not cred_id.startswith(CREDENTIAL_PREFIX) or len(cred_id) < len(CREDENTIAL_PREFIX) + 8:
        raise CredentialError("expected 'CremindTag tagc_….<secret>' (the authorization value Cremind showed once)")
    if not secret or any(c.isspace() for c in secret) or len(secret) < 16:
        raise CredentialError("the credential's secret part is missing or malformed")
    return Credential(cred_id, secret)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """A connector request failed."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 body: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body: Mapping[str, Any] = body or {}


class ConnectorAuthError(ConnectorError):
    """401/403: revoked, invalid, or wrong kind of credential. Stop and report."""


class ConnectorConflict(ConnectorError):
    """409 (``already_claimed``, ``already_completed``); ``body['command']`` is the current command."""


class ConnectorNotFound(ConnectorError):
    """404 (``command_not_found``, ``tag_not_found``)."""


class ConnectorRejected(ConnectorError):
    """400/422: Cremind refused the request's content (retrying it cannot help)."""


class ConnectorUnavailable(ConnectorError):
    """5xx, 429, a network failure or a timeout: retry later."""


class ConnectorTlsError(ConnectorError):
    """TLS/scheme misconfiguration (untrusted certificate, the server moved to HTTPS)."""


class CursorExpired(ConnectorError):
    """410 ``cursor_expired``: the cursor is outside the retained history — call ``sync``."""

    def __init__(self, message: str, *, oldest_seq: int | None, head_seq: int | None, stream_id: str | None,
                 body: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, status=410, code="cursor_expired", body=body)
        self.oldest_seq = oldest_seq
        self.head_seq = head_seq
        self.stream_id = stream_id


PERMANENT_ERRORS = (ConnectorAuthError, ConnectorTlsError)


# ---------------------------------------------------------------------------
# Back-off
# ---------------------------------------------------------------------------


class Backoff:
    """Bounded exponential back-off with jitter: ``initial * factor**n`` capped at ``maximum``, ±``jitter``."""

    def __init__(self, initial: float = 1.0, maximum: float = 60.0, *, factor: float = 2.0, jitter: float = 0.2,
                 rng: random.Random | None = None) -> None:
        if initial <= 0 or maximum <= 0 or factor < 1 or not 0 <= jitter < 1:
            raise ValueError("invalid back-off parameters")
        self.initial = min(initial, maximum)
        self.maximum = maximum
        self.factor = factor
        self.jitter = jitter
        self.attempts = 0
        self._rng = rng or random.Random()

    def next(self) -> float:
        base = min(self.maximum, self.initial * self.factor ** self.attempts)
        self.attempts += 1
        return base * (1 + self._rng.uniform(-self.jitter, self.jitter))

    def reset(self) -> None:
        self.attempts = 0

    @staticmethod
    def delay_for(attempt: int, initial: float, maximum: float, *, factor: float = 2.0, jitter: float = 0.2,
                  rng: random.Random | None = None) -> float:
        """The delay before retry number ``attempt`` (0-based), for persisted retry counters."""
        base = min(maximum, initial * factor ** max(0, attempt))
        return base * (1 + (rng or random).uniform(-jitter, jitter))


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def ssl_context(ca_file: Path | str | None = None) -> ssl.SSLContext:
    """System trust, plus ``ca_file`` (a PEM bundle) for a Cremind behind a private CA."""
    context = ssl.create_default_context()
    if ca_file:
        path = Path(ca_file)
        if not path.is_file():
            raise ConnectorTlsError(f"CA file {path} does not exist")
        try:
            context.load_verify_locations(cafile=str(path))
        except (ssl.SSLError, OSError) as exc:
            raise ConnectorTlsError(f"CA file {path} is not a PEM certificate bundle: {exc}") from None
    return context


def _ssl_cause(exc: BaseException) -> ssl.SSLError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return current
        current = current.__cause__ or current.__context__
    return None


def normalize_base_url(url: str) -> str:
    """``http(s)://host[:port][/prefix]`` without a trailing slash; the connector prefix is added per request."""
    text = (url or "").strip()
    if not text:
        raise ValueError("no Cremind URL configured (cremind-tag connect server URL)")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    path = parts.path.rstrip("/")
    if path.endswith(API_PREFIX):
        path = path[: -len(API_PREFIX)]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ConnectorClient:
    """Typed methods over the connector endpoints for ONE credential (see the module docstring)."""

    def __init__(self, base_url: str, credential: Credential, *, ca_file: Path | str | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = normalize_base_url(base_url)
        self.credential = credential
        self.ca_file = Path(ca_file) if ca_file else None
        self.timeout = timeout
        verify: ssl.SSLContext | bool = True
        if transport is None and self.base_url.startswith("https://"):
            verify = ssl_context(self.ca_file)
        self._http = httpx.AsyncClient(
            base_url=self.base_url + API_PREFIX,
            headers={"Authorization": credential.authorization, "User-Agent": f"cremind-tag/{__version__}",
                     "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            verify=verify,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def credential_id(self) -> str:
        return self.credential.credential_id

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ConnectorClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"ConnectorClient({self.base_url!r}, {self.credential_id!r})"

    # -- transport ---------------------------------------------------------------

    async def _request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
                       timeout: float | None = None) -> Any:
        where = f"{method} {path}"
        try:
            response = await self._http.request(method, path, json=json, params=params,
                                                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException as exc:
            raise ConnectorUnavailable(f"{where}: timed out ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:
            raise await self._transport_error(where, exc) from None
        return self._decode(where, response)

    async def _transport_error(self, where: str, exc: httpx.HTTPError) -> ConnectorError:
        cause = _ssl_cause(exc)
        if cause is not None:
            # Only configuration problems are ConnectorTlsError: an untrusted certificate, or an https://
            # URL for a server that speaks plain HTTP. An EOF, a reset or any other failure during the
            # handshake is what a restarting server or a dropping proxy produces: retry it.
            if isinstance(cause, ssl.SSLCertVerificationError):
                hint = (f" (the CA file {self.ca_file} does not cover it)" if self.ca_file
                        else "; pass the Cremind CA with `cremind-tag connect server URL --ca-file CA.pem`")
                return ConnectorTlsError(f"{where}: the certificate of {self.base_url} is not trusted: "
                                         f"{getattr(cause, 'verify_message', None) or cause}{hint}")
            if self.base_url.startswith("https://") and "WRONG_VERSION_NUMBER" in str(cause):
                return ConnectorTlsError(f"{where}: {self.base_url} does not speak TLS; is the URL http://…?")
            return ConnectorUnavailable(f"{where}: TLS handshake with {self.base_url} interrupted: "
                                        f"{type(cause).__name__}: {cause}")
        if self.base_url.startswith("http://") and isinstance(exc, httpx.RemoteProtocolError | httpx.ReadError):
            if await self._https_answers():
                return ConnectorTlsError(self._moved_to_https_message(where))
        return ConnectorUnavailable(f"{where}: {type(exc).__name__}: {exc}")

    def _moved_to_https_message(self, where: str, location: str | None = None) -> str:
        https = location or "https://" + self.base_url[len("http://"):]
        return (f"{where}: Cremind at {self.base_url} now serves HTTPS ({https}); run "
                f"`cremind-tag connect server {https}` (add --ca-file for a private CA)")

    async def _https_answers(self) -> bool:
        """Whether the same host:port completes a TLS handshake (a plain-HTTP URL for an HTTPS server)."""
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        port = parts.port or 80
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=context,
                                                                       server_hostname=host or None), 5.0)
        except (OSError, TimeoutError, ssl.SSLError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    def _decode(self, where: str, response: httpx.Response) -> Any:
        status = response.status_code
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            if location.startswith("https://") and self.base_url.startswith("http://"):
                raise ConnectorTlsError(self._moved_to_https_message(where, location.split(API_PREFIX)[0]),
                                        status=status)
            raise ConnectorRejected(f"{where}: unexpected redirect {status} to {location or '?'}", status=status)
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if status < 300:
            if body is None:
                raise ConnectorUnavailable(f"{where}: HTTP {status} without a JSON body", status=status)
            return body
        obj = body if isinstance(body, Mapping) else {}
        code = obj.get("error") if isinstance(obj.get("error"), str) else None
        detail = obj.get("detail") or obj.get("message") or (response.text[:200] if body is None else "")
        message = f"{where}: HTTP {status}" + (f" {code}" if code else "") + (f": {detail}" if detail else "")
        if status == 400 and self.base_url.startswith("http://") and "https" in response.text[:500].lower():
            raise ConnectorTlsError(self._moved_to_https_message(where), status=status)
        if status in (401, 403):
            raise ConnectorAuthError(message, status=status, code=code, body=obj)
        if status == 410 and code == "cursor_expired":
            def opt(key: str) -> Any:
                value = obj.get(key)
                return value if isinstance(value, int | str) and not isinstance(value, bool) else None

            raise CursorExpired(message, oldest_seq=opt("oldest_seq"), head_seq=opt("head_seq"),
                                stream_id=opt("stream_id"), body=obj)
        if status == 409:
            raise ConnectorConflict(message, status=status, code=code, body=obj)
        if status == 404:
            raise ConnectorNotFound(message, status=status, code=code, body=obj)
        if status == 429 or status >= 500:
            raise ConnectorUnavailable(message, status=status, code=code, body=obj)
        raise ConnectorRejected(message, status=status, code=code, body=obj)

    @staticmethod
    def _parse[T](where: str, parser: Callable[[Any], T], body: Any) -> T:
        try:
            return parser(body)
        except MalformedResponse as exc:
            raise ConnectorUnavailable(f"{where}: malformed response: {exc}") from None

    # -- any credential ------------------------------------------------------------

    async def whoami(self) -> WhoAmI:
        return self._parse("whoami", WhoAmI.from_json, await self._request("GET", "/whoami"))

    # -- hardware credential ------------------------------------------------------

    async def inventory(self, body: Mapping[str, Any]) -> InventoryResult:
        return self._parse("inventory", InventoryResult.from_json, await self._request("POST", "/inventory",
                                                                                          json=dict(body)))

    async def heartbeat(self, body: Mapping[str, Any]) -> HeartbeatResult:
        return self._parse("heartbeat", HeartbeatResult.from_json, await self._request("POST", "/heartbeat",
                                                                                          json=dict(body)))

    async def commands(self, wait: int = 25) -> list[Command]:
        """Long-poll (``wait`` ≤ 30 s) for queued hardware commands."""
        wait = max(0, min(int(wait), MAX_COMMAND_WAIT_S))
        body = await self._request("GET", "/commands", params={"wait": wait}, timeout=wait + self.timeout)
        items = body.get("commands") if isinstance(body, Mapping) else None
        return [self._parse("commands", Command.from_json, c) for c in (items or [])]

    async def claim(self, command_id: str) -> Command:
        """Claim a command; :class:`ConnectorConflict` (``already_claimed``) when it is not queued any more."""
        return self._parse("claim", Command.from_json, await self._request("POST", f"/commands/{command_id}/claim",
                                                                             json={}))

    async def result(self, command_id: str, status: str, result: Mapping[str, Any] | None = None,
                     error: str | None = None) -> dict[str, Any]:
        """Report a command's outcome (idempotent for the same status)."""
        if status not in ("succeeded", "failed"):
            raise ValueError("status must be 'succeeded' or 'failed'")
        payload: dict[str, Any] = {"status": status}
        if result is not None:
            payload["result"] = dict(result)
        if error is not None:
            payload["error"] = error
        body = await self._request("POST", f"/commands/{command_id}/result", json=payload)
        return dict(body) if isinstance(body, Mapping) else {}

    # -- content credential ---------------------------------------------------------

    async def sync(self, cursor: int | None) -> SyncResult:
        return self._parse("sync", SyncResult.from_json, await self._request("POST", "/sync", json={"cursor": cursor}))

    async def events(self, after: int, limit: int = EVENTS_PAGE_LIMIT) -> EventsPage:
        body = await self._request("GET", "/events", params={"after": max(0, int(after)),
                                                              "limit": max(1, min(int(limit), EVENTS_PAGE_LIMIT))})
        return self._parse("events", EventsPage.from_json, body)

    async def accepted(self, through_seq: int, delivery_ids: list[int]) -> int:
        body = await self._request("POST", "/accepted", json={"through_seq": through_seq,
                                                              "delivery_ids": list(delivery_ids)})
        value = body.get("accepted") if isinstance(body, Mapping) else None
        return value if isinstance(value, int) else 0

    async def receipts(self, receipts: list[Mapping[str, Any]]) -> ReceiptsResult:
        """``{applied, rejected?}``; each rejection names the delivery and ``epoch_mismatch``, ``unknown``,
        ``terminal`` or ``not_owned``."""
        body = await self._request("POST", "/receipts", json={"receipts": [dict(r) for r in receipts]})
        return ReceiptsResult.from_json(body)

    async def previews(self, *, tag_id: str, revision: int, kind: str, png_base64: str,
                       delivery_ids: list[int], epoch: int | None = None) -> bool:
        """Store a preview; ``epoch`` is the tag epoch it was rendered for (409 ``epoch_mismatch`` when that
        is not the tag's current one)."""
        payload: dict[str, Any] = {"tag_id": tag_id, "revision": revision, "kind": kind, "png_base64": png_base64,
                                   "delivery_ids": list(delivery_ids)}
        if epoch is not None:
            payload["epoch"] = epoch
        body = await self._request("POST", "/previews", json=payload)
        return bool(body.get("stored")) if isinstance(body, Mapping) else False

    # -- v2: private workers (docs/setup-api.md §3), hardware credential -------------

    async def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        body = await self._request(method, path, **kwargs)
        return dict(body) if isinstance(body, Mapping) else {}

    async def lease(self) -> dict[str, Any]:
        """Renew the 60 s authorization lease: ``{expires_at, ttl_s, renew_s, state, paused, generation}``."""
        return await self._object("POST", "/lease", json={})

    async def worker_state(self) -> dict[str, Any]:
        """``{generation, state, paused, bindings, revoked, operations}`` — reconcile before draining."""
        return await self._object("GET", "/state")

    async def operation(self, operation_id: str) -> dict[str, Any]:
        """``{operation: {id, kind, state, stage, args, setup_secret, owner, expires_at}}``."""
        out = await self._object("GET", f"/operations/{operation_id}")
        return dict(out.get("operation") or {})

    async def progress(self, operation_id: str, **fields: Any) -> dict[str, Any]:
        """Report ``stage``/``detail``/``state``/``candidates``/``device``/``devices``/``result``/``error``."""
        body = {k: v for k, v in fields.items() if v is not None}
        out = await self._object("POST", f"/operations/{operation_id}/progress", json=body)
        return dict(out.get("operation") or {})

    async def grant(self, *, operation_id: str, op: str, device_id: bytes, role: str, gen_from: int,
                    challenge: bytes, ik: bytes | None = None) -> tuple[bytes, bytes, bytes]:
        """A server-signed grant: ``(grant, sig, authority_pub)``."""
        body: dict[str, Any] = {"operation_id": operation_id, "op": op, "device_id": device_id.hex(), "role": role,
                                "gen_from": gen_from, "challenge": challenge.hex()}
        if ik is not None:
            body["ik"] = ik.hex()
        out = await self._object("POST", "/grants", json=body)
        try:
            return bytes.fromhex(out["grant"]), bytes.fromhex(out["sig"]), bytes.fromhex(out["authority_pub"])
        except (KeyError, TypeError, ValueError):
            raise ConnectorUnavailable("grants: malformed response") from None

    async def vault_put(self, subject: str, state: Mapping[str, Any], *, stage: str, generation: int,
                        expected_version: int | None) -> int:
        """Save one version (compare-and-set); :class:`ConnectorConflict` ``version_conflict`` carries
        the current ``version`` in its body."""
        out = await self._object("PUT", f"/vault/{subject}", json={
            "expected_version": expected_version, "stage": stage, "generation": generation, "state": dict(state)})
        version = out.get("version")
        if not isinstance(version, int):
            raise ConnectorUnavailable("vault: malformed response")
        return version

    async def vault_get(self) -> list[dict[str, Any]]:
        """The recovery entries (only while a recovery of this worker is open)."""
        out = await self._object("GET", "/vault")
        return [dict(e) for e in out.get("entries") or [] if isinstance(e, Mapping)]


__all__ = [
    "API_PREFIX", "PERMANENT_ERRORS", "SCHEME", "Backoff", "ConnectorAuthError", "ConnectorClient",
    "ConnectorConflict", "ConnectorError", "ConnectorNotFound", "ConnectorRejected", "ConnectorTlsError",
    "ConnectorUnavailable", "Credential", "CredentialError", "CursorExpired", "normalize_base_url",
    "parse_credential", "ssl_context",
]
