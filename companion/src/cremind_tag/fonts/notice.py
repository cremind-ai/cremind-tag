"""NOTICE and licence texts for a font pack.

A pack is a Modified Version (bitmap conversion) of OFL-1.1 fonts, so it is
distributed under OFL-1.1 with every face's copyright and trademark notices.
It carries a neutral name (the manifest's ``pack_name``) and says it is derived
from Noto. The icon face is rendered from Material Icons (Apache-2.0), whose
licence ships alongside. Licence texts are the pinned upstream files, committed
under ``fonts/LICENSES/`` (``cremind-tag fonts lock`` refreshes them).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from cremind_tag.fonts.manifest import Lock, Manifest, ManifestError, load_lock

NOTICE_FILE = "NOTICE"


def install_licenses(manifest: Manifest, lock: Lock, cache_dir: Path) -> list[Path]:
    """Copy the locked upstream licence texts to their committed location."""
    written = []
    for lic in manifest.licenses:
        src = cache_dir / lock.file(f"license:{lic.id}").cache
        dst = manifest.directory / lic.file
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            written.append(dst)
    return written


def _checked_license(manifest: Manifest, lock: Lock, license_id: str) -> Path:
    lic = next((x for x in manifest.licenses if x.id == license_id), None)
    if lic is None:
        raise ManifestError(f"licence {license_id!r} is not in the manifest")
    path = manifest.directory / lic.file
    expected = lock.file(f"license:{license_id}").sha256
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ManifestError(f"{path} is missing or differs from the pinned upstream text; run `cremind-tag fonts lock`")
    return path


def notice_text(manifest: Manifest, sidecar: dict[str, Any]) -> str:
    faces = sidecar["faces"]
    lines = [
        f"{sidecar['pack_name']} — profile {sidecar['profile']}",
        f"pack id {sidecar['pack_id']}, manifest id {sidecar['manifest_id']}, {sidecar['total_size']} bytes",
        "",
        "This font pack contains 1-bit bitmap renderings of the fonts listed below. It is",
        "derived from Noto fonts; it is not an official Noto release and is not endorsed",
        "by Google or the Noto project. The rendering is a Modified Version under the SIL",
        "Open Font License, Version 1.1: the pack as a whole is distributed under that",
        "licence (LICENSES/OFL-1.1.txt). It may be bundled, embedded or redistributed with",
        "any software, but must not be sold by itself. The OFL grants no trademark rights:",
        "\"Noto\" is a trademark of Google LLC and is not used as the name of this pack.",
    ]
    if any(f["role"] == "icons" for f in faces):
        lines += [
            "",
            "Face 0 (icons) is rendered from Material Icons, Copyright Google LLC, licensed",
            "under the Apache License, Version 2.0 (LICENSES/Apache-2.0.txt); those terms",
            "also apply to that face.",
        ]
    lines += ["", f"Faces ({len(faces)}), rendered with FreeType {sidecar['generator']['freetype']}:", ""]
    for f in faces:
        file = f["file"]
        lines += [
            f"[{f['face_id']}] {f['family']} {f['version']} — {', '.join(f['scripts'])}",
            f"    Copyright:  {f['copyright']}",
            f"    Trademark:  {f['trademark'] or '(none)'}",
            f"    Version:    {f['name_version']}",
            f"    Licence:    {f['license']}",
            f"    Source:     {file['url']}",
            f"    SHA-256:    {file['sha256']}",
            "",
        ]
    return "\n".join(lines)


def write_notice(manifest: Manifest, sidecar: dict[str, Any], out_dir: Path) -> Path:
    """Write ``NOTICE`` and ``LICENSES/<id>.txt`` for the licences the pack's faces use."""
    lock = load_lock(manifest.lock_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for license_id in sorted({f["license"] for f in sidecar["faces"]} | {"OFL-1.1"}):
        src = _checked_license(manifest, lock, license_id)
        dst = out_dir / "LICENSES" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    path = out_dir / NOTICE_FILE
    path.write_bytes(notice_text(manifest, sidecar).encode("utf-8"))
    return path
