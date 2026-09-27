# The companion

The companion is the Python program (`cremind-tag`) on the PC the gateway is
plugged into. It connects **out** to Cremind, keeps every delivery job in a
durable SQLite queue, composes each tag's screen (multilingual text layout,
[layout.md](layout.md)), sends it through the gateway, and tells Cremind what
happened — stage by stage, until the tag reports that the panel refreshed.

```
Cremind ──sync/events──▶ jobs ─▶ card set per tag ─▶ composed screen (revision) ─▶ DELIVER_LAYOUT ─▶ gateway
   ▲                                                                                                  │
   └──── accepted · receipts · previews · command results ◀── EVT_STAGE / EVT_RESULT (commit, then ACK)┘
```

Related documents: [connector API](connector-api.md) (the wire contract with
Cremind) · [simulator](simulator.md) · [enrollment](enrollment.md) ·
[fonts](fonts.md) and [font packs](fontpack.md) · [screen layout](layout.md) ·
[protocols](protocol.md) · [security](security.md).

## 1. Install

Requirements: Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
cd companion
uv sync                                  # installs cremind-tag and its dependencies
uv run cremind-tag --help
uv run cremind-tag fonts fetch           # pinned Noto fonts (docs/fonts.md)
uv run cremind-tag fonts build --profile full    # or: dev (small, for trying things)
```

In a checkout every command below is `uv run cremind-tag …`. The daemon composes
with the font pack the bridges have active and talks to one gateway; both go
in the configuration file (or `--pack` / `--gateway` on `daemon run`):

```toml
# <config dir>/cremind-tag/config.toml  (CREMIND_TAG_CONFIG overrides the path)
[hardware]
gateway_url = "COM7"                          # or /dev/ttyACM0, socket://127.0.0.1:7777 (simulator)
fontpack = "C:/cremind-tag/fonts/out/full/fontpack.ctfp"

[cremind]                                      # written by `cremind-tag connect`
url = "https://cremind.example.org"
ca_file = "C:/certs/cremind-ca.pem"            # only for a private CA
hardware_credential = "tagc_…"
content_credentials = ["tagc_…"]

[daemon]                                       # optional tuning; defaults suit real hardware
bridge_maintenance = ["br-0011…eeff=COM9"]     # for install_fontpack
```

Every setting has an environment override: `CREMIND_TAG_<FIELD>` for
`[hardware]` and `[paths]`, `CREMIND_TAG_CREMIND_<FIELD>`,
`CREMIND_TAG_DAEMON_<FIELD>`, `CREMIND_TAG_SECRETS_BACKEND`. State lives in the
data directory (`paths.data_dir`, `CREMIND_TAG_DATA_DIR`): `companion.sqlite3`,
`logs/daemon.log`, `daemon-status.json` and, without an OS keyring,
`secrets.json` (owner-only: mode 0600, and on Windows a protected ACL for your
user and SYSTEM — Windows ignores the mode and a file would inherit its
folder's ACL; several processes may use it at once, under a lock).

## 2. Connect to Cremind

Cremind issues two kinds of connector credentials (connector-api.md):

| Credential | Created where | Grants |
|---|---|---|
| **hardware** | admin: Settings → Tags hardware → *Register companion* (`POST /api/tags/hardware/companions`) | inventory, heartbeat, hardware commands of this companion |
| **content** | each profile: Settings → Tags → *Credentials* (`POST /api/tags/credentials`, pick this companion) | that profile's delivery jobs for the tags it owns on this companion |

Cremind shows the secret once, as an authorization value `CremindTag tagc_….<secret>`:

```bash
cremind-tag connect server https://cremind.example.org:1180 [--ca-file cremind-ca.pem]
cremind-tag connect add-hardware "CremindTag tagc_….<secret>"
cremind-tag connect add-content  "CremindTag tagc_….<secret>"     # one per profile
cremind-tag connect list          # ids, kinds, whether the secret is stored (never the secret)
cremind-tag connect test          # GET whoami with every credential
cremind-tag connect remove tagc_…
```

- `server` checks that the URL answers (an unauthenticated `whoami` must come
  back 401) before saving it. TLS uses the system trust store plus `--ca-file`
  (a PEM bundle, e.g. the CA Cremind's HTTPS setup exports). A plain-HTTP URL for
  a Cremind that moved to HTTPS, an untrusted certificate or a redirect to
  `https://` is reported as a configuration error with the command to fix it.
