"""`cremind-tag fonts` — pin, fetch, build and size font packs (all Noto scripts).

docs/fonts.md describes the pipeline; docs/fontpack.md the pack format and the
flash rule. Every command exits non-zero when a verification fails.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(name="fonts", help="Pin, fetch, build and size font packs (all Noto scripts).", no_args_is_help=True)

ManifestOpt = typer.Option(None, "--manifest", help="Manifest to use (default: <repo>/fonts/manifest.yaml).")
CacheOpt = typer.Option(None, "--cache",
                        help="Font cache directory (default: $CREMIND_TAG_FONT_CACHE or <repo>/fonts/cache).")


@app.callback()
def _fonts() -> None:
    """Pin, fetch, build and size font packs."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _mib(n: int) -> str:
    return f"{n:,} B ({n / 1048576:.2f} MiB)"


def _load(manifest_path: Path | None) -> Any:
    from cremind_tag.fonts.manifest import ManifestError, load_manifest

    try:
        return load_manifest(manifest_path)
    except ManifestError as exc:
        raise _fail(str(exc)) from None


def _cache(cache: Path | None) -> Path:
    from cremind_tag.fonts.manifest import default_cache_dir

    return cache or default_cache_dir()


def _default_pack(manifest: Any, profile: str) -> Path:
    return manifest.directory / "out" / profile / "fontpack.ctfp"


