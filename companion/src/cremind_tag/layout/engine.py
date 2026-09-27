"""Multilingual paragraph layout: text -> positioned glyphs and line boxes.

Pipeline per paragraph (docs/layout.md "Pipeline"):

1. NFC; grapheme clusters (ICU) — nothing below ever splits a cluster;
2. paragraph direction (explicit, else language hint / first strong character)
   and bidi embedding levels (ICU ``Bidi``);
3. script itemisation: each cluster takes its base character's script; Common
   and Inherited clusters take the preceding script (the following one at the
   start), paired brackets take their opener's script;
4. font fallback per cluster (`FontContext.choose`);
5. HarfBuzz shaping of every (level, face, script) run with the whole
   paragraph as context, giving each cluster an advance;
6. line break opportunities (ICU, locale-tailored: dictionaries for Thai, Lao,
   Khmer, Myanmar; CJK rules) and greedy fitting on those cluster advances,
   trailing spaces hanging (not counted, not drawn), an over-long word broken
   between clusters;
7. every line **reshaped on its own** (line-boundary reshaping: joining,
   ligatures and kerning are recomputed with the line as the whole context) and
   re-broken earlier if the reshaped line no longer fits;
8. max lines: the last line keeps what fits before a trailing ellipsis
   (U+2026 in whichever face covers it; "..." otherwise);
9. rule L1/L2 visual reordering of the runs of each line (ICU line levels);
10. positions accumulated in 26.6 fixed point, each glyph origin rounded half up
    to whole pixels (no drift); lines aligned start/end/center respecting the
    paragraph direction; line boxes from the base face's strike metrics grown to
    the ink of the line's glyphs.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import uharfbuzz as hb

from cremind_tag.fonts.fontset import FontSet
from cremind_tag.protocol.layout import Glyphs

from .fonts import FontContext, han_language, is_han_family
from .unicode import (
    ParagraphBidi,
    Utf16Map,
    emoji_presentation,
    graphemes,
    is_space,
    line_breaks,
    nfc,
    normalize_language,
    paired_bracket,
    paragraph_level,
    script_of,
    visual_order,
)

Direction = Literal["auto", "ltr", "rtl"]
Align = Literal["start", "end", "center", "left", "right"]

ELLIPSIS = 0x2026
_NEUTRAL = frozenset({"Zyyy", "Zinh", "Zzzz"})
_CONTROLS = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f]")
"""C0/C1 controls except the line feed (paragraph separator): removed; a tab becomes a space."""
_BOT = int(hb.BufferFlags.BOT)
_EOT = int(hb.BufferFlags.EOT)
MAX_WIDTH = 16000


@dataclass(frozen=True, slots=True)
class PositionedGlyph:
    """One glyph to draw: pen origin (``x``, baseline ``y``) in the block's coordinates."""

    face_id: int
    size_px: int
    glyph_id: int
    x: int
    y: int
    cluster: int
    """Code-point index in `TextBlock.text` of the grapheme cluster the glyph belongs to (-1 = ellipsis)."""
    line: int


