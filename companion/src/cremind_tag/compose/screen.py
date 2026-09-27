"""Screen composition: a tag's active cards -> one logical screen (docs/layout.md "Screen model").

``compose_screen`` implements the `Composer` protocol of `compose.api`::

    ┌────────────────────────────────────────────┐
    │ Desk                    Sun, Sep 27, 2:05 PM│  header: tag name · local date and time
    ├────────────────────────────────────────────┤
    │ [icon] Approve deployment?           [QR]  │  headline card: icon, title (red on BWR for
    │        2:01 PM                             │  needs_input / error), time, progress bar,
    │        [██████████░░░░░░]  3/10            │  body (only with excerpts enabled)
    │        body text …                         │
    ├────────────────────────────────────────────┤
    │ [i] Second card title …          1:40 PM   │  up to three more cards
    │ [i] Third card title …            Sep 26   │
    ├────────────────────────────────────────────┤
    │ 4 more updates waiting for this tag · Updated 2:05 PM │  footer
    └────────────────────────────────────────────┘

The logical canvas is the panel turned by ``panel.rotation`` (400x300
landscape, 300x400 portrait on a 4.2" panel); every size comes from the
canvas, so other panels work too (small panels drop the header and list
rows). A right-to-left profile language mirrors the chrome (icons on the
right); each text is aligned by its own paragraph direction.

The screen must stay within ``MAX_BYTES`` = ``min(LAYOUT_HARD_MAX,
LAYOUT_SERIAL_MAX)`` bytes (4000: DELIVER_LAYOUT's CBOR envelope has to fit one
serial frame), ``LAYOUT_MAX_GLYPHS`` glyphs and ``LAYOUT_MAX_COMMANDS``
commands. The body gets whatever budget the rest leaves (fewer lines, down to
none); beyond that the composer walks the
fixed `PLANS` list — fewer list rows, fewer and then smaller title lines —
and takes the first plan that fits. Everything is a pure function of the
inputs (``now`` included), so the same cards give the same bytes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import icu

from cremind_tag.compose.api import ActiveCard, ComposedScreen, ScreenSettings, TagPanel
from cremind_tag.compose.cards import CardView, ordered_views
from cremind_tag.compose.timefmt import DATE_TIME, TIME, card_stamp, format_time
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.layout.engine import TextBlock, layout_text
from cremind_tag.layout.fonts import FontContext
from cremind_tag.layout.unicode import icu_locale, is_rtl_language
from cremind_tag.protocol.ids import (
    ICON_SIZES,
    LAYOUT_HARD_MAX,
    LAYOUT_MAX_COMMANDS,
    LAYOUT_MAX_GLYPHS,
    LAYOUT_SERIAL_MAX,
    Color,
    Icon,
    QrEcc,
)
from cremind_tag.protocol.layout import (
    Command,
    Glyphs,
    Layout,
    LayoutError,
    Line,
    Progress,
    Qr,
    Rect,
    check_panel,
    check_strikes,
    encode_layout,
    qr_code,
)
from cremind_tag.protocol.layout import Icon as IconCmd
from cremind_tag.protocol.msgs import (
    LayoutCmdGlyphs,
    LayoutCmdIcon,
    LayoutCmdLine,
    LayoutCmdProgress,
    LayoutCmdQr,
    LayoutCmdRect,
    LayoutGlyph,
    LayoutHeader,
)

SMALL = 16
GAP = 6
QR_QUIET = 2
"""Quiet-zone modules left white around a QR code."""
MAX_LIST_ROWS = 3
MAX_BYTES = min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX)
"""Largest layout the companion produces: DELIVER_LAYOUT's CBOR envelope must fit one serial frame."""


@dataclass(frozen=True)
class Plan:
    """One step of the degradation ladder."""

    title_sizes: tuple[int, ...]
    """Title sizes to try, largest first; all but the last are used only when the whole title fits in 2 lines."""
    title_lines: int
    list_rows: int
    body: bool = True
    header_date: bool = True
    qr: bool = True


