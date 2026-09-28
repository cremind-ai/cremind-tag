"""Cremind's bootstrap API, as the setup window uses it (docs/setup-api.md §2, docs/connect-setup.md §9.2).

::

    client = BootstrapClient(parse_setup_link(url), installation_key)
    bound = client.bind(computer="Anna's laptop", platform="windows", version="0.2.0")
    client.approve(gateway_identity)          # after the person clicks Approve
    while client.poll()["state"] != "redeeming": ...
    result = client.redeem(key, controller_pub, hardware_sha256, content_sha256)

Every call carries ``Authorization: CremindSetup <session>.<token>`` (the token
from the launch link, never logged) and, from ``bind`` on, an Ed25519 proof by
the installation key over ``"cremind-connect/v1/" ‖ action ‖ 0 ‖ session ‖ 0 ‖
server_nonce ‖ 0 ‖ SHA-256(canonical body)`` — in the body for POST, in
``X-Cremind-Connect-Proof`` for GET.

TLS: an ``https`` link with a ``pin`` trusts exactly the CA whose SHA-256 (of
its DER encoding) is the pin; the CA comes from the server's public
``/ca.pem`` and is refused unless it matches. Without a pin the system trust
store decides. The accepted CA is kept (:attr:`BootstrapClient.ca_pem`) so the
worker trusts the same one.

Blocking (``httpx.Client``): the setup window calls it from a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any

import httpx

from .links import SetupLink

log = logging.getLogger(__name__)

PREFIX = "/api/tag-setup/v1"
PROOF_PREFIX = b"cremind-connect/v1/"
TIMEOUT_S = 15.0


class BootstrapError(RuntimeError):
    """A bootstrap call failed. ``code`` is Cremind's error code (or a local one: ``unreachable``,
    ``tls``, ``pin_mismatch``, ``bad_answer``); the message is written for a person."""

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.body = body or {}

    @property
    def final(self) -> bool:
        """The session can no longer succeed (expired, cancelled, refused): stop and say so."""
        return self.status in (401, 403, 404, 409, 410, 422) and self.code not in ("not_confirmed", "wrong_state")


@dataclass(frozen=True)
class Bound:
    """The answer to ``bind``: what the window shows and what later calls need."""

    session_id: str
    operation: str
    state: str
    expires_at: str | None
    server_origin: str
    server_name: str
    installation_id: str
    authority_pub: bytes
    authority_id: bytes
    profile_name: str
    profile_id: str
    verification_phrase: str
    server_nonce: str
    recover: dict[str, Any] | None

    @property
    def phrase_words(self) -> list[str]:
        return self.verification_phrase.split()


def canonical_body(body: dict[str, Any]) -> bytes:
    """Keys sorted, no spaces, UTF-8, ``proof`` left out (docs/setup-api.md §2)."""
    clean = {k: v for k, v in body.items() if k != "proof"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def proof_message(action: str, session_id: str, server_nonce: str, body: dict[str, Any]) -> bytes:
    return (PROOF_PREFIX + action.encode() + b"\x00" + session_id.encode() + b"\x00" + server_nonce.encode()
            + b"\x00" + hashlib.sha256(canonical_body(body)).digest())


def ca_fingerprint(pem: str | bytes) -> str:
    """SHA-256 (hex) of the DER encoding of the first certificate in ``pem``."""
    text = pem.decode("ascii") if isinstance(pem, bytes) else pem
    der = ssl.PEM_cert_to_DER_cert(_first_certificate(text))
    return hashlib.sha256(der).hexdigest()


def _first_certificate(text: str) -> str:
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    start = text.find(begin)
    stop = text.find(end, start)
    if start < 0 or stop < 0:
        raise ValueError("no PEM certificate")
    return text[start:stop + len(end)] + "\n"


class BootstrapClient:
    """One setup session's calls (see the module docstring). ``installation`` signs the proofs
    (:class:`.installation.Installation`)."""

    def __init__(self, link: SetupLink, installation: Any, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = TIMEOUT_S) -> None:
        self.link = link
        self.installation = installation
        self._transport = transport
        self._timeout = timeout
        self.server_nonce: str | None = None
        self.ca_pem: str | None = None
        self._client: httpx.Client | None = None

    # -- HTTP ----------------------------------------------------------------------------------

    def _verify(self) -> ssl.SSLContext | bool:
        if not self.link.server.startswith("https://") or self._transport is not None:
            return True
        if self.link.pin is None:
            return ssl.create_default_context()
        if self.ca_pem is None:
            self.ca_pem = self._fetch_pinned_ca()
        return ssl.create_default_context(cadata=self.ca_pem)

    def _fetch_pinned_ca(self) -> str:
        """The server's CA from ``/ca.pem``, accepted only when it matches the link's pin."""
        try:
            # Integrity comes from the pin, not from this connection: it cannot verify yet.
            with httpx.Client(verify=False, timeout=self._timeout, follow_redirects=False) as plain:  # noqa: S501
                response = plain.get(self.link.server + "/ca.pem")
        except httpx.HTTPError as exc:
            raise BootstrapError("unreachable", f"Cremind at {self.link.server} could not be reached ({exc}).") \
                from None
        if response.status_code != 200:
            raise BootstrapError("pin_mismatch", "Cremind did not provide its certificate authority. "
                                                 "Start again from Cremind.", status=response.status_code)
        try:
            pem = _first_certificate(response.text)
            fingerprint = ca_fingerprint(pem)
        except (ValueError, UnicodeDecodeError):
            raise BootstrapError("pin_mismatch", "Cremind's certificate authority could not be read.") from None
        if fingerprint != self.link.pin:
            raise BootstrapError("pin_mismatch", "The server's certificate does not match the link. "
                                                 "Start again from Cremind on this network.")
        return pem

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.link.server, verify=self._verify(), timeout=self._timeout,
                                        transport=self._transport, follow_redirects=False,
                                        headers={"Authorization": f"CremindSetup {self.link.session}.{self.link.token}",
                                                 "User-Agent": "cremind-connect"})
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> BootstrapClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _path(self, action: str | None) -> str:
        base = f"{PREFIX}/sessions/{self.link.session}"
        return base if action is None else f"{base}/{action}"

    def _send(self, method: str, action: str | None, *, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = self.client().request(method, self._path(action), json=body, headers=headers)
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
                raise BootstrapError("tls", f"Cremind's certificate is not trusted on this computer ({exc}).") \
                    from None
            raise BootstrapError("unreachable", f"Cremind at {self.link.server} could not be reached ({exc}).") \
                from None
        except httpx.HTTPError as exc:
            raise BootstrapError("unreachable", f"Cremind at {self.link.server} did not answer ({exc}).") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code >= 400:
            code = str(data.get("error") or f"http_{response.status_code}")
            message = str(data.get("message") or f"Cremind answered {response.status_code}.")
            raise BootstrapError(code, message, status=response.status_code, body=data)
        return data

    def _signed(self, action: str, body: dict[str, Any], nonce: str) -> dict[str, Any]:
        proof = self.installation.sign(proof_message(action, self.link.session, nonce, body)).hex()
        return {**body, "proof": proof}

    def _nonce(self) -> str:
        if self.server_nonce is None:
            raise BootstrapError("not_bound", "The setup session is not bound yet.")
        return self.server_nonce

    # -- calls -------------------------------------------------------------------------------------

    def bind(self, *, computer: str, platform: str, version: str) -> Bound:
        inst = self.installation
        body = {"installation": {"id": inst.id, "public_key": inst.public_key.hex(), "computer": computer,
                                 "platform": platform, "version": version}}
        data = self._send("POST", "bind", body=self._signed("bind", body, ""))
        try:
            session, server, profile = data["session"], data["server"], data["profile"]
            bound = Bound(
                session_id=str(session["id"]), operation=str(session["operation"]), state=str(session["state"]),
                expires_at=session.get("expires_at"), server_origin=str(server.get("origin") or self.link.server),
                server_name=str(server.get("name") or "Cremind"), installation_id=str(server["installation_id"]),
                authority_pub=bytes.fromhex(server["authority_pub"]), authority_id=bytes.fromhex(server["authority_id"]),
                profile_name=str(profile["name"]), profile_id=str(profile["id"]),
                verification_phrase=str(data.get("verification_phrase") or ""),
                server_nonce=str(data["server_nonce"]), recover=data.get("recover"))
        except (KeyError, TypeError, ValueError) as exc:
            raise BootstrapError("bad_answer", f"Cremind's answer to bind was not understood ({exc}).") from None
        if bound.session_id != self.link.session or len(bound.authority_pub) != 32:
            raise BootstrapError("bad_answer", "Cremind's answer to bind does not match this setup.")
        self.server_nonce = bound.server_nonce
        return bound

    def poll(self) -> dict[str, Any]:
        """``{state, native_approved, browser_confirmed, error}``."""
        proof = self.installation.sign(proof_message("poll", self.link.session, self._nonce(), {})).hex()
        return self._send("GET", None, headers={"X-Cremind-Connect-Proof": proof})

    def approve(self, gateway: dict[str, Any]) -> dict[str, Any]:
        """The person approved ``gateway`` (its IDENTIFY answer: device_id, ik, fw, proto, board,
        owner_state, gen, authority_id, challenge — hex for bytes)."""
        return self._send("POST", "approve", body=self._signed("approve", {"gateway": gateway}, self._nonce()))

    def redeem(self, *, idempotency_key: str, controller_pub: bytes, hardware_sha256: str,
               content_sha256: str) -> dict[str, Any]:
        body = {"idempotency_key": idempotency_key, "controller_pub": controller_pub.hex(),
                "credentials": {"hardware_sha256": hardware_sha256, "content_sha256": content_sha256}}
        return self._send("POST", "redeem", body=self._signed("redeem", body, self._nonce()))

    def fail(self, code: str, message: str) -> dict[str, Any]:
        body = {"code": code[:64], "message": message[:400]}
        return self._send("POST", "fail", body=self._signed("fail", body, self._nonce()))


def gateway_json(identity: Any) -> dict[str, Any]:
    """A probe's :class:`~.probe.GatewayIdentity` as ``approve`` sends it."""
    return {"device_id": identity.device_id.hex(), "ik": identity.ik.hex(), "role": int(identity.role),
            "proto": int(identity.proto), "fw": identity.fw, "board": int(identity.board),
            "owner_state": int(identity.owner_state), "gen": int(identity.gen),
            "authority_id": identity.authority_id.hex() if identity.authority_id else None,
            "challenge": identity.challenge.hex()}


__all__ = ["BootstrapClient", "BootstrapError", "Bound", "canonical_body", "ca_fingerprint", "gateway_json",
           "proof_message"]
