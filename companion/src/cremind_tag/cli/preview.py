"""`cremind-tag preview` — render cards, screens or text to PNG exactly as a bridge would draw them.

Every image goes through the companion's layout engine and the normative
renderer with a built font pack (docs/layout.md). ``--pack`` defaults to
``<repo>/fonts/out/full/fontpack.ctfp`` (else the dev pack); the font cache to
``$CREMIND_TAG_FONT_CACHE`` or ``<repo>/fonts/cache``.
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(name="preview", help="Render cards, screens or text to PNG exactly as a bridge would draw them.",
                  no_args_is_help=True)

PackOpt = typer.Option(None, "--pack", help="Font pack (.ctfp; default: <repo>/fonts/out/full, else dev).")
CacheOpt = typer.Option(None, "--cache", help="Font cache (default: $CREMIND_TAG_FONT_CACHE or <repo>/fonts/cache).")
ScaleOpt = typer.Option(1, "--scale", min=1, max=8, help="Enlarge pixels in the PNG (nearest neighbour).")


@app.callback()
def _preview() -> None:
    """Render cards, screens or text to PNG."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _fonts(pack: Path | None, cache: Path | None) -> Any:
    from cremind_tag.fonts.fontset import FontSet
    from cremind_tag.fonts.manifest import ManifestError, repo_root

    if pack is None:
        try:
            out = repo_root() / "fonts" / "out"
        except ManifestError as exc:
            raise _fail(f"{exc}; pass --pack") from None
        pack = next((p for p in (out / "full" / "fontpack.ctfp", out / "dev" / "fontpack.ctfp") if p.is_file()), None)
        if pack is None:
            raise _fail(f"no pack under {out}; run `cremind-tag fonts build` or pass --pack")
    try:
        return FontSet.load(pack, cache)
    except (OSError, ValueError) as exc:
        raise _fail(str(exc)) from None


def _panel(kind: str, rotation: int, name: str, tag_id: int, width: int, height: int) -> Any:
    from cremind_tag.compose.api import TagPanel

    if kind not in ("bw", "bwr"):
        raise _fail("--panel must be bw or bwr")
    return TagPanel(tag_id, width, height, 2 if kind == "bwr" else 1, 0x03 if kind == "bwr" else 0x01, rotation, name)


def _now(value: str | None) -> datetime:
    from cremind_tag.compose.timefmt import parse_timestamp

    if not value:
        return datetime.now(UTC).replace(microsecond=0)
    parsed = parse_timestamp(value)
    if parsed is None:
        raise _fail(f"--now {value!r} is not an ISO 8601 time")
    return parsed


def _active_cards(doc: Any, now: datetime) -> list[Any]:
    """Jobs (connector job shape) or bare cards -> ActiveCard list."""
    from cremind_tag.compose.api import ActiveCard
    from cremind_tag.compose.timefmt import parse_timestamp

    items = doc if isinstance(doc, list) else doc.get("cards", doc.get("jobs", [doc])) if isinstance(doc, dict) else []
    out = []
    for n, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise _fail(f"card {n} is not an object")
        card = item.get("card") if isinstance(item.get("card"), dict) else item
        kind = str(item.get("kind") or card.get("kind") or "notification")
        created = parse_timestamp(item.get("created_at")) or parse_timestamp(card.get("ts")) or now
        priority = item.get("priority")
        if not isinstance(priority, int):
            from cremind_tag.compose.cards import KIND_ICONS

            priority = 90 if kind == "needs_input" else 50 if kind in KIND_ICONS else 40
        out.append(ActiveCard(int(item.get("delivery_id") or n), kind, priority, created, card))
    return out


def _settings(doc: Any, language: str | None, timezone: str | None, excerpts: bool | None,
              qr: bool | None) -> Any:
    from cremind_tag.compose.api import ScreenSettings

    s = doc.get("settings", {}) if isinstance(doc, dict) else {}
    return ScreenSettings(
        show_excerpts=bool(s.get("show_excerpts", False)) if excerpts is None else excerpts,
        qr_links=bool(s.get("qr_links", False)) if qr is None else qr,
        timezone=timezone or str(s.get("timezone") or "UTC"),
        language=language or str(s.get("language") or "en"),
    )


def _report_screen(screen: Any, out: Path, png: bytes) -> None:
    from cremind_tag.protocol.layout import Glyphs, decode_layout

    layout = decode_layout(screen.layout)
    glyphs = sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs))
    typer.echo(f"{out}: {layout.width}x{layout.height} rotation {layout.rotation}, {len(screen.layout)} bytes, "
               f"{len(layout.commands)} commands, {glyphs} glyphs, PNG {len(png)} bytes")
    typer.echo(f"shown deliveries {list(screen.delivery_ids)}, pending {screen.pending_count} "
               f"{list(screen.pending_delivery_ids)}")
    if screen.unsupported_chars:
        typer.secho("unsupported: " + " ".join(f"U+{ord(c):04X}" for c in screen.unsupported_chars),
                    fg=typer.colors.YELLOW)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"{path}: {exc}") from None


