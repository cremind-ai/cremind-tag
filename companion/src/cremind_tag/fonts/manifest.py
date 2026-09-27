"""The pinned font manifest (``fonts/manifest.yaml``), its icon map and its lock.

``load_manifest`` parses and validates the manifest; ``load_icon_map`` the
spec-icon -> Material Icons map (``fonts/icons.yaml``); ``Lock`` the
``fonts/manifest.lock.json`` written by ``cremind-tag fonts lock``. The lock
pins every file by SHA-256; the manifest id stamped into packs is
``SHA-256(lock file bytes)[0:8]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, get_args
from urllib.parse import quote

import yaml

from cremind_tag.fonts.fontset import FaceRole
from cremind_tag.protocol.ids import FONT_SIZES, ICON_SIZES, Icon

MANIFEST_SCHEMA = "cremind-tag/fonts-manifest@1"
LOCK_SCHEMA = "cremind-tag/fonts-lock@1"
HINTING_CLASSES = ("truetype-bytecode", "none", "cff")
HINT_MODES = ("native", "autohint", "none")
ICON_FACE_ID = 0

_SHA1 = re.compile(r"[0-9a-f]{40}")
_SCRIPT = re.compile(r"[A-Z][a-z]{3}")
_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LANG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_AXIS = re.compile(r"[A-Za-z0-9 ]{4}")


class ManifestError(ValueError):
    """The manifest, icon map or lock is invalid or inconsistent."""


def repo_root() -> Path:
    """The cremind-tag checkout: ``$CREMIND_TAG_REPO``, else the nearest parent
    of the working directory or of this package that holds ``fonts/manifest.yaml``."""
    env = os.environ.get("CREMIND_TAG_REPO")
    if env:
        return Path(env)
    for start in (Path.cwd(), Path(__file__).resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "fonts" / "manifest.yaml").is_file():
                return candidate
    raise ManifestError("cannot find fonts/manifest.yaml; run inside the cremind-tag checkout or set CREMIND_TAG_REPO")


def default_manifest_path() -> Path:
    return repo_root() / "fonts" / "manifest.yaml"


def default_cache_dir() -> Path:
    """``$CREMIND_TAG_FONT_CACHE``, else ``<repo>/fonts/cache``."""
    env = os.environ.get("CREMIND_TAG_FONT_CACHE")
    return Path(env) if env else repo_root() / "fonts" / "cache"


@dataclass(frozen=True)
class Source:
    id: str
    repo: str
    ref: str
    commit: str
    url_template: str

    def url(self, path: str) -> str:
        return self.url_template.format(commit=self.commit, path=quote(path, safe="/"))


@dataclass(frozen=True)
class PinnedFile:
    """A file at a pinned source commit, identified by size and git blob SHA-1."""

    source: str
    path: str
    size: int
    git_blob_sha1: str

    @property
    def basename(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def cache_name(self) -> str:
        """Location under the cache directory (``<source>/<basename>``)."""
        return f"{self.source}/{self.basename}"


@dataclass(frozen=True)
class License:
    id: str
    file: str
    """Committed copy, relative to the manifest directory (e.g. ``LICENSES/OFL-1.1.txt``)."""
    pinned: PinnedFile


@dataclass(frozen=True)
class UnicodeData:
    version: str
    url_template: str
    files: tuple[str, ...]

    def url(self, name: str) -> str:
        return self.url_template.format(version=self.version, path=name)


@dataclass(frozen=True)
class RenderPolicy:
    freetype: str
    freetype_py: str
    hinting: dict[str, str]
    """Hinting class -> mode (``native``, ``autohint`` or ``none``)."""
    icons: str


@dataclass(frozen=True)
class Profile:
    name: str
    faces: tuple[str, ...] | None
    """Face keys, or None for every face whose role is not ``optional``."""
    text_sizes: tuple[int, ...]
    icon_sizes: tuple[int, ...]
    flash_size: int | None = None
    working_space: int | None = None


@dataclass(frozen=True)
class FaceEntry:
    face_id: int
    key: str
    family: str
    role: FaceRole
    scripts: tuple[str, ...]
    languages: tuple[str, ...]
    rtl: bool
    file: PinnedFile
    version: str
    name_version: str
    num_glyphs: int
    hinting: str
    variations: tuple[tuple[str, float], ...]
    copyright: str
    trademark: str | None
    license: str
    release_tag: str | None = None
    release_published: str | None = None
    note: str | None = None
    icon_map: str | None = None
    icon_codepoints: PinnedFile | None = None

    @property
    def is_icons(self) -> bool:
        return self.role == "icons"

    @property
    def is_cjk(self) -> bool:
        return self.role == "cjk-region" or set(self.scripts) <= {"Hani", "Hans", "Hant", "Hira", "Kana", "Hrkt",
                                                                   "Jpan", "Kore", "Hang", "Bopo"}

    @property
    def pack_name(self) -> str:
        """The face-table name, e.g. ``Noto Sans Arabic 2.013``."""
        return f"{self.family} {self.version}"


@dataclass(frozen=True)
class IconGlyph:
    id: int
    name: str
    material: str
    codepoint: int
    note: str | None = None


@dataclass(frozen=True)
class Manifest:
    path: Path
    pack_name: str
    sources: dict[str, Source]
    licenses: tuple[License, ...]
    unicode: UnicodeData
    render: RenderPolicy
    profiles: dict[str, Profile]
    faces: tuple[FaceEntry, ...]

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def lock_path(self) -> Path:
        return self.directory / "manifest.lock.json"

    def face(self, key: str) -> FaceEntry:
        for face in self.faces:
            if face.key == key:
                return face
        raise ManifestError(f"no face {key!r} in the manifest")

    def face_by_id(self, face_id: int) -> FaceEntry:
        for face in self.faces:
            if face.face_id == face_id:
                return face
        raise ManifestError(f"no face id {face_id} in the manifest")

    @property
    def icon_face(self) -> FaceEntry:
        return self.face_by_id(ICON_FACE_ID)

    def url(self, pinned: PinnedFile) -> str:
        return self.sources[pinned.source].url(pinned.path)

    def pinned_files(self) -> dict[str, PinnedFile]:
        """Every git-pinned file by lock id: faces by key, ``<key>.codepoints``, ``license:<id>``."""
        files: dict[str, PinnedFile] = {}
        for face in self.faces:
            files[face.key] = face.file
            if face.icon_codepoints is not None:
                files[f"{face.key}.codepoints"] = face.icon_codepoints
        for lic in self.licenses:
            files[f"license:{lic.id}"] = lic.pinned
        return files

    def hint_mode(self, face: FaceEntry) -> str:
        return self.render.icons if face.is_icons else self.render.hinting[face.hinting]


def _req(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ManifestError(f"{where}: missing {key!r}")
    return d[key]


def _pinned(d: dict[str, Any], where: str, sources: dict[str, Source]) -> PinnedFile:
    source = str(_req(d, "source", where))
    if source not in sources:
        raise ManifestError(f"{where}: unknown source {source!r}")
    path = str(_req(d, "path", where))
    size = _req(d, "size", where)
    blob = str(_req(d, "git_blob_sha1", where))
    if not isinstance(size, int) or size <= 0:
        raise ManifestError(f"{where}: size must be a positive integer")
    if not _SHA1.fullmatch(blob):
        raise ManifestError(f"{where}: git_blob_sha1 must be 40 lowercase hex digits")
    if path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise ManifestError(f"{where}: path must be relative to the source root")
    return PinnedFile(source, path, size, blob)


def _sizes(value: Any, allowed: tuple[int, ...], where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(v, int) for v in value):
        raise ManifestError(f"{where}: sizes must be a non-empty list of integers")
    if bad := sorted(set(value) - set(allowed)):
        raise ManifestError(f"{where}: sizes {bad} are not in {list(allowed)} (protocol/spec.yaml)")
    return tuple(sorted(set(value)))


def parse_manifest(data: dict[str, Any], path: Path) -> Manifest:
    """Validate a parsed manifest document."""
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"{path}: schema must be {MANIFEST_SCHEMA!r}")

    sources: dict[str, Source] = {}
    for sid, s in (_req(data, "sources", "manifest") or {}).items():
        where = f"sources.{sid}"
        commit = str(_req(s, "commit", where))
        template = str(_req(s, "url_template", where))
        if not _SHA1.fullmatch(commit):
            raise ManifestError(f"{where}: commit must be a full 40-hex SHA-1")
        if "{commit}" not in template or "{path}" not in template:
            raise ManifestError(f"{where}: url_template needs {{commit}} and {{path}}")
        sources[sid] = Source(sid, str(_req(s, "repo", where)), str(s.get("ref", "")), commit, template)

    licenses = []
    for i, lic in enumerate(_req(data, "licenses", "manifest")):
        where = f"licenses[{i}]"
        licenses.append(License(str(_req(lic, "id", where)), str(_req(lic, "file", where)),
                                _pinned(lic, where, sources)))
    license_ids = {lic.id for lic in licenses}

    u = _req(data, "unicode", "manifest")
    unicode = UnicodeData(str(_req(u, "version", "unicode")), str(_req(u, "url_template", "unicode")),
                          tuple(_req(u, "files", "unicode")))
    if {"Scripts.txt", "ScriptExtensions.txt", "PropertyValueAliases.txt"} - set(unicode.files):
        raise ManifestError("unicode.files must list Scripts.txt, ScriptExtensions.txt and PropertyValueAliases.txt")

    r = _req(data, "render", "manifest")
    hinting = dict(_req(r, "hinting", "render"))
    if set(hinting) != set(HINTING_CLASSES) or any(m not in HINT_MODES for m in hinting.values()):
        raise ManifestError(f"render.hinting must map {list(HINTING_CLASSES)} to one of {list(HINT_MODES)}")
    icons_mode = str(_req(r, "icons", "render"))
    if icons_mode not in HINT_MODES:
        raise ManifestError(f"render.icons must be one of {list(HINT_MODES)}")
    render = RenderPolicy(str(_req(r, "freetype", "render")), str(_req(r, "freetype_py", "render")), hinting,
                          icons_mode)

    roles = set(get_args(FaceRole))
    faces: list[FaceEntry] = []
    for i, f in enumerate(_req(data, "faces", "manifest")):
        where = f"faces[{i}]"
        key = str(_req(f, "key", where))
        where = f"face {key!r}"
        face_id = _req(f, "face_id", where)
        role = _req(f, "role", where)
        scripts = _req(f, "scripts", where)
        languages = f.get("languages") or []
        hint = _req(f, "hinting", where)
        num_glyphs = _req(f, "num_glyphs", where)
        lic = str(_req(f, "license", where))
        variations = f.get("variations") or {}
        if not _KEY.fullmatch(key):
            raise ManifestError(f"{where}: key must be a lowercase slug")
        if not isinstance(face_id, int) or not 0 <= face_id <= 0xFFFF:
            raise ManifestError(f"{where}: face_id must be 0..65535")
        if role not in roles:
            raise ManifestError(f"{where}: role must be one of {sorted(roles)}")
        if not scripts or any(not _SCRIPT.fullmatch(str(s)) for s in scripts):
            raise ManifestError(f"{where}: scripts must be ISO 15924 codes")
        if any(not _LANG.fullmatch(str(lang)) for lang in languages):
            raise ManifestError(f"{where}: languages must be BCP-47 tags")
        if hint not in HINTING_CLASSES:
            raise ManifestError(f"{where}: hinting must be one of {list(HINTING_CLASSES)}")
        if not isinstance(num_glyphs, int) or not 0 < num_glyphs <= 0xFFFF:
            raise ManifestError(f"{where}: num_glyphs must be 1..65535")
        if lic not in license_ids:
            raise ManifestError(f"{where}: license {lic!r} is not in licenses")
        if not isinstance(variations, dict) or any(not _AXIS.fullmatch(str(a)) for a in variations):
            raise ManifestError(f"{where}: variations must map 4-character axis tags to values")
        release = f.get("release") or {}
        icon_map = icon_codepoints = None
        if role == "icons":
            icons = _req(f, "icons", where)
            icon_map = str(_req(icons, "map", f"{where}.icons"))
            cp = dict(_req(icons, "codepoints", f"{where}.icons"))
            cp.setdefault("source", f.get("source"))
            icon_codepoints = _pinned(cp, f"{where}.icons.codepoints", sources)
        faces.append(FaceEntry(
            face_id=face_id, key=key, family=str(_req(f, "family", where)), role=role,
            scripts=tuple(str(s) for s in scripts), languages=tuple(str(lang) for lang in languages),
            rtl=bool(f.get("rtl", False)), file=_pinned(f, where, sources), version=str(_req(f, "version", where)),
            name_version=str(_req(f, "name_version", where)), num_glyphs=num_glyphs, hinting=hint,
            variations=tuple((str(a), float(v)) for a, v in sorted(variations.items())),
            copyright=str(_req(f, "copyright", where)), trademark=f.get("trademark"), license=lic,
            release_tag=release.get("tag"), release_published=release.get("published"), note=f.get("note"),
            icon_map=icon_map, icon_codepoints=icon_codepoints))

    ids = [f.face_id for f in faces]
    keys = [f.key for f in faces]
    if len(set(ids)) != len(ids):
        raise ManifestError("duplicate face_id")
    if len(set(keys)) != len(keys):
        raise ManifestError("duplicate face key")
    icon_faces = [f for f in faces if f.is_icons]
    if len(icon_faces) != 1 or icon_faces[0].face_id != ICON_FACE_ID:
        raise ManifestError("exactly one face must have role 'icons', with face_id 0")
    cache_names = [f.file.cache_name for f in faces]
    if len(set(cache_names)) != len(cache_names):
        raise ManifestError("two faces share a cache file name (<source>/<basename>)")

    profiles: dict[str, Profile] = {}
    for name, p in (_req(data, "profiles", "manifest") or {}).items():
        where = f"profiles.{name}"
        pf = _req(p, "faces", where)
        if pf == "all":
            profile_faces = None
        else:
            profile_faces = tuple(str(k) for k in pf)
            if unknown := sorted(set(profile_faces) - set(keys)):
                raise ManifestError(f"{where}: unknown faces {unknown}")
            if icon_faces[0].key not in profile_faces:
                raise ManifestError(f"{where}: must include the icon face")
        profiles[name] = Profile(name, profile_faces, _sizes(_req(p, "text_sizes", where), FONT_SIZES, where),
                                 _sizes(_req(p, "icon_sizes", where), ICON_SIZES, where),
                                 p.get("flash_size"), p.get("working_space"))
    if "full" not in profiles:
        raise ManifestError("profiles must define 'full'")

    return Manifest(path=path, pack_name=str(_req(data, "pack_name", "manifest")), sources=sources,
                    licenses=tuple(licenses), unicode=unicode, render=render, profiles=profiles,
                    faces=tuple(sorted(faces, key=lambda f: f.face_id)))


def load_manifest(path: Path | None = None) -> Manifest:
    path = Path(path) if path else default_manifest_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    return parse_manifest(data, path)


def load_icon_map(manifest: Manifest) -> tuple[IconGlyph, ...]:
    """The spec icons in id order; must match ``protocol/spec.yaml`` exactly."""
    face = manifest.icon_face
    assert face.icon_map is not None
    path = manifest.directory / face.icon_map
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if data.get("face") != face.key:
        raise ManifestError(f"{path}: face must be {face.key!r}")
    icons = tuple(IconGlyph(int(i["id"]), str(i["name"]), str(i["material"]), int(i["codepoint"]), i.get("note"))
                  for i in data.get("icons") or [])
    expected = [(icon.value, icon.name.lower()) for icon in Icon]
    if [(i.id, i.name) for i in icons] != expected:
        raise ManifestError(f"{path}: icons must list exactly the spec icons in id order: {expected}")
    return icons


def parse_codepoints(text: str) -> dict[str, int]:
    """Material Icons ``.codepoints``: ``<name> <hex>`` per line."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        if line.strip():
            name, hexcode = line.split()
            out[name] = int(hexcode, 16)
    return out