PLANS: tuple[Plan, ...] = (
    Plan((32, 24), 3, 3),
    Plan((32, 24), 3, 2),
    Plan((32, 24), 3, 1),
    Plan((32, 24), 3, 0),
    Plan((24,), 2, 0),
    Plan((24,), 1, 0, header_date=False),
    Plan((16,), 2, 0, header_date=False),
    Plan((16,), 1, 0, body=False, header_date=False, qr=False),
)


def logical_size(panel: TagPanel) -> tuple[int, int]:
    """Logical canvas for the panel's rotation (§4.4 Rotation)."""
    return (panel.width, panel.height) if panel.rotation % 2 == 0 else (panel.height, panel.width)


def _cost(cmd: Command) -> int:
    """Encoded bytes of one command."""
    if isinstance(cmd, Glyphs):
        return 1 + LayoutCmdGlyphs.LEN + LayoutGlyph.LEN * len(cmd.glyphs)
    if isinstance(cmd, Qr):
        return 1 + LayoutCmdQr.LEN + len(cmd.text)
    return 1 + {IconCmd: LayoutCmdIcon, Line: LayoutCmdLine, Progress: LayoutCmdProgress}.get(
        type(cmd), LayoutCmdRect).LEN


class _Budget(Exception):
    """The plan does not fit the §4.3 limits."""


class _Screen:
    """Commands of one screen under construction, with the §4.3 budget and RTL mirroring."""

    def __init__(self, panel: TagPanel, fonts: FontSet, settings: ScreenSettings) -> None:
        self.panel = panel
        self.fonts = fonts
        self.ctx = FontContext.for_fontset(fonts)
        self.width, self.height = logical_size(panel)
        self.rtl = is_rtl_language(settings.language)
        self.language = settings.language
        self.margin = 8 if min(self.width, self.height) >= 200 else 4
        self.content_width = self.width - 2 * self.margin
        self.commands: list[Command] = []
        self.glyphs = 0
        self.bytes = LayoutHeader.LEN
        self.red = panel.planes == 2
        self.uses_red = False
        self.unsupported: dict[str, None] = {}
        self.sizes = self.ctx.text_sizes()

    # ------------------------------------------------------------------ budget

    def room(self) -> tuple[int, int, int]:
        """Glyphs, commands and bytes still available."""
        return (LAYOUT_MAX_GLYPHS - self.glyphs, LAYOUT_MAX_COMMANDS - len(self.commands), MAX_BYTES - self.bytes)

    @staticmethod
    def cost(cmds: Sequence[Command]) -> tuple[int, int, int]:
        return (sum(len(c.glyphs) for c in cmds if isinstance(c, Glyphs)), len(cmds), sum(_cost(c) for c in cmds))

    def fits(self, cmds: Sequence[Command]) -> bool:
        g, n, b = self.cost(cmds)
        rg, rn, rb = self.room()
        return g <= rg and n <= rn and b <= rb

    def add(self, cmds: Sequence[Command]) -> None:
        if not self.fits(cmds):
            raise _Budget
        g, n, b = self.cost(cmds)
        self.glyphs += g
        self.bytes += b
        self.commands.extend(cmds)
        if any(getattr(c, "color", Color.BLACK) == Color.RED for c in cmds):
            self.uses_red = True

    # ------------------------------------------------------------------ geometry

    def x(self, x: int, w: int) -> int:
        """Left edge of a ``w``-wide element placed ``x`` from the start side (mirrored in RTL UIs)."""
        return self.width - x - w if self.rtl else x

    @property
    def ui_align(self) -> str:
        return "right" if self.rtl else "left"

    def color(self, red: bool) -> int:
        return Color.RED if red and self.red else Color.BLACK

    def size(self, want: int) -> int:
        """``want`` when the pack has that text size, else the largest smaller one (else the smallest)."""
        fitting = [s for s in self.sizes if s <= want]
        return max(fitting) if fitting else min(self.sizes)

    def title_size(self, want: int) -> int:
        """Title sizes step down one size on small panels (short side under 200 px)."""
        if min(self.width, self.height) < 200:
            want = {32: 24, 24: 16}.get(want, want)
        return self.size(want)

    def icon_size(self, want: int) -> int:
        have = [s for s in ICON_SIZES if s <= want and self.fonts.has_strike(0, s)]
        return max(have) if have else 0

    # ------------------------------------------------------------------ drawing

    def block(self, text: str, width: int, size: int, *, language: str | None = None, max_lines: int | None = 1,
              align: str = "start", direction: str = "auto") -> TextBlock:
        return layout_text(text, self.fonts, width=max(1, width), size_px=self.size(size),
                           language=language if language is not None else self.language,
                           align=align, direction=direction, max_lines=max_lines)  # type: ignore[arg-type]

    def chrome(self, text: str, width: int, size: int = SMALL, *, align: str | None = None) -> TextBlock:
        """A composer string (English sentence around localised values): LTR paragraph, UI-side aligned."""
        return self.block(text, width, size, align=align or self.ui_align, direction="ltr")

    def text_cmds(self, block: TextBlock, x: int, y: int, *, red: bool = False) -> list[Glyphs]:
        return block.commands(x, y, self.color(red))

    def draw_text(self, block: TextBlock, x: int, y: int, *, red: bool = False) -> None:
        self.add(self.text_cmds(block, x, y, red=red))
        for ch in block.unsupported:
            self.unsupported.setdefault(ch, None)

    def draw_icon(self, icon: int, size: int, x: int, y: int, *, red: bool = False) -> None:
        if size:
            self.add([IconCmd(int(icon), size, self.color(red), x, y)])

    def hline(self, y: int) -> None:
        self.add([Line(self.margin, y, self.width - self.margin - 1, y, 1, Color.BLACK)])

    def layout(self) -> Layout:
        return Layout(self.width, self.height, self.panel.rotation, Color.WHITE, tuple(self.commands),
                      flags=1 if self.uses_red else 0)


