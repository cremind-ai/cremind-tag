# Text layout and screen composition

Tags contain no font engine: the companion turns card text into positioned
glyph ids that bridges draw from the font pack ([`fonts.md`](fonts.md),
[`fontpack.md`](fontpack.md)) with the logical screen format of
[`protocol.md`](protocol.md) §4. This page covers how text is laid out, how a
tag's screen is composed from its active cards, how previews are rendered and
what is known not to work.

| Code (`companion/src/cremind_tag/`) | What |
|---|---|
| `layout/plaintext.py` | card text (Markdown / HTML-ish) → plain NFC text |
| `layout/unicode.py` | ICU: graphemes, line breaks, bidi, scripts, emoji properties, NFC |
| `layout/fonts.py` | `FontContext`: HarfBuzz fonts, cmap coverage, face choice, pack glyphs |
| `layout/engine.py` | `layout_text` → `TextBlock` (`LineBox`es, `PositionedGlyph`s) |
| `layout/commands.py` | positioned glyphs → `GLYPHS` commands |
| `compose/api.py` | the contract: `TagPanel`, `ActiveCard`, `ScreenSettings`, `ComposedScreen`, `Composer` |
| `compose/cards.py` | card policy: visibility, icon, body, colour, progress, QR link, order |
| `compose/timefmt.py` | ICU date/time formatting in the profile's language and time zone |
| `compose/screen.py` | `compose_screen`, `compose_identify`, `compose_blank` |
| `compose/preview.py` | PNG previews through `render/reference.py` |
| `compose/samples.py` | the multilingual sample set, per-face samples, `write_samples` |
| `cli/preview.py` | `cremind-tag preview text|card|screen|identify|samples` |

## Pipeline

`layout_text(text, fonts, width=, size_px=, language=, direction=, align=,
max_lines=, ellipsis=)` lays out plain text (`\n` separates paragraphs) in a
box `width` pixels wide. Per paragraph:

1. **Normalise.** C0/C1 controls are removed (a tab becomes a space), then NFC
   with ICU's data (ICU 77 = Unicode 16; Python's `unicodedata` is older).
