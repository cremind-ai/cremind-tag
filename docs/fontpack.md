# Font packs

Tags contain no font engine. The companion shapes text with HarfBuzz and sends
positioned glyph ids; the bridge composites 1-bpp glyph bitmaps from a **font
pack** stored in its soldered external flash.

## 1. Contents

- One regular face per script covered by the pinned Noto collection, plus the
  CJK regional faces (SC, TC, HK, JP, KR), plus weight variants of a face
  (e.g. Noto Sans Bold) that hosts use only for emphasis; bridges treat every
  face alike. See the host software's
  [`fonts.md`](https://github.com/cremind-ai/cremind/blob/main/docs/tags/fonts.md) and
  its `fonts/manifest.yaml` (packs are built by the host, not in this repository).
- Face 0 is the icon face (ids from `spec.yaml` `icons`).
- Each text face carries strikes at sizes from `FONT_SIZES` (12, 14, 16, 24,
  32 px; a pack may build a subset, and layouts use only the strikes the active
  pack has); the icon face carries `ICON_SIZES` (16, 24, 32, 48 px).
- **Every glyph of every face** (contextual and ligature glyphs included), so
  whatever HarfBuzz produces can be drawn.

Glyphs are rasterised with FreeType: `FT_Set_Pixel_Sizes(face, 0, size)` then
`FT_Load_Glyph(gid, FT_LOAD_RENDER | FT_LOAD_TARGET_MONO)`. `bearing_x =
bitmap_left`, `bearing_y = bitmap_top`, `advance = round(advance.x / 64)`.
Strike metrics: `ascent = ceil(ascender / 64)`, `descent = ceil(−descender /
64)`, `line_height = ceil(height / 64)` from `size->metrics`. Icon glyphs are
rendered into an exact `size × size` cell (centred, zero bearings).

## 2. Binary format (version 1, little-endian)

```
header (128) | face table | strike table | string table | glyph indexes | bitmap area
```

Writers lay the sections out contiguously in this order with no padding (glyph
indexes in strike-table order); readers rely only on the offsets.

### Header (128 bytes)

| Off | Type | Field |
|---:|---|---|
| 0 | u32 | magic `'C''T''F''P'` (0x50465443) |
| 4 | u16 | format version = 1 |
| 6 | u16 | header size = 128 |
| 8 | u32 | flags (0) |
| 12 | u32 | total size of the pack in bytes |
| 16 | bytes[8] | pack id = `content_hash[0:8]` |
| 24 | bytes[32] | `content_hash` = SHA-256 of bytes `[128, total)` |
| 56 | u16 | face count |
| 58 | u16 | strike count |
| 60 | u32 | face table offset |
| 64 | u32 | strike table offset |
| 68 | u32 | string table offset |
| 72 | u32 | string table size |
| 76 | u32 | bitmap area offset |
| 80 | u32 | bitmap area size |
| 84 | bytes[8] | manifest id = SHA-256(the host's `fonts/manifest.lock.json`)[0:8] |
| 92 | bytes[32] | reserved, zero |
| 124 | u32 | CRC-32/IEEE of bytes `[0, 124)` |

### Face record (16 bytes, sorted by face id)

`face_id u16`, `flags u16` (bit0 icon face, bit1 CJK, bit2 RTL script),
`name_off u32` (NUL-terminated UTF-8 in the string table, e.g.
`"Noto Sans Arabic 2.013"`), `scripts_off u32` (NUL-terminated,
comma-separated ISO 15924 codes), `glyph_count u32` (font `numGlyphs`).
`name_off`/`scripts_off` are relative to the string table; writers append the
strings in face-table order (name, then scripts), without de-duplication.

### Strike record (24 bytes, sorted by `(face_id, size_px)`)

`face_id u16`, `size_px u8`, `flags u8`, `glyph_count u32`, `index_off u32`
(absolute), `ascent i16`, `descent i16`, `line_height u16`, `reserved u16`,
`index_crc32 u32` (CRC-32 of the strike's glyph index bytes).

### Glyph index entry (12 bytes, one per glyph id `0..glyph_count−1`)

`bitmap_off u32` (relative to the bitmap area; `0xFFFFFFFF` = empty glyph),
`width u8`, `height u8`, `bearing_x i8`, `bearing_y i8`, `advance u16`,
`reserved u16`.

### Bitmaps

`height` rows of `ceil(width / 8)` bytes, MSB-first, 1 = ink, row padding bits
0. A glyph is empty (`bitmap_off = 0xFFFFFFFF`) exactly when `width = 0` or
`height = 0`; any other glyph stores a bitmap, even one without ink. Strike
`flags` is 0 (reserved). **Identical bitmaps are stored once** (de-duplicated by content across all strikes — CJK
regional faces share most Han glyphs). Bitmaps are appended in order of first
use, iterating strikes in table order and glyph ids ascending, so a pack is a
pure function of the pinned fonts and the generator version (reproducible
builds; CI compares the pack id of two builds).

### Validation (bridge, at install and at boot)

Header magic/version/size/CRC; every table inside `total`; face records sorted
by face id, their name and scripts strings NUL-terminated valid UTF-8 inside the
string table; strike records sorted, each naming an existing face and pointing
inside the pack; index CRCs; every non-empty `bitmap_off + bytes(w,h)` inside
the bitmap area (Cremind's `app/tags/runtime/fontpack/format.py` is the
reference validator). At install the bridge additionally checks
`content_hash` (SHA-256 of bytes `[128, total)`, which reads the whole pack).

## 3. External flash layout (bridge)

```
0                         slot_size              2·slot_size                  flash_size
| slot 0 (pack A)          | slot 1 (pack B)       | working space (≥ 16 MiB)    |
                                                   | dir A | dir B | pending layouts | spare |
```

- `slot_size = align_down_64K((flash_size − FONTPACK_WORKING_SPACE) / 2)`.
- **Capacity rule:** a flash part is acceptable only when
  `flash_size ≥ 2 × erase_aligned(P) + 16 MiB` where `P` is the complete pack
  size including indexes (`erase_aligned` rounds up to the 64 KiB erase block).
  `cremind tags tools fonts size` prints `P`, the rule's result and the smallest
  standard NOR density that satisfies it.
- Slot directory: two 4 KiB sectors at the start of the working space. Each
  holds one 64-byte record: `magic 'CTSL' u32`, `version u16`, `reserved u16`,
  `seq u32`, `slot u8`, `pad[3]`, `pack_id[8]`, `size u32`,
  `content_hash[32]`, `crc32 u32`. The active pack is the valid record with the
  highest `seq`. Activation writes the *other* directory sector with `seq + 1`
  — a power loss at any moment leaves the previous record valid (atomic
  activation).
- Pending layouts: 8 KiB records (validated layout + its delivery metadata)
  written round-robin across the rest of the working space, so accepted work
  survives a bridge reset and no single sector is erased on every delivery.
- Parts above 16 MiB need 4-byte addressing; `FLASH_TEST` (maintenance port)
  writes and reads back patterns at the start, the 16 MiB boundary, and the
  last sector of **the whole device** before a board is qualified.

## 4. Installation (maintenance port)

```
FONT_BEGIN{size, digest, fontpack_id} → {slot, flash_size}   (inactive slot chosen; TOO_LARGE if size > slot_size)
FONT_DATA{offset, data} × n                                  (strictly sequential; erase ahead per 64 KiB)
FONT_COMMIT{} → {fontpack_id}                                (SHA-256 over the slot == digest, format validation, directory flip)
```

`FONT_ABORT` or a reset before `FONT_COMMIT` leaves the active pack untouched.
A dedicated external-flash programming loader (J-Link + QSPI on the nRF52840
DK, `nrfjprog --qspi*`) is the alternative for factory programming; it writes
the same image and a directory record produced by `cremind tags tools fonts image`.
