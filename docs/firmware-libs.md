# Firmware libraries

Portable C99 libraries under `lib/` shared by the gateway, bridge and tag
applications. Every library works on caller-provided buffers, allocates
nothing, keeps no global state (only `const` tables) and is reentrant per
context struct. Public headers are `include/ctag/ctag_*.h` (include them as
`<ctag/ctag_frame.h>`); the generated `proto_ids.h` / `proto_msgs.h` supply the
protocol constants, status codes and fixed-layout codecs the libraries build
on. Behaviour mirrors the companion's Python reference implementation, which
is the oracle: every library is tested against `protocol/fixtures/`.

Conventions:

- Protocol outcomes are `enum ctag_status` values returned as `uint8_t`
  (`CTAG_STATUS_OK` = 0); API misuse, sizes and I/O are negative errno values
  (`-EINVAL`, `-EMSGSIZE`, `-EBADMSG`, `-EIO`, `-ENOENT`, `-EAGAIN`).
- Multi-byte values are read and written byte by byte (no unaligned access,
  no host-endian assumptions); arithmetic that can exceed 16 bits (glyph pens,
  Bresenham error terms, rectangle edges) is `int32_t`, the progress fill
  product `uint32_t`.
- Only `ctag_cbor` (zcbor) and the PSA backend of `ctag_session` need Zephyr;
  everything else also builds on the host.
