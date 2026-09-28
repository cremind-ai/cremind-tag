# Gateway firmware (`apps/gateway`)

The gateway is the companion's radio: a serial protocol server on USB CDC ACM
(nRF52840) or a UART (nRF52832) in front of a Bluetooth Mesh provisioner that
provisions and configures the bridges and delivers layouts to them. This
document describes the firmware; the protocols it implements are normative in
[protocol.md](protocol.md) (§1, §2, §3, §10) and `protocol/spec.yaml`. The
companion's gateway client (`companion/src/cremind_tag/gateway/`) is the peer
it must interoperate with, and the companion's simulator
(`companion/src/cremind_tag/sim/gateway.py`, [simulator.md](simulator.md))
implements the same rules; §12 lists every place the firmware differs and why.

| Target | Board | Status | Memory |
|---|---|---|---|
| `gateway-nrf52840dk` | `nrf52840dk/nrf52840` | builds, `verify_stack.py` 16/16, meets its targets; not yet run on hardware | [§8](#8-memory) |
| `gateway-nrf52dk` | `nrf52dk/nrf52832` (stand-in for the nRF52832 + CH340 board) | builds, `verify_stack.py` 16/16; **4,688 B RAM free with a reduced queue and unmeasured stacks — subject to resource qualification** | [§8](#8-memory) |

Build: `python tools/build.py gateway-nrf52840dk gateway-nrf52dk` (on Windows:
`unset VIRTUAL_ENV; companion/.venv/Scripts/python.exe tools/build.py …`); see
[building.md](building.md).

---

## 1. Architecture

```
  Bluetooth context (BT RX thread, mesh advertiser / work queues)          UART or CDC ACM interrupt
  vendor model handlers ─┐ send_cb start/end ─┐ cfg-client callbacks ─┐        │ RX ring  ▲ TX ring
  PB-ADV link/node_added ─┤ scan listener (unprovisioned beacons) ─────┤        ▼          │
                          ▼                                            ▼   gw_wake()       │
                     gw_evq (k_msgq of struct gw_evt, 24 deep)  ─────────────┐             │
                                                                             ▼             │
                      main thread after start-up: the gateway loop (gw_thread.c, cooperative)
                      drain RX ring → gw_core_rx()  ·  drain gw_evq → gw_core_*()  ·  gw_core_poll()
                      sleep until gw_core_next_deadline() or the next wake-up
                                                   │
                ┌──────────────────────────────────┴──────────────────────────────────┐
                │ src/core (Bluetooth-free, single-threaded, time passed in)          │
                │  gw_serial.c   framing, credits, HELLO, answers, retained/best-     │
                │                effort events, idempotency, dispatcher (§1, §10)     │
                │  gw_delivery.c queue + layout arena, one transfer at a time, BEGIN/ │
                │                CHUNK/COMMIT, INCOMPLETE, commit timeouts, results   │
                │  gw_lane.c     the one outstanding segmented send (§3.2 rule 1),    │
                │                unsegmented send queue                               │
                │  gw_nodes.c    node table, provisioning, configuration, removal,    │
                │                ASSIGN/UNASSIGN/TAG_CMD, scan, TAG_SEEN, inventory   │
                │  gw_ring.c     FIFO byte arena (layouts, best-effort events)        │
                └───────────────────────┬─────────────────────────────────────────────┘
                                        │ struct gw_backend (write, mesh_send, mesh_cfg,
                                        ▼ provision, CDB, settings, SHA-256, counters)
          mesh.c (models, send_cb, cfg client, PB-ADV, CDB) · store.c (settings) · uart_io.c
```

- **One context owns all protocol state.** Every Bluetooth callback only
  copies what it received into a `gw_evt` and posts it to `gw_evq`
  (`K_NO_WAIT`; a full queue is counted in `evq_dropped` and recovered by the
  protocol's own retries). The UART interrupt only fills and drains two
  rings. The gateway loop is the only caller of the core and of the mesh send
  APIs, so the core needs no locks and never blocks the Bluetooth stack.
- **The loop runs on the main thread** once start-up is done
  (`CONFIG_MAIN_THREAD_PRIORITY=-2`, cooperative, below the Bluetooth RX
  thread, as the Zephyr mesh provisioner sample calls the Bluetooth API). No
  second stack is spent; each pass handles the bytes and events that arrived,
  runs the timers, pumps the transmitter and sleeps until the core's next
  deadline.
- **The core is portable C** (`src/core/`): no Bluetooth or kernel calls.
  Time, bytes and mesh events are passed in; mesh sends, configuration-client
  steps, provisioning, CDB updates, settings and SHA-256 go through `struct
  gw_backend`. The native_sim tests drive the same sources with a mocked
  backend and a fake clock (§11); the interop build runs them with the real
  loop and UART glue on a PTY.

### Start-up (`main.c`, `mesh.c`)

1. Serial port first (`uart_io_init`): a HELLO sent while the mesh starts is
   buffered in the RX ring.
2. `psa_crypto_init()`, `bt_enable()`, `bt_mesh_init()`, `settings_load()` (the
   order of firmware-notes §3).
3. `bt_mesh_cdb_create(random net key)`: `-EALREADY` means the network was
   loaded from settings. On first boot a random app key (index 0) is added to
   the CDB.
4. If the node is not provisioned: `bt_mesh_provision()` of itself at
   `0x0001` with a random device key (a stale CDB entry for `0x0001`, left by a
   power loss mid first boot, is removed first).
5. Self-configuration once (CDB flag `CONFIGURED` of `0x0001`): the
   configuration client adds app key 0 to the local node and binds
   `LAYOUT_CLI`, `MGMT_CLI` and the Health Client to it.
6. `boot_id` from `sys_csrand_get()`; the CDB's nodes (except `0x0001`) and the
   stored names and assignments are loaded into the core; the loop starts and
   asks every configured bridge for `CAPS_GET` + `HEALTH_GET`.

If the mesh does not come up the serial server still runs: `INFO` reports the
error in the `mesh_init` counter and requests to bridges fail.

---

## 2. Serial protocol server (§1, §10)

| Rule | Implementation |
|---|---|
| Framing (§1.1) | `ctag_serial_rx` (COBS stream decoder + frame checks, counters `len_errors`, `crc_errors`, `version_errors`, `cobs_errors`, `oversize`) into one 4096-byte receive buffer. Frames are processed as soon as they complete. |
| Transmit | Answers and events are built in one decoded-frame buffer (`CONFIG_CTAG_GW_TX_FRAME`) and COBS-encoded **while** they are written into the UART ring, looking ahead for each code byte, so no encoded copy is kept. The output equals `ctag_cobs_encode()` byte for byte (tested around the 254-byte block boundary). |
| Unknown type | `UNSUPPORTED`. `FONT_*` and `FLASH_TEST` are bridge maintenance-port messages: `UNSUPPORTED` here, as in the simulator. |
| Malformed payload / missing required field | `INVALID` (`text` "malformed CBOR payload" / "missing field"), counted in `invalid`, never remembered as an op result. Required fields are those of `cbor_msgs.py` `REQUESTS`. |
| Before HELLO | nothing is answered (`overruns`). Frames flagged RESPONSE or EVENT are ignored (`unexpected_frames`). |
| HELLO (§10) | Exempt from credits. Requires `proto` and `name`; `proto` ≠ 1 answers `VERSION_MISMATCH` and opens no session. Drops unsent answers and queued best-effort events, sets the gateway's send window to `SERIAL_DEFAULT_CREDITS` + the request's grant byte, answers at once with grant byte 0 and `caps {max_frame 4096, credits 4, role GATEWAY, board, max_bridges 5, max_tags 20}`, then re-sends every retained event. |
| Credits (§1.3) | `caps.credits` = `CONFIG_CTAG_GW_SERIAL_CREDITS` (4) answer slots. A request is processed when its frame completes; its answer waits in a slot until the gateway holds a host credit, and **the request's credit is returned in the grant byte of its own answer**, so a host that respects credits never has more than four requests outstanding. The gateway sends only while it holds host credits; each received frame's grant byte adds to them. A frame the host sent without credit is processed if a slot is free, else dropped (`overruns`); `credit_violations` counts them. |
| Order of output | HELLO answer, then answers, then retained events not yet sent this session, then best-effort events. |
| Retained events (§1.2) | `EVT_PROVISIONED`, `EVT_NODE_CONFIGURED`, `EVT_NODE_REMOVED`, `EVT_ASSIGN_RESULT`, `EVT_RESULT` get `seq` from 1 per boot and are kept encoded in a ring of `SERIAL_EVENT_RETAIN` (16) slots of 104 bytes (`EVT_RESULT` is at most 100) until `EVENT_ACK {seq}` (cumulative). Overflow drops the oldest (`events_dropped`). Counters `retained` and `event_seq` show the ring. |
| Best-effort events | `EVT_STAGE`, `EVT_UNPROV_BEACON`, `EVT_TAG_SEEN`, `EVT_BRIDGE_INFO` are encoded straight into a FIFO arena (`CONFIG_CTAG_GW_EVQ_BYTES`); discarded (`events_discarded`) without a session, without a host credit at emit time, or when the FIFO is full. |
| Idempotency (§1.4, §10) | The last `SERIAL_IDEMPOTENCY_SLOTS` (32) `op_id`s of `PROVISION`, `CONFIGURE_NODE`, `REMOVE_NODE`, `ASSIGN_TAG`, `UNASSIGN_TAG`, `DELIVER_LAYOUT`, `CANCEL_DELIVERY`, `TAG_COMMAND`, `REBOOT`, `IDENTIFY_NODE` with their `status` and `text`. A repeat answers them with `detail = DUPLICATE` and does nothing (`duplicate_ops`). `BUSY`, `NO_RESOURCES`, `PROVISIONING_ACTIVE` are not remembered. The oldest entry is forgotten first. |
| REBOOT (§10) | Answered, then reset once the answer's last byte is in the driver (the firmware flushes the ring, waits 50 ms, `sys_reboot`). If the answer cannot go out (dropped by a HELLO, no credit), the gateway resets anyway 2 s later, as the simulator does. |
| Identity (§10) | nRF52840: the USB serial number is the device id (`CONFIG_HWINFO`, FICR, 16 hex digits), from which the companion derives `gw-<uuid5>`. nRF52832: no USB serial number; the companion uses the port. |

`PING` answers `uptime_s`; `INFO` `fw` (`VERSION`, `0.1.0`), `build` (the git
hash Zephyr's `APP_BUILD_VERSION` records), `boot_id`, `caps` and `counters`;
`GET_COUNTERS` the counters. Answers to `PING`, `INFO`, `LIST_NODES`,
`GET_INVENTORY`, `GET_COUNTERS` are encoded when they are sent (live data).

**Counters** (INFO / GET_COUNTERS): `frames_rx`, `frames_tx`, `crc_errors`,
`len_errors`, `version_errors`, `cobs_errors`, `oversize`, `unexpected_frames`,
`overruns`, `credit_violations`, `unsupported`, `invalid`, `internal_errors`,
`hellos`, `events_dropped`, `events_discarded`, `retained`, `event_seq`,
`duplicate_ops`, `busy`, `deliveries_accepted`, `results`, `duplicate_results`,
`repeated_results`, `stale_status`, `mesh_send_retries`, `mesh_send_failures`,
`mesh_busy`, `chunks_resent`, `commit_resends`, `unexpected_mesh`,
`unseg_dropped`, `provisions`, `beacons`, `tag_seen_limited`, `reboots`,
`queue_depth`, `layout_arena_used`, `nodes`, `assignments`, `uptime_s`, and
from the platform `uart_rx_bytes`, `uart_tx_bytes`, `uart_rx_overflow`,
`uart_rx_paused`, `mesh_init`, `mesh_start_errors`, `cfg_send_errors`,
`evq_dropped`.

**Serial driver.** `uart_io.c` uses the interrupt-driven UART API on every
link (the nRF UARTE, the device_next CDC ACM UART, the native PTY UART). When
the RX ring is full it disables RX interrupts until the loop has drained it:
lossless back-pressure on USB (the host is NAKed); on a plain UART the
hardware FIFO may overflow (`uart_rx_paused`, `uart_rx_overflow`), which the
frame CRC catches and the companion's retry repairs.

---

## 3. Mesh provisioner (§2)

**Composition** (element 0): Configuration Server, Configuration Client, Health
Client, vendor `LAYOUT_CLI` (0x0002) and `MGMT_CLI` (0x0004) of company
`MESH_COMPANY_ID`. Handlers accept exactly the spec lengths
(`BT_MESH_LEN_EXACT`). Addresses: the gateway is `0x0001`; bridges get the CDB
allocator's next free address. The gateway is not a relay (`BT_MESH_RELAY=n`);
the bridges relay. Friend, Proxy and Low Power are off. Default TTL 5,
network transmit 3 × 20 ms, `BT_MESH_ADV_EXT` (never the legacy advertiser),
`TX_SEG_MAX = RX_SEG_MAX = 16`.

**Scan (`SCAN_UNPROV {duration_s, uuid_filter?}`).** The mesh scans all the
time; a `bt_le_scan_cb_register()` listener picks out unprovisioned device
beacons (AD type 0x2B, beacon type 0) with their RSSI, which the provisioner's
`unprovisioned_beacon` callback does not carry. While the scan window is open,
each UUID matching the prefix is reported once as `EVT_UNPROV_BEACON {uuid,
rssi, oob}` (up to 16 devices per scan). `duration_s = 0` closes the window.

**`PROVISION {op_id, uuid, name?}`**

| Condition | Answer / outcome |
|---|---|
| a provisioning, configuration or removal is running | `PROVISIONING_ACTIVE` (not remembered) |
| the UUID is already in the CDB | `ACCEPTED`, then at once `EVT_PROVISIONED {addr, elements, OK}` of the existing node |
| `MAX_BRIDGES` (5) bridges in the CDB | `NO_RESOURCES` "MAX_BRIDGES reached" |
| `bt_mesh_provision_adv()` refuses (`-EBUSY`: a link is still open) | `PROVISIONING_ACTIVE` |
| started | `ACCEPTED`; PB-ADV, no OOB, address from the CDB |
| `node_added` then link closed | `EVT_PROVISIONED {op_id, uuid, addr, elements, OK}`; the name (≤ 32 bytes, cut at a UTF-8 boundary) is stored |
| link closed without `node_added`, the link never opened | `EVT_PROVISIONED {addr 0, elements 0, NOT_FOUND}` (nobody answered with that UUID) |
| link opened but provisioning did not complete, or no link close within 90 s | `… TIMEOUT` |

**`CONFIGURE_NODE {op_id, addr, relay, ttl}`**: `NOT_FOUND` for an address
not in the CDB, `PROVISIONING_ACTIVE` while another provisioning /
configuration / removal runs, `INVALID` for a TTL of 1 or above 127, else
`ACCEPTED` and this sequence, one step at a time, each answered by the
configuration client's status callback within 5 s, each tried up to 3 times:

| Step | Message | Success |
|---|---|---|
| 1 | Config AppKey Add (net 0, app 0, the CDB's app key) — segmented, sent through the lane of §4 with its own `send_cb` | status 0 |
| 2 | Model App Bind, `LAYOUT_SRV` | status 0 |
| 3 | Model App Bind, `MGMT_SRV` | status 0 |
| 4 | Relay Set (`relay`, retransmit 2 × 20 ms) | relay state as asked |
| 5 | Default TTL Set (`ttl`) | TTL echoed |
| 6 | Network Transmit Set (3 × 20 ms) | any answer |

Then the CDB node is flagged `CONFIGURED` and stored,
`EVT_NODE_CONFIGURED {op_id, addr, OK}` is emitted and `CAPS_GET` +
`HEALTH_GET` refresh the inventory. A failure emits the event with `TIMEOUT`
(no answer after 3 tries), `UNSUPPORTED` (status 0x02: the node lacks the
vendor model) or `NO_RESOURCES` / `INTERNAL`.

**`REMOVE_NODE {op_id, addr}`**: `NOT_FOUND` / `PROVISIONING_ACTIVE` as above,
else `ACCEPTED`; Config Node Reset (3 tries); whether or not the node answers,
the CDB entry is deleted (`bt_mesh_cdb_node_del(…, true)`), the stored name and
the node's assignments are dropped, and `EVT_NODE_REMOVED {op_id, addr, OK}`
follows.

**`LIST_NODES`**: the bridges in the CDB sorted by address `{addr, uuid,
elements, configured, name, last_seen_s}`; the gateway itself is not listed
(as in the simulator). `last_seen_s` counts from the last mesh message of that
node.

---

## 4. Delivery engine (§1.5, §3, §10)

### Accepting (`DELIVER_LAYOUT`)

Checked in this order: bridge unknown or not configured → `NOT_FOUND` (§10, at
once); empty layout → `INVALID`; larger than `CONFIG_CTAG_GW_LAYOUT_MAX`
(4000 = `LAYOUT_SERIAL_MAX`) → `TOO_LARGE`; `CONFIG_CTAG_GW_DELIVERY_QUEUE`
layouts already queued, or no room in the layout arena → `BUSY` (retry later,
not remembered); else the layout is copied into the arena and the answer is
`ACCEPTED` (stage `GATEWAY_RECEIVED`).

The **layout arena** (`gw_ring.c`) stores queued layouts back to back
(4-byte header, 4-byte alignment) and reclaims space in acceptance order; a
cancelled layout's space returns when the older ones are gone. The nRF52840
arena (20 KiB) holds four queued full-size layouts behind the active one; the
nRF52832's 5 KiB holds one full-size layout plus a typical one.

### Transfer state machine

One transfer at a time, in acceptance order (the simulator's delivery worker):

```
 QUEUED ──start──▶ BEGIN ──end OK──▶ CHUNKS ──last chunk end OK──▶ COMMIT (LAYOUT_COMMIT sent, 10 s)
   │  xfer_id++,    │ LAYOUT_BEGIN     │ LAYOUT_CHUNK i (150 B;           │
   │  SHA-256[0:16] │ (lane)           │ the last one shorter) (lane)     ├─ STATUS OK / DUPLICATE ──▶ EVT_STAGE BRIDGE_RECEIVED,
   │                │                  │                                  │                           at the bridge (waits for its result)
   │                └─ end failed 4× ──┴──────────▶ EVT_RESULT TIMEOUT    ├─ STATUS INCOMPLETE {missing} ─▶ CHUNKS (exactly the missing
   │                                                                      │     chunks, then COMMIT; ≤ 3 rounds, then EVT_RESULT INCOMPLETE)
   │                                                                      ├─ other STATUS ──▶ EVT_RESULT <status>, zero digest
   │                                                                      ├─ no STATUS in 10 s ──▶ COMMIT again (3×), then EVT_RESULT TIMEOUT
   │                                                                      └─ DELIVERY_RESULT for this update_id ──▶ done (its EVT_RESULT)
   └─ CANCEL_DELIVERY ──▶ EVT_RESULT CANCELLED (answer OK)
```

- **The lane (§3.2 rule 1).** `gw_lane.c` holds one segmented send
  gateway-wide: `LAYOUT_BEGIN`, `LAYOUT_CHUNK`, `ASSIGN_SET`, `TAG_CMD` and the
  configuration client's AppKey Add all request it and wait in FIFO order. The
  next segmented message is handed to `bt_mesh_model_send()` only after the
  previous one's `send_cb.end` reported success; a failed `end` (or a `start`
  error) re-sends the same message up to 3 times, then the owner fails
  (`mesh_send_failures`; a delivery ends with `TIMEOUT`). A refusal for lack of
  buffers (`-ENOBUFS`, `-EBUSY`) is retried after 100 ms without counting as a
  failed end (`mesh_busy`). A 30 s watchdog covers an end that never comes. A
  queued `ASSIGN_SET` goes between two chunks of a transfer, never during one.
- **Unsegmented messages** (≤ 11 access bytes: `LAYOUT_COMMIT`,
  `LAYOUT_CANCEL`, `RESULT_ACK`, `ASSIGN_DEL`, `CAPS_GET`, `HEALTH_GET`,
  `IDENTIFY`) queue in a 16-entry ring that is re-offered after 100 ms when the
  advertiser has no buffer (`unseg_dropped` if the ring is full). They have no
  acknowledgement; their loss is covered by the reply timeouts.
- `xfer_id` is gateway-wide and wrapping, seeded from `boot_id` so a new boot
  does not reuse the previous boot's ids.
- A `LAYOUT_STATUS` that does not match the transfer in its COMMIT phase
  (source, `xfer_id`) is ignored (`stale_status`).
- **`DUPLICATE` is `OK`** (§10): it answers a re-sent commit whose first `OK`
  was lost, or a revision the bridge already holds. The gateway emits
  `BRIDGE_RECEIVED` and waits for the bridge's result either way; a lost
  status never ends a delivery.
- A `DELIVERY_RESULT` for the transfer that is still waiting for its
  `LAYOUT_STATUS` (the status was lost, the bridge already finished) ends the
  transfer at once instead of re-committing.

### Results (§3.4, §10)

Every `DELIVERY_RESULT` is answered with `RESULT_ACK {result_seq}`, also a
copy. The first copy of each `(bridge, result_seq)` (the last 64 pairs, 32 on
the nRF52832) becomes a retained `EVT_RESULT {seq, update_id, bridge, tag_id,
epoch, revision, status, digest(8), battery_mv, timing {wake_ms, mesh_ms,
transfer_ms, refresh_ms, suspend_ms}, flags, stored_epoch}`; `mesh_ms` is the
gateway's transfer time (start to `LAYOUT_STATUS`), the rest come from the
bridge — `flags` (bit0 the tag's stored ACK, bit1 an escalated
unauthenticated status) and `stored_epoch` (the tag's stored epoch, 0 =
unknown) exactly as `DELIVERY_RESULT` carried them. The gateway's own results
(`TIMEOUT`, `CANCELLED`, rejections) carry `flags` 0 and `stored_epoch` 0.
**Exactly one `EVT_RESULT` per `update_id`**: the last 64 (32) reported
`update_id`s — the gateway's own results (`TIMEOUT`, `CANCELLED`, rejections,
failed tag commands) and the bridges' — are remembered, and a later result for
one of them is acknowledged to the bridge and dropped (`repeated_results`).
`DELIVERY_STAGE` becomes `EVT_STAGE` (best effort).

### Cancelling (`CANCEL_DELIVERY {op_id, update_id}`)

Queued here: removed, `EVT_RESULT CANCELLED`, answer `OK`. In transfer or at
the bridge (remembered for the last 16 / 12 validated layouts): `LAYOUT_CANCEL
{update_id}` to the bridge, answer `ACCEPTED`; the bridge ends it. Unknown:
`NOT_FOUND`.

---

## 5. Management operations

| Request | Mesh | Outcome |
|---|---|---|
| `ASSIGN_TAG {op_id, bridge, tag_id, epoch, key}` | `ASSIGN_SET {tag_id, epoch, key, flags 1}` (segmented, lane) | `ASSIGN_STATUS` from that bridge for `(tag_id, epoch)` within 5 s, the request sent at most 4 times → `EVT_ASSIGN_RESULT {status}`; `TIMEOUT` otherwise or when the send fails |
| `UNASSIGN_TAG {op_id, bridge, tag_id, epoch}` | `ASSIGN_DEL {tag_id, epoch}` (unsegmented) | as above |
| `TAG_COMMAND {op_id, bridge, tag_id, epoch, cmd}` | `TAG_CMD {update_id = op_id, …}` (segmented, lane) | the bridge's `DELIVERY_RESULT` with `update_id = op_id` → `EVT_RESULT`; a failed send → `EVT_RESULT TIMEOUT` (revision 0) |
| `IDENTIFY_NODE {op_id, addr}` | `IDENTIFY {seconds 5}` | answer `OK` |
| `GET_INVENTORY` | `CAPS_GET` + `HEALTH_GET` to every configured bridge | answer with the cached items; fresher data follow as `EVT_BRIDGE_INFO` |

These require a configured bridge (`NOT_FOUND` otherwise); at most
`CONFIG_CTAG_GW_MESH_OPS` (8) assignments and tag commands run at once (`BUSY`
beyond). An `ASSIGN_STATUS OK` updates the gateway's assignment table (as the
simulator does: `ASSIGN_TAG` sets `(bridge, tag) → epoch`, `UNASSIGN_TAG`
removes it unless a newer epoch is stored), which the inventory and
`EVT_BRIDGE_INFO` list. Inventory items: `addr, uuid, name, configured,
last_seen_s, assigned` and, once the bridge answered, `fw, fontpack_id, caps
{board, flash_size, max_tags, assigned_count, flags, proto}, board, flash_size`
and `counters` (its HEALTH_STATUS). `caps.assigned_count` is the bridge's own
count of assigned tags (`CAPS_STATUS.assigned`), which `EVT_BRIDGE_INFO`
carries too: the gateway's `assigned` list only holds what it assigned itself,
so a bridge restored or moved with tags already on it reports more; the
companion reports the bridge's count as its capacity use.

`TAG_SEEN` from a bridge becomes `EVT_TAG_SEEN {bridge, tag_id, rssi,
battery_mv, flags}`, at most one per `(bridge, tag)` every 10 s
(`CONFIG_CTAG_GW_TAG_SEEN_INTERVAL_MS`; `tag_seen_limited`).

---

## 6. Persistence

| What | Where | Survives reboot |
|---|---|---|
| Net key, app key, device keys, IV index, sequence number, replay list, CDB (nodes, `CONFIGURED` flags) | Zephyr mesh settings; keys as persistent PSA keys in trusted storage (`BT_MESH_SECURE_STORAGE`) | yes |
| Bridge names | settings `ctag/gw/n/<addr>` | yes |
| Assignment table | settings `ctag/gw/a` (10-byte records) | yes |
| Retained events, idempotency slots, delivery queue, transfers, results de-duplication, scan state | RAM | no — a new `boot_id` tells the companion (§1.2) |

Settings live on NVS in the board's `storage_partition` (nRF52840 DK: 32 KiB at
`0xf8000`; nRF52840 Dongle: 32 KiB at `0xd8000`, below its USB bootloader;
nRF52 DK: 24 KiB at `0x7a000`). The image links into
`code_partition`, which ends where the storage begins (`USE_DT_CODE_PARTITION`),
so it can never grow into it. The replay list is written at most every 10
minutes (`BT_MESH_RPL_STORE_TIMEOUT=600`). A chip erase starts a new network:
every bridge must then be reset and provisioned again.

---

## 7. Configuration

| Option | nRF52840 | nRF52832 | Meaning |
|---|---:|---:|---|
| `CTAG_GW_SERIAL_CREDITS` | 4 | 4 | answer slots = `caps.credits` |
| `CTAG_GW_TX_FRAME` | 2560 | 2048 | largest frame the gateway sends (a full inventory needs ~1.9 KiB) |
| `CTAG_GW_EVENT_MAX` | 104 | 104 | retained event slot (`EVT_RESULT` ≤ 100 bytes: every field at its largest, `flags` and `stored_epoch` included; tested) |
| `CTAG_GW_EVQ_BYTES` | 2048 | 768 | best-effort event FIFO |
| `CTAG_GW_DELIVERY_QUEUE` | 4 | 3 | layouts queued behind the active transfer |
| `CTAG_GW_LAYOUT_ARENA` | 20480 | 5120 | bytes for queued + active layouts |
| `CTAG_GW_LAYOUT_MAX` | 4000 | 4000 | largest layout accepted |
| `CTAG_GW_AT_BRIDGE` | 16 | 12 | validated layouts remembered for cancel and `mesh_ms` |
| `CTAG_GW_RESULT_DEDUP` / `CTAG_GW_REPORTED` | 64 / 64 | 32 / 32 | `(bridge, result_seq)` pairs / reported `update_id`s |
| `CTAG_GW_MESH_OPS` | 8 | 8 | assignments and tag commands in flight |
| `CTAG_GW_ASSIGN_MAX` | 32 | 24 | assignment table |
| `CTAG_GW_UART_RX_RING` / `_TX_RING` | 1024 / 1024 | 768 / 256 | serial driver rings |
| `CTAG_GW_EVQ_DEPTH` | 24 | 16 | Bluetooth → loop event queue |
| `MAIN_STACK_SIZE` (start-up + loop) | 4096 | 3072 | |
| `SYSTEM_WORKQUEUE_STACK_SIZE` | 4096 | 2560 | |
| `BT_RX_STACK_SIZE` | 3300 | 2560 | |
| `BT_MESH_ADV_STACK_SIZE` | 4000 | 2048 | |
| `BT_MESH_SETTINGS_WORKQ_STACK_SIZE` | 1700 | 1400 | |
| `MBEDTLS_PSA_KEY_SLOT_COUNT` | 24 | 20 | persistent mesh keys incl. one device key per CDB node |
| `BT_MESH_ADV_BUF_COUNT` / `RX_SEG_MSG_COUNT` | 16 / 4 | 10 / 2 | |
| `BT_BUF_EVT_RX_COUNT` | 10 | 4 | no connections: few HCI event buffers |

The nRF52840 stacks are the Zephyr mesh provisioner sample's
thread-analysis figures (+50 %); the nRF52832 ones are estimates. Both must be
measured on hardware (§10). Common: `CDB_NODE_COUNT = 6` (MAX_BRIDGES + the
gateway), one subnet and one app key, `BT_EXT_ADV_MAX_ADV_SET = BT_CTLR_ADV_SET
= 1` (no relay/proxy/friend sets), `TX_SEG_MSG_COUNT = 2` (the gateway sends
one segmented message at a time; the second slot only absorbs the tail of a
configuration message whose status beat its segment ACK), no console or
logging, entropy from the RNG peripheral (`ENTROPY_CC3XX=n` on the nRF52840),
USB `1209:0002` (pid.codes test pair — replace with an allocated VID/PID before
shipping), manufacturer "Cremind", product "Cremind Tag gateway".

---

## 8. Memory

`tools/build.py` (NCS v3.4.1, Zephyr controller, `--no-sysbuild`), 2026-09-28:

| Target | Flash used / code partition | Headroom (min 15 %) | RAM used / RAM | RAM free | Stack check |
|---|---|---|---|---|---|
| `gateway-nrf52840dk` | 229,912 / 1,015,808 B (22.6 %) | 77.4 % | 100,372 / 262,144 B | 161,772 B | pass 16/16 |
| `gateway-nrf52dk` | 198,764 / 499,712 B (39.8 %) | 60.2 % | 60,848 / 65,536 B | **4,688 B** | pass 16/16 |
| debug (`debug/rtt.conf`) nRF52840 / nRF52832, before the 104-byte event slots (+128 B RAM since) | 299,052 / 255,764 B | | 103,252 / 63,408 B | 158,892 / 2,128 B | |

The protocol v1 finalisation (`EVT_RESULT` with `flags` and `stored_epoch`)
cost 48–64 B of flash and 128 B of RAM on each: the 16 retained event slots grew
from 96 to 104 bytes (the largest `EVT_RESULT` is now 100 bytes).

Largest RAM users of the nRF52832 build: the core 21,032 B (receive frame
4,096, layout arena 5,120, transmit frame 2,048, encoder scratch ~2.9 KiB,
retained ring 1,792, event FIFO 768, idempotency 512, node table, operation
and de-duplication tables), `main` stack 3,136, system work queue 2,624,
Bluetooth RX thread 2,624, ISR stack 2,112, mesh advertiser 2,112, controller
RX PDUs 1,732, mesh settings work queue 1,472, HCI RX buffers 1,032, mbedTLS
heap 1,024, `gw_evq` 1,024, controller threads 1,472, mesh segmentation 1,504
(segment buffers, `seg_rx`, `seg_tx`), UART rings 1,024. On the nRF52840 the
core is 39,168 B (a 20 KiB arena) and USB adds ~5 KiB.

**nRF52832 verdict.** The application fits with 4.6 KiB spare only after
reducing the delivery queue (a 5 KiB layout arena), the event and HCI buffers
and the thread stacks. The trimmed stacks are not measured; they are the risk,
not the static RAM. Until the resource qualification in
[the board's report](qualification/nrf52832_gateway.md) has measured every
stack's high-water mark through provisioning, configuration and sustained
delivery, treat the nRF52832 gateway as unqualified. If a stack must grow by
more than the spare RAM, the next levers are the layout arena (down to 4,100 B:
one full-size layout at a time) and `CTAG_GW_TX_FRAME` (1,536 B still holds an
inventory of five bridges without long assignment lists).

---

## 9. Flashing (J-Link)

Artifacts are in `build/<target>/zephyr.hex`. Flash from the host (Docker
Desktop cannot pass USB probes through):

```bash
JLinkExe -device nRF52840_xxAA -if SWD -speed 4000 -autoconnect 1   # nRF52832_xxAA for the 832
J-Link> loadfile build/gateway-nrf52840dk/zephyr.hex
J-Link> r
J-Link> g
J-Link> exit
```

or `nrfjprog -f NRF52 --program build/gateway-nrf52840dk/zephyr.hex --sectorerase --verify && nrfjprog -f NRF52 --reset`.

- `--sectorerase` / `loadfile` erase only the image's sectors: the mesh network
  (settings partition) survives an update. `--chiperase` or `nrfjprog --eraseall`
  creates a **new network** on the next boot (new keys): the bridges then need a
  node reset (their maintenance port or a factory reset) and provisioning again.
- nRF52840 DK: the companion link is the **nRF USB** connector (the CDC ACM
  port "Cremind Tag gateway", VID:PID `1209:0002`), not the J-Link USB port.
  The DK's VCOM stays unused (no console).
- nRF52840 Dongle (`gateway-nrf52840dongle`): no probe; it is flashed over its
  factory USB bootloader ([building.md](building.md#flashing-the-nrf52840-dongle-usb-bootloader)),
  and the companion link is the same USB port once the gateway runs.
- nRF52 DK: the companion link is the J-Link OB virtual COM port (115200 8N1);
  on the target board, the CH340's port.
- `cremind-tag gateway ports` lists candidate ports with their serial numbers;
  `cremind-tag gateway info --url <port>` checks the link.

## 10. Debugging

- **Counters first**: `cremind-tag gateway counters` (and `gateway info`,
  `gateway events` to watch events live). Serial problems show in
  `crc_errors`, `overruns`, `credit_violations`, `uart_rx_overflow`; mesh
  problems in `mesh_send_failures`, `mesh_busy`, `commit_resends`,
  `chunks_resent`, `stale_status`, `unexpected_mesh`; lost events in
  `events_dropped` (retained ring overflow) and `evq_dropped`.
- **RTT build** (logs and the thread analyzer on RTT, the serial link
  untouched), in the toolchain container:

  ```bash
  west build --no-sysbuild -p always -d /build/gw-debug -b nrf52840dk/nrf52840 -S bt-ll-sw-split \
    /work/apps/gateway -- -DZEPHYR_EXTRA_MODULES=/work -DEXTRA_CONF_FILE=debug/rtt.conf
  ```

  then `JLinkRTTViewer` (or `JLinkRTTLogger -Device NRF52840_XXAA -If SWD -Speed 4000 -RTTChannel 0 rtt.log`).
  The thread analyzer prints every thread's stack high-water mark every 30 s —
  this is the resource qualification's measurement.

## 11. Tests

**Core ztests** (`apps/gateway/tests/core`, native_sim and
native_sim/native/64; the same `src/core` sources, a mocked backend, a fake
clock). 61 tests in four suites, all passing on both platforms (122 test cases,
twister, 2026-09-28):

- `gw_serial` (19): HELLO caps, version mismatch, nothing before HELLO,
  PING/INFO, UNSUPPORTED types, INVALID payloads not remembered, ignored
  answer-flagged frames and CRC errors, answers waiting for credits and
  returning them, overruns without credit, HELLO dropping unsent answers and
  re-sending retained events, cumulative EVENT_ACK, ring overflow, best-effort
  events needing a credit, repeated `op_id`, transient refusals not remembered,
  idempotency eviction, REBOOT after the answer, REBOOT grace, partial writes.
- `gw_delivery` (22): BEGIN/CHUNK/COMMIT layout with the digest, one
  outstanding segmented send gateway-wide, failed end retried 3× then TIMEOUT,
  status OK → stage → bridge result with timing, `(bridge, result_seq)`
  de-duplication, INCOMPLETE resending exactly the missing chunks for 3
  rounds, commit re-sent 3× then TIMEOUT, rejection → result with zero digest,
  DUPLICATE treated as OK, **lost LAYOUT_STATUS OK recovered by the
  re-commit**, a result ending the status wait, **exactly one EVT_RESULT per
  update_id**, queue BUSY not remembered, arena BUSY, NOT_FOUND bridges,
  INVALID/TOO_LARGE, cancel (queued, in transfer, at the bridge, unknown),
  buffer shortage not counted as a failed send, acceptance order.
- `gw_nodes` (17): provisioning (success with name, conflict, absent device,
  timeout, MAX_BRIDGES, known UUID), configuration (step order and
  arguments, status before the segment ACK, timeouts, refusals, missing
  vendor model), removal (answered and silent), assign/unassign, assignment
  retries and timeout, tag command send failure, configured-bridge checks,
  identify, scan filter and de-duplication, TAG_SEEN rate limit, LIST_NODES
  and GET_INVENTORY with nested assignments and bridge info.
- `gw_ring` (3): arena FIFO/wrap/out-of-order release, reserve/commit, and the
  streaming COBS transmitter equal to the library encoder across the 254-byte
  block boundary.

Run: `west twister -T /work/apps/gateway/tests/core -p native_sim -p native_sim/native/64 -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-gw-core`
(after `apt-get install -y make`).

**Interop test** (`apps/gateway/tests/interop`, [README](../apps/gateway/tests/interop/README.md)):
the gateway core, loop and UART glue on a native PTY UART with a simulated
mesh, driven by the companion's real `GatewayClient`. 2026-09-28: **8/8
scenarios pass** —

| Scenario | Result |
|---|---|
| HELLO, INFO, PING, LIST_NODES, GET_INVENTORY | pass |
| credits under load: 60 PINGs + 20 INFOs at once | pass: 0 credit violations, 0 overruns, no resync, ~540 ms |
| idempotent retries | pass: repeat answers `ACCEPTED` + `DUPLICATE`, no new work, one result |
| retained events: ACK only after the handler, re-sent after HELLO | pass |
| DELIVER_LAYOUT → mesh → EVT_RESULT OK | pass: stages 3, 4, 5, result OK with the layout digest in ~550 ms, `flags` 0 and the tag's `stored_epoch`; a tag's stored ACK arrives as `flags` bit0; INCOMPLETE round; a 4000-byte layout (27 chunks) in ~950 ms; 12 concurrent deliveries with BUSY retried in 2.5 s |
| lost LAYOUT_STATUS OK → DUPLICATE → one EVT_RESULT | pass (re-commit after 10 s) |
| provisioning, configuration, removal | pass |
| REBOOT → new `boot_id`, `SessionStarted.boot_changed` | pass |

---

## 12. Differences from the simulator

| Topic | Simulator | Firmware | Why |
|---|---|---|---|
| Credits | returns a request's credit when its buffer is freed | returns it with the request's answer (four answer slots) | the answer slot is the scarce resource; the host sees the same one-credit-per-answer flow |
| `DUPLICATE` `LAYOUT_STATUS` | no stage event | `EVT_STAGE BRIDGE_RECEIVED`, as for `OK` | protocol §10 (added 2026-09-28): DUPLICATE is OK for the transfer |
| Result while waiting for LAYOUT_STATUS | keeps waiting, may report TIMEOUT too | the result ends the transfer | a bridge that reports a result has the layout; saves 10–40 s and a second result |
| Layout size | any | > 4000 bytes → `TOO_LARGE` | RAM: layouts are kept until transferred; the companion never sends more than `LAYOUT_SERIAL_MAX` |
| Queue full | count only | count or arena bytes | RAM (the nRF52832 arena holds one full-size layout) |
| Concurrent configuration | several `CONFIGURE_NODE` may run | one provisioning/configuration/removal at a time (`PROVISIONING_ACTIVE`) | the configuration client handles one request stream; the lane serialises AppKey Add |
| `REMOVE_NODE` while provisioning | allowed | `PROVISIONING_ACTIVE` | as above |
| `CONFIGURE_NODE` TTL | not checked | 1 or > 127 → `INVALID` | the Configuration Server would reject it |
| Provisioning failures | `NOT_FOUND` | `NOT_FOUND` (link never opened) or `TIMEOUT` (opened, not completed, or 90 s guard) | PB-ADV reports only a link close |
| Known UUID `PROVISION` | reported after the provisioning delay | reported at once | nothing to wait for |
| `UNASSIGN_TAG` send failure | immediate `TIMEOUT` | `TIMEOUT` after 4 × 5 s | `ASSIGN_DEL` is unsegmented: its loss is only visible as a missing reply |
| `EVT_TAG_SEEN` | every sighting | ≤ 1 per (bridge, tag) per 10 s | spec: "rate-limited sighting" |
| `EVT_UNPROV_BEACON` | only devices the simulator knows are unprovisioned | every matching unprovisioned beacon, once per scan, ≤ 16 devices | radio reality; a provisioned node does not beacon |
| Names | any length | ≤ 32 bytes, cut at a UTF-8 boundary | RAM, settings |
| REBOOT on a UART | the TCP connection drops | the port stays open; the companion notices through a timeout and a new `boot_id` | UART has no enumeration (USB re-enumerates as in the simulator) |

## 13. Hardware test plan

Equipment: the gateway DK, five bridge DKs (or bridge boards) with the bridge
firmware, at least two tags, the companion on a PC, a J-Link for RTT. Record
results in the board's qualification report.

1. **Provisioning 5 bridges.** Flash the gateway with a chip erase (new
   network). `cremind-tag mesh scan -d 10` lists five UUIDs with RSSI; for each
   `cremind-tag mesh provision <uuid> --name bN` (provisions and configures).
   Expect `EVT_PROVISIONED` with consecutive addresses `0x0002…`, every
   `EVT_NODE_CONFIGURED OK`, `cremind-tag mesh nodes` listing five configured
   bridges, and a sixth `PROVISION` answering `NO_RESOURCES`. Measure the time
   per bridge; check `mesh_send_failures = 0`.
2. **Relay through an intermediate bridge.** Place bridge B out of the
   gateway's range but in bridge A's (verify with RSSI or by powering A off:
   B's inventory stops refreshing). Deliver layouts to a tag assigned to B.
   Expect results with `mesh_ms` higher than for A, no `commit_resends`, and the
   configuration of a node reachable only through A to succeed (default TTL 5).
3. **Throughput.** From the companion daemon (or a script with
   `GatewayClient`), queue 100 deliveries of 1 KiB and 4 KiB layouts across the
   five bridges. Record deliveries per minute at the gateway (stage
   `BRIDGE_RECEIVED` rate), `mesh_ms` distribution, `busy`,
   `mesh_send_retries`, `chunks_resent`, `commit_resends`; for the nRF52832 also
   `uart_rx_overflow` at 115200 baud (a 4 KiB layout is ~0.4 s of UART time).
4. **Reboot recovery.** (a) `cremind-tag gateway reboot` during a transfer:
   the companion reconnects with a new `boot_id`, re-delivers its uncertain
   jobs; the network, names and assignments are intact (`mesh nodes`,
   `gateway` inventory). (b) Power-cycle the gateway during provisioning and
   during `CONFIGURE_NODE`: after boot the CDB is consistent (a half-provisioned
   node is either listed or absent; re-running `provision` / `configure`
   succeeds). (c) Unplug and replug USB / the UART adapter: the companion
   resynchronises (HELLO) and receives every unacknowledged retained event.
5. **Resource measurement** (both boards, mandatory for the nRF52832): the
   RTT build through steps 1–4; record each thread's stack high-water mark
   from the thread analyzer and check no `PSA_ERROR_INSUFFICIENT_MEMORY` /
   mesh storage errors appear in the log.
6. **Endurance.** 72 h with a delivery every minute per tag; `events_dropped`,
   `evq_dropped`, `internal_errors` stay 0; the settings partition does not fill
   (NVS garbage collection keeps up).

## 14. Open items

- Nothing has run on real hardware: the mesh stack integration (PB-ADV
  provisioning, the configuration client sequence, `send_cb` behaviour with 16
  segments, CDB persistence through reboots, secure storage of device keys),
  USB CDC ACM enumeration and the nRF UARTE path are verified only by building.
- Stack sizes are unmeasured on both boards (§10, §13 step 5).
- The nRF52832 gateway's RAM margin (4.6 KiB) and its reduced queue need the
  resource qualification before it is used.
- USB VID/PID `1209:0002` is a pid.codes test pair; `MESH_COMPANY_ID` 0xFFFF is
  the SIG test value (spec.yaml): both need assigned values before production.
- No watchdog is enabled (optional per the plan); a hang would need a power
  cycle. `CONFIG_WATCHDOG` with a feed from the gateway loop is a small addition
  once the loop's worst-case pass time is measured.
- PB-ADV without OOB authentication (protocol §2): provision in a controlled
  environment.
