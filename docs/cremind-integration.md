# Cremind-side design (branch `feat/tags-integration` of the Cremind repo)

Normative for the Cremind implementation. The wire contract with the companion
is [`connector-api.md`](connector-api.md). Cremind conventions that shape this
design: Starlette handlers check `request.user` themselves; one pod; string
UUID primary keys; `Float` millisecond timestamps; `profile` columns hold the
profile name; no DB sequences (restore never resets them); stores open their
own transactions.

## 1. Package layout

```
app/tags/__init__.py
app/tags/models (in app/storage/models.py)   ORM models below
app/tags/journal.py        append_async(session, …) / append_sync(conn, …), enabled-profile cache
app/tags/sanitize.py       allowlisted payload builders, OTP/secret redaction, excerpt policy
app/tags/cards.py          journal event -> normalised card(s) (the card JSON of connector-api.md)
app/tags/routing.py        profile settings (+ admin defaults) -> target tags per card kind
app/tags/projection.py     TagProjectionWorker: journal -> deliveries, expiry, pruning, periodic content
app/tags/storage.py        TagStorage (async, Core statements): companions, credentials, devices,
                           deliveries, commands, previews, settings, streams
app/tags/credentials.py    create/verify/revoke connector credentials (sha256, constant-time)
app/tags/service.py        operations shared by REST handlers (claim, assign, display, clear, …)
app/api/tags.py            /api/tags/*            (profile)
app/api/tags_hardware.py   /api/tags/hardware/*   (admin)
app/api/tag_connector.py   /api/tag-connector/v1/* (connector credentials only)
app/middleware/tag_connector_guard.py   401 for the CremindTag scheme outside the connector prefix
app/cli/client/tags.py, app/cli/commands/tags.py
app/cremind_documents/bundled/[cli]cremind tags.md, [cli]cremind tags hardware.md
ui/src/views/TagsPage.vue, ui/src/views/settings/TagsSettings.vue (+ components/tags/*), ui/src/services/tagsApi.ts, ui/src/stores/tags.ts
```

## 2. Tables (one additive migration `20260930_tags`, ≤ 32 chars, down_revision `20260929_profile_working_dir`)

All timestamps are epoch **milliseconds** (`Float`). JSON columns are plain `JSON`.

| Table | Columns (PK first) | Notes |
|---|---|---|
| `tag_companions` | `id` str36; `name`; `created_by`; `created_at`; `updated_at`; `last_seen_at`?; `version`?; `host`?; `heartbeat` JSON? | one row per PC companion |
| `tag_credentials` | `id` str40 (= public credential id `tagc_…`); `companion_id` FK→companions CASCADE; `kind` (`hardware`/`content`); `profile`? FK→profiles.name CASCADE (content only); `secret_sha256` str64; `label`; `created_by`; `created_at`; `last_used_at`?; `revoked_at`? | excluded from backup dumps entirely |
| `tag_devices` | `id` str36; `companion_id` FK CASCADE; `kind` (`gateway`/`bridge`/`tag`); `hw_id` str64; `name`; `owner_profile`? FK→profiles.name SET NULL; `bridge_device_id`? FK→tag_devices SET NULL; `epoch` int=0; `rotation` int=0; `board`?, `panel`?, `width`?, `height`?, `planes`?; `fw`?; `info` JSON; `status` str32 = `unclaimed`; `battery_mv`?; `rssi`?; `last_contact_at`?; `desired_revision` int=0; `displayed_revision` int=0; `displayed_digest`?; `clear_required` bool=false; `claimed_at`?; `created_at`; `updated_at` | `UNIQUE(companion_id, kind, hw_id)` |
| `tag_streams` | `profile` PK FK CASCADE; `stream_id` str36; `next_seq` bigint=0; `projected_seq` bigint=0; `next_delivery_seq` bigint=0; `state` JSON (periodic-content hashes); `updated_at` | per-profile head row; row lock orders commits |
| `tag_events` | `id` str36; `profile` FK CASCADE; `seq` bigint; `kind` str64; `durability` (`durable`/`checkpoint`); `replace_key`?; `source_type`; `source_id`?; `payload` JSON; `created_at`; `expires_at` | `UNIQUE(profile, seq)`; the journal |
| `tag_deliveries` | `id` **bigint** (explicit, = connector `delivery_id` = serial `update_id`); `profile` FK CASCADE; `seq` bigint; `companion_id`? FK SET NULL; `tag_device_id` FK CASCADE; `epoch` int; `event_id`? FK SET NULL; `kind`; `priority` int; `replace_key`?; `resolves`?; `card` JSON; `stage` str32 = `queued`; `outcome`?; `status_code`?; `revision`?; `digest`?; `detail`?; `timing` JSON?; `stage_times` JSON; `created_at`; `updated_at`; `expires_at`; `finished_at`? | `UNIQUE(profile, seq)`, `ix_tag_deliveries_device_created`, `ix_tag_deliveries_stage` |
| `tag_counters` | `name` PK str32; `value` bigint | singleton `delivery_id` allocator (UPDATE … RETURNING) |
| `tag_commands` | `id` str36; `companion_id` FK CASCADE; `kind`; `args` JSON; `requested_by`; `status` (`queued`/`claimed`/`succeeded`/`failed`/`expired`/`cancelled`); `result` JSON?; `error`?; `created_at`; `claimed_at`?; `completed_at`?; `expires_at` | hardware operations; `ix_tag_commands_companion_status` |
| `tag_previews` | `id` str36; `tag_device_id` FK CASCADE; `kind` (`desired`/`displayed`); `revision`; `png_base64` Text; `delivery_ids` JSON; `created_at` | `UNIQUE(tag_device_id, kind)` |
| `tag_settings` | `profile` PK FK CASCADE; `enabled` bool=false; `options` JSON; `updated_at` | admin defaults in `server_config` key `tags_defaults` |

