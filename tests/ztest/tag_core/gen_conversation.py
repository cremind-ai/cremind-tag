#!/usr/bin/env python3
"""Script bridge <-> tag conversations for the tag core tests (native_sim).

The bridge side is played with the companion's Python reference
(``cremind_tag.protocol.session``, ``.fragments``, ``.msgs``): HELLO, AUTH and
the authenticated records are built and fragmented exactly as a bridge sends
them, and every ATT value the tag must answer (CHALLENGE, AUTH_OK, CREDIT,
PROGRESS, RESULT, ERROR, fragment by fragment) is computed with the same
reference and the rules of docs/protocol.md 5-6 and docs/tag-firmware.md.
The C test (src/main.c) replays each script against apps/tag/src/tag_core.c
with fake panel/storage/clock/RNG hooks and compares byte for byte.

Frames are the small-panel scenarios of protocol/fixtures/render.json whose
plane bytes are in the fixture, so FRAME_BEGIN carries the fixture's frame
digest.

Run with the companion environment (it needs ``cryptography``):

    companion/.venv/bin/python tests/ztest/tag_core/gen_conversation.py          # write
    companion/.venv/bin/python tests/ztest/tag_core/gen_conversation.py --check  # CI: stale?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "companion" / "src"))

from cremind_tag.protocol import session as S  # noqa: E402
from cremind_tag.protocol.fragments import Fragmenter  # noqa: E402
from cremind_tag.protocol.ids import (  # noqa: E402
    PROTO_VERSION,
    TAG_CTRL_MSG_MAX,
    TAG_RECORD_WIRE_MAX,
    CtrlMsg,
    DeliveryStage,
    PlainMsg,
    RecordDir,
    RecordType,
    Status,
    TagCommand,
)
from cremind_tag.protocol.msgs import (  # noqa: E402
    CtrlAuth,
    CtrlChallenge,
    CtrlHello,
    RecCmd,
    RecFrameBegin,
    RecPlaneData,
    RecProgress,
    RecResult,
)

OUT = Path(__file__).resolve().parent / "src" / "conversation.h"

TAG_ID = 0x1A2B3C4D
SECRET = bytes(range(0x60, 0x80))
BATTERY_MV = 2950
CREDITS = 2
PLANE_DATA_MAX = 189


def nonce(tag: int) -> bytes:
    return bytes((tag + i) & 0xFF for i in range(16))


@dataclass
class Frame:
    name: str
    planes: int
    plane_flags: int
    plane_len: int
    data: list[bytes]
    digest: bytes


def load_frames() -> dict[str, Frame]:
    fx = json.loads((ROOT / "protocol" / "fixtures" / "render.json").read_text(encoding="utf-8"))
    frames = {}
    for sc in fx["scenarios"]:
        if "planes_hex" not in sc:
            continue
        data = [bytes.fromhex(p) for p in sc["planes_hex"]]
        digest = bytes.fromhex(sc["frame_digest"])
        assert hashlib.sha256(b"".join(data)).digest() == digest, sc["name"]
        p = sc["panel"]
        frames[sc["name"]] = Frame(sc["name"], p["planes"], p["plane_flags"], sc["plane_len"], data, digest)
    return frames


def white(frame: Frame) -> tuple[list[bytes], bytes]:
    """CMD{CLEAR}: plane 0 white, plane 1 not red (docs/protocol.md 4.4)."""
    p0 = b"\xff" if frame.plane_flags & 1 else b"\x00"
    p1 = b"\x00" if frame.plane_flags & 2 else b"\xff"
    planes = [(p0 if i == 0 else p1) * frame.plane_len for i in range(frame.planes)]
    return planes, hashlib.sha256(b"".join(planes)).digest()


@dataclass
class Init:
    """Persisted state before the scenario (the fake NVS)."""

    rec: tuple[int, int, int, bytes, int, int] | None = None  # epoch, revision, update_id, digest, status, state
    epoch_entry: int | None = None
    panel_ok: bool = True


@dataclass
class Conv:
    name: str
    frame: Frame
    init: Init = field(default_factory=Init)
    steps: list[tuple] = field(default_factory=list)

    # ---- link -----------------------------------------------------------------
    def link_up(self) -> None:
        self.b_ctrl = Fragmenter(TAG_CTRL_MSG_MAX)
        self.b_data = Fragmenter(TAG_RECORD_WIRE_MAX)
        self.t_ctrl = Fragmenter(TAG_CTRL_MSG_MAX)
        self.t_status = Fragmenter(TAG_RECORD_WIRE_MAX)
        self.sender: S.RecordSender | None = None
        self.t_sender: S.RecordSender | None = None
        self.epoch = 0
        self.steps.append(("LINK_UP",))

    def link_down(self) -> None:
        self.steps.append(("LINK_DOWN",))

    def reboot(self) -> None:
        """Power loss: the core re-initialises from the fake NVS."""
        self.steps.append(("REBOOT",))

    # ---- bridge -> tag ---------------------------------------------------------
    def write_ctrl(self, msg: bytes) -> None:
        for v in self.b_ctrl.split(msg):
            self.steps.append(("WRITE", "CTRL", v))

    def write_record(self, rtype: int, pt: bytes, poll: bool = True) -> None:
        assert self.sender is not None
        values = self.b_data.split(self.sender.seal(rtype, pt))
        for i, v in enumerate(values):
            self.steps.append(("WRITE" if poll or i < len(values) - 1 else "WRITE_NOPOLL", "DATA", v))

    def poll(self) -> None:
        self.steps.append(("POLL",))

    # ---- expected tag -> bridge ------------------------------------------------
    def expect_ctrl(self, msg: bytes) -> None:
        for v in self.t_ctrl.split(msg):
            self.steps.append(("EXPECT", "CTRL", v))

    def expect_credit(self, n: int = 1) -> None:
        for v in self.t_status.split(bytes([PlainMsg.CREDIT, n])):
            self.steps.append(("EXPECT", "STATUS", v))

    def expect_record(self, rtype: int, pt: bytes) -> None:
        assert self.t_sender is not None
        for v in self.t_status.split(self.t_sender.seal(rtype, pt)):
            self.steps.append(("EXPECT", "STATUS", v))

    def expect_result(self, update_id: int, revision: int, status: Status, digest: bytes = bytes(8),
                      refresh_ms: int = 0, flags: int = 0, epoch: int | None = None) -> None:
        epoch = self.epoch if epoch is None else epoch
        self.expect_record(RecordType.RESULT, RecResult(update_id, epoch, revision, status, digest[:8],
                                                        BATTERY_MV, refresh_ms, flags).pack())

    def expect_progress(self) -> None:
        self.expect_record(RecordType.PROGRESS, RecProgress(DeliveryStage.REFRESHING).pack())

    def expect_idle(self) -> None:
        self.steps.append(("IDLE",))

    def expect_closing(self) -> None:
        self.steps.append(("CLOSING",))

    def advance(self, ms: int) -> None:
        self.steps.append(("ADVANCE", ms))

    def refresh_done(self, status: Status = Status.OK) -> None:
        self.steps.append(("REFRESH_DONE", int(status)))

    # ---- composite -------------------------------------------------------------
    def handshake(self, epoch: int, tag_nonce: int, challenge: dict, good: bool = True) -> None:
        """HELLO / CHALLENGE / AUTH / AUTH_OK + CREDIT (or ERROR{AUTH_FAILED})."""
        hello = bytes([CtrlMsg.HELLO]) + CtrlHello(PROTO_VERSION, TAG_ID, epoch, nonce(0xA0 + tag_nonce)).pack()
        nonce_t = nonce(0x40 + tag_nonce)
        self.steps.append(("NONCE", nonce_t))
        self.write_ctrl(hello)
        ch = bytes([CtrlMsg.CHALLENGE]) + CtrlChallenge(
            PROTO_VERSION, nonce_t, challenge.get("stored_epoch", 0), challenge.get("displayed_rev", 0),
            challenge.get("last_status", 0), BATTERY_MV, challenge.get("flags", 0)).pack()
        self.expect_ctrl(ch)
        k_epoch = S.derive_k_epoch(SECRET, TAG_ID, epoch)
        th = S.transcript_hash(hello, ch)
        mac_b = S.mac_b(k_epoch, th)
        if not good:
            mac_b = bytes([mac_b[0] ^ 0x01]) + mac_b[1:]
        self.write_ctrl(bytes([CtrlMsg.AUTH]) + CtrlAuth(mac_b).pack())
        if not good:
            self.expect_ctrl(bytes([CtrlMsg.ERROR, Status.AUTH_FAILED]))
            self.expect_closing()
            return
        self.expect_ctrl(bytes([CtrlMsg.AUTH_OK]) + S.mac_t(k_epoch, th, mac_b))
        k_b2t, k_t2b = S.session_keys(k_epoch, th)
        self.sender = S.RecordSender(k_b2t, RecordDir.B2T)
        self.t_sender = S.RecordSender(k_t2b, RecordDir.T2B)
        self.epoch = epoch
        self.expect_credit(CREDITS)

    def frame_begin(self, revision: int, update_id: int, digest: bytes | None = None,
                    planes: int | None = None, plane_len: int | None = None) -> None:
        f = self.frame
        self.write_record(RecordType.FRAME_BEGIN, RecFrameBegin(
            revision, update_id, f.digest if digest is None else digest, planes or f.planes,
            plane_len or f.plane_len).pack())

    def plane_data(self, planes: list[bytes] | None = None, stop_after: int | None = None,
                   batch: bool = False) -> int:
        """Stream the planes; each record is answered by CREDIT{1}. With batch,
        records go out two at a time before the tag is polled (both buffers)."""
        sent = 0
        pending = 0
        for p, data in enumerate(planes or self.frame.data):
            for off in range(0, len(data), PLANE_DATA_MAX):
                if stop_after is not None and sent == stop_after:
                    return sent
                chunk = data[off : off + PLANE_DATA_MAX]
                self.write_record(RecordType.PLANE_DATA, RecPlaneData(p, off, chunk).pack(),
                                  poll=not batch or pending == 1)
                sent += 1
                pending += 1
                if not batch or pending == 2:
                    for _ in range(pending):
                        self.expect_credit()
                    pending = 0
        if pending:
            self.poll()
            for _ in range(pending):
                self.expect_credit()
        return sent

    def frame_end(self) -> None:
        self.write_record(RecordType.FRAME_END, b"")

    def cmd(self, cmd: int, update_id: int) -> None:
        self.write_record(RecordType.CMD, RecCmd(cmd, update_id).pack())


# ---------------------------------------------------------------------------
# Scenarios (docs/tag-firmware.md "Tests")


def scenarios(frames: dict[str, Frame]) -> list[Conv]:
    bw = frames["thick_lines"]  # 64 x 48, one plane, 384 bytes: three PLANE_DATA
    bwr = frames["glyph_bearings"]  # 96 x 40, two planes, 480 bytes each
    other = hashlib.sha256(b"another frame").digest()
    out = []

    # Full happy path; two records in flight exercise both buffers.
    c = Conv("happy", bw)
    c.link_up()
    c.handshake(1, 1, {})
    c.frame_begin(1, 100)
    c.expect_credit()
    c.plane_data(batch=True)
    c.frame_end()
    c.expect_progress()
    c.expect_idle()
    c.advance(3900)
    c.refresh_done()
    c.expect_result(100, 1, Status.OK, bw.digest, 3900)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    # Two planes (black/white/red).
    c = Conv("two_planes", bwr)
    c.link_up()
    c.handshake(1, 2, {})
    c.frame_begin(7, 70)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_progress()
    c.advance(15000)
    c.refresh_done()
    c.expect_result(70, 7, Status.OK, bwr.digest, 15000)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    displayed = (1, 5, 100, bw.digest, Status.OK, 0)

    # A displayed revision again: the stored ACK with the duplicate flag.
    c = Conv("duplicate", bw, Init(rec=displayed, epoch_entry=1))
    c.link_up()
    c.handshake(1, 3, {"stored_epoch": 1, "displayed_rev": 5})
    c.frame_begin(5, 101)
    c.expect_result(100, 5, Status.OK, bw.digest, 0, flags=1)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    c = Conv("stale_revision", bw, Init(rec=displayed, epoch_entry=1))
    c.link_up()
    c.handshake(1, 4, {"stored_epoch": 1, "displayed_rev": 5})
    c.frame_begin(4, 102)
    c.expect_result(102, 4, Status.STALE_REVISION)
    c.expect_credit()
    out.append(c)

    c = Conv("revision_conflict", bw, Init(rec=displayed, epoch_entry=1))
    c.link_up()
    c.handshake(1, 5, {"stored_epoch": 1, "displayed_rev": 5})
    c.frame_begin(5, 103, digest=other)
    c.expect_result(103, 5, Status.REVISION_CONFLICT)
    c.expect_credit()
    out.append(c)

    # The planes do not hash to the announced digest: no refresh.
    c = Conv("digest_mismatch", bw)
    c.link_up()
    c.handshake(1, 6, {})
    c.frame_begin(2, 104, digest=other)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_result(104, 2, Status.DIGEST_MISMATCH)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    # A wrong mac_b: ERROR{AUTH_FAILED} and the session ends.
    c = Conv("auth_failed", bw)
    c.link_up()
    c.handshake(1, 7, {}, good=False)
    out.append(c)

    # Disconnect after one PLANE_DATA; the next session restarts at offset 0.
    c = Conv("restart_from_zero", bw)
    c.link_up()
    c.handshake(1, 8, {})
    c.frame_begin(3, 105)
    c.expect_credit()
    c.plane_data(stop_after=1)
    c.link_down()
    c.link_up()
    c.handshake(1, 9, {"stored_epoch": 1})
    c.frame_begin(3, 105)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_progress()
    c.advance(4000)
    c.refresh_done()
    c.expect_result(105, 3, Status.OK, bw.digest, 4000)
    c.expect_credit()
    out.append(c)

    # Power loss after REFRESH_INTENT: the boot rule reports the unknown state,
    # the bridge re-delivers the same revision and the tag refreshes again.
    c = Conv("power_loss_recovery", bw)
    c.link_up()
    c.handshake(1, 10, {})
    c.frame_begin(4, 106)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_progress()
    c.reboot()
    c.link_up()
    c.handshake(1, 11, {"stored_epoch": 1, "displayed_rev": 4,
                        "last_status": Status.DISPLAY_STATE_UNKNOWN, "flags": 0x01})
    c.frame_begin(4, 107)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_progress()
    c.advance(3000)
    c.refresh_done()
    c.expect_result(107, 4, Status.OK, bw.digest, 3000)
    c.expect_credit()
    out.append(c)

    # A newer epoch is stored only once AUTH verified (5.4).
    c = Conv("epoch_after_auth", bw, Init(epoch_entry=3))
    c.link_up()
    c.handshake(4, 12, {"stored_epoch": 3}, good=False)
    c.link_down()
    c.link_up()
    c.handshake(4, 13, {"stored_epoch": 3})
    c.link_down()
    c.link_up()
    hello = bytes([CtrlMsg.HELLO]) + CtrlHello(PROTO_VERSION, TAG_ID, 3, nonce(0xA0 + 14)).pack()
    c.steps.append(("NONCE", nonce(0x40 + 14)))
    c.write_ctrl(hello)
    c.expect_ctrl(bytes([CtrlMsg.ERROR, Status.STALE_EPOCH]))
    c.expect_closing()
    out.append(c)

    # Companion-level commands and an unverified SLEEP: UNSUPPORTED (10).
    c = Conv("unsupported_commands", bw)
    c.link_up()
    c.handshake(2, 15, {})
    c.cmd(TagCommand.IDENTIFY, 7)
    c.expect_result(7, 0, Status.UNSUPPORTED)
    c.expect_credit()
    c.cmd(TagCommand.REFRESH, 8)
    c.expect_result(8, 0, Status.UNSUPPORTED)
    c.expect_credit()
    c.cmd(TagCommand.SLEEP, 9)
    c.expect_result(9, 0, Status.UNSUPPORTED)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    # CMD{CLEAR}: white planes, refresh, revision 0 with the digest of white.
    _, white_digest = white(bwr)
    c = Conv("clear", bwr, Init(rec=(2, 9, 90, bwr.digest, Status.OK, 0), epoch_entry=2))
    c.link_up()
    c.handshake(2, 16, {"stored_epoch": 2, "displayed_rev": 9})
    c.cmd(TagCommand.CLEAR, 11)
    c.expect_progress()
    c.advance(12000)
    c.refresh_done()
    c.expect_result(11, 0, Status.OK, white_digest, 12000)
    c.expect_credit()
    # Revision 1 is newer than the cleared revision 0.
    c.frame_begin(1, 12)
    c.expect_credit()
    out.append(c)

    # Panel id 255 (UNVERIFIED): frames and CLEAR are refused, the panel untouched.
    c = Conv("unverified_panel", bw, Init(panel_ok=False))
    c.link_up()
    c.handshake(1, 17, {})
    c.frame_begin(1, 13)
    c.expect_result(13, 1, Status.UNSUPPORTED)
    c.expect_credit()
    c.cmd(TagCommand.CLEAR, 14)
    c.expect_result(14, 0, Status.UNSUPPORTED)
    c.expect_credit()
    c.expect_idle()
    out.append(c)

    # A refresh whose BUSY never releases: REFRESH_TIMEOUT, REFRESH_INTENT kept.
    c = Conv("refresh_timeout", bw)
    c.link_up()
    c.handshake(1, 18, {})
    c.frame_begin(6, 108)
    c.expect_credit()
    c.plane_data()
    c.frame_end()
    c.expect_progress()
    c.advance(10000)
    c.refresh_done(Status.REFRESH_TIMEOUT)
    c.expect_result(108, 6, Status.REFRESH_TIMEOUT, refresh_ms=10000)
    c.expect_credit()
    c.link_down()
    c.link_up()
    c.handshake(1, 19, {"stored_epoch": 1, "displayed_rev": 6, "last_status": Status.REFRESH_TIMEOUT,
                        "flags": 0x01})
    out.append(c)
    return out


# ---------------------------------------------------------------------------
# C output

OPS = {"LINK_UP": 0, "LINK_DOWN": 1, "REBOOT": 2, "NONCE": 3, "WRITE": 4, "WRITE_NOPOLL": 5, "POLL": 6,
       "EXPECT": 7, "IDLE": 8, "CLOSING": 9, "ADVANCE": 10, "REFRESH_DONE": 11}
CHRS = {"CTRL": 0, "DATA": 1, "STATUS": 2}


def c_bytes(data: bytes) -> str:
    return "(const uint8_t[]){" + ", ".join(f"0x{b:02x}" for b in data) + "}" if data else "NULL"


def emit(convs: list[Conv], frames: dict[str, Frame]) -> str:
    lines = [
        "/* Generated by tests/ztest/tag_core/gen_conversation.py from the companion's",
        " * protocol reference and protocol/fixtures/render.json. Do not edit. */",
        "#ifndef CONVERSATION_H_",
        "#define CONVERSATION_H_",
        "",
        "#include <stdbool.h>",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        f"#define CONV_TAG_ID 0x{TAG_ID:08x}u",
        "static const uint8_t conv_secret[32] = {" + ", ".join(f"0x{b:02x}" for b in SECRET) + "};",
        f"#define CONV_BATTERY_MV {BATTERY_MV}u",
        "",
        "enum conv_op {",
        *(f"\tCONV_{k} = {v}," for k, v in OPS.items()),
        "};",
        "",
        "struct conv_step {",
        "\tuint8_t op;",
        "\tuint8_t chr;       /* enum tag_chr */",
        "\tuint16_t len;",
        "\tuint32_t arg;      /* ADVANCE ms, REFRESH_DONE status */",
        "\tconst uint8_t *data;",
        "};",
        "",
        "struct conv_frame {",
        "\tuint8_t planes;",
        "\tuint8_t plane_flags;",
        "\tuint16_t plane_len;",
        "\tconst uint8_t *data[2];",
        "\tconst uint8_t *digest;",
        "};",
        "",
        "struct conv {",
        "\tconst char *name;",
        "\tconst struct conv_frame *frame;",
        "\tbool has_rec;",
        "\tuint32_t rec_epoch;",
        "\tuint32_t rec_revision;",
        "\tuint64_t rec_update_id;",
        "\tconst uint8_t *rec_digest;",
        "\tuint8_t rec_status;",
        "\tuint8_t rec_state;",
        "\tbool has_epoch;",
        "\tuint32_t epoch_entry;",
        "\tbool panel_ok;",
        "\tconst struct conv_step *steps;",
        "\tsize_t count;",
        "};",
        "",
    ]
    used = {c.frame.name for c in convs}
    for name in sorted(used):
        f = frames[name]
        data = [c_bytes(p) for p in f.data] + ["NULL"] * (2 - len(f.data))
        lines.append(f"static const struct conv_frame conv_frame_{name} = {{{f.planes}u, {f.plane_flags}u, "
                     f"{f.plane_len}u, {{{data[0]}, {data[1]}}}, {c_bytes(f.digest)}}};")
    lines.append("")
    for c in convs:
        lines.append(f"static const struct conv_step conv_{c.name}_steps[] = {{")
        for s in c.steps:
            op = OPS[s[0]]
            if s[0] in ("WRITE", "WRITE_NOPOLL", "EXPECT"):
                lines.append(f"\t{{{op}, {CHRS[s[1]]}, {len(s[2])}u, 0u, {c_bytes(s[2])}}},")
            elif s[0] == "NONCE":
                lines.append(f"\t{{{op}, 0, {len(s[1])}u, 0u, {c_bytes(s[1])}}},")
            elif s[0] in ("ADVANCE", "REFRESH_DONE"):
                lines.append(f"\t{{{op}, 0, 0u, {s[1]}u, NULL}},")
            else:
                lines.append(f"\t{{{op}, 0, 0u, 0u, NULL}},")
        lines.append("};")
        i = c.init
        if i.rec is None:
            rec = "false, 0u, 0u, 0u, NULL, 0u, 0u"
        else:
            e, r, u, d, st, state = i.rec
            rec = f"true, {e}u, {r}u, {u}u, {c_bytes(d)}, {int(st)}u, {state}u"
        ep = "false, 0u" if i.epoch_entry is None else f"true, {i.epoch_entry}u"
        lines.append(f"static const struct conv conv_{c.name} = {{\"{c.name}\", &conv_frame_{c.frame.name}, "
                     f"{rec}, {ep}, {str(i.panel_ok).lower()}, conv_{c.name}_steps, "
                     f"sizeof(conv_{c.name}_steps) / sizeof(conv_{c.name}_steps[0])}};")
        lines.append("")
    lines += ["#endif /* CONVERSATION_H_ */", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="fail when src/conversation.h is stale")
    args = ap.parse_args()
    frames = load_frames()
    text = emit(scenarios(frames), frames)
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run {Path(__file__).relative_to(ROOT)}", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
