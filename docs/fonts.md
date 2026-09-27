# Fonts

The companion shapes text with HarfBuzz; bridges draw the resulting glyph ids
from a **font pack** in their external flash ([`fontpack.md`](fontpack.md) is
the binary format and the flash rule). This page covers where the fonts come
from, how packs are built, what they cover and how much flash they need.

Everything here is produced by `cremind-tag fonts …` from pinned inputs:

| Path | What | In git |
|---|---|---|
| `fonts/manifest.yaml` | Pinned sources, faces, face ids, render rules, build profiles | yes |
| `fonts/manifest.lock.json` | SHA-256 of every pinned file (written by `fonts lock`) | yes |
| `fonts/icons.yaml` | Spec icon ids → Material Icons glyphs | yes |
| `fonts/LICENSES/` | OFL-1.1 and Apache-2.0 texts, byte-identical to the pinned upstream files | yes |
| `fonts/cache/` | Downloaded fonts and Unicode data (`<source>/<file>`, `unicode/<version>/`) | no |
| `fonts/out/<profile>/` | `fontpack.ctfp`, `fontpack.json`, `NOTICE`, `LICENSES/`, `coverage.json`, `image/` | no |

## Rebuilding

```sh
cd companion
cremind-tag fonts fetch                  # fill fonts/cache from the lock (≈ 56 MB, verified by SHA-256)
cremind-tag fonts build --profile full   # fonts/out/full/fontpack.ctfp
cremind-tag fonts build --profile dev    # fonts/out/dev/fontpack.ctfp (development only)
cremind-tag fonts size                   # P, the flash rule and the smallest standard part
cremind-tag fonts coverage               # per-script report -> fonts/out/full/coverage.json
cremind-tag fonts coverage --text "…"    # characters no face covers (exit 1 if any)
cremind-tag fonts image                  # external-flash image of the dev pack for J-Link QSPI
cremind-tag fonts list [--profile dev]   # faces with ids, roles, scripts, language hints
```

`build` also takes `--faces key,key|id` (replaces the profile's faces; the icon
face is always kept) and `--sizes 16,24` (keeps only those sizes). Builds are
reproducible: the pack is a pure function of the locked files, the manifest's
render rules, the FreeType version and the generator version, and the process
count does not matter (CI can compare the pack ids of two builds).

**Changing a pin**: edit `fonts/manifest.yaml` (new commit, path, size, git
blob SHA-1, version and name strings), run `cremind-tag fonts lock` and commit
both files. `lock` downloads each file once into `fonts/cache/`, rejects it
unless its size and git blob SHA-1 match the manifest, checks that the
manifest's glyph count, hinting class and `name` strings (version, copyright,
trademark) are the file's, records its SHA-256 and refreshes `fonts/LICENSES/`.
Builds and `fetch` then accept only files with the locked SHA-256. The pack
header's manifest id is `SHA-256(fonts/manifest.lock.json)[0:8]`; the lock has
no timestamps, so re-locking unchanged pins keeps the id.

## Sources (pinned by commit)

| Source | Repository | Ref | Commit | Used for |
|---|---|---|---|---|
| `notofonts` | notofonts/notofonts.github.io | `noto-monthly-release-2026.09.01` | `76fff9eee60dcef8381bfcae8d0b4311e2186e67` | 165 per-script faces (`fonts/<Fam>/hinted/ttf/<Fam>-Regular.ttf`), OFL text |
| `noto-cjk` | notofonts/noto-cjk | `Sans2.004` | `523d033d6cb47f4a80c58a35753646f5c3608a78` | Noto Sans SC/TC/HK/JP/KR (`Sans/SubsetOTF/<R>/…`) |
| `google-fonts` | google/fonts | main (Noto Emoji 3.002) | `b979dba422e445492b0eb9951ac52ee0b4d648c3` | Noto Emoji (monochrome, `ofl/notoemoji/NotoEmoji[wght].ttf`) |
| `material-icons` | google/material-design-icons | master (`font/` unchanged since) | `f7bd4f25f3764883717c09a1fd867f560c9a9581` | `font/MaterialIcons-Regular.ttf` 1.017 + `.codepoints`, Apache-2.0 text |
| Unicode | unicode.org/Public/17.0.0/ucd | 17.0.0 | (SHA-256 in the lock) | `Scripts.txt`, `ScriptExtensions.txt`, `PropertyValueAliases.txt` |

The lock holds 178 files, 55,962,354 bytes; a cold `fonts lock` took 12.5 s.

