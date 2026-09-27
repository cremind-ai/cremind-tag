"""Schema of the durable delivery queue: companion database version 2 onwards.

The inventory (version 1) belongs to :mod:`cremind_tag.store.db`; the queue
appends its versions through ``Database.open(..., extra_migrations=...)``.
Every companion component opens the database with :func:`open_database`, so
the CLI and the daemon agree on one schema.

Tables (timestamps: ISO 8601 UTC text for people, ``*_ts`` epoch seconds for
the scheduler's comparisons):

- ``streams`` — one row per content credential: Cremind's ``stream_id``, the
  events cursor ``after_seq``, the profile's display settings.
- ``jobs`` — one row per Cremind delivery (``delivery_id``). ``state`` is the
  card's life on the tag (``active`` = part of the tag's card set);
  ``outcome`` is the terminal outcome the companion reported (NULL until then;
  a displayed card stays ``active`` on later screens until it expires or is
  resolved). ``last_stage`` is the highest stage receipted.
- ``tag_views`` — per tag: Cremind's view from ``sync`` (owner credential,
  epoch, rotation, ``clear_required``, desired/displayed revisions) plus the
  scheduler's flags (dirty, force, blank, identify override, block reason);
  v4: ``epoch_floor``, the epoch a tag's ``STALE_EPOCH`` reported as stored
  (bounded, docs/companion.md "Epoch floor"), reported as the inventory epoch.
- ``revisions`` — every composed screen: layout, digests, the deliveries it
  shows, the ``op_id`` persisted BEFORE the ``DELIVER_LAYOUT`` that uses it,
  attempts and the retry time (v3: ``not_found_count``, consecutive
  ``EVT_RESULT NOT_FOUND`` answers, docs/protocol.md §10).
- ``outbox`` — receipts, accepted acknowledgements, previews and command
  results, kept until Cremind confirms them.
- ``commands`` — hardware commands, persisted before they are claimed; the
  ``progress`` JSON holds each step's ``op_id`` so a restart resumes them.
- ``gateway_ops`` — op ids of side-effecting gateway requests made by
  commands, with the retained result once it arrived.
- ``device_status`` — telemetry for the heartbeat (battery, RSSI, last contact).
- ``daemon_state`` — small key/value facts (the last gateway ``boot_id``).
"""

from __future__ import annotations

from pathlib import Path

from ..store.db import Database, Migration