@app.command()
def lock(
    refresh: bool = typer.Option(False, "--refresh", help="Re-download files even when the cache already matches."),
    jobs: int = typer.Option(8, "--jobs", help="Parallel downloads."),
    manifest: Path | None = ManifestOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Download every pinned file once, verify size + git blob SHA-1, write fonts/manifest.lock.json."""
    from cremind_tag.fonts import fetch
    from cremind_tag.fonts.manifest import ManifestError
    from cremind_tag.fonts.notice import install_licenses

    m = _load(manifest)
    cache_dir = _cache(cache)
    started = time.perf_counter()
    try:
        lk = fetch.lock(m, cache_dir, workers=jobs, refresh=refresh,
                        progress=lambda msg: typer.echo(msg) if msg.startswith("downloaded") else None)
        updated = install_licenses(m, lk, cache_dir)
    except ManifestError as exc:
        raise _fail(str(exc)) from None
    total = sum(f.size for f in lk.files.values())
    typer.echo(f"locked {len(lk.files)} files ({_mib(total)}) in {time.perf_counter() - started:.1f} s")
    for path in updated:
        typer.echo(f"updated {path}")
    typer.echo(f"lock:        {m.lock_path}")
    typer.echo(f"manifest id: {lk.manifest_id.hex()}")


@app.command("fetch")
def fetch_(
    jobs: int = typer.Option(8, "--jobs", help="Parallel downloads."),
    manifest: Path | None = ManifestOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Fill (or repair) the font cache from the lock; every file must match its locked SHA-256."""
    from cremind_tag.fonts import fetch
    from cremind_tag.fonts.manifest import ManifestError

    m = _load(manifest)
    started = time.perf_counter()
    try:
        paths = fetch.fetch(m, _cache(cache), workers=jobs,
                            progress=lambda msg: typer.echo(msg) if msg.startswith("downloaded") else None)
    except ManifestError as exc:
        raise _fail(str(exc)) from None
    typer.echo(f"{len(paths)} files verified in {_cache(cache)} ({time.perf_counter() - started:.1f} s)")


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


@app.command()
def build(
    profile: str = typer.Option("full", "--profile", help="Build profile from the manifest (full, dev)."),
    faces: str | None = typer.Option(None, "--faces", help="Comma-separated face keys or ids (replaces the "
                                     "profile's faces; the icon face is always kept)."),
    sizes: str | None = typer.Option(None, "--sizes", help="Comma-separated pixel sizes to keep (e.g. 16,24)."),
    out: Path | None = typer.Option(None, "--out", help="Output directory (default: fonts/out/<profile>)."),
    jobs: int | None = typer.Option(None, "--jobs", help="Rasteriser processes (default: CPU count)."),
    allow_freetype_mismatch: bool = typer.Option(False, "--allow-freetype-mismatch",
                                                 help="Build with an unpinned FreeType (output is not reproducible)."),
    manifest: Path | None = ManifestOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Rasterise the selected faces and write fontpack.ctfp, fontpack.json, NOTICE and LICENSES/."""
    from cremind_tag.fonts.build import build as run_build
    from cremind_tag.fonts.build import plan_build
    from cremind_tag.fonts.manifest import ManifestError
    from cremind_tag.fonts.sizing import density_label, size_pack

    m = _load(manifest)
    try:
        plan = plan_build(m, profile, _csv(faces), [int(s) for s in _csv(sizes) or []] or None)
    except (ManifestError, ValueError) as exc:
        raise _fail(str(exc)) from None
    glyphs = sum(f.num_glyphs for f in plan.faces if not f.is_icons)
    typer.echo(f"profile {plan.name}: {len(plan.faces)} faces ({glyphs:,} text glyphs), text sizes "
               f"{list(plan.text_sizes)}, icon sizes {list(plan.icon_sizes)}")
    try:
        r = run_build(m, plan, cache_dir=_cache(cache), out_dir=out, jobs=jobs,
                      strict_freetype=not allow_freetype_mismatch, progress=typer.echo)
    except ManifestError as exc:
        raise _fail(str(exc)) from None
    saved = r.dedup_saved
    typer.echo(f"pack:          {r.pack_path}")
    typer.echo(f"pack id:       {r.pack_id.hex()}   manifest id: {r.manifest_id.hex()}")
    typer.echo(f"size P:        {_mib(r.total_size)}")
    typer.echo(f"strikes:       {r.strike_count} ({r.glyph_entries:,} glyph entries)")
    typer.echo(f"bitmaps:       {_mib(r.bitmap_area_size)} stored, {_mib(r.undeduplicated_bitmap_size)} before "
               f"de-duplication (saved {saved:,} B, {100 * saved / max(1, r.undeduplicated_bitmap_size):.1f} %)")
    if r.cjk_bitmap_size:
        typer.echo(f"CJK regions:   {_mib(r.cjk_bitmap_size)} rendered, {_mib(r.cjk_unique_bitmap_size)} distinct")
    for key, s in r.face_stats.items():
        if s.clipped or s.dropped or s.errors:
            typer.echo(f"  {key}: {s.clipped} glyphs cropped to the format limits, {s.dropped} dropped, "
                       f"{s.errors} failed to load")
    sizing = size_pack(r.total_size)
    part = density_label(sizing.flash_size) if sizing.flash_size else "none (above 2 Gbit)"
    typer.echo(f"flash rule:    2 x {sizing.erase_aligned:,} + 16 MiB = {_mib(sizing.required)} -> {part}")
    typer.echo(f"built in {r.seconds:.1f} s")


def _sidecar(pack: Path) -> dict[str, Any]:
    from cremind_tag.fonts.build import read_sidecar

    doc = read_sidecar(pack)
    if doc is None:
        raise _fail(f"{pack.with_suffix('.json')} is missing (rebuild the pack)")
    return doc


@app.command()
def size(
    packs: list[Path] = typer.Argument(None, help="Packs to size (default: fonts/out/{full,dev}/fontpack.ctfp)."),
    flash_size: str | None = typer.Option(None, "--flash-size", help="Check this part instead (e.g. 128MiB)."),
    working_space: str | None = typer.Option(None, "--working-space",
                                             help="Working space (default 16 MiB; smaller = development only)."),
    manifest: Path | None = ManifestOpt,
) -> None:
    """Print P, the capacity rule and the smallest standard NOR part (docs/fontpack.md, flash layout)."""
    from cremind_tag.fonts.sizing import density_label, parse_size, size_pack
    from cremind_tag.protocol.ids import FONTPACK_WORKING_SPACE

    m = _load(manifest)
    if not packs:
        packs = [p for p in (_default_pack(m, "full"), _default_pack(m, "dev")) if p.is_file()]
        if not packs:
            raise _fail("no packs given and none built; run `cremind-tag fonts build`")
    ok = True
    for pack in packs:
        if not pack.is_file():
            raise _fail(f"{pack} does not exist")
        doc = _sidecar(pack)
        total = doc["total_size"]
        typer.echo(f"{pack}  (profile {doc['profile']}, pack id {doc['pack_id']})")
        ws = parse_size(working_space) if working_space else FONTPACK_WORKING_SPACE
        s = size_pack(total, flash_size=parse_size(flash_size) if flash_size else None, working_space=ws)
        typer.echo(f"  P                    {_mib(total)}")
        typer.echo(f"  erase-aligned P      {_mib(s.erase_aligned)}")
        typer.echo(f"  required             2 x erase-aligned P + {_mib(ws)} = {_mib(s.required)}")
        if s.flash_size is None:
            typer.echo("  part                 none: exceeds the largest standard NOR density (2 Gbit)")
            ok = False
            continue
        label = "part" if flash_size else "smallest part"
        typer.echo(f"  {label:<20} {density_label(s.flash_size)}{'' if s.fits else '  -- DOES NOT SATISFY THE RULE'}")
        typer.echo(f"  slot size            {_mib(s.slot_size)} (pack headroom {_mib(s.headroom)})")
        if s.four_byte_addressing:
            typer.echo("  addressing           4-byte (part above 16 MiB)")
        if s.development_only:
            typer.echo("  NOTE: working space below 16 MiB -- development only")
        ok &= s.fits
        prof = m.profiles.get(doc["profile"].removesuffix("-custom"))
        if not flash_size and prof and prof.flash_size and prof.working_space:
            d = size_pack(total, flash_size=prof.flash_size, working_space=prof.working_space)
            typer.echo(f"  DEVELOPMENT ONLY: {density_label(prof.flash_size)} with a {_mib(prof.working_space)} "
                       f"working space -> slots of {_mib(d.slot_size)}; "
                       + (f"fits (headroom {_mib(d.headroom)})" if d.headroom >= 0 else "DOES NOT FIT"))
    if not ok:
        raise typer.Exit(1)


def _char_line(ch: str) -> str:
    name = unicodedata.name(ch, "<unnamed or newer than Python's Unicode data>")
    return f"U+{ord(ch):04X}  {ch}  {name}"


@app.command()
def coverage(
    pack: Path | None = typer.Option(None, "--pack",
                                     help="Built pack (default: the profile's faces from the manifest)."),
    profile: str = typer.Option("full", "--profile", help="Profile whose faces to check when no --pack is given."),
    text: str | None = typer.Option(None, "--text", help="Check this text; list the characters no face covers."),
    file: Path | None = typer.Option(None, "--file", help="Check the text of this UTF-8 file."),
    json_out: Path | None = typer.Option(None, "--json",
                                         help="Report path (default: fonts/out/<profile>/coverage.json)."),
    manifest: Path | None = ManifestOpt,
    cache: Path | None = CacheOpt,
) -> None:
    """Per-script coverage of a pack at the pinned Unicode version, or check a text."""
    from cremind_tag.fonts.build import load_fontset, plan_build
    from cremind_tag.fonts.coverage import CmapIndex, coverage_report, load_unicode
    from cremind_tag.fonts.fetch import verify_cached
    from cremind_tag.fonts.manifest import ManifestError, load_lock

    m = _load(manifest)
    cache_dir = _cache(cache)
    try:
        if pack is not None:
            fs = load_fontset(pack, cache_dir)
            faces = [(f.face_id, f.key, f.scripts, f.path) for f in fs.faces if f.path is not None]
            name = _sidecar(pack)["profile"]
        else:
            plan = plan_build(m, profile)
            text_faces = [f for f in plan.faces if not f.is_icons]
            paths = verify_cached(load_lock(m.lock_path), cache_dir, [f.key for f in text_faces])
            faces = [(f.face_id, f.key, f.scripts, paths[f.key]) for f in text_faces]
            name = plan.name
    except (ManifestError, FileNotFoundError) as exc:
        raise _fail(str(exc)) from None

    if text is not None or file is not None:
        sample = (text or "") + (file.read_text(encoding="utf-8") if file else "")
        missing = CmapIndex((fid, path) for fid, _k, _s, path in faces).check_text(sample)
        if not missing:
            typer.echo(f"all {len(set(sample))} distinct characters are covered by {name}")
            return
        typer.echo(f"{len(missing)} characters are not covered by {name}:")
        for ch in missing:
            typer.echo("  " + _char_line(ch))
        raise typer.Exit(1)

    unicode = load_unicode(cache_dir / "unicode" / m.unicode.version, m.unicode.version)
    report = coverage_report(unicode, faces)
    report["profile"] = name
    out = json_out or (pack.parent if pack else m.directory / "out" / name) / "coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((json.dumps(report, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    s = report["summary"]
    typer.echo(f"{name}: Unicode {unicode.version}, {s['scripts']} scripts (Common/Inherited excluded): "
               f"{s['full']} fully covered, {s['partial']} partially, {s['none']} not at all; "
               f"{s['code_points_covered']:,} code points mapped")
    for code in s["partial_scripts"]:
        e = report["scripts"][code]
        typer.echo(f"  partial  {code} {e['name']:<28} {e['covered']:>7,}/{e['total']:<7,}")
    for code in s["missing_scripts"]:
        e = report["scripts"][code]
        typer.echo(f"  missing  {code} {e['name']:<28} {0:>7}/{e['total']:<7,}")
    for code in ("Zyyy", "Zinh"):
        if e := report["scripts"].get(code):
            typer.echo(f"  {e['name']:<9} {code} {e['covered']:>7,}/{e['total']:<7,}")
    typer.echo(f"report: {out}")


@app.command()
def notice(
    pack: Path | None = typer.Option(None, "--pack", help="Built pack (default: fonts/out/full/fontpack.ctfp)."),
    out: Path | None = typer.Option(None, "--out", help="Output directory (default: the pack's directory)."),
    manifest: Path | None = ManifestOpt,
) -> None:
    """Write NOTICE and LICENSES/ for a built pack."""
    from cremind_tag.fonts.manifest import ManifestError
    from cremind_tag.fonts.notice import write_notice

    m = _load(manifest)
    pack = pack or _default_pack(m, "full")
    try:
        path = write_notice(m, _sidecar(pack), out or pack.parent)
    except ManifestError as exc:
        raise _fail(str(exc)) from None
    typer.echo(f"wrote {path} and {path.parent / 'LICENSES'}")


@app.command()
def image(
    pack: Path | None = typer.Option(None, "--pack", help="Built pack (default: fonts/out/dev/fontpack.ctfp)."),
    flash_size: str | None = typer.Option(None, "--flash-size", help="Part size (default: the profile's, else "
                                          "the smallest part the rule allows)."),
    working_space: str | None = typer.Option(None, "--working-space",
                                             help="Working space (default: the profile's, else 16 MiB)."),
    out: Path | None = typer.Option(None, "--out", help="Output directory (default: <pack dir>/image)."),
    manifest: Path | None = ManifestOpt,
) -> None:
    """Write a raw external-flash image: the pack in slot 0 + an active slot-directory record."""
    from cremind_tag.fonts.image import flash_image, write_image
    from cremind_tag.fonts.sizing import density_label, parse_size, size_pack
    from cremind_tag.protocol.ids import FONTPACK_WORKING_SPACE

    m = _load(manifest)
    pack = pack or _default_pack(m, "dev")
    if not pack.is_file():
        raise _fail(f"{pack} does not exist; run `cremind-tag fonts build`")
    doc = _sidecar(pack)
    prof = m.profiles.get(doc["profile"].removesuffix("-custom"))
    ws = parse_size(working_space) if working_space else (prof.working_space if prof and prof.working_space
                                                            else FONTPACK_WORKING_SPACE)
    fs = parse_size(flash_size) if flash_size else (prof.flash_size if prof and prof.flash_size
                                                    else size_pack(doc["total_size"], working_space=ws).flash_size)
    if fs is None:
        raise _fail("the pack needs more than the largest standard part; pass --flash-size")
    try:
        img = flash_image(pack.read_bytes(), fs, ws)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    paths = write_image(img, out or pack.parent / "image")
    typer.echo(f"{density_label(fs)} part, working space {_mib(ws)}"
               + ("  -- DEVELOPMENT ONLY" if ws < FONTPACK_WORKING_SPACE else ""))
    typer.echo(f"slot size {_mib(img.slot_size)}; pack {doc['pack_id']} in slot 0; directory at {img.dir_offset:#x}")
    for kind, path in paths.items():
        typer.echo(f"{kind:>4}: {path}")
    typer.echo(f"program: nrfjprog -f NRF52 --program {paths['hex']} --qspisectorerase --verify")


@app.command("list")
def list_(
    profile: str | None = typer.Option(None, "--profile", help="Only faces of this profile."),
    manifest: Path | None = ManifestOpt,
) -> None:
    """List faces with ids, roles, scripts, language hints and glyph counts."""
    from cremind_tag.fonts.build import plan_build

    m = _load(manifest)
    faces = plan_build(m, profile).faces if profile else m.faces
    typer.echo(f"{'id':>3}  {'key':<36} {'role':<10} {'glyphs':>6}  {'hint':<8} scripts [languages]")
    for f in faces:
        langs = f"  [{', '.join(f.languages)}]" if f.languages else ""
        rtl = "  rtl" if f.rtl else ""
        typer.echo(f"{f.face_id:>3}  {f.key:<36} {f.role:<10} {f.num_glyphs:>6}  {m.hint_mode(f):<8} "
                   f"{','.join(f.scripts)}{langs}{rtl}")
    typer.echo(f"{len(faces)} faces, {sum(f.num_glyphs for f in faces if not f.is_icons):,} text glyphs")
