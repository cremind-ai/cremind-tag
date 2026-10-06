# Gateway firmware (`apps/gateway`)

The gateway is the companion's radio: a serial protocol server on USB CDC ACM
(nRF52840) or a UART (nRF52832) in front of a Bluetooth Mesh provisioner that
provisions and configures the bridges and delivers layouts to them. This
document describes the firmware; the protocols it implements are normative in
[protocol.md](protocol.md) (§1, §2, §3, §10) and `protocol/spec.yaml`. The
companion's gateway client (Cremind's `app/tags/runtime/gateway/`) is the peer
it must interoperate with, and the companion's simulator
(Cremind's `app/tags/runtime/sim/gateway.py`, [simulator.md](https://github.com/cremind-ai/cremind/blob/main/docs/tags/simulator.md))
implements the same rules; §12 lists every place the firmware differs and why.

**Protocol v2** ([connect-setup.md](connect-setup.md): device identity,
ownership, Noise IK sessions, static-OOB provisioning, DISCOVER, tunnels) is
built with `CONFIG_CTAG_GW_SECURE` on the nRF52840 targets and described in
[§15](#15-protocol-v2-config_ctag_gw_secure); the nRF52832 gateway stays on
protocol v1 (§15.12). Sections 1–14 describe v1 and what v2 keeps. The
nRF52840 gateways also connect to the tags in their own range, as a BLE
relay for the companion's tag sessions (`CONFIG_CTAG_GW_RADIO`,
[protocol.md §11](protocol.md#11-tags-on-the-gateways-own-radio),
[§16](#16-tags-on-the-gateways-own-radio-config_ctag_gw_radio)).

| Target | Board | Protocol | Status | Memory |
|---|---|---|---|---|
| `gateway-nrf52840dk` | `nrf52840dk/nrf52840` | v2, 2 tag links | builds, `verify_stack.py` 16/16, meets its targets; not yet run on hardware | [§8](#8-memory) |
| `gateway-nrf52840dongle` | `nrf52840dongle/nrf52840` | v2, 2 tag links | builds, `verify_stack.py` 16/16, meets its targets; not yet run on hardware | [§8](#8-memory) |
| `gateway-nrf52dk` | `nrf52dk/nrf52832` (stand-in for the nRF52832 + CH340 board) | v1 | builds, `verify_stack.py` 16/16; **4.5 KiB RAM free with a reduced queue and unmeasured stacks — subject to resource qualification**; too small for v2 (a v2 fit build overflows RAM by 12 KB, §15.12) | [§8](#8-memory) |

Build: `python tools/build.py gateway-nrf52840dk gateway-nrf52840dongle
gateway-nrf52dk` (or `uv run python tools/build.py …` when `python` lacks
PyYAML); see [building.md](building.md).

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
                │  v2: gw_secure.c, gw_tunnel.c (§15); gw_radio.c the own radio (§16) │
                └───────────────────────┬─────────────────────────────────────────────┘
                                        │ struct gw_backend (write, mesh_send, mesh_cfg,
                                        ▼ provision, CDB, settings, SHA-256, counters)
          mesh.c (models, send_cb, cfg client, PB-ADV, CDB) · store.c (settings) · uart_io.c
          · central.c (tag links: scan listener, connections, GATT, §16)
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
   `LAYOUT_CLI`, `MGMT_CLI` and the Health Client to it. The two vendor binds
   are Config Model App Bind messages the gateway builds itself (as for
   bridges, §3) and waits up to 2 s for their status:
   `bt_mesh_cfg_cli_mod_app_bind_vnd()` refuses company id 0xFFFF, Zephyr's
   `CID_NVAL`, with `-EINVAL`.
6. `boot_id` from `sys_csrand_get()`; the CDB's nodes (except `0x0001`) and the
   stored names and assignments are loaded into the core; the loop starts and
   asks every configured bridge for `CAPS_GET` + `HEALTH_GET`.

If the mesh does not come up the serial server still runs: `INFO` reports the
error in the `mesh_init` counter and the step that failed in `mesh_step` (1
`bt_enable`, 2 `bt_mesh_init`, 3 `settings_load`, 4 the CDB and its net key, 5
the first app key, 6 provisioning itself, 7 self-configuration's app key, 8 its
binds; 0 once the mesh runs), and requests to bridges fail. Tags on the own
radio are not heard either (the listener rides the mesh's scan).

---

## 2. Serial protocol server (§1, §10)

| Rule | Implementation |
|---|---|
| Framing (§1.1) | `ctag_serial_rx` (COBS stream decoder + frame checks, counters `len_errors`, `crc_errors`, `version_errors`, `cobs_errors`, `oversize`) into one 4096-byte receive buffer. Frames are processed as soon as they complete. |
| Transmit | Answers and events are built in one decoded-frame buffer (`CONFIG_CTAG_GW_TX_FRAME`) and COBS-encoded **while** they are written into the UART ring, looking ahead for each code byte, so no encoded copy is kept. The output equals `ctag_cobs_encode()` byte for byte (tested around the 254-byte block boundary). |
| Unknown type | `UNSUPPORTED`. `FONT_*` and `FLASH_TEST` are bridge maintenance-port messages: `UNSUPPORTED` here, as in the simulator. |
| Malformed payload / missing required field | `INVALID` (`text` "malformed CBOR payload" / "missing field"), counted in `invalid`, never remembered as an op result. Required fields are those of `cbor_msgs.py` `REQUESTS`. |
| Before HELLO | nothing is answered (`overruns`). Frames flagged RESPONSE or EVENT are ignored (`unexpected_frames`). |
| HELLO (§10) | Exempt from credits. Requires `proto` and `name`; `proto` ≠ 1 answers `VERSION_MISMATCH` and opens no session. Drops unsent answers and queued best-effort events, sets the gateway's send window to `SERIAL_DEFAULT_CREDITS` + the request's grant byte, answers at once with grant byte 0 and `caps {max_frame 4096, credits 4, role GATEWAY, board, max_bridges 5, max_tags 20}` (with tag links also `tag_links 2`, §16), then re-sends every retained event. |
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
`uart_rx_paused`, `mesh_init`, `mesh_step`, `mesh_start_errors`, `cfg_send_errors`,
`evq_dropped`; v2 adds its own (§15), tag links theirs (§16.7).

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

**`PROVISION {op_id, uuid, name?}`** (v1; v2 requires `static_oob` and
authenticates with it, §15.7)

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
| 2 | Model App Bind, `LAYOUT_SRV` — built by the gateway (company 0xFFFF, §1 step 5) | status 0 |
| 3 | Model App Bind, `MGMT_SRV` — likewise | status 0 |
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
| v2: identity key (X25519, 32 B) | settings `ctag/gw/id` | yes; a factory reset keeps it |
| v2: ownership record (176 B, CRC-32) | settings `ctag/gw/own` | yes; a factory reset stores UNOWNED |
| v2: generation floor (u32) | settings `ctag/gw/genf` | yes, and survives a corrupt record (§15.2) |
| Retained events, idempotency slots, delivery queue, transfers, results de-duplication, scan state; v2: the secure session, challenges, tunnels, DISCOVER windows | RAM | no — a new `boot_id` tells the companion (§1.2); a v2 session is opened again |

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
| `CTAG_GW_EVQ_DEPTH` | 24 | 16 | Bluetooth → loop event queue (v2: 172-byte events, room for a `TUNNEL_UP`; v1: 64) |
| `CTAG_GW_SECURE` | y | n | protocol v2 (§15) |
| `CTAG_SECURE_HEAP_SIZE` | 12288 | — | the Noise*/HACL* heap: one session plus the largest message sealed or opened (§15.11) |
| `CTAG_GW_TUNNELS` / `CTAG_GW_DISCOVERED_SLOTS` | 2 / 16 | — | tunnels open at once / `EVT_DISCOVERED` rate-limit entries |
| `CTAG_GW_RADIO` / `CTAG_GW_TAG_LINKS` / `CTAG_GW_RADIO_TUNNELS` / `CTAG_GW_LINK_INFLIGHT` | y / 2 / 8 / 4 | — | tags on the own radio: connections at once (`caps.tag_links`), own-radio tunnels, `DATA` fragments in flight per link (§16.7) |
| `BT_CENTRAL`, `BT_GATT_CLIENT`, `BT_MAX_CONN` | y, y, 2 | n, n, — | the tag links' central (§16.7) |
| `BT_ATT_TX_COUNT` / `BT_L2CAP_TX_BUF_COUNT` / `BT_BUF_ACL_TX_COUNT` | 12 / 12 / 12 | — | above both links' `DATA` fragments in flight (asserted) |
| `BT_MESH_ECDH_P256_HMAC_SHA256_AES_CCM`, `BT_MESH_OOB_AUTH_REQUIRED` | y, y | —, — | static OOB over HMAC-SHA256 (§15.7) |
| `MAIN_STACK_SIZE` (start-up + loop) | 12288 (v2) | 3072 | the v2 crypto runs on the loop: 9,916 B worst static chain (§15.11) |
| `SYSTEM_WORKQUEUE_STACK_SIZE` | 4096 | 2560 | |
| `BT_RX_STACK_SIZE` | 3300 | 2560 | |
| `BT_MESH_ADV_STACK_SIZE` | 4000 | 2048 | |
| `BT_MESH_SETTINGS_WORKQ_STACK_SIZE` | 1700 | 1400 | |
| `MBEDTLS_PSA_KEY_SLOT_COUNT` | 24 | 20 | persistent mesh keys incl. one device key per CDB node |
| `BT_MESH_ADV_BUF_COUNT` / `RX_SEG_MSG_COUNT` | 16 / 4 | 10 / 2 | |
| `BT_BUF_EVT_RX_COUNT` | 16 | 4 | nRF52832: no connections, few HCI event buffers; nRF52840: more than its ACL TX buffers (the tag links) |

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

`tools/build.py` (NCS v3.4.1, Zephyr controller, `--no-sysbuild`), 2026-09-28,
protocol v2 with two tag links on the nRF52840 targets:

| Target | Flash used / code partition | Headroom (min 15 %) | RAM used / RAM | RAM free | Stack check |
|---|---|---|---|---|---|
| `gateway-nrf52840dk` (v2, tag links) | 359,552 / 1,015,808 B (35.4 %) | 64.6 % | 142,144 / 262,144 B | 120,000 B | pass 16/16 |
| `gateway-nrf52840dongle` (v2, tag links) | 356,072 / 880,640 B (40.4 %) | 59.6 % | 142,016 / 262,144 B | 120,128 B | pass 16/16 |
| `gateway-nrf52dk` (v1) | 198,896 / 499,712 B (39.8 %) | 60.2 % | 60,912 / 65,536 B | **4,624 B** | pass 16/16 |
| `gateway-nrf52840dk` without tag links (same tree, same day) | 289,392 / 1,015,808 B (28.5 %) | 71.5 % | 127,348 / 262,144 B | 134,796 B | pass 16/16 |
| `gateway-nrf52840dk` as v1 (before v2, same day) | 229,912 / 1,015,808 B (22.6 %) | 77.4 % | 100,372 / 262,144 B | 161,772 B | pass 16/16 |
| debug (`debug/rtt.conf`) nRF52840 / nRF52832, v1, before the 104-byte event slots (+128 B RAM since) | 299,052 / 255,764 B | | 103,252 / 63,408 B | 158,892 / 2,128 B | |

Protocol v2 costs the nRF52840 **+59.2 KB of flash and +27.0 KB of RAM**
(§15.11 itemises both); the tag links on the own radio **+70.2 KB of flash
and +14.8 KB of RAM** more (§16.8: mostly the Bluetooth central and GATT
client). The nRF52832 build (v1) is unchanged by the tag links (it has
none): 132 B of flash and 64 B of RAM above the earlier figures (shared
code: the v2 CBOR keys, the core's 64-bit retained-event cursor; not
itemised further).

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
core is 45,112 B (a 20 KiB arena, the v2 state and the own radio's 2,528 B)
and USB adds ~5 KiB.

**nRF52832 verdict.** The application fits with 4.5 KiB spare only after
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
- `cremind tags tools gateway ports` lists candidate ports with their serial numbers;
  `cremind tags tools gateway info --url <port>` checks the link.

## 10. Debugging

- **Counters first**: `cremind tags tools gateway counters` (and `gateway info`,
  `gateway events` to watch events live). Serial problems show in
  `crc_errors`, `overruns`, `credit_violations`, `uart_rx_overflow`; mesh
  problems in `mesh_send_failures`, `mesh_busy`, `commit_resends`,
  `chunks_resent`, `stale_status`, `unexpected_mesh`; lost events in
  `events_dropped` (retained ring overflow) and `evq_dropped`; tag links in
  `attempts`, `connect_failed`, `sessions_fail`, `rate_limited`,
  `mesh_paused`, `radio_adv_dropped`, `gatt_failures` (§16.7).
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

**Protocol v2** (`ctag.gateway.core.v2`, 103 tests with the own radio's 24
of §16.9; `ctag.gateway.core.v2.noradio`, 80: v2 without tag links, as the
interop build; 2026-09-28 twister: v1 61, v2 103 and v2 without tag links 80
on both platforms, 488 test cases, all passing; the same sources with
`CONFIG_CTAG_GW_SECURE`, `lib/secure` linked, a 24 KiB secure heap shared by
the gateway and the test's Noise initiator). Every suite above runs again
*through a secure session* — the test host opens Noise IK as the pinned
worker, seals each request into `SECURE_DATA` and opens every answer and
event, asserting the outer `request_id` 0 — except two `gw_serial` tests whose
premise v2 changes (HELLO re-sending retained events, and a REBOOT answer held
back by a HELLO: in v2 a HELLO ends the session, and its events wait for the
next one, which `gw_v2` tests), plus the `gw_v2` suite (20): plaintext v1 requests need a
session, IDENTIFY (fields, fresh challenges, no authority when unowned),
SECURE_OPEN failures (`INVALID`, `AUTH_FAILED`, `NO_RESOURCES` with the heap
recovering), RNG failures (`INTERNAL` for IDENTIFY, SECURE_OPEN, STATUS), a
decrypt failure ending the session with a plaintext `AUTH_REQUIRED`, HELLO
dropping the session, answers of an old session never reaching a new one,
inner RESPONSE/EVENT flags ignored with the credit returned, the access table
of an unowned gateway (and `UNSUPPORTED` for bridge messages and a sealed
SECURE_OPEN), CLAIM fields and grant rules (`STORAGE_ERROR`,
`STALE_GENERATION`, single-use challenges), STATUS fields (the owner only to
the pinned controller), another controller recovering and receiving the
retained events, RELEASE wiping and rebooting, the boot rule, PROVISION's
static OOB (required; `SECURITY_CONFIG` for no static OOB offered, a failed
exchange and the deadline after the capabilities; `TIMEOUT` before them),
DISCOVER (rate limit, window, idempotency), tunnels (round trip with
fragmentation through the lane, reassembly, gaps, busy, too large, close,
idle timeout, failed send), CAPS2 in the inventory, and without tag links
`GATEWAY_ADDR` refused (`UNSUPPORTED` / `NOT_FOUND`, no `tag_links`). Between
tests the harness frees both Noise objects and asserts the secure heap is
empty. With tag links, the `gw_radio` suite (§16.9) runs too, and the mocked
backend fails any mesh send, configuration step or provisioning while the
mesh is suspended, in every suite.

Run: `west twister -T /work/apps/gateway/tests/core -p native_sim -p native_sim/native/64 -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-gw-core`
(after `apt-get install -y make`).

**Interop test** (`apps/gateway/tests/interop`, [README](../apps/gateway/tests/interop/README.md)):
the gateway core, loop and UART glue on a native PTY UART with a simulated
mesh, driven by the companion's real `GatewayClient`. It has no Bluetooth
and builds without `CONFIG_CTAG_GW_RADIO` (the option's default), v1 and
v2 alike. 2026-09-28: **8/8 scenarios pass** —

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

**Interop, protocol v2** (the same app with `v2.conf`, driven by
`interop_v2.py` with the companion's reference `SecureChannel`, grants and
identity; the simulated bridge `0x0002` runs `lib/secure` as a BRIDGE behind
tunnels). 2026-09-28: **9/9 scenarios pass** (and v1 8/8 in the same run) —

| Scenario | Result |
|---|---|
| plaintext layer: HELLO, IDENTIFY, `AUTH_REQUIRED`, SECURE_OPEN with garbage → `AUTH_FAILED` | pass |
| SECURE_OPEN + CLAIM with a signed grant | pass: gen 1, answered in ~21 ms on the host; a replayed grant `GRANT_INVALID`; STATUS with the owner for the pinned worker |
| access table: another controller, RECOVER | pass: no owner in its STATUS, `NOT_OWNER`, RECOVER gen 2, then pinned; the first worker refused |
| sealed answers and events | pass: DELIVER_LAYOUT → sealed `EVT_RESULT OK` with the digest, EVENT_ACK |
| DISCOVER → EVT_DISCOVERED | pass: one per bridge, duplicates rate-limited (counters), 121 s `INVALID` |
| tunnel to the bridge's endpoint: Noise IK + PAIR | pass: `ident2` in `EVT_TUNNEL OPEN`, Noise IK through the tunnel (`Link.TUNNEL`), PAIR with the setup proof, `proof_d` checks, MAINT_AUTH through the tunnel `NOT_OWNER`, a tunnel to 0x0003 closed `BUSY` |
| PROVISION with static OOB | pass: none → `INVALID`, wrong → `SECURITY_CONFIG` addr 0, derived → `0x0004` |
| decrypt failure ends the session | pass |
| RELEASE → UNOWNED, network wiped | pass: reboot, UNOWNED at gen 3, CLAIM gen 4 on an empty network |

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
| v2 `PROVISION` without `static_oob` | `ACCEPTED`, then `EVT_PROVISIONED SECURITY_CONFIG` | `INVALID` "missing field" at once | the field is required of a v2 request (spec: "required by v2 gateways"); nothing is started |
| v2 failure after the capabilities (radio loss mid-exchange) | n/a | `SECURITY_CONFIG` | Zephyr reports no reason for a closed link (§15.7) |

## 13. Hardware test plan

Equipment: the gateway DK, five bridge DKs (or bridge boards) with the bridge
firmware, at least two tags, the companion on a PC, a J-Link for RTT. Record
results in the board's qualification report.

1. **Provisioning 5 bridges.** Flash the gateway with a chip erase (new
   network). `cremind tags tools mesh scan -d 10` lists five UUIDs with RSSI; for each
   `cremind tags tools mesh provision <uuid> --name bN` (provisions and configures).
   Expect `EVT_PROVISIONED` with consecutive addresses `0x0002…`, every
   `EVT_NODE_CONFIGURED OK`, `cremind tags tools mesh nodes` listing five configured
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
4. **Reboot recovery.** (a) `cremind tags tools gateway reboot` during a transfer:
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

- First hardware run (2026-10-06, nRF52840 Dongle with Nordic's bootloader,
  a Hema 52811 tag, Cremind's integrated worker): USB CDC ACM enumeration, the
  serial server with the v2 secure session, the mesh start-up with
  self-configuration (after two fixes: §1 step 5's vendor binds, and the
  static CCC UUID of `central.c`), and the own radio — adverts, connections,
  the SESSION GATT setup, a v1 session's `CLEAR` and a layout displayed — all
  ran, and the network and ownership survived reboots and DFU updates. Still
  verified only by building: PB-ADV provisioning, the configuration client
  sequence of a bridge, `send_cb` behaviour with 16 segments, bridge nodes in
  the CDB, the PAIR tunnel and the nRF UARTE path. The mesh suspend for a
  connection took up to 763 ms.
- Stack sizes are unmeasured on both boards (§10, §13 step 5).
- The nRF52832 gateway's RAM margin (4.5 KiB) and its reduced queue need the
  resource qualification before it is used.
- USB VID/PID `1209:0002` is a pid.codes test pair; `MESH_COMPANY_ID` 0xFFFF is
  the SIG test value (spec.yaml): both need assigned values before production.
- No watchdog is enabled (optional per the plan); a hang would need a power
  cycle. `CONFIG_WATCHDOG` with a feed from the gateway loop is a small addition
  once the loop's worst-case pass time is measured.
- v1 (the nRF52832): PB-ADV without OOB authentication (protocol §2): provision
  in a controlled environment. v2 authenticates with static OOB (§15.7).
- v2: see §15.13; the tag links on the own radio: §16.10.

---

## 15. Protocol v2 (`CONFIG_CTAG_GW_SECURE`)

The normative description is [connect-setup.md](connect-setup.md) (§2
identity, §3 keys and sessions, §4 ownership, §5 messages, §6 mesh); the
reference is the companion's `app.tags.runtime.secure` package (`device.py` for
the device rules) and `protocol/fixtures/v2_secure.json`. The gateway runs
the role-independent secure endpoint of `lib/secure`
([firmware-libs.md](firmware-libs.md#ctag_secure--protocol-v2-secure-endpoint-connect-setupmd-25))
behind its serial server: `src/core/gw_secure.c` (plaintext layer, access
table, the ownership messages, sealing), `src/core/gw_tunnel.c` (DISCOVER,
tunnels), `src/reset.c` (the button), and v2 parts of `main.c`, `mesh.c`,
`store.c`, `gw_serial.c`, `gw_nodes.c`. Everything in `src/core` stays
Bluetooth-free and runs in the native_sim tests.

### 15.1 Start-up

Before anything else `main()` checks the factory-reset button (§15.10). Then
the v1 start-up (§1), and `secure_boot()`:

1. The identity key from settings `ctag/gw/id`; on first boot 32 bytes of
   `sys_csrand_get()` (retried until the entropy driver is ready — never a
   weaker source), stored. Its X25519 public key is `ik`; `device_id` and
   `short_id` follow (connect-setup.md §2.1).
2. The ownership record (`ctag/gw/own`) and the generation floor
   (`ctag/gw/genf`) are loaded into the endpoint (§15.2). The RAM copies of
   the key and the record are wiped.
3. A factory reset stores an UNOWNED record at the kept generation, wipes the
   network (below) and reboots.
4. **An unowned gateway never keeps a network**: if the record is not OWNED
   and the CDB has bridges or the assignment table entries (a v1 network at
   the first v2 boot, or a RELEASE interrupted by a power loss), the network
   is wiped and the gateway reboots into a new, empty one.

*Wiping the network* (`gw_mesh_wipe`, also RELEASE): every bridge's stored
name, `bt_mesh_cdb_clear()`, `bt_mesh_reset()` of the gateway node, and the
assignment table. The next boot creates a new network key and app key.

### 15.2 Ownership record and generation floor

`struct ctag_owner_record`, stored as 176 bytes: `version` (1), `state`
(UNOWNED 0, OWNED 1, RELEASED 2), `flags`, a zero byte, `gen` u32 LE, the
authority's Ed25519 key (32), `owner` (16), the pinned controller (32), and
the fields bridges and tags use (`op_key`, override and pending secrets,
pending controller), then a CRC-32 over the first 172 bytes. A record with
another length, version, CRC, state, an unknown flag or a non-zero reserved
byte is corrupt.

A change is **stored before it is answered**: the endpoint calls
`store_owner(record, gen)`, which first raises the floor (`ctag/gw/genf`,
only when `gen` is above it), then writes the record. A failed write answers
`STORAGE_ERROR` and changes nothing. At boot a corrupt or missing record
means UNOWNED at the floor's generation; a good one keeps its content with
`gen = max(gen, floor)` — a lost or rolled-back record can never rewind the
generation a grant is checked against.

### 15.3 Plaintext layer

| Frame | Answer |
|---|---|
| `HELLO` | as in v1 (`proto` 1); it also ends the secure session and drops its unsent answers |
| `PING` | answered in plaintext |
| `IDENTIFY {}` | `{status, proto 2, role GATEWAY, device_id, ik, fw, build, board, owner_state, gen, challenge, authority_id (OWNED only)}` — a fresh 16-byte challenge each time, valid until the next `IDENTIFY` or `STATUS`, one grant check or a reboot |
| `SECURE_OPEN {data: Noise message 1, 96 B}` | the Noise IK responder (prologue `"cremind-tag/v2" \| link 1 (serial) \| device_id`) → `{status OK, data: message 2, 48 B}`; it replaces any session. `AUTH_FAILED` when message 1 does not authenticate, `NO_RESOURCES` when the secure heap is exhausted, `INTERNAL` when the RNG fails (`secure_failures`) |
| `SECURE_DATA {data}` | one sealed secure message (§15.4) |
| anything else | `AUTH_REQUIRED` (`auth_required`) |

### 15.4 The session

- **Framing.** A secure message is `type u8 | flags u8 | request_id u16le |
  CBOR` (the serial catalogue), sealed with the session. It travels as a
  `SECURE_DATA` frame with `request_id` 0 and `flags` 0 whose payload is
  `{41 (data): ciphertext}`, in both directions; the **inner header is
  authoritative**. Credits count the outer frames: every sealed answer
  returns its request's credit, as in v1.
- **Failures.** `SECURE_DATA` without a session, or one that does not
  decrypt, is answered in plaintext: a `SECURE_DATA` frame flagged RESPONSE,
  `request_id` 0, `{status: AUTH_REQUIRED}`. A message that fails to decrypt
  (tampered, replayed, out of order) also **ends the session**
  (`decrypt_failures`). An inner header shorter than 4 bytes or flagged
  RESPONSE/EVENT is dropped (`unexpected_frames`; its credit returns).
- **Answers belong to their session.** Each answer records the session it
  was asked in; one whose session ended meanwhile (HELLO, a new
  SECURE_OPEN, a decrypt failure) is dropped unsent, never sealed into
  another session.
- **Events only for the owner.** Retained and best-effort events go out
  only into a *privileged* session — the gateway is OWNED and the session's
  controller is the pinned one — and are sealed into it. Retained events
  wait (up to the ring's 16) and are re-sent when such a session opens
  (`SECURE_OPEN`) or a session becomes privileged (`CLAIM`, `RECOVER`).
- Inside a session `HELLO`, `IDENTIFY`, `SECURE_OPEN`, `SECURE_DATA` and the
  bridge/tag messages `PAIR`, `REKEY`, `MAINT_AUTH`, `RECOMMISSION`,
  `FACTORY_SETUP` answer `UNSUPPORTED`.

### 15.5 Access table (connect-setup.md §4.2)

| Gateway | Session's controller | Served |
|---|---|---|
| UNOWNED | any | `INFO`, `PING`, `STATUS`, `CLAIM` |
| OWNED | the pinned controller | everything (the v1 catalogue, `STATUS`, `RECOVER`, `RELEASE`, `DISCOVER`, `TUNNEL_*`) |
| OWNED | another controller | `INFO`, `PING`, `STATUS`, `RECOVER` |

Anything else is `NOT_OWNER` (`not_owner`), decided before the message's own
rules: a refused grant message leaves the challenge unused.

### 15.6 STATUS, CLAIM, RECOVER, RELEASE

The endpoint (`ctag_secure_handle`) implements `device.py`:

- `STATUS {}` → `{status, owner_state, gen, challenge (fresh), controller_match,
  authority_id (OWNED), owner (OWNED, and only to the pinned controller)}`.
- `CLAIM {grant, sig}` (op CLAIM) → OWNED at `gen_to` with the grant's
  authority, owner and controller; `{gen}`.
- `RECOVER {grant, sig}` (OWNED only, op RECOVER) → the controller becomes the
  grant's, `gen_to`; authority and owner stay; `{gen}`.
- `RELEASE {grant, sig}` (op RELEASE) → UNOWNED at `gen_to`, `{gen, data: b""}`;
  then the network is wiped (§15.1), the tunnels, retained events and
  idempotency slots are forgotten and the gateway **reboots once the answer
  is out** (as REBOOT, §2).

A missing `grant` or `sig` is `INVALID` and keeps the challenge; otherwise
the challenge is used up whatever the outcome. The grant rules run in the
order of `check_grant`: canonical grant (`GRANT_INVALID`), 64-byte
signature, device id, role and op, the challenge (constant time), `gen_from
= gen` (`STALE_GENERATION`), the grant's controller = the session's, the
Ed25519 signature over `"cremind-tag/v2/grant" | grant`, then ownership
(OWNED: the op must be one an owner may use and authority and owner must be
the pinned ones, else `NOT_OWNER`; UNOWNED: only CLAIM). Counters `claims`,
`recovers`, `releases`.

### 15.7 PROVISION with static OOB (connect-setup.md §3.4, §6)

`PROVISION {op_id, uuid, name?, static_oob}`: `static_oob` (32 bytes) is
required (`INVALID` "missing field" otherwise, not remembered). It is handed
to `gw_mesh_provision()`, which keeps it for the link and wipes it at link
close. In the provisioner's `capabilities` callback:

- the device offers static OOB and the HMAC-SHA256 algorithm →
  `bt_mesh_auth_method_set_static(value, 32)`; the core is told the
  authentication began (`GW_EVT_PROV_AUTH`);
- anything else → `GW_EVT_PROV_SECURITY` and
  `bt_mesh_auth_method_set_input(ENTER_NUMBER, 1)`, a method this provisioner
  can never complete (it has no input callback), so the stack fails the link.
  There is no fallback to unauthenticated provisioning.

| Outcome | `EVT_PROVISIONED` |
|---|---|
| `node_added` | `OK` with the address |
| the link never opened | `NOT_FOUND` |
| the link opened, no capabilities arrived before it closed or the 90 s guard | `TIMEOUT` (transient) |
| the capabilities arrived (static OOB refused, or its exchange began) and the node was not added | **`SECURITY_CONFIG`**, addr 0 (final) |

Zephyr reports no reason for a closed provisioning link; a wrong static OOB
shows only as a link that closes without `node_added` (the device's
Provisioning Failed "confirmation failed", or the provisioner's own check),
so every failure after the capabilities counts as an authentication failure:
a mistyped setup code must end the worker's retries, which `TIMEOUT` would
not. `prov_security` counts `SECURITY_CONFIG` outcomes. Kconfig:
`CONFIG_BT_MESH_ECDH_P256_HMAC_SHA256_AES_CCM=y` and
`CONFIG_BT_MESH_OOB_AUTH_REQUIRED=y` (names verified in NCS v3.4.1,
[firmware-notes.md](firmware-notes.md) §3; the latter matters only for the
provisionee role, which the gateway never uses over PB-ADV).

### 15.8 DISCOVER

`DISCOVER {op_id, bridge (0 = every configured bridge), duration_s ≤ 120,
tag_id (0 = any)}` → `ACCEPTED` (idempotent by `op_id`; > 120 s is
`INVALID`): the mesh `DISCOVER` (unsegmented) goes to each bridge, which
opens a window of `duration_s` + 2 s for its answers. Each `DISCOVERED` inside
its bridge's window becomes a best-effort `EVT_DISCOVERED {bridge, tag_id,
rssi, flags}`, at most one per `(bridge, tag)` per 5 s (16 entries;
`discovered`, `discovered_limited`). Outside a window it is
`unexpected_mesh`. Candidates are never kept or listed. With tag links,
`bridge` 0 also opens a window on the gateway's own radio and
`GATEWAY_ADDR` opens only that one (§16.5); without, `GATEWAY_ADDR` is
`NOT_FOUND`.

### 15.9 Tunnels

| Request | Behaviour |
|---|---|
| `TUNNEL_OPEN {op_id, bridge, tag_id (0 = the bridge's own endpoint), duration_s 1–255}` | `{status OK, tunnel}` with a fresh non-zero id (idempotent by `op_id`: a repeat answers the same id with `detail DUPLICATE`); the mesh `TUNNEL_OPEN {tunnel, tag_id, timeout_s}`. One tunnel per bridge and `CTAG_GW_TUNNELS` (2) at once: `BUSY` beyond. `NOT_FOUND` for an unknown or unconfigured bridge |
| `TUNNEL_SEND {tunnel, data}` | one message of 1–400 bytes (`INVALID` empty, `TOO_LARGE` beyond, `NOT_FOUND` unknown tunnel, `BUSY` while the previous message is still being sent) → `OK`; fragmented into `TUNNEL_DATA` of ≤ 150 bytes (`seq` from 0, START, END), each an acknowledged segmented send through the lane (§4). A fragment that fails (the lane's retries exhausted) closes the tunnel: mesh `TUNNEL_CLOSE TIMEOUT` and `EVT_TUNNEL CLOSED TIMEOUT` |
| `TUNNEL_CLOSE {tunnel}` | `OK`; mesh `TUNNEL_CLOSE OK`; no event |

Upward, `TUNNEL_UP` fragments are reassembled in order; the endpoint's first
message becomes `EVT_TUNNEL {tunnel, bridge, tag_id, state OPEN, data}` (the
bridge's or tag's `ident2`), every later one `state DATA`. A gap drops the
message (`tunnel_gaps`; the Noise session above fails and the worker opens a
new one). The CLOSE flag (data = status) is `EVT_TUNNEL {state CLOSED,
status}`. A tunnel without traffic for its `duration_s` + 5 s is closed with
`TIMEOUT` both ways. `EVT_TUNNEL` is best effort, sealed like every event.
The gateway never looks into tunnel messages: the worker's Noise session
runs end to end with the bridge's or tag's endpoint. `TUNNEL_OPEN` takes an
optional `mode` (absent = `PAIR`); `SESSION` through a bridge is `INVALID`.
`bridge` `GATEWAY_ADDR` opens a tunnel on the gateway's own radio (§16.2;
`UNSUPPORTED` without tag links), in the same id space.

### 15.10 Factory reset (connect-setup.md §4.3)

The board button (devicetree `sw0`: Button 1 on the DK, SW1 on the Dongle)
held through power-up: the LED (`led0`) blinks fast for 10 s, then stays on
and the reset runs (§15.1 step 3) — ownership, network and assignments go,
the identity key and the generation stay, and the gateway reboots. Releasing
earlier boots normally. There is no software or radio path to it.

### 15.11 Resources

**Flash** (nRF52840 DK, v2 − v1 = +59.2 KB): HACL* 35.9 KB (Curve25519_51
13,962 B, Ed25519 12,800, ChaCha20-Poly1305 4,982, SHA-2 3,692, HMAC 424),
Noise* IK 7,535, `lib/secure` 7,987 (endpoint 2,642, noise 1,648, grant 797,
keys 759, message 740, record 576, glue 566, crypto 259), `gw_secure.c`
2,530, `gw_tunnel.c` 2,301, `reset.c` 278, the rest in the serial server,
nodes, mesh and settings glue. SHA-1 and BLAKE2 (vendored for `Hacl_HMAC.c`)
are dropped by the linker.

**RAM** (+27.0 KB): the secure heap 12,288 B; the main stack +8,192 B
(12 KiB, below); the event queue +2,592 B (24 events of 172 bytes: a
`TUNNEL_UP` fits); the core +3,408 B (endpoint and keys, two tunnels of
2 × 400-byte buffers, the `EVT_DISCOVERED` table, answer slots with a secure
answer each); the rest in `lib/secure` state and the settings glue.

**Secure heap** (`CONFIG_CTAG_SECURE_HEAP_SIZE`, a `sys_heap`): measured with
the host allocator (8-byte headers, the same call sequence): a device object
248 B, a peer 112 B, a responder handshake peaks +1,096 B and leaves a
session of 776 B; sealing 100 B takes +264 B transiently, 2,522 B +5,112 B,
4,061 B +8,184 B; opening 4,077 B peaks +8,168 B and holds 4,072 B until
the message is handled. The largest message is a `DELIVER_LAYOUT` of 4,000
bytes inside `SECURE_DATA` (4,083 of the 4,084 payload bytes a frame
allows), so 12 KiB holds the session and the largest open. Every Noise*
call runs under the allocation guard: an exhausted heap or an RNG failure
fails the call, wipes and re-initialises the heap and ends the session
(`NO_RESOURCES` / `AUTH_REQUIRED` to the host, `secure_heap_failures`); it
never reaches the rest of the firmware. `secure_heap_peak` shows the high
water.

**Stacks.** The v2 crypto runs on the gateway loop, i.e. the main thread
(`SECURE_OPEN`: two X25519 plus Noise; `CLAIM`/`RECOVER`/`RELEASE`: Ed25519
verification; every sealed frame: ChaCha20-Poly1305). Static analysis of the
nRF52840 DK build (GCC 14.3 `-Os`, `-fstack-usage -fcallgraph-info=su`, the
worst path through the call graph, 4,009 functions):

| From | Worst chain | Largest frames on it |
|---|---:|---|
| `main` (start-up, then the loop) | **9,916 B** | `Hacl_Ed25519_verify` 6,432 (two precomputed point tables), `Field51_fmul` 848, point decompression 632, `do_pair` 400, `gw_run` 328, `gw_v2_outer` 280 |
| `ctag_secure_handle` (a grant check) | 9,188 B | the Ed25519 chain above |
| `ctag_secure_open` (SECURE_OPEN) | 3,324 B | `Field51_fmul2` 1,520, Noise* `state_handshake_read` 608, `scalarmult` 584 |
| `gw_core_poll` → sealing an answer or event | 3,252 B | the X25519 chain (static worst case of `Noise_IK_session_write`) |
| `gw_core_secure_init` (boot: `ik` from the key) | 2,468 B | X25519 |

The analysis cannot follow function pointers (the backend: settings writes,
the RNG, the UART) — those run after the crypto returns, not inside it — and
counts `do_pair`, which the gateway never reaches (`do_claim`'s frame is
smaller). `MAIN_STACK_SIZE` is therefore **12,288 B** on the nRF52840 targets
(4,096 in v1): ~2.3 KiB above the worst chain for exception frames and the
untracked calls. A thread-analyzer measurement on hardware (§10) through
SECURE_OPEN, CLAIM and sustained sealed traffic is the confirmation still to
do. (Any device that verifies grants with HACL*'s Ed25519 needs this ~9.5 KB
chain; see firmware-libs.md `ctag_secure`.)

### 15.12 The nRF52832 gateway stays on protocol v1

The nRF52832 build has 4.5 KiB of RAM free (§8). v2 needs, at the least, a
secure heap that opens a 4 KiB `SECURE_DATA` (~9.3 KiB: a session plus the
unseal peak), a main stack of ~10 KiB for the Ed25519 chain (§15.11, v1:
3 KiB), larger events and the tunnel buffers. A fit build with the smallest
plausible settings — 10 KiB heap, one tunnel, 8 `EVT_DISCOVERED` slots and
only a 5 KiB main stack — fails to link: **`region 'RAM' overflowed by
12,272 bytes`** (2026-09-28); with the stack the analysis requires it would
be ~17 KB short of the 64 KiB. `gateway-nrf52dk` therefore builds without `CONFIG_CTAG_GW_SECURE`
(`socs/nrf52832.conf`), serves protocol v1 only and is for development; a
v2 deployment uses an nRF52840 gateway (DK or Dongle).

### 15.13 Open items (v2)

- Nothing of v2 has run on hardware: the static OOB exchange with a real v2
  bridge, the capabilities callback, `bt_mesh_cdb_clear()` + `bt_mesh_reset()`
  followed by a reboot into a new network, settings writes of the record and
  the floor, the button and LED, and the time a handshake and a grant check
  take on the Cortex-M4 (X25519 with the portable 128-bit arithmetic,
  Ed25519 verification) are verified only by building and on native_sim.
- The main stack size comes from static analysis; measure it with the
  thread analyzer through SECURE_OPEN, CLAIM and sustained sealed traffic.
- Provisioning reports `SECURITY_CONFIG` for any failure after the
  capabilities, including a radio loss in the middle of the exchange, which
  the worker will not retry: re-running `PROVISION` is the recovery.
- The bridge and tag firmware sides (their endpoints, `CAPS2_STATUS`,
  `DISCOVERED`, `TUNNEL_UP`) are other applications' work; the gateway's
  side is tested against the native_sim network of `tests/interop`.

---

## 16. Tags on the gateway's own radio (`CONFIG_CTAG_GW_RADIO`)

The normative description is [protocol.md §11](protocol.md#11-tags-on-the-gateways-own-radio)
(with [connect-setup.md](connect-setup.md) §5.2, §6 and §8.3). The nRF52840
gateways (`socs/nrf52840.conf`) also connect to the tags in their range,
as a thin BLE relay: the companion runs the tag session (protocol.md
§5.4–§5.6 and every §10 rule) and renders (§4.4); the gateway connects to
the tag, sets GATT up and carries whole messages between the serial tunnel
messages and the tag's characteristics, fragmented and reassembled per §5.3.
It renders nothing, holds no tag key and never looks into a message. HELLO
and INFO report `caps.tag_links` = `CONFIG_CTAG_GW_TAG_LINKS` (2); the
nRF52832 gateway (v1) has none.

### 16.1 Architecture

```
 Bluetooth contexts (central.c)                                        the gateway loop
 scan listener: ADV_IND with the 5.1 data, ─── GW_EVT_TAG_ADV ────────▶ gw_core_tag_adv()
   only while the core listens, dropped once gw_evq is half full
 connected / disconnected ──────────────────── GW_EVT_CONN / _DISCONN ─┐ central.c: bt_conn refs,
 discovery / subscribe / read callbacks ────── GW_EVT_GATT ────────────┤ the GATT setup's steps ─▶
                                                                       │ gw_core_link_connected/
                                                                       │ _disconnected/_ready()
 notifications, indications ────────────────── GW_EVT_LINK_VALUE ─────▶ gw_core_link_rx()
 write response, sent callback ─────────────── GW_EVT_LINK_WRITTEN ───▶ gw_core_link_written()

 src/core/gw_radio.c: own-radio tunnels, per-link transmit queues and 5.3 state, the
 sched_ops of lib/sched (5.2) ── struct gw_backend ──▶ central.c: radio_listen,
 radio_suspend / radio_resume (bt_mesh_suspend/resume), link_connect (bt_conn_le_create),
 link_disconnect (bt_conn_disconnect), link_setup, link_write (bt_gatt_*)
```

- The core stays Bluetooth-free (§1): `gw_radio.c` owns the tunnels and the
  links and drives the connection scheduler (`lib/sched`,
  [firmware-libs.md](firmware-libs.md#ctag_sched--tag-connection-scheduler-52)),
  whose operations it implements over the core's own state and the backend.
  The native_sim tests run it with a mocked backend (§16.9).
- `central.c` runs the Bluetooth procedures the core asks for. Every
  callback only posts a `gw_evt` (§1); the discovery and read callbacks also
  collect handles and value bytes into their link's record, which the loop
  reads only after that procedure's completion event (the queue orders
  them). The setup's steps — the tag service, the characteristics of the
  mode, their CCC descriptors, the subscriptions, the read — run on the loop.
- Tags are heard through the mesh's own scan (`bt_le_scan_cb_register()`,
  beside the unprovisioned-beacon listener of §3): the gateway never starts
  or stops scanning. The listener takes connectable `ADV_IND`s with the §5.1
  manufacturer data (company `MESH_COMPANY_ID`, `ver` 1 or 2) and posts them
  only while the core listens (backend `radio_listen`: an own-radio tunnel
  waits for its tag, or a DISCOVER window is open), and not once the event
  queue is half full (`radio_adv_dropped`: a tag advertises again 250 ms
  later), so advertisements never crowd out mesh or link events.

### 16.2 Tunnels

`TUNNEL_OPEN {op_id, bridge GATEWAY_ADDR, tag_id, duration_s, mode?}`
(protocol.md §11.3; `mode` absent = `PAIR`), checked in this order:

| Condition | Answer |
|---|---|
| no tag links (`CONFIG_CTAG_GW_RADIO` off) | `UNSUPPORTED` |
| `tag_id` 0, `duration_s` 0 or above 255, a `mode` other than `PAIR` (0) and `SESSION` (1) | `INVALID` |
| a tunnel to that tag exists, or all `CONFIG_CTAG_GW_RADIO_TUNNELS` (8) are taken | `BUSY` (not remembered) |
| otherwise | `{status OK, tunnel}` at once; the tunnel waits for its tag |

`mode` `SESSION` (or any mode but `PAIR`) through a bridge is `INVALID`,
before the bridge is looked up. Own-radio tunnels take their ids from the
mesh tunnels' counter and never share one; `TUNNEL_SEND` and
`TUNNEL_CLOSE` find the id in whichever table holds it, and a repeated
`op_id` answers the same id with `detail DUPLICATE` (§15.9). `ASSIGN_TAG`,
`UNASSIGN_TAG`, `DELIVER_LAYOUT`, `TAG_COMMAND` and the node requests
naming `GATEWAY_ADDR` answer `NOT_FOUND`: the gateway is not in its node
table (protocol.md §11.1).

```
WAITING ──its tag's advertisement (ver 1 or 2)──▶ the scheduler (§16.4): suspend, connect, resume
   │  not connected within duration_s ─▶ EVT_TUNNEL CLOSED TIMEOUT (an attempt already
   │  running at the deadline decides first: at most 3.5 s later)
   ▼ connected, the mesh resumed
SETUP  link_setup(mode): the tag service; CAPS, CTRL, DATA, STATUS (SESSION) or IDENT, PAIR;
   │   their CCCs; subscriptions (CTRL indications + STATUS notifications, or PAIR indications);
   │   the read of CAPS (18 B) or IDENT (ident2, 91 B, a long read)
   │  no service, characteristic or CCC of the mode ─▶ CLOSED UNSUPPORTED
   │  a GATT step failed, nothing read, a value above 154 B ─▶ CLOSED INVALID
   │  not done within 5 s of the connection ─▶ CLOSED TIMEOUT
   ▼
OPEN   EVT_TUNNEL {OPEN, data = the value read, rssi = the advertisement the connection was made on}
   │  TUNNEL_CLOSE ─▶ the link goes down, no event
   │  the link dropped ─▶ CLOSED DISCONNECTED
   │  a 5.3 violation, or a value longer than the event holds ─▶ CLOSED INVALID
   │  a write the tag refused (an ATT error) ─▶ CLOSED INVALID; any other failed write ─▶ DISCONNECTED
   └  duration_s + 5 s without a message either way ─▶ CLOSED TIMEOUT
```

Every close but `TUNNEL_CLOSE` and RELEASE is `EVT_TUNNEL {CLOSED, status}`
(best effort, like every `EVT_TUNNEL`) and hands the link back to the
scheduler with that status: a failure starts the tag's back-off (protocol.md
§5.2 step 2), the host's own close does not. A tunnel closed while its tag
is being connected leaves an unwanted connection: it is taken down as soon
as it is up, without GATT.

### 16.3 Messages

- **Sending.** `TUNNEL_SEND {tunnel, data}` carries one message. `SESSION`:
  its first byte picks the characteristic — `0x01`–`0x0F` `CTRL` (at most
  `TAG_CTRL_MSG_MAX`, 64), `0x10`–`0x2F` `DATA` (at most
  `TAG_RECORD_WIRE_MAX`, 205); an empty message or another type is
  `INVALID`, a longer one `TOO_LARGE`. `PAIR`: every message to `PAIR` (at
  most `PAIR_MSG_MAX`, 320). Then `BUSY` before OPEN or with two messages
  queued (a link's queue holds two); else the message is queued and answered
  `OK`.
- Messages leave in order and **one at a time**, cut by `ctag_frag_next()`
  into ATT values of `ATT_VALUE_MAX` (20) bytes with the gateway's own `SEQ`
  per characteristic, from 0 at the connection and continuous across
  messages. `CTRL` and `PAIR` fragments are written with response, one
  outstanding; `DATA` fragments without response, `CONFIG_CTAG_GW_LINK_INFLIGHT`
  (4) outstanding. A message is done when its last fragment completed (the
  write response or the sent callback); the next one starts then. A host out
  of buffers (`-EAGAIN`, `-ENOMEM`, `-ENOBUFS`) is offered the same fragment,
  with the same `SEQ`, again after 100 ms. Writes are paced by the ATT
  buffers, never by the serial link.
- **Receiving.** `SESSION` reassembles `CTRL` indications (≤ 64 bytes) and
  `STATUS` notifications (≤ 205) separately, `PAIR` its indications (≤ 320);
  each complete message is `EVT_TUNNEL {DATA, data}`. Values before OPEN, or
  on a characteristic the mode does not use, are dropped (`radio_stray`).
- Every message either way (accepted, written, received) restarts the idle
  deadline.

### 16.4 The mesh around a connection (protocol.md §5.2)

The gateway connects under the bridge's rules with the bridge's code:
`lib/sched` ([bridge-firmware.md §4](bridge-firmware.md#4-tag-connection-scheduler-protocolmd-52))
with these operations:

| `sched_ops` | The gateway |
|---|---|
| `has_work(tag)` | an own-radio tunnel waits for the tag |
| `node_ready` | no provisioning, configuration or removal runs (and no reboot is pending) |
| `mesh_busy` | the segmented lane (§4) has a send active or queued, or the unsegmented queue is not empty: they finish first (polled every 20 ms, at most 500 ms, else the attempt waits for a later advertisement, `deferred`) |
| `link_idle(link)` | its tunnel is OPEN with nothing queued or in flight: a further connection starts only then |
| `mesh_suspend` / `mesh_resume` | `bt_mesh_suspend()` / `bt_mesh_resume()` (`-EALREADY` counts as done) |
| `conn_create` | `bt_conn_le_create()` to the advertiser: scan interval = window = 30 ms, connection interval 30–50 ms, latency 0, supervision timeout 4 s, create timeout `BRIDGE_CONN_ATTEMPT_MS` (1 s) |
| `conn_cancel`, `disconnect` | `bt_conn_disconnect()` |
| `session_start` | binds the waiting tunnel to the link and starts its GATT setup |
| `session_abort` | `EVT_TUNNEL CLOSED` with the status (`DISCONNECTED` when the link dropped) |
| `timer`, `now`, `reboot` | a core deadline run by `gw_core_poll()`, the core's clock, the backend's reboot (protocol.md §5.2 step 7) |

So: one attempt at a time, each in its own suspend window; at most 6
attempts per rolling minute; a 15 s per-tag back-off after a failure, with
one quick retry inside the tag's advertising window after `CONNECT_FAILED`;
at most `CTAG_GW_TAG_LINKS` (2) connections. The advertisement that started
an attempt gives the tunnel's `rssi`.

**While the mesh is suspended nothing reaches the mesh stack**
(`gw_mesh_paused()`): the lane and the unsegmented queue wait as for a
buffer shortage (offered again every 100 ms, counted in `mesh_paused`), a
configuration step waits the same way, and `PROVISION` answers
`PROVISIONING_ACTIVE` (transient, never remembered: the companion retries).
The resume releases them at once. What bridges send to the gateway
meanwhile is lost or re-sent by them (protocol.md §11.5: about one second
per attempt).

### 16.5 DISCOVER

`DISCOVER` with `bridge` 0 (every configured bridge and the own radio) or
`GATEWAY_ADDR` (the own radio only) opens a window of `duration_s` + 2 s on
the own radio, as a bridge's (§15.8); `duration_s` 0 closes it. Every `ver` 2
advertisement with the `SETUP` flag (and the request's `tag_id`, unless 0)
heard in the window is `EVT_DISCOVERED {bridge GATEWAY_ADDR, tag_id, rssi,
flags}`, rate-limited in the same `(bridge, tag)` table as the bridges'
candidates (5 s, `discovered_limited`).

### 16.6 RELEASE

RELEASE (§15.6) closes every own-radio tunnel and takes their links down,
without events, before the reboot.

### 16.7 Configuration and counters

| Option | nRF52840 | Meaning |
|---|---:|---|
| `CTAG_GW_RADIO` | y | tag links (depends on `CTAG_GW_SECURE`; selects `CTAG_SCHED` and `CTAG_FRAG`) |
| `CTAG_GW_TAG_LINKS` | 2 | connections at once = `caps.tag_links` = `CTAG_SCHED_LINKS` (asserted) |
| `CTAG_GW_RADIO_TUNNELS` | 8 | own-radio tunnels, waiting or connected |
| `CTAG_GW_LINK_INFLIGHT` | 4 | `DATA` fragments handed to the host at once, per link |
| `CTAG_SCHED_TAGS` | 8 | the scheduler's per-tag back-off entries |
| `BT_CENTRAL`, `BT_GATT_CLIENT`, `BT_MAX_CONN` | y, y, 2 | the bridge's central options, with `BT_GATT_CACHING=n` and `BT_GAP_AUTO_UPDATE_CONN_PARAMS=n` |
| `BT_L2CAP_TX_MTU` / `BT_BUF_ACL_RX_SIZE` / `BT_BUF_ACL_TX_SIZE` | 23 / 27 / 27 | ATT MTU 23: fragments of `ATT_VALUE_MAX` bytes |
| `BT_ATT_TX_COUNT`, `BT_L2CAP_TX_BUF_COUNT`, `BT_BUF_ACL_TX_COUNT` | 12 | above both links' `DATA` in flight (2 × 4, asserted in `central.c`): a write from the loop never waits for a buffer (the host allocates with `K_FOREVER` outside the system work queue) |
| `BT_BUF_EVT_RX_COUNT` | 16 | more HCI event buffers than ACL TX buffers, as the bridge |

Counters (INFO / GET_COUNTERS): `radio_adverts` (advertisements the core
got), `radio_tunnels` (opened), `radio_stray` (link events no tunnel
wanted), `mesh_paused` (mesh sends held back while suspended, per retry),
the scheduler's `attempts`, `suspend_count`, `suspend_fail`,
`suspend_max_ms`, `resume_fail`, `connect_failed`, `quick_retries`,
`deferred`, `rate_limited`, `backoff_skips`, `sessions_ok`, `sessions_fail`
(a session: a connected tunnel), and from `central.c` `radio_adv_dropped`
and `gatt_failures`. INFO then carries 82 counters (`MAX_COUNTERS` 88: 96
keys at most with INFO's top-level keys, firmware-libs.md `ctag_cbor`) in
about 1.3 KB of the 2,560-byte transmit frame.

### 16.8 Resources

`gateway-nrf52840dk` against the same tree without `CONFIG_CTAG_GW_RADIO`
(2026-09-28, §8): **+70,160 B of flash and +14,796 B of RAM** (the Dongle
+70,368 / +14,732 B against the earlier v2 figures). By symbol (the ELF's
`nm -S -l`), flash: the Zephyr controller's central role and connections
(`ull_conn`, the LL control procedures, `lll_conn`, `ull_central`) +32.1 KB,
the host's connections, L2CAP, ATT and GATT client +25.8 KB, `gw_radio.c`
3.9 KB, `central.c` 2.9 KB, `lib/sched` 2.2 KB, `lib/frag` 0.2 KB, the rest
of the gateway 0.5 KB. RAM: host buffers and connection objects +7.3 KB
(16 HCI event buffers of 4.9 KB, ATT, ACL and L2CAP pools), the controller's
two connection contexts and control procedures +4.3 KB, the core +2.5 KB
(two links of 1,016 B — a two-message queue of 324-byte entries and a
320-byte reassembly buffer each — eight tunnels of 24 B, the scheduler
260 B), `central.c`'s link records 632 B. 120 KB of RAM stay free.

Stacks: the Bluetooth calls (`bt_mesh_suspend()`, `bt_conn_le_create()`,
`bt_gatt_*()`) run on the gateway loop (the main thread, 12 KiB), outside
the Ed25519 chain of §15.11 that sized it; the callbacks put a 172-byte
`gw_evt` on the Bluetooth RX thread's stack, as the mesh handlers already
do. Neither is measured yet (§10).

### 16.9 Tests

`gw_radio` (24 tests in `ctag.gateway.core.v2`, which enables
`CONFIG_CTAG_GW_RADIO`; the mocked backend records the radio operations,
asserts that connections start only while the mesh is suspended and GATT
only after the resume, and fails any mesh send, configuration step or
provisioning while it is suspended):

- caps `tag_links` in HELLO and INFO;
- TUNNEL_OPEN: `INVALID` for tag 0, duration 0 and 256, an unknown mode and
  `SESSION` through a bridge; `BUSY` (not remembered) for a second tunnel to
  a tag and beyond eight; the repeated `op_id`; ids shared with mesh
  tunnels, each id reaching its own table; `ASSIGN_TAG`, `UNASSIGN_TAG`,
  `TAG_COMMAND`, `DELIVER_LAYOUT`, `IDENTIFY_NODE`, `REMOVE_NODE` to
  `GATEWAY_ADDR` `NOT_FOUND`;
- advertisement (ver 1) → suspend → create (the advertiser, 1 s) →
  connected → resume → setup → `EVT_TUNNEL OPEN` with CAPS and the
  advertisement's RSSI; one attempt at a time; `BUSY` before OPEN; a value
  before OPEN dropped; `PAIR` with ident2, `PAIR` messages written with
  response and reassembled; setup `UNSUPPORTED`, `INVALID`, nothing read, a
  refused setup and the 5 s bound;
- `CTRL` fragments one at a time, `DATA` fragments four in flight with their
  own `SEQ`, `SEQ` continuing across messages; `INVALID` types and empty
  messages, `TOO_LARGE`, two queued then `BUSY`; a host buffer shortage
  retried with the same `SEQ`; an ATT error (`INVALID`), a failed `DATA`
  completion and a refused write (`DISCONNECTED`, after the `OK`);
- `CTRL` and `STATUS` reassembled separately, interleaved; a `SEQ` gap, an
  overlong message and an oversized value (`INVALID`);
- `TUNNEL_CLOSE` (the link down, no event, no back-off), closed while
  waiting and while connecting; a dropped link (`DISCONNECTED`, then the
  back-off); the waiting and idle timeouts and what restarts the latter; an
  attempt running at the waiting deadline (reached: OPEN; failed: `TIMEOUT`
  at once);
- DISCOVER on the own radio: the filter, versions and flags, the rate
  limit, the window's end, `bridge` 0 beside a bridge's candidate, duration
  0, 121 s;
- a delivery, an IDENTIFY and a PROVISION while the mesh is suspended (held,
  then sent at the resume; `PROVISIONING_ACTIVE`, then accepted); an attempt
  waiting for a segmented send, and deferred after 500 ms; no attempt while
  provisioning or configuring; the rate limit across seven tags; a second
  link only while the first idles, a third tag waiting for a free link,
  links failing independently; RELEASE.

`ctag.gateway.core.v2.noradio` (v2 without tag links, as the interop build)
checks that `GATEWAY_ADDR` stays refused (protocol.md §11). The interop
build has no Bluetooth and no tag links (§11, "Interop test").

### 16.10 Open items

- Nothing of it has run on hardware: a connection while the mesh is
  suspended, the GATT setup and the long IDENT read against a real tag, the
  write pacing, the controller's scheduling of two links beside the mesh's
  scanning and advertising, and the stack use on the loop and the Bluetooth
  RX thread (§10) are verified only by building and on native_sim.
- The tag firmware does not serve `IDENT` / `PAIR` yet (connect-setup.md
  §7.2): `PAIR` tunnels on the own radio are tested against the mock only.
- No GATT handle cache (the bridge keeps one per tag): every connection
  discovers the tag service again, a few connection events.
