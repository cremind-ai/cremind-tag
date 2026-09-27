"""The companion's local SQLite database: versioned migrations + the hardware inventory.

Connection settings: WAL journal, ``synchronous=FULL`` (a committed row survives
power loss, which the gateway's ACK-after-commit contract relies on), foreign
keys on, a busy timeout. One connection per :class:`Database`, opened in
autocommit mode; every write goes through :meth:`Database.transaction`
(``BEGIN IMMEDIATE`` ... ``COMMIT``). Methods are synchronous and thread-safe;
async code calls them with :meth:`Database.run` (a worker thread) so the event
loop never blocks on disk.

Schema versions are a single linear sequence recorded in ``PRAGMA
user_version`` and in ``schema_migrations`` (version, name, applied_at). The
core owns version 1 (the inventory below). Other components append their own
versions (the delivery queue is version 2 onwards) by passing
``extra_migrations`` to :meth:`Database.open`; a database written by a newer
build (unknown version) or whose recorded migration names differ from this
build's is refused with :class:`SchemaError`.

Schema v1 — local inventory:

- ``gateways``: one row per gateway ever seen (``hw_id``, port, boot id, fw).
- ``bridges``: mesh nodes by device UUID (``hw_id`` = ``br-<uuid hex>``) with
  their unicast address, name, fw, active font pack id, flash size and whether
  ``CONFIGURE_NODE`` succeeded.
- ``tags``: enrolled tags — board, panel geometry and plane encoding, the
  reference of the secret in the secret store (never the secret, never
  ``K_epoch``, which is derived on demand), the current assignment
  ``(epoch, bridge_addr)`` and ``last_revision``, the highest display revision
  allocated for the tag (:meth:`Database.allocate_revision`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, cast

BUSY_TIMEOUT_MS = 5000


class SchemaError(RuntimeError):
    """The database schema does not match what this build knows."""


class NotFoundError(LookupError):
    """No inventory row with that key."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema step. ``apply`` is SQL (may hold several statements) or a callable.

    A callable receives the connection inside the migration's transaction and
    must not commit or roll back.
    """

    version: int
    name: str
    apply: str | Callable[[sqlite3.Connection], None]


_V1_INVENTORY = """
CREATE TABLE gateways (
    hw_id          TEXT PRIMARY KEY,
    port           TEXT,
    boot_id        INTEGER,
    fw             TEXT,
    build          TEXT,
    board          INTEGER,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);

