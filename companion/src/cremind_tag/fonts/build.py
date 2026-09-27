"""Build font packs from the pinned manifest and load them back as a `FontSet`.

A build renders every glyph of every selected face at every selected size
(`cremind_tag.fonts.rasterize`), serialises the strikes with
`cremind_tag.fontpack.format.build_pack` and writes, into the output directory:

- ``fontpack.ctfp`` — the pack (docs/fontpack.md),
- ``fontpack.json`` — the sidecar: which pinned file each face id was rendered
  from (source URL, SHA-256, cache location) plus the face metadata layout
  needs; `load_fontset` reads it,
- ``NOTICE`` and ``LICENSES/`` (`cremind_tag.fonts.notice`).

The pack is a pure function of the locked font files, the manifest, the
FreeType version and ``GENERATOR_VERSION``: two builds give the same pack id.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cremind_tag import __version__
from cremind_tag.fontpack.format import (
    FACE_FLAG_CJK,
    FACE_FLAG_ICON,
    FACE_FLAG_RTL,
    Face,
    FontPack,
    StrikeSpec,
    build_pack,
)
from cremind_tag.fonts.fetch import sha256_file, verify_cached
from cremind_tag.fonts.fontset import FaceInfo, FontSet, StrikeMetrics
from cremind_tag.fonts.manifest import (
    FaceEntry,
    IconGlyph,
    Lock,
    Manifest,
    ManifestError,
    check_icon_codepoints,
    check_lock_current,
    default_cache_dir,
    load_icon_map,
    load_lock,
    load_manifest,
    parse_codepoints,
)
from cremind_tag.fonts.rasterize import StrikeResult, StrikeTask, freetype_py_version, freetype_version, render_task
from cremind_tag.protocol.ids import FONT_SIZES, ICON_SIZES

GENERATOR_VERSION = 1
"""Bump when a change to this package changes pack bytes for the same inputs."""
PACK_FILE = "fontpack.ctfp"
SIDECAR_FILE = "fontpack.json"
SIDECAR_SCHEMA = "cremind-tag/fontpack-faces@1"

Progress = Callable[[str], None]


class BuildError(ManifestError):
    """A build cannot proceed (wrong FreeType, unverified cache, bad selection)."""


@dataclass(frozen=True)
class BuildPlan:
    name: str
    """Profile name, or ``<profile>-custom`` when --faces/--sizes narrowed it."""
    profile: str
    faces: tuple[FaceEntry, ...]
    text_sizes: tuple[int, ...]
    icon_sizes: tuple[int, ...]

    def sizes_for(self, face: FaceEntry) -> tuple[int, ...]:
        return self.icon_sizes if face.is_icons else self.text_sizes


def plan_build(manifest: Manifest, profile: str = "full", faces: Sequence[str] | None = None,
               sizes: Sequence[int] | None = None) -> BuildPlan:
    """Resolve a profile plus optional face (keys or ids) and size filters.

    ``faces`` replaces the profile's face list (the icon face is always kept);
    ``sizes`` intersects both the text and the icon sizes.
    """
    if profile not in manifest.profiles:
        raise BuildError(f"unknown profile {profile!r}; choose from {sorted(manifest.profiles)}")
    prof = manifest.profiles[profile]
    if faces:
        chosen: list[FaceEntry] = [manifest.icon_face]
        for item in faces:
            face = manifest.face_by_id(int(item)) if str(item).isdigit() else manifest.face(str(item))
            if face not in chosen:
                chosen.append(face)
    elif prof.faces is None:
        chosen = [f for f in manifest.faces if f.role != "optional"]
    else:
        chosen = [manifest.face(k) for k in prof.faces]
    text_sizes, icon_sizes = prof.text_sizes, prof.icon_sizes
    if sizes:
        if bad := sorted(set(sizes) - set(FONT_SIZES) - set(ICON_SIZES)):
            raise BuildError(f"sizes {bad} are neither FONT_SIZES {list(FONT_SIZES)} nor ICON_SIZES {list(ICON_SIZES)}")
        text_sizes = tuple(s for s in text_sizes if s in sizes)
        icon_sizes = tuple(s for s in icon_sizes if s in sizes)
    if not text_sizes and len(chosen) > 1:
        raise BuildError("no text sizes left after --sizes")
    name = profile if not faces and not sizes else f"{profile}-custom"
    return BuildPlan(name, profile, tuple(sorted(chosen, key=lambda f: f.face_id)), text_sizes, icon_sizes)


def face_flags(face: FaceEntry) -> int:
    return (FACE_FLAG_ICON if face.is_icons else 0) | (FACE_FLAG_CJK if face.is_cjk else 0) | \
        (FACE_FLAG_RTL if face.rtl else 0)


def check_freetype(manifest: Manifest) -> None:
    actual = freetype_version()
    if actual != manifest.render.freetype:
        raise BuildError(f"FreeType {actual} (freetype-py {freetype_py_version()}) is loaded but the manifest pins "
                         f"{manifest.render.freetype} (freetype-py {manifest.render.freetype_py}); rasterisation "
                         "differs between releases")


def _faces_doc(manifest: Manifest, lock: Lock, faces: Sequence[FaceEntry],
               icons: tuple[IconGlyph, ...]) -> list[dict[str, Any]]:
    out = []
    for face in faces:
        locked = lock.file(face.key)
        doc: dict[str, Any] = {
            "face_id": face.face_id, "key": face.key, "family": face.family, "version": face.version,
            "pack_name": face.pack_name, "role": face.role, "scripts": list(face.scripts),
            "languages": list(face.languages), "rtl": face.rtl, "flags": face_flags(face),
            "variations": {axis: value for axis, value in face.variations},
            "hinting": face.hinting, "render_mode": manifest.hint_mode(face),
            "glyph_count": len(icons) + 1 if face.is_icons else face.num_glyphs,
            "license": face.license, "copyright": face.copyright, "trademark": face.trademark,
            "name_version": face.name_version,
            "file": {"source": face.file.source, "commit": manifest.sources[face.file.source].commit,
                     "path": face.file.path, "url": locked.url, "size": locked.size, "sha256": locked.sha256,
                     "cache": locked.cache},
        }
        if face.is_icons:
            doc["icons"] = [{"id": i.id, "name": i.name, "material": i.material, "codepoint": i.codepoint}
                            for i in icons]
        out.append(doc)
    return out


@dataclass
class FaceStats:
    clipped: int = 0
    dropped: int = 0
    errors: int = 0
    empty: int = 0


@dataclass
class BuildResult:
    plan: BuildPlan
    pack_path: Path
    sidecar_path: Path
    pack_id: bytes
    manifest_id: bytes
    total_size: int
    bitmap_area_size: int
    undeduplicated_bitmap_size: int
    cjk_bitmap_size: int
    cjk_unique_bitmap_size: int
    glyph_entries: int
    strike_count: int
    seconds: float
    face_stats: dict[str, FaceStats] = field(default_factory=dict)

    @property
    def dedup_saved(self) -> int:
        return self.undeduplicated_bitmap_size - self.bitmap_area_size


def _render_all(tasks: list[StrikeTask], jobs: int, progress: Progress) -> dict[tuple[int, int], StrikeResult]:
    results: dict[tuple[int, int], StrikeResult] = {}
    jobs = min(jobs, len(tasks))
    if jobs <= 1:
        iterator = map(render_task, tasks)
        pool = None
    else:
        pool = ProcessPoolExecutor(max_workers=jobs)
        iterator = pool.map(render_task, tasks)
    try:
        for n, result in enumerate(iterator, start=1):
            results[(result.face_id, result.size)] = result
            if n % 50 == 0 or n == len(tasks):
                progress(f"rendered {n}/{len(tasks)} strikes")
    finally:
        if pool is not None:
            pool.shutdown()
    return results


def build(manifest: Manifest, plan: BuildPlan, *, cache_dir: Path | None = None, out_dir: Path | None = None,
          jobs: int | None = None, strict_freetype: bool = True, progress: Progress = lambda _: None) -> BuildResult:
    """Render ``plan`` and write the pack, its sidecar and its NOTICE into ``out_dir``."""
    from cremind_tag.fonts.notice import write_notice

    started = time.perf_counter()
    cache_dir = cache_dir or default_cache_dir()
    out_dir = out_dir or manifest.directory / "out" / plan.name
    if strict_freetype:
        check_freetype(manifest)
    lock = load_lock(manifest.lock_path)
    check_lock_current(manifest, lock)
    icon_face = manifest.icon_face
    ids = [f.key for f in plan.faces] + [f"{icon_face.key}.codepoints"]
    paths = verify_cached(lock, cache_dir, ids)
    progress(f"verified {len(ids)} cached files against the lock")
    icons = load_icon_map(manifest)
    check_icon_codepoints(icons, parse_codepoints(paths[f"{icon_face.key}.codepoints"].read_text(encoding="utf-8")))

    tasks = []
    for face in plan.faces:
        for size in plan.sizes_for(face):
            tasks.append(StrikeTask(face.face_id, str(paths[face.key]), size, manifest.hint_mode(face),
                                    face.variations, tuple(i.codepoint for i in icons) if face.is_icons else None))
    # Largest faces first so the pool stays busy; results are keyed, so order never reaches the pack.
    tasks.sort(key=lambda t: (-manifest.face_by_id(t.face_id).num_glyphs, t.face_id, t.size))
    results = _render_all(tasks, jobs or os.cpu_count() or 1, progress)

    face_records = [Face(f.face_id, face_flags(f), f.pack_name, ",".join(f.scripts),
                         len(icons) + 1 if f.is_icons else f.num_glyphs) for f in plan.faces]
    strikes: list[StrikeSpec] = []
    stats: dict[str, FaceStats] = {f.key: FaceStats() for f in plan.faces}
    raw_bitmaps = 0
    cjk_raw = 0
    cjk_unique: set[bytes] = set()
    for face in plan.faces:
        for size in plan.sizes_for(face):
            r = results[(face.face_id, size)]
            glyphs = r.glyphs()
            if not face.is_icons and len(glyphs) != face.num_glyphs:
                raise BuildError(f"{face.key}: {len(glyphs)} glyphs rendered, the manifest says {face.num_glyphs}")
            s = stats[face.key]
            s.clipped += len(r.stats.clipped)
            s.dropped += len(r.stats.dropped)
            s.errors += len(r.stats.errors)
            s.empty += r.stats.empty
            raw_bitmaps += len(r.bitmaps)
            if face.role == "cjk-region":
                cjk_raw += len(r.bitmaps)
                cjk_unique.update(g.bitmap for g in glyphs if not g.empty)
            strikes.append(StrikeSpec(face.face_id, size, r.ascent, r.descent, r.line_height, glyphs))
    progress("serialising the pack")
    data = build_pack(face_records, strikes, lock.manifest_id)
    pack = FontPack(data)  # round-trip validation, including the content hash

    out_dir.mkdir(parents=True, exist_ok=True)
    pack_path = out_dir / PACK_FILE
    pack_path.write_bytes(data)
    sidecar = {
        "schema": SIDECAR_SCHEMA, "pack_name": manifest.pack_name, "profile": plan.name,
        "pack_id": pack.pack_id.hex(), "content_hash": pack.content_hash.hex(),
        "manifest_id": lock.manifest_id.hex(), "total_size": pack.total_size,
        "generator": {"cremind_tag": __version__, "generator_version": GENERATOR_VERSION,
                      "freetype": freetype_version(), "freetype_py": freetype_py_version()},
        "text_sizes": list(plan.text_sizes), "icon_sizes": list(plan.icon_sizes),
        "faces": _faces_doc(manifest, lock, plan.faces, icons),
    }
    sidecar_path = out_dir / SIDECAR_FILE
    sidecar_path.write_bytes((json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    write_notice(manifest, sidecar, out_dir)
    cjk_unique_size = sum(len(b) for b in cjk_unique)
    return BuildResult(plan, pack_path, sidecar_path, pack.pack_id, lock.manifest_id, pack.total_size,
                       bitmap_area_size(data), raw_bitmaps, cjk_raw, cjk_unique_size,
                       sum(s.glyph_count for s in pack.strikes), len(pack.strikes), time.perf_counter() - started,
                       stats)


def bitmap_area_size(data: bytes) -> int:
    """Header field "bitmap area size" (docs/fontpack.md §2, offset 80)."""
    return int.from_bytes(data[80:84], "little")


# --------------------------------------------------------------------------- loading


_sha_memo: dict[tuple[str, int, int], str] = {}


def _verified(path: Path, sha256: str) -> None:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} is missing; run `cremind-tag fonts fetch`") from None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _sha_memo:
        _sha_memo[key] = sha256_file(path)
    if _sha_memo[key] != sha256:
        raise ManifestError(f"{path} does not match the file the pack was rendered from (SHA-256 {sha256}); "
                            "run `cremind-tag fonts fetch`")


def read_sidecar(pack_path: Path) -> dict[str, Any] | None:
    path = Path(pack_path).with_suffix(".json")
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != SIDECAR_SCHEMA:
        raise ManifestError(f"{path}: schema must be {SIDECAR_SCHEMA!r}")
    return doc


def _sidecar_from_manifest(pack: FontPack) -> dict[str, Any]:
    manifest = load_manifest()
    lock = load_lock(manifest.lock_path)
    if pack.manifest_id != lock.manifest_id:
        raise ManifestError(f"the pack was built from manifest {pack.manifest_id.hex()}, the checkout's lock is "
                            f"{lock.manifest_id.hex()}; rebuild the pack or use its fontpack.json")
    faces = [manifest.face_by_id(f.face_id) for f in pack.faces]
    return {"pack_id": pack.pack_id.hex(), "faces": _faces_doc(manifest, lock, faces, load_icon_map(manifest))}


def load_fontset(pack_path: Path, cache_dir: Path | None = None, *, verify_files: bool = True) -> FontSet:
    """Read a built pack and resolve each face to its cached font file (the `FontSet` contract).

    Face metadata comes from the pack's sidecar (``fontpack.json`` next to it),
    else from the checkout's manifest when its lock matches the pack's manifest
    id. Text faces resolve to ``<cache>/<source>/<file>``; with ``verify_files``
    each file must have the SHA-256 it was rasterised from.
    """
    pack_path = Path(pack_path)
    pack = FontPack(pack_path.read_bytes())
    doc = read_sidecar(pack_path) or _sidecar_from_manifest(pack)
    if doc["pack_id"] != pack.pack_id.hex():
        raise ManifestError(f"{pack_path.with_suffix('.json')} describes pack {doc['pack_id']}, "
                            f"not {pack.pack_id.hex()}")
    by_id = {f["face_id"]: f for f in doc["faces"]}
    cache = Path(cache_dir) if cache_dir else None
    faces: list[FaceInfo] = []
    for record in pack.faces:
        meta = by_id.get(record.face_id)
        if meta is None or meta["pack_name"] != record.name:
            raise ManifestError(f"face {record.face_id} ({record.name}) is not described by the sidecar")
        path = None
        if meta["role"] != "icons":
            cache = cache or default_cache_dir()
            path = cache / meta["file"]["cache"]
            if verify_files:
                _verified(path, meta["file"]["sha256"])
        faces.append(FaceInfo(face_id=record.face_id, key=meta["key"], family=meta["family"], path=path,
                              scripts=tuple(meta["scripts"]), role=meta["role"], languages=tuple(meta["languages"]),
                              rtl=bool(meta["rtl"]),
                              variations=tuple((a, float(v)) for a, v in sorted(meta["variations"].items()))))
    strikes = {(s.face_id, s.size_px): StrikeMetrics(s.face_id, s.size_px, s.ascent, s.descent, s.line_height,
                                                     s.glyph_count) for s in pack.strikes}
    return FontSet(pack_path, pack.pack_id, tuple(faces), strikes)


def pack_digest(path: Path) -> str:
    """SHA-256 of a whole pack file (what FONT_BEGIN announces)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
