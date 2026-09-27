# Cremind Tag protocols

This document is normative. Numeric identifiers, field orders and limits live in
[`protocol/spec.yaml`](../protocol/spec.yaml); the generated
`include/ctag/proto_ids.h` and `companion/src/cremind_tag/protocol/ids.py` are
the only way code may refer to them. Byte-exact test vectors live in
[`protocol/fixtures/`](../protocol/fixtures/) and are checked by both the C host
tests and the Python test-suite.

All multi-byte integers are **little-endian** unless stated otherwise.

```
Cremind ──HTTPS──▶ companion (PC) ──serial──▶ gateway ──mesh──▶ bridge ──BLE GATT──▶ tag
          §7 connector API        §1               §2          §3,§4         §5,§6
```

---

## 1. Serial protocol (companion ↔ gateway, companion ↔ bridge maintenance port)

### 1.1 Framing

Every frame is COBS-encoded and terminated by a single `0x00`. The decoded frame is:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 1 | `version` = `PROTO_VERSION` |
| 1 | 1 | `type` (`serial.message_types`) |
| 2 | 2 | `request_id` (echoed in the response; `0` for events) |
| 4 | 2 | `length` of the payload |
| 6 | 1 | `flags` (`RESPONSE` 0x01, `EVENT` 0x02) |
| 7 | 1 | `credits` — incremental credit grant (see 1.3) |
| 8 | `length` | payload: one CBOR map with integer keys (`serial.cbor_keys`) or empty |
| 8+`length` | 4 | CRC-32/IEEE (poly 0x04C11DB7 reflected, init 0xFFFFFFFF, xorout 0xFFFFFFFF) over bytes `0 .. 8+length` |

- COBS is the standard Cheshire–Baker encoding. A block of 254 non-zero bytes
  (code `0xFF`) implies no zero, and the encoder emits no trailing `0x01` block
  when the data ends right after such a block (so 254 non-zero bytes encode to
  255 bytes); decoders accept either form. Empty frames (consecutive `0x00`)
  are ignored.
- A decoded frame larger than `SERIAL_MAX_FRAME` (4096) is discarded while it is
  still being received (the receiver resynchronises on the next `0x00`).
- A frame with a bad CRC, a bad `length`, or an unknown `version` is dropped and
  counted (`crc_errors`, `len_errors`, `version_errors`), checked in this order:
  decoded size ≥ 12 and ≤ `SERIAL_MAX_FRAME` (`len_errors`), CRC over all but
  the last 4 bytes, `length` = decoded size − 12, `version`. A request with an unknown `type` gets a
  `RESPONSE` with `{status: UNSUPPORTED}`.
- Empty payload (`length = 0`) is allowed for requests with no arguments.
- CBOR: definite-length maps/arrays/strings only, integers in the shortest form,
  no tags, no floats. Unknown keys are ignored (forward compatibility).

### 1.2 Requests, responses, events

- Every request carries a fresh non-zero `request_id` (wrapping u16). The device
  answers with the same `type`, the same `request_id`, `flags = RESPONSE`, and a
  map whose key `status` is always present.
- **A serial ACK confirms acceptance only.** Asynchronous work answers
  `status = ACCEPTED`; its outcome arrives later as an event.
- Events use `flags = EVENT` and `request_id = 0`. Events that matter for
  durability (`EVT_PROVISIONED`, `EVT_NODE_CONFIGURED`, `EVT_NODE_REMOVED`,
  `EVT_ASSIGN_RESULT`, `EVT_RESULT`) carry a monotonically increasing `seq` and
  are **retained** (up to `SERIAL_EVENT_RETAIN`) until the host sends
  `EVENT_ACK {seq}` (cumulative). Retained events are re-sent after every
  `HELLO`. If the retention ring overflows, the oldest event is dropped and the
  `events_dropped` counter increments; the companion reconciles by timeout (the
  delivery becomes `UNCERTAIN` and is retried — the tag's stored ACK makes the
  retry idempotent).
- `boot_id` (random u32 per gateway boot) is returned by `HELLO`/`INFO`. A
  changed `boot_id` tells the companion that in-flight gateway state was lost.

### 1.3 Credit-based flow control

- After `HELLO`, each side may send `SERIAL_DEFAULT_CREDITS` frames before
  receiving any grant. `HELLO`'s response also reports the device's `credits`.
- Each frame sent consumes one of the peer's credits. The `credits` header byte
  of **any** frame is an incremental grant: "you may now send this many more
  frames to me". A receiver grants a credit back when it has finished with a
  receive buffer. A frame with only a grant may be sent as `PING`'s response or
  as any response/event with the grant byte set.