@dataclass(frozen=True, slots=True)
class LineBox:
    start: int
    """First code point of the line in `TextBlock.text`."""
    end: int
    """End of the line's visible text (trailing spaces excluded; an ellipsis is not counted)."""
    x: int
    """Left edge of the line's advance box."""
    width: int
    """Advance width in pixels (rounded up)."""
    top: int
    baseline: int
    bottom: int
    rtl: bool
    """Paragraph direction."""
    ellipsis: bool = False

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class TextBlock:
    """A laid-out text: lines stacked from y = 0, glyphs in visual order line by line."""

    text: str
    size_px: int
    box_width: int
    lines: tuple[LineBox, ...]
    glyphs: tuple[PositionedGlyph, ...]
    """Glyphs with ink (ink-less glyphs such as spaces only move the pen and are not emitted)."""
    height: int
    truncated: bool
    unsupported: tuple[str, ...]
    """Characters of ``text`` no face maps (shown as the face's .notdef)."""
    notdef: int
    """Glyphs HarfBuzz returned as .notdef (id 0) on the visible lines."""
    shaped: int
    """All glyphs HarfBuzz produced on the visible lines, ink-less ones included."""
    unsupported_clusters: tuple[str, ...] = ()
    """Grapheme clusters no single face maps completely (drawn with the face that maps their base)."""

    @property
    def width(self) -> int:
        """Widest line's advance width."""
        return max((ln.width for ln in self.lines), default=0)

    @property
    def ink_width(self) -> int:
        return max((ln.x + ln.width for ln in self.lines), default=0) - min((ln.x for ln in self.lines), default=0)

    @property
    def faces(self) -> tuple[int, ...]:
        return tuple(sorted({g.face_id for g in self.glyphs}))

    def moved(self, dx: int, dy: int) -> list[PositionedGlyph]:
        """Glyphs translated to absolute coordinates (block top-left at ``(dx, dy)``)."""
        return [PositionedGlyph(g.face_id, g.size_px, g.glyph_id, g.x + dx, g.y + dy, g.cluster, g.line)
                for g in self.glyphs]

    def commands(self, x: int, y: int, color: int) -> list[Glyphs]:
        """GLYPHS commands drawing the block with its top-left at ``(x, y)``."""
        from .commands import glyph_commands

        return glyph_commands(self.moved(x, y), color)


# --------------------------------------------------------------------------- paragraph state


@dataclass
class _Paragraph:
    text: str
    cps: list[int]
    bounds: list[int]
    level: int
    bidi: ParagraphBidi
    scripts: list[str]
    faces: list[int]
    language: str
    han_language: str
    breaks: list[int]
    """Cluster-boundary indices (into ``bounds``) after which a line may end, ascending, ending with K."""
    hard: set[int]
    prefix: list[int]
    """prefix[k] = advance (26.6) of clusters [0, k) from the whole-paragraph shaping."""
    space: list[bool]
    partial: list[str]
    """Clusters no single face maps completely."""

    @property
    def clusters(self) -> int:
        return len(self.bounds) - 1


@dataclass
class _Line:
    glyphs: list[tuple[int, int, int, int, int]]
    """(face, glyph id, x 26.6 from the line's pen start, y offset 26.6 (up), cluster cp index in the paragraph)."""
    advance: int
    notdef: int
    start: int
    end: int
    ellipsis: bool


def _lang_for(script: str, para_lang: str, han_lang: str) -> str:
    return han_lang if is_han_family(script) else para_lang


def _resolve_scripts(cps: Sequence[int], bounds: Sequence[int]) -> list[str]:
    """One script per cluster; Common/Inherited resolved from neighbours and paired brackets."""
    k_count = len(bounds) - 1
    raw: list[str | None] = []
    for k in range(k_count):
        sc = None
        for cp in cps[bounds[k]:bounds[k + 1]]:
            s = script_of(cp)
            if s not in _NEUTRAL:
                sc = s
                break
        raw.append(sc)
    resolved: list[str | None] = [None] * k_count
    current: str | None = None
    stack: list[tuple[int, str | None]] = []
    for k in range(k_count):
        if raw[k] is not None:
            current = raw[k]
            resolved[k] = current
            continue
        kind, pair = paired_bracket(cps[bounds[k]])
        if kind == 1:
            if len(stack) < 64:
                stack.append((pair, current))
            resolved[k] = current
        elif kind == 2:
            match = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == cps[bounds[k]]:
                    match = stack[i][1]
                    del stack[i:]
                    break
            resolved[k] = match if match is not None else current
            if match is not None:
                current = match
        else:
            resolved[k] = current
    # Leading neutrals take the first following script.
    following: str | None = None
    for k in range(k_count - 1, -1, -1):
        if raw[k] is not None:
            following = raw[k]
        if resolved[k] is None:
            resolved[k] = following
    return [s or "Zyyy" for s in resolved]