- `add-*` verifies the credential with `whoami` (its kind must match; `--no-verify`
  stores it offline). Ids go to the config file, secrets to the secret store
  (OS keyring, service `cremind-tag`, key `credential:<id>`; or `secrets.json`).
- Restart the daemon after changing credentials.

## 3. Hardware

- **Gateway**: flash the gateway firmware, plug it in, set
  `hardware.gateway_url`; `cremind-tag gateway info` checks it.
  `cremind-tag firmware flash --target gateway-nrf52840dk --hex <image>`
  verifies a release or `build/` image against its metadata and programs it
  over the DK's J-Link (bridges likewise); `cremind-tag firmware
  list|verify|info` show a release's images, check one, and read a board's
  FICR/UICR ([releasing.md](releasing.md#first-release-j-link-and-flashing)).
- **Bridges**: provisioned from Cremind (admin: *Scan* → *Provision*, which the
  daemon executes as `scan_unprovisioned` / `provision_bridge`) or locally with
  `cremind-tag mesh scan|provision`. Each bridge needs the same font pack as the
  companion: `cremind-tag bridge fonts-install PACK --url <maintenance port>`
  ([fontpack.md](fontpack.md) §4), or Cremind's `install_fontpack` when
  `daemon.bridge_maintenance` names the bridge's port.
- **Tags**: enrolled over SWD with `cremind-tag tag enroll`
  ([enrollment.md](enrollment.md)); the secret stays in this PC's secret store.
  The daemon reports enrolled tags to Cremind; an admin then *claims* a tag for
  a profile (Cremind queues `assign_tag` + `clear_tag`, which the daemon runs).
- **No hardware yet?** `cremind-tag sim run --register --pack PACK` runs a
  simulated gateway, bridges and tags ([simulator.md](simulator.md)) and adds
  them to the inventory; point `hardware.gateway_url` at the printed
  `socket://…` URL (use a separate data dir and `CREMIND_TAG_SECRETS_BACKEND=file`).

## 4. The daemon

```bash
cremind-tag daemon run [--gateway COM7] [--pack PACK] [--font-cache DIR] [-v]   # until Ctrl-C
cremind-tag daemon run --once [--max-seconds 120]   # catch up, deliver what is due, wait for its
                                                    # results, flush receipts, exit (1 if not caught up)
cremind-tag daemon status [--json]                  # running?, gateway, credentials, holds, queue
```

The daemon logs to `<data dir>/logs/daemon.log` (rotated, 3 × `daemon.log_max_bytes`)
and stderr — key=value lines, never secrets or card text — and writes
`daemon-status.json` every 2 s. To run it at logon on Windows, create a Task
Scheduler task running `cremind-tag daemon run`; on Linux a systemd user unit
with `ExecStart=… cremind-tag daemon run` and `Restart=on-failure`. A daemon
that exits (a task failed, or a signal) loses nothing: everything it accepted is
in the database and resumes at the next start.

### What it runs