_V2_QUEUE = """
CREATE TABLE streams (
    credential_id  TEXT PRIMARY KEY,
    profile        TEXT,
    companion_id   TEXT,
    stream_id      TEXT,
    after_seq      INTEGER NOT NULL DEFAULT 0 CHECK (after_seq >= 0),
    head_seq       INTEGER NOT NULL DEFAULT 0,
    settings       TEXT NOT NULL DEFAULT '{}',
    synced_at      TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE jobs (
    delivery_id    INTEGER PRIMARY KEY,
    credential_id  TEXT NOT NULL,
    profile        TEXT NOT NULL DEFAULT '',
    seq            INTEGER NOT NULL,
    tag_id         INTEGER NOT NULL,
    epoch          INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 0,
    replace_key    TEXT,
    resolves       TEXT,
    card           TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    expires_ts     REAL NOT NULL,
    state          TEXT NOT NULL DEFAULT 'active'
                   CHECK (state IN ('active', 'resolved', 'expired', 'cancelled', 'superseded', 'done', 'failed')),
    last_stage     TEXT,
    outcome        TEXT CHECK (outcome IS NULL OR outcome IN
                   ('displayed', 'superseded', 'expired', 'cancelled', 'failed', 'uncertain')),
    status_code    INTEGER,
    detail         TEXT,
    revision       INTEGER,
    digest         TEXT,
    timing         TEXT,
    uncertain      INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0, 1)),
    received_at    TEXT NOT NULL,
    finished_at    TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX ix_jobs_tag_state ON jobs(tag_id, state);
CREATE INDEX ix_jobs_credential_state ON jobs(credential_id, state);
CREATE INDEX ix_jobs_expiry ON jobs(state, expires_ts);

CREATE TABLE tag_views (
    tag_id             INTEGER PRIMARY KEY CHECK (tag_id BETWEEN 1 AND 4294967294),
    credential_id      TEXT,
    profile            TEXT,
    name               TEXT NOT NULL DEFAULT '',
    epoch              INTEGER NOT NULL DEFAULT 0,
    bridge_hw_id       TEXT,
    rotation           INTEGER NOT NULL DEFAULT 0 CHECK (rotation BETWEEN 0 AND 3),
    clear_required     INTEGER NOT NULL DEFAULT 0 CHECK (clear_required IN (0, 1)),
    cleared_epoch      INTEGER NOT NULL DEFAULT 0,
    cremind_desired    INTEGER NOT NULL DEFAULT 0,
    cremind_displayed  INTEGER NOT NULL DEFAULT 0,
    blank              INTEGER NOT NULL DEFAULT 0 CHECK (blank IN (0, 1)),
    override           TEXT,
    override_until     REAL,
    blocked_reason     TEXT,
    blocked_epoch      INTEGER,
    dirty              INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
    dirty_gen          INTEGER NOT NULL DEFAULT 0,
    force              INTEGER NOT NULL DEFAULT 0 CHECK (force IN (0, 1)),
    progress_pending   INTEGER NOT NULL DEFAULT 0 CHECK (progress_pending IN (0, 1)),
    displayed_revision INTEGER NOT NULL DEFAULT 0,
    displayed_digest   TEXT,
    displayed_at       TEXT,
    stale_jumps        INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL
);

CREATE TABLE revisions (
    tag_id               INTEGER NOT NULL,
    revision             INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 4294967295),
    epoch                INTEGER NOT NULL,
    bridge_addr          INTEGER,
    fontpack_id          TEXT,
    purpose              TEXT NOT NULL DEFAULT 'screen'
                         CHECK (purpose IN ('screen', 'blank', 'identify', 'refresh')),
    layout               BLOB NOT NULL,
    layout_digest        TEXT NOT NULL,
    content_key          TEXT NOT NULL,
    frame_digest         TEXT,
    delivery_ids         TEXT NOT NULL DEFAULT '[]',
    pending_delivery_ids TEXT NOT NULL DEFAULT '[]',
    preview_png          BLOB,
    op_id                INTEGER NOT NULL,
    state                TEXT NOT NULL
                         CHECK (state IN ('pending', 'sent', 'displayed', 'superseded', 'failed', 'uncertain')),
    last_stage           TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    uncertain_count      INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts      REAL NOT NULL DEFAULT 0,
    last_status          TEXT,
    detail               TEXT,
    timing               TEXT,
    created_at           TEXT NOT NULL,
    created_ts           REAL NOT NULL,
    sent_at              TEXT,
    sent_ts              REAL,
    finished_at          TEXT,
    PRIMARY KEY (tag_id, revision)
);
CREATE INDEX ix_revisions_state ON revisions(state, next_attempt_ts);
CREATE UNIQUE INDEX ix_revisions_op ON revisions(op_id);

CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('receipts', 'accepted', 'previews', 'command_result')),
    credential_id   TEXT NOT NULL,
    dedupe_key      TEXT,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    dead            INTEGER NOT NULL DEFAULT 0 CHECK (dead IN (0, 1)),
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_outbox_due ON outbox(credential_id, dead, next_attempt_ts);

CREATE TABLE commands (
    command_id  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL CHECK (state IN ('claiming', 'running', 'succeeded', 'failed')),
    progress    TEXT NOT NULL DEFAULT '{}',
    result      TEXT,
    error       TEXT,
    expires_at  TEXT,
    expires_ts  REAL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE gateway_ops (
    op_id       INTEGER PRIMARY KEY,
    command_id  TEXT,
    kind        TEXT NOT NULL,
    status      INTEGER,
    result      TEXT,
    boot_id     INTEGER,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);

CREATE TABLE device_status (
    hw_id            TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    battery_mv       INTEGER,
    rssi             INTEGER,
    last_contact_at  TEXT,
    last_contact_ts  REAL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE daemon_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

_V3_NOT_FOUND = """
ALTER TABLE revisions ADD COLUMN not_found_count INTEGER NOT NULL DEFAULT 0;
"""

_V4_EPOCH_FLOOR = """
ALTER TABLE tag_views ADD COLUMN epoch_floor INTEGER NOT NULL DEFAULT 0
    CHECK (epoch_floor BETWEEN 0 AND 4294967295);
"""

QUEUE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(2, "delivery_queue", _V2_QUEUE),
    Migration(3, "revision_not_found_count", _V3_NOT_FOUND),
    Migration(4, "tag_view_epoch_floor", _V4_EPOCH_FLOOR),
)
"""The queue's schema versions (append new ones; never edit an applied migration)."""


def open_database(path: Path | str) -> Database:
    """Open the companion database with the inventory and the delivery queue migrated to the newest version."""
    return Database.open(path, extra_migrations=QUEUE_MIGRATIONS)


__all__ = ["QUEUE_MIGRATIONS", "open_database"]