The migration creates tables guarded by inspector checks (copy
`20260927_userdocs_research.py`), creates indexes guarded, and inserts the
`tag_counters` row. Never `batch_alter_table` on existing tables.

## 3. Journal

- `append_async(session, profile, entries)` / `append_sync(conn, profile, entries)`
  run **inside the caller's transaction**, **last**:
  `UPDATE tag_streams SET next_seq = next_seq + k … WHERE profile = :p RETURNING next_seq`
  (insert the head row first when missing, `ON CONFLICT DO NOTHING`), then insert
  `k` rows with consecutive seqs. On PostgreSQL the head-row lock is held until
  commit, so commit order = seq order per profile; SQLite has one writer.
- Only profiles with Tags enabled are journalled (5-second cache of
  `tag_settings.enabled`, invalidated on save). Appends never raise into the
  source operation's success path except on DB errors that would fail the
  transaction anyway.
- **Allowlisted kinds** and payload fields only (never row diffs):

| Kind | Durability | Source hook (same transaction) | Payload |
|---|---|---|---|
| `assistant.result` | durable | `ConversationStorage.add_message(role="agent")` for `chat` conversations only | conversation id/title, message id, errored, cancelled, `excerpt` (≤ 280 chars of final text, sanitised) |
| `chat.needs_input` / `chat.needs_input_resolved` | durable | same `add_message` when `plan_mode.stage` is `awaiting_answers`/`awaiting_approval` / `cancelled`/`executing` | conversation id/title, stage, question (sanitised, ≤ 200) |
| `run.started` | checkpoint | `EventRunStorage.create`/`update_status` → running | run id, title |
| `run.progress` | checkpoint | todos snapshot at plan events (projection worker samples) | done/total |
| `run.needs_input` | durable | `update_status(status="pending", pending_question=…)` | run id, title, question |
| `run.resumed` | durable (resolves) | pending → running | run id |
| `run.completed` / `run.failed` | durable | terminal `update_status`/`create(status="failed")`/`recover_after_restart` (RETURNING) | run id, title, error summary |
| `channel.failed` / `channel.unlinked` / `channel.recovered` | durable | `ConversationStorage.update_channel` from `_disable_channel`, `_mark_unlinked`, `_mark_linked` | channel id, name, type, error summary |
| `automation.failed` | durable | `AutostartStorage.set_error` (sync, `append_sync`), schedule/dispatch failures (standalone) | automation kind, name, error summary |
| `subscription.changed` | durable | `update_sender` when the **access flag** changes (never `pending_otp`) | channel id, masked sender, subscribed |
| `notification` | durable | standalone: `EventNotificationsBuffer.push` → async append task; **`channel_otp` and anything whose preview/extra carries an OTP is dropped** | kind, title, sanitised preview |
| `tag.diagnostics` | checkpoint | projection worker from heartbeats (battery low, offline > 2 h) | device, battery, last contact |
| periodic: `calendar.upcoming`, `automation.upcoming`, `usage.summary`, `indexing.problem`, `health.summary` | checkpoint | projection worker every 5 min, only when the content hash changed | small summaries |

`sanitize.py` redacts OTP-like codes (`\b\d{4,8}\b` next to otp/code/passcode/pin),
bearer/API tokens, `key=…`/`password=…` pairs, and base64/hex runs ≥ 24 chars;
it never journals reasoning, tool output or terminal output.

