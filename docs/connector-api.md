# Connector API (Cremind ↔ companion)

The companion always connects **outbound** to Cremind (works with remote and
Docker-hosted Cremind). All endpoints live under `/api/tag-connector/v1/` and
accept **only** connector credentials:

```http
Authorization: CremindTag <credential-id>.<secret>
```

- `credential-id`: `tagc_` + 26 lowercase base32 characters (public identifier).
- `secret`: 43-character base64url string (32 random bytes). Shown once at
  creation; Cremind stores `SHA-256(secret)` and compares in constant time.
- Generic Cremind APIs reject this scheme (401); connector endpoints reject JWTs
  and API keys (401). Revoked credentials fail immediately (401
  `credential_revoked`).
- The profile is **derived from the credential**, never from a request field.

| Credential kind | Bound to | Grants |
|---|---|---|
| `hardware` | one companion | its inventory, heartbeat, hardware command queue |
| `content` | one profile **and** one companion | that profile's delivery jobs for tags owned by the profile on that companion |

Errors use `{"error": "<code>", "detail": "..."}` with HTTP 400/401/403/404/409/410/422.

## Hardware endpoints (`hardware` credential)

### `GET whoami` (any kind)
`{credential_id, kind, companion_id, profile|null, api_version: 1, server_time}`

### `POST inventory`
Upsert what the companion physically manages.
```json
{"gateways": [{"hw_id": "gw-<uuid>", "fw": "0.1.0", "board": 1, "boot_id": 123, "port": "COM7"}],
 "bridges":  [{"hw_id": "br-<mesh-uuid>", "addr": 2, "fw": "0.1.0", "board": 3, "fontpack_id": "a1b2c3d4e5f60718", "flash_size": 67108864,
               "max_tags": 20, "assigned": 7}],
 "tags":     [{"tag_id": "1A2B3C4D", "board": 16, "panel": 1, "width": 400, "height": 300, "planes": 1, "fw": "0.1.0", "epoch": 5}]}
```