CREATE TABLE bridges (
    uuid           TEXT PRIMARY KEY CHECK (length(uuid) = 32),
    addr           INTEGER UNIQUE CHECK (addr IS NULL OR addr BETWEEN 1 AND 32767),
    name           TEXT NOT NULL DEFAULT '',
    elements       INTEGER NOT NULL DEFAULT 1,
    fw             TEXT,
    board          INTEGER,
    fontpack_id    TEXT CHECK (fontpack_id IS NULL OR length(fontpack_id) = 16),
    flash_size     INTEGER,
    configured     INTEGER NOT NULL DEFAULT 0 CHECK (configured IN (0, 1)),
    gateway_hw_id  TEXT REFERENCES gateways(hw_id) ON DELETE SET NULL,
    provisioned_at TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE tags (
    tag_id         INTEGER PRIMARY KEY CHECK (tag_id BETWEEN 1 AND 4294967294),
    name           TEXT NOT NULL DEFAULT '',
    board          INTEGER NOT NULL,
    panel          INTEGER NOT NULL,
    width          INTEGER NOT NULL CHECK (width BETWEEN 1 AND 2048),
    height         INTEGER NOT NULL CHECK (height BETWEEN 1 AND 2048),
    planes         INTEGER NOT NULL CHECK (planes IN (1, 2)),
    plane_flags    INTEGER NOT NULL DEFAULT 0 CHECK (plane_flags BETWEEN 0 AND 255),
    fw             TEXT,
    enrolled_at    TEXT NOT NULL,
    secret_ref     TEXT NOT NULL,
    protected      INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    epoch          INTEGER NOT NULL DEFAULT 0 CHECK (epoch BETWEEN 0 AND 4294967295),
    bridge_addr    INTEGER REFERENCES bridges(addr) ON DELETE SET NULL ON UPDATE CASCADE,
    last_revision  INTEGER NOT NULL DEFAULT 0 CHECK (last_revision BETWEEN 0 AND 4294967295),
    updated_at     TEXT NOT NULL
);

CREATE INDEX ix_tags_bridge_addr ON tags(bridge_addr);
"""

CORE_MIGRATIONS: tuple[Migration, ...] = (Migration(1, "inventory", _V1_INVENTORY),)


def utc_now() -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix (the connector API's format)."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Inventory records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GatewayRecord:
    hw_id: str
    port: str | None = None
    boot_id: int | None = None
    fw: str | None = None
    build: str | None = None
    board: int | None = None
    first_seen_at: str = ""
    last_seen_at: str = ""


@dataclass(frozen=True, slots=True)
class BridgeRecord:
    uuid: str  # 32 lowercase hex digits
    addr: int | None = None
    name: str = ""
    elements: int = 1
    fw: str | None = None
    board: int | None = None
    fontpack_id: str | None = None  # 16 lowercase hex digits
    flash_size: int | None = None
    configured: bool = False
    gateway_hw_id: str | None = None
    provisioned_at: str | None = None
    updated_at: str = ""

    @property
    def hw_id(self) -> str:
        """Connector API identifier (docs/connector-api.md)."""
        return f"br-{self.uuid}"


@dataclass(frozen=True, slots=True)
class TagRecord:
    tag_id: int
    board: int
    panel: int
    width: int
    height: int
    planes: int
    plane_flags: int
    secret_ref: str
    enrolled_at: str = ""
    name: str = ""
    fw: str | None = None
    protected: bool = False
    epoch: int = 0
    bridge_addr: int | None = None
    last_revision: int = 0
    updated_at: str = ""

    @property
    def hw_id(self) -> str:
        """Connector API identifier: 8 upper-case hex digits."""
        return f"{self.tag_id:08X}"


def _row_to[R](cls: type[R], row: sqlite3.Row) -> R:
    names = {f.name for f in fields(cast(Any, cls))}
    values = {k: row[k] for k in row.keys() if k in names}
    for key in ("configured", "protected"):
        if key in values:
            values[key] = bool(values[key])
    return cls(**values)


def normalize_uuid(uuid: bytes | str) -> str:
    """Mesh device UUID as 32 lowercase hex digits (accepts bytes, hex, or dashed form)."""
    if isinstance(uuid, bytes | bytearray):
        if len(uuid) != 16:
            raise ValueError("a mesh UUID is 16 bytes")
        return bytes(uuid).hex()
    text = uuid.replace("-", "").strip().lower()
    if len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError(f"not a mesh UUID: {uuid!r}")
    return text


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _check_migrations(migrations: Sequence[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda m: m.version)
    for expected, migration in enumerate(ordered, start=1):
        if migration.version != expected:
            raise SchemaError(f"migration versions must be 1..n without gaps or duplicates; got {migration.version} "
                              f"where {expected} was expected")
    return ordered


class Database:
    """The companion database (see the module docstring)."""

    def __init__(self, path: Path | str, migrations: Sequence[Migration] = CORE_MIGRATIONS) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.migrations = _check_migrations(migrations)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), autocommit=True, check_same_thread=False,
                                     timeout=BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise SchemaError(f"could not enable WAL on {self.path} (journal_mode={mode})")
        self._conn.execute("PRAGMA synchronous = FULL")

    @classmethod
    def open(cls, path: Path | str, *, extra_migrations: Iterable[Migration] = ()) -> Database:
        """Open (creating if needed) and migrate to the newest known version."""
        db = cls(path, [*CORE_MIGRATIONS, *extra_migrations])
        try:
            db.migrate()
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transactions -------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on error); re-entrant calls join."""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection under the lock, for reads (autocommit: each statement is consistent)."""
        with self._lock:
            yield self._conn

    async def run[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking database call in a worker thread."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- migrations -----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def applied_migrations(self) -> list[tuple[int, str, str]]:
        with self._lock:
            if not self._has_table("schema_migrations"):
                return []
            rows = self._conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            return [(r[0], r[1], r[2]) for r in rows]

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
        return row is not None

    def migrate(self) -> int:
        """Apply every pending migration, each in its own transaction; return the version."""
        with self._lock:
            current = self.schema_version
            known = {m.version: m for m in self.migrations}
            newest = max(known, default=0)
            if current > newest:
                raise SchemaError(f"{self.path} has schema version {current}; this build knows up to {newest}")
            with self.transaction() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                             " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for version, name, _ in self.applied_migrations():
                if version in known and known[version].name != name:
                    raise SchemaError(f"{self.path}: migration {version} is {name!r} in the database but "
                                      f"{known[version].name!r} in this build")
            for migration in self.migrations:
                if migration.version <= current:
                    continue
                with self.transaction() as conn:
                    if isinstance(migration.apply, str):
                        conn.executescript(migration.apply)
                    else:
                        migration.apply(conn)
                    conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                                 (migration.version, migration.name, utc_now()))
                    conn.execute(f"PRAGMA user_version = {int(migration.version)}")
                current = migration.version
            return current

    # -- gateways -------------------------------------------------------------

    def upsert_gateway(self, gateway: GatewayRecord) -> GatewayRecord:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO gateways (hw_id, port, boot_id, fw, build, board, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(hw_id) DO UPDATE SET port = excluded.port, boot_id = excluded.boot_id,"
                " fw = excluded.fw, build = excluded.build, board = COALESCE(excluded.board, gateways.board),"
                " last_seen_at = excluded.last_seen_at",
                (gateway.hw_id, gateway.port, gateway.boot_id, gateway.fw, gateway.build, gateway.board, now, now))
        return self.get_gateway(gateway.hw_id)

    def get_gateway(self, hw_id: str) -> GatewayRecord:
        with self.reading() as conn:
            row = conn.execute("SELECT * FROM gateways WHERE hw_id = ?", (hw_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"gateway {hw_id}")
        return _row_to(GatewayRecord, row)

    def list_gateways(self) -> list[GatewayRecord]:
        with self.reading() as conn:
            rows = conn.execute("SELECT * FROM gateways ORDER BY last_seen_at DESC").fetchall()
        return [_row_to(GatewayRecord, r) for r in rows]

    # -- bridges --------------------------------------------------------------

    def upsert_bridge(self, bridge: BridgeRecord) -> BridgeRecord:
        """Insert or update by UUID; a different bridge holding the same address loses it."""
        uuid = normalize_uuid(bridge.uuid)
        now = utc_now()
        with self.transaction() as conn:
            if bridge.addr is not None:
                conn.execute("UPDATE bridges SET addr = NULL, updated_at = ? WHERE addr = ? AND uuid != ?",
                             (now, bridge.addr, uuid))
            conn.execute(
                "INSERT INTO bridges (uuid, addr, name, elements, fw, board, fontpack_id, flash_size, configured,"
                " gateway_hw_id, provisioned_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(uuid) DO UPDATE SET addr = excluded.addr, name = excluded.name,"
                " elements = excluded.elements, fw = COALESCE(excluded.fw, bridges.fw),"
                " board = COALESCE(excluded.board, bridges.board),"
                " fontpack_id = COALESCE(excluded.fontpack_id, bridges.fontpack_id),"
                " flash_size = COALESCE(excluded.flash_size, bridges.flash_size),"
                " configured = excluded.configured,"
                " gateway_hw_id = COALESCE(excluded.gateway_hw_id, bridges.gateway_hw_id),"
                " provisioned_at = COALESCE(bridges.provisioned_at, excluded.provisioned_at),"
                " updated_at = excluded.updated_at",
                (uuid, bridge.addr, bridge.name, bridge.elements, bridge.fw, bridge.board, bridge.fontpack_id,
                 bridge.flash_size, int(bridge.configured), bridge.gateway_hw_id, bridge.provisioned_at or now, now))
        return self.get_bridge(uuid=uuid)

    def update_bridge(self, uuid: bytes | str, **changes: Any) -> BridgeRecord:
        """Change selected columns of one bridge, identified by its device UUID (``addr`` is a column)."""
        current = self.get_bridge(uuid=uuid)
        allowed = {f.name for f in fields(BridgeRecord)} - {"uuid", "updated_at"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"unknown bridge fields {sorted(unknown)}")
        return self.upsert_bridge(replace(current, **changes))

    def get_bridge(self, *, uuid: bytes | str | None = None, addr: int | None = None) -> BridgeRecord:
        if (uuid is None) == (addr is None):
            raise ValueError("give exactly one of uuid or addr")
        with self.reading() as conn:
            if uuid is not None:
                row = conn.execute("SELECT * FROM bridges WHERE uuid = ?", (normalize_uuid(uuid),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM bridges WHERE addr = ?", (addr,)).fetchone()
        if row is None:
            raise NotFoundError(f"bridge {normalize_uuid(uuid) if uuid is not None else addr}")
        return _row_to(BridgeRecord, row)

    def find_bridge(self, *, uuid: bytes | str | None = None, addr: int | None = None) -> BridgeRecord | None:
        try:
            return self.get_bridge(uuid=uuid, addr=addr)
        except NotFoundError:
            return None

    def list_bridges(self) -> list[BridgeRecord]:
        with self.reading() as conn:
            rows = conn.execute("SELECT * FROM bridges ORDER BY addr IS NULL, addr, uuid").fetchall()
        return [_row_to(BridgeRecord, r) for r in rows]

    def delete_bridge(self, *, uuid: bytes | str | None = None, addr: int | None = None) -> bool:
        """Remove a bridge; tags assigned to it keep their epoch but lose ``bridge_addr``."""
        bridge = self.find_bridge(uuid=uuid, addr=addr)
        if bridge is None:
            return False
        with self.transaction() as conn:
            conn.execute("DELETE FROM bridges WHERE uuid = ?", (bridge.uuid,))
        return True

    # -- tags -----------------------------------------------------------------

    def insert_tag(self, tag: TagRecord) -> TagRecord:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tags (tag_id, name, board, panel, width, height, planes, plane_flags, fw, enrolled_at,"
                " secret_ref, protected, epoch, bridge_addr, last_revision, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tag.tag_id, tag.name, tag.board, tag.panel, tag.width, tag.height, tag.planes, tag.plane_flags,
                 tag.fw, tag.enrolled_at or now, tag.secret_ref, int(tag.protected), tag.epoch, tag.bridge_addr,
                 tag.last_revision, now))
        return self.get_tag(tag.tag_id)

    def get_tag(self, tag_id: int) -> TagRecord:
        with self.reading() as conn:
            row = conn.execute("SELECT * FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"tag {tag_id:08X}")
        return _row_to(TagRecord, row)

    def find_tag(self, tag_id: int) -> TagRecord | None:
        try:
            return self.get_tag(tag_id)
        except NotFoundError:
            return None

    def tag_exists(self, tag_id: int) -> bool:
        with self.reading() as conn:
            return conn.execute("SELECT 1 FROM tags WHERE tag_id = ?", (tag_id,)).fetchone() is not None

    def list_tags(self, *, bridge_addr: int | None = None) -> list[TagRecord]:
        with self.reading() as conn:
            if bridge_addr is None:
                rows = conn.execute("SELECT * FROM tags ORDER BY tag_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM tags WHERE bridge_addr = ? ORDER BY tag_id",
                                    (bridge_addr,)).fetchall()
        return [_row_to(TagRecord, r) for r in rows]

    def update_tag(self, tag_id: int, **changes: Any) -> TagRecord:
        allowed = {"name", "fw", "protected", "epoch", "bridge_addr", "width", "height", "planes", "plane_flags",
                   "panel", "last_revision"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"cannot update tag fields {sorted(unknown)}")
        if not changes:
            return self.get_tag(tag_id)
        values = [int(v) if isinstance(v, bool) else v for v in changes.values()]
        assignments = ", ".join(f"{k} = ?" for k in changes)
        with self.transaction() as conn:
            cur = conn.execute(f"UPDATE tags SET {assignments}, updated_at = ? WHERE tag_id = ?",
                               (*values, utc_now(), tag_id))
            if cur.rowcount == 0:
                raise NotFoundError(f"tag {tag_id:08X}")
        return self.get_tag(tag_id)

    def set_assignment(self, tag_id: int, bridge_addr: int | None, epoch: int) -> TagRecord:
        """Record the tag's current assignment (after ``EVT_ASSIGN_RESULT OK``)."""
        with self.transaction() as conn:
            row = conn.execute("SELECT epoch FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"tag {tag_id:08X}")
            if epoch < row[0]:
                raise ValueError(f"epoch {epoch} is older than the recorded epoch {row[0]} of tag {tag_id:08X}")
            conn.execute("UPDATE tags SET bridge_addr = ?, epoch = ?, updated_at = ? WHERE tag_id = ?",
                         (bridge_addr, epoch, utc_now(), tag_id))
        return self.get_tag(tag_id)

    def allocate_revision(self, tag_id: int) -> int:
        """Atomically reserve the next display revision for a tag (monotonic across epochs)."""
        with self.transaction() as conn:
            row = conn.execute("UPDATE tags SET last_revision = last_revision + 1, updated_at = ? WHERE tag_id = ?"
                               " RETURNING last_revision", (utc_now(), tag_id)).fetchone()
            if row is None:
                raise NotFoundError(f"tag {tag_id:08X}")
            return int(row[0])

    def delete_tag(self, tag_id: int) -> bool:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM tags WHERE tag_id = ?", (tag_id,)).rowcount > 0