def check_icon_codepoints(icons: tuple[IconGlyph, ...], codepoints: dict[str, int]) -> None:
    for icon in icons:
        actual = codepoints.get(icon.material)
        if actual != icon.codepoint:
            raise ManifestError(f"icon {icon.name}: Material {icon.material!r} is "
                                f"{'missing' if actual is None else hex(actual)} in the codepoints file, "
                                f"icons.yaml says {icon.codepoint:#x}")


# --------------------------------------------------------------------------- lock


@dataclass(frozen=True)
class LockedFile:
    id: str
    url: str
    size: int
    sha256: str
    cache: str
    """Path relative to the cache directory."""
    source: str | None = None
    path: str | None = None
    git_blob_sha1: str | None = None

    def as_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"url": self.url, "size": self.size, "sha256": self.sha256, "cache": self.cache}
        if self.source is not None:
            d |= {"source": self.source, "path": self.path, "git_blob_sha1": self.git_blob_sha1}
        return d


@dataclass(frozen=True)
class Lock:
    files: dict[str, LockedFile]
    raw: bytes

    @property
    def manifest_id(self) -> bytes:
        """SHA-256 of the lock file bytes, first 8 bytes (the pack header's manifest id)."""
        return hashlib.sha256(self.raw).digest()[:8]

    def file(self, file_id: str) -> LockedFile:
        try:
            return self.files[file_id]
        except KeyError:
            raise ManifestError(f"{file_id!r} is not in the lock; run `cremind-tag fonts lock`") from None