## Faces

The policy: every script the pinned Noto collection covers, **one Regular face
per script** (Sans preferred, Serif or the only family otherwise), the CJK
regional faces, monochrome Noto Emoji, no colour emoji. Tie-breaks (Arabic UI
style over Naskh, loopless Thai, Estrangela Syriac, joined Adlam/NKo, …) are
recorded in the manifest's notes.

`fonts/manifest.yaml` lists 172 faces: the icon face, 164 notofonts faces
(148 Sans — Tamil Supplement included —, 14 Serif where no Sans exists —
Hentaigana included —, Noto Music and Noto Znamenny Musical Notation), 5 CJK
regional faces, Noto Emoji and the optional Noto Nastaliq Urdu. The `full`
profile takes every face except role `optional`: **171 faces** (170 text faces
+ icons), 236,739 text glyphs. 34 of them are right-to-left (`rtl: true`, face
flag bit 2).

**Face ids are stable**: 0 is the icon face, the rest keep the id the manifest
gives them (new faces take the next free id, removed faces retire theirs), so a
subset pack such as `dev` keeps the full pack's ids.

| Id | Face | Id | Face |
|---:|---|---:|---|
| 0 | Material Icons (icons) | 166 | Noto Sans SC (`zh-Hans`, `zh-CN`, `zh-SG`, `zh-MY`, `zh`) |
| 1 | Noto Sans (Latn, Grek, Cyrl — Vietnamese included) | 167 | Noto Sans TC (`zh-Hant`, `zh-TW`; also Bopomofo) |
| 5 | Noto Sans Arabic | 168 | Noto Sans HK (`zh-HK`, `zh-MO`, `zh-Hant-HK`, `zh-Hant-MO`, `yue`) |
| 29 | Noto Sans Devanagari | 169 | Noto Sans JP (`ja`) |
| 47 | Noto Sans Hebrew | 170 | Noto Sans KR (`ko`) |
| 151 | Noto Sans Thai | 171 | Noto Emoji (variable, rendered at `wght` 400) |
| 48 | Noto Serif Hentaigana (supplement, `ja`) | 94 | Noto Nastaliq Urdu (optional, `ur`, not in `full`) |

`cremind-tag fonts list` prints all of them.

## Rendering

- FreeType **2.13.2**, the library bundled by freetype-py **2.5.1** (pinned in
  the manifest; a build with another FreeType fails unless
  `--allow-freetype-mismatch`). The TrueType interpreter (v40), the Adobe CFF
  engine and stem darkening off are set explicitly.
- Every glyph id `0 … numGlyphs−1` of every face — contextual, ligature and
  other GSUB-only glyphs included — at `FT_Set_Pixel_Sizes(face, 0, size)` with
  `FT_LOAD_RENDER | FT_LOAD_TARGET_MONO` plus the face's hinting mode.
  `bearing_x = bitmap_left`, `bearing_y = bitmap_top`,
  `advance = (advance.x + 32) >> 6` (the hinted advance, rounded half up);
  strike `ascent`, `descent`, `line_height` are the ceilings of
  `size->metrics` ascender, −descender and height.
- **Hinting rule**: every text face uses FreeType's auto-fitter
  (`FT_LOAD_FORCE_AUTOHINT`) — in `full`, the 127 faces with TrueType bytecode,
  the 37 TrueType faces without instructions (Noto Emoji included) and the 6
  CFF faces (5 CJK + Duployan, a CFF font despite its `hinted/ttf` path)
  alike. Compared at 16 and 24 px on Noto Sans, Arabic, Devanagari, Oriya,
  Thai, SC and JP, the fonts' own hints (ttfautohint bytecode, CFF hints) leave
  vertical stems 1 or 2 px wide depending on their sub-pixel position in 1 bpp,
  while the auto-fitter's mono mode snaps both directions and gives even 1 px
  stems. The rule is per hinting class in `render.hinting`, so switching a class
  back to `native` is a one-line manifest change.
- Noto Emoji is a variable font; it is rendered at `wght` 400 (its default
  instance), set through the variation coordinates.
- A glyph without ink (space, zero-width marks, glyphs whose ink vanishes at a
  size) is stored as an empty entry that keeps its advance.