| Loop | Does |
|---|---|
| content (per content credential) | `POST sync` at start, every `resync_s` (300 s), after `410 cursor_expired`, a changed `stream_id` or an unknown tag; then `GET events` every `active_poll_s` (2 s) while jobs arrive, doubling to `idle_poll_s` (10 s) when idle |
| hardware (hardware credential) | `POST inventory` at start and on every hardware change (bridges carry `max_tags` and `assigned` from their CAPS, the bridge's own count of assigned tags — the gateway's caps `assigned_count`, else the gateway's assignments for it — when the gateway has reported them; tags carry `max(epoch, epoch floor)`); `POST heartbeat` every 30 s; long-poll `GET commands` → claim → execute → result |
| scheduler | expiry; composes changed card sets into revisions; sends due revisions as `DELIVER_LAYOUT` |
| gateway event handler | `EVT_STAGE`/`EVT_RESULT`/assignment and provisioning results → the queue, committed before the event is ACKed |
| outbox (per credential) | `accepted`, `receipts`, `previews`, command results, until Cremind confirms |

A 401/403 (revoked, invalid, wrong kind) stops only the loops of that
credential. A TLS configuration error (an untrusted certificate, an `https://`
URL for a plain-HTTP server, a plain-HTTP URL for a server that moved to HTTPS)
pauses them and retries every `tls_retry_s` (300 s) — it may be fixed on the
server side. `daemon status` shows both (`stopped` / `tls_error`). Transient
failures — 5xx, network errors, and an EOF or reset during the TLS handshake (a
restarting server, a proxy dropping the connection) — retry with bounded
exponential back-off and jitter (`connector_retry_max_s`, 60 s).

`POST receipts` may answer `rejected` receipts (`{"delivery_id", "reason"}`):
`terminal`, `not_owned` and `unknown` are dropped (Cremind already has the final
word, or the delivery is not this profile's); `epoch_mismatch` — an assignment
moved the delivery to another epoch — makes the credential re-sync, and the
sync sends the terminal receipt again with the current epoch. Connector
timestamps are accepted with or without milliseconds. Previews carry the tag
`epoch` they were rendered for; a preview Cremind refuses with 409
`epoch_mismatch` (the tag changed hands or was reassigned) is dropped, not
retried, and the credential re-syncs.

A command whose claim failed — including a claim Cremind committed although its
answer was lost (Cremind never offers a claimed command again) — stays
`claiming` locally and is claimed again, with back-off, before every command
poll; `409 already_claimed` with status `claimed` then means it is ours.

## 5. From a job to a screen

**Jobs and the card set.** A job is one Cremind delivery (connector-api.md "Job
shape"). Per tag the companion keeps the *card set*: the owner's active jobs.
Every content job has a `replace_key` (`delivery:<id>` when it shares none). A
`resolved` job removes the cards whose `replace_key` equals its `resolves`;
a job with a `replace_key` replaces older cards with that key; `clear` removes
every older card and blanks the tag until new content arrives. Cremind already
ended the replaced deliveries, so these need no receipt. A cancel in Cremind
arrives the same way — a `resolved` job naming the card's key: a card that is on
a screen still being delivered leaves it (the screen is composed again) and is
never receipted `displayed`. `events` may also return jobs that are already
final in Cremind (cancelled or expired while the companion was away): they are
recorded by their `stage` and never shown.

**Validation (defence in depth).** Before a card joins the set, its `title` and
`body` are checked with the rules of Cremind's sanitiser: one-time codes next to
`otp`/`code`/`pin`/…, bearer/API tokens, `password=…` pairs, long hex/base64
runs, and raw tool or terminal output (ANSI escapes, code fences, tracebacks,
shell prompts, log lines). Such a card is refused — never rewritten — and
receipted `failed` with detail `refused_by_companion`; its text is never logged.

**Composition.** The screen is `compose_screen` of the card set (headline,
up to three more cards, footer "N more updates waiting…", [layout.md](layout.md)),
`compose_identify` for an `identify` command, `compose_blank` after a `clear`.
Times are shown in the profile's `timezone` (an IANA name; a Windows zone id
or a bare UTC offset is mapped defensively, anything else shows UTC). It is
composed again only when its inputs change (cards, panel, rotation, profile
settings, font pack — not the clock) and, even then, sent only when the
layout digest differs from the current revision's. A change that only moves a
progress bar waits until `progress_cadence_s` (profile setting, 300 s) after
the previous revision. A tag nothing was shown on yet, with no cards, is left
alone (a fresh database never paints "No updates" over a screen).

**Revisions.** Every new screen gets the next number from the inventory's
per-tag allocator (`Database.allocate_revision`, never decreasing, continued
above the `desired_revision`/`displayed_revision` Cremind reports at `sync`). A
revision lists the deliveries it **shows** (`delivery_ids`) and those only
counted in its footer (`pending_delivery_ids`). A newer revision supersedes an
undelivered older one; the shown cards move with it. When revision R is
displayed, exactly its `delivery_ids` are receipted `displayed` (with revision,
frame digest and timing); footer-only cards stay active until a later screen
shows them, they expire, are resolved or cancelled.

**Delivery.** `DELIVER_LAYOUT {bridge, tag, epoch, revision, update_id, fontpack_id,
layout}` with the tag's current assignment and the active pack id; the `op_id`
(also the `update_id`) is persisted with the revision before the request. The
layout is at most `LAYOUT_SERIAL_MAX` (4000) bytes.

| Answer / result | Companion does |
|---|---|
| `ACCEPTED` (or a `DUPLICATE` of it) | revision `sent`; receipts `gateway_received` |
| `BUSY`, `NO_RESOURCES` | retry shortly with the same op id (not remembered by the gateway) |
| `EVT_STAGE` | receipts `bridge_received` / `transferring` / `refreshing` |
| `EVT_RESULT OK` | revision `displayed`; its `delivery_ids` receipted `displayed`; displayed preview uploaded; older undelivered revisions superseded. With `flags` bit0 (`RESULT_FLAG_DUPLICATE`: the tag answered from its stored ACK, the revision was already on the panel and nothing was redrawn) the receipts carry `detail` "duplicate (stored acknowledgement)" |
| `DISPLAY_STATE_UNKNOWN` | re-deliver the **same** revision (new op id) — the tag repeats the refresh (§6); a job whose TTL runs out meanwhile is receipted `uncertain` |
| `STALE_REVISION` | jump the allocator (below) and compose again |
| `REVISION_CONFLICT` | compose again under a new revision |
| `SUPERSEDED` | nothing (a newer revision carries the cards) |
| link failures: `TIMEOUT`, `DISCONNECTED`, `CONNECT_FAILED`, `MESH_SUSPEND_FAILED`, `MESH_RESUME_FAILED`, `INCOMPLETE`, `DIGEST_MISMATCH`, `PANEL_ERROR`, `REFRESH_TIMEOUT`, `STORAGE_ERROR`, `INTERNAL`, `CANCELLED` | same revision, new op id, exponential back-off (`retry_initial_s` 5 s … `retry_max_s` 600 s) until the jobs expire (`expired`) |
| `NOT_FOUND` | ambiguous in `EVT_RESULT` (the bridge lost the transfer, or the tag refused its id, §10): retried like a link failure; the third `NOT_FOUND` for one revision is treated as below. A later `OK` for that revision is still recorded as displayed and lifts the block |
| `STALE_EPOCH` whose `stored_epoch` is above the attempt's epoch | the tag authenticated a newer epoch (an assignment this database lost, a restore): the **epoch floor** rule below — the tag is held (blocked `stale_epoch`, its jobs stay active), the inventory reports the floor, Cremind re-queues `assign_tag` above it, and the successful assignment moves the held work to the new epoch |
| `AUTH_FAILED`, `SECURITY_CONFIG`, `NOT_ASSIGNED`, `STALE_EPOCH` (any other), `VERSION_MISMATCH` (and a repeated `NOT_FOUND`) | stop the tag's work of that epoch: its unfinished jobs of that epoch or older are receipted `failed` with the status code and a detail, the tag is **blocked**, and the assignment is re-synced (`sync` + `inventory`); a successful `assign_tag` unblocks it. Such a result for an older attempt, or for an epoch below the tag's current one (a bridge still holding an old key), is ignored |
| `FONTPACK_MISMATCH` | the revision's jobs `failed`; the tag waits (blocked) until the bridge reports the companion's pack, `install_fontpack` succeeds or the daemon restarts |
| `INVALID`, `TOO_LARGE`, `UNSUPPORTED` | the revision and the cards it shows `failed` |
| a revision `sent` without any result for `result_timeout_s` (30 min) | sent again (a result lost from the gateway's retention ring, §1.2) |
| gateway `boot_id` changed (also across companion restarts) | every sent-but-unresolved revision is sent again |

The gateway emits exactly one `EVT_RESULT` per `update_id`, and a `LAYOUT_COMMIT`
repeated after a lost `LAYOUT_STATUS` answers `DUPLICATE` (never `NOT_FOUND`)
— docs/protocol.md §10; the simulator follows both rules.

**The `STALE_REVISION` rule.** The refusal says only that the tag (or bridge)
has seen a higher revision than this database knows — after a restored or lost
database. The companion cannot learn how much higher, so it raises the tag's
allocator to `max(last used, refused revision) + 65 536 × 2^k` and composes
again, where `k` counts consecutive refusals of that tag (at most 8, reset by a
displayed revision). 65 536 revisions are more than a year of one screen change
every ten minutes; the doubling bounds the number of retries.

**Epoch floor (the `STALE_EPOCH` rule).** Every `EVT_RESULT` carries the tag's
`stored_epoch` as the bridge last heard it (protocol.md §3.4); the bridge ends
jobs with a tag's `STALE_EPOCH` only after 3 consecutive refusals (`flags`
bit1, §10). When that `stored_epoch` is above every epoch this database knows
for the tag — its assignment, Cremind's epoch from `sync`, an earlier floor —
the tag has authenticated an assignment the companion lost (a restored or lost
database, a second companion). The companion records it as the tag's **epoch
floor** (`tag_views.epoch_floor`, schema v4), reports `max(epoch, floor)` as the
tag's inventory `epoch`, and Cremind — which keeps `max(stored, reported)` —
re-queues the owner's `assign_tag` (and a pending `clear_tag`) at `floor + 1`.
Until then the tag is held (`blocked (stale_epoch)`, no job fails); the
assignment unblocks it and moves its jobs and pending revisions to the new
epoch, and the tag accepts it. The value is unauthenticated (a plaintext
`ERROR`), so one report raises the floor by at most **256** above the highest
known epoch (`EPOCH_FLOOR_STEP`; a real gap larger than that closes in a few
rounds of 3 sessions each), never above `2^32 − 2`, and never lowers anything:
a forged `stored_epoch` costs epoch numbers, never access. An `assign_tag` at or
below the floor fails at once (the inventory reports the floor, Cremind
re-queues above it). A `STALE_EPOCH` without a higher `stored_epoch` (the
bridge's own refusal of an older epoch, `stored_epoch = 0`) still stops the tag
as in the table.

**Assignment and ownership.** Cremind's claim bumps the tag's epoch, sets
`clear_required` and queues `assign_tag` then `clear_tag`. While
`clear_required`, no content is composed for the tag (the old owner's screen
must go first). `assign_tag` derives `K_epoch` from the tag secret, sends
`ASSIGN_TAG` to the new bridge, records the assignment, moves unfinished jobs
and revisions to the new epoch/bridge, then `UNASSIGN_TAG` on the previous
bridge. `clear_tag` sends `TAG_COMMAND CLEAR` at the new epoch and succeeds only
on `EVT_RESULT OK`; the tag is then white at revision 0 and content resumes.
While the tag is away, the step's timeout (15 min) re-sends the request with
the **same** op id — the gateway answers from memory and queues nothing new, so
the bridge holds one `CLEAR`, not one per timeout; only a gateway reboot sends
under a new op id.

## 6. Hardware commands

| Kind | The companion |
|---|---|
| `scan_unprovisioned {duration_s}` | `SCAN_UNPROV`, collects `EVT_UNPROV_BEACON`s → `{beacons: [{uuid, hw_id, rssi, oob}]}` |
| `provision_bridge {uuid, name}` | `PROVISION` → `EVT_PROVISIONED` → `CONFIGURE_NODE` → `EVT_NODE_CONFIGURED`; inventory updated |
| `configure_bridge {hw_id}` | `CONFIGURE_NODE` → `EVT_NODE_CONFIGURED` |
| `remove_bridge {hw_id}` | `REMOVE_NODE` → `EVT_NODE_REMOVED`; the bridge leaves the inventory |
| `identify {hw_id}` | a bridge: `IDENTIFY_NODE`; a tag: the identify screen as one new revision, held `identify_hold_s` (60 s) after it is displayed, then the regular screen returns |
| `refresh_tag {tag_id}` | the current screen as a new revision; succeeds when it is displayed |
| `assign_tag {tag_id, bridge_hw_id, epoch}` | see §5. A bridge whose assignment table is full (`ASSIGN_SET` answers `NO_RESOURCES`: `CAPS max_tags` tags, 10 on an nRF52832 bridge, 20 on an nRF52840) fails it at once, not retried, with error `bridge_full` and result `{"error": "bridge_full", "max_tags": n}` (`max_tags` from the gateway's inventory, left out when unknown); Cremind marks the tag `assign_failed`, drops it from that bridge and records `max_tags` |
| `clear_tag {tag_id, epoch}` | see §5 |
| `install_fontpack {bridge_hw_id}` | with a maintenance port (`daemon.bridge_maintenance`, or `hardware.bridge_url` when there is one bridge): `FONT_*` install of the configured pack; otherwise fails with the operator instruction |
| `collect_diagnostics {}` | companion version/host, queue statistics, blocked tags, gateway `INFO` counters, bridge info |

Commands of one tag run in arrival order; mesh changes are serialised.
`assign_tag` and `clear_tag` run to completion even past their expiry — Cremind
accepts a late `succeeded` — and a `clear_tag` Cremind re-queued for a clear this
companion already performed at that epoch succeeds at once
(`already_cleared`) without refreshing the panel again.

## 7. Durability

The daemon's promise: **a job Cremind issued and the companion acknowledged
(`accepted`) is never lost, and no delivery is ever reported with two different
terminal outcomes.** Every state change is one SQLite transaction
(`synchronous=FULL`, WAL) committed before anyone is told about it; every
message to Cremind or the gateway is idempotent, so repeating it after a crash
is harmless. Boundary by boundary (each is a crash-injection point in the tests):

| Crash … | Why nothing is lost |
|---|---|
| after an events page arrived, before it was committed | the cursor did not move; the restart's `sync`/`events` returns the same jobs |
| after the page committed, before `accepted` was POSTed | jobs, the new cursor and the `accepted` message are one transaction; the outbox POSTs it after the restart (`accepted` only moves `queued` deliveries — idempotent) |
| after a revision was persisted, before `DELIVER_LAYOUT` | the revision and its op id are in the database; the scheduler sends it after the restart |
| after `DELIVER_LAYOUT` was sent, before its answer was recorded | the same op id is sent again; the gateway answers from its op-id memory (`DUPLICATE`) without doing the work twice, or, when the result already arrived, the revision is `displayed` and the answer changes nothing |
| inside the gateway event handler, before its transaction committed | the client ACKs a retained event only after the handler returned; the gateway re-sends it after the next `HELLO`, and the handler (idempotent by tag + revision, or op id) applies it once |
| after a result was committed, before the receipts were POSTed | the receipts are outbox rows; the sender POSTs them after the restart (receipts never move a stage backwards and a terminal outcome is final) |
| after a hardware command was claimed | the command was persisted before the claim; at start it is claimed again (`409 already_claimed` with status `claimed` means it is ours) and runs; each step's op id is persisted before its request |
| the local database is lost | `sync` without a cursor rebuilds the outstanding jobs from Cremind; revisions continue above what Cremind reported (a tag that has seen more answers `STALE_REVISION` → the jump rule) |
| Cremind restored from a backup | a new `stream_id` or `410 cursor_expired` → `sync` rebuilds and drops local jobs Cremind no longer lists |

A terminal outcome is written once per job (`outcome IS NULL` guards every
update) and its receipt is enqueued in the same transaction, so two different
outcomes can never be produced. If Cremind still lists a delivery the companion
already finished (a receipt was refused because an epoch moved meanwhile), the
next `sync` sends the terminal receipt again with the current epoch.

## 8. The queue

```bash
cremind-tag queue list [--all] [--tag 1A2B3C4D] [--json]
cremind-tag queue show 501           # the job and every revision that carried it
cremind-tag queue retry 501 | --tag 1A2B3C4D   # unblock, send now, compose afresh
cremind-tag queue cancel 501         # drop the card; Cremind is told `cancelled`
cremind-tag queue purge --older-than 7d
```

| Job `state` | Meaning |
|---|---|
| `active` | in the tag's card set (possibly already displayed) |
| `resolved` / `superseded` | removed by a `resolved` job / replaced by a newer card with its `replace_key` |
| `cancelled` | Cremind ended it (claim, clear, restore, cancel) or an operator cancelled it |
| `expired` | past `expires_at` |
| `done` | an instruction (`resolved`, `clear`) whose effect is on screen |
| `failed` | refused by the validator, or stopped (security/configuration) |

`outcome` is what the companion reported (`displayed`, `expired`, `uncertain`,
`failed`, `cancelled`) or learned (`superseded`); `last_stage` the highest stage
receipted. The running daemon notices changes made with these commands within
`daemon.scan_interval_s`.

## 9. Doctor and diagnostics

`cremind-tag doctor [--gateway URL] [--pack PACK] [--json]` checks, each `ok`,
`warn` or `fail` (exit 1 on a failure): the configuration and data directory;
the database (`PRAGMA integrity_check`, schema version); the secret store
backend; Cremind reachability and TLS; every credential's `whoami`; the gateway
(`HELLO`, `INFO`) and that every bridge reports the companion's font pack id;
the font pack (format and verified font files); an SWD tool for enrollment.

`cremind-tag diag collect --out diag.zip [--gateway URL] [--no-counters]` writes
the configuration (credential ids only — secrets are never read), versions,
the daemon status, queue statistics (ids, kinds, states, stages — no card text,
no payloads), the inventory (secret references only), the gateway's counters and
the tail of the daemon logs.

## 10. Troubleshooting

| Symptom | Look at |
|---|---|
| `daemon status`: credential `stopped` with `credential_revoked` | the credential was revoked in Cremind: create a new one, `connect add-…`, restart |
| TLS error "not trusted" (`tls_error` in `daemon status`) | `connect server URL --ca-file CA.pem` (the CA Cremind's HTTPS setup exports); the daemon retries every 5 min |
| doctor: "secrets file … grants access to …" | the secrets file is readable by other users (an old file, or a data directory outside your profile): the next write fixes it, or `CREMIND_TAG_DATA_DIR` under your profile |
| "now serves HTTPS" | `connect server https://…` |
| a tag `blocked (stale_epoch)` / `(not_assigned)` | the tag's key is older than its epoch: the inventory reports the tag's epoch floor and Cremind's re-queued `assign_tag` unblocks it (§5 "Epoch floor"; it takes 3 refused sessions, about 1.5 min); else re-assign it in Cremind (admin → Tags hardware → Assign) |
| a tag `assign_failed` in Cremind, command error `bridge_full` | the bridge holds `max_tags` tags already: assign the tag to another bridge (or release one) |
| a tag `blocked (not_enrolled)` | Cremind routes jobs to a tag this database does not know (a lost database): enroll/register it again |
| deliveries `failed` with `FONTPACK_MISMATCH` | install the companion's pack on the bridge (`bridge fonts-install` or `install_fontpack`) |
| `held (waiting for clear_tag)` | the claim's `clear_tag` has not succeeded yet (the tag must wake up: about 30 s) |
| nothing moves | `doctor`, then `daemon.log`; `queue list` shows what waits and why |

## 11. Tests

`uv run pytest -q` runs, among others, the daemon against a stateful fake
Cremind (`companion/tests/daemon/fake_cremind.py`, `httpx.MockTransport`) and the
simulator: jobs to displayed receipts and previews, crash injection at every
durability boundary with a restart, resynchronisation (expired cursor, restored
Cremind, lost database), duplicates, `STALE_REVISION`, the epoch floor healing
a `STALE_EPOCH` end to end (the fake Cremind re-queues `assign_tag` above a
reported epoch as Cremind does), `bridge_full`, power loss during a refresh,
expiry, progress cadence, the claim's `clear_required` hold, epoch changes,
refused cards and revoked credentials.

The first vertical slice runs the real Cremind: `CREMIND_E2E=1 uv run pytest
tests/e2e -q` or `uv run --project companion python tools/e2e_slice.py` (from the
repository root) boots a throwaway Cremind from its repository with a fresh
`CREMIND_SYSTEM_DIR` under `build/e2e-slice/` (never a copy of `~/.cremind`),
registers a companion, claims a simulated tag, and checks that a pinned note
and an event run's outcome card reach stage `displayed` with a revision, a
digest and a displayed preview; it prints the time each stage took.