# --------------------------------------------------------------------------- parts


def _header(s: _Screen, now: datetime, settings: ScreenSettings, plan: Plan) -> int:
    """Tag name and local time; returns the y of the first free row below the separator."""
    if s.height < 160:
        return s.margin // 2
    clock_text = format_time(now, settings.language, settings.timezone, DATE_TIME if plan.header_date else TIME)
    clock = s.block(clock_text, s.content_width * 2 // 3, SMALL, align=s.ui_align)
    name_width = s.content_width - clock.width - 2 * GAP
    name_text = s.panel.name.strip() or f"Tag {s.panel.tag_id:08X}"
    name = s.block(name_text, name_width, SMALL, align=s.ui_align) if name_width >= SMALL else None
    top = 3
    baseline = max(clock.lines[0].baseline, name.lines[0].baseline if name else 0)
    s.draw_text(clock, s.x(s.width - s.margin - clock.width, clock.width) - clock.lines[0].x,
                top + baseline - clock.lines[0].baseline)
    bottom = top + max(clock.height + baseline - clock.lines[0].baseline,
                       (name.height + baseline - name.lines[0].baseline) if name else 0)
    if name is not None:
        s.draw_text(name, s.x(s.margin, name_width), top + baseline - name.lines[0].baseline)
    sep = bottom + 2
    s.hline(sep)
    return sep + 4


def footer_texts(pending: int, updated: str) -> list[str]:
    """Footer wordings, longest first; the composer draws the first that fits on one line."""
    if pending <= 0:
        return [f"Updated {updated}"]
    noun = "update" if pending == 1 else "updates"
    return [f"{pending} more {noun} waiting for this tag · Updated {updated}",
            f"{pending} more {noun} waiting · Updated {updated}",
            f"{pending} more {noun} · Updated {updated}",
            f"+{pending} · Updated {updated}"]


def _footer(s: _Screen, pending: int, now: datetime, settings: ScreenSettings) -> TextBlock:
    updated = format_time(now, settings.language, settings.timezone, TIME)
    block = None
    for text in footer_texts(pending, updated):
        block = s.chrome(text, s.content_width)
        if not block.truncated:
            break
    assert block is not None
    return block


@dataclass
class _Row:
    view: CardView
    title: TextBlock
    stamp: TextBlock
    height: int
    baseline: int


def _rows(s: _Screen, views: Sequence[CardView], now: datetime, settings: ScreenSettings) -> list[_Row]:
    icon = s.icon_size(SMALL)
    rows = []
    for view in views:
        stamp = s.block(card_stamp(view.ts, now, settings.language, settings.timezone), s.content_width // 3, SMALL,
                        align=s.ui_align)
        width = s.content_width - icon - GAP - stamp.width - GAP
        title = s.block(view.title, width, SMALL, language=view.language, align=s.ui_align)
        baseline = max(title.lines[0].baseline, stamp.lines[0].baseline)
        height = max(baseline + max(title.height - title.lines[0].baseline, stamp.height - stamp.lines[0].baseline),
                     icon)
        rows.append(_Row(view, title, stamp, height, baseline))
    return rows


def _draw_row(s: _Screen, row: _Row, y: int) -> None:
    icon = s.icon_size(SMALL)
    ascent = s.fonts.strike(s.ctx.base_face(SMALL), SMALL).ascent
    # Icon bottom sits a little under the baseline, like a capital letter's.
    icon_y = y + row.baseline - ascent + max(0, (ascent - icon) // 2 + 2)
    s.draw_icon(row.view.icon, icon, s.x(s.margin, icon), icon_y, red=row.view.red)
    title_width = row.title.box_width
    s.draw_text(row.title, s.x(s.margin + icon + GAP, title_width), y + row.baseline - row.title.lines[0].baseline)
    stamp = row.stamp
    s.draw_text(stamp, s.x(s.width - s.margin - stamp.width, stamp.width) - stamp.lines[0].x,
                y + row.baseline - stamp.lines[0].baseline)


def _number(n: int, language: str) -> str:
    return str(icu.NumberFormat.createInstance(icu_locale(language or "en")).format(n))


def _qr_geometry(s: _Screen, link: bytes, zone_h: int) -> tuple[int, int] | None:
    """(module_px, side incl. quiet zone) of the largest QR that fits the headline zone."""
    try:
        modules = qr_code(link, int(QrEcc.LOW)).get_size()
    except LayoutError:
        return None
    for module in (4, 3, 2):
        side = (modules + 2 * QR_QUIET) * module
        if side <= zone_h and side <= s.content_width // 3:
            return module, side
    return None


def _headline(s: _Screen, view: CardView, top: int, bottom: int, plan: Plan, now: datetime,
              settings: ScreenSettings) -> None:
    zone_h = bottom - top
    cw = s.content_width
    icon = s.icon_size(48 if zone_h >= 110 and cw >= 300 else 32 if zone_h >= 60 and cw >= 200 else 24)
    qr = _qr_geometry(s, view.link, zone_h) if plan.qr and view.link else None
    text_left = s.margin + (icon + GAP if icon else 0)
    text_width = cw - (icon + GAP if icon else 0) - (qr[1] + GAP if qr else 0)

    meta = s.block(card_stamp(view.ts, now, settings.language, settings.timezone, long=True), text_width, SMALL,
                   align=s.ui_align)
    progress_h = 0
    label = None
    if view.progress:
        done, total = view.progress
        label = s.block(f"{_number(done, settings.language)}/{_number(total, settings.language)}", text_width // 3,
                        SMALL, align="left")
        progress_h = label.height + 2
    reserve = meta.height + 2 + progress_h

    # Title: a larger size only when the whole title fits in two lines, else the plan's size and line count.
    sizes = list(dict.fromkeys(s.title_size(z) for z in plan.title_sizes))
    title = None
    for i, size in enumerate(sizes):
        last = i == len(sizes) - 1
        lines = plan.title_lines if last else 2
        while True:
            title = s.block(view.title, text_width, size, language=view.language, max_lines=lines)
            if title.height + reserve <= zone_h or lines == 1:
                break
            lines -= 1
        if last or (not title.truncated and title.height + reserve <= zone_h):
            break
    assert title is not None

    y = top
    s.draw_icon(view.icon, icon, s.x(s.margin, icon), top, red=view.red)
    if qr:
        module, side = qr
        s.add([Qr(s.x(s.margin + cw - side, side) + QR_QUIET * module, top + QR_QUIET * module, module,
                  int(QrEcc.LOW), Color.BLACK, view.link)])
    s.draw_text(title, s.x(text_left, text_width), y, red=view.red)
    y += title.height + 2
    s.draw_text(meta, s.x(text_left, text_width), y)
    y += meta.height + 2
    if view.progress and label is not None:
        done, total = view.progress
        scale = max(1, -(-total // 0xFFFF))
        bar_w = max(8, text_width - label.width - GAP)
        bar_h = 12
        base = label.lines[0].baseline
        bar_y = y + max(0, base - bar_h + 1)
        s.add([Progress(s.x(text_left, bar_w), bar_y, bar_w, bar_h, done // scale, total // scale, Color.BLACK)])
        s.draw_text(label, s.x(text_left + bar_w + GAP, label.width) - label.lines[0].x, y)
        y += progress_h
    if view.body and plan.body:
        _body(s, view, text_left, text_width, y + 2, bottom)


def _body(s: _Screen, view: CardView, x: int, width: int, top: int, bottom: int) -> None:
    """The body gets the height and the §4.3 budget left over: as many lines as fit both."""
    base = s.fonts.strike(s.ctx.base_face(SMALL), SMALL)
    lines = (bottom - top) // max(1, base.ascent + base.descent)
    while lines >= 1:
        block = s.block(view.body or "", width, SMALL, language=view.language, max_lines=lines)
        if block.height <= bottom - top:
            cmds = s.text_cmds(block, s.x(x, width), top)
            if s.fits(cmds):
                s.draw_text(block, s.x(x, width), top)
                return
        lines -= 1


def _empty(s: _Screen, top: int, bottom: int) -> None:
    icon = s.icon_size(48 if bottom - top >= 110 else 32)
    text = s.chrome("No updates", s.content_width, 24 if bottom - top >= 90 else SMALL, align="center")
    total = icon + GAP + text.height
    y = top + max(0, (bottom - top - total) // 2)
    s.draw_icon(Icon.CHECK_CIRCLE, icon, (s.width - icon) // 2, y)
    s.draw_text(text, s.margin, y + icon + GAP)


# --------------------------------------------------------------------------- entry points


def _compose(panel: TagPanel, views: list[CardView], fonts: FontSet, settings: ScreenSettings, now: datetime,
             plan: Plan) -> tuple[_Screen, list[int], list[int]]:
    s = _Screen(panel, fonts, settings)
    top = _header(s, now, settings, plan)
    headline, others = (views[0], views[1:]) if views else (None, [])

    # Footer height does not depend on the count; size it with the worst case, draw it last.
    probe = _footer(s, len(views), now, settings)
    footer_top = s.height - s.margin // 2 - probe.height
    list_bottom = footer_top - 4
    min_headline = 0
    if headline is not None:
        title_size = s.title_size(min(plan.title_sizes))
        strike = s.fonts.strike(s.ctx.base_face(title_size), title_size)
        small = s.fonts.strike(s.ctx.base_face(SMALL), SMALL)
        min_headline = strike.ascent + strike.descent + small.ascent + small.descent + 4

    rows = _rows(s, others[:min(plan.list_rows, MAX_LIST_ROWS)], now, settings)
    while rows and list_bottom - sum(r.height + 3 for r in rows) - 5 - top < min_headline:
        rows.pop()
    rows_top = list_bottom - sum(r.height + 3 for r in rows)
    headline_bottom = (rows_top - 5) if rows else list_bottom
    shown = [v.delivery_id for v in views[:1 + len(rows)]]
    pending = [v.delivery_id for v in views[1 + len(rows):]]

    s.hline(footer_top - 2)
    if rows:
        s.hline(rows_top - 3)
        y = rows_top
        for row in rows:
            _draw_row(s, row, y)
            y += row.height + 3
    footer = _footer(s, len(pending), now, settings)
    s.draw_text(footer, s.x(s.margin, s.content_width), footer_top)
    if headline is None:
        _empty(s, top, headline_bottom)
    else:
        _headline(s, headline, top, headline_bottom, plan, now, settings)
    return s, shown, pending


def _finish(s: _Screen) -> bytes:
    layout = s.layout()
    data = encode_layout(layout)  # §4.3 structural checks
    if len(data) > MAX_BYTES:
        raise _Budget
    check_strikes(layout, s.fonts.has_strike)
    check_panel(layout, s.panel.width, s.panel.height)
    return data


def compose_screen(panel: TagPanel, cards: list[ActiveCard], fonts: FontSet, settings: ScreenSettings,
                   now: datetime) -> ComposedScreen:
    """The `Composer`: header, headline card, up to three more cards, footer (module docstring)."""
    views = ordered_views(cards, settings)
    last_error: Exception | None = None
    for plan in PLANS:
        try:
            s, shown, pending = _compose(panel, views, fonts, settings, now, plan)
            data = _finish(s)
        except (_Budget, LayoutError) as exc:
            last_error = exc
            continue
        return ComposedScreen(data, tuple(shown), len(pending), tuple(s.unsupported), tuple(pending))
    raise RuntimeError(f"no plan fits the layout limits: {last_error!r}")


def compose_identify(panel: TagPanel, fonts: FontSet, tag_id: int | str | None = None) -> ComposedScreen:
    """"Which tag is this?": a thick frame, an info icon, the tag id in the largest size and the tag's name.

    What the daemon delivers (as a new revision) for the ``identify`` command.
    ``tag_id`` defaults to ``panel.tag_id`` (an int prints as 8 hex digits).
    """
    tid = panel.tag_id if tag_id is None else tag_id
    id_text = f"{tid:08X}" if isinstance(tid, int) else str(tid)
    for name_lines in (2, 1, 0):
        s = _Screen(panel, fonts, ScreenSettings())
        big = s.chrome(id_text, s.content_width, 32, align="center")
        name = s.block(panel.name.strip(), s.content_width, 24, language="", align="center", max_lines=name_lines) \
            if panel.name.strip() and name_lines else None
        icon = s.icon_size(48 if s.height >= 200 else 32)
        total = icon + GAP + big.height + ((GAP + name.height) if name else 0)
        y = max(s.margin, (s.height - total) // 2)
        border = 4 if min(s.width, s.height) >= 150 else 2
        try:
            s.add([Rect(0, 0, s.width, s.height, border, Color.BLACK)])
            s.draw_icon(Icon.INFO, icon, (s.width - icon) // 2, y)
            y += icon + GAP
            s.draw_text(big, s.margin, y)
            if name:
                s.draw_text(name, s.margin, y + big.height + GAP)
            return ComposedScreen(_finish(s), (), 0, tuple(s.unsupported))
        except _Budget:
            continue
    raise RuntimeError("the identify screen does not fit the layout limits")


def compose_blank(panel: TagPanel) -> ComposedScreen:
    """An all-white screen (``clear`` jobs, ownership changes)."""
    w, h = logical_size(panel)
    layout = Layout(w, h, panel.rotation, Color.WHITE, ())
    data = encode_layout(layout)
    check_panel(layout, panel.width, panel.height)
    return ComposedScreen(data, (), 0)
