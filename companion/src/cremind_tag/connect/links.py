"""The ``cremind-connect://setup`` launch link (docs/connect-setup.md §11.4, §8.1).

::

    cremind-connect://setup?v=1&server=<origin>&session=<uuid>&token=<capability>[&pin=<sha256 hex>]

The link only *starts* a setup: it grants no hardware access by itself (the
person approves in Connect's own window and confirms the phrase in Cremind).
Parsing is strict because the link arrives from a browser:

- scheme ``cremind-connect``, host ``setup``, no path beyond ``/``, no fragment;
- ``v`` must be ``1`` (a newer link asks for a newer Connect);
- ``server`` is a bare ``http``/``https`` origin — ``scheme://host[:port]`` with
  no user information, path, query or fragment — normalised to lower case,
  without a default port (what a browser's ``location.origin`` gives);
- ``session`` is a UUID, ``token`` 16–512 URL-safe characters;
- ``pin`` (optional) is 64 hex characters, the SHA-256 of the CA certificate
  Cremind's HTTPS uses; only meaningful (and accepted) with ``https``;
- a parameter given twice is refused; unknown parameters are ignored, so a later
  ``v=1`` link may add optional ones.

Every refusal is a :class:`LinkError` with a ``code`` and a sentence a person
can read.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

SCHEME = "cremind-connect"
MAX_LINK_LEN = 4096
SUPPORTED_VERSION = "1"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,512}")
_PIN = re.compile(r"[0-9A-Fa-f]{64}")
_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class LinkError(ValueError):
    """A launch link Connect refuses (``code`` for programs, the message for people)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SetupLink:
    version: int
    server: str
    """The Cremind origin, e.g. ``https://cremind.example.org:1180``."""
    session: str
    """The setup session id (canonical lower-case UUID)."""
    token: str
    """The setup capability (secret: never log it)."""
    pin: str | None = None
    """SHA-256 of Cremind's CA certificate (lower-case hex), when it serves HTTPS with its own CA."""

    def __repr__(self) -> str:  # the token is a credential
        return f"SetupLink(server={self.server!r}, session={self.session!r}, pin={self.pin!r})"

    def to_url(self) -> str:
        from urllib.parse import urlencode

        params = {"v": str(self.version), "server": self.server, "session": self.session, "token": self.token}
        if self.pin:
            params["pin"] = self.pin
        return f"{SCHEME}://setup?{urlencode(params)}"


def validate_origin(origin: str) -> str:
    """The normalised origin, or :class:`LinkError` (see the module docstring)."""
    if not isinstance(origin, str) or not origin:
        raise LinkError("bad_server", "The link does not say which Cremind server to use.")
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in origin) or not origin.isascii():
        raise LinkError("bad_server", "The Cremind server address in the link is not valid.")
    parts = urlsplit(origin)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise LinkError("bad_server", "The Cremind server address must start with https:// or http://.")
    if parts.path or parts.query or parts.fragment or "?" in origin or "#" in origin:
        raise LinkError("bad_server", "The Cremind server address must be just an origin "
                                      "(like https://cremind.example.org), without a path.")
    netloc = parts.netloc
    if "@" in netloc:
        raise LinkError("bad_server", "The Cremind server address must not contain a user name or password.")
    try:
        port = parts.port
    except ValueError:
        raise LinkError("bad_server", "The port in the Cremind server address is not valid.") from None
    host = parts.hostname or ""
    if not host:
        raise LinkError("bad_server", "The Cremind server address has no host name.")
    if netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise LinkError("bad_server", "The Cremind server address is not a valid IPv6 address.") from None
        shown = f"[{host.lower()}]"
    else:
        labels = host.rstrip(".").split(".") if host != "." else []
        if not labels or not all(_LABEL.fullmatch(label) for label in labels) or len(host) > 253:
            raise LinkError("bad_server", "The host name in the Cremind server address is not valid.")
        shown = host.lower()
    if port is not None and not 0 < port < 65536:
        raise LinkError("bad_server", "The port in the Cremind server address is not valid.")
    if port is None and netloc.endswith(":"):
        raise LinkError("bad_server", "The port in the Cremind server address is empty.")
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{shown}:{port}"
    return f"{scheme}://{shown}"


def parse_setup_link(url: str) -> SetupLink:
    """Validate a launch link (see the module docstring)."""
    if not isinstance(url, str) or not url.strip():
        raise LinkError("empty", "No link was given.")
    url = url.strip()
    if len(url) > MAX_LINK_LEN:
        raise LinkError("too_long", "The link is too long to be a Cremind Connect link.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise LinkError("malformed", "The link contains control characters.")
    parts = urlsplit(url)
    if parts.scheme.lower() != SCHEME:
        raise LinkError("wrong_scheme", "This is not a Cremind Connect link.")
    if parts.netloc.lower() != "setup" or parts.path not in ("", "/"):
        raise LinkError("unknown_action", "This Cremind Connect link asks for something this version cannot do.")
    if parts.fragment:
        raise LinkError("malformed", "The link has an unexpected '#' part.")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise LinkError("malformed", "The link's parameters are not readable.") from None
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise LinkError("malformed", f"The link gives '{key}' more than once.")
        params[key] = value
    version = params.get("v")
    if version is None:
        raise LinkError("malformed", "The link has no version (v).")
    if version != SUPPORTED_VERSION:
        raise LinkError("unsupported_version",
                        "This link needs a newer Cremind Connect. Install the latest version and try again.")
    server = validate_origin(params.get("server", ""))
    session_text = params.get("session", "")
    try:
        session = str(uuid.UUID(session_text))
    except ValueError:
        raise LinkError("bad_session", "The link's setup session is not valid.") from None
    if session_text.lower() not in (session, session.replace("-", ""), "{" + session + "}"):
        raise LinkError("bad_session", "The link's setup session is not valid.")
    token = params.get("token", "")
    if not _TOKEN.fullmatch(token):
        raise LinkError("bad_token", "The link's setup code is missing or damaged. Start again from Cremind.")
    pin = params.get("pin")
    if pin is not None:
        if not _PIN.fullmatch(pin):
            raise LinkError("bad_pin", "The link's certificate fingerprint is not valid.")
        if not server.startswith("https://"):
            raise LinkError("bad_pin", "A certificate fingerprint only makes sense for an https:// server.")
        pin = pin.lower()
    return SetupLink(int(version), server, session, token, pin)


def redact(url: str) -> str:
    """A link safe to log: the token is replaced."""
    return re.sub(r"(?i)([?&]token=)[^&#]*", r"\1…", url)


__all__ = ["MAX_LINK_LEN", "SCHEME", "LinkError", "SetupLink", "parse_setup_link", "redact", "validate_origin"]