- A sender with zero credits waits. The device never blocks the radio on the
  host: events that cannot be sent wait in their bounded ring (retained) or are
  dropped (non-retained `EVT_STAGE`, `EVT_LOG`, `EVT_UNPROV_BEACON`,
  `EVT_TAG_SEEN`) with a counter.
- Frames sent without credit are processed if a buffer happens to be free, else
  dropped and counted (`overruns`); the companion treats a missing response
  after 2 s as a timeout and retries with the same `op_id`.

### 1.4 Idempotency

Side-effecting requests (`PROVISION`, `CONFIGURE_NODE`, `REMOVE_NODE`,
`ASSIGN_TAG`, `UNASSIGN_TAG`, `DELIVER_LAYOUT`, `CANCEL_DELIVERY`,
`TAG_COMMAND`, `REBOOT`, `IDENTIFY_NODE`) carry an `op_id` (u64 chosen by the
companion). The device remembers the last `SERIAL_IDEMPOTENCY_SLOTS` op ids with
their immediate status; a repeated `op_id` returns the remembered status with
`detail = DUPLICATE` and performs no new work. `DELIVER_LAYOUT` is additionally
idempotent end-to-end by `(tag_id, epoch, revision, digest)` (§3.4, §5.6).

### 1.5 Delivery on the serial link

`DELIVER_LAYOUT {op_id, bridge, tag_id, epoch, revision, update_id,
fontpack_id, layout}` answers:

- `ACCEPTED` — queued in the gateway (stage `GATEWAY_RECEIVED`),
- `BUSY` — the gateway's delivery queue is full; retry later,
- `INVALID` / `TOO_LARGE` — rejected (companion marks the job `FAILED`),
- the remembered status with `detail = DUPLICATE` — this `op_id` was seen
  before (§1.4).

The companion never sends a layout larger than `LAYOUT_SERIAL_MAX` (4000
bytes): the request's CBOR envelope must fit `SERIAL_MAX_PAYLOAD`. Bridges
still validate against `LAYOUT_HARD_MAX` (§4.3).

Then `EVT_STAGE` (`BRIDGE_RECEIVED`, `TRANSFERRING`, `REFRESHING`) best effort,
and exactly one retained `EVT_RESULT` with the final `status`
(`OK` = displayed; see §5.6 for the rest).

### 1.6 Bridge maintenance port

A bridge exposes the same framing on its USB CDC ACM (nRF52840) or UART
(nRF52832) maintenance port, answering `HELLO`, `PING`, `INFO`, `REBOOT` and the
font-installation messages `FONT_BEGIN`, `FONT_DATA`, `FONT_COMMIT`,
`FONT_STATUS`, `FONT_ABORT` (§8.4). It never accepts mesh or delivery requests
on this port.

---

## 2. Mesh network (gateway ↔ bridges)

- One gateway (provisioner, configuration client, `LAYOUT_CLI`, `MGMT_CLI`) and
  up to `MAX_BRIDGES` bridges (relay, `LAYOUT_SRV`, `MGMT_SRV`). One net key
  (index 0) and one app key (index 0) bound to all four vendor models.
- Provisioning: PB-ADV only, **explicitly selected devices only** (the companion
  sends `PROVISION` with a UUID the operator chose from `EVT_UNPROV_BEACON`s).
  No OOB authentication in v1 (documented risk: provisioning must happen in a
  controlled environment). The gateway persists the CDB (net key, app key,
  addresses, device keys, IV index, sequence number) via Zephyr settings.
- Configuration after provisioning (`CONFIGURE_NODE`): app key add, bind the
  two server models on the bridge, relay on (retransmit 2 × 20 ms), default TTL
  `MESH_DEFAULT_TTL`, network transmit 3 × 20 ms. Friend, Proxy and Low Power
  features are disabled at build time on every node.
- Access messages use vendor opcodes `(0xC0 | op), MESH_COMPANY_ID`.
  Parameters never exceed `MESH_MAX_VENDOR_PARAMS` (160 bytes); every node sets
  `CONFIG_BT_MESH_TX_SEG_MAX` and `CONFIG_BT_MESH_RX_SEG_MAX` ≥ `MESH_MIN_SEG`.
- Addresses: gateway `0x0001`; bridges get consecutive unicast addresses from
  the CDB allocator. All gateway→bridge messages are unicast to the bridge's
  primary element; bridge→gateway messages go to `0x0001`.

---

## 3. Layout transfer (gateway → bridge)

### 3.1 Messages

