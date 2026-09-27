#!/usr/bin/env python3
"""Drive the bridge maintenance port (the native_sim build of tests/maint_pty)
with the companion's BridgeMaintClient: HELLO, PING, INFO, FONT_STATUS, a
complete font install (FONT_ABORT, FONT_BEGIN, FONT_DATA, FONT_COMMIT), an
install of the active pack (skipped), a forced install into the other slot and
FLASH_TEST twice with the same op_id.

    python3 interop.py <zephyr.exe> [pack.ctfp]

Needs pyserial and cbor2 (both in the NCS toolchain image) and the companion
sources. Exit status 0 = interoperable.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "companion" / "src"))

from cremind_tag.bridge_maint.client import BridgeMaintClient  # noqa: E402
from cremind_tag.fontpack.format import FontPack  # noqa: E402
from cremind_tag.protocol.ids import NodeRole, Status  # noqa: E402


def start(exe: str) -> tuple[subprocess.Popen[str], str]:
    # A fresh directory: the simulated part (flash.bin) starts erased.
    workdir = tempfile.mkdtemp(prefix="maint_pty-")
    proc = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            cwd=workdir)
    assert proc.stdout is not None
    for _ in range(50):
        line = proc.stdout.readline()
        m = re.search(r"connected to pseudotty: (\S+)", line)
        if m:
            return proc, m.group(1)
    proc.kill()
    raise SystemExit("no pseudo-terminal announced")


def names(items: object) -> list[tuple[str, str]]:
    return [(hex(i.offset), getattr(i.status, "name", str(i.status))) for i in items]  # type: ignore[attr-defined]


async def run(pty: str, pack: bytes) -> None:
    pack_id = FontPack(pack).pack_id
    async with BridgeMaintClient(pty) as client:
        hello = client.hello_info
        assert hello is not None and hello.caps.role == NodeRole.BRIDGE, hello
        print(f"HELLO: fw {hello.fw} build {hello.build} boot_id {hello.boot_id:08x} caps "
              f"max_frame={hello.caps.max_frame} credits={hello.caps.credits} board={hello.caps.board}")
        print("PING: uptime", await client.ping(), "s")
        status = await client.font_status()
        assert status.fontpack_id is None, status
        print("FONT_STATUS: no pack yet; flash_size", status.flash_size)
        result = await client.font_install(pack)
        assert result.fontpack_id == pack_id and not result.skipped and result.slot == 0, result
        print(f"install: pack {pack_id.hex()} in slot {result.slot} of a {result.flash_size}-byte part")
        status = await client.font_status()
        assert status.fontpack_id == pack_id and status.slot == 0 and status.size == len(pack), status
        again = await client.font_install(pack)
        assert again.skipped, again
        print("install of the active pack: skipped")
        forced = await client.font_install(pack, force=True, chunk_size=333)
        assert forced.slot == 1 and forced.fontpack_id == pack_id, forced
        print("forced install (333-byte chunks): slot", forced.slot)
        test = await client.flash_test(op_id=77)
        print("FLASH_TEST:", names(test.items))
        assert test.status == Status.OK and test.items, test
        assert all(i.status in (Status.OK, Status.BUSY) for i in test.items), test
        repeat = await client.flash_test(op_id=77)
        assert repeat.items == test.items, repeat
        info = await client.info()
        assert info.counters.get("fonts_installed") == 2, info.counters
        # Every FONT_DATA frame fit caps.max_frame: the client sized its chunks from HELLO.
        assert info.counters.get("oversize", 0) == 0 and info.counters.get("len_errors", 0) == 0, info.counters
        print(f"FONT_DATA: {min(2048, hello.caps.max_frame - 36)}-byte chunks for max_frame "
              f"{hello.caps.max_frame}, none oversize")
        print(f"INFO: {len(info.counters)} counters, fonts_installed={info.counters['fonts_installed']}, "
              f"crc_errors={info.counters.get('crc_errors')}, overruns={info.counters.get('overruns')}")


def main() -> int:
    exe = sys.argv[1]
    pack_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "protocol" / "fixtures" / "fontpack_test.ctfp"
    proc, pty = start(exe)
    try:
        asyncio.run(asyncio.wait_for(run(pty, pack_path.read_bytes()), 120))
    finally:
        proc.kill()
    print("interop: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
