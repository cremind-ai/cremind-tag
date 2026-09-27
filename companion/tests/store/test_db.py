"""SQLite store: pragmas, versioned migrations, inventory API."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from cremind_tag.store import (
    BridgeRecord,
    Database,
    GatewayRecord,
    Migration,
    NotFoundError,
    SchemaError,
    TagRecord,
    normalize_uuid,
)

UUID_A = "00112233445566778899aabbccddeeff"
UUID_B = "ffeeddccbbaa99887766554433221100"


def tag(tag_id: int = 0x1A2B3C4D, **kw: object) -> TagRecord:
    values: dict[str, object] = {"board": 16, "panel": 1, "width": 400, "height": 300, "planes": 1,
                                 "plane_flags": 1, "secret_ref": f"file:tag:{tag_id:08X}"}
    values.update(kw)
    return TagRecord(tag_id=tag_id, **values)  # type: ignore[arg-type]


def test_pragmas_and_v1(tmp_path: Path) -> None:
    with Database.open(tmp_path / "c.sqlite3") as db:
        assert db.schema_version == 1
        assert [(v, n) for v, n, _ in db.applied_migrations()] == [(1, "inventory")]
        with db.reading() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"gateways", "bridges", "tags", "schema_migrations"} <= tables
    with Database.open(tmp_path / "c.sqlite3") as db:  # re-open: nothing to do
        assert db.schema_version == 1 and len(db.applied_migrations()) == 1


def test_extension_migrations(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite3"
    Database.open(path).close()

    def backfill(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE jobs_meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO jobs_meta VALUES ('created', 'yes')")

    extra = [Migration(2, "queue", "CREATE TABLE jobs (id INTEGER PRIMARY KEY, tag_id INTEGER"
                                   " REFERENCES tags(tag_id) ON DELETE CASCADE);"
                                   "CREATE INDEX ix_jobs_tag ON jobs(tag_id);"),
             Migration(3, "queue-meta", backfill)]
    with Database.open(path, extra_migrations=extra) as db:
        assert db.schema_version == 3
        with db.reading() as conn:
            assert conn.execute("SELECT v FROM jobs_meta").fetchone()[0] == "yes"
    with pytest.raises(SchemaError, match="knows up to 1"):
        Database.open(path)  # an older build refuses a newer database
    with pytest.raises(SchemaError, match="'queue'"):
        Database.open(path, extra_migrations=[Migration(2, "other", "SELECT 1"), extra[1]])
    with pytest.raises(SchemaError, match="without gaps"):
        Database.open(tmp_path / "gap.sqlite3", extra_migrations=[Migration(3, "x", "SELECT 1")])


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite3"
    broken = Migration(2, "broken", "CREATE TABLE ok (x); CREATE TABLE ok (x);")
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path, extra_migrations=[broken])
    with Database.open(path) as db:
        assert db.schema_version == 1
        with db.reading() as conn:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ok'").fetchone() is None


def test_gateways(tmp_path: Path) -> None:
    with Database.open(tmp_path / "c.sqlite3") as db:
        first = db.upsert_gateway(GatewayRecord("gw-1", port="COM7", boot_id=5, fw="0.1.0", board=1))
        second = db.upsert_gateway(GatewayRecord("gw-1", port="COM8", boot_id=6, fw="0.1.1"))
        assert (second.port, second.boot_id, second.board) == ("COM8", 6, 1)
        assert second.first_seen_at == first.first_seen_at
        assert [g.hw_id for g in db.list_gateways()] == ["gw-1"]
        with pytest.raises(NotFoundError):
            db.get_gateway("gw-2")


def test_bridges_and_tag_assignment(tmp_path: Path) -> None:
    with Database.open(tmp_path / "c.sqlite3") as db:
        a = db.upsert_bridge(BridgeRecord(UUID_A, addr=2, name="hall", fontpack_id="a1b2c3d4e5f60718"))
        assert a.hw_id == f"br-{UUID_A}" and not a.configured
        a = db.update_bridge(db.get_bridge(addr=2).uuid, configured=True, fw="0.1.0")
        assert a.configured and a.fw == "0.1.0" and a.fontpack_id == "a1b2c3d4e5f60718"
        # A re-provisioned device taking address 2 moves the old row off it.
        b = db.upsert_bridge(BridgeRecord(bytes.fromhex(UUID_B).hex(), addr=2))
        assert db.get_bridge(uuid=UUID_A).addr is None and db.get_bridge(addr=2).uuid == b.uuid
        db.update_bridge(bytes.fromhex(UUID_A), addr=3)
        with pytest.raises(ValueError):
            db.update_bridge(UUID_A, updated_at="yesterday")

        t = db.insert_tag(tag())
        assert t.hw_id == "1A2B3C4D" and t.epoch == 0 and t.last_revision == 0
        assert db.tag_exists(t.tag_id) and not db.tag_exists(1)
        t = db.set_assignment(t.tag_id, 3, 2)
        assert (t.bridge_addr, t.epoch) == (3, 2)
        with pytest.raises(ValueError, match="older"):
            db.set_assignment(t.tag_id, 3, 1)
        with pytest.raises(sqlite3.IntegrityError):
            db.set_assignment(t.tag_id, 99, 3)  # no such bridge (foreign key)
        assert [x.tag_id for x in db.list_tags(bridge_addr=3)] == [t.tag_id]

        assert db.delete_bridge(uuid=UUID_A)
        assert db.get_tag(t.tag_id).bridge_addr is None  # ON DELETE SET NULL
        assert db.get_tag(t.tag_id).epoch == 2
        assert not db.delete_bridge(addr=42)


def test_tags(tmp_path: Path) -> None:
    with Database.open(tmp_path / "c.sqlite3") as db:
        db.insert_tag(tag(1, name="desk"))
        db.insert_tag(tag(0xFFFFFFFE, planes=2, plane_flags=3))
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_tag(tag(1))
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_tag(tag(0xFFFFFFFF))
        assert [t.tag_id for t in db.list_tags()] == [1, 0xFFFFFFFE]
        assert db.update_tag(1, protected=True, name="door").protected
        with pytest.raises(ValueError):
            db.update_tag(1, secret_ref="x")
        assert db.delete_tag(1) and not db.delete_tag(1)
        with pytest.raises(NotFoundError):
            db.update_tag(1, name="x")


def test_allocate_revision_is_atomic_across_threads(tmp_path: Path) -> None:
    with Database.open(tmp_path / "c.sqlite3") as db:
        db.insert_tag(tag(7))
        got: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(25):
                value = db.allocate_revision(7)
                with lock:
                    got.append(value)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(got) == list(range(1, 101))
        with pytest.raises(NotFoundError):
            db.allocate_revision(8)


def test_async_run_and_transaction_rollback(tmp_path: Path) -> None:
    import asyncio

    with Database.open(tmp_path / "c.sqlite3") as db:
        assert asyncio.run(db.run(db.tag_exists, 5)) is False
        with pytest.raises(RuntimeError), db.transaction() as conn:
            conn.execute("INSERT INTO gateways (hw_id, first_seen_at, last_seen_at) VALUES ('x', 'a', 'a')")
            raise RuntimeError("boom")
        assert db.list_gateways() == []


def test_normalize_uuid() -> None:
    assert normalize_uuid(bytes(range(16))) == bytes(range(16)).hex()
    assert normalize_uuid("00112233-4455-6677-8899-AABBCCDDEEFF") == UUID_A
    for bad in ("xyz", "00" * 15):
        with pytest.raises(ValueError):
            normalize_uuid(bad)
