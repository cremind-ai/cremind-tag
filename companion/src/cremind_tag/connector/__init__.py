"""The Cremind connector: typed async client, models and the ``[cremind]`` config section (docs/connector-api.md)."""

from .client import (
    API_PREFIX,
    PERMANENT_ERRORS,
    Backoff,
    ConnectorAuthError,
    ConnectorClient,
    ConnectorConflict,
    ConnectorError,
    ConnectorNotFound,
    ConnectorRejected,
    ConnectorTlsError,
    ConnectorUnavailable,
    Credential,
    CredentialError,
    CursorExpired,
    normalize_base_url,
    parse_credential,
    ssl_context,
)
from .models import (
    Assignment,
    Command,
    EventsPage,
    HeartbeatResult,
    InventoryResult,
    Job,
    MalformedResponse,
    ProfileSettings,
    Receipt,
    ReceiptsResult,
    Rejection,
    SyncResult,
    TagInfo,
    WhoAmI,
    iso,
    iso_now,
    parse_tag_hw_id,
    parse_time,
    tag_hw_id,
)
from .settings import CremindSettings

__all__ = [
    "API_PREFIX", "PERMANENT_ERRORS", "Assignment", "Backoff", "Command", "ConnectorAuthError", "ConnectorClient",
    "ConnectorConflict", "ConnectorError", "ConnectorNotFound", "ConnectorRejected", "ConnectorTlsError",
    "ConnectorUnavailable", "CremindSettings", "Credential", "CredentialError", "CursorExpired", "EventsPage",
    "HeartbeatResult", "InventoryResult", "Job", "MalformedResponse", "ProfileSettings", "Receipt", "ReceiptsResult", "Rejection", "SyncResult",
    "TagInfo", "WhoAmI", "iso", "iso_now", "normalize_base_url", "parse_credential", "parse_tag_hw_id",
    "parse_time", "ssl_context", "tag_hw_id",
]