- **Format limits**: a glyph entry holds ≤ 255 px width/height and bearings in
  −128…127. At 32 px ten glyphs are wider than 255 px and are cropped to their
  first 255 columns: U+FDFD ARABIC LIGATURE BISMILLAH AR-RAHMAN AR-RAHEEM
  (312 px, Noto Sans Arabic) and nine Dives Akuru stacked-ligature variants
  (256–276 px). Nothing is dropped. Showing them in full at 32 px would need a wider glyph entry
  (a format-version change, not an additive one); layouts can use 16/24 px.

### Icons

Face 0 is rendered from Material Icons (Apache-2.0). `fonts/icons.yaml` maps
every spec icon (`protocol/spec.yaml` `icons`) to a Material glyph by name and
codepoint; `lock`/`build` check both against the pinned `.codepoints` file. Two
names differ: `battery_low` → `battery_alert`, `hourglass` → `hourglass_empty`.

The icon face's glyph id is the icon id (glyph 0 is empty; 25 glyphs per
strike). Each icon is rendered without hinting (the icons are drawn on a 24 px
grid, so 24 and 48 px are exact) into an exact `size × size` cell with zero
bearings and `advance = size`: the cell is centred on the glyph's advance and
on the font's ascender–descender span — for Material Icons the em square, so
the designed padding (2 px at 24 px) is kept. Ink outside the cell is clipped.
Icon strikes: 16, 24, 32, 48 px, `ascent = size`, `descent = 0`.

## Packs

| | `full` | `dev` (development only) |
|---|---|---|
| Faces | 171 (170 text + icons) | 6: icons, Noto Sans, Arabic, Hebrew, Thai, Devanagari |
| Sizes | text 16/24/32, icons 16/24/32/48 | text 16/24, icons 16/24/32/48 |
| Strikes / glyph entries | 514 / 710,317 | 14 / 12,938 |
| **P** (pack size) | **34,478,342 B (32.88 MiB)** | **470,296 B (0.45 MiB)** |
| Glyph indexes | 8,523,804 B | 155,256 B |
| Bitmap area stored | 25,934,090 B | 314,309 B |
| Bitmaps before de-duplication | 38,397,671 B | 338,185 B |
| De-duplication saves | 12,463,581 B (32.5 %) | 23,876 B (7.1 %) |
| Pack id (manifest id `f2c67d9125acaf06`) | `3367a767d30c5b49` | `c1d7af1ec9fc564e` |
| Build time (22 processes / 1 process) | 6.1 s / 23.6 s | 0.7 s |

The CJK regional faces render 23,077,919 B of bitmaps of which 13,611,751 B
are distinct (41 % shared, mostly TC/HK and SC/TC Han). The dev faces at
16/24/32 px would be 899,156 B.

## Flash sizing

`Required = 2 × erase_aligned(P) + 16 MiB` ([`fontpack.md`](fontpack.md) §3):

| Pack | erase-aligned P | Required | Smallest standard NOR | Slot size | Headroom per slot |
|---|---|---|---|---|---|
| `full` | 34,537,472 B | 85,852,160 B (81.88 MiB) | **1 Gbit (128 MiB)** | 56 MiB | 23.12 MiB |
| `dev` (production rule) | 524,288 B | 17,825,792 B (17.00 MiB) | 256 Mbit (32 MiB) | 8 MiB | 7.55 MiB |

**Bridges need a soldered 1 Gbit (128 MiB) serial NOR** with QSPI and 4-byte
addressing (e.g. the Winbond W25Q01JV or Macronix MX66L1G45G class; qualify the
exact part with `FLASH_TEST`). A 64 MiB part would need P ≤ 24 MiB, which the
full policy does not meet.

**Development only:** the nRF52840 DK's 8 MiB MX25R6435F cannot satisfy the
rule. With the `dev` profile's reduced 1 MiB working space (slot directory,
pending layouts, spare) the slots are
`align_down_64K((8 MiB − 1 MiB) / 2) = 3,670,016 B`, and the dev pack fits
with 3,199,720 B to spare. Firmware built for the DK must use the same working
space for `fonts image` output to line up.

`cremind-tag fonts image` writes `flash.bin` (raw bytes from offset 0 through
both directory sectors), `flash.hex` (Intel HEX at the QSPI XIP base
0x12000000 with only the pack's sectors and both directory sectors, for
`nrfjprog -f NRF52 --program flash.hex --qspisectorerase --verify`) and
`flash.json`. The directory record (`seq` 1, slot 0) sits at the start of the
working space as in [`fontpack.md`](fontpack.md) §3; directory sector B is
written erased. (`protocol/spec.yaml` describes `FONTPACK_DIR_SIZE` as a
directory at flash offset 0, which disagrees with `fontpack.md`; the image
follows `fontpack.md`.)