`epoch` (optional, u32) is the highest assignment epoch the companion has used
for the tag or learned from it: its epoch floor, when a tag's `STALE_EPOCH`
reported a higher `stored_epoch` (protocol.md §10, companion.md §5 "Epoch
floor"; bounded, at most 256 above what the companion knew per report).
Cremind keeps `epoch = max(stored, reported)`, so a tag that was
forgotten and re-reported, or a restore that rewound epochs, never falls below
the epoch the tag will accept. When the reported epoch is ahead of work Cremind
still owes, that work is re-queued at `reported + 1` (`assign_tag` for an owned
tag with a bridge, `clear_tag` for a pending clear) and the tag's active
deliveries move to the new epoch.

`max_tags` (optional, 1..255) is the bridge's assignment-table capacity from its
`CAPS_STATUS` (20 on an nRF52840 bridge, 10 on an nRF52832) and `assigned`
(optional, 0..255) the tags assigned on it: the bridge's own count from its
`CAPS_STATUS` (the gateway's `caps.assigned_count`), or the assignments the
gateway holds for it when the gateway does not report that count; both are
left out while the gateway has not reported the bridge. Cremind keeps them in the
bridge's `info` (a missing or bad value keeps the last good one) and refuses to
claim or assign a tag onto a bridge that is full.
→ `{"devices": [...device rows...], "assignments": [{"tag_id", "owner_profile", "bridge_hw_id", "epoch", "rotation"}]}`

### `POST heartbeat`
```json
{"companion": {"version": "0.1.0", "host": "desk-pc", "started_at": "..."},
 "queue": {"depth": 3, "oldest_age_s": 42},
 "devices": [{"hw_id": "1A2B3C4D", "kind": "tag", "battery_mv": 2900, "last_contact_at": "...", "rssi": -61,
              "displayed_revision": 17, "displayed_digest": "…hex8…", "status": "ok|pending|offline|error"}]}
```
→ `{"server_time", "commands_pending": 2}`

### Hardware commands
Admin actions in Cremind (`/api/tags/hardware/*`) create **asynchronous
operations**; the companion executes them.

- `GET commands?wait=25` — long-poll (≤ 30 s) → `{"commands": [{"id", "kind", "args", "created_at", "expires_at"}]}`
- `POST commands/{id}/claim` → `200 {command}` or `409 already_claimed`
- `POST commands/{id}/result` `{"status": "succeeded|failed", "result": {...}, "error": "..."}` → `200`
  (idempotent for the same status)

Result shapes the companion reports (`result` of `commands/{id}/result`):
`scan_unprovisioned` → `{"duration_s": 60, "beacons": [{"uuid": "<32 hex>",
"hw_id": "br-<32 hex>", "rssi": -48, "oob": 0}]}` (strongest first, at most
40); other kinds return a small object describing what was done (for example
`{"addr": 2}` after provisioning) or `{}`. A failed `assign_tag` because the
bridge's table is full (`ASSIGN_SET NO_RESOURCES`) reports `{"status":
"failed", "error": "bridge_full", "result": {"error": "bridge_full",
"max_tags": 10}}` (`max_tags` omitted when unknown); Cremind then marks the tag
`assign_failed`, drops it from that bridge and records `max_tags`.

Kinds: `scan_unprovisioned {duration_s}`, `provision_bridge {uuid, name}`,
`configure_bridge {hw_id}`, `remove_bridge {hw_id}`,
`assign_tag {tag_id, bridge_hw_id, epoch}` (companion derives `K_epoch`, sends
`ASSIGN_TAG`, then `UNASSIGN_TAG` to the previous bridge),
`clear_tag {tag_id, epoch}`, `identify {hw_id}`, `refresh_tag {tag_id}`,
`install_fontpack {bridge_hw_id}` (operator-assisted),
`collect_diagnostics {}`.

## Content endpoints (`content` credential)

### `POST sync`
Start-up, reconnection and recovery after local database loss.
```json
{"cursor": 1200}
```
→
```json
{"profile": "alice", "companion_id": "…", "stream_id": "…uuid…",
 "cursor_valid": true, "oldest_seq": 900, "head_seq": 1234,
 "outstanding": [ job, … ],              // every non-terminal, unexpired job for this profile+companion, any seq
 "tags": [{"tag_id": "1A2B3C4D", "name": "Desk", "epoch": 3, "bridge_hw_id": "br-…",
           "width": 400, "height": 300, "planes": 1, "rotation": 0,
           "desired_revision": 18, "displayed_revision": 17, "clear_required": false}],
 "settings": {"enabled": true, "layout": "status", "show_excerpts": false, "qr_links": false,
              "progress_cadence_s": 300, "timezone": "Asia/Ho_Chi_Minh", "language": "vi"}}
```

`timezone` is always an IANA name. All connector timestamps are ISO 8601 UTC
with milliseconds (`2026-09-27T10:00:00.123Z`).

### `GET events?after=<seq>&limit=<n≤200>`
→ `{"stream_id": "…", "jobs": [job, …], "next_after": 1250, "head_seq": 1250}`

`410 {"error": "cursor_expired", "oldest_seq": 900}` when `after` is older than
retained history, or newer than `head_seq` (the server was restored from a
backup) — the companion must call `sync` (explicit resynchronisation). A
`stream_id` different from the one the companion stored also means "restored":
the companion calls `sync` and drops local jobs Cremind no longer lists.

`events` may include jobs that are already terminal (for example cancelled
before the companion fetched them); the companion records them by `stage` and
does not display them. Jobs are served only for tags the profile still owns,
and live jobs only at the tag's current epoch.

### `POST accepted`
The companion commits received jobs **and** its cursor in one local SQLite
transaction, then acknowledges:
```json
{"through_seq": 1250, "delivery_ids": [501, 502]}
```
→ `{"accepted": 2}` — moves those deliveries to `companion_accepted`.

### `POST receipts`
```json
{"receipts": [{"delivery_id": 501, "stage": "displayed", "outcome": "displayed|superseded|expired|cancelled|failed|uncertain|null",
               "status_code": 0, "at": "2026-09-27T10:00:00Z", "tag_id": "1A2B3C4D",
               "epoch": 3, "revision": 18, "digest": "…hex8…",
               "timing": {"wake_ms": 12000, "mesh_ms": 800, "transfer_ms": 4100, "refresh_ms": 3900},
               "detail": "optional text"}]}
```
→ `{"applied": 1, "rejected": [{"delivery_id": 502, "reason": "epoch_mismatch"}]}`.
`reason` ∈ `invalid, unknown, not_owned, epoch_mismatch, terminal`. Idempotent:
repeating a final receipt with the same outcome is neither applied nor
rejected; a stage never moves backwards (compare-and-set); a terminal outcome
is final. On `epoch_mismatch` the companion re-syncs; `terminal`, `not_owned`
and `unknown` receipts are dropped.

### `POST previews`
```json
{"tag_id": "1A2B3C4D", "epoch": 3, "revision": 18, "kind": "desired|displayed", "png_base64": "…", "delivery_ids": [501, 502]}
```
Stores the latest rendered preview (1-bit PNG, ≤ 64 KiB) for the Tags page.
Revisions are compared within one epoch; a preview for a non-current epoch is
refused with 409 `epoch_mismatch`, and every change of owner deletes the tag's
previews.

## Job shape

```json
{"delivery_id": 501, "seq": 1249, "tag_id": "1A2B3C4D", "epoch": 3,
 "kind": "needs_input", "priority": 90, "replace_key": "run:…:input", "resolves": null,
 "created_at": "…", "expires_at": "…", "stage": "queued",
 "card": {"v": 1, "kind": "needs_input", "severity": "attention", "icon": "help",
          "title": "Approve deployment?", "body": null, "lang": "en",
          "ts": "…", "progress": null, "link": null,
          "source": {"type": "event_run", "id": "…"}}}
```

`kind` ∈ `notification, task_outcome, needs_input, excerpt, progress, health,
indexing_problem, calendar, automation, usage, pinned_note, tag_diagnostics,
resolved, clear`. Every content job has a `replace_key` (`delivery:<id>` when it
shares none). A `resolved` job removes the card whose `replace_key` equals its
`resolves` from the tag's active set; cancelling a delivery in Cremind emits
such a job (card title "Cancelled", `source.type` `delivery`), so a cancel
reaches a companion that already fetched the card. `clear` asks for a blank
screen (ownership change). A `clear_tag` command that fails or expires is
re-queued up to three times, then the device shows `clear_failed` until an
admin claims or releases it again; a late `succeeded` result for an expired
command is still accepted.

## Screen model

A tag shows one screen. The companion composes it from the tag's **active
cards** (unexpired, unresolved), highest priority first, then newest:
headline card (icon, title, optional body, progress), up to three more titles
with timestamps, and a footer "N more updates waiting for this tag · Updated
HH:MM". Every composition is a new **revision** that includes a set of
delivery ids. A newer revision supersedes an undisplayed older one without
losing cards (the newer screen includes them). When revision R is displayed,
every delivery it includes is receipted `displayed`.

## Defaults

| Item | Default |
|---|---|
| ordinary notification TTL | 24 h |
| needs-input | until resolved, 7-day safeguard expiry |
| journal + terminal history retention | 30 days |
| progress | replaces older pending revisions; screen cadence 5 min |
| expired cursor | explicit `sync` |
| transient errors | bounded exponential back-off until expiry |
| security/configuration errors | stop the job, report `failed` |

## Delivery stages

`queued → companion_accepted → gateway_received → bridge_received →
transferring → refreshing → displayed`, plus terminal `superseded`, `expired`,
`cancelled`, `failed`, `uncertain`.
