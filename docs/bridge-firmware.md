# Bridge firmware (`apps/bridge`)

The bridge is a Bluetooth Mesh relay node that receives logical screens
("layouts") from the gateway, renders them for each assigned tag with the font
pack in its external flash, and pushes the image to the tag over a BLE GATT
connection. This document describes the firmware; the protocols it implements
are normative in [protocol.md](protocol.md) (§1.6, §2, §3, §4, §5, §10) and
[fontpack.md](fontpack.md). The companion's simulator
(`companion/src/cremind_tag/sim/bridge.py`) implements the same rules;
§12 lists every place the firmware differs and why.

| Target | Board | Status | Memory |
|---|---|---|---|
| `bridge-nrf52840dk` | `nrf52840dk/nrf52840` (+ the DK's 8 MiB MX25R64) | builds, verified, meets targets (two tag sessions at once); not yet run on hardware | [§9](#9-memory) |
| `bridge-nrf52dk` | `nrf52dk/nrf52832` (+ a placeholder SPI NOR) | builds, verified, meets targets (8,409 B RAM free, 10 tags per bridge, 1 QR slot, one tag session); not yet run on hardware | [§9](#9-memory) |

Build: `python tools/build.py bridge-nrf52840dk bridge-nrf52dk` (the companion
venv's interpreter on Windows: `unset VIRTUAL_ENV; companion/.venv/Scripts/python.exe tools/build.py …`);
see [building.md](building.md).

---

## 1. Architecture

```
            Bluetooth callbacks (BT RX thread, mesh contexts)
  scan recv ─┐  connected/disconnected ─┐  GATT callbacks ─┐   vendor model handlers ─┐
             ▼                          ▼                  ▼                           ▼
        bev_q (k_msgq: advert, conn, GATT events, values)              mesh_rx_q (k_msgq)
             └──────────────────────────────┬───────────────────────────────┘
                                            ▼
                         bridge work queue "bwq" (one thread, prio 10)
   ┌──────────────┬──────────────┬───────────────┬──────────────┬─────────────────┐
   │ mesh.c       │ delivery     │ sched         │ tagsess      │ central.c       │
   │ models, TX   │ §3.3, jobs,  │ §5.2 window   │ §5.3–5.6     │ GATT discovery, │
   │ queue        │ results      │ (pure)        │ (pure)       │ sched/tsess ops │
   └──────────────┴──────┬───────┴───────────────┴──────┬───────┴─────────────────┘
                         │ transfers, pending ring,     │ glyph reads (cache)
                         │ directory                    │
                         ▼                              ▼
                    bflash (external NOR) ◄──── fontstore (mutex) ◄─── maintenance thread = main
                                                                       (maint.c over UART/USB)
```

- **Everything runs on one work queue.** Bluetooth callbacks and mesh model
  handlers only copy their event into a queue (`bev_q`, `mesh_rx_q`) and
  submit a work item; the protocol state lives in structures owned by the
  work queue, so the core needs no locks. Advertisements are dropped once
  `bev_q` is half full, so they never crowd out connection or GATT events.
- **The core is Bluetooth-free** (`src/core/`): `bflash` (external flash),
  `fontstore` (packs), `delivery` (§3, §10), `sched` (§5.2), `tagsess`
  (§5.3–§5.6) and `maint` (§1.6). Time is passed in or read through an ops
  table, Bluetooth operations go through `sched_ops` / `tsess_io`, and
  persistence through a `save` callback — which is how the native_sim tests
  run the same code with mocks, the flash simulator and a fake tag (§10).
- **The maintenance port** has its own low-priority thread: font installs
  (seconds of flash and SHA-256 work) never delay mesh processing. It shares
  only the font store with the work queue, behind the store's mutex (and
  reads the counters for INFO). That thread is the **main thread**: `main()`
  starts the work queue, submits the boot and then runs the port's loop, so
  one stack (`CONFIG_MAIN_STACK_SIZE`) serves the kernel's init and the port.
- The boot runs as the first work item of the work queue (flash and pack
  validation, `bt_enable`, `bt_mesh_init`, `settings_load`, pending-layout
  restore), then it opens the port and the main thread starts serving it.
- **One layout buffer** (`LAYOUT_HARD_MAX`, in the delivery core) is shared:
  `LAYOUT_COMMIT` reads the transfer into it to check and validate it, the
  session renders its job's layout from it (§3, §5). A transfer being
  received lives in its external-flash record, not in RAM.

| File | Role |
|---|---|
| `src/main.c` | boot, the work queue, the event queue, DELIVERY_RESULT re-send timer, INFO counters |
| `src/mesh.c` | composition, provisioning, vendor models, outbound queue |
| `src/central.c` | scan callback, connection lifecycle, GATT discovery/cache/subscriptions, `sched_ops`, `tsess_io` |
| `src/maint_port.c` | UART / CDC ACM transport of the maintenance port |
| `src/persist.c` | settings handler (`ctag/…` records) |
| `src/identify.c` | IDENTIFY / Health attention LED |
| `src/core/*.c` | the portable core (tested on native_sim) |

## 2. Mesh node

- **Provisionee**, PB-ADV only, no OOB (protocol.md §2's documented risk).
  Device UUID: `"CTBR"`, the board id, three zero bytes, then the 8-byte FICR
  device id (`hwinfo_get_device_id`), so `cremind-tag mesh scan --filter 43544252`
  lists only bridges.
- **Composition**: one element with the Config Server, the Health Server
  (attention blinks the LED), and the vendor models `LAYOUT_SRV` (0x0001) and
  `MGMT_SRV` (0x0003) of company `MESH_COMPANY_ID` (0xFFFF, the SIG test
  value — production needs an assigned id). Opcodes are
  `CTAG_MESH_OPCODE_*` (= `BT_MESH_MODEL_OP_3(op, cid)`), parameter lengths
  are checked by the access layer (`BT_MESH_LEN_EXACT`/`_MIN`) and again by
  the generated unpackers.
- **Features**: relay on (retransmit 2 × 20 ms; the gateway's
  `CONFIGURE_NODE` sets it too), Friend, LPN, GATT Proxy and PB-GATT off,
  `BT_MESH_ADV_EXT` (never the legacy advertiser: `bt_mesh_suspend()` can
  block forever with it — firmware-notes correction 4), `TX/RX_SEG_MAX = 16`
  (= `MESH_MIN_SEG`), default TTL 5, no CDB.
- **Settings** (NVS on the internal `storage_partition`) hold the mesh state
  and the bridge's own records: `ctag/a/<i>` assignment (tag id, epoch,
  `K_epoch`, primary flag), `ctag/h/<i>` history (last accepted revision,
  layout digest, its current `update_id`, the final result and the
  `result_seq` it was reported under), `ctag/rs` the result_seq reservation.
  Transfers and pending layouts live in external flash (§7).
- **Ready for tag connections** only when provisioned, both vendor models are
  bound to an app key, and at least `CTAG_BRIDGE_CFG_QUIET_MS` (10 s) have
  passed since provisioning completed (the gateway's `CONFIGURE_NODE` is then
  over). `bt_mesh_suspend()` itself refuses while a provisioning link is open.
- **Outbound**: one queue (`CTAG_BRIDGE_MESH_TXQ` entries), one
  `bt_mesh_model_send()` at a time — the next is handed over from the send's
  `end` callback (a 10 s watchdog frees a lost one); `-EBUSY` retries after
  50 ms. The queue is held while the mesh is suspended. Replies go to the
  requester's address, everything unsolicited (results, stages, TAG_SEEN) to
  the gateway `0x0001`; LAYOUT_STATUS / DELIVERY_* leave from `LAYOUT_SRV`,
  CAPS / HEALTH / ASSIGN_STATUS / TAG_SEEN from `MGMT_SRV`.
- **Config Node Reset** (`REMOVE_NODE`): a running session ends, assignments,
  history, jobs and pending records are forgotten, unprovisioned beaconing
  restarts.

### Management model

| Request | Answer |
|---|---|
| `CAPS_GET` | `CAPS_STATUS`: proto 1, fw (`apps/bridge/VERSION` through `app_version.h`, like the tag's CAPS and the gateway's HELLO; tools/version.py keeps it equal to the repository's `VERSION`), board 3/4, active pack id (zeros when none), `flash_mib`, `max_tags` (`CTAG_BRIDGE_MAX_TAGS`: 20 = `MAX_TAGS_PER_BRIDGE` on the nRF52840, **10 on the nRF52832**, §9), assigned count, flags bit0 pack valid, bit1 busy (an attempt or session in progress) |
| `HEALTH_GET` | `HEALTH_STATUS`: uptime, sessions ok/fail, suspend count, max suspend ms, resume failures, queue depth (jobs), last status |
| `ASSIGN_SET` | `STALE_EPOCH` below the stored epoch; `NO_RESOURCES` beyond `max_tags` (a 21st tag, an 11th on the nRF52832); a higher epoch cancels the older epoch's jobs; the same epoch overwrites key and flags |
| `ASSIGN_DEL` | `OK` when absent (§10); `STALE_EPOCH` when the stored epoch is newer; cancels the tag's jobs of that epoch and older (inclusive, so `epoch` 0xFFFFFFFF cancels them all); a job in a session ends `CANCELLED` when its session ends |
| `TAG_CMD` | `NOT_ASSIGNED` / `STALE_EPOCH` results at once; `CLEAR` and `SLEEP` queue a job for the tag's next session; `IDENTIFY` / `REFRESH` (and unknown commands) answer `UNSUPPORTED` at once. A repeated `TAG_CMD` (the gateway re-sends it when a segment ACK was lost) queues no second job and re-sends the first result under its `result_seq` |
| `IDENTIFY` | blinks the DK LED (`led0`) for the given seconds |
| — | `TAG_SEEN` for an assigned tag's advertisement, at most once per 60 s per tag (RSSI, the battery from its last CHALLENGE, the advertising flags) |

## 3. Layout reception (`LAYOUT_SRV`, protocol.md §3, §10)

One transfer is received at a time, **straight into external flash**: a
`LAYOUT_BEGIN` replaces any transfer, takes the next free slot of the pending
ring (§7) and erases it; each `LAYOUT_CHUNK` is written into that slot at its
own 4-byte-aligned position (chunk *i* at `64 + 152·i`), in whatever order
the chunks arrive. A chunk already received is ignored (NOR is written once
per erase; the digest decides anyway), a chunk of another transfer or with an
index ≥ `chunk_count` (or ≥ 32) is ignored and counted. A `LAYOUT_BEGIN`
whose `total_len` is 0 or above `LAYOUT_HARD_MAX` takes no slot (the commit
fails on its length). Until the commit accepts it the slot has no header, so
a reset forgets the transfer (the commit then answers `NOT_FOUND`).

`LAYOUT_COMMIT` validates **in the order of §3.3** and answers `LAYOUT_STATUS`:

1. a transfer with this `xfer_id` exists → `NOT_FOUND`
2. all chunks present → `INCOMPLETE` + the `missing` bitmap (the transfer stays open)
3. `total_len` and the concatenated length ≤ `LAYOUT_HARD_MAX` → `TOO_LARGE`
4. lengths consistent, every chunk but the last exactly 150 bytes → `INVALID`
5. `SHA-256(layout)[0:16]` = digest, over the layout read from the slot into
   the shared layout buffer → `DIGEST_MISMATCH` (`STORAGE_ERROR` when the
   slot could not be erased, written or read)
6. the tag is assigned here with exactly this epoch → `NOT_ASSIGNED` (none, or an older assignment) / `STALE_EPOCH`
7. revision: below the history's revision, or equal with another digest → `STALE_REVISION`;
   equal with the same digest → **DUPLICATE** (below)
8. `fontpack_id` = the active, validated pack → `FONTPACK_MISMATCH`
9. `ctag_layout_validate()` on the shared buffer — §4.3 bounds in order, then
   every strike against the active pack → `INVALID` / `UNSUPPORTED` /
   `TOO_LARGE` / `FONTPACK_MISMATCH`

Accepted (`OK`), in this order: the job table must have room (a free entry,
or an older pending layout of the tag that the new one replaces) — else
`NO_RESOURCES`; the slot is **sealed** — its 64-byte header (with a CRC over
header and layout) written last — else `STORAGE_ERROR`; only then does the
history take the new revision (saved), any older pending layout of the tag
that is **not** in a session end `SUPERSEDED` (a layout being streamed
finishes; the new one waits for the next session), and the new job take the
sealed record. A refusal changes neither the history nor the older jobs.

**The shared layout buffer.** Steps 5 and 9 need the layout contiguous in
RAM, and the tag session renders from it (§5). Both run on the work queue,
so they never overlap: a commit may take the buffer between two strips of a
frame; the session then reloads its job's layout from the job's record (CRC
and identity checked) and re-initialises the renderer before the next strip
— the same bytes, so the frame is unchanged (tested, §10).

**A repeated `LAYOUT_COMMIT`** of a transfer that was answered `OK` or
`DUPLICATE` answers **`DUPLICATE`** again (§10: a lost status never ends a
delivery) and does nothing else — whatever became of its job meanwhile
(finished, cancelled, cleared). This holds across a reset: every accepted
transfer leaves a sealed record naming its `xfer_id`, and at boot the newest
intact record restores "the last accepted transfer" until the next
`LAYOUT_BEGIN`. A record torn by a power loss while sealing does not count
(that commit was answered `STORAGE_ERROR`).

**DUPLICATE** (§10, same revision and digest in a new transfer): a displayed
revision (stored result `OK`) re-sends its stored result under the new
`update_id`; a revision still pending adopts the new `update_id` (the new
transfer's sealed record replaces the job's, so a reset keeps the adoption,
and a session drawing it reloads from the new record); the `update_id` whose
result the history holds gets **that** result again and no new work,
whatever its status; any other revision that ended without being displayed
is accepted again as a re-delivery.

**Results** (§3.4): every final outcome is a `DELIVERY_RESULT` with a
bridge-local `result_seq`, re-sent every `MESH_RESULT_RETRY_MS` (2 s) up to
`MESH_RESULT_RETRIES` (5) times until `RESULT_ACK`. A result a tag session
produced carries the tag's `stored_epoch` (from its CHALLENGE or ERROR; after
`AUTH_OK` at least the session's epoch) and `flags`: bit0 when the tag
answered with its stored ACK (`RESULT.flags.bit0`), bit1 when an
unauthenticated status ended the job (§5); every other result carries 0 and
0. The history keeps the stored result's `stored_epoch` and `flags` with it
(`ctag/h/<i>` bytes 58–62), so a `DUPLICATE` replay repeats them. `result_seq` survives
resets without a settings write per result: the bridge persists the end of a
reserved block of 16 and starts the next boot there, so a sequence number is
never reused (§10). Before a result is sent, whatever keeps the layout from
being delivered again after a reset is durable: for the revision the history
records, the history (settings) is saved first, then the result is sent, then
the pending record is consumed — a record whose revision already has a stored
result is dropped at boot; any other job (`SUPERSEDED`, `CANCELLED`, an older
epoch) has its record consumed first. A reset in between never delivers a
layout twice.
Results waiting for an ack are kept in RAM only
(`CTAG_BRIDGE_RESULT_SLOTS`); after a reset the companion's job TTL covers a
lost one, as in the simulator.

**One `result_seq` per `update_id`** (§10): the bridge never sends two
`DELIVERY_RESULT`s with different `result_seq` for one `update_id`. The
results table keeps acknowledged and given-up results too (evicting the
oldest finished one first, an unacknowledged one only when nothing else is
left), and a result for an `update_id` it already holds re-sends that first
result under its own `result_seq` (unless its re-sends are still running)
instead of numbering a new one. The history keeps each tag's last reported
`(update_id, result_seq)` across resets for the DUPLICATE path. The ways a
second result could arise — a repeated commit, a repeated `TAG_CMD`, the same
`update_id` in a new transfer after a gateway restart — are covered by these
memories and tested (§10). Beyond them (an `update_id` whose result left both
the table and the history) the gateway's own "exactly one `EVT_RESULT` per
`update_id`" rule still holds.

`DELIVERY_STAGE` `TRANSFERRING` (authenticated, streaming) and `REFRESHING`
(the tag's `PROGRESS`) are best effort. `LAYOUT_CANCEL` ends a job that is
not in a session with `CANCELLED`. A successful `CMD CLEAR` resets the tag's
history to revision 0 (§10).

## 4. Tag connection scheduler (protocol.md §5.2)

`src/core/sched.c`: one **initiator** (one connection attempt at a time, each
in its own mesh suspend window) and `CONFIG_CTAG_BRIDGE_SESSIONS` **links**
(tag connections: **2 on the nRF52840, 1 on the nRF52832**, protocol.md §5.2
"Concurrent sessions"):

```
IDLE ──advert of T with work pending──▶ checks: T has no link, a link is FREE, every open session's link idles
  ▲                                            (its tag refreshing: tsess_link_idle), node ready (provisioned,
  │                                            configured, not configuring), per-tag back-off (BRIDGE_TAG_BACKOFF_MS
  │                                            after a failure; after CONNECT_FAILED one quick retry within
  │                                            TAG_ADV_WINDOW_MS), ≤ BRIDGE_MAX_SUSPENDS_PER_MIN attempts per minute
  │         the link: FREE ─▶ ATTEMPT
  │         own mesh sends in flight ─▶ WAIT_SENDS: poll 20 ms, ≤ 500 ms, else defer (no back-off, link FREE)
  │         bt_mesh_suspend() ── -EINVAL / -EBUSY ─▶ MESH_SUSPEND_FAILED, back-off, no connection attempt
  │                           ── other error ─▶ MESH_SUSPEND_FAILED, back-off, RECOVERY (mesh state unknown)
  │         bt_conn_le_create(link, T, timeout = BRIDGE_CONN_ATTEMPT_MS / 10 ms)
  │                     ── refused ─▶ resume ─▶ CONNECT_FAILED, back-off (+ quick retry)
  │  CONNECTING ──connected(link, err)──▶ bt_mesh_resume()   (suspend_ms = suspend → resume's return)
  │     │ watchdog 1.5 s ─▶ cancel (bt_conn_disconnect) ─▶ CANCELLING ──connected(err) or 2 s──▶ resume
  │     ▼
  │  resume ok ── err == 0 ─▶ IDLE; the link SESSION: GATT discovery, handshake, transfer (§5)
  │           └─ failed / cancelled ─▶ IDLE; CONNECT_FAILED, back-off (+ quick retry), the link FREE
  │  resume error ─▶ the link disconnected, MESH_RESUME_FAILED, RECOVERY: retry 100, 200, 400, 800, 1000… ms;
  │                  still failing after 5 s ─▶ sys_reboot (mesh reloads from settings)
  │  RECOVERY after an unknown-state suspend: each retry suspends fully, then resumes
  └── (per link) SESSION done ─▶ disconnect ─▶ DISCONNECTING ──disconnected (or 10 s)──▶ FREE
```

- **Two sessions (nRF52840).** A second tag is initiated only while the first
  session waits for its tag's `RESULT` with nothing left to send (the panel
  refresh after `FRAME_END` or a `CMD`, about 4 s for black/white, 15 s with
  red): the initiation's suspend window and the second handshake and frame
  then share the radio with a link that carries only empty connection events.
  Each link has its own connection (`CONFIG_BT_MAX_CONN=2`), GATT state
  (`central.c` `struct clink`), `struct tsess` with its own records, credits,
  pacing and deadline timers, and its own disconnect wait; the initiator's
  and the links' deadlines share one timer, armed for the earliest. A failing
  session or attempt on one link never touches the other (tested, §10). Both
  sessions render from the one shared layout buffer (§3): when a session's
  next job streams beside the other session's frame they take turns in it,
  each reloading its layout from its record when the other used it
  (`layout_reloads`).
- **Quick retry.** `CONNECT_FAILED` (the attempt ended without a connection:
  refused, timed out in the host, failed to be established, cancelled) marks
  the tag's back-off entry with one retry, allowed on an advertisement within
  `TAG_ADV_WINDOW_MS` (2 s) of the failure; the retry is a normal attempt
  (a suspend window, the rate limit) and a failed retry leaves the plain
  15 s back-off. Suspend and resume failures and failed sessions back off at
  once. The flag lives in the back-off entry's padding: no RAM.
- INFO counts both: `quick_retries` (attempts that were a tag's retry) and
  `concurrent_sessions` (sessions started beside another one).

- Tags are seen through the **mesh's own passive scan**: `bt_le_scan_cb_register()`
  sees every report (firmware-notes §3); the bridge never starts or stops
  scanning. Only connectable `ADV_IND`s with the §5.1 manufacturer data
  (company, version 1) count; the advertiser's address is the connection
  target.
- **Scanning and initiating never overlap** on the Zephyr controller: the
  mesh is suspended (which stops its scanner) before `bt_conn_le_create()`
  and resumed only after the attempt is over — connected, failed, or its
  cancellation confirmed by the `connected()` callback. A resume attempted
  while the controller still initiates fails (`-EPERM`) and the recovery
  loop retries it, so no path leaves the mesh suspended.
- **A suspend that fails part-way.** The pinned `bt_mesh_suspend()` stops the
  scanner, beacons and models before it disables the advertiser; if that
  last step fails it returns the error without flagging the mesh suspended,
  so a plain `bt_mesh_resume()` answers `-EALREADY` and restores nothing.
  Only `-EINVAL` (not ready) and `-EBUSY` (provisioning) are refusals that
  touched nothing; any other error puts the scheduler in RECOVERY with the
  mesh state unknown: each retry suspends fully (the scanner stop tolerates
  an already stopped scanner) and then resumes, and after 5 s without a
  working mesh the bridge reboots.
- **Liveness.** A provisioned bridge that is idle (its mesh scanning) and
  hears no advertising report at all — mesh traffic, beacons, tags — for
  `CONFIG_CTAG_BRIDGE_LIVENESS_S` (**N = 1800 s**) reboots: its scanner
  stopped without an error the scheduler could see. Every mesh node in range
  sends a secure network beacon at least every 600 s, so a working scanner
  hears something well within N; a bridge with nothing in range reboots
  every 30 min, harmlessly. Silence counts from the later of the last report
  and the moment the scheduler went idle (a session's pause never counts).
- **Nothing reaches the tag before the mesh has resumed**: GATT discovery and
  the handshake start only in `SESSION`.
- Link failures (`DISCONNECTED`, `TIMEOUT`, `CONNECT_FAILED`,
  `MESH_SUSPEND_FAILED`, `MESH_RESUME_FAILED`) never produce a result: the
  job stays pending and the tag is retried after the back-off (§10).
- Connection parameters: interval 30–50 ms, peripheral latency 0, supervision
  timeout 4 s; initiator scan interval = window = 30 ms.

### Timing rationale

| Value | Why |
|---|---|
| attempt 1 s (`BRIDGE_CONN_ATTEMPT_MS`) | the tag advertises every 250 ms for 2 s: ≥ 4 advertising events per attempt, while the mesh pause stays about one second |
| ≤ 6 attempts per rolling minute | bounds the mesh's suspended share to ~10 % (6 × ~1 s per 60 s) whatever the tags do; the native_sim test measures ≤ 12 % over 5 minutes of failing tags |
| per-tag back-off 15 s | half a wake period: a failing tag is retried in its next window |
| one quick retry within 2 s (`TAG_ADV_WINDOW_MS`) after `CONNECT_FAILED` | a failed attempt ends within ~1 s and the tag advertises every 250 ms for 2 s: the rest of its window is usually still open; without the retry a 5 % connection failure rate forfeited the whole wake period (30 s), about 30 % of the late trials of the scale test (scale-test.md §6.2). At most two attempts per window, both under the rate limit |
| a second session only while every open link idles | an initiation suspends the mesh and scans for the tag: it must not compete with a frame being streamed, and the refresh (4–15 s) is where a single-session bridge lost windows (about 70 % of the late trials) |
| wait for own sends ≤ 500 ms | a pending segmented send (a result) is not stalled by the pause; beyond that the attempt is deferred to a later advertisement rather than holding the mesh |
| watchdog 1.5 s, cancel confirmation 2 s | the host's own create timeout normally reports first; the watchdog only covers a lost report |
| resume retries ≤ 1 s apart, reboot after 5 s | a stuck scanner restart recovers from settings instead of leaving the relay silent |
| liveness 1800 s (`CTAG_BRIDGE_LIVENESS_S`) | 3 × the longest secure-network-beacon interval (600 s) of any mesh node in range: a working scanner always hears one; a silent one is a stopped scanner |

A message the gateway sends while the bridge is suspended may exhaust the
lower-transport retransmissions (NCS defaults: 2 unicast retransmissions,
~200 ms apart); the gateway then re-sends the whole message (§3.2 rule 1, up
to 3 times). The hardware test plan measures this (§11).

## 5. Tag session (protocol.md §5.3–§5.6, §10)

`src/core/tagsess.c`, event-driven on the work queue:

1. **GATT** (`central.c`): primary service by UUID, its characteristics, then
   the CCC descriptors (each belongs to the closest characteristic value
   before it). Handles are cached per tag (one entry per assignment) and
   rediscovered after a session ending `INVALID` or `TIMEOUT`. `CTRL` is
   subscribed for indications, `STATUS` for notifications. GATT setup counts
   toward the handshake bound (item 6).
2. **CAPS** read: exactly `CTAG_TAG_CAPS_LEN` (18) bytes (else `INVALID`),
   `proto` 1 (else `VERSION_MISMATCH`), the expected tag id (else
   `NOT_FOUND`), panel geometry, planes, plane flags, initial credits.
3. **Handshake** (`ctag_session`, bridge role): `HELLO` with a fresh
   `nonce_b` (`sys_csrand_get`), `CHALLENGE` → `AUTH`, `AUTH_OK` verified;
   then the first `CREDIT` (the tag's window). The transcript covers the CAPS
   value read (§5.4): the session keeps the parsed CAPS it renders with and
   re-packs it for the hash — every one of the 18 bytes is a field, so the
   bytes hashed are exactly the bytes read — and a relay that changed any of
   them fails `AUTH` at the tag before a frame exists. A `CREDIT` before
   `AUTH_OK` ends the session `INVALID`. A tag `ERROR` or a failed `mac_t`
   ends the session; **unauthenticated statuses** (below) end jobs only when
   they repeat.
4. **Jobs** present when the session started, in arrival order:
   - `CMD`: wait for a credit, `CMD{cmd, update_id}`, the tag's `RESULT`.
   - layout: if `FRAME_END` went out in an earlier session and this
     `CHALLENGE` reports the unknown display state for exactly the job's
     `(epoch, revision)`, the job ends `DISPLAY_STATE_UNKNOWN`. Otherwise the
     pending record is read back into the shared layout buffer (reloaded
     before a strip if a commit used the buffer meanwhile, §3),
     `ctag_render_init()` checks the layout
     against the tag's geometry (the §4.4 panel rule: `INVALID`), and the
     **render pre-pass** `ctag_render_frame_digest()` renders every strip of
     every plane once to compute `FRAME_BEGIN.digest`. Then `FRAME_BEGIN`,
     wait for **that record's** credit (or the tag's immediate `RESULT`;
     credits are counted per record — the first `CREDIT` after `AUTH_OK` is
     the window, every later one returns processed records — so the credit
     of a previous record or job never starts the plane data), `PLANE_DATA`
     records of ≤ 189 bytes rendered strip by strip (`BRIDGE_STRIP_ROWS`
     rows per strip, re-rendered from the layout — no frame buffer), plane 0
     then plane 1, `FRAME_END`, and the `RESULT` (60 s bound for the refresh).
5. **Flow control**: every record consumes one of the tag's credits; at most
   4 records per connection event (after the fourth the session waits one
   connection interval); at most `CTAG_BRIDGE_ATT_INFLIGHT` DATA fragments
   are handed to the host at once (below `BT_ATT_TX_COUNT`, so a write never
   blocks the work queue). Fragments carry `ATT_VALUE_MAX` bytes (ATT MTU 23).
6. **Deadlines** (§10 "Session deadlines"): the 5 s step deadline moves
   only on **progress** — a complete handshake message (CAPS, `CHALLENGE`,
   `AUTH_OK`), an authenticated record, or a `CREDIT` with n > 0 while the
   session waits for credit; fragments, `CREDIT{0}` and the bridge's own
   sends do not. `RESULT` within 60 s of `FRAME_END`/`CMD`. Absolute bounds
   on top: GATT setup and the handshake within **5 s of the connection**,
   and each frame (`FRAME_BEGIN` to `RESULT`) within **60 s + 1 s per KiB of
   plane data + 60 s** (the refresh bound; 150 s for a 400×300 BWR frame).
   Expiry ends the session with `TIMEOUT`, a link-level failure (the job
   stays pending). A peer that sends `CREDIT{0}` or never-ending fragments
   therefore holds a session slot for at most 5 s. Deadlines are per session
   (each `struct tsess` has its own step and pace timers): with two sessions
   neither moves the other's.
7. **Result timing** in `DELIVERY_RESULT`: `wake_ms` = layout validated →
   connected (restored jobs count from the boot), `suspend_ms` = the §5.2
   pause, `transfer_ms` = the job's `FRAME_BEGIN` → `RESULT` (without the
   pre-pass), `refresh_ms` from the tag.

**Unauthenticated statuses** (§10). Everything before `AUTH_OK` is
plaintext and anyone who can advertise a tag's public id could send it: a
`VERSION_MISMATCH` or `NOT_FOUND` from CAPS, a `CHALLENGE` refusing the
session (`STALE_EPOCH`, `NOT_FOUND`, an unsupported `proto`), a wrong `mac_t`
(`AUTH_FAILED`), and any plaintext `ERROR` on CTRL. Such a status is a
link-level failure (back-off, no result) until the **same status ends 3
consecutive sessions for that tag and epoch**; only the third ends the tag's
jobs of that epoch with it, each `DELIVERY_RESULT` flagged
`RESULT_FLAG_ESCALATED` (bit1) and carrying the tag's stored epoch from its
`ERROR` or `CHALLENGE` (for `STALE_EPOCH`, the epoch the companion must
assign above). The count lives in RAM beside the assignment; an
authenticated session (`AUTH_OK` verified), another status or a new epoch
starts it over. Statuses inside authenticated `RESULT` records act at once;
a `RESULT` that is the tag's stored ACK (`flags.bit0`) is reported with
`RESULT_FLAG_DUPLICATE` (bit0).

**Session end**: a job whose assignment was deleted (or moved to a newer
epoch) while its session held it ends `CANCELLED` when the session ends,
instead of waiting for a session that would never come.

A session holds a *view* of the active pack (a copy of its reader bound to
its slot): a pack installed meanwhile does not disturb it, and `FONT_BEGIN`
answers `BUSY` rather than overwrite the slot a session still reads.

## 6. Rendering

The strip renderer and the font-pack reader are the shared libraries
(`ctag_render`, [firmware-libs.md](firmware-libs.md)); the bridge supplies the
panel from the tag's CAPS and a glyph source over external flash. Glyph
lookups (strike records 12 B, index entries 10 B, bitmap rows ≤ 32 B) go
through a small LRU **read cache** (`CTAG_BRIDGE_READ_CACHE_LINES` ×
`_LINE`: 32 × 128 B on the nRF52840, 8 × 64 B on the nRF52832); reads larger
than a line bypass it. The golden test (§10) renders every
`protocol/fixtures/render.json` scenario from the fixture pack installed in
the store and reproduces each frame and plane digest.

**Render cost.** The session renders every strip of every plane twice (the
digest pre-pass, then streaming) on the work queue that also receives mesh
traffic, so §4.3 bounds what a valid layout can cost: line endpoints within
`[−W, 2W) × [−H, 2H)`, at most 16384 Bresenham steps over all lines, at most
four QR commands. The commit validation refuses anything more (`INVALID`),
before a session ever renders it. QR symbols are encoded once per frame and
kept across the strips (`CONFIG_CTAG_RENDER_QR_SLOTS` slots of 408 bytes,
firmware-libs.md): **4 on the nRF52840** (one per QR command §4.3 allows),
**1 on the nRF52832**, where RAM does not allow four (three more slots cost
1,224 B; the SoC keeps 281 B above its 8 KiB target with one, §9). There a layout with
several QR codes re-encodes each one for every strip it reaches, but with
the mask remembered from its first encoding (the same symbol without the
eight-mask penalty search: 9–25× faster on the host, `qrcodegen` at -O2/-Os),
and only strips its real size reaches. The worst valid layout — four large
QR codes overlapping every strip — thus costs four fixed-mask encodes per
strip instead of the automatic search per strip the review measured; a
layout with one QR code (all the companion composes) is encoded once per
frame on both SoCs.

## 7. External flash

Flash size comes from the devicetree node (`flash_get_size()` of the chosen
`cremind,bridge-flash`). With `W` = `CONFIG_CTAG_BRIDGE_WORKING_SPACE`:

```
0                  slot_size            2·slot_size                                     flash_size
| slot 0           | slot 1             | dir A | dir B | … | pending-layout ring | spare  |
                                        |◄── FONTPACK_DIR_SIZE (64 KiB) ──►|             |◄64 KiB►|
slot_size = align_down_64K((flash_size − W) / 2)
```

| Part | W | slot_size | directory | ring |
|---|---|---|---|---|
| nRF52840 DK MX25R64, 8 MiB (development) | 1 MiB | 3.5 MiB | 7 MiB | 112 records |
| 32 MiB production part | 16 MiB | 8 MiB | 16 MiB | 2032 records |
| 64 MiB production part | 16 MiB | 24 MiB | 48 MiB | 2032 records |

- **Slot directory**: two 4 KiB sectors at the start of the working space,
  each one 64-byte record `'CTSL'`, version 1, seq, slot, pack id, size,
  content hash, CRC-32 (the layout of `cremind-tag fonts image`). The active
  pack is the valid record with the highest seq; activation erases and writes
  the *other* sector with seq + 1, so a power loss at any byte leaves the
  previous record valid (tested at every byte, §10). At boot the active
  pack is validated as fontpack.md §2 requires (header, tables, strike index
  CRCs, bitmap bounds; the full content hash only at install); a pack that
  fails is not used and FONT_STATUS reports no pack, so the companion
  reinstalls it.
- **Pending layouts**: 8 KiB records, version 2: a 64-byte header (magic
  `'CTPL'`, tag, epoch, revision, update_id, pack id, layout digest, length,
  the `xfer_id` that carried it, and a CRC-32 over header and layout), the
  layout as its mesh chunks at a **152-byte stride** (chunk *i* at
  `64 + 152·i`: 150 bytes and 2 pad bytes, so every chunk starts on a write
  unit and is programmed straight from its `LAYOUT_CHUNK`), and a "consumed"
  word in the last 4 bytes. A slot is erased at `LAYOUT_BEGIN`, filled chunk
  by chunk, and **sealed** by the header after the commit accepted it (a torn
  or unsealed slot is never valid); a record is consumed by programming its
  last word (no erase). Slots are taken **round-robin over the ring**, so the
  erase wear of frequent deliveries spreads over the whole working space
  instead of one sector per tag. The ring keeps at least 2 × 20 + 2 records
  (a session's record and a waiting one per tag, and the transfer being
  received; the job table is smaller than that). At boot every record is
  scanned: live ones for an assigned tag and epoch become jobs again (in
  write order), the rest are consumed, the ring position continues after the
  newest record, and the newest intact record names the last accepted
  transfer (§3).
- The device's **last 64 KiB** stay free (margin, and `FLASH_TEST`'s last
  sector).
- Every write is a 4-byte unit at a 4-byte offset (the nRF QSPI requirement);
  font data carries the odd bytes of a `FONT_DATA` over to the next one.
- Parts above 16 MiB need 4-byte addressing in the devicetree
  (`address-size-32;` and `enter-4byte-addr = <0x01>` on QSPI,
  firmware-notes §8); the native_sim tests use a 32 MiB part whose directory
  and ring lie above 16 MiB.

## 8. Maintenance port (protocol.md §1.6, fontpack.md §4)

| | nRF52840 | nRF52832 |
|---|---|---|
| transport | USB CDC ACM (`device_next`, `cdc_acm_uart0`) | UART0 (P0.06 TX / P0.08 RX: the DK's VCOM; a CH340 on a bridge board), 115200 baud |
| USB identity | VID:PID 1209:0001 (pid.codes **test** pair — replace before shipping), "Cremind" / "Cremind Tag bridge", serial number from the device id | — |
| `caps.max_frame` / `caps.credits` | 4096 / 2 | 512 / 1 |
| client chunk (`FONT_DATA`) | 2048 B | 476 B |
| response buffer / transmit ring | 1536 B / 512 B | 1280 B / 256 B |

The port speaks the §1.1–§1.3 framing (`ctag_frame`) with CBOR maps
(`ctag_cbor`) and answers `HELLO`, `PING`, `INFO`, `REBOOT`, `EVENT_ACK`,
`FONT_BEGIN`, `FONT_DATA`, `FONT_COMMIT`, `FONT_STATUS`, `FONT_ABORT` and
`FLASH_TEST`; every other type (mesh and delivery requests included) answers
`UNSUPPORTED`, a malformed payload `INVALID` with a text.

- Nothing is answered before a `HELLO`. `HELLO` is exempt from credits,
  drops an answer still waiting for a credit and restarts the credit count
  (`SERIAL_DEFAULT_CREDITS` + the HELLO's grant). Frames are processed one at
  a time and each answer grants its receive buffer back; an answer without a
  host credit waits for the next grant.
- Answers are COBS-encoded straight to the port, block by block (no second
  buffer; byte-identical to `ctag_serial_wire_encode`, tested).
- **Frame size.** The host learns `caps.max_frame` from `HELLO`; the
  companion's `BridgeMaintClient` (`gateway/link.py` `_accept_hello`) keeps
  `min(caps.max_frame, 4096)` and sizes `FONT_DATA` chunks as
  `min(2048, max_frame − 36)`, so the nRF52832's 512-byte frames carry
  476-byte chunks (verified: the PTY interop runs against a 512-byte build
  and the bridge counts no `oversize` frame, §10). A frame above
  `max_frame` is dropped and counted (`oversize`).
- `FONT_COMMIT` reads the slot for its SHA-256 through the response buffer
  (no separate read buffer): an answer still held there only waits for a
  credit and is replaced by the commit's own answer anyway.
- **Install**: `FONT_BEGIN{size, digest, fontpack_id}` picks the inactive slot
  (`NO_RESOURCES` without slots, `TOO_LARGE` above slot_size, `BUSY` while a
  session reads that slot); `FONT_DATA` needs strictly sequential offsets
  (`INVALID` otherwise, `NOT_FOUND` without an install) and erases 4 KiB
  sectors ahead of the write — a 64 KiB block erase can take up to 3.5 s on
  the MX25R and would outlast the client's 2 s request timeout;
  `FONT_COMMIT` checks the byte count (`INCOMPLETE`), SHA-256 of the slot
  against `digest` (`DIGEST_MISMATCH`), the format validation including the
  content hash, the pack id (`INVALID`), then flips the directory.
  `FONT_ABORT` or a reset before the commit leaves the active pack untouched.
- **FLASH_TEST{op_id}**: erase / pattern / read-back / erase at the first and
  last sectors of the inactive slot, the sectors either side of the 16 MiB
  boundary (when inside the part) and the device's last sector; a position in
  the active slot, in a slot a session reads, in the directory area or the
  pending ring, or any position while an install is open, reports `BUSY`. A
  repeated `op_id` returns the remembered result with `detail = DUPLICATE`
  (last 4 op ids).
- `REBOOT` answers, lets the answer leave the port, then resets.
- `INFO.counters` include the port's frame counters and the bridge's (mesh,
  scheduler, session, delivery, flash cache): 61 counters, all reported (up
  to 64; at most about 1,160 bytes framed, below the 1,280/1,536-byte response
  buffer — should counters ever outgrow it, the last ones are left out rather
  than INFO failing). The lists are written straight into INFO's array, not
  copied on the maintenance stack.

The companion's `bridge_maint/client.py` drives it unchanged: see §10.

## 9. Memory

`python tools/build.py bridge-nrf52840dk bridge-nrf52dk` (NCS v3.4.1,
2026-09-28). Flash against the code partition (the DKs' default MCUboot
partitions are replaced by one `code_partition` that ends at the settings
storage); RAM after every static allocation including stacks.

| Target | Flash used / region | Headroom (min 15 %) | RAM used / 64·256 KiB | RAM free (target) | verify_stack | Result |
|---|---|---|---|---|---|---|
| bridge-nrf52840dk | 307,960 / 1,015,808 B | 69.7 % | 114,474 / 262,144 B | 147,670 B (none) | 16/16 pass | ok |
| bridge-nrf52dk | 277,460 / 499,712 B | 44.5 % | 57,127 / 65,536 B | **8,409 B (8,192)** | 16/16 pass | ok |

Static RAM by component (linker map and ELF symbols; the nRF52840 holds two
tag sessions, the nRF52832 one; the "nRF52832 before" column is the
`a232ac0` build, 215 B free, and "nRF52840, one session" the build before
the second session):

| Component | nRF52840 | nRF52840, one session | nRF52832 | nRF52832 before |
|---|---:|---:|---:|---:|
| bridge core state (`br`) | 22,752 | 18,200 | 11,088 | 15,776 |
| — the one shared layout buffer (was: assembly + session buffers) | 4,096 | 4,096 | 4,096 | 8,192 |
| — rest of the delivery core: jobs, history, results, assignments | 4,528 | 4,528 | 2,744 | 3,288 |
| — tag sessions (`struct tsess` each: strip buffer, render work with 4 / 1 QR slots, records) | 9,040 (2 × 4,520) | 4,524 | 3,068 | 3,032 |
| — flash read cache, font store, scheduler (with its link table) | 5,088 | 5,044 | 1,180 | 1,264 |
| maintenance port: frame buffers, rings, SHA context | 15,291 | 15,291 | 3,484 | 5,662 |
| maintenance thread stack | (main) | (main) | (main) | 2,112 |
| bridge work-queue stack | 4,096 | 4,096 | 3,584 | 3,584 |
| event queues, mesh queues, GATT cache, sightings, per-link GATT state (`struct clink`, 336 B each) | 4,393 | 4,120 | 2,520 | 2,840 |
| Zephyr controller (two connections on the nRF52840) | 17,783 | 16,269 | 9,439 | 9,439 |
| Bluetooth host (14 ATT/L2CAP/ACL TX buffers, 16 event buffers, two connections on the nRF52840) | 17,848 | 14,483 | 9,195 | 9,195 |
| Bluetooth Mesh | 8,448 | 8,448 | 5,336 | 5,336 |
| kernel stacks: ISR, system work queue, **main = maintenance**, idle | 8,426 | 8,426 | 7,370 | 6,346 |
| PSA (nrf_security + Oberon) | 1,772 | 1,772 | 1,856 | 1,856 |
| other (USB stack on the nRF52840, drivers) | 10,627 | 10,627 | 813 | 813 |

The second session costs the nRF52840 **9,704 B** (104,770 → 114,474 B):
its `struct tsess` (4,520 B), its link state (336 B), the scheduler's link
table, a second controller connection (+1,514 B) and the host's buffers for
two links' DATA fragments in flight (`CTAG_BRIDGE_SESSIONS` ×
`CTAG_BRIDGE_ATT_INFLIGHT` = 12 < `BT_ATT_TX_COUNT` = 14; a host assertion
wants more event than ACL TX buffers: +3,365 B); 147,670 B stay free.

What reached the target on the nRF52832 (7,977 B were missing), measured
build by build, without giving up a function or a security property:

| Step | Both SoCs | RAM used | Free |
|---|---|---:|---:|
| `a232ac0` | | 65,321 | 215 |
| Layouts **assembled in their external-flash ring slot** (§3, §7) and **one** 4 KiB layout buffer shared by the commit's digest/validation and the session's renderer (the session reloads from the job's record after a commit used it); `FONT_COMMIT` hashes through the response buffer; 512-byte maintenance frames (`caps.max_frame`, the client adapts its `FONT_DATA` chunks, §8). The history grew by `update_id` and `result_seq` (§3) | all but the frame size | 60,071 | 5,465 |
| The maintenance thread **is the main thread** (its 2 KiB stack also serves the kernel's init; the separate 1 KiB main stack is gone); a 256-byte transmit ring | the main thread | 58,663 | 6,873 |
| **10-tag assignment table** (`CTAG_BRIDGE_MAX_TAGS=10`, reported as `CAPS_STATUS.max_tags`): assignments, history, battery, GATT handle cache, sightings, back-off table | | 57,127 | 8,409 |
| The review fixes of §4–§5 (session deadlines, per-record credits, the unauthenticated-status count, the liveness watchdog) | yes | 57,255 | 8,281 |
| A 1,280-byte response buffer (INFO with all 59 counters needs at most 1,117 bytes) | | 56,999 | 8,537 |
| Protocol v1 finalisation: `DELIVERY_RESULT` with `stored_epoch` and `flags` (+8 B per result slot), the session's `tag_epoch`, the QR memo (16 B); CAPS bound into the transcript at no RAM cost (re-packed from the parsed CAPS); **one QR slot** here (`CONFIG_CTAG_RENDER_QR_SLOTS=1`; the default four would cost 1,224 B more, 7,193 B free: below the target) | yes (4 slots on the nRF52840: +1,408 B) | 57,063 | 8,473 |
| The scheduling of §4: link slots (`CONFIG_CTAG_BRIDGE_SESSIONS`, **one here**, two on the nRF52840: +9,704 B there), the per-link GATT state grouped in `struct clink` (+40 B of padding), the scheduler's link table and the initiator's deadline (+24 B); the quick retry's flag rides in the back-off entry's padding (0 B) | yes | 57,127 | **8,409** |

The nRF52840 keeps `MAX_TAGS_PER_BRIDGE` = 20 and its 4 KiB frames (and
gains 5.7 KiB from the shared changes). The 10-tag table was still needed
after the other changes (6,873 B free without it).

What stays configured as before on the nRF52832 (`socs/nrf52832.conf`): one
advertising set (relays share the main set), one auxiliary set (0 does
not build), 31-byte extended-advertising receive PDUs, 68-byte HCI events and
65-byte commands, 10 discardable report buffers, no controller duplicate
filter, no delayable mesh messages, mesh settings on the system work queue,
8 PSA key slots, a 3.5 KiB work-queue stack and a 2 KiB main (maintenance)
stack — both **to be measured** with `debug.conf` on hardware (H12) — 16
jobs and 8 result slots. The controller stays the Zephyr controller
(`bt-ll-sw-split`). The margin above the target is 217 B (a second QR slot,
408 B, or a second tag session, about 9 KiB, does not fit): anything added to
this SoC's RAM must be paid for.

The shared buffer costs nothing on the air: the commit's extra flash read
(4 KiB) replaces a RAM copy, and a session reloads its layout (4 KiB from
the record plus a renderer re-init) only after a commit arrived mid-frame,
or — two sessions on the nRF52840 — when the other session's frame took the
buffer (a session's next job streaming beside the other's frame, rare: the
second session starts only while the first idles).
Assembly in flash erases the transfer's 8 KiB slot at `LAYOUT_BEGIN` instead
of at the commit — the same erase count per delivery (one slot per
transfer), and a `LAYOUT_BEGIN` that never commits wears one slot of the
round-robin ring.

## 10. Tests

`apps/bridge/tests/core` — ztest on `native_sim` and `native_sim/native/64`
(twister; the same sources as the firmware core, the flash simulator as a
32 MiB NOR with 4-byte program units and power-cut injection), in two
configurations: `ctag.bridge.core` (the nRF52840's sizes, two tag
sessions) and `ctag.bridge.core.nrf52832` (the nRF52832's: one session, 10
tags, 16 jobs, 8 result slots, an 800-byte strip, 8 × 64-byte cache lines,
512-byte maintenance frames with one credit), so every shared code path runs
with both:

| Suite | Covers |
|---|---|
| `bridge_flash` | geometry (DK 8 MiB/1 MiB, production 16 MiB, caps, errors); > 16 MiB address map; directory record layout; A/B activation; **a power cut at every byte of an activation** and before its erase; pending records (odd and maximum length, CRC, consume, torn writes at every stage); **assembly in place** (chunks in any order at their stride, a chunk written twice refused as NOR would be, sealing, a header over other bytes failing the CRC, consumed records still intact); read cache |
| `bridge_fonts` | install over the FONT_* path in odd chunk sizes, boot validation, a corrupt index at boot, every install error in order, the slot flip, a session view blocking an overwrite (`BUSY`), FLASH_TEST positions and `BUSY` rules |
| `bridge_delivery` | §3.3 order (NOT_FOUND, INCOMPLETE + bitmap + resend, TOO_LARGE, INVALID, DIGEST_MISMATCH, NOT_ASSIGNED, STALE_EPOCH, FONTPACK_MISMATCH, UNSUPPORTED, missing strike, STALE_REVISION before the pack check); DUPLICATE for a pending, a displayed and an undisplayed revision (and after a reset); **a repeated commit of an accepted transfer answers DUPLICATE** whatever became of its job (pending, cancelled, cleared), after a reset, and not after the next BEGIN; **one result_seq per update_id** (a restarted transfer of an update_id already reported, before and after a reset; repeated TAG_CMD with a pending job, after its result, after RESULT_ACK, after the retries gave up; results-table eviction keeps unacknowledged results); **the transfer assembled in flash** (reverse order, a repeated chunk, no record before the commit, a reset before the commit, a chunk write failure → STORAGE_ERROR); **a power cut at every 4 bytes of the seal** (no job and no accepted transfer after the reset unless the header is complete); **the shared layout buffer** (held, borrowed by another tag's commit and reloaded, moved to a re-delivery's record, a damaged record refused, released by a finished job); **finish order** (history saved and result sent while the record is live; a superseded record consumed before its result); a stored result's `stored_epoch` and `flags` replayed under a new `update_id` after a reset; **accept order** (a seal failure or a full job table changes neither history nor older jobs; a replaceable older layout makes room); SUPERSEDED, in-session jobs untouched, LAYOUT_CANCEL; assignments (idempotent delete, inclusive delete up to epoch 0xFFFFFFFF, epochs, the `max_tags` table, persistence); tag commands and CLEAR resetting the history; pending layouts surviving resets, finished ones not redelivered; ring wear levelling across resets; result retry schedule and RESULT_ACK; result_seq never reused across four resets; node reset |
| `bridge_sched` | mocked `bt_mesh_suspend/resume` and `bt_conn_le_create`: connection with resume before the session and suspend_ms; **suspend rejected → no connection attempt** (`-EBUSY`, `-EINVAL` touch nothing); **a suspend failing part-way** (scanner stopped, mesh not flagged suspended: RECOVERY suspends fully and resumes; still failing → reboot after 5 s); **liveness** (idle and silent for the limit, a report since, not idle, idle only recently, disabled); `-EALREADY` never leaves the mesh suspended; **every failed attempt resumes** (refused create, host timeout, failed establishment); **cancellation** confirmed, unconfirmed (resume refused while initiating → recovery), and completing mid-cancel; **resume failure → disconnect, recovery with back-off, reboot after 5 s**; **disconnect mid-transfer**; waiting for / deferring on own sends; not while configuring; **the quick retry** (one retry inside the window after `CONNECT_FAILED`, none after a failed retry, after the window, after a suspend failure or a failed session; a retry counts toward the rate limit); **20 tags waking in 2 s windows, all failing to connect, for 5 minutes never exceed 6 suspends in any rolling minute, mesh suspended ≤ 12 % of the time**; two sessions (`ctag.bridge.core`): **initiations serialised** (nothing starts while one initiates, each its own suspend window and `suspend_ms`), **a second tag only while the first link idles**, never a second link to one tag, a third tag waits for a free link (a disconnecting link is not free), **a failing second link** (connect failure, resume failure and recovery, a dropped session) **never touches the first**, **a tag whose sessions keep failing never starves the other** (served in every wake over 10 rounds), **the rate limit with a session open**, a link's lost disconnected event freed after 10 s while the other session and the initiator's watchdog go on |
| `bridge_session` | a fake tag built from `ctag_session` (tag role), `ctag_txn` and `ctag_frag` over a timed event queue: end-to-end deliveries (1 plane; 2 planes rotated; QR on a BWR panel) whose RESULT digest equals the fixture frame digest, ≤ 4 records per connection event, a credit for every record, indications before or after the write response, one result_seq per update_id; the tag's duplicate answer (reported with `flags` bit0 and the tag's stored epoch); **disconnect mid-transfer** (no result, job pending, next session restarts at offset 0); RESULT lost at a reset → `DISPLAY_STATE_UNKNOWN` then redrawn; **STALE_EPOCH and AUTH_FAILED as link failures twice, final in the third consecutive session** (the result flagged `RESULT_FLAG_ESCALATED` and carrying the stored epoch of the tag's ERROR), **a relay rewriting `plane_flags` in the CAPS the bridge reads: the tag refuses AUTH, no record or frame ever exists, and the same tag delivers once the relay is gone**, the count restarting after an authenticated session, with another status and with a new epoch; the panel rule; CAPS of another tag; a silent tag timing out after 5 s; CMD CLEAR/SLEEP in arrival order; **another tag's commit mid-frame** (the session reloads its layout, same frame digest) and **a re-delivery of the frame being drawn** (one result, under the adopted update_id); **review probes: CREDIT{0} every 4 s for an hour instead of CHALLENGE (ends INVALID at once); CREDIT{0} forever after AUTH_OK (TIMEOUT in 5 s); a never-ending CTRL fragment stream (TIMEOUT 5 s after the connection); every handshake message 3 s late (TIMEOUT at the 5 s handshake bound); a credit every 4 s (TIMEOUT at the frame bound, 135 s); a CMD then a layout refused at FRAME_BEGIN (2 records sent, no PLANE_DATA on the CMD's credit)**; ASSIGN_DEL during a session that then drops (the job ends CANCELLED); two sessions (`ctag.bridge.core`, two fake tags over one event queue): **side by side** (B connected while A refreshes, both frames the reference frames, each result with its own `suspend_ms`, A never reloads), **deadlines per session** (A's refresh never ends: A times out exactly at its own `RESULT` bound while B delivers; B silent: B times out 5 s after its own connection while A delivers; B drops mid-frame: A untouched), **both streaming at once** (A's CLEAR, then A's frame beside B's two-plane frame: both exact, the shared layout buffer reloaded) |
| `bridge_maint` | HELLO (caps, credits, exemption, reset), answers held without credit, version mismatch, UNSUPPORTED (unknown, mesh and delivery types), malformed CBOR, CRC errors, the client's install sequence with its chunk size derived from `caps.max_frame`, a frame above `max_frame` dropped and counted, INFO counters (and INFO still answered with every slot filled with long names and 5-byte values), FLASH_TEST idempotency, REBOOT answered first, the streaming COBS writer byte-identical to the library |
| `bridge_golden` | **every `render.json` scenario rendered from `fontpack_test.ctfp` installed in the store (slot 1, through the cache) reproduces the fixture frame digest and each plane digest strip by strip** |

`apps/bridge/tests/maint_pty` (build-only in twister, two builds: 4096-byte
frames with two credits, and the nRF52832's 512-byte frames with one) runs
the maintenance core behind a native_sim pseudo-terminal; `interop.py` drives
it with the companion's own `BridgeMaintClient`: HELLO, PING, FONT_STATUS, a
complete install, the skip of an active pack, a forced install into the other
slot in 333-byte chunks, FLASH_TEST twice, INFO, and checks that no frame
exceeded `caps.max_frame` (the client sends 2048- and 476-byte chunks). It
starts the image in a fresh directory (an erased simulated part).

```bash
python tools/build.py --shell          # then, inside the container:
apt-get update && apt-get install -y make
west twister -T /work/apps/bridge/tests -p native_sim -p native_sim/native/64 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-bridge
for v in ctag.bridge.maint_pty ctag.bridge.maint_pty.nrf52832; do
  python3 /work/apps/bridge/tests/maint_pty/interop.py \
    $(find /build/twister-bridge -path "*/$v/zephyr/zephyr.exe" | head -1)
done
```

Result (2026-09-28, two tag sessions and the quick retry): 336 of 336 test
cases pass (88 per platform in `ctag.bridge.core`, 80 in
`ctag.bridge.core.nrf52832`, which runs one session and renders with one QR
slot); the interop script passes against both builds; `tests/ztest` 456 of
456; the gateway's 122. A mutation check: dropping the "every open link
idles" rule fails 2 tests, disabling the quick retry 3 (5 over both
configurations).

## 11. Hardware test plan

What only hardware can show — the plan's bridge-scheduling acceptance tests
plus the items the builds could not verify. Equipment: an nRF52840 DK as
bridge (RTT logs: build with `-DEXTRA_CONF_FILE=debug.conf`), a gateway, two
or more tags (nRF52 DK development tags or enrolled boards), a second bridge
for relay checks, an nRF52840 dongle with the nRF Sniffer, a Power Profiler
Kit (radio activity), the companion (`cremind-tag`) with the daemon's event
log.

| # | Test | Procedure | Pass |
|---|---|---|---|
| H1 | Connection success | Deliver a layout to a tag in range | `DELIVERY_RESULT OK`, stages TRANSFERRING and REFRESHING; `suspend_ms` in the result and in HEALTH (`suspend_max_ms`); sniffer shows no scan PDUs between CONNECT_IND and the resume |
| H2 | Suspend rejected | Deliver while the bridge is being provisioned (`bt_mesh_suspend` `-EBUSY`); or force with a debug build returning an error | no CONNECT_IND on air; `suspend_fail` +1; retried after 15 s |
| H3 | Attempt timeout | Deliver to a tag out of range / not advertising at the moment of the attempt (shield it after its advert) | the pause ends ≤ 1.1 s after it began (PPK/sniffer: mesh scan and relay resume); `connect_failed` +1; next attempt ≥ 15 s later |
| H4 | Cancellation | Debug build whose `op_create` (central.c) passes a create timeout above the 1.5 s watchdog, towards a tag that stopped advertising | `cancels` +1; the resume follows the `connected(err)` confirmation; no resume failure |
| H5 | Disconnect mid-transfer | Power the tag off during PLANE_DATA | no result; `sessions_fail` +1, last status DISCONNECTED; the next session restarts at offset 0 and ends OK |
| H6 | Resume failure | Debug build whose `op_resume` (central.c) fails the first N calls (N = 3, then N = 100) | no session on the link, which is dropped; `resume_fail` counts; recovery retries; with N = 100 a reboot after 5 s, the mesh reloaded from settings (CAPS_GET still answered) |
| H7 | Complete suspend pause | 100 deliveries; log `suspend_ms` | histogram of the pause (suspend → resume return) and of the full radio gap (sniffer: last mesh adv/scan before → first after); target ≤ 1.2 s |
| H8 | Relay recovery | A second bridge beyond the gateway's range, reachable only through the bridge under test; continuous CAPS_GET to it while tags are served | every reply arrives; the relay's gap per pause ≈ the pause; no relay loss after the pause |
| H9 | Mesh traffic during a pause | Deliver to bridge B a layout (28 chunks) while bridge A (same path) connects to tags | B's delivery completes (gateway SAR / whole-message retries absorb the pause); count gateway retries |
| H10 | Coexistence with image transfer | Stream a 2-plane 400×300 frame while the gateway sends layouts and results through the bridge | transfer completes (≤ 4 records per event, credits respected); mesh messages relayed and answered during the transfer; `transfer_ms` recorded; no supervision timeout |
| H11 | Rate limit | 20 tags with work, all failing to connect (shielded) for 5 min | ≤ 6 suspensions in any minute (`suspend_count`), relay reachable throughout |
| H12 | Stack depth | `debug.conf` thread analyzer during H1, H10, a FONT_COMMIT and INFO | every stack keeps ≥ 25 % unused; resize the Kconfig stacks accordingly (nRF52832 values are provisional) |
| H13 | Render pre-pass time | `render_ms_max` counter for a text-heavy 400×300 BWR card | recorded; mesh latency impact acceptable (the work queue renders ~2 × 19 strips in one item) |
| H14 | Font install | `cremind-tag bridge fonts-install` of the full pack over USB (nRF52840) and UART (nRF52832) | completes; slot flip; reset during FONT_DATA leaves the old pack active; FLASH_TEST all OK/BUSY as designed |
| H15 | QSPI > 16 MiB | a 32 MiB part with `address-size-32` / `enter-4byte-addr` | FLASH_TEST items either side of 16 MiB OK |
| H16 | Resets | reset during commit, during a session, during a result retry | pending layouts restored; no duplicate delivery; result_seq continues upward; a repeated commit after the reset answers DUPLICATE |
| H17 | Spoofed tag | A second DK advertising an assigned tag's id (companion `sim` peer or a test build) answering HELLO with CREDIT{0}, then with ERROR{STALE_EPOCH} | the session ends at once (INVALID), then link failures with back-off; the real tag's jobs end only after 3 consecutive sessions with the same status; other tags keep being served |
| H18 | Liveness | Debug build that stops the scanner (`bt_le_scan_stop`) behind the mesh's back | the bridge reboots after `CTAG_BRIDGE_LIVENESS_S` idle without reports and serves again |
| H19 | Two sessions (nRF52840) | Two tags of one bridge with work, the second waking while the first refreshes (a BWR panel makes the window 15 s); sniffer on both links; repeat with the first tag's refresh interrupted by a power loss | the second tag connects during the first one's refresh in its own suspend window (no scan PDUs between its CONNECT_IND and the resume), both deliveries OK, `concurrent_sessions` +1; the first link keeps its connection events through the second initiation (no supervision timeout); mesh relayed meanwhile |
| H20 | Quick retry | A tag with work at the edge of range (or shielded for the first 300 ms of its window) | one `CONNECT_FAILED`, then a second attempt within the same 2 s window (`quick_retries` +1) that connects; with the tag shielded for the whole window: two attempts, then nothing for 15 s; never more than 6 suspensions in any minute |

## 12. Differences from the simulator

The firmware follows `sim/bridge.py`; where it differs:

| Firmware | Simulator | Why |
|---|---|---|
| A repeated `LAYOUT_COMMIT` of the last accepted transfer answers `DUPLICATE` after a reset too | remembered in RAM | the newest sealed ring record names it (§3) |
| Session deadlines advance only on progress, with absolute bounds for the handshake and each frame (§5) | a 5 s timeout per awaited message and 60 s for a `RESULT` | the simulator bounds each wait; the firmware also bounds a peer that keeps a session alive with slow progress (docs/protocol.md §10) |
| `TAG_CMD IDENTIFY` / `REFRESH` (and unknown commands) answer `UNSUPPORTED` without a session | forwarded to the tag, which answers `UNSUPPORTED` | same result, no connection and no mesh pause for a command no tag supports (task requirement) |
| Pending layouts in 8 KiB records in a wear-levelled ring | a dictionary; fontpack.md §3 describes one 4 KiB sector per tag | a `LAYOUT_HARD_MAX` layout plus its header does not fit 4 KiB, and a fixed sector per tag would be erased on every delivery to it (NOR endurance) |
| Tag commands are not persisted across a reset | kept (the simulator's jobs survive `reboot()`) | only layouts are written to flash; the companion's TTL covers a lost command |
| `FONT_DATA` erases 4 KiB sectors ahead | 64 KiB blocks | bounds each request below the client's 2 s timeout (MX25R block erase up to 3.5 s) |
| `FONT_STATUS` reports a pack only when it passed the boot validation | the directory record | a corrupt active pack is then reinstalled by the companion instead of being skipped as "already active" |
| `FLASH_TEST` also tests the **last** sector of the inactive slot and treats the pending ring as in use | first sector of the inactive slot, the 16 MiB boundary, the last sector | spec.yaml's `FLASH_TEST` text; the ring holds accepted work |
| `wake_ms` of a job restored at boot counts from the boot | validation time kept | the validation time is RAM state |
| A link idles once its session waits for the `RESULT` with no record left to send and no DATA fragment in flight | the session awaits the `RESULT` | the firmware also waits for its last fragments' completions; the simulator hands a record over at once |
| Two sessions streaming at once take turns in the one layout buffer (reloading from the job's record) | each session holds its frame | RAM (§3); rare, since a second session starts only while the first idles |

## 13. Flashing and debugging

Artifacts land in `build/bridge-nrf52840dk/` and `build/bridge-nrf52dk/`
(`zephyr.hex`, `zephyr.elf`, `zephyr.map`, `.config`, `verify.json`).

```bash
nrfjprog -f NRF52 --program build/bridge-nrf52840dk/zephyr.hex --sectorerase --verify
nrfjprog -f NRF52 --reset
# or J-Link Commander: JLinkExe -device nRF52840_xxAA -if SWD -speed 4000 -autoconnect 1
#   loadfile build/bridge-nrf52840dk/zephyr.hex, r, g
```

- The image replaces the DK's MCUboot layout: it links at 0 up to the
  settings storage (`0xF8000` on the nRF52840, `0x7A000` on the nRF52832).
  `--sectorerase` keeps the settings (mesh keys, assignments); `--chiperase`
  (or `nrfjprog --eraseall`) forgets the node — re-provision it afterwards.
- Factory font pack on the nRF52840 DK: `cremind-tag fonts image <pack> --flash-size 8MiB --working-space 1MiB`
  writes `flash.hex` at the QSPI XIP base; program it with
  `nrfjprog -f NRF52 --program flash.hex --qspisectorerase --verify`.
  Otherwise install over the maintenance port (`cremind-tag bridge fonts-install --url <port>`).
- Maintenance port: the nRF52840 DK's **nRF USB** connector enumerates as a
  CDC ACM port (1209:0001, "Cremind Tag bridge"); the nRF52 DK uses the
  J-Link VCOM at 115200 baud.
- Logs: build with `-DEXTRA_CONF_FILE=debug.conf` (RTT logs, asserts,
  thread analyzer every 60 s) and read them with `JLinkRTTViewer` /
  `JLinkRTTLogger`; release builds have no console (the serial link belongs
  to the maintenance port). `tools/build.py` builds release images; for a
  debug image run `west build` by hand in `python tools/build.py --shell`
  with the same arguments plus the fragment.
- Counters: `cremind-tag bridge info --url <port>` (INFO) and the gateway's
  `GET_INVENTORY` (CAPS/HEALTH) show the scheduler, session, mesh and flash
  counters.

## 14. Open items

- Everything in §11: nothing has run on hardware yet (stack depths, the
  actual pause, relay and SAR behaviour during pauses, QSPI/SPI NOR timing,
  USB enumeration, render time).
- Two sessions on the Zephyr controller (H19): in the pinned sources
  `ll_create_connection()` refuses only while the one scan/initiator set is
  in use or for a peer already connected (Z `controller/ll_sw/ull_central.c`),
  so a central initiates beside an existing central connection, and
  `BT_CTLR_SCHED_ADVANCED` (on with `BT_CENTRAL`) places the new connection's
  events beside the first (`BT_CTLR_CENTRAL_SPACING`, firmware-notes §2); what
  the first link's connection events cost the second initiation, and two
  links streaming beside the mesh, are measured on hardware.
- nRF52832: RAM meets the target with 217 B to spare, a 10-tag table, one QR slot
  and one tag session (§9); its 3.5 KiB work-queue and 2 KiB main (maintenance) stacks are
  estimates until H12; the SPI NOR pins of `boards/nrf52dk_nrf52832.overlay`
  are placeholders; `BT_BUF_EVT_RX_SIZE=68` assumes no HCI event above 68
  bytes (true for the commands the bridge uses — verify with a debug build).
- Tag placement by capacity is Cremind's: the companion reports each
  bridge's `max_tags` and `assigned` (both from its `CAPS_STATUS`, which the
  gateway forwards in the caps map of `EVT_BRIDGE_INFO` and `GET_INVENTORY`
  as `max_tags` and `assigned_count`) in its inventory, Cremind
  refuses to claim or assign onto a full bridge, and an `ASSIGN_SET` that still
  meets a full table (`NO_RESOURCES`, e.g. an 11th tag on an nRF52832 bridge)
  fails the `assign_tag` at once with `bridge_full` (companion.md §6) — the
  admin picks another bridge; nothing chooses one automatically.
- USB VID/PID 1209:0001 is the pid.codes test pair; `MESH_COMPANY_ID` is
  0xFFFF (spec).
- CI: `.github/workflows/ci.yml` runs twister on `tests/ztest` only; add
  `-T /work/apps/bridge/tests`.