## Coverage

At Unicode 17.0.0 (`Scripts.txt`), counting every code point assigned to a
script, the `full` pack maps 81,026 code points:

- **172 scripts** (Common and Inherited excluded): **143 fully covered, 20
  partially, 9 not at all**.
- Partial: Arab 1,368/1,413, Bali 124/127, Bopo 73/77, Cyrl 443/508 (no
  Cyrillic Extended-D), Diak 49/72, Egyp 1,078/5,105 (no Extended-A),
  Glag 132/134, **Hani 30,620/103,351** (CJK Extensions B–J), Hira 376/381,
  Kana 302/321, Kawi 86/87, Khoj 64/65, Kits 471/472, Knda 91/92,
  Latn 1,475/1,492, Mymr 223/243, Shrd 96/104, Syrc 77/88, Tang 6,914/7,059,
  Telu 100/101.
- Not covered (no released Noto font): Berf, Gara, Gukh, Krai, Onao, Sidt,
  Tayo, Tols, Tutg.
- Common 8,170/9,123, Inherited 398/684.

`fonts/out/<profile>/coverage.json` lists, per script, the covered/total
counts, the providing faces, every missing code point range and the
Script_Extensions coverage, and per face its cmap size and coverage of its
declared scripts. `cremind_tag.fonts.coverage.check_text(text, fontset)`
returns the characters no face maps (controls, format characters other than
the visible prepended concatenation marks, separators and variation selectors
are never reported); previews and `ComposedScreen.unsupported_chars` use it.

## Using a pack from the companion (FontSet)

`FontSet.load(pack_path, cache_dir=None)` validates the pack, reads the sidecar
`fontpack.json` next to it (or, without one, the checkout's manifest when its
lock matches the pack's manifest id) and resolves each text face to its cached
font file, checking that the file has the SHA-256 it was rasterised from
(missing or different → run `cremind-tag fonts fetch`). The cache defaults to
`$CREMIND_TAG_FONT_CACHE`, else `<repo>/fonts/cache`.

- `FaceInfo.path` is the exact file that was rasterised; load it into HarfBuzz
  (`hb.Face(path.read_bytes())`) so glyph ids match the pack 1:1. Apply
  `FaceInfo.variations` (`font.set_variations({"wght": 400})` for Noto Emoji).
  The icon face has `path = None`: icons are drawn by id, never shaped.
- `FaceInfo.scripts` are ISO 15924 codes (CJK faces list `Hani` plus their
  region's codes; Common-script faces such as Music list `Zyyy`),
  `languages` are BCP-47 prefixes, `rtl` marks right-to-left scripts, `role` is
  `primary`, `supplement`, `cjk-region`, `optional`, `emoji` or `icons`.
- **Picking a face**: `cremind_tag.fonts.coverage.candidate_faces(fonts,
  script, language)` orders the faces that declare a script — the longest
  matching language prefix first (`zh-Hant-HK` → HK over TC, `zh-TW` → TC,
  `ja` → JP, `ko` → KR), then primary, CJK-region, supplement, optional. Han
  with no matching language uses the `zh-Hans` face (SC). Then check the face's
  cmap and fall back to `CmapIndex.for_fontset(fonts).faces_for(cp)` (e.g.
  Tamil Supplement, Hentaigana, Common-script symbols, emoji).
- `StrikeMetrics` (`ascent`, `descent`, `line_height`, `glyph_count`) are
  whole pixels as stored in the pack: ceilings of FreeType's scaled
  ascender/−descender/height at that size. Glyph advances in the pack are the
  hinted advances rounded half up; HarfBuzz positions are unhinted, so layouts
  that want crisp spacing for simple scripts can use the pack advances.

## Licensing

Every text face is OFL-1.1 (no Reserved Font Names). A pack is a Modified
Version (bitmap conversion), so it is distributed under OFL-1.1 with a
`NOTICE` listing each face's copyright, trademark, version string, source URL
and SHA-256, and carries the neutral name "Cremind Tag Glyph Pack" with a
"derived from Noto" statement ("Noto" is a Google trademark and the OFL grants
no trademark rights). It may be bundled with firmware but not sold by itself.
Face 0 comes from Material Icons under Apache-2.0, whose text ships in
`LICENSES/` too. Unicode data is used only by the tooling, not shipped in packs.