@app.command()
def text(
    value: str = typer.Argument(..., help="Text to lay out ('\\n' in the text separates paragraphs)."),
    size: int = typer.Option(24, "--size", help="Strike size in px (16, 24, 32)."),
    width: int = typer.Option(380, "--width", min=1, max=4000, help="Box width in px."),
    lang: str = typer.Option("", "--lang", help="BCP-47 language hint (face choice, breaking, direction)."),
    direction: str = typer.Option("auto", "--dir", help="Paragraph direction: auto, ltr or rtl."),
    align: str = typer.Option("start", "--align", help="start, end, center, left or right."),
    max_lines: int | None = typer.Option(None, "--max-lines", min=1, help="Cut with an ellipsis after N lines."),
    red: bool = typer.Option(False, "--red", help="Draw in red (a black/white/red preview)."),
    out: Path = typer.Option(Path("preview-text.png"), "--out", "-o", help="PNG to write."),
    scale: int = ScaleOpt,
    pack: Path | None = PackOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Lay out one text and render it; prints lines, faces and unsupported characters."""
    from cremind_tag.compose.preview import render_png
    from cremind_tag.layout import layout_text
    from cremind_tag.protocol.ids import Color
    from cremind_tag.protocol.layout import Layout

    if direction not in ("auto", "ltr", "rtl") or align not in ("start", "end", "center", "left", "right"):
        raise _fail("--dir must be auto/ltr/rtl and --align start/end/center/left/right")
    fonts = _fonts(pack, cache)
    if not fonts.has_strike(1 if any(f.face_id == 1 for f in fonts.faces) else fonts.faces[1].face_id, size):
        raise _fail(f"the pack has no {size} px strikes")
    block = layout_text(value.replace("\\n", "\n"), fonts, width=width, size_px=size, language=lang,
                        direction=direction, align=align, max_lines=max_lines)  # type: ignore[arg-type]
    pad = 4
    layout = Layout(width + 2 * pad, max(1, block.height + 2 * pad), 0, Color.WHITE,
                    tuple(block.commands(pad, pad, Color.RED if red else Color.BLACK)))
    png = render_png(layout, fonts, scale=scale)
    out.write_bytes(png)
    typer.echo(f"{out}: {len(block.lines)} lines, {len(block.glyphs)} glyphs ({block.shaped} shaped), "
               f"faces {list(block.faces)}, {'truncated, ' if block.truncated else ''}PNG {len(png)} bytes")
    for n, line in enumerate(block.lines, start=1):
        shown = block.text[line.start:line.end] + ("…" if line.ellipsis else "")
        typer.echo(f"  {n}: x={line.x} w={line.width} top={line.top} baseline={line.baseline} "
                   f"{'rtl' if line.rtl else 'ltr'} | {shown}")
    if block.unsupported or block.notdef or block.unsupported_clusters:
        typer.secho("unsupported: " + " ".join(f"U+{ord(c):04X}" for c in block.unsupported)
                    + f" ({block.notdef} .notdef glyphs; clusters no single face maps: "
                    + ", ".join(" ".join(f"U+{ord(c):04X}" for c in cl) for cl in block.unsupported_clusters) + ")",
                    fg=typer.colors.YELLOW)


@app.command()
def card(
    path: Path = typer.Argument(..., help="JSON file: a delivery job (with 'card') or a bare card."),
    panel: str = typer.Option("bw", "--panel", help="bw (black/white) or bwr (black/white/red)."),
    rotation: int = typer.Option(0, "--rotation", min=0, max=3, help="Quarter turns (1 = portrait on 400x300)."),
    width: int = typer.Option(400, "--width", help="Native panel width."),
    height: int = typer.Option(300, "--height", help="Native panel height."),
    name: str = typer.Option("Tag", "--name", help="Tag name for the header."),
    lang: str | None = typer.Option(None, "--lang", help="Profile language (default: settings or en)."),
    tz: str | None = typer.Option(None, "--tz", help="Profile time zone (default: settings or UTC)."),
    excerpts: bool | None = typer.Option(None, "--excerpts/--no-excerpts", help="Show bodies."),
    qr: bool | None = typer.Option(None, "--qr/--no-qr", help="Allow QR links."),
    now: str | None = typer.Option(None, "--now", help="Screen time (ISO 8601; default: now)."),
    out: Path = typer.Option(Path("preview-card.png"), "--out", "-o"),
    scale: int = ScaleOpt,
    pack: Path | None = PackOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Compose a screen showing one card and render it."""
    _screen_command(path, panel, rotation, width, height, name, lang, tz, excerpts, qr, now, out, scale, pack, cache,
                    single=True)


@app.command()
def screen(
    path: Path = typer.Argument(..., help="JSON: a list of jobs/cards, or {cards|jobs: [...], settings: {...}}."),
    panel: str = typer.Option("bw", "--panel", help="bw (black/white) or bwr (black/white/red)."),
    rotation: int = typer.Option(0, "--rotation", min=0, max=3, help="Quarter turns (1 = portrait on 400x300)."),
    width: int = typer.Option(400, "--width", help="Native panel width."),
    height: int = typer.Option(300, "--height", help="Native panel height."),
    name: str = typer.Option("Tag", "--name", help="Tag name for the header."),
    lang: str | None = typer.Option(None, "--lang", help="Profile language (default: settings or en)."),
    tz: str | None = typer.Option(None, "--tz", help="Profile time zone (default: settings or UTC)."),
    excerpts: bool | None = typer.Option(None, "--excerpts/--no-excerpts", help="Show bodies."),
    qr: bool | None = typer.Option(None, "--qr/--no-qr", help="Allow QR links."),
    now: str | None = typer.Option(None, "--now", help="Screen time (ISO 8601; default: now)."),
    out: Path = typer.Option(Path("preview-screen.png"), "--out", "-o"),
    scale: int = ScaleOpt,
    pack: Path | None = PackOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Compose a tag screen from a set of active cards and render it."""
    _screen_command(path, panel, rotation, width, height, name, lang, tz, excerpts, qr, now, out, scale, pack, cache,
                    single=False)


def _screen_command(path: Path, panel_kind: str, rotation: int, width: int, height: int, name: str,
                    lang: str | None, tz: str | None, excerpts: bool | None, qr: bool | None, now_s: str | None,
                    out: Path, scale: int, pack: Path | None, cache: Path | None, *, single: bool) -> None:
    from cremind_tag.compose.preview import preview_png
    from cremind_tag.compose.screen import compose_screen

    doc = _read_json(path)
    now = _now(now_s)
    cards = _active_cards(doc, now)
    if single and len(cards) != 1:
        raise _fail(f"{path} holds {len(cards)} cards; use `preview screen` for several")
    tp = _panel(panel_kind, rotation, name, 0x1A2B3C4D, width, height)
    fonts = _fonts(pack, cache)
    composed = compose_screen(tp, cards, fonts, _settings(doc, lang, tz, excerpts, qr), now)
    png = preview_png(composed, tp, fonts, scale=scale)
    out.write_bytes(png)
    _report_screen(composed, out, png)


@app.command()
def identify(
    panel: str = typer.Option("bw", "--panel", help="bw or bwr."),
    rotation: int = typer.Option(0, "--rotation", min=0, max=3),
    name: str = typer.Option("Desk", "--name"),
    tag_id: str = typer.Option("1A2B3C4D", "--tag-id", help="Tag id (hex)."),
    out: Path = typer.Option(Path("preview-identify.png"), "--out", "-o"),
    scale: int = ScaleOpt,
    pack: Path | None = PackOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Render the identify screen (large tag id and name)."""
    from cremind_tag.compose.preview import preview_png
    from cremind_tag.compose.screen import compose_identify

    try:
        tid = int(tag_id, 16)
    except ValueError:
        raise _fail("--tag-id must be hexadecimal") from None
    tp = _panel(panel, rotation, name, tid, 400, 300)
    fonts = _fonts(pack, cache)
    composed = compose_identify(tp, fonts)
    png = preview_png(composed, tp, fonts, scale=scale)
    out.write_bytes(png)
    _report_screen(composed, out, png)


@app.command()
def samples(
    out: Path = typer.Option(..., "--out", help="Directory for the PNG pages and summary.json."),
    scale: int = ScaleOpt,
    width: int = typer.Option(800, "--width", min=200, max=4000, help="Sample page width."),
    pack: Path | None = PackOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Render the multilingual sample set, one automatic sample per face and example screens."""
    from cremind_tag.compose.samples import write_samples

    fonts = _fonts(pack, cache)
    summary = write_samples(fonts, out, scale=scale, width=width, progress=typer.echo)
    bad = [f for f in summary["faces"] if not f["text"] or f["unsupported"] or f["notdef"]
           or f["face_id"] not in f["faces"]]
    typer.echo(f"{len(summary['multilingual'])} multilingual samples, {len(summary['faces'])} faces "
               f"({len(summary['faces']) - len(bad)} drawn by their own face), {len(summary['screens'])} screens")
    for f in bad:
        typer.secho(f"face {f['face_id']} {f['key']}: sample problem {f}", fg=typer.colors.YELLOW)
    if bad:
        raise typer.Exit(1)
