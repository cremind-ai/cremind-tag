"""The ``[cremind]`` configuration section: where Cremind is and which credentials to use.

::

    [cremind]
    url = "https://cremind.example.org"         # env CREMIND_TAG_CREMIND_URL
    ca_file = "C:/certs/cremind-ca.pem"          # a private CA (optional)
    hardware_credential = "tagc_…"               # the companion's hardware credential id
    content_credentials = ["tagc_…", "tagc_…"]   # one per profile that routes content here

Only credential ids (public) live in the file; their secrets are in the secret
store (``cremind_tag.secrets``, key ``credential:<id>``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ..config import ENV_PREFIX, ConfigError, register_section


@register_section
@dataclass
class CremindSettings:
    """Cremind server and connector credentials."""

    SECTION: ClassVar[str] = "cremind"
    ENV_PREFIX: ClassVar[str] = ENV_PREFIX + "CREMIND_"

    url: str | None = None
    """Cremind base URL (``https://host[:port]``)."""
    ca_file: Path | None = None
    """PEM bundle of a private CA that signed Cremind's certificate (system trust is always used too)."""
    hardware_credential: str | None = None
    """Credential id of the companion's hardware credential."""
    content_credentials: list[str] = field(default_factory=list)
    """Credential ids of content credentials (one per profile)."""
    timeout_s: float = 30.0
    """HTTP timeout per request (the command long-poll adds its wait)."""

    def validate(self) -> None:
        if self.url is not None and self.url.strip() and "://" in self.url \
                and not self.url.strip().lower().startswith(("http://", "https://")):
            raise ConfigError("cremind.url must be an http:// or https:// URL")
        if self.timeout_s <= 0:
            raise ConfigError("cremind.timeout_s must be positive")
        for cred in [self.hardware_credential, *self.content_credentials]:
            if cred is not None and not cred.startswith("tagc_"):
                raise ConfigError(f"cremind credential ids start with 'tagc_' (got {cred!r})")


__all__ = ["CremindSettings"]