2. **Grapheme clusters** (UAX #29, ICU character break iterator). Nothing
   below — face choice, breaking, ellipsis — ever splits a cluster.
3. **Paragraph direction.** `direction="ltr"`/`"rtl"` wins. With `"auto"`:
   an RTL language hint (`ar`, `he`, `fa`, `ur`, `ps`, `yi`, `dv`, `ug`, `ckb`,
   `sd`, `syr`, `nqo`, … or any `-Arab` tag) makes the paragraph RTL when it
   contains any strong RTL character (so "Google Drive: تم الحفظ" stays RTL);
   otherwise the first strong character decides (UAX #9 P2/P3); a paragraph
   without strong characters follows the hint. Embedding levels come from
   `icu.Bidi` at that fixed paragraph level.
4. **Script itemisation.** Each cluster takes the script (ICU `Script`) of its
   first character that is not Common/Inherited. Common and Inherited clusters
   take the preceding script (the following one at the paragraph start); an
   opening paired bracket remembers the script it was opened in and its
   closing bracket gets the same (UAX #24-style, 64-deep stack).
5. **Font fallback per cluster** — see "Font selection" below.
6. **Shaping** with uharfbuzz: every maximal run of clusters with the same
   (bidi level, face, script) is shaped with the **whole paragraph as
   context** (`add_codepoints(text, offset, length)`), direction from the
   level, script and language set explicitly, default features, `BOT`/`EOT`
   flags at the text ends. The font is the exact cached file the pack was
   rasterised from (glyph ids match the pack 1:1) with the face's variation
   coordinates (Noto Emoji `wght` 400), scaled to 26.6 at the strike size:
   `scale = (size·64, size·64)`, `ppem = (size, size)`. Each cluster's advance
   is the sum of its glyphs' `x_advance`.
7. **Line breaking.** Break opportunities from ICU's line break iterator for
   the language: dictionaries for Thai, Lao, Khmer and Myanmar (under any
   language hint), CJK rules, spaces and hyphens. Chinese and Japanese use
   `lb=strict` (no line starts with `ー`, small kana, `。`, `、`); Han text
   under a non-CJK hint is broken as `zh`. Greedy fitting on the cluster
   advances: the longest line whose width **without trailing spaces** fits;
   trailing spaces hang (not counted, not drawn) and a new line never starts
   with a space after a soft break. A word wider than the box is broken
   between grapheme clusters.
8. **Line-boundary reshaping.** Every line is shaped again with **only the
   line as context**, so joining, ligatures and kerning are recomputed at the
   break (a joining Arabic word broken across lines ends in a final form and
   restarts with an initial form). If the reshaped line no longer fits, it is
   broken at the previous opportunity (or cluster) and reshaped again.
9. **Max lines and ellipsis.** When `max_lines` cuts the text (in this
   paragraph or a later one) the last line keeps its natural content minus
   trailing spaces plus U+2026 `…` in whichever face covers it (face choice as
   for any Common character, so the run's face when it has one; `...` when no
   face has U+2026); clusters are dropped from the end, re-shaping each time,
   until it fits. The ellipsis is resolved by bidi with the line (at the
   logical end: the right end of an LTR paragraph, the left end of an RTL one).
10. **Visual order.** Line levels from `Bidi.setLine` (rule L1), then rule L2
    over the line's runs; HarfBuzz already returns each RTL run's glyphs left
    to right.
11. **Positions.** The pen accumulates HarfBuzz `x_advance`s in 26.6; each
    glyph origin is `pen + x_offset` (and `baseline − y_offset`) rounded half
    up to whole pixels: `(v + 32) >> 6`. Nothing accumulates rounded values, so
    there is no drift (`test_positions_accumulate_in_26_6`). Glyph advances
    stored in the pack (hinted) are not used.
12. **Alignment.** `start`/`end` follow the paragraph direction (start = left
    in LTR, right in RTL), `center`, or absolute `left`/`right`; the line's
    left edge is a whole pixel.
13. **Line boxes.** Ascent and descent start from the base face's strike
    metrics (Noto Sans, face 1: 18/5 px at 16 px, 26/8 at 24, 35/10 at 32) and
    grow to the ink of the line's glyphs (bitmap extents from the pack), so
    Arabic, Thai, Tibetan or stacked Vietnamese lines are taller only when
    their marks need it and lines never overlap. Lines stack with no gap
    (`line_spacing` adds one).

Glyphs without ink (spaces, zero-width marks) only move the pen and are not
emitted, which saves glyph budget. A `TextBlock` holds the normalised `text`,
`lines` (`LineBox`: code-point range, `x`, `width`, `top`, `baseline`,
`bottom`, `rtl`, `ellipsis`), `glyphs` (`PositionedGlyph`: `face_id`,
`size_px`, `glyph_id`, `x`, `y` = baseline origin, `cluster`, `line`),
`height`, `truncated`, `unsupported` (characters no face maps),
`unsupported_clusters` (clusters no single face maps completely), `notdef`
(glyph id 0 count on the visible lines) and `shaped`. Results are cached
(1024 entries, LRU) — `TextBlock` is immutable.

### Font selection

For each grapheme cluster, among faces that have a strike at the requested
size (the dev pack has no 32 px):

1. **Emoji presentation** — VS16, a keycap, a ZWJ sequence, a skin-tone
   modifier, a flag or an `Emoji_Presentation` base — picks the emoji face
   (171) when it maps the cluster. VS15 asks for text presentation; other
   `Extended_Pictographic` characters default to text.
2. The faces that **declare the cluster's resolved script**, ordered by
   `fonts.coverage.candidate_faces` (longest BCP-47 language match, then
   primary, CJK region, supplement, optional; Han without a matching language
   → `zh-Hans`, Noto Sans SC). The first that maps every character that needs
   a glyph wins. For Han, Hiragana, Katakana, Hangul and Bopomofo the language
   is the hint when it is Chinese/Japanese/Korean, else inferred from the
   paragraph: kana → `ja`, hangul → `ko` ("直す" under an `en` hint uses the JP
   face).
3. The **previous cluster's face** when it maps the cluster (digits and
   punctuation stay in the run's font; never the emoji face for non-emoji).
4. **Any face mapping the whole cluster**, by language match, Noto Sans
   first, then role (primary, CJK region, supplement, optional, emoji last
   unless emoji presentation was asked) and face id.
5. Otherwise the cluster is **unsupported**: drawn with the face that maps its
   base character (else the previous / base face), whose `.notdef` box shows
   the gap; the characters no face maps are reported in `unsupported` and the
   cluster in `unsupported_clusters`.

Controls, format characters (except visible prepended concatenation marks),
separators and variation selectors need no glyph and never count against a
face (`fonts.coverage.needs_glyph`).

### GLYPHS commands

`glyph_commands(glyphs, color)` groups consecutive glyphs with the same face,
size and colour into one `GLYPHS` command: `origin` = the first glyph's origin
(its entry has `dx = dy = 0`), every later entry the i8 delta from the
previous origin. A run is split when a delta leaves −128…127 or at 255 glyphs.
Lines of a block are emitted alternately left-to-right and right-to-left
("serpentine"), so the step to the next line is a short diagonal instead of a
jump back across the box and a paragraph in one face usually needs one
command; glyphs of one colour never depend on drawing order.

### Plain text

`plain_text(text, keep_newlines=False)` (titles: one paragraph; bodies keep
single line breaks): HTML comments and `<script>`/`<style>` bodies are
dropped, `<br>` and block-closing tags become line breaks, other tags are
removed and entities decoded; Markdown fences and inline code keep their
text, links and images keep label / alt text, emphasis, headings, block
quotes, bullets, rules, table pipes and reference definitions go, backslash
escapes are resolved; controls go, runs of ASCII spaces collapse (no-break
and ideographic spaces stay), blank lines collapse, NFC.

## Screen model

`compose_screen(panel, cards, fonts, settings, now) -> ComposedScreen`
implements `compose.api.Composer`. The logical canvas is the native panel
turned by `panel.rotation` (400×300 landscape; rotation 1 or 3 → 300×400
portrait); every coordinate is derived from the canvas, so other sizes work
(tested: 400×300 at all four rotations, 296×128, 250×122 portrait, 800×480).

```
┌──────────────────────────────────────────────┐
│ Desk                     Sun, Sep 27, 2:05 PM │  header (16 px): tag name · local date and time
├──────────────────────────────────────────────┤
│ [48] Approve deployment of             [QR]   │  headline: icon, title (32 px when the whole
│      release 2.4?                             │  title fits in 2 lines, else 24 px, ≤ 3 lines),
│      2:02 PM                                  │  card time, progress bar + "7/12",
│      [██████████░░░░░]  7/12                  │  body (16 px, what fits)
│      body text …                              │
├──────────────────────────────────────────────┤
│ [16] Telegram channel stopped       Sep 26    │  up to 3 more cards: icon, title (1 line, …),
│ [16] Reply ready: Tóm tắt cuộc họp  1:35 PM   │  time today / date otherwise
├──────────────────────────────────────────────┤
│ 3 more updates waiting for this tag · Updated 2:05 PM │  footer (16 px)
└──────────────────────────────────────────────┘
```

- **Order**: displayable cards (`resolved` and `clear` are instructions, never
  shown) by priority descending, then newest `created_at`, then highest
  delivery id. The first is the headline; up to three more are list rows (as
  many as leave the headline room for one title line and its time).
- **Header**: `panel.name` (else "Tag 1A2B3C4D") at the start side, the local
  date and time (`settings.timezone`, ICU skeleton `MMMEdjm` in
  `settings.language`: "Sun, Sep 27, 2:05 PM", "14:05 CN, 27 thg 9", "الأحد، 27
  سبتمبر، 10:05 ص") at the end side. Panels under 160 px high have no header.
  An unknown time zone falls back to UTC.
- **Headline**: icon — `card.icon` when it names a built-in icon, else the
  kind's default (notification → notifications, task_outcome → task,
  needs_input → help, excerpt → chat, progress → sync, health → warning,
  indexing_problem → folder, calendar → event, automation → schedule, usage →
  bar_chart, pinned_note → push_pin, tag_diagnostics → battery_low; unknown
  kinds by severity, else info) at 48/32/24 px depending on the room; the
  title in the card's `lang` (else the profile language), **red on two-plane
  panels for `needs_input` cards and `error` severity** (icon too); the card
  time (`jm` today, `MMMdjm` otherwise); a `PROGRESS` bar and a localised
  "done/total" label for `progress` cards with real counts (integers,
  `done ≥ 0`, `total > 0`, `done` clamped; totals above 65535 are scaled into
  u16); the body.
- **Body**: only when `settings.show_excerpts` is on and the kind is one whose
  body Cremind sanitises (`excerpt`, `needs_input`, `task_outcome`,
  `notification`, `health`, `indexing_problem`, `calendar`, `automation`,
  `usage`, `tag_diagnostics`, `progress`); a `pinned_note` body is text the
  owner wrote for this tag and is always shown. Unknown kinds never show a
  body. The body gets the height and the budget left after everything else.
- **QR**: only with `settings.qr_links` and a link that is a short, token-free
  `https` URL: printable ASCII, ≤ `LAYOUT_QR_MAX_TEXT` bytes, a host, no user
  info, no query (not even `?`), no `=`, `&` or `;`, no 24+-character
  `[A-Za-z0-9_-]` run once UUID record ids are set aside, none of `token`,
  `secret`, `passw`, `apikey`, `api_key`, `api-key`, `access_key`,
  `signature`, `jwt`, `bearer`. ECC LOW, 4/3/2 px modules — the largest that
  fits the headline height and a third of the width — with a 2-module quiet
  zone left white, at the end side of the headline.
- **Footer**: "N more updates waiting for this tag · Updated HH:MM"; when it
  does not fit on one line, "N more updates waiting · …", "N more updates ·
  …", "+N · …"; "Updated HH:MM" alone when nothing is pending. The composer's
  own strings are English (LTR paragraphs); times and numbers are localised.
- **Empty**: no displayable card → a check-circle icon and "No updates".
- **Right-to-left profiles** (`settings.language` RTL) mirror the chrome:
  icons and names on the right, times on the left. Every card text is still
  aligned by its own paragraph direction (an Arabic title is right-aligned in
  an English UI; list rows and the header align to the UI's start side).

### Delivery ids

`ComposedScreen.delivery_ids` lists **only the cards whose title is drawn**
(headline first, then list rows). Cards counted in the footer are in
`pending_delivery_ids` (`len == pending_count`) and are **not** part of the
revision. Reason: connector-api.md receipts every delivery a displayed
revision includes as `displayed`; a card only counted in "3 more updates"
has not been seen by anyone, and a `needs_input` card reported displayed
while hidden would mislead Cremind. The pending cards stay active in the
daemon's store and are shown (and receipted) by a later revision when
higher-priority cards resolve or expire — or they end as `expired`/`cancelled`
without ever being reported displayed. The daemon must therefore not mark a
pending card `superseded` when a newer revision stops showing it: a newer
revision "includes" (docs/connector-api.md "Screen model") every still-active
card either by showing it or by counting it.

### Limits and degradation

A screen must stay within **`min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX)` =
4000 bytes** (DELIVER_LAYOUT's CBOR envelope has to fit one 4 KiB serial
frame; bridges accept up to 4096), `LAYOUT_MAX_GLYPHS` (512) glyphs and
`LAYOUT_MAX_COMMANDS` (256) commands. The composer tracks the three budgets
while adding commands:

1. the body is laid out last with as many lines as fit the height **and** the
   remaining budget (down to none);
2. if a fixed part does not fit, the next plan of the fixed ladder is tried:
   list rows 3 → 2 → 1 → 0, then title 24 px ≤ 2 lines, 24 px 1 line (header
   shows the time only), 16 px ≤ 2 lines, 16 px 1 line without body or QR.

Every step is a pure function of the inputs (`now` included): the same cards
give the same bytes (`test_same_input_same_bytes`, `test_plans_degrade_*`).
On a 400×300 panel even 20 cards with 400-character titles and 1200-character
bodies in any script stay far inside the limits (largest measured: 2476
bytes, 353 glyphs, 140 commands for a title alternating six scripts per
character); the ladder matters on large panels (an 800×480 worst case uses
491 glyphs) and is tested by tightening each limit.

`ComposedScreen.unsupported_chars` lists the characters of the **drawn**
texts that no face maps.

### Identify and blank

- `compose_identify(panel, fonts, tag_id=None)`: a 4 px frame, the info icon,
  the tag id (`panel.tag_id` as 8 hex digits, or the string given) at 32 px
  (24 px on the dev pack) and the tag's name (24 px, ≤ 2 lines, dropped if the
  budget demands), centred. The daemon delivers it as a new revision for the
  `identify` command; `delivery_ids` is empty.
- `compose_blank(panel)`: an all-white screen with no commands (12 bytes), for
  `clear` jobs and ownership changes.

## Previews

`compose.preview.preview_png(screen, panel, fonts, scale=1)` renders the
layout through `render/reference.py` — the normative renderer, the same pack,
the same plane encoding a bridge produces — and decodes the planes back to a
picture: a true 1-bit PNG for black/white panels, a 2-bit palette PNG (white,
black, red) for black/white/red panels, turned back to the **logical**
orientation (`orientation="native"` keeps panel rows). A scaled image over the
limit falls back to scale 1; over `MAX_PREVIEW_BYTES` (64 KiB, the connector's
limit) it raises `PreviewTooLarge`. Measured: 1–5 KiB for 400×300 screens,
worst cases included. `render_image` / `render_png` take any layout (bytes or
`protocol.layout.Layout`), `panel=None` for a free-standing canvas.

## API for the daemon

| Call | Cost (full pack, this PC) |
|---|---|
| `FontSet.load(pack, cache)` | ≈ 0.7 s (34 MB pack, SHA-256 of every font) — once |
| first `compose_screen` on a FontSet | ≈ 0.3 s: pack parse, 170 cmaps, HarfBuzz faces as used (cached per FontSet, weakly) |
| `compose_screen` after warm-up | ≈ 5 ms with every text shaped again, ≈ 1 ms when the texts are cached; worst cases ≤ 70 ms |
| `compose_identify`, `compose_blank` | < 5 ms / < 1 ms |
| `preview_png` | 10–50 ms at scale 1 |

`ComposedScreen.layout` is already validated (§4.3 structure,
`check_strikes` against the pack, `check_panel` for the rotation). The
composer never mutates its inputs and is thread-safe (break iterators per
thread, formatter and cache locks).

## `cremind-tag preview`

```sh
cd companion
cremind-tag preview text "Tiếng Việt مرحبا 123 שלום" --size 24 --width 300 --lang vi --out t.png --scale 2
cremind-tag preview card job.json --panel bwr --out card.png            # one job or bare card
cremind-tag preview screen cards.json --panel bwr --rotation 1 --out s.png   # [jobs] or {jobs|cards, settings}
cremind-tag preview identify --tag-id 1A2B3C4D --name Desk --out id.png
cremind-tag preview samples --out ../build/layout-samples [--scale 2]
```

`--pack` defaults to `<repo>/fonts/out/full/fontpack.ctfp` (else the dev
pack), `--cache` to the font cache. `text` prints every line (range, x,
width, baseline, direction), the faces used and unsupported characters;
`card`/`screen` print the layout size, glyph and command counts, shown and
pending delivery ids. `screen` files take the connector job shape (or bare
cards) and optional `settings` (`language`, `timezone`, `show_excerpts`,
`qr_links`); `--lang`, `--tz`, `--excerpts/--no-excerpts`, `--qr/--no-qr`,
`--now` override.

### Sample gallery

`preview samples --out DIR` writes:

- `multilingual-NN.png` — the hand-written set (`compose.samples.MULTILINGUAL`):
  English, Vietnamese, French, German, Polish, Turkish, Greek, Russian,
  Ukrainian, Arabic, Persian, Urdu, Hebrew, Thai, Lao, Khmer, Myanmar, Hindi,
  Bengali, Tamil, Telugu, Kannada, Malayalam, Gujarati, Punjabi, Odia,
  Sinhala, Tibetan, Georgian, Armenian, Amharic, Chinese (SC/TC/HK), Japanese,
  Korean, Mongolian, Cherokee, emoji, mixed directions, combining marks;
- `faces-NN.png` — one automatic sample per text face (see below);
- `screen-*.png` — example screens: landscape BW, landscape BWR with excerpts
  and QR, portrait BWR in Vietnamese, landscape with an Arabic UI, portrait
  progress, empty, identify;
- `summary.json` — per sample: faces used, unsupported characters, `.notdef`
  count; per screen: bytes, shown and pending ids, PNG size.

It exits 1 when a face's sample is not drawn by that face, is unsupported or
has `.notdef`. The gallery built from the current full pack is in
`build/layout-samples/` (scale 1) and `build/layout-samples/scale2/`.

## Tests

`companion/tests/layout` and `companion/tests/compose` (tests that need the
packs are marked `fonts` and skip when `fonts/out/<profile>` or
`fonts/cache` is missing):

- plain text, ICU helpers (graphemes, Thai/Lao dictionary breaks, CJK strict
  breaks, paragraph direction, L1/L2, emoji properties);
- engine: Arabic contextual forms, lam-alef, RTL reordering of Arabic + Latin
  + digits, Hebrew, mixed directions on one line, Thai dictionary line breaks
  at word boundaries, CJK kinsoku, Han regional faces by language (and inferred
  from kana/hangul, with differing bitmaps), line-boundary reshaping, trailing
  spaces, long words, Vietnamese NFC = NFD and stacked marks, Devanagari /
  Bengali / Tamil conjuncts and reordering, emoji presentation, unsupported
  characters and clusters, ellipsis (LTR at the right, RTL at the left), max
  lines across paragraphs, alignment, 26.6 rounding without drift, line boxes
  grown to the ink, determinism, the dev pack;
- GLYPHS: grouping, i8 overflow splits, 255-glyph runs, serpentine order,
  real blocks encoding and validating with `protocol/layout.py`;
- **every face** (acceptance, "cover every installed script with automated
  samples"): for each of the **170 text faces** of the full pack a sample is
  generated from the face's cmap ∩ the pinned `Scripts.txt` — letters of the
  face's first declared script (numbers and symbols for music, numerals,
  symbols and emoji faces) that the engine draws **with that face**, evenly
  spread over the repertoire, grouped in four-letter words — laid out and
  rendered: the face is used, nothing is unsupported, HarfBuzz returns no
  `.notdef`, the rendering has ink. The 41 multilingual samples are checked
  the same way;
- composer: every panel and rotation validates, delivery ids and pending
  counts, red only on BWR, bodies only with excerpts, QR rules, progress,
  identify and blank, the dev pack, worst cases (20 cards, 400-character
  titles and 1200-character bodies in 13 scripts/styles incl. a title
  switching script every character, on 400×300, 300×400, 296×128 and
  800×480) within 4000 bytes / 512 glyphs / 256 commands, the degradation
  ladder under tightened limits, determinism, < 200 ms per screen;
- previews: 1-bit and 3-colour PNGs match the reference frame, logical
  orientation, plane polarity independence, ≤ 64 KiB;
- the CLI.

## Known limitations

- **Wide glyphs cropped at 32 px**: the ten glyphs wider than 255 px at 32 px
  (U+FDFD ARABIC LIGATURE BISMILLAH and nine Dives Akuru stacked ligatures,
  [`fonts.md`](fonts.md)) are cropped to 255 columns in the pack; titles that
  use 32 px show them cut (24/16 px are complete).
- **Nastaliq off**: Urdu is drawn with Noto Sans Arabic (Naskh-style); Noto
  Nastaliq Urdu is an optional face outside the `full` pack.
- **Spacing**: glyphs are placed at HarfBuzz's unhinted positions while the
  bitmaps are auto-hinted 1-bpp; spacing can look uneven by a pixel at 16 px.
- **Unicode versions**: ICU 77 knows Unicode 16; characters new in the pinned
  Unicode 17 data are treated as Common (their face is still found by cmap).
- **No hyphenation or justification**; no vertical text (Mongolian is shown
  horizontally, as Noto Sans Mongolian draws it).
- **English chrome**: "N more updates waiting…", "Updated", "No updates",
  "Tag …" are English (times, dates and numbers are localised).
- **Right-to-left UI**: `PROGRESS` always fills left to right.
- **Emoji are monochrome** (Noto Emoji); flags show as boxed letters, skin
  tones are not distinguished.
- **Coverage gaps** of the pack (CJK Extensions B–J, Cyrillic Extended-D, …,
  [`fonts.md`](fonts.md) "Coverage") show as `.notdef` boxes and are reported
  in `unsupported_chars`.
- Line heights follow the ink: a line with tall stacks (Tibetan, Myanmar,
  Arabic marks) is taller than its neighbours.
