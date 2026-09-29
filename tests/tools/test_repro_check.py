"""tools/repro_check.py: image comparison, diagnosis and the two-checkout orchestration (no Docker needed)."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import pytest

import repro_check as rc


def to_hex(data: bytes, base: int = 0) -> str:
    lines = []
    if base >> 16:
        rec = bytes([2, 0, 0, 4]) + (base >> 16).to_bytes(2, "big")
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    for off in range(0, len(data), 16):
        chunk, addr = data[off : off + 16], (base + off) & 0xFFFF
        rec = bytes([len(chunk), addr >> 8, addr & 0xFF, 0]) + chunk
        lines.append(":" + (rec + bytes([(-sum(rec)) & 0xFF])).hex().upper())
    return "\n".join([*lines, ":00000001FF"]) + "\n"


def elf_with_symbols(symbols: list[tuple[str, int, int, int]]) -> bytes:
    """ELF32 LE: .text at 0 (0x100), .data at 0x20000000 loaded from 0x100, a symtab (name, value, size, type)."""
    shstr = b"\0.text\0.data\0.symtab\0.strtab\0.shstrtab\0"
    strtab, names = b"\0", []
    for name, *_ in symbols:
        names.append(len(strtab))
        strtab += name.encode() + b"\0"
    symtab = bytes(16) + b"".join(
        struct.pack("<IIIBBH", n, value, size, (1 << 4) | kind, 0, 1)
        for n, (_, value, size, kind) in zip(names, symbols)
    )
    segments = [(0x0, 0x0, 0x100), (0x20000000, 0x100, 0x10)]
    phoff = 52
    cur = phoff + 32 * len(segments)
    offsets = []
    for blob in (shstr, strtab, symtab):
        offsets.append(cur)
        cur += len(blob)

    def at(name: str) -> int:
        return shstr.index(name.encode() + b"\0")

    sections = [
        (0,) * 10,
        (at(".text"), 1, 0x6, 0x0, 0, 0x100, 0, 0, 4, 0),
        (at(".data"), 1, 0x3, 0x20000000, 0, 0x10, 0, 0, 4, 0),
        (at(".symtab"), 2, 0, 0, offsets[2], len(symtab), 4, 1, 4, 16),
        (at(".strtab"), 3, 0, 0, offsets[1], len(strtab), 0, 0, 1, 0),
        (at(".shstrtab"), 3, 0, 0, offsets[0], len(shstr), 0, 0, 1, 0),
    ]
    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9) + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, 0, phoff, cur, 0, 52, 32, len(segments), 40, len(sections), 5)
    ph = b"".join(struct.pack("<IIIIIIII", 1, 0, v, p, fs, fs, 5, 4) for v, p, fs in segments)
    return header + ph + shstr + strtab + symtab + b"".join(struct.pack("<IIIIIIIIII", *s) for s in sections)


SYMBOLS = [("main", 0x10, 0x40, 2), ("assert_file", 0x60, 0x30, 1), ("config_blob", 0x20000000, 0x10, 1)]


def write_build(d: Path, image: bytes, elf: bytes | None = None, meta: dict | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "zephyr.hex").write_bytes(to_hex(image).encode())
    (d / "zephyr.bin").write_bytes(image)
    (d / "zephyr.elf").write_bytes(elf if elf is not None else elf_with_symbols(SYMBOLS))
    (d / "metadata.json").write_text(json.dumps(meta or {"inputs": {"kconfig_sha256": "k"},
                                                        "verify_stack": {"ok": True}}), encoding="utf-8")
    return d


def test_intel_hex_and_ranges():
    mem = rc.parse_intel_hex(to_hex(b"\x01\x02\x03", base=0x10000))
    assert mem == {0x10000: 1, 0x10001: 2, 0x10002: 3}
    assert rc.diff_ranges({0: 1, 1: 2, 2: 3, 5: 9}, {0: 1, 1: 7, 2: 8, 6: 9}) == [(1, 3), (5, 7)]
    with pytest.raises(ValueError, match="checksum"):
        rc.parse_intel_hex(":0100000001FF\n")


def test_elf_locate():
    elf = rc.read_elf(elf_with_symbols(SYMBOLS))
    assert [s.name for s in elf.sections][1:] == [".text", ".data", ".symtab", ".strtab", ".shstrtab"]
    assert elf.locate(0x18) == (".text", "main+0x8")
    assert elf.locate(0x104) == (".data", "config_blob+0x4")  # loaded from flash, runs in RAM
    assert elf.locate(0xF0) == (".text", None)
    with pytest.raises(ValueError):
        rc.read_elf(b"nope")


def _image(path_text: bytes, date: bytes = b"Sep 28 2026") -> bytes:
    body = bytes(0x60) + path_text.ljust(0x30, b"\0") + date.ljust(0x10, b"\0")
    return body.ljust(0x100, b"\xff")


def test_diagnose_names_paths_timestamps_and_symbols(tmp_path: Path):
    a = write_build(tmp_path / "a", _image(b"/build/repro/a/lib/x.c"))
    b = write_build(tmp_path / "b", _image(b"/build/repro/b-longer/lib/x.c"))
    diag = rc.diagnose(a, b, ["/build/repro/a", "/build/repro/b-longer"])
    assert diag["differing_bytes"] > 0 and diag["range_count"] >= 1
    first = diag["ranges"][0]
    assert first["section"] == ".text" and first["symbol_a"].startswith("assert_file+")
    assert "/build/repro/a/lib/x.c" in diag["path_strings"]["only_in_one_image"]
    assert any("-ffile-prefix-map" in h for h in diag["hints"])

    c = write_build(tmp_path / "c", _image(b"/same/path.c", b"Sep 28 2026"))
    d = write_build(tmp_path / "d", _image(b"/same/path.c", b"Sep 29 2026"))
    diag = rc.diagnose(c, d, [])
    assert "Sep 28 2026" in diag["timestamps"] and any("SOURCE_DATE_EPOCH" in h for h in diag["hints"])


def test_compare_target_statuses(tmp_path: Path):
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    image = _image(b"/cremind-tag/lib/x.c")
    write_build(out_a / "same", image)
    write_build(out_b / "same", image)
    assert rc.compare_target("same", out_a, out_b, [])["status"] == "reproducible"
    write_build(out_a / "elf", image)
    write_build(out_b / "elf", image, elf=elf_with_symbols(SYMBOLS[:2]))
    assert rc.compare_target("elf", out_a, out_b, [])["status"] == "image-reproducible"
    write_build(out_a / "diff", image)
    write_build(out_b / "diff", _image(b"/other/lib/x.c"))
    result = rc.compare_target("diff", out_a, out_b, [])
    assert result["status"] == "DIFFERENT" and result["diagnosis"]["ranges"]
    (out_a / "failed").mkdir()
    (out_a / "failed" / "build.log").write_text("boom", encoding="utf-8")
    assert rc.compare_target("failed", out_a, out_b, [])["status"] == "build-failed"
    assert rc.compare_target("absent", out_a, out_b, [])["status"] == "skipped"
    write_build(out_a / "half", image)
    assert rc.compare_target("half", out_a, out_b, [])["status"] == "build-failed"
    statuses = [{"status": "reproducible"}, {"status": "image-reproducible"}]
    assert rc._exit_status(statuses) == 0
    assert rc._exit_status([*statuses, {"status": "skipped"}]) == 3
    assert rc._exit_status([*statuses, {"status": "DIFFERENT"}]) == 1


def test_snapshot_skips_output_and_caches_only(tmp_path: Path):
    src = tmp_path / "src"
    for rel in ("build/t/zephyr.hex", "build-x/y", "dist/0.1.0/a", "tools/.venv/lib/x", "apps/tag/__pycache__/m.pyc",
                "apps/tag/out/keep.c", "apps/tag/src/main.c", ".git/HEAD", "tests/host/build/x", "tests/host/t.c"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    rc.snapshot(src, tmp_path / "dst")
    copied = {p.relative_to(tmp_path / "dst").as_posix() for p in (tmp_path / "dst").rglob("*") if p.is_file()}
    assert copied == {"apps/tag/out/keep.c", "apps/tag/src/main.c", ".git/HEAD", "tests/host/t.c"}


def test_run_firmware_builds_two_checkouts(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "apps" / "tag").mkdir(parents=True)
    (repo / "apps" / "tag" / "main.c").write_text("int main;", encoding="utf-8")
    monkeypatch.setattr(rc, "REPO_ROOT", repo)
    monkeypatch.setattr(rc, "REPORT_DIR", repo / "build" / "repro")
    calls = []

    def fake_build(checkout: Path, build_root: Path, targets, skip, log: Path) -> int:
        calls.append((checkout, build_root, skip))
        assert (checkout / "apps" / "tag" / "main.c").is_file()
        log.write_text("log", encoding="utf-8")
        leak = b"/x" if not calls[1:] else b"/y"  # the second build of "leaky" differs
        write_build(checkout / "build" / "repro-out" / "stable", _image(b"/cremind-tag/a.c"))
        write_build(checkout / "build" / "repro-out" / "leaky", _image(checkout.as_posix().encode()[:40] + leak))
        return 0

    monkeypatch.setattr(rc, "_build", fake_build)
    args = argparse.Namespace(work_root=str(tmp_path / "work"), keep=False)
    results, _ = rc.run_firmware(args, ["stable", "leaky"])
    (a, root_a, skip_a), (b, root_b, skip_b) = calls
    assert a != b and a.name != b.name and len(b.parts) > len(a.parts)  # other path, other depth
    assert root_a != root_b and not skip_a and skip_b
    assert [r["status"] for r in results] == ["reproducible", "DIFFERENT"]
    kept = repo / "build" / "repro" / "leaky"
    assert (kept / "a" / "zephyr.hex").is_file() and (kept / "b" / "zephyr.hex").is_file()
    assert not (tmp_path / "work").exists()  # scratch removed without --keep