class _Shaper:
    """HarfBuzz shaping of one run with a context (26.6 at the strike size)."""

    def __init__(self, ctx: FontContext, size_px: int) -> None:
        self.ctx = ctx
        self.size = size_px

    def shape(self, face: int, context: list[int], start: int, end: int, rtl: bool, script: str,
              language: str) -> list[tuple[int, int, int, int, int]]:
        """Glyphs of ``context[start:end]`` in visual order: (gid, cluster, x_advance, x_offset, y_offset)."""
        buf = hb.Buffer()
        buf.add_codepoints(context, start, end - start)
        buf.direction = "rtl" if rtl else "ltr"
        buf.script = script
        if language:
            buf.language = language
        flags = (_BOT if start == 0 else 0) | (_EOT if end == len(context) else 0)
        if flags:
            buf.flags = hb.BufferFlags(flags)
        hb.shape(self.ctx.hb_font(face, self.size), buf, {})
        return [(i.codepoint, i.cluster, p.x_advance, p.x_offset, p.y_offset)
                for i, p in zip(buf.glyph_infos, buf.glyph_positions, strict=True)]


def _runs(keys: Sequence[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """Maximal [i, j) ranges of equal consecutive keys."""
    out = []
    i = 0
    while i < len(keys):
        j = i + 1
        while j < len(keys) and keys[j] == keys[i]:
            j += 1
        out.append((i, j))
        i = j
    return out


class _Engine:
    def __init__(self, ctx: FontContext, size_px: int, width: int, language: str, direction: Direction) -> None:
        self.ctx = ctx
        self.size = size_px
        self.width26 = width * 64
        self.language = language
        self.direction = direction
        self.shaper = _Shaper(ctx, size_px)

    # ------------------------------------------------------------------ paragraph analysis

    def paragraph(self, text: str) -> _Paragraph:
        cps = [ord(c) for c in text]
        u16 = Utf16Map.of(text)
        bounds = graphemes(text, u16)
        level = paragraph_level(text, self.direction, self.language)
        bidi = ParagraphBidi(text, level, u16)
        scripts = _resolve_scripts(cps, bounds)
        han_lang = han_language(self.language, set(scripts))
        faces: list[int] = []
        partial: list[str] = []
        previous: int | None = None
        for k in range(len(bounds) - 1):
            cluster = cps[bounds[k]:bounds[k + 1]]
            lang = _lang_for(scripts[k], self.language, han_lang)
            face, complete = self.ctx.choose(cluster, scripts[k], lang, self.size, previous,
                                             emoji_presentation(cluster))
            faces.append(face)
            if not complete:
                partial.append(text[bounds[k]:bounds[k + 1]])
            previous = face
        at = {b: k for k, b in enumerate(bounds)}
        breaks: list[int] = []
        hard: set[int] = set()
        break_lang = han_lang
        if "Hani" in scripts and han_lang.split("-", 1)[0].lower() not in ("zh", "ja", "ko", "yue"):
            break_lang = "zh"
        for pos, is_hard in line_breaks(text, break_lang, u16):
            k = at.get(pos)
            if k is not None and k > 0:
                breaks.append(k)
                if is_hard:
                    hard.add(k)
        if not breaks or breaks[-1] != len(bounds) - 1:
            breaks.append(len(bounds) - 1)
        space = [all(is_space(cp) for cp in cps[bounds[k]:bounds[k + 1]]) for k in range(len(bounds) - 1)]
        para = _Paragraph(text, cps, bounds, level, bidi, scripts, faces, self.language, han_lang, sorted(set(breaks)),
                          hard, [], space, partial)
        para.prefix = self._advances(para)
        return para

    def _advances(self, para: _Paragraph) -> list[int]:
        """Per-cluster advances from shaping each run with the whole paragraph as context."""
        k_count = para.clusters
        cluster_of = [0] * len(para.cps)
        for k in range(k_count):
            for i in range(para.bounds[k], para.bounds[k + 1]):
                cluster_of[i] = k
        keys = [(para.bidi.levels[para.bounds[k]], para.faces[k], para.scripts[k]) for k in range(k_count)]
        adv = [0] * k_count
        for i, j in _runs(keys):
            level, face, script = keys[i]
            lang = _lang_for(script, para.language, para.han_language)
            for _gid, cl, xa, _xo, _yo in self.shaper.shape(face, para.cps, para.bounds[i], para.bounds[j],
                                                             bool(level & 1), script, lang):
                adv[cluster_of[cl]] += xa
        prefix = [0]
        for a in adv:
            prefix.append(prefix[-1] + a)
        return prefix

    # ------------------------------------------------------------------ one line

    def build_line(self, para: _Paragraph, ks: int, ke: int, ellipsis: bool) -> _Line:
        """Shape and reorder clusters [ks, ke) of ``para`` as one line (optionally + ellipsis)."""
        start, end = para.bounds[ks], para.bounds[ke]
        cps = para.cps[start:end]
        cluster_bounds = [b - start for b in para.bounds[ks:ke + 1]]
        faces = para.faces[ks:ke]
        scripts = para.scripts[ks:ke]
        if ellipsis:
            tail, tail_faces, tail_scripts = self._ellipsis(para, ks, ke)
            if not tail:
                ellipsis = False
            for cp, face, script in zip(tail, tail_faces, tail_scripts, strict=True):
                cps.append(cp)
                cluster_bounds.append(cluster_bounds[-1] + 1)
                faces.append(face)
                scripts.append(script)
        if ellipsis:
            text = para.text[:end] + "".join(chr(c) for c in cps[end - start:])
            bidi = ParagraphBidi(text, para.level)
            levels = bidi.line_levels(start, len(text))
        else:
            levels = para.bidi.line_levels(start, end)
        n_clusters = len(cluster_bounds) - 1
        keys = [(levels[cluster_bounds[k]], faces[k], scripts[k]) for k in range(n_clusters)]
        items = _runs(keys)
        shaped = []
        for i, j in items:
            level, face, script = keys[i]
            lang = _lang_for(script, para.language, para.han_language)
            shaped.append(self.shaper.shape(face, cps, cluster_bounds[i], cluster_bounds[j], bool(level & 1),
                                            script, lang))
        glyphs: list[tuple[int, int, int, int, int]] = []
        pen = 0
        notdef = 0
        visible_end = end - start
        for idx in visual_order([keys[i][0] for i, _j in items]):
            face = keys[items[idx][0]][1]
            for gid, cl, xa, xo, yo in shaped[idx]:
                glyphs.append((face, gid, pen + xo, yo, start + cl if cl < visible_end else -1))
                pen += xa
                if gid == 0:
                    notdef += 1
        return _Line(glyphs, pen, notdef, start, end, ellipsis)

    def _ellipsis(self, para: _Paragraph, ks: int, ke: int) -> tuple[list[int], list[int], list[str]]:
        script = para.scripts[ke - 1] if ke > ks else (para.scripts[ks] if ks < para.clusters else "Zyyy")
        previous = para.faces[ke - 1] if ke > ks else None
        lang = _lang_for(script, para.language, para.han_language)
        face, ok = self.ctx.choose([ELLIPSIS], script, lang, self.size, previous, None)
        if ok:
            return [ELLIPSIS], [face], [script]
        face, ok = self.ctx.choose([0x2E], script, lang, self.size, previous, None)
        return ([0x2E] * 3, [face] * 3, [script] * 3) if ok else ([], [], [])

    # ------------------------------------------------------------------ breaking

    def _trim(self, para: _Paragraph, ks: int, ke: int) -> int:
        while ke > ks and para.space[ke - 1]:
            ke -= 1
        return ke

    def _fits(self, para: _Paragraph, ks: int, ke: int) -> bool:
        te = self._trim(para, ks, ke)
        return para.prefix[te] - para.prefix[ks] <= self.width26

    def _greedy_end(self, para: _Paragraph, ks: int) -> int:
        best = None
        for b in para.breaks:
            if b <= ks:
                continue
            if self._fits(para, ks, b):
                best = b
                if b in para.hard:
                    break
            else:
                break
        if best is not None:
            return best
        e = ks + 1  # the first word alone is too wide: break between clusters
        while e < para.clusters and self._fits(para, ks, e + 1):
            e += 1
        return e

    def lines(self, para: _Paragraph, max_lines: int | None, continued: bool,
              ellipsis: bool = True) -> tuple[list[_Line], bool]:
        """Break ``para`` into reshaped lines; True when text was cut (the last line then ends in an ellipsis)."""
        out: list[_Line] = []
        ks = 0
        k_count = para.clusters
        if k_count == 0:
            return [_Line([], 0, 0, 0, 0, False)], False
        while ks < k_count:
            if out and ks not in para.hard:
                while ks < k_count and para.space[ks]:
                    ks += 1
                if ks >= k_count:
                    break
            last_allowed = max_lines is not None and len(out) == max_lines - 1
            ke = self._greedy_end(para, ks)
            while True:
                te = self._trim(para, ks, ke)
                line = self.build_line(para, ks, te, ellipsis=False)
                if line.advance <= self.width26 or ke <= ks + 1:
                    break
                earlier = [b for b in para.breaks if ks < b < ke]
                ke = earlier[-1] if earlier else ke - 1
            rest = self._trim(para, ke, k_count) > ke
            if last_allowed and (rest or continued):
                out.append(self._ellipsis_line(para, ks, self._trim(para, ks, ke)) if ellipsis else line)
                return out, True
            out.append(line)
            ks = ke
        return out, False

    def _ellipsis_line(self, para: _Paragraph, ks: int, te: int) -> _Line:
        e = te
        while True:
            line = self.build_line(para, ks, self._trim(para, ks, e), ellipsis=True)
            if line.advance <= self.width26 or e <= ks:
                return line
            # Estimate how many clusters to drop from the overflow, then verify by reshaping.
            over = line.advance - self.width26
            step = e - 1
            while step > ks and para.prefix[e] - para.prefix[step] < over:
                step -= 1
            e = max(ks, min(e - 1, step))


# --------------------------------------------------------------------------- public API


_cache: OrderedDict[tuple, TextBlock] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 1024


def layout_text(text: str, fonts: FontSet, *, width: int, size_px: int = 16, language: str = "",
                direction: Direction = "auto", align: Align = "start", max_lines: int | None = None,
                ellipsis: bool = True, line_spacing: int = 0) -> TextBlock:
    """Lay out ``text`` (plain; ``\\n`` separates paragraphs) in a box ``width`` pixels wide.

    ``language`` is a BCP-47 hint (face choice for Han, HarfBuzz language,
    locale-tailored line breaking, paragraph direction without strong
    characters). ``max_lines`` cuts the text, ending the last line with an
    ellipsis unless ``ellipsis`` is False. Results are cached (TextBlock is
    immutable).
    """
    if not 1 <= width <= MAX_WIDTH:
        raise ValueError(f"width {width} outside 1..{MAX_WIDTH}")
    if max_lines is not None and max_lines < 1:
        raise ValueError("max_lines must be >= 1")
    language = normalize_language(language)
    key = (fonts.pack_id, str(fonts.pack_path), id(fonts), text, width, size_px, language, direction, align,
           max_lines, ellipsis, line_spacing)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    block = _layout(text, FontContext.for_fontset(fonts), width, size_px, language, direction, align, max_lines,
                    ellipsis, line_spacing)
    with _cache_lock:
        _cache[key] = block
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return block


def measure_text(text: str, fonts: FontSet, *, size_px: int = 16, language: str = "") -> int:
    """Advance width in pixels of ``text`` on one line (no wrapping)."""
    return layout_text(text, fonts, width=MAX_WIDTH, size_px=size_px, language=language, max_lines=1).width


def _layout(text: str, ctx: FontContext, width: int, size_px: int, language: str, direction: Direction,
            align: Align, max_lines: int | None, ellipsis: bool, line_spacing: int) -> TextBlock:
    text = nfc(_CONTROLS.sub(lambda m: " " if m.group(0) == "\t" else "",
                             text.replace("\r\n", "\n").replace("\r", "\n")))
    engine = _Engine(ctx, size_px, width, language, direction)
    base = ctx.fonts.strike(ctx.base_face(size_px), size_px)
    paragraphs = text.split("\n")
    offsets = []
    pos = 0
    for p in paragraphs:
        offsets.append(pos)
        pos += len(p) + 1

    lines: list[LineBox] = []
    glyphs: list[PositionedGlyph] = []
    truncated = False
    partial: dict[str, None] = {}
    notdef = shaped = 0
    y = 0
    for p_index, (ptext, offset) in enumerate(zip(paragraphs, offsets, strict=True)):
        remaining = None if max_lines is None else max_lines - len(lines)
        if remaining is not None and remaining <= 0:
            truncated = truncated or any(p.strip() for p in paragraphs[p_index:])
            break
        para = engine.paragraph(ptext)
        partial.update(dict.fromkeys(para.partial))
        continued = remaining is not None and any(p.strip() for p in paragraphs[p_index + 1:])
        plines, cut = engine.lines(para, remaining, continued, ellipsis)
        truncated = truncated or cut
        rtl = bool(para.level & 1)
        for line in plines:
            box, lglyphs = _place(ctx, size_px, width, align, rtl, base.ascent, base.descent, line, offset,
                                  len(lines), y)
            lines.append(box)
            glyphs.extend(lglyphs)
            notdef += line.notdef
            shaped += len(line.glyphs)
            y = box.bottom + line_spacing
    height = lines[-1].bottom if lines else 0
    unsupported = ctx.index.check_text(text)
    return TextBlock(text, size_px, width, tuple(lines), tuple(glyphs), height, truncated, unsupported, notdef,
                     shaped, tuple(partial))


def _place(ctx: FontContext, size_px: int, width: int, align: Align, rtl: bool, base_ascent: int,
           base_descent: int, line: _Line, offset: int, index: int, top: int) -> tuple[LineBox, list[PositionedGlyph]]:
    adv_px = (line.advance + 63) >> 6
    side = align
    if align == "start":
        side = "right" if rtl else "left"
    elif align == "end":
        side = "left" if rtl else "right"
    if side == "right":
        x0 = width - adv_px
    elif side == "center":
        x0 = (width - adv_px) // 2
    else:
        x0 = 0
    placed = []
    ascent, descent = base_ascent, base_descent
    for face, gid, gx, yo, cl in line.glyphs:
        x = (x0 * 64 + gx + 32) >> 6
        dy = (-yo + 32) >> 6
        ink = ctx.ink(face, size_px, gid)
        if ink is None:
            continue
        ascent = max(ascent, -(dy + ink[1]))
        descent = max(descent, dy + ink[3])
        placed.append((face, gid, x, dy, cl))
    baseline = top + ascent
    glyphs = [PositionedGlyph(face, size_px, gid, x, baseline + dy, (offset + cl) if cl >= 0 else -1, index)
              for face, gid, x, dy, cl in placed]
    box = LineBox(offset + line.start, offset + line.end, x0, adv_px, top, baseline, baseline + descent, rtl,
                  line.ellipsis)
    return box, glyphs
