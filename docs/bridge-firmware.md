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
| `bridge-nrf52840dk` | `nrf52840dk/nrf52840` (+ the DK's 8 MiB MX25R64) | builds, verified, meets targets; not yet run on hardware | [§9](#9-memory) |
| `bridge-nrf52dk` | `nrf52dk/nrf52832` (+ a placeholder SPI NOR) | builds and verifies, **misses the 8 KiB free-RAM target** (215 B free) | [§9](#9-memory) |

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
                         │ pending ring, directory      │ glyph reads (cache)
                         ▼                              ▼
                    bflash (external NOR) ◄──── fontstore (mutex) ◄─── maintenance thread
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
  only the font store with the work queue, behind the store's mutex.
- The boot runs as the first work item of the work queue (flash and pack
  validation, `bt_enable`, `bt_mesh_init`, `settings_load`, pending-layout
  restore), then the maintenance thread starts.

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
  layout digest, final result), `ctag/rs` the result_seq reservation.
  Pending layouts live in external flash (§7).
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
| `CAPS_GET` | `CAPS_STATUS`: proto 1, fw 0.1.0, board 3/4, active pack id (zeros when none), `flash_mib`, `max_tags` 20, assigned count, flags bit0 pack valid, bit1 busy (an attempt or session in progress) |
| `HEALTH_GET` | `HEALTH_STATUS`: uptime, sessions ok/fail, suspend count, max suspend ms, resume failures, queue depth (jobs), last status |
| `ASSIGN_SET` | `STALE_EPOCH` below the stored epoch; `NO_RESOURCES` for a 21st tag; a higher epoch cancels the older epoch's jobs; the same epoch overwrites key and flags |
| `ASSIGN_DEL` | `OK` when absent (§10); `STALE_EPOCH` when the stored epoch is newer; cancels the tag's jobs (not one in a session) |
| `TAG_CMD` | `NOT_ASSIGNED` / `STALE_EPOCH` results at once; `CLEAR` and `SLEEP` queue a job for the tag's next session; `IDENTIFY` / `REFRESH` (and unknown commands) answer `UNSUPPORTED` at once |
| `IDENTIFY` | blinks the DK LED (`led0`) for the given seconds |
| — | `TAG_SEEN` for an assigned tag's advertisement, at most once per 60 s per tag (RSSI, the battery from its last CHALLENGE, the advertising flags) |

## 3. Layout reception (`LAYOUT_SRV`, protocol.md §3, §10)

One transfer is assembled at a time (`ctag_layout_asm` into a
`LAYOUT_HARD_MAX` buffer); a `LAYOUT_BEGIN` replaces it, a chunk of another
transfer or with an index ≥ `chunk_count` (or ≥ 32) is ignored and counted.
`LAYOUT_COMMIT` validates **in the order of §3.3** and answers `LAYOUT_STATUS`:

1. a transfer with this `xfer_id` exists → `NOT_FOUND`
2. all chunks present → `INCOMPLETE` + the `missing` bitmap (the transfer stays open)
3. `total_len` and the concatenated length ≤ `LAYOUT_HARD_MAX` → `TOO_LARGE`
4. lengths consistent, every chunk but the last exactly 150 bytes → `INVALID`
5. `SHA-256(layout)[0:16]` = digest → `DIGEST_MISMATCH`
6. the tag is assigned here with exactly this epoch → `NOT_ASSIGNED` (none, or an older assignment) / `STALE_EPOCH`
7. revision: below the history's revision, or equal with another digest → `STALE_REVISION`;
   equal with the same digest → **DUPLICATE** (below)
8. `fontpack_id` = the active, validated pack → `FONTPACK_MISMATCH`
9. `ctag_layout_validate()` — §4.3 bounds in order, then every strike against the active pack
   → `INVALID` / `UNSUPPORTED` / `TOO_LARGE` / `FONTPACK_MISMATCH`

Accepted (`OK`): the history takes the new revision, any older pending layout
of the tag that is **not** in a session ends `SUPERSEDED` (a layout being
streamed finishes; the new one waits for the next session), and the layout
is written to the pending ring (§7) before `OK` is answered. A flash failure
answers `STORAGE_ERROR`.

**DUPLICATE** (§10): a displayed revision (stored result `OK`) re-sends its
stored result under the new `update_id`; a revision still pending adopts the
new `update_id` (and its pending record is rewritten so a reset keeps it); a
revision that ended without being displayed is accepted again as a
re-delivery.

**Results** (§3.4): every final outcome is a `DELIVERY_RESULT` with a
bridge-local `result_seq`, re-sent every `MESH_RESULT_RETRY_MS` (2 s) up to
`MESH_RESULT_RETRIES` (5) times until `RESULT_ACK`. `result_seq` survives
resets without a settings write per result: the bridge persists the end of a
reserved block of 16 and starts the next boot there, so a sequence number is
never reused (§10). A job's result is written to the history (settings)
*before* its pending record is consumed, and a record whose revision already
has a stored result is dropped at boot — a reset between the two never
delivers a layout twice. Results waiting for an ack are kept in RAM only
(`CTAG_BRIDGE_RESULT_SLOTS`); after a reset the companion's job TTL covers a
lost one, as in the simulator.

`DELIVERY_STAGE` `TRANSFERRING` (authenticated, streaming) and `REFRESHING`
(the tag's `PROGRESS`) are best effort. `LAYOUT_CANCEL` ends a job that is
not in a session with `CANCELLED`. A successful `CMD CLEAR` resets the tag's
history to revision 0 (§10).

## 4. Tag connection scheduler (protocol.md §5.2)

`src/core/sched.c`, one attempt or session at a time:

```
IDLE ──advert of T with work pending──▶ checks: node ready (provisioned, configured, not configuring)
  ▲                                            per-tag back-off (BRIDGE_TAG_BACKOFF_MS after a failure)
  │                                            ≤ BRIDGE_MAX_SUSPENDS_PER_MIN attempts per rolling minute
  │         own mesh sends in flight ─▶ WAIT_SENDS: poll 20 ms, ≤ 500 ms, else defer (no back-off)
  │         bt_mesh_suspend() ── error ─▶ MESH_SUSPEND_FAILED, back-off, no connection attempt
  │         bt_conn_le_create(T, timeout = BRIDGE_CONN_ATTEMPT_MS / 10 ms)
  │                     ── refused ─▶ resume ─▶ CONNECT_FAILED, back-off
  │  CONNECTING ──connected(err)──▶ bt_mesh_resume()   (suspend_ms = suspend → resume's return)
  │     │ watchdog 1.5 s ─▶ cancel (bt_conn_disconnect) ─▶ CANCELLING ──connected(err) or 2 s──▶ resume
  │     ▼
  │  resume ok ── err == 0 ─▶ SESSION: GATT discovery, handshake, transfer (§5)
  │           └─ failed / cancelled ─▶ CONNECT_FAILED, back-off
  │  resume error ─▶ disconnect, MESH_RESUME_FAILED, RECOVERY: retry 100, 200, 400, 800, 1000… ms;
  │                  still failing after 5 s ─▶ sys_reboot (mesh reloads from settings)
  └── SESSION done ─▶ disconnect ─▶ DISCONNECTING ──disconnected──▶ IDLE
```

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
| per-tag back-off 15 s | half a wake period: a failing tag is retried in its next window, at most one attempt per window |
| wait for own sends ≤ 500 ms | a pending segmented send (a result) is not stalled by the pause; beyond that the attempt is deferred to a later advertisement rather than holding the mesh |
| watchdog 1.5 s, cancel confirmation 2 s | the host's own create timeout normally reports first; the watchdog only covers a lost report |
| resume retries ≤ 1 s apart, reboot after 5 s | a stuck scanner restart recovers from settings instead of leaving the relay silent |

A message the gateway sends while the bridge is suspended may exhaust the
lower-transport retransmissions (NCS defaults: 2 unicast retransmissions,
~200 ms apart); the gateway then re-sends the whole message (§3.2 rule 1, up
to 3 times). The hardware test plan measures this (§11).

## 5. Tag session (protocol.md §5.3–§5.6, §10)

`src/core/tagsess.c`, event-driven on the work queue:

1. **GATT** (`central.c`): primary service by UUID, its characteristics, then
   the CCC descriptors (each belongs to the closest characteristic value
   before it). Handles are cached per tag (20 entries) and rediscovered after
   a session ending `INVALID` or `TIMEOUT`. `CTRL` is subscribed for
   indications, `STATUS` for notifications.
2. **CAPS** read: `proto` 1 (else `VERSION_MISMATCH`), the expected tag id
   (else `NOT_FOUND`), panel geometry, planes, plane flags, initial credits.
3. **Handshake** (`ctag_session`, bridge role): `HELLO` with a fresh
   `nonce_b` (`sys_csrand_get`), `CHALLENGE` → `AUTH`, `AUTH_OK` verified;
   then the first `CREDIT`. A tag `ERROR` or a failed `mac_t` ends the session;
   `AUTH_FAILED`, `STALE_EPOCH`, `VERSION_MISMATCH` and `NOT_FOUND` also end
   the tag's jobs of that epoch with that status (§10).
4. **Jobs** present when the session started, in arrival order:
   - `CMD`: wait for a credit, `CMD{cmd, update_id}`, the tag's `RESULT`.
   - layout: if `FRAME_END` went out in an earlier session and this
     `CHALLENGE` reports the unknown display state for exactly the job's
     `(epoch, revision)`, the job ends `DISPLAY_STATE_UNKNOWN`. Otherwise the
     pending record is read back, `ctag_render_init()` checks the layout
     against the tag's geometry (the §4.4 panel rule: `INVALID`), and the
     **render pre-pass** `ctag_render_frame_digest()` renders every strip of
     every plane once to compute `FRAME_BEGIN.digest`. Then `FRAME_BEGIN`,
     wait for its credit (or the tag's immediate `RESULT`), `PLANE_DATA`
     records of ≤ 189 bytes rendered strip by strip (`BRIDGE_STRIP_ROWS`
     rows per strip, re-rendered from the layout — no frame buffer), plane 0
     then plane 1, `FRAME_END`, and the `RESULT` (60 s bound for the refresh).
5. **Flow control**: every record consumes one of the tag's credits; at most
   4 records per connection event (after the fourth the session waits one
   connection interval); at most `CTAG_BRIDGE_ATT_INFLIGHT` DATA fragments
   are handed to the host at once (below `BT_ATT_TX_COUNT`, so a write never
   blocks the work queue). Fragments carry `ATT_VALUE_MAX` bytes (ATT MTU 23).
6. **Timeouts**: 5 s without progress (any value received, or a record sent)
   → `TIMEOUT`; `RESULT` within 60 s of `FRAME_END`/`CMD`.
7. **Result timing** in `DELIVERY_RESULT`: `wake_ms` = layout validated →
   connected (restored jobs count from the boot), `suspend_ms` = the §5.2
   pause, `transfer_ms` = the job's `FRAME_BEGIN` → `RESULT` (without the
   pre-pass), `refresh_ms` from the tag.

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
- **Pending layouts**: 8 KiB records (64-byte header with tag, epoch,
  revision, update_id, pack id, layout digest, length and a CRC over header
  and layout; the layout; a "consumed" word in the last 4 bytes) written
  **round-robin over the ring**. A record is written layout first and header
  last (a torn write is never valid), consumed by programming its last word
  (no erase), and the next record goes to the next free slot, so the erase
  wear of frequent deliveries spreads over the whole working space instead
  of one sector per tag. The ring keeps at least 2 × 20 + 2 records (a
  session's record and a waiting one per tag). At boot every record is
  scanned: live ones for an assigned tag and epoch become jobs again (in
  write order), the rest are consumed, and the ring position continues after
  the newest record.
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
| `caps.max_frame` / `caps.credits` | 4096 / 2 | 1024 / 1 |
| client chunk (`FONT_DATA`) | 2048 B | 988 B |

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
  scheduler, session, delivery, flash cache).

The companion's `bridge_maint/client.py` drives it unchanged: see §10.

## 9. Memory

`python tools/build.py bridge-nrf52840dk bridge-nrf52dk` (NCS v3.4.1,
2026-09-28). Flash against the code partition (the DKs' default MCUboot
partitions are replaced by one `code_partition` that ends at the settings
storage); RAM after every static allocation including stacks.

| Target | Flash used / region | Headroom (min 15 %) | RAM used / 64·256 KiB | RAM free (target) | verify_stack | Result |
|---|---|---|---|---|---|---|
| bridge-nrf52840dk | 301,272 / 1,015,808 B | 70.3 % | 109,186 / 262,144 B | 152,958 B (none) | 16/16 pass | ok |
| bridge-nrf52dk | 272,928 / 499,712 B | 45.4 % | 65,321 / 65,536 B | **215 B (8,192)** | 16/16 pass | resource-miss |

Static RAM by component (linker map):

| Component | nRF52840 | nRF52832 |
|---|---:|---:|
| bridge core state (`br`: delivery, session, flash cache, scheduler) | 20,512 | 15,776 |
| — of which the layout being assembled + the layout being rendered | 8,192 | 8,192 |
| maintenance port (buffers, rings, thread stack) | 19,587 | 7,774 |
| bridge work-queue stack | 4,096 | 3,584 |
| event queues, mesh queues, GATT cache, sightings | 4,120 | 2,840 |
| Zephyr controller | 16,269 | 9,439 |
| Bluetooth host | 14,483 | 9,195 |
| Bluetooth Mesh | 8,448 | 5,336 |
| kernel stacks (ISR, system work queue, main, idle) | 6,378 | 6,346 |
| PSA (nrf_security + Oberon) | 1,772 | 1,856 |
| other (USB stack on the nRF52840, drivers) | 10,627 | 813 |

The nRF52832 fragment (`socs/nrf52832.conf`) already trims what a mesh
bridge does not need: one advertising set (relays share the main set), one
auxiliary set (0 does not build), 31-byte extended-advertising receive PDUs,
68-byte HCI events and 65-byte commands, 10 discardable report buffers, no
controller duplicate filter, no delayable mesh messages, mesh settings on the
system work queue, 8 PSA key slots, 1 KiB maintenance frames with one credit,
a 3.5 KiB work-queue stack and a 2 KiB maintenance stack (both **to be
measured** with `debug.conf` on hardware), 16 jobs and 8 result slots. The
remaining gap to 8 KiB free is structural. Options, none implemented:

- assemble layouts in their pending-ring record in external flash (chunks
  written as they arrive) and keep **one** 4 KiB layout buffer, borrowed by
  the commit validation and by the session; the session then pre-renders its
  frame into flash during the digest pre-pass and streams from flash (−4 KiB;
  more flash wear, slower first record);
- a 10-tag assignment table on this SoC (`CAPS_STATUS.max_tags` = 10; −1.3 KiB);
- 512-byte maintenance frames and a trimmed INFO counter set (−1.5 KiB);
- smaller Bluetooth RX / system work queue / ISR stacks once measured on
  hardware.

The controller stays the Zephyr controller (`bt-ll-sw-split`) in every case.
`hardware/matrix.yaml` records the board as `blocked` with this measurement.

## 10. Tests

`apps/bridge/tests/core` — ztest on `native_sim` and `native_sim/native/64`
(twister; the same sources as the firmware core, the flash simulator as a
32 MiB NOR with 4-byte program units and power-cut injection):

| Suite | Covers |
|---|---|
| `bridge_flash` | geometry (DK 8 MiB/1 MiB, production 16 MiB, caps, errors); > 16 MiB address map; directory record layout; A/B activation; **a power cut at every byte of an activation** and before its erase; pending records (odd and maximum length, CRC, consume, torn writes at every stage); read cache |
| `bridge_fonts` | install over the FONT_* path in odd chunk sizes, boot validation, a corrupt index at boot, every install error in order, the slot flip, a session view blocking an overwrite (`BUSY`), FLASH_TEST positions and `BUSY` rules |
| `bridge_delivery` | §3.3 order (NOT_FOUND, INCOMPLETE + bitmap + resend, TOO_LARGE, INVALID, DIGEST_MISMATCH, NOT_ASSIGNED, STALE_EPOCH, FONTPACK_MISMATCH, UNSUPPORTED, missing strike, STALE_REVISION before the pack check); DUPLICATE for a pending, a displayed and an undisplayed revision (and after a reset); SUPERSEDED, in-session jobs untouched, LAYOUT_CANCEL; assignments (idempotent delete, epochs, the 20-tag table, persistence); tag commands and CLEAR resetting the history; pending layouts surviving resets, finished ones not redelivered; ring wear levelling across resets; result retry schedule and RESULT_ACK; result_seq never reused across four resets; node reset |
| `bridge_sched` | mocked `bt_mesh_suspend/resume` and `bt_conn_le_create`: connection with resume before the session and suspend_ms; **suspend rejected → no connection attempt**; `-EALREADY` never leaves the mesh suspended; **every failed attempt resumes** (refused create, host timeout, failed establishment); **cancellation** confirmed, unconfirmed (resume refused while initiating → recovery), and completing mid-cancel; **resume failure → disconnect, recovery with back-off, reboot after 5 s**; **disconnect mid-transfer**; waiting for / deferring on own sends; not while configuring; **20 failing tags for 5 minutes never exceed 6 suspends in any rolling minute, mesh suspended ≤ 12 % of the time** |
| `bridge_session` | a fake tag built from `ctag_session` (tag role), `ctag_txn` and `ctag_frag`: end-to-end deliveries (1 plane; 2 planes rotated; QR on a BWR panel) whose RESULT digest equals the fixture frame digest, ≤ 4 records per connection event, a credit for every record, indications before or after the write response; the tag's duplicate answer; **disconnect mid-transfer** (no result, job pending, next session restarts at offset 0); RESULT lost at a reset → `DISPLAY_STATE_UNKNOWN` then redrawn; STALE_EPOCH and AUTH_FAILED ending the epoch's jobs; the panel rule; CAPS of another tag; a silent tag timing out after 5 s; CMD CLEAR/SLEEP in arrival order |
| `bridge_maint` | HELLO (caps, credits, exemption, reset), answers held without credit, version mismatch, UNSUPPORTED (unknown, mesh and delivery types), malformed CBOR, CRC errors, the client's install sequence, INFO counters, FLASH_TEST idempotency, REBOOT answered first, the streaming COBS writer byte-identical to the library |
| `bridge_golden` | **every `render.json` scenario rendered from `fontpack_test.ctfp` installed in the store (slot 1, through the cache) reproduces the fixture frame digest and each plane digest strip by strip** |

`apps/bridge/tests/maint_pty` (build-only in twister) runs the maintenance
core behind a native_sim pseudo-terminal; `interop.py` drives it with the
companion's own `BridgeMaintClient`: HELLO, PING, FONT_STATUS, a complete
install, the skip of an active pack, a forced install into the other slot in
333-byte chunks, FLASH_TEST twice, INFO.

```bash
python tools/build.py --shell          # then, inside the container:
apt-get update && apt-get install -y make
west twister -T /work/apps/bridge/tests -p native_sim -p native_sim/native/64 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-bridge
west build --no-sysbuild -p always -b native_sim -d /build/bridge-maint-pty /work/apps/bridge/tests/maint_pty \
  -- -DZEPHYR_EXTRA_MODULES=/work
python3 /work/apps/bridge/tests/maint_pty/interop.py /build/bridge-maint-pty/zephyr/zephyr.exe
```

Result (2026-09-28): 108 of 108 test cases pass (54 per platform); the
interop script passes.

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
| H16 | Resets | reset during commit, during a session, during a result retry | pending layouts restored; no duplicate delivery; result_seq continues upward |

## 12. Differences from the simulator

The firmware follows `sim/bridge.py`; where it differs:

| Firmware | Simulator | Why |
|---|---|---|
| A repeated `LAYOUT_COMMIT` after acceptance answers `DUPLICATE` | `NOT_FOUND` (the transfer is dropped once validated) | the assembler keeps the layout until the next BEGIN (library design); a lost `LAYOUT_STATUS OK` then does not end the gateway's delivery with `NOT_FOUND` |
| `TAG_CMD IDENTIFY` / `REFRESH` (and unknown commands) answer `UNSUPPORTED` without a session | forwarded to the tag, which answers `UNSUPPORTED` | same result, no connection and no mesh pause for a command no tag supports (task requirement) |
| Pending layouts in 8 KiB records in a wear-levelled ring | a dictionary; fontpack.md §3 describes one 4 KiB sector per tag | a `LAYOUT_HARD_MAX` layout plus its header does not fit 4 KiB, and a fixed sector per tag would be erased on every delivery to it (NOR endurance) |
| Tag commands are not persisted across a reset | kept (the simulator's jobs survive `reboot()`) | only layouts are written to flash; the companion's TTL covers a lost command |
| `FONT_DATA` erases 4 KiB sectors ahead | 64 KiB blocks | bounds each request below the client's 2 s timeout (MX25R block erase up to 3.5 s) |
| `FONT_STATUS` reports a pack only when it passed the boot validation | the directory record | a corrupt active pack is then reinstalled by the companion instead of being skipped as "already active" |
| `FLASH_TEST` also tests the **last** sector of the inactive slot and treats the pending ring as in use | first sector of the inactive slot, the 16 MiB boundary, the last sector | spec.yaml's `FLASH_TEST` text; the ring holds accepted work |
| `wake_ms` of a job restored at boot counts from the boot | validation time kept | the validation time is RAM state |

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
- nRF52832: RAM (§9); the SPI NOR pins of `boards/nrf52dk_nrf52832.overlay`
  are placeholders; `BT_BUF_EVT_RX_SIZE=68` assumes no HCI event above 68
  bytes (true for the commands the bridge uses — verify with a debug build).
- USB VID/PID 1209:0001 is the pid.codes test pair; `MESH_COMPANY_ID` is
  0xFFFF (spec).
- CI: `.github/workflows/ci.yml` runs twister on `tests/ztest` only; add
  `-T /work/apps/bridge/tests`.
