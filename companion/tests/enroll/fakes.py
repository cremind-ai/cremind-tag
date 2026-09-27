"""A simulated nRF target behind fake nrfutil / nrfjprog / J-Link Commander runners.

The device keeps a sparse NOR memory (erased = 0xFF, programming only clears
bits), so a plan that forgets to erase UICR before rewriting it reads back a
corrupted blob — exactly what real flash would do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cremind_tag.enroll.tools import CompletedResult

UICR_BASE = 0x10001000
UICR_END = 0x10002000
PAGE = 0x1000


def parse_intel_hex(text: str) -> dict[int, int]:
    """Address -> byte for the data records of an I32HEX file."""
    memory: dict[int, int] = {}
    upper = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        assert line.startswith(":"), line
        raw = bytes.fromhex(line[1:])
        assert sum(raw) & 0xFF == 0, f"bad checksum: {line}"
        count, addr, rtype, data = raw[0], int.from_bytes(raw[1:3], "big"), raw[3], raw[4:-1]
        assert len(data) == count
        if rtype == 0x00:
            for i, b in enumerate(data):
                memory[(upper << 16) + addr + i] = b
        elif rtype == 0x04:
            upper = int.from_bytes(data, "big")
        elif rtype == 0x01:
            break
    return memory


def ascii_column(chunk: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)


def nrfjprog_dump(data: bytes, address: int) -> str:
    """``--memrd --w 32``: four little-endian words per line and an ASCII column."""
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        words = " ".join(f"{int.from_bytes(chunk[i:i + 4], 'little'):08X}" for i in range(0, len(chunk), 4))
        lines.append(f"0x{address + off:08X}: {words}   |{ascii_column(chunk)}|")
    return "\n".join(lines) + "\n"


def jlink_dump(data: bytes, address: int) -> str:
    """``mem32``: ``ADDR = w w w w`` lines, surrounded by the noise J-Link prints."""
    lines = ["J-Link>h", "PC = 00000D14, CycleCnt = 00000000",
             "R0 = 00000000, R1 = 20000C88, R2 = 00000001, R3 = 00000000",
             "SP(R13)= 20001000, MSP= 20001000, PSP= 00000000, R14(LR) = FFFFFFF9",
             "XPSR = 01000000: APSR = nzcvq, EPSR = 01000000, IPSR = 000 (NoException)",
             f"J-Link>mem32 0x{address:08X}, 0x{(len(data) + 3) // 4:02X}"]
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        words = " ".join(f"{int.from_bytes(chunk[i:i + 4], 'little'):08X}" for i in range(0, len(chunk), 4))
        lines.append(f"{address + off:08X} = {words} ")
    lines.append("J-Link>qc")
    return "\n".join(lines) + "\n"


def byte_dump(data: bytes, address: int) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        lines.append(f"0x{address + off:08X}: {' '.join(f'{b:02X}' for b in chunk)}  |{ascii_column(chunk)}|")
    return "\n".join(lines) + "\n"


class FakeDevice:
    """A CommandRunner for all three tools, simulating one nRF target."""

    def __init__(self, *, memory: dict[int, int] | None = None, fail_on: str | None = None,
                 corrupt_readback: bool = False, fail_reset: bool = False, nrfutil_json: bool = True) -> None:
        self.memory: dict[int, int] = dict(memory or {})
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []
        self.protected = False
        self.resets = 0
        self.fail_on = fail_on
        self.corrupt_readback = corrupt_readback
        self.fail_reset = fail_reset
        self.nrfutil_json = nrfutil_json
        self._nvmc_config = 0

    # -- memory -------------------------------------------------------------------

    def read(self, address: int, length: int) -> bytes:
        return bytes(self.memory.get(a, 0xFF) for a in range(address, address + length))

    def erase_all(self) -> None:
        self.memory.clear()

    def erase_range(self, start: int, end: int) -> None:
        for a in [a for a in self.memory if start <= a < end]:
            del self.memory[a]

    def program(self, image: dict[int, int]) -> None:
        for a, b in image.items():
            self.memory[a] = self.memory.get(a, 0xFF) & b

    def erase_touched(self, image: dict[int, int]) -> None:
        for page in {a // PAGE * PAGE for a in image}:
            self.erase_range(page, page + PAGE)

    def _dump_bytes(self, address: int, length: int) -> bytes:
        data = bytearray(self.read(address, length))
        if self.corrupt_readback:
            data[7] ^= 0x01
        return bytes(data)

    # -- runner --------------------------------------------------------------------

    def __call__(self, argv: list[str], *, input: str | None = None, timeout: float) -> CompletedResult:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert timeout > 0
        self.calls.append(list(argv))
        if "-CommanderScript" in argv:
            script = Path(argv[argv.index("-CommanderScript") + 1]).read_text(encoding="ascii")
            self.scripts.append(script)
            if self.fail_on and self.fail_on in script:
                return CompletedResult(1, "****** Error: simulated failure\n", "")
            return self._jlink(script)
        if self.fail_on and self.fail_on in argv:
            return CompletedResult(1, "", f"ERROR: simulated failure of {self.fail_on}\n")
        if argv[1:2] == ["device"]:
            return self._nrfutil(argv[2:])
        return self._nrfjprog(argv[1:])

    def _reset(self) -> CompletedResult:
        if self.fail_reset:
            return CompletedResult(1, "", "ERROR: reset failed\n")
        self.resets += 1
        return CompletedResult(0, "Applying system reset.\nRun.\n", "")

    def _nrfjprog(self, args: list[str]) -> CompletedResult:
        if "--program" in args:
            image = parse_intel_hex(Path(args[args.index("--program") + 1]).read_text(encoding="ascii"))
            if "--chiperase" in args:
                self.erase_all()
            elif "--sectoranduicrerase" in args or "--sectorerase" in args:
                self.erase_touched(image)
            self.program(image)
            if "--verify" in args and any(self.memory.get(a) != b for a, b in image.items()):
                return CompletedResult(1, "", "ERROR: Write verify failed.\n")
            return CompletedResult(0, "Parsing image file.\nVerified OK.\n", "")
        if "--eraseuicr" in args:
            self.erase_range(UICR_BASE, UICR_END)
            return CompletedResult(0, "Erasing UICR flash area.\n", "")
        if "--eraseall" in args:
            self.erase_all()
            return CompletedResult(0, "Erasing user available code and UICR flash areas.\n", "")
        if "--memrd" in args:
            address = int(args[args.index("--memrd") + 1], 16)
            length = int(args[args.index("--n") + 1])
            return CompletedResult(0, nrfjprog_dump(self._dump_bytes(address, length), address), "")
        if "--rbp" in args:
            self.protected = True
            return CompletedResult(0, "", "")
        if "--reset" in args:
            return self._reset()
        return CompletedResult(2, "", f"ERROR: unknown fake nrfjprog arguments {args}\n")

    def _nrfutil(self, args: list[str]) -> CompletedResult:
        op = args[0]
        if op == "program":
            image = parse_intel_hex(Path(args[args.index("--firmware") + 1]).read_text(encoding="ascii"))
            options = dict(o.split("=", 1) for o in args[args.index("--options") + 1].split(","))
            mode = options["chip_erase_mode"]
            if mode == "ERASE_ALL":
                self.erase_all()
            elif mode == "ERASE_RANGES_TOUCHED_BY_FIRMWARE":
                self.erase_touched(image)
            else:
                assert mode == "ERASE_NONE", mode
            self.program(image)
            return CompletedResult(0, "[00:00:01] ###### 100% [1/1 682000123] Programmed\n", "")
        if op == "erase" and "--all" in args:
            self.erase_all()
            return CompletedResult(0, "", "")
        if op == "read":
            address = int(args[args.index("--address") + 1], 16)
            length = int(args[args.index("--bytes") + 1])
            data = self._dump_bytes(address, length)
            if self.nrfutil_json:
                return CompletedResult(0, json.dumps({"type": "info", "data": {"address": address}}) + "\n"
                                       + json.dumps({"type": "task_end", "data": {"data": list(data)}}) + "\n", "")
            return CompletedResult(0, nrfjprog_dump(data, address), "")
        if op == "protection-set":
            self.protected = args[1] == "All"
            return CompletedResult(0, "", "")
        if op == "reset":
            return self._reset()
        return CompletedResult(2, "", f"error: unknown fake nrfutil arguments {args}\n")

    def _jlink(self, script: str) -> CompletedResult:
        out: list[str] = ["SEGGER J-Link Commander V8.10 (Compiled Jan  1 2026)", "Connecting to target via SWD",
                          "Cortex-M0 identified."]
        for line in script.splitlines():
            parts = line.replace(",", " ").split()
            if not parts:
                continue
            cmd = parts[0].lower()
            if cmd == "erase":
                self.erase_all()
                out.append("Erasing device...\nJ-Link: Flash download: Total time needed: 0.5s\nErasing done.")
            elif cmd == "loadfile":
                path = line.split(None, 1)[1].strip().strip('"')
                self.program(parse_intel_hex(Path(path).read_text(encoding="ascii")))
                out.append("Downloading file... O.K.")
            elif cmd == "w4":
                address, value = int(parts[1], 16), int(parts[2], 16)
                if address == 0x4001E504:
                    self._nvmc_config = value
                elif address == 0x4001E514 and value == 1:
                    assert self._nvmc_config == 2, "ERASEUICR needs NVMC.CONFIG = EEN"
                    self.erase_range(UICR_BASE, UICR_END)
                elif UICR_BASE <= address < UICR_END:
                    assert self._nvmc_config == 1, "UICR writes need NVMC.CONFIG = WEN"
                    self.program({address + i: b for i, b in enumerate(value.to_bytes(4, "little"))})
                    if address in (0x10001208, 0x10001004):
                        self.protected = True
            elif cmd == "mem32":
                address, words = int(parts[1], 16), int(parts[2], 16)
                out.append(jlink_dump(self._dump_bytes(address, words * 4), address))
            elif cmd == "r":
                out.append("Reset delay: 0 ms\nReset type NORMAL: Resets core & peripherals via SYSRESETREQ.")
            elif cmd == "g":
                if self.fail_reset:
                    return CompletedResult(1, "****** Error: could not start CPU\n", "")
                self.resets += 1
            elif cmd in ("h", "qc", "sleep"):
                pass
            else:
                return CompletedResult(1, f"Unknown command. '?' for help. ({line})\n", "")
        return CompletedResult(0, "\n".join(out) + "\n", "")


def call_names(calls: list[list[str]]) -> list[str]:
    """A one-word summary per call, for asserting the order of operations."""
    names: list[str] = []
    for argv in calls:
        joined = " ".join(argv)
        match = re.search(r"jlink-([a-z]+)-", joined)
        if match:
            names.append(f"jlink:{match.group(1)}")
        elif argv[1:2] == ["device"]:
            names.append(f"nrfutil:{argv[2]}")
        else:
            names.append("nrfjprog:" + next(a for a in argv[1:] if a.startswith("--")
                                            and a not in ("--snr",)).lstrip("-"))
    return names