def lock_bytes(files: dict[str, LockedFile], unicode_version: str) -> bytes:
    """Deterministic lock-file serialisation (sorted, LF, no timestamps)."""
    doc = {"schema": LOCK_SCHEMA, "unicode_version": unicode_version,
           "files": {fid: files[fid].as_json() for fid in sorted(files)}}
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def load_lock(path: Path) -> Lock:
    try:
        raw = path.read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}; run `cremind-tag fonts lock`") from exc
    if doc.get("schema") != LOCK_SCHEMA:
        raise ManifestError(f"{path}: schema must be {LOCK_SCHEMA!r}")
    files = {fid: LockedFile(fid, f["url"], f["size"], f["sha256"], f["cache"], f.get("source"), f.get("path"),
                             f.get("git_blob_sha1")) for fid, f in doc["files"].items()}
    return Lock(files, raw)


def check_lock_current(manifest: Manifest, lock: Lock) -> None:
    """Raise when the manifest pins a file the lock does not record identically."""
    stale = []
    for fid, pinned in manifest.pinned_files().items():
        locked = lock.files.get(fid)
        if (locked is None or locked.source != pinned.source or locked.path != pinned.path
                or locked.size != pinned.size or locked.git_blob_sha1 != pinned.git_blob_sha1
                or locked.url != manifest.url(pinned)):
            stale.append(fid)
    for name in manifest.unicode.files:
        locked = lock.files.get(f"unicode:{name}")
        if locked is None or locked.url != manifest.unicode.url(name):
            stale.append(f"unicode:{name}")
    if stale:
        raise ManifestError(f"the lock is out of date for {', '.join(stale[:5])}"
                            f"{' ...' if len(stale) > 5 else ''}; run `cremind-tag fonts lock`")