`LAYOUT_BEGIN`, `LAYOUT_CHUNK`, `LAYOUT_COMMIT`, `LAYOUT_STATUS`,
`LAYOUT_CANCEL`, `DELIVERY_STAGE`, `DELIVERY_RESULT`, `RESULT_ACK` — field
layouts in `spec.yaml` (`mesh.opcodes`).

### 3.2 Gateway sender rules

1. **One outstanding segmented send** in the whole gateway: the next segmented
   message is only handed to `bt_mesh_model_send()` after the previous one's
   `end` callback reported success (the lower transport's segment ACK). A failed
   `end` retries the same message up to 3 times, then fails the delivery with
   `TIMEOUT`.
2. A delivery is: `LAYOUT_BEGIN` → `LAYOUT_CHUNK` × `chunk_count` (index 0..n-1,
   all but the last exactly `LAYOUT_CHUNK_DATA_MAX` = 150 bytes) →
   `LAYOUT_COMMIT`.
3. The bridge answers `LAYOUT_STATUS`. `INCOMPLETE` lists missing chunks in the
   `missing` bitmap; the gateway re-sends exactly those, then `LAYOUT_COMMIT`
   (at most 3 rounds). No `LAYOUT_STATUS` within 10 s of a commit → re-send the
   commit (3 times) → `TIMEOUT`.
4. `xfer_id` is a gateway-wide wrapping u16; a bridge treats a `LAYOUT_BEGIN`
   with a new `xfer_id` as replacing any partially received transfer
   (**one layout being assembled** per bridge).

### 3.3 Bridge validation at `LAYOUT_COMMIT` (in this order)

| Check | Status on failure |
|---|---|
| A transfer with this `xfer_id` exists | `NOT_FOUND` |
| All chunks present | `INCOMPLETE` (+ `missing`) |
| `total_len` (and the concatenated length) ≤ `LAYOUT_HARD_MAX` | `TOO_LARGE` |
| Concatenated length = `total_len`, every chunk but the last exactly `LAYOUT_CHUNK_DATA_MAX` | `INVALID` |
| `SHA-256(layout)[0:16]` = `digest` | `DIGEST_MISMATCH` |
| Tag is assigned to this bridge with exactly this `epoch` | `NOT_ASSIGNED` / `STALE_EPOCH` |
| `revision` > last accepted revision for the tag (same revision + same digest → `DUPLICATE`, the stored result is re-sent) | `STALE_REVISION` |
| `fontpack_id` = active font pack id | `FONTPACK_MISMATCH` |
| Layout parses and passes §4.3 bounds; every referenced strike exists | `INVALID` / `FONTPACK_MISMATCH` |

A `LAYOUT_CHUNK` whose `index` ≥ `chunk_count` (or ≥ 32, the width of the
`missing` bitmap) is ignored. A `LAYOUT_BEGIN` repeating the current `xfer_id`
restarts that transfer.

On success: `LAYOUT_STATUS OK`, the layout replaces any older pending layout for
that tag (the older one is reported `SUPERSEDED` via `DELIVERY_RESULT`), and the
tag scheduler (§5) takes over.

### 3.4 Results

`DELIVERY_RESULT` carries a bridge-local `result_seq`; the bridge re-sends it
every `MESH_RESULT_RETRY_MS` up to `MESH_RESULT_RETRIES` times until the gateway
answers `RESULT_ACK {result_seq}`. The gateway de-duplicates by
`(bridge, result_seq)` and turns the first copy into a retained `EVT_RESULT`.

---

## 4. Logical screen format ("layout")

### 4.1 Header (12 bytes)

`magic 'C''L'`, `version`, `flags`, `width`, `height`, `rotation`,
`background`, `cmd_count` — see `spec.yaml` `layout.header`.

### 4.2 Commands

`CLEAR`, `GLYPHS`, `ICON`, `LINE`, `RECT`, `PROGRESS`, `QR` (field layouts in
`spec.yaml`). A `GLYPHS` glyph entry is 4 bytes: `glyph_id u16, dx i8, dy i8`.

### 4.3 Bounds (validated by the bridge and by the companion before sending)

- total size ≤ `LAYOUT_HARD_MAX`; `cmd_count` ≤ `LAYOUT_MAX_COMMANDS`; sum of
  glyph counts ≤ `LAYOUT_MAX_GLYPHS`; the byte stream is consumed exactly;
- `width`, `height` ∈ 1..2048; `rotation` ∈ 0..3; colours ∈ {0,1,2};
- `LINE.width` ∈ 1..8; `QR.module_px` ∈ 1..8; `QR.ecc` ∈ 0..3;
  `QR.len` ∈ 1..`LAYOUT_QR_MAX_TEXT` and every byte ∈ 0x21..0x7E;
- `PROGRESS.value` is clamped to `max` when rendering (not an error).

Validators stop at the first failure and check in this order:

1. total size ≤ `LAYOUT_HARD_MAX` → else `TOO_LARGE`;
2. header present (12 bytes) and `magic` → `INVALID`; `version` =
   `PROTO_VERSION` → `UNSUPPORTED`; `width`, `height`, `rotation`,
   `background` → `INVALID`; `cmd_count` → `TOO_LARGE`;
3. each command in stream order: known `op` → `UNSUPPORTED`; fixed and
   variable parts present → `INVALID`; field bounds in field order →
   `INVALID`; running glyph total → `TOO_LARGE`;
4. no bytes after the last command → `INVALID`;
5. only then, in command order, the strikes against the active font pack:
   `GLYPHS (face, size_px)` and `ICON (0, size_px)` exist → `FONTPACK_MISMATCH`.

`flags` is informational (bit0: the layout paints `RED`); validators and
renderers ignore it. `GLYPHS.count = 0` is valid. With `LAYOUT_QR_MAX_TEXT` =
96 every valid text fits version 10 (119 bytes at ECC `HIGH`), so the §4.4 QR
fit rule never fails in v1. The panel-geometry rule of §4.4 (Rotation) is a
separate check that yields `INVALID`.

### 4.4 Rendering semantics (normative — C and Python must match bit for bit)

**Canvas.** Logical size `W×H` from the header. Every pixel starts as
`background`. Commands paint in order; the last writer wins. Pixels outside
`[0,W)×[0,H)` are discarded.

**Glyph strikes.** `GLYPHS` uses strike `(face, size_px)`. The pen starts at
`(origin_x, origin_y)`; for each glyph, `pen += (dx, dy)` **then** the glyph is
drawn with its bitmap's top-left at `(pen.x + bearing_x, pen.y − bearing_y)`.
Bitmap bit `1` paints `color`; bit `0` is transparent. A glyph id ≥ the strike's
glyph count, or with an empty bitmap, draws nothing.

**ICON.** Strike `(face 0, size_px)`, glyph id = icon id. Icon bitmaps are
exactly `size_px × size_px` with zero bearings; the bitmap's top-left is drawn
at `(x, y)` (stored bearings are not applied). Empty or out-of-range glyphs
draw nothing, as for `GLYPHS`.

**LINE.** Integer Bresenham over all octants:

```
dx = |x1-x0|, sx = x0<x1 ? 1 : -1, dy = -|y1-y0|, sy = y0<y1 ? 1 : -1, err = dx+dy
loop: plot(x0,y0); if x0==x1 && y0==y1 break
      e2 = 2*err; if e2 >= dy { err += dy; x0 += sx }; if e2 <= dx { err += dx; y0 += sy }
```

`plot(px,py)` fills the square `[px−o, px−o+width) × [py−o, py−o+width)` with
`o = (width−1) / 2` (integer division).

**RECT.** `w = 0` or `h = 0` draws nothing. `border = 0` fills
`[x, x+w) × [y, y+h)`. Otherwise a pixel of that rectangle is painted when
`lx < x+border || lx ≥ x+w−border || ly < y+border || ly ≥ y+h−border`.

**PROGRESS.** Outline `RECT(x, y, w, h, border 1, color)`; then, when `w > 4`
and `h > 4`, fill `[x+2, x+2+f) × [y+2, y+h−2)` where
`f = floor((w−4) × min(value, max) / max)` (`max = 0` → `f = 0`).

**QR.** Encode `text` with the Nayuki QR Code generator algorithm
(`encodeText`, `ecl = ecc`, `minVersion = 1`, `maxVersion = 10`,
`mask = auto`, `boostEcl = true`; `ecc` 0..3 = LOW, MEDIUM, QUARTILE, HIGH).
Both implementations vendor Nayuki QR-Code-generator **v1.8.0**: C
`qrcodegen_encodeText(text, tmp, qr, ecc, 1, 10, qrcodegen_Mask_AUTO, true)`
(the text copied into a NUL-terminated buffer), Python
`QrCode.encode_segments(QrSegment.make_segments(text), ecl, 1, 10, -1, True)`.
Each dark module `(mx, my)` paints the square
`[x + mx·m, x + (mx+1)·m) × [y + my·m, y + (my+1)·m)` with `m = module_px`.
Light modules and the quiet zone are not painted (layouts reserve white space).
Text that does not fit version 10 makes the layout `INVALID`.

**Rotation (logical → native).** Native panel size `Wn×Hn` (tag CAPS).

| `rotation` | requires | `nx` | `ny` |
|---|---|---|---|
| 0 | `W=Wn, H=Hn` | `lx` | `ly` |
| 1 (90° cw) | `W=Hn, H=Wn` | `Wn−1−ly` | `lx` |
| 2 | `W=Wn, H=Hn` | `Wn−1−lx` | `Hn−1−ly` |
| 3 | `W=Hn, H=Wn` | `ly` | `Hn−1−lx` |

A layout whose size does not satisfy the requirement for the tag's panel is
`INVALID` at session time (the bridge learns `Wn×Hn` from CAPS; the companion
already knows it from enrollment and validates earlier).

**Planes.** Native row-major, MSB-first, `row_bytes = ceil(Wn/8)`,
`plane_len = row_bytes × Hn`. Padding bits at the end of a row hold the plane's
white value.

- `planes = 1`: `RED` renders as `BLACK`. Plane 0 bit = 1 means white when
  `plane_flags.bit0` is set, black otherwise.
- `planes = 2`: plane 0 encodes black vs. not-black (a red pixel takes plane
  0's white value); plane 1 encodes red vs. not-red, bit = 1 meaning red when
  `plane_flags.bit1` is set.

**Strips.** The bridge renders native rows `[y0, y0+BRIDGE_STRIP_ROWS)` of one
plane at a time. Rendering a strip must produce exactly the bytes of the same
rows of a full-frame render.

**Frame digest.** `SHA-256(plane0 ‖ plane1)` (plane 1 only when `planes = 2`).

---

## 5. Bridge ↔ tag session (BLE GATT)

### 5.1 Advertising (tag)

Every `TAG_WAKE_PERIOD_MS ± TAG_WAKE_JITTER_MS` (uniform), the tag advertises
connectably for `TAG_ADV_WINDOW_MS` at `TAG_ADV_INTERVAL_MS` using **legacy 1M
advertising** from its static random identity address. AD structures:

- Flags `0x06`;
- Manufacturer Specific Data: `company u16 = MESH_COMPANY_ID`, `ver u8 = 1`,
  `tag_id u32`, `flags u8` (bit0 result pending, bit1 low battery, bit2
  display state unknown), `disp_rev u16` (low 16 bits of the displayed revision).

No scan response. Between windows the tag idles in System ON with the RTC
running.

### 5.2 Connection (bridge) — the mesh suspend window

The Zephyr Controller in NCS v3.4.1 requires scanning to be stopped before it
creates a central connection, so the bridge runs this state machine (all steps
in a work-queue item, never in a Bluetooth callback):

1. A validated layout is pending for tag *T* and *T*'s advertisement is seen in
   the shared scan callback (`bt_le_scan_cb_register`).
2. Rate limit: at most `BRIDGE_MAX_SUSPENDS_PER_MIN` attempts per minute across
   all tags, and `BRIDGE_TAG_BACKOFF_MS` per tag after a failure.
3. Wait for local mesh application sends to finish (or defer the attempt).
   Never initiate while provisioning/configuration of this node is in progress.
4. `bt_mesh_suspend()`. Failure → no connection is attempted; record
   `MESH_SUSPEND_FAILED` and retry on a later advertisement.
5. `bt_conn_le_create()` towards the advertiser with a
   `BRIDGE_CONN_ATTEMPT_MS` timeout.
6. On connected, on failure, or on a confirmed cancellation: `bt_mesh_resume()`.
   Measure `suspend_ms` from step 4 to the resume's return.
7. Resume failure → disconnect, abort the session with `MESH_RESUME_FAILED`,
   and enter controlled recovery (retry `bt_mesh_resume()` with back-off; if it
   still fails after 5 s, reboot — the mesh stack reloads from settings).
8. Only after a successful resume: GATT discovery, handshake and image
   transfer. The link then shares radio time with mesh under the Controller's
   scheduler; connection interval 30–50 ms, peripheral latency 0, supervision
   timeout 4 s, and the bridge sends at most 4 records per connection event.

### 5.3 Fragmentation

Each ATT value on `CTRL`, `DATA`, `STATUS` starts with a header byte:
`START` 0x80, `END` 0x40, `SEQ` (6 bits). Fragments of one message are
consecutive; `SEQ` increments by one per fragment (mod 64) per characteristic
**and direction** (each sender numbers its own fragments, starting at 0 on
connect). Every fragment carries at least one payload byte; senders fill every
fragment but the last to the maximum (ATT value − 1). A gap, an overlong message
(`CTRL` > `TAG_CTRL_MSG_MAX`, `DATA`/`STATUS` > `TAG_RECORD_WIRE_MAX`), a
`START` in the middle of a message, a fragment without `START` outside a
message, or an empty fragment aborts the session (`INVALID`). The reassembled
message's first byte is its type (§5.4).

### 5.4 Handshake (plaintext on `CTRL`)

```
bridge                                      tag
HELLO{proto, tag_id, epoch, nonce_b}  ───▶  checks tag_id, proto, epoch ≥ stored_epoch
                                     ◀───  CHALLENGE{proto, nonce_t, stored_epoch,
                                                     displayed_rev, last_status, battery_mv, flags}
AUTH{mac_b}                          ───▶  verifies mac_b
                                     ◀───  AUTH_OK{mac_t}   then STATUS: CREDIT{caps.credits}
```

Keys (`spec.yaml` `crypto`):

```
K_epoch  = HKDF-SHA256(IKM = tag_secret, salt = "cremind-tag/v1/epoch",
                       info = "K_epoch" ‖ tag_id ‖ epoch, L = 16)
th       = SHA-256(HELLO ‖ CHALLENGE)            (the reassembled messages, type byte included)
mac_b    = HMAC-SHA256(K_epoch, "B" ‖ th)[0:16]
mac_t    = HMAC-SHA256(K_epoch, "T" ‖ th ‖ mac_b)[0:16]
k_b2t ‖ k_t2b = HKDF-SHA256(IKM = K_epoch, salt = th, info = "cremind-tag/v1/session", L = 32)
```

- The companion derives `K_epoch` from the tag secret and sends it to the
  assigned bridge (`ASSIGN_SET`); bridges never see `tag_secret`.
- The tag checks `HELLO` in this order and answers `ERROR` with the first
  failure: malformed → `INVALID`; `tag_id` not its own → `NOT_FOUND`; `proto`
  unsupported → `VERSION_MISMATCH`; `epoch < stored_epoch` → `STALE_EPOCH`. A
  malformed `AUTH` is `AUTH_FAILED`.
- The tag rejects `epoch < stored_epoch` (`ERROR STALE_EPOCH`). An
  `epoch > stored_epoch` is persisted only after `AUTH` verifies; from then on
  older epochs (and bridges holding their keys) are refused.
- MAC comparison is constant-time. Any failure → `ERROR{AUTH_FAILED}` and the
  tag disconnects. Three consecutive failures make the tag skip the next wake
  window (anti-brute-force pacing).

### 5.5 Authenticated records (`DATA` bridge→tag, `STATUS` tag→bridge)

`record = type u8 ‖ counter u32 ‖ AES-128-CCM(k_dir, nonce, aad = type ‖ counter, plaintext) ‖ mic[8]`,
`nonce = dir u8 (0 = b2t, 1 = t2b) ‖ counter u32 LE ‖ 8 × 0x00`.
Counters start at 0 per session and direction and must increase by exactly one;
anything else is `AUTH_FAILED`. Keys are fresh per session (both nonces), so a
recorded session cannot be replayed. Plaintext ≤ `TAG_RECORD_PAYLOAD_MAX`.

The tag owns **two** `TAG_RECORD_BUF` buffers: one reassembling, one being
authenticated/written to the panel. It grants `DATA` credits with
`CREDIT{n}` on `STATUS`; each record consumes one credit. A record is fully
reassembled and authenticated **before** any byte of its plaintext reaches the
panel controller.

### 5.6 Frame transfer and results

```
FRAME_BEGIN{revision, update_id, digest, planes, plane_len}
PLANE_DATA{plane, offset, data} × n        (plane 0 fully, then plane 1)
FRAME_END{}
                     ◀─ PROGRESS{REFRESHING}  after validation, before refresh
                     ◀─ RESULT{update_id, epoch, revision, status, digest[0:8], battery_mv, refresh_ms, flags}
```

At `FRAME_BEGIN` the tag compares `(epoch, revision, digest)` with its persisted
result (§6; `epoch` is the session's). The first matching row decides:

| Condition | Tag answers immediately |
|---|---|
| `(epoch, revision)` < stored (lexicographic) | `RESULT STALE_REVISION` |
| equal, different digest | `RESULT REVISION_CONFLICT` |
| equal, same digest, stored state `DISPLAYED` | stored `RESULT OK` with `flags.bit0` (duplicate) |
| `planes`/`plane_len` ≠ the panel's | `RESULT INVALID` |
| otherwise — incl. equal + same digest with stored state `REFRESH_INTENT` (`DISPLAY_STATE_UNKNOWN` recovery, which may repeat the refresh) and no stored record | accept, `begin_frame()` |

`PLANE_DATA.offset` must equal the bytes already received for that plane and
planes arrive in order. The tag keeps an incremental SHA-256 of all plane
bytes. At `FRAME_END`: all bytes present **and** the computed digest equals
`FRAME_BEGIN.digest`, else `RESULT DIGEST_MISMATCH`/`INCOMPLETE` and
`abort_frame()` (the panel never refreshes). Then the transaction in §6 runs
and `RESULT` is sent. A `FRAME_ABORT`, a disconnect or a timeout before
`FRAME_END` aborts the frame; the next session restarts from offset 0.

`CMD{CLEAR}` makes the tag write both planes with white itself, refresh, and
persist `(epoch, revision 0, digest_of_white)`. `CMD{SLEEP}` enters System OFF
only on boards whose external wake is verified (else `UNSUPPORTED`). Identify
and forced refresh are companion-level operations that deliver a new revision.

---

## 6. Tag display transaction

```
RECEIVING ─▶ VALIDATED ─▶ REFRESH_INTENT persisted ─▶ REFRESHING ─▶ BUSY complete
          ─▶ DISPLAYED result persisted ─▶ RESULT(ACK) sent
```

Persistent record (one NVS entry, written atomically), 60 bytes little-endian:
`version u8 (= 1)`, `state u8` (0 = `DISPLAYED`, 1 = `REFRESH_INTENT`),
`status u8`, `reserved u8`, `tag_id u32`, `epoch u32`, `revision u32`,
`update_id u64`, `digest[32]`, `crc32 u32` over the previous 56 bytes. A record
with a wrong length, version, state or CRC is treated as corrupt (the tag
behaves as if it had no record and reports `STORAGE_ERROR` in its next
`CHALLENGE.last_status`).

- `REFRESH_INTENT` (with the new `(epoch, revision, update_id, digest)`) is
  written **before** `commit_refresh()`.
- `DISPLAYED`/`OK` is written after `wait_refresh_complete()` succeeds, **then**
  `RESULT` is sent.
- On boot, a record in `REFRESH_INTENT` becomes `status =
  DISPLAY_STATE_UNKNOWN` (persisted; `state` stays `REFRESH_INTENT`) and every
  `CHALLENGE` sets `flags.bit0` while the stored state is `REFRESH_INTENT`. The bridge reports `DISPLAY_STATE_UNKNOWN`
  (`UNCERTAIN` outcome) and may re-deliver the same revision.
- A duplicate committed revision always returns the stored ACK.
- The ACK means "the panel controller finished the refresh"; it is not optical
  verification and not proof that a person read it.

Panel interface (per panel profile): `begin_frame`, `write_plane_chunk`,
`validate_frame`, `commit_refresh`, `wait_refresh_complete`, `sleep_panel`,
`abort_frame`. Writes stage into controller RAM and never trigger a refresh;
only `commit_refresh` issues the refresh command.

---

## 7. Connector API (Cremind ↔ companion)

See [`connector-api.md`](connector-api.md).

---

## 8. Font packs

See [`fontpack.md`](fontpack.md) for the binary format, the flash layout, slot
switching, the maintenance-port installation flow (`FONT_*`) and the capacity
rule `Required ≥ 2 × erase_aligned(P) + 16 MiB`.

---

## 9. Enrollment blob

Written by J-Link to `UICR.CUSTOMER[0..11]` (48 bytes): `magic 'CTAG'`,
`version = 1`, `board`, `panel`, `flags`, `tag_id`, `secret[32]`, `crc32` over
the previous 44 bytes. Firmware refuses to advertise (and blinks/logs
`SECURITY_CONFIG`) when the blob is missing or its CRC fails. The companion
keeps the secret in the OS credential store and never sends it to Cremind or to
a bridge.

---

## 10. Shared implementation rules (gateway, bridge, tag, simulator)

These close gaps the sections above leave open. The companion's simulator
(`docs/simulator.md`) implements exactly these rules; firmware must match.

**Serial link**

- `HELLO` is exempt from credit counting in both directions. Its response's
  `credits` header byte is added to the `SERIAL_DEFAULT_CREDITS` the host starts
  with; grants received before the `HELLO` response are ignored.
- On `HELLO` the device drops responses it has not yet sent, answers `HELLO`
  immediately, then re-sends every retained event.
- On a response timeout or a credit stall the host sends `HELLO` again (which
  repairs credit state after a lost frame) and re-sends pending requests with the
  same `op_id`.
- A repeated `op_id` answers the remembered status with `detail = DUPLICATE`.
  Transient refusals (`BUSY`, `NO_RESOURCES`, `PROVISIONING_ACTIVE`) are **not**
  remembered, so a retry with the same `op_id` can succeed.
- `REBOOT` answers first, then the device resets (USB re-enumerates; the host
  reconnects and sees a new `boot_id`).
- The gateway identifies itself to the companion by its USB serial number (or,
  without one, the port it was reached on); the companion derives the hardware id
  `gw-<uuid5>` from it.

**Delivery**

- A `LAYOUT_STATUS` other than `OK`, `INCOMPLETE` or `DUPLICATE` ends the
  delivery: the gateway emits its `EVT_RESULT` with that status and a zero digest.
  Delivering to an unknown or unconfigured bridge answers `NOT_FOUND` at once.
- `DUPLICATE` at the bridge: a displayed revision's stored result is re-sent under
  the new `update_id`; a still-pending revision adopts the new `update_id`; a
  revision that ended without being displayed is accepted again.
- A `LAYOUT_COMMIT` repeated for a transfer the bridge already accepted (its OK
  was lost) answers `DUPLICATE`, never `NOT_FOUND`, so a lost status never ends
  a delivery. The gateway treats `DUPLICATE` like `OK` for that transfer.
- The gateway emits **exactly one** `EVT_RESULT` per `update_id`; later results
  for an `update_id` it already reported are acknowledged to the bridge and
  dropped.
- In `EVT_RESULT`, `NOT_FOUND` is ambiguous (the bridge lost the transfer, or
  the tag refused the `tag_id`): the companion retries it like a link failure
  (new `op_id`, back-off) and escalates only after repeated `NOT_FOUND` for the
  same revision.
- The bridge persists its `result_seq` counter across reboots so the gateway's
  `(bridge, result_seq)` de-duplication never swallows a new result.
- A job whose `FRAME_END` was sent but whose `RESULT` was lost ends
  `DISPLAY_STATE_UNKNOWN` when the next `CHALLENGE` reports the unknown state for
  that `(epoch, revision)`; the companion then re-delivers the same revision and
  the tag repeats the refresh.
- After `FRAME_BEGIN` the bridge waits for that record's credit (or the tag's
  immediate `RESULT`) before streaming plane data.
- Statuses a tag sends **before `AUTH_OK`** are unauthenticated (a plaintext
  `ERROR`, a `CHALLENGE` with an unsupported `proto`, a wrong `mac_t`): anyone
  who can advertise a tag's public id could send them. The bridge treats them
  as link-level failures (back-off, no result) until the same status repeats in
  3 consecutive sessions for that tag and epoch; only then does it end the tag's
  jobs of that epoch with it (`AUTH_FAILED`, `STALE_EPOCH`, `VERSION_MISMATCH`,
  `NOT_FOUND`). Statuses inside authenticated `RESULT` records act at once.
- Session deadlines: the bridge's step deadline advances only on progress (a
  complete handshake message, an authenticated record, or a `CREDIT` with n > 0
  while it waits for credit). A `CREDIT` before `AUTH_OK` ends the session
  (`INVALID`). Every session is also bounded absolutely: the handshake must
  complete within 5 s of the connection, and each frame (from `FRAME_BEGIN` to
  its `RESULT`) within 60 s + 1 s per KiB of plane data + the panel's refresh
  timeout. Expiry ends the session with `TIMEOUT` (a link-level failure).
- Plane data for a frame is streamed only after the tag has granted a credit
  for **that** `FRAME_BEGIN` record (credits are counted per record, never
  carried over from a previous record or job).
- Link-level failures (`DISCONNECTED`, `TIMEOUT`,
  `CONNECT_FAILED`, `MESH_SUSPEND_FAILED`, `MESH_RESUME_FAILED`) are retried after
  the per-tag back-off and never produce a result on their own; the companion's
  job TTL bounds them.
- A successful `CLEAR` resets the bridge's history for that tag to revision 0,
  mirroring the tag, so a later delivery of the previously shown revision is
  drawn again rather than answered from history.
- `ASSIGN_DEL` of an absent assignment answers `OK`.
- Tag commands `IDENTIFY` and `REFRESH` are companion-level (a new revision); a
  tag that receives them answers `UNSUPPORTED`.

**Font flash**

- `FLASH_TEST` reports `BUSY` for any test position inside the active slot or the
  slot directory, and tests the rest.

**Known limitation.** `EVT_RESULT`/`DELIVERY_RESULT` carry no "stored ACK re-sent"
flag, so the tag's duplicate flag (`RESULT.flags.bit0`) does not reach the
companion; a duplicate is reported as `OK`.