- `ctag_secure` (protocol v2) is the exception to "allocates nothing": the
  vendored Noise* allocates, so the library owns one bounded heap and one
  RNG provider, and is used by one thread at a time
  ([below](#ctag_secure--protocol-v2-secure-endpoint-connect-setupmd-25)).

## Overview

| Library | Kconfig | Headers | Used by |
|---|---|---|---|
| `ctag_frame` | `CONFIG_CTAG_FRAME` | `ctag_frame.h` | gateway, bridge maintenance port |
| `ctag_cbor` | `CONFIG_CTAG_CBOR` | `ctag_cbor.h` | gateway, bridge maintenance port |
| `ctag_layout` | `CONFIG_CTAG_LAYOUT` | `ctag_layout.h` | bridge |
| `ctag_render` | `CONFIG_CTAG_RENDER` | `ctag_render.h`, `ctag_fontpack.h` | bridge |
| `ctag_frag` | `CONFIG_CTAG_FRAG` | `ctag_frag.h` | bridge, tag |
| `ctag_session` | `CONFIG_CTAG_SESSION` | `ctag_session.h`, `ctag_crypto.h` | bridge (client), tag (server) |
| `ctag_txn` | `CONFIG_CTAG_TXN` | `ctag_txn.h` | tag |
| `ctag_enroll` | `CONFIG_CTAG_ENROLL` | `ctag_enroll.h` | tag |
| `ctag_secure` | `CONFIG_CTAG_SECURE` | `ctag_secure.h` | gateway (protocol v2); the bridge and tag endpoints later |
| `ctag_crc32` | selected (`CONFIG_CTAG_CRC32`) | `ctag_crc32.h` | frame, render, txn, enroll, secure |
| `ctag_utf8` | selected (`CONFIG_CTAG_UTF8`) | `ctag_utf8.h` | cbor, render |

`CTAG_RENDER` selects `CTAG_LAYOUT`; `CTAG_CBOR` selects `ZCBOR` and
`ZCBOR_CANONICAL`; `CTAG_SESSION` depends on `PSA_CRYPTO`. The CRC-32
implementation is a choice: `CONFIG_CTAG_CRC32_BITWISE` (no table, default
unless `CTAG_FRAME` or `CTAG_RENDER` is enabled), `CONFIG_CTAG_CRC32_NIBBLE`
(16-entry table, about four times faster; default on gateway and bridge) or
`CONFIG_CTAG_CRC32_ZEPHYR` (`crc32_ieee_update()`).

## Measured cost

NCS v3.4.1 container, `zephyr/gnu` (GCC 14.3), Zephyr's `-Os`, built by
[`tests/size`](../tests/size) for `qemu_cortex_m0` (nRF51, Cortex-M0) and
`nrf52840dk/nrf52840` (Cortex-M4F). Flash in bytes. **Library** = all objects
of the library; **linked** = what the link kept when the image calls exactly
the API of the role (`ctag.size.tag`: the tag's API only; bridge libraries from
`ctag.size.all`). No library has `.data` or `.bss`: RAM is only the context
structs and buffers the application declares (sizes for 32-bit ARM below).
Frames are the largest single stack frame of the library's functions
(`CONFIG_STACK_USAGE`, Cortex-M0), callees excluded.

| Library | M0 library | M0 linked | M4 library | M4 linked | Context structs (M0) | Largest frame (M0) |
|---|---:|---:|---:|---:|---|---|
| `ctag_crc32` (bitwise / nibble) | 48 / 116 | 48 | 44 / 116 | 44 | — | 16 B |
| `ctag_utf8` | 154 | 154 | 148 | 148 | `ctag_utf8` 4 | 32 B |
| `ctag_frame` (cobs + serial) | 764 (382 + 382) | 764 | 706 (372 + 334) | 706 | `ctag_serial_rx` 36, `ctag_cobs_decoder` 24, `ctag_credits` 4 | 48 B `frame_build` |
| `ctag_cbor` (excl. zcbor) | 2142 | 2142 | 2120 | 2120 | `ctag_cbor_field` 16 each | 432 B `decode_payload` (zcbor states), 416 B `well_formed` (key stack) |
| `ctag_layout` (validator + assembler) | 1448 (1098 + 350) | 1448 | 1280 (920 + 360) | 1280 | `ctag_layout_asm` 72, `ctag_layout_iter` 16 | 136 B `validate` |
| `ctag_render` (render + fontpack + qrcodegen) | 8912 (2410 + 1482 + 5020) | 8554 | 8708 (2290 + 1400 + 5018) | 8392 | `ctag_render` 36, `ctag_render_work` 920, `ctag_fontpack` 80 | 488 B `fontpack_open`, 200 B `render_strip` |
| `ctag_frag` | 206 | 206 | 194 | 194 | `ctag_frag_rx` 12, `ctag_frag_tx` 1 | 24 B |
| `ctag_session` (session + PSA backend) | 2178 (1752 + 426) | 1748 (tag) | 2350 (1932 + 418) | 1834 (tag) | `ctag_session` 124 | 280 B `transcript` (PSA hash op) |
| `ctag_txn` | 614 | 614 | 606 | 606 | `ctag_txn_record` 64 | 72 B `load`/`save` |
| `ctag_enroll` | 142 | 142 | 128 | 128 | `ctag_enrollment` 48 | 24 B |

**Tag total** (frag + session tag role + txn + enroll + bitwise CRC):
**2758 B** on Cortex-M0, 2806 B on Cortex-M4, excluding PSA itself (the lean
PSA profile's cost is part of the baseline in
[`firmware-notes.md`](firmware-notes.md) §12). Tag RAM for the libraries:
`ctag_session` 124 + two `ctag_frag_rx` 24 + `ctag_txn_record` 64 B plus the
application's two `TAG_RECORD_BUF` buffers, its plaintext buffer and its PSA
hash operation for the plane digest.

Stack, deepest own chains on the tag (Cortex-M0): `ctag_session_tag_hello`
(40) → `transcript` (280, holds the PSA hash operation) → PSA SHA-256;
`ctag_session_tag_hello` → `ctag_session_k_epoch` (96) → `hkdf` (80) →
`ctag_crypto_hmac_sha256` (40) → `psa_mac_compute`. Firmware-notes §5 measured
HKDF through `psa_mac_compute` at 936 B and SHA-256 streaming at 376 B high
water on Cortex-M0 with Oberon, so a handshake step needs about 1 KiB of the
calling thread's stack; records (`ctag_record_seal/open` 72 B + CCM) less.
Re-measure with `CONFIG_THREAD_ANALYZER` on hardware. On the bridge the own
frames of `ctag_render_strip` (200) → `qrcodegen_encodeText` (80) →
`qrcodegen_encodeSegmentsAdvanced` (112) → mask evaluation (≤ 48 each) sum to
about 500 B, plus the glyph source's `read` callback on the glyph path;
`ctag_fontpack_open` peaks at 488 B. `ctag_cbor_decode` (32) runs its two
phases out of line, one after the other, so their frames never add up: the
well-formedness scan, `well_formed` (416, the key stack) + at most 10 levels
of `scan` (72 each) = 1168 B, the peak the scan had before the key stack
existed (448 + 10 × 72); then the typed decoding, `decode_payload` (432, the
zcbor states) + `decode_map` (80) per nested known map + zcbor's skip.

## ctag_frame — serial framing (docs/protocol.md §1.1, §1.3)

```c
int ctag_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t size);
int ctag_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t size);
void ctag_cobs_decoder_init(struct ctag_cobs_decoder *d, uint8_t *buf, size_t size);
int ctag_cobs_decoder_put(struct ctag_cobs_decoder *d, uint8_t byte); /* len >= 0 or -EAGAIN */

int ctag_serial_frame_build(uint8_t *frame, size_t size, const struct ctag_serial_header *hdr,
                            const uint8_t *payload /* NULL = already at frame + 8 */, size_t payload_len);
enum ctag_serial_check ctag_serial_frame_check(const uint8_t *frame, size_t len, struct ctag_serial_header *hdr);
int ctag_serial_wire_encode(const uint8_t *frame, size_t len, uint8_t *wire, size_t size); /* COBS + 0x00 */
void ctag_serial_rx_init(struct ctag_serial_rx *rx, uint8_t *buf, size_t size);
bool ctag_serial_rx_put(struct ctag_serial_rx *rx, uint8_t byte, struct ctag_serial_header *hdr);
```

- Encoder: no trailing `0x01` after a final `0xFF` block; `CTAG_COBS_MAX_ENCODED(n)`
  bounds the output. The decoders accept either form.
- Stream decoder: consecutive delimiters are ignored, a frame growing past the
  buffer is discarded up to the next `0x00` (`oversize`), a delimiter inside a
  block counts in `errors`. The completed frame stays in the buffer until the
  next byte is fed.
- `ctag_serial_frame_check()` applies the §1.1 order: decoded size in
  `12..SERIAL_MAX_FRAME` (`LEN`), CRC (`CRC`), `length` = size − 12 (`LEN`),
  `version` (`VERSION`). `ctag_serial_rx` combines the two and keeps
  `len_errors`, `crc_errors`, `version_errors`; the payload of an accepted
  frame is at `rx.cobs.buf + CTAG_SERIAL_HEADER_LEN`, `hdr.length` bytes.
- Build responses in place: encode the CBOR payload at
  `frame + CTAG_SERIAL_HEADER_LEN`, then `ctag_serial_frame_build(frame, size,
  &hdr, NULL, n)` and `ctag_serial_wire_encode()`.
- Credits (inline): `ctag_credits_reset()` after `HELLO`,
  `ctag_credits_take()` before each send (false = wait), `ctag_credits_add()`
  with the `credits` byte of every received frame, `ctag_credits_release()`
  when a receive buffer is freed, `ctag_credits_grant()` for the `credits`
  byte of the next frame sent.

## ctag_cbor — serial payload maps (§1.1–§1.2; Zephyr + zcbor)

```c
struct ctag_cbor_field { uint8_t key; uint8_t kind; bool present; union { u, i, b, str, map, maps, counters } v; };
int ctag_cbor_encode(const struct ctag_cbor_field *fields, size_t count, uint8_t *buf, size_t size);
int ctag_cbor_decode(const uint8_t *buf, size_t len, struct ctag_cbor_field *fields, size_t count);
int ctag_cbor_maps(const struct ctag_cbor_str *maps, struct ctag_cbor_str *items, size_t max);
int ctag_cbor_counters(const struct ctag_cbor_str *counters, struct ctag_cbor_counter *items, size_t max);
uint8_t ctag_cbor_key_kind(uint32_t key);
```

- Encode takes the fields in any order and emits canonical CBOR: keys
  ascending, shortest integers and lengths, definite lengths (requires
  `CONFIG_ZCBOR_CANONICAL`); nested `CTAG_CBOR_MAP` (caps, timing),
  `CTAG_CBOR_MAPS` (nodes, items, assigned) and `CTAG_CBOR_COUNTERS` (text →
  uint32, sorted shorter-first then bytewise, use `CTAG_CBOR_COUNTER("name",
  v)`). Each field's kind, range and size must match its key (the key table of
  `cbor_msgs.py`); a repeated key, a wrong kind or an unknown key is `-EINVAL`,
  a short buffer `-EMSGSIZE`. An empty field set is an empty payload.
- Decode: set `key` of each wanted field; `kind` and `present` and `v` are
  filled. The payload must be well formed as `cbor_msgs.py` demands (definite
  lengths, shortest forms, no tags or floats, `false`/`true` the only simple
  values, UTF-8 text, no duplicate keys in any map, nesting ≤ 8); every known
  key anywhere is type-checked, unknown keys are skipped, keys may come in any
  order. Nested values are returned as the span of their encoding: pass a
  `CTAG_CBOR_MAP` span to `ctag_cbor_decode()` again, split `CTAG_CBOR_MAPS`
  with `ctag_cbor_maps()`, read `CTAG_CBOR_COUNTERS` with
  `ctag_cbor_counters()`. Byte and text strings point into the payload.
- Decode work is linear in the payload. The scan visits each item once and
  compares a key only with the earlier keys of its own map, which it records
  as (start, end) offsets on a 96-entry stack shared by the maps being
  scanned (a map and the maps it is nested in). Beyond `cbor_msgs.py`, a
  payload is `-EBADMSG` when a map's keys plus the keys its enclosing maps had
  read before it exceed 96, when it holds more than 512 map entries in all, or
  when it is longer than 65535 bytes (16-bit offsets). Every spec message fits:
  at most 72 keys held at once (INFO: 8 top-level keys before a counters map
  of up to 64, `MAINT_COUNTERS` and the gateway's `MAX_COUNTERS`) and 383 map
  entries (GET_INVENTORY with 5 bridges and `CONFIG_CTAG_GW_ASSIGN_MAX` = 128).
  A protocol v2 gateway (`MAX_COUNTERS` 72, three more keys per inventory
  item) stays within the limits: at most 80 keys held at once and 398 entries.
  The old scan re-scanned every earlier entry, nested maps included, for each
  new key: N^depth work. A 680-byte payload of 7 nested levels × 32 entries
  did not finish in 84 s on a desktop; it now takes 3921 steps.

## ctag_layout — validator, iterator, assembler (§3.2–§3.3, §4.1–§4.3)

```c
uint8_t ctag_layout_validate(const uint8_t *data, size_t len, ctag_layout_has_strike_fn has_strike, void *ctx);
uint8_t ctag_layout_check_panel(const struct ctag_layout_header *hdr, uint16_t native_width, uint16_t native_height);
int ctag_layout_iter_init(struct ctag_layout_iter *it, const uint8_t *data, size_t len, struct ctag_layout_header *hdr);
bool ctag_layout_iter_next(struct ctag_layout_iter *it, struct ctag_layout_command *cmd);
void ctag_layout_glyph_at(const struct ctag_layout_command *cmd, uint8_t i, struct ctag_layout_glyph *g);

void ctag_layout_asm_init(struct ctag_layout_asm *a, uint8_t *buf, size_t size); /* buf: LAYOUT_HARD_MAX */
void ctag_layout_asm_begin(struct ctag_layout_asm *a, const struct ctag_mesh_layout_begin *b);
uint8_t ctag_layout_asm_chunk(struct ctag_layout_asm *a, const struct ctag_mesh_layout_chunk *c);
uint8_t ctag_layout_asm_commit(struct ctag_layout_asm *a, uint16_t xfer_id, uint32_t *missing,
                               const struct ctag_sha256_ops *sha);
```

- `ctag_layout_validate()` performs the §4.3 checks in their order and returns
  the first failing status; the strike step (5) runs only with a
  `has_strike` callback (`ctag_glyph_source.has_strike` of the font pack fits).
  The QR version-10 fit rule is not evaluated: with `LAYOUT_QR_MAX_TEXT` = 96
  it cannot fail (§4.3). The render-cost bounds are part of step 3: every
  `LINE` endpoint in `[−W, 2W) × [−H, 2H)` (checked with the field bounds, in
  field order), at most `LAYOUT_MAX_QR` QR commands and at most
  `LAYOUT_MAX_LINE_STEPS` Bresenham steps over all lines (running totals,
  `INVALID`), so a valid layout never makes the renderer walk more than
  16384 line plots per strip or encode more than four QR symbols per frame.
- The iterator is for validated layouts; `cmd->var` points at the glyph entries
  (`ctag_layout_glyph_at()`) or the QR text; `cmd->offset` is the op byte's
  offset.
- The assembler holds one transfer: `begin` replaces any transfer in progress;
  `chunk` stores chunk `index` at `index × 150` (returns `NOT_FOUND` for another
  `xfer_id`, `INVALID` for an index outside the transfer; the chunk is then
  ignored); `commit` runs the first four §3.3 checks — `NOT_FOUND`,
  `INCOMPLETE` with the `missing` bitmap, `TOO_LARGE`/`INVALID`, and
  `DIGEST_MISMATCH` through the caller's SHA-256 — and leaves the layout in
  `buf` (`begin.total_len` bytes) until the next `begin`, so a repeated commit
  answers the same. The bridge then checks assignment, epoch, revision and font
  pack and calls `ctag_layout_validate()`.
- `struct ctag_sha256_ops {init, update, finish, ctx}` is the hash interface
  of `ctag_layout` and `ctag_render`; on Zephyr wrap `ctag_crypto_sha256_*()`.

## ctag_render — strip renderer and font packs (§4.4, docs/fontpack.md)

```c
uint8_t ctag_render_init(struct ctag_render *r, const uint8_t *layout, size_t len,
                         const struct ctag_render_panel *panel, const struct ctag_glyph_source *glyphs,
                         struct ctag_render_work *work);
int ctag_render_strip(struct ctag_render *r, uint8_t plane, uint16_t y0, uint16_t rows, uint8_t *out, size_t size);
int ctag_render_frame_digest(struct ctag_render *r, uint8_t *buf, size_t size,
                             const struct ctag_sha256_ops *sha, uint8_t digest[32]);

uint8_t ctag_fontpack_open(struct ctag_fontpack *fp, ctag_fontpack_read_fn read, void *ctx, uint32_t avail);
uint8_t ctag_fontpack_verify_content(struct ctag_fontpack *fp, const struct ctag_sha256_ops *sha,
                                     uint8_t *buf, size_t size);
void ctag_fontpack_glyph_source(struct ctag_fontpack *fp, struct ctag_glyph_source *src);
```

- `ctag_render_init()` validates the layout (§4.3 including strikes and the
  render-cost bounds), the panel geometry rule of §4.4 and `planes` ∈ {1, 2},
  and returns the first failing status. The layout, the glyph source and
  `work` must stay unchanged while rendering (`work` keeps QR symbols across
  strips; every `ctag_render_init()` starts the cache empty).
- **QR cache.** `ctag_render_work` holds `CONFIG_CTAG_RENDER_QR_SLOTS`
  (1..`LAYOUT_MAX_QR`, default 4; `CTAG_RENDER_QR_SLOTS` on the host)
  encoded symbols of 408 bytes plus one 408-byte scratch buffer. With a slot
  per QR command every symbol is encoded once per frame (pre-pass and
  streaming included). With fewer, the first commands keep their slots and
  the rest share the last one, re-encoded for each strip they reach — with
  the mask the automatic choice picked the first time (read back from the
  symbol's format bits), which yields the same symbol without the eight-mask
  penalty search. A per-command memo (offset, symbol size, mask; 16 bytes)
  also culls strips by the symbol's real size instead of version 10's 57
  modules, and painting visits only the module rows and columns that reach
  the strip. Tested bit for bit with one slot (`ctag_host_tests_qr1`,
  `ctag.host_suites.qr_one_slot`: four `qr.json` symbols side by side in
  one-row strips) and with four.
- `ctag_render_strip()` writes native rows `[y0, min(y0 + rows, height))` of
  one plane, `row_bytes = ceil(width / 8)` per row, exactly the bytes of those
  rows of a full frame. Each command paints logical rectangles clipped to the
  canvas and to the logical region of the strip (rotation 0: `y ∈ [y0, y1)`;
  1: `x ∈ [y0, y1)`; 2: `y ∈ [Hn−y1, Hn−y0)`; 3: `x ∈ [Hn−y1, Hn−y0)`), mapped
  straight into plane bits; lines always walk their full Bresenham path
  (bounded by `LAYOUT_MAX_LINE_STEPS`).
  Glyphs whose box cannot reach the strip are skipped before their index entry
  is read; bitmap rows are read one row at a time through `read`.
- `ctag_render_frame_digest()` is the bridge's pre-pass for `FRAME_BEGIN.digest`:
  plane 0 then plane 1 rendered into `buf` (as many whole rows as fit; use
  `BRIDGE_STRIP_ROWS × row_bytes`).
- Glyph sources implement `has_strike`, `glyph` (`-ENOENT` for a missing strike
  or an out-of-range id: the glyph is not drawn) and `read`. The font-pack
  source reads through `ctag_fontpack_read_fn` (external flash), binary-searches
  the strike table and remembers the last strike.
- `ctag_fontpack_open()` runs the boot-time validation in the order of
  `fontpack/format.py`: header magic (`INVALID`), version / header size
  (`UNSUPPORTED`), header CRC (`CRC_ERROR`), `total` within `avail`, every table
  inside the pack, faces sorted with NUL-terminated UTF-8 name and scripts
  strings, strikes sorted and naming an existing face, each glyph index inside
  the pack with a matching CRC (`CRC_ERROR`) and every non-empty bitmap inside
  the bitmap area (`INVALID`). Read failures are `STORAGE_ERROR`.
  `ctag_fontpack_verify_content()` adds the install-time SHA-256 check
  (`DIGEST_MISMATCH`).
- QR codes use the vendored Nayuki qrcodegen v1.8.0
  ([`lib/third_party/qrcodegen`](../lib/third_party/qrcodegen), MIT): the text
  is copied into a NUL-terminated buffer and encoded with
  `qrcodegen_encodeText(text, tmp, qr, ecc, 1, 10, qrcodegen_Mask_AUTO, true)`
  into a slot of `ctag_render_work` (a re-encode passes the remembered mask
  instead of `AUTO`: the same symbol).

## ctag_frag — GATT fragmentation (§5.3)

```c
int ctag_frag_next(struct ctag_frag_tx *tx, const uint8_t *msg, size_t len, size_t *off,
                   size_t max_payload, uint8_t *out); /* ATT value length, or -EINVAL when done */
void ctag_frag_rx_init(struct ctag_frag_rx *rx, uint8_t *buf, uint16_t max_msg);
int ctag_frag_rx_put(struct ctag_frag_rx *rx, const uint8_t *value, size_t len); /* msg len, 0, or -EINVAL */
```

One `ctag_frag_tx` and one `ctag_frag_rx` per characteristic and direction,
initialised on connect. `max_payload` is ATT value − 1 (`FRAG_PAYLOAD_MAX` at
the baseline MTU); `max_msg` is `TAG_CTRL_MSG_MAX` for CTRL and
`TAG_RECORD_WIRE_MAX` for DATA/STATUS. The reassembler aborts (`-EINVAL`, the
session ends with `INVALID`) on an empty fragment, a SEQ gap, `START` inside a
message, a continuation without `START` or an overlong message, and then
refuses everything until re-initialised. `rx->buf` may be switched to the
other record buffer after a message completes.

## ctag_session — handshake and records (§5.4–§5.5)

Bridge (client):

```c
int ctag_session_bridge_hello(struct ctag_session *s, uint32_t tag_id, uint32_t epoch,
                              const uint8_t k_epoch[16], const uint8_t nonce_b[16], uint8_t out[26]);
uint8_t ctag_session_bridge_challenge(struct ctag_session *s, const uint8_t *caps, size_t caps_len,
                                      const uint8_t *msg, size_t len, struct ctag_ctrl_challenge *ch,
                                      uint8_t out[17]); /* -> AUTH */
uint8_t ctag_session_bridge_auth_ok(struct ctag_session *s, const uint8_t *msg, size_t len);
```

Tag (server):

```c
uint8_t ctag_session_tag_hello(struct ctag_session *s, uint32_t tag_id, const uint8_t secret[32],
                               const uint8_t *caps, size_t caps_len,
                               const struct ctag_ctrl_challenge *ch, const uint8_t *msg, size_t len,
                               uint8_t *out /* 30 */, size_t *out_len); /* -> CHALLENGE or ERROR */
uint8_t ctag_session_tag_auth(struct ctag_session *s, uint32_t stored_epoch, const uint8_t *msg,
                              size_t len, uint8_t *out /* 17 */, size_t *out_len); /* -> AUTH_OK or ERROR */
```

Records and helpers:

```c
int ctag_record_seal(struct ctag_record_dir *d, uint8_t type, const uint8_t *pt, size_t len,
                     uint8_t *out, size_t size);                      /* -> len + 13 */
int ctag_record_open(struct ctag_record_dir *d, const uint8_t *rec, size_t len, uint8_t *type,
                     uint8_t *pt, size_t size);                       /* -> plaintext len, -EBADMSG */
int ctag_session_k_epoch(const uint8_t secret[32], uint32_t tag_id, uint32_t epoch, uint8_t k_epoch[16]);
bool ctag_session_equal(const uint8_t *a, const uint8_t *b, size_t len); /* constant time */
size_t ctag_session_error_pack(uint8_t out[6], uint8_t status, uint32_t stored_epoch);
bool ctag_session_error_unpack(const uint8_t *msg, size_t len, uint8_t *status, uint32_t *stored_epoch);
```

- Messages are the reassembled CTRL messages including their type byte. The
  caller supplies fresh nonces (`ctag_crypto_random()`); the tag supplies the
  CHALLENGE fields (`stored_epoch`, `displayed_rev`, `last_status`,
  `battery_mv`, `flags` — bit0 from `ctag_txn_unknown_pending()`), `proto` is
  set by the library.
- `th = SHA-256(CAPS ‖ HELLO ‖ CHALLENGE)` (§5.4): both roles pass the CAPS
  characteristic value — the bridge the bytes it read, the tag the bytes it
  serves (the tag app builds them on the stack at HELLO, keeping nothing in
  RAM). A missing CAPS (`caps_len` 0) is refused (`INTERNAL`), never hashed as
  an empty prefix.
- Tag `HELLO` checks, in order: message (`INVALID`), `tag_id` (`NOT_FOUND`),
  `proto` (`VERSION_MISMATCH`), `epoch ≥ stored_epoch` (`STALE_EPOCH`). Any
  `AUTH` failure is `AUTH_FAILED`. On failure `out` holds
  `ERROR{status, stored_epoch}` (6 bytes; the `stored_epoch` of `ch` at
  HELLO, the argument at AUTH) to send, and the session is wiped
  (`CTAG_SESSION_FAILED`). After `AUTH_OK`, `s->epoch` is authenticated:
  persist it when above `stored_epoch`. Counting consecutive failures for the
  wake-window pacing is the tag app's; its own `ERROR`s use
  `ctag_session_error_pack()`.
- The bridge returns the tag's `ERROR` status when one arrives instead of
  `CHALLENGE`/`AUTH_OK` (at `CHALLENGE`, `*ch` then holds only the ERROR's
  `stored_epoch`), `AUTH_FAILED` when `mac_t` does not verify.
- Once established, `s->tx` and `s->rx` are the record directions (bridge:
  tx = B2T; tag: tx = T2B); `K_epoch` and the handshake secrets are wiped.
  Records need `13 ≤ len ≤ TAG_RECORD_WIRE_MAX`, the expected counter and a
  valid MIC, else `-EBADMSG` (`AUTH_FAILED`); the counter advances only on
  success. Plaintext and record buffers must not overlap.
- HKDF is two `HMAC-SHA256` calls (every output is ≤ 32 bytes), never the PSA
  key-derivation API; MAC comparisons are constant time; intermediate keys are
  wiped with volatile stores.

### ctag_crypto.h and the PSA backend

`ctag_crypto_init()`, `ctag_crypto_sha256_init/update/finish/abort()`,
`ctag_crypto_hmac_sha256()`, `ctag_crypto_ccm8(encrypt, key, nonce, aad,
aad_len, in, len, out)` and `ctag_crypto_random()`, implemented by
`lib/session/crypto_psa.c` (`CONFIG_CTAG_CRYPTO_PSA`, selected by
`CTAG_SESSION`): every key is imported as a volatile PSA key and destroyed
after the one operation (one key slot suffices), CCM uses
`PSA_ALG_AEAD_WITH_SHORTENED_TAG(PSA_ALG_CCM, 8)`, random bytes come from
`sys_csrand_get()`. The lean profile of firmware-notes §12 is enough:
`CONFIG_PSA_CRYPTO`, `PSA_WANT_ALG_HMAC`, `PSA_WANT_ALG_SHA_256`,
`PSA_WANT_ALG_CCM`, `PSA_WANT_KEY_TYPE_AES`, `PSA_WANT_KEY_TYPE_HMAC`
(+ `PSA_WANT_AES_KEY_SIZE_128` with nrf_security),
`PSA_WANT_GENERATE_RANDOM=n`, `MBEDTLS_PSA_KEY_SLOT_COUNT=2`. Call
`ctag_crypto_init()` once at boot. The tag's plane digest (§5.6) uses the same
incremental SHA-256.

## ctag_txn — display transaction (§5.6, §6)

```c
uint8_t ctag_txn_frame_begin(const struct ctag_txn_record *stored /* NULL = none */, uint32_t epoch,
                             const struct ctag_rec_frame_begin *fb, uint8_t panel_planes, uint16_t panel_plane_len);
bool ctag_txn_boot(struct ctag_txn_record *rec);             /* true = persist */
bool ctag_txn_unknown_pending(const struct ctag_txn_record *rec); /* CHALLENGE flags.bit0 */
void ctag_txn_intent(struct ctag_txn_record *rec, uint32_t tag_id, uint32_t epoch, uint32_t revision,
                     uint64_t update_id, const uint8_t digest[32]);
void ctag_txn_complete(struct ctag_txn_record *rec, uint8_t status);
void ctag_txn_result(const struct ctag_txn_record *rec, uint8_t flags, struct ctag_rec_result *res);
int ctag_txn_load(const struct ctag_txn_store *store, struct ctag_txn_record *rec); /* 1, 0, -EIO, -EBADMSG */
int ctag_txn_save(const struct ctag_txn_store *store, const struct ctag_txn_record *rec);
void ctag_txn_record_encode(const struct ctag_txn_record *rec, uint8_t out[60]);
int ctag_txn_record_decode(struct ctag_txn_record *rec, const uint8_t *in, size_t len);
```

- `ctag_txn_frame_begin()` applies the §5.6 table, first matching row wins:
  `CTAG_TXN_ACCEPT` → `begin_frame()`; `STALE_REVISION`, `REVISION_CONFLICT`,
  `INVALID` → answer that `RESULT`; `OK` → re-send the stored `RESULT OK` with
  `flags.bit0` (`ctag_txn_result(rec, 1, &res)`).
- The transaction of §6: `ctag_txn_intent()` + `ctag_txn_save()` before
  `commit_refresh()`; after `wait_refresh_complete()`,
  `ctag_txn_complete(rec, OK)` + save, then send `RESULT`. A failed refresh
  (`REFRESH_TIMEOUT`, `PANEL_ERROR`) keeps `REFRESH_INTENT` with that status.
  At boot, `ctag_txn_load()` then `ctag_txn_boot()` (persist when it returns
  true). `CMD{CLEAR}` is an intent with revision 0 and the digest of white.
- Persisted record (one NVS entry, 60 bytes, little-endian): `version u8` (1),
  `state u8` (0 `DISPLAYED`, 1 `REFRESH_INTENT`), `status u8`, `reserved u8`,
  `tag_id u32`, `epoch u32`, `revision u32`, `update_id u64`, `digest[32]`,
  `crc32 u32` over the first 56 bytes. `ctag_txn_store.read` returns bytes read
  or `-ENOENT` (as `nvs_read()` does); a record of any other length, version,
  state or CRC is `-EBADMSG`.

## ctag_enroll — enrollment blob (§9)

```c
uint8_t ctag_enroll_parse(const uint8_t *blob, size_t len, struct ctag_enrollment *out);
```

Checks, as `enrollment.py` does: length 48, magic `'CTAG'`, CRC-32 over bytes
`[0, 44)` (computed here; the generated unpack does not), version 1. Returns
`OK` or `SECURITY_CONFIG` with `*out` zeroed. Pass the UICR address directly,
e.g. `(const uint8_t *)&NRF_UICR->CUSTOMER[0]`; an erased UICR fails the magic.

## ctag_secure — protocol v2 secure endpoint (connect-setup.md §2–§5)

The device side of protocol v2 for every role: identity, the key schedule,
canonical grants and the device's grant rules, the ownership record,
secure-message framing, tunnel fragments, the Noise IK responder and the v2
secure messages. It mirrors the companion's `cremind_tag.secure`
(`identity.py`, `grants.py`, `noise.py`, `messages.py`, `device.py`) and is
tested against `protocol/fixtures/v2_secure.json`. Primitives are the
formally verified HACL* and the handshake the verified Noise* IK
([`lib/third_party/hacl`](../lib/third_party/hacl/README.md),
[`lib/third_party/noise_ik`](../lib/third_party/noise_ik/README.md): upstream
commits, licences, the one patch).

```c
/* primitives, identity, key schedule */
void ctag_secure_sha256(...); void ctag_secure_hmac_sha256(...);
int  ctag_secure_hkdf(ikm, ikm_len, salt, salt_len, info, info_len /* <= 63 */, out, len /* 1..32 */);
void ctag_secure_x25519_public(priv, pub);
bool ctag_secure_grant_sig_ok(authority_pub, grant, len, sig);   /* Ed25519 over "cremind-tag/v2/grant" | grant */
bool ctag_secure_equal(a, b, len);                                /* constant time */
void ctag_secure_wipe(p, len);
void ctag_secure_device_id(role, ik_pub, out16); uint32_t ctag_secure_short_id(device_id);
void ctag_secure_authority_id(...); ctag_secure_k_setup(...); ctag_secure_static_oob(...);
void ctag_secure_k_epoch(root, tag_id, epoch, out16);             /* K_epoch v2 */
void ctag_secure_proof_s(...); ctag_secure_proof_d(...); ctag_secure_root_proof(...); ctag_secure_maint_proof(...);
void ctag_secure_setup_payload(role, short_id, secret, out15);
/* grants */
int  ctag_grant_encode(const struct ctag_grant *g, uint8_t *buf, size_t size);
int  ctag_grant_decode(const uint8_t *raw, size_t len, struct ctag_grant *g);   /* canonical only */
uint8_t ctag_grant_check(const struct ctag_grant_ctx *c, grant, len, sig, sig_len, struct ctag_grant *g, bool *decoded);
/* ownership record (176 bytes, CRC-32) */
void ctag_owner_record_encode(r, out); int ctag_owner_record_decode(r, in, len);
bool ctag_owner_record_load(r, in, len, gen_floor);              /* the boot rule */
/* secure messages, tunnel fragments */
void ctag_secure_hdr_pack(h, out4); int ctag_secure_hdr_unpack(h, in, len);
int  ctag_tunnel_frag(len, off, &seq, &flags); void ctag_tunnel_rx_init(...); int ctag_tunnel_rx_feed(...);
/* Noise IK (Noise*) */
int  ctag_noise_accept(nz, s_priv, prologue, plen, msg1, len, msg2, rs, h);      /* responder */
int  ctag_noise_connect(...); int ctag_noise_finish(...);                         /* initiator (tests, tools) */
int  ctag_noise_seal(nz, pt, len, out, size); int ctag_noise_unseal(nz, ct, len, &pt, &pt_len);
/* the endpoint (device.py SecureDevice) */
void ctag_secure_keys_init(k, role, ik_priv, factory_secret, board, fw...);
void ctag_secure_ep_init(ep, keys, rec, ops /* persist, random */);
int  ctag_secure_draw_challenge(ep); int ctag_secure_ident2(ep, out);
int  ctag_secure_open(ep, link, msg1, len, msg2);                 /* SECURE_OPEN / tunnel HANDSHAKE */
int  ctag_secure_seal(ep, ...); int ctag_secure_unseal(ep, ...); void ctag_secure_close(ep);
bool ctag_secure_controller_match(ep);
void ctag_secure_handle(ep, type, const struct ctag_secure_req *req, struct ctag_secure_answer *ans);
int  ctag_secure_answer_encode(ans, buf, size);                   /* canonical CBOR answer */
```

- **Grants.** `ctag_grant_decode` accepts exactly the canonical CBOR of
  `Grant.encode()` (a map of the keys 0–9 in order, shortest integers, exact
  byte-string lengths, version 2, a known op and role, ≤ `GRANT_MAX`), so the
  signed bytes and the checked fields cannot differ. `ctag_grant_check` runs
  the rules in `check_grant`'s order — decode, signature length, device /
  role / op, the challenge (constant time), `STALE_GENERATION`, the
  controller, the Ed25519 signature, then ownership and the setup proof — and
  returns the first failure's status; every `grant_cases` fixture passes.
- **Endpoint.** `ctag_secure_handle` implements `device.py` rule for rule:
  STATUS (the owner only to the pinned controller), CLAIM and RECOVER
  (gateway), RELEASE (the tag's two stages: stage 0 draws a pending override
  secret, stage 1 by the same controller releases; a REKEY clears a pending
  release), PAIR (setup proof and `proof_d`; `LOCKED`, failure counter
  `CTAG_SECURE_MAX_FAILURES` behind `ctag_secure_pairing_paused`), REKEY,
  MAINT_AUTH and RECOMMISSION (bridge, over the serial link only: `NOT_OWNER`
  through a tunnel; RECOMMISSION of an UNOWNED bridge is `INVALID`). A single
  challenge is used up by every grant check; `INVALID` without grant or
  signature keeps it. A new record goes through `ops.persist` first:
  `STORAGE_ERROR` when it fails, and the record in RAM does not change. The
  answer (`struct ctag_secure_answer`) carries the status, the answer fields
  and the side effects the application acts on (`released`,
  `recommissioned`, `rekeyed`). Secrets on the stack are wiped after use.
- **Sessions.** `ctag_secure_open` replaces any session; the session's
  controller is the Noise initiator's static key; `session_serial` changes
  whenever a session opens or closes, so an application can bind queued
  answers to their session. A message that fails to decrypt ends the
  session. The responder of this Noise* instantiation only accepts known
  peers: `noise.c` recovers the initiator's key from message 1 with Noise*'s
  own primitives, registers it and lets the verified state machine
  authenticate the message (one extra X25519; see the Noise* README).
- **Heap.** `KRML_HOST_MALLOC/CALLOC/FREE` go to a `sys_heap` of
  `CONFIG_CTAG_SECURE_HEAP_SIZE` bytes (default 12,288; a first-fit pool of
  `CTAG_SECURE_HEAP_SIZE` in host builds); every block is wiped when freed.
  Each Noise* call runs under a `setjmp` guard: a failed allocation, a failed
  RNG draw or a KaRaMeL abort long-jumps out, the heap is wiped and
  re-initialised, every Noise object of the old heap epoch is dropped, and the
  call returns `-ENOMEM`, `-EIO` or `-EFAULT` — the session fails, never the
  device. `ctag_secure_heap_stats()` gives size, use, peak and failures.
  Measured (host allocator): device 248 B, peer 112 B, responder handshake
  peak +1,096 B leaving a 776-byte session, sealing *n* bytes ≈ +2*n* (4,061 B:
  +8,184 B), opening *n* bytes peaks ≈ 2*n* and holds *n* until
  `ctag_noise_unseal_done`.
- **Randomness.** `sys_csrand_get()` on Zephyr (set another provider with
  `ctag_secure_rng_set`); a failure is `-EIO`, never a weak ephemeral.
  `ctag_secure_test_ephemeral()` fixes the next ephemeral for the byte-exact
  fixture conversation.

Cost in the nRF52840 gateway (`zephyr.map`, `-Os`): HACL* 35.9 KB (X25519
13,962 B, Ed25519 12,800, ChaCha20-Poly1305 4,982, SHA-2 3,692, HMAC 424),
Noise* 7,535, the library itself 7,987; RAM: the heap plus 243 B.

**Stack** (Cortex-M4, GCC 14.3 `-Os`, `-fstack-usage -fcallgraph-info=su`,
worst path): a grant check (`ctag_secure_handle` → `ctag_grant_check` →
`Hacl_Ed25519_verify`) needs **9,188 B** — `Hacl_Ed25519_verify` alone has a
6,432-byte frame (two tables of precomputed points for the double scalar
multiplication); `ctag_secure_open` 3,324 B (X25519: `Field51_fmul2` 1,520);
sealing and opening a transport message are bounded by the same X25519 chain
(≤ 3,020 B, statically). The gateway gives its loop 12 KiB
([gateway-firmware.md §15.11](gateway-firmware.md#1511-resources)). A bridge
or tag that verifies grants with this Ed25519 needs the same ~9.2 KB on the
verifying thread — beyond what an nRF51 or nRF52810/811 tag can spare; those
need a smaller-stack Ed25519 verification (not part of this library yet). The
vendored sources are compiled verbatim with KaRaMeL's configuration
force-included (`lib/secure/ctag_krml.h`) and the warnings the generated code
raises disabled; the library's own sources use the full warning set.
vendored sources are compiled verbatim with KaRaMeL's configuration
force-included (`lib/secure/ctag_krml.h`) and the warnings the generated code
raises disabled; the library's own sources use the full warning set.

## ctag_crc32, ctag_utf8

`uint32_t ctag_crc32(uint32_t crc, const void *data, size_t len)` has zlib
semantics (start with 0, chain with the previous result). `ctag_utf8_init()`,
`ctag_utf8_feed()`, `ctag_utf8_valid()` validate UTF-8 incrementally with the
acceptance of Python's `bytes.decode("utf-8")`.

## Tests

| Suite | Where | Fixtures |
|---|---|---|
| CRC-32, UTF-8 | host, ztest | `crc32.json` |
| COBS vectors, decode-only, errors, stream; serial frames, invalid frames, receiver counters; credits | host, ztest | `cobs.json`, `serial_frames.json` |
| Mesh messages through the generated codecs (checks generated from the fixture) | host, ztest | `mesh_msgs.json` |
| Layouts: valid (with digests), every invalid case with its status, panel rule, iterator, assembler | host, ztest | `layouts.json`, `fontpack_test.ctfp` |
| Font pack: header, strikes, glyph samples and bitmaps, corruptions (boot and install status), invalid packs | host, ztest | `fontpack.json`, `fontpack_test.ctfp` |
| QR matrices of the exact §4.4 call | host, ztest | `qr.json` |
| Render: every scenario (plane SHA-256, plane bytes where given, frame digest via the strip pre-pass), strip-vs-full equality for strip heights 1, 3, 7, 16, 37, 64 on every plane | host, ztest | `render.json` |
| Fragments: vectors both ways, error cases, SEQ wrap-around | host, ztest | `fragments.json` |
| FRAME_BEGIN table, boot rule, persisted record, storage, transaction flow | host, ztest | `tag_txn.json` |
| Enrollment | host, ztest | `enrollment.json` |
| Mutation robustness (layouts → renderer, font packs → reader, COBS, fragments) | host, ztest | — |
| Session: K_epoch, handshake from both roles over the fixture CAPS, a relayed CAPS (the bridge's AUTH is the fixture's, the tag refuses it), no CAPS refused, ERROR pack/unpack and the fixture ERROR{STALE_EPOCH, 4}, records both ways, tampered/replayed/skipped records, handshake failures, backend sanity | ztest (PSA via Mbed TLS) | `session.json` |
| CBOR: every fixture payload encoded byte for byte and decoded field for field (nested maps, counters), arrays of maps, strictness rules, encoder rejections; work bounds (the review's nested-map payloads, work counted by `ctag_cbor_steps` under `CTAG_CBOR_STEPS`), scan limits, duplicate keys per map, INFO and GET_INVENTORY at their largest; the v2 keys' kinds | ztest | `serial_frames.json` |
| v2 (`test_secure.c`, 13 tests): identities, the key schedule (HKDF, `k_setup`, `static_oob`, `K_epoch` v2, the proofs, setup payloads), every `grant_cases` rule in order, strict grant decoding (non-canonical forms, lengths, big generations), the byte-exact conversation (ident2, Noise IK messages 1 and 2, `h`, the sealed PAIR request and answer both ways, a replay ending the session), handshake errors, the gateway, tag and bridge rules of `device.py` (single-use challenges, persist failures, STATUS privacy, two-stage release and its cancellation by REKEY, locked bridges, MAINT_AUTH / RECOMMISSION only over serial, recommission of an unowned bridge refused), the ownership record (round trip, the boot rule, every corruption), tunnel fragments and reassembly, heap exhaustion and RNG failure at every allocation of a handshake and a transport message | host, ztest | `v2_secure.json` |

C test vectors are generated from the JSON fixtures at build time by
[`tests/host/gen_vectors.py`](../tests/host/gen_vectors.py) (standard library
only; `tests/host/vectors.cmake`), so they are rebuilt whenever a fixture,
`proto_msgs.h` or the generator changes and cannot go stale.

Host (no dependencies beyond a C compiler, CMake and Python 3):

```bash
cmake -S tests/host -B tests/host/build -G Ninja
cmake --build tests/host/build && ctest --test-dir tests/host/build --output-on-failure
# 32-bit and sanitizer variants (Linux, gcc-multilib):
cmake -S tests/host -B /tmp/h32 -DCMAKE_C_FLAGS=-m32
cmake -S tests/host -B /tmp/hsan -DCMAKE_C_FLAGS="-O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all" \
      -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address,undefined"
```

The libraries and tests build with `-std=c99 -Wall -Wextra -Wpedantic
-Wconversion -Wsign-conversion -Wshadow -Wundef -Wstrict-prototypes
-Wmissing-prototypes -Wcast-qual -Wvla -Werror`; the exceptions are the
vendored qrcodegen, compiled verbatim with the compiler's default warnings,
and the vendored Noise* and HACL* (the defaults minus the unused-variable,
unused-parameter, unused-function and infinite-recursion warnings their
generated code raises, as Noise*'s own Makefile does; the host tests build
them with `KRML_VERIFIED_UINT128`, the portable 128-bit arithmetic the
Cortex-M targets use).
Verified with GCC 15.2 (Windows, x86-64) and GCC 13.3 (Linux: x86-64, `-m32`,
`-O2`, `-Os -m32`, ASan + UBSan).

Zephyr (`native_sim` and `native_sim/native/64`):
[`tests/ztest/session`](../tests/ztest/session),
[`tests/ztest/cbor`](../tests/ztest/cbor) and
[`tests/ztest/host_suites`](../tests/ztest/host_suites) (every host suite
unchanged under ztest, three times: default nibble CRC, bitwise CRC, Zephyr's
CRC). Run as in [`building.md`](building.md):

```bash
west twister -T /work/tests/ztest -p native_sim -p native_sim/native/64 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-ctag
```

### Measuring

[`tests/size`](../tests/size) is a build-only twister app: `ctag.size.tag`
calls the tag's API, `ctag.size.all` adds the gateway/bridge API. Build it and
print the per-library table:

```bash
west twister -T /work/tests/size -p qemu_cortex_m0 -p nrf52840dk/nrf52840 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-ctag-size
python3 /work/tests/size/footprint.py \
  /build/twister-ctag-size/qemu_cortex_m0_nrf51822/zephyr_gnu/work/tests/size/ctag.size.tag
```
