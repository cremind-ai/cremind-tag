"""Download pinned font files into the cache and verify them.

Two integrity anchors: while locking, a file must match the manifest's size and
git blob SHA-1 (what the pinned commit's tree says); afterwards it must match
the SHA-256 the lock recorded. ``lock`` creates the lock, ``fetch`` fills or
repairs the cache from it, ``verify_cached`` is the offline check builds use.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

from cremind_tag.fonts.manifest import (
    Lock,
    LockedFile,
    Manifest,
    ManifestError,
    PinnedFile,
    check_icon_codepoints,
    check_lock_current,
    load_icon_map,
    load_lock,
    lock_bytes,
    parse_codepoints,
)

Progress = Callable[[str], None]
USER_AGENT = "cremind-tag-fonts/1"


class VerificationError(ManifestError):
    """A downloaded or cached file does not match its pin."""


def git_blob_sha1(data: bytes) -> str:
    """``git hash-object`` of ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, *, client: httpx.Client | None = None, retries: int = 4, timeout: float = 120.0) -> bytes:
    """GET ``url`` with exponential back-off on network errors, 429 and 5xx."""
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                response = client.get(url)
            except httpx.TransportError as exc:
                problem = str(exc) or type(exc).__name__
            else:
                if response.status_code == 200:
                    return response.content
                problem = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    break
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        raise VerificationError(f"download failed: {url}: {problem}")
    finally:
        if own:
            client.close()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _check_pin(data: bytes, pinned: PinnedFile, what: str) -> None:
    if len(data) != pinned.size:
        raise VerificationError(f"{what}: {len(data)} bytes, the manifest pins {pinned.size}")
    blob = git_blob_sha1(data)
    if blob != pinned.git_blob_sha1:
        raise VerificationError(f"{what}: git blob {blob}, the manifest pins {pinned.git_blob_sha1}")


@dataclass(frozen=True)
class _Job:
    file_id: str
    url: str
    cache: str
    pinned: PinnedFile | None


def _jobs(manifest: Manifest) -> list[_Job]:
    jobs = [_Job(fid, manifest.url(p), p.cache_name, p) for fid, p in manifest.pinned_files().items()]
    for name in manifest.unicode.files:
        jobs.append(_Job(f"unicode:{name}", manifest.unicode.url(name), f"unicode/{manifest.unicode.version}/{name}",
                         None))
    return jobs


def _parallel(fn: Callable[[_Job], LockedFile], jobs: Iterable[_Job], workers: int) -> dict[str, LockedFile]:
    jobs = list(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fn, jobs))
    return {r.id: r for r in results}


def lock(manifest: Manifest, cache_dir: Path, *, progress: Progress = lambda _: None, workers: int = 8,
         refresh: bool = False) -> Lock:
    """Download every pinned file once, verify size + git blob SHA-1, write the lock.

    A cached file that already matches its pin is reused unless ``refresh``.
    Unicode data files carry no git pin; the lock records what was downloaded.
    """
    client = httpx.Client(follow_redirects=True, timeout=120.0, headers={"User-Agent": USER_AGENT})

    def one(job: _Job) -> LockedFile:
        path = cache_dir / job.cache
        data = path.read_bytes() if path.is_file() and not refresh else None
        if data is not None and job.pinned is not None:
            try:
                _check_pin(data, job.pinned, job.file_id)
            except VerificationError:
                data = None
        if data is None:
            data = download(job.url, client=client)
            if job.pinned is not None:
                _check_pin(data, job.pinned, job.file_id)
            _write_atomic(path, data)
            progress(f"downloaded {job.file_id} ({len(data):,} B)")
        else:
            progress(f"cached     {job.file_id}")
        p = job.pinned
        return LockedFile(job.file_id, job.url, len(data), hashlib.sha256(data).hexdigest(), job.cache,
                          p.source if p else None, p.path if p else None, p.git_blob_sha1 if p else None)

    try:
        files = _parallel(one, _jobs(manifest), workers)
    finally:
        client.close()
    if problems := check_font_facts(manifest, {f.key: cache_dir / files[f.key].cache for f in manifest.faces}):
        raise VerificationError("the manifest does not describe the pinned files:\n  " + "\n  ".join(problems))
    codepoints = cache_dir / files[f"{manifest.icon_face.key}.codepoints"].cache
    check_icon_codepoints(load_icon_map(manifest), parse_codepoints(codepoints.read_text(encoding="utf-8")))
    raw = lock_bytes(files, manifest.unicode.version)
    manifest.lock_path.write_bytes(raw)
    return Lock(files, raw)


def fetch(manifest: Manifest, cache_dir: Path, *, ids: Iterable[str] | None = None,
          progress: Progress = lambda _: None, workers: int = 8) -> dict[str, Path]:
    """Make the cache hold every locked file (or ``ids``), re-downloading missing
    or corrupt ones; every file must end up matching its locked SHA-256."""
    lk = load_lock(manifest.lock_path)
    check_lock_current(manifest, lk)
    wanted = set(ids) if ids is not None else set(lk.files)
    client = httpx.Client(follow_redirects=True, timeout=120.0, headers={"User-Agent": USER_AGENT})

    def one(locked: LockedFile) -> LockedFile:
        path = cache_dir / locked.cache
        if path.is_file() and sha256_file(path) == locked.sha256:
            progress(f"ok         {locked.id}")
            return locked
        data = download(locked.url, client=client)
        digest = hashlib.sha256(data).hexdigest()
        if digest != locked.sha256:
            raise VerificationError(f"{locked.id}: downloaded SHA-256 {digest}, the lock pins {locked.sha256}")
        _write_atomic(path, data)
        progress(f"downloaded {locked.id} ({len(data):,} B)")
        return locked

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(one, [lk.files[i] for i in sorted(wanted)]))
    finally:
        client.close()
    return {f.id: cache_dir / f.cache for f in done}


def check_font_facts(manifest: Manifest, paths: dict[str, Path]) -> list[str]:
    """Compare each face's recorded glyph count, hinting class and name strings with its file."""
    from cremind_tag.fonts.rasterize import font_facts

    problems = []
    for face in manifest.faces:
        facts = font_facts(paths[face.key])
        expected = {"num_glyphs": face.num_glyphs, "hinting": face.hinting, "name_version": face.name_version,
                    "copyright": face.copyright, "trademark": face.trademark}
        actual = {"num_glyphs": facts.num_glyphs, "hinting": facts.hinting, "name_version": facts.names.get(5),
                  "copyright": facts.names.get(0), "trademark": facts.names.get(7)}
        problems += [f"{face.key}: {k} is {actual[k]!r} in the file, {expected[k]!r} in the manifest"
                     for k in expected if expected[k] != actual[k]]
    return problems


def verify_cached(lock: Lock, cache_dir: Path, ids: Iterable[str]) -> dict[str, Path]:
    """Offline check that each file is cached with its locked SHA-256 (builds fail otherwise)."""
    out: dict[str, Path] = {}
    for fid in ids:
        locked = lock.file(fid)
        path = cache_dir / locked.cache
        if not path.is_file():
            raise VerificationError(f"{fid}: {path} is missing; run `cremind-tag fonts fetch`")
        digest = sha256_file(path)
        if digest != locked.sha256:
            raise VerificationError(f"{fid}: {path} has SHA-256 {digest}, the lock pins {locked.sha256}; "
                                    "run `cremind-tag fonts fetch` to replace it")
        out[fid] = path
    return out
