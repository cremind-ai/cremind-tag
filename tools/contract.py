#!/usr/bin/env python3
"""Build (or check) the protocol contract artifact that host software pins.

The wire protocol is defined here, next to the firmware that implements it.
Host software (Cremind's hardware runtime) does not read this checkout: it
pins an immutable **contract artifact** built from it and tests its own
bindings against that snapshot. The artifact holds:

- ``contract.json`` (schema ``cremind-tag/contract@1``): the contract
  ``version`` (this repository's ``VERSION``), the protocol capabilities
  (``spec_version``, ``PROTO_VERSION``, ``SECURE_PROTO_VERSION``, the font
  pack format version), the source repository and revision, the SHA-256 of
  every file, and ``digest``;
- ``spec.yaml`` (the single source of truth for identifiers and limits);
- ``fixtures/`` (the byte-exact golden vectors both sides test against);
- ``docs/`` (the normative protocol documents).

``digest`` is the SHA-256 of the ``SHA256SUMS``-style listing of every other
file (``<sha256>  <path>`` lines, sorted by path, LF endings), so one value
names the whole contract. The archive is deterministic (sorted members, no
timestamps or owners), so building the same revision twice gives the same
bytes.

Usage::

    python tools/contract.py                   # dist/contract/cremind-tag-contract-<version>{,.tar.gz,.tar.gz.sha256}
    python tools/contract.py --out DIR         # somewhere else
    python tools/contract.py --check DIR       # verify an unpacked contract (exit 1 on any mismatch)

Only the standard library and PyYAML are needed. A build from a checkout whose
contract inputs have uncommitted changes is refused (the recorded revision
would not describe them) unless ``--allow-dirty`` is given.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "cremind-tag/contract@1"
NAME = "cremind-tag-contract"
REPOSITORY = "https://github.com/cremind-ai/cremind-tag"
SPEC = "protocol/spec.yaml"
FIXTURES = "protocol/fixtures"
DOCS = ("docs/protocol.md", "docs/fontpack.md", "docs/connect-setup.md")
META = "contract.json"


class ContractError(Exception):
    """The contract cannot be built, or a checked contract does not match its manifest."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def listing(files: dict[str, str]) -> str:
    """``SHA256SUMS`` text of ``{path: sha256}``: the input of ``digest``."""
    return "".join(f"{files[path]}  {path}\n" for path in sorted(files))


def digest_of(files: dict[str, str]) -> str:
    return sha256_bytes(listing(files).encode("ascii"))


def _normalised(path: Path) -> bytes:
    """File bytes as committed: text files with LF endings (Windows checkouts may carry CRLF)."""
    data = path.read_bytes()
    if path.suffix in (".yaml", ".json", ".md"):
        data = data.replace(b"\r\n", b"\n")
    return data


def contract_inputs(root: Path = ROOT) -> dict[str, Path]:
    """Artifact path -> source file, in artifact order."""
    out: dict[str, Path] = {"spec.yaml": root / SPEC}
    fixtures = root / FIXTURES
    for path in sorted(fixtures.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            out[f"fixtures/{path.name}"] = path
    for doc in DOCS:
        path = root / doc
        if path.is_file():
            out[f"docs/{path.name}"] = path
    return out


def protocol_capabilities(spec_text: str) -> dict[str, int]:
    spec = yaml.safe_load(spec_text)
    constants = spec.get("constants") or {}

    def const(name: str) -> int:
        value = (constants.get(name) or {}).get("value")
        if not isinstance(value, int):
            raise ContractError(f"{SPEC}: constant {name} is missing")
        return value

    return {"spec_version": int(spec["spec_version"]), "proto_version": const("PROTO_VERSION"),
            "secure_proto_version": const("SECURE_PROTO_VERSION"),
            "fontpack_version": int((spec.get("fontpack") or {})["version"])}


def _git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def source_revision(root: Path = ROOT, *, allow_dirty: bool = False) -> dict[str, Any]:
    revision = _git("rev-parse", "HEAD", root=root)
    tracked = [SPEC, FIXTURES, *DOCS]
    dirty = bool(_git("status", "--porcelain", "--", *tracked, root=root))
    if dirty and not allow_dirty:
        raise ContractError("the contract's inputs have uncommitted changes; commit them first "
                            "(or pass --allow-dirty for a local experiment)")
    return {"repository": REPOSITORY, "revision": revision, "dirty": dirty}


def build(out_dir: Path, *, root: Path = ROOT, allow_dirty: bool = False) -> tuple[Path, Path]:
    """Write ``<out_dir>/<name>-<version>/`` and its ``.tar.gz`` (+ ``.sha256``); returns (dir, archive)."""
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    inputs = contract_inputs(root)
    blobs = {name: _normalised(path) for name, path in inputs.items()}
    files = {name: sha256_bytes(data) for name, data in blobs.items()}
    meta = {
        "schema": SCHEMA,
        "name": NAME,
        "version": version,
        "protocol": protocol_capabilities(blobs["spec.yaml"].decode("utf-8")),
        "source": source_revision(root, allow_dirty=allow_dirty),
        "files": dict(sorted(files.items())),
        "digest": digest_of(files),
    }
    base = f"{NAME}-{version}"
    target = out_dir / base
    if target.exists():
        shutil.rmtree(target)
    for name, data in blobs.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    meta_bytes = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
    (target / META).write_bytes(meta_bytes)
    archive = out_dir / f"{base}.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted({**blobs, META: meta_bytes}.items()):
            info = tarfile.TarInfo(f"{base}/{name}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    archive.write_bytes(compressed.getvalue())
    (out_dir / f"{base}.tar.gz.sha256").write_text(f"{sha256_bytes(compressed.getvalue())}  {archive.name}\n",
                                                   encoding="ascii")
    return target, archive


def check(directory: Path) -> dict[str, Any]:
    """Verify an unpacked contract; returns its ``contract.json`` or raises :class:`ContractError`."""
    try:
        meta = json.loads((directory / META).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"{directory / META}: {exc}") from None
    if meta.get("schema") != SCHEMA:
        raise ContractError(f"{directory / META}: schema {meta.get('schema')!r}, expected {SCHEMA}")
    files = meta.get("files")
    if not isinstance(files, dict) or not files:
        raise ContractError(f"{directory / META} lists no files")
    problems = []
    for name, expected in sorted(files.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"missing {name}")
        elif sha256_bytes(_normalised(path)) != expected:
            problems.append(f"changed {name}")
    extra = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")
                   if p.is_file() and p.name != META and p.relative_to(directory).as_posix() not in files)
    problems += [f"unlisted {name}" for name in extra]
    if digest_of(files) != meta.get("digest"):
        problems.append("digest does not match the file list")
    if problems:
        raise ContractError(f"{directory}: " + "; ".join(problems))
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "contract", help="output directory")
    parser.add_argument("--allow-dirty", action="store_true", help="build although the inputs have changes")
    parser.add_argument("--check", type=Path, metavar="DIR", help="verify an unpacked contract instead")
    args = parser.parse_args(argv)
    try:
        if args.check is not None:
            meta = check(args.check)
            print(f"ok: {meta['name']} {meta['version']} ({meta['source']['revision'][:12]}), digest {meta['digest']}")
            return 0
        args.out.mkdir(parents=True, exist_ok=True)
        target, archive = build(args.out, allow_dirty=args.allow_dirty)
        meta = json.loads((target / META).read_text(encoding="utf-8"))
        print(f"wrote {archive} (digest {meta['digest']})")
        return 0
    except (ContractError, subprocess.CalledProcessError, OSError, KeyError) as exc:
        print(f"contract: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