## 4. Projection worker

`TagProjectionWorker` (template: `TaskTimeoutManager`; wake event +
2 s poll). Started after `sweep_undelivered()` in `boot_storage_and_post_storage`,
stopped in `_do_shutdown`. Per enabled profile, in **one transaction**: read
events `seq > projected_seq` (≤ 200), build cards (`cards.py`), route
(`routing.py`) to owned tags (`owner_profile = profile`, not `clear_required`
for content kinds), allocate `delivery_id`s (`tag_counters`) and per-profile
delivery `seq`s (`tag_streams.next_delivery_seq`), insert deliveries, mark
older non-terminal deliveries with the same `(tag, replace_key)` `superseded`,
set `projected_seq`. Also: expire deliveries past `expires_at`; prune terminal
deliveries and events older than 30 days; derive `tag.diagnostics`; run
periodic content every 5 minutes.

TTL defaults: notification 24 h; needs-input 7 days (until resolved);
progress 1 h; periodic 24 h; `clear` 7 days.

## 5. REST

Profile API (`require_auth`; profile = `request.user.username`; a device is
visible only when `owner_profile` = profile):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tags` | overview: enabled, owned devices (status, battery, last contact, revisions, previews), counts |
| GET/PUT | `/api/tags/settings` | effective settings (+ admin defaults) / save |
| GET/PATCH | `/api/tags/devices/{id}` | detail + recent deliveries / rename |
| POST | `/api/tags/devices/{id}/display` | pinned note `{title, body?, icon?, ttl_s?}` (sanitised, OTP-checked) |
| POST | `/api/tags/devices/{id}/clear` | `clear` delivery |
| POST | `/api/tags/devices/{id}/refresh` | hardware command `refresh_tag` |
| POST | `/api/tags/devices/{id}/identify` | hardware command `identify` |
| GET | `/api/tags/devices/{id}/preview?kind=desired\|displayed` | PNG |
| GET | `/api/tags/deliveries?device=&state=&limit=&before=` | history |
| GET | `/api/tags/deliveries/{id}` / POST `…/cancel` | detail / cancel |
| GET | `/api/tags/companions` | companions (id, name, last seen) for credential creation |
| GET/POST | `/api/tags/credentials` | list own content credentials / create `{companion_id, label}` → secret once |
| DELETE | `/api/tags/credentials/{id}` | revoke |

Admin API (`require_admin`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tags/hardware` | companions, all devices, pending commands |
| POST | `/api/tags/hardware/companions` | register `{name}` → companion + hardware credential (secret once) |
| POST | `/api/tags/hardware/companions/{id}/rotate` | new hardware credential, old revoked |
| DELETE | `/api/tags/hardware/companions/{id}` | revoke credentials, delete companion |
| POST | `/api/tags/hardware/commands` | `{companion_id, kind, args}` → operation id (bounded kinds) |
| GET | `/api/tags/hardware/commands/{id}` | operation status/result |
| POST | `/api/tags/hardware/tags/{id}/claim` | `{owner, bridge_id?, name?}` → epoch+1, `clear_required`, cancel old work, `assign_tag` + `clear_tag` commands |
| POST | `/api/tags/hardware/tags/{id}/assign` | `{bridge_id}` → epoch+1, `assign_tag` |
| POST | `/api/tags/hardware/tags/{id}/release` | owner → none, epoch+1, cancel, `clear_tag` |
| PATCH/DELETE | `/api/tags/hardware/devices/{id}` | rename / forget |

Connector API: exactly [`connector-api.md`](connector-api.md). Every response
is scoped by the credential (content → its profile + companion; hardware → its
companion). A `clear_tag` command result with `succeeded` clears
`clear_required`; receipts update `desired_revision` / `displayed_revision` /
`displayed_digest` on the device.

## 6. Isolation, security, lifecycle

- Content credentials die with their profile (FK CASCADE); deleting a profile
  releases its tags (owner → none, epoch + 1, `clear_required`, `clear_tag`).
- `TagConnectorGuard` (pure ASGI, between `ClientProtocolGuard` and
  `AuthenticationMiddleware`) answers 401 to `Authorization: CremindTag …` on any
  path outside `/api/tag-connector/v1/`; connector handlers reject `Bearer`.
- Backups exclude `tag_credentials`. Restore close-out: every non-terminal
  delivery → `cancelled` (`detail: restored`), every stream gets a new
  `stream_id`, `tag_counters.delivery_id` jumps by 2³², companions
  re-`sync`.
- `display` via REST applies the same sanitiser and refuses text containing an
  OTP-like code (422 `otp_refused`).
