#!/usr/bin/env python3
"""Drive the native_sim gateway (apps/gateway/tests/interop) with the companion's
real GatewayClient over the gateway's native PTY UART.

Usage (inside the NCS toolchain container, see README.md)::

    PYTHONPATH=/work/companion/src python3 interop.py /build/gw-interop/zephyr/zephyr.exe

Every scenario talks to the same gateway process through pyserial on the PTY,
exactly as the companion talks to a USB/UART gateway. Exit status 0 = every
scenario passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Awaitable, Callable

from cremind_tag.gateway import (
    AssignResult,
    BridgeInfoEvent,
    GatewayClient,
    GatewayEvent,
    NodeConfigured,
    NodeRemoved,
    Provisioned,
    ResultEvent,
    SessionStarted,
    StageEvent,
    StatusError,
    UnprovBeacon,
    matches,
)
from cremind_tag.protocol.ids import RESULT_FLAG_DUPLICATE, DeliveryStage, NodeRole, SerialMsg, Status

BRIDGE = 0x0002
PACK = bytes(8)
KEY = bytes(range(16))


class Check(Exception):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise Check(what)


async def until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise Check(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def counters(client: GatewayClient) -> dict[str, int]:
    return await client.get_counters()


# ---------------------------------------------------------------------------


async def s_hello(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        h = c.hello_info
        assert h is not None
        check(h.proto == 1, f"proto {h.proto}")
        check(h.caps.role == NodeRole.GATEWAY, f"role {h.caps.role}")
        check(h.caps.credits == 4 and h.caps.max_frame == 4096, f"caps {h.caps}")
        check(h.caps.max_bridges == 5 and h.caps.max_tags == 20, f"caps {h.caps}")
        check(h.fw == "0.1.0" and h.build == "interop", f"fw {h.fw} build {h.build}")
        check(h.boot_id != 0, "boot_id")
        info = await c.info()
        check(info.boot_id == h.boot_id, "INFO boot_id")
        check(info.counters.get("hellos", 0) >= 1, "hellos counter")
        check(await c.ping() >= 0, "PING")
        nodes = await c.list_nodes()
        check([n.addr for n in nodes] == [2, 3], f"nodes {[n.addr for n in nodes]}")
        check(nodes[0].name == "hall" and nodes[0].configured, f"node {nodes[0]}")
        with c.subscribe(BridgeInfoEvent) as sub:
            await c.get_inventory()
            ev = await sub.get(5)
            assert isinstance(ev, BridgeInfoEvent)
            check(ev.info.addr in (2, 3) and ev.info.fw == "0.1.0", f"bridge info {ev.info}")
        inventory = await c.get_inventory()
        check(all(i.fw == "0.1.0" for i in inventory), "inventory carries CAPS")
        try:
            await c._checked(SerialMsg.FONT_STATUS)
            raise Check("FONT_STATUS answered OK")
        except StatusError as exc:
            check(exc.status == Status.UNSUPPORTED, f"FONT_STATUS {exc.status}")
        notes.append(f"boot_id {h.boot_id:08x}, {len(nodes)} bridges, inventory {len(inventory)} items")


async def s_credits(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        before = await counters(c)
        t0 = time.monotonic()
        results = await asyncio.gather(*(c.ping() for _ in range(60)), *(c.info() for _ in range(20)))
        dt = time.monotonic() - t0
        after = await counters(c)
        check(len(results) == 80, "80 answers")
        for key in ("credit_violations", "overruns"):
            check(after[key] == before[key], f"{key} {before[key]} -> {after[key]}")
        check(after["hellos"] == before["hellos"], "no resynchronisation")
        check(c.link.stats["timeouts"] == 0 and c.link.stats["resyncs"] == 1, f"link {c.link.stats}")
        check(c.link.credits >= 1, "host credits left")
        notes.append(f"80 concurrent requests in {dt * 1000:.0f} ms, 0 credit violations, 0 overruns, "
                     f"grant PINGs {c.link.stats['grant_pings']}")


async def s_idempotent(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        before = await counters(c)
        op = c.new_op_id()
        a1 = await c.assign_tag(BRIDGE, 0x1111, 1, KEY, op_id=op)
        a2 = await c.assign_tag(BRIDGE, 0x1111, 1, KEY, op_id=op)
        check(a1.status == Status.ACCEPTED and not a1.duplicate, f"first {a1}")
        check(a2.status == Status.ACCEPTED and a2.duplicate, f"retry {a2}")
        update_id = c.new_op_id()
        results: list[ResultEvent] = []
        with c.subscribe(ResultEvent) as sub:
            op = c.new_op_id()
            layout = os.urandom(300)
            d1 = await c.deliver_layout(bridge=BRIDGE, tag_id=0x0A0B0C01, epoch=1, revision=1,
                                        update_id=update_id, fontpack_id=PACK, layout=layout, op_id=op)
            d2 = await c.deliver_layout(bridge=BRIDGE, tag_id=0x0A0B0C01, epoch=1, revision=1,
                                        update_id=update_id, fontpack_id=PACK, layout=layout, op_id=op)
            check(d1.status == Status.ACCEPTED and not d1.duplicate, f"first delivery {d1}")
            check(d2.status == Status.ACCEPTED and d2.duplicate, f"retried delivery {d2}")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    ev = await sub.get(max(0.05, deadline - time.monotonic()))
                except TimeoutError:
                    break
                if isinstance(ev, ResultEvent) and ev.update_id == update_id:
                    results.append(ev)
        check(len(results) == 1, f"{len(results)} results for one update_id")
        after = await counters(c)
        check(after["duplicate_ops"] - before["duplicate_ops"] == 2, "duplicate_ops +2")
        check(after["deliveries_accepted"] - before["deliveries_accepted"] == 1, "one delivery queued")
        notes.append("same op_id twice: ACCEPTED + detail DUPLICATE, no new work, one EVT_RESULT")


async def s_retained(url: str, notes: list[str]) -> None:
    # (a) ACK only after the handler succeeded.
    calls: list[AssignResult] = []
    gate = asyncio.Event()

    async def failing(ev: GatewayEvent) -> None:
        if isinstance(ev, AssignResult):
            calls.append(ev)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            await gate.wait()

    c = GatewayClient(url, reconnect=False, handler_backoff=(0.05, 0.05))
    c.add_event_handler(failing)
    async with c:
        await c.assign_tag(BRIDGE, 0x2222, 1, KEY)
        await until(lambda: len(calls) >= 2, 10, "the handler's retry")
        held = (await counters(c))["retained"]
        check(held >= 1, "the gateway keeps the unacknowledged event")
        check(c.stats["acks"] == 0 and c.stats["handler_failures"] == 1, f"client {c.stats}")
        gate.set()
        await c.drain_events()
        await until(lambda: c.stats["acks"] >= 1, 5, "EVENT_ACK")
        released = (await counters(c))["retained"]
        check(released == 0, f"retained after ACK: {released}")
    # (b) re-sent after HELLO to the next session until acknowledged.
    async with GatewayClient(url, reconnect=False) as observer:  # no handler: never ACKs
        with observer.expect(matches(AssignResult)) as waiter:
            await observer.assign_tag(BRIDGE, 0x3333, 2, KEY)
            first = await waiter.wait(10)
        check((await counters(observer))["retained"] == 1, "one retained event")
    assert isinstance(first, AssignResult)
    seen: list[GatewayEvent] = []

    async def record(ev: GatewayEvent) -> None:
        seen.append(ev)

    c2 = GatewayClient(url, reconnect=False)
    c2.add_event_handler(record)
    async with c2:
        await until(lambda: any(isinstance(e, AssignResult) for e in seen), 10, "the re-sent event")
        await c2.drain_events()
        resent = next(e for e in seen if isinstance(e, AssignResult))
        check((resent.op_id, resent.seq, resent.boot_id) == (first.op_id, first.seq, first.boot_id),
              "same op_id, seq and boot_id")
        check(isinstance(seen[0], SessionStarted) and not seen[0].boot_changed, "SessionStarted first")
        await until(lambda: c2.stats["acks"] >= 1, 5, "EVENT_ACK")
        check((await counters(c2))["retained"] == 0, "released after the handler")
    notes.append(f"handler failure kept seq {calls[0].seq}; unacknowledged seq {first.seq} re-sent after HELLO")


async def deliver_and_wait(c: GatewayClient, layout: bytes, tag_id: int, revision: int,
                           timeout: float = 15) -> tuple[ResultEvent, list[int], float]:
    update_id = c.new_op_id()
    stages: list[int] = []
    t0 = time.monotonic()
    with c.subscribe(StageEvent) as sub, c.expect(matches(ResultEvent, update_id=update_id)) as waiter:
        while True:
            ack = await c.deliver_layout(bridge=BRIDGE, tag_id=tag_id, epoch=1, revision=revision,
                                         update_id=update_id, fontpack_id=PACK, layout=layout)
            if not ack.busy:
                break
            await asyncio.sleep(0.2)  # queue full: retry later (a new op_id)
        check(ack.status == Status.ACCEPTED, f"DELIVER_LAYOUT {ack}")
        result = await waiter.wait(timeout)
        while True:
            try:
                ev = await sub.get(0.01)
            except TimeoutError:
                break
            if isinstance(ev, StageEvent) and ev.update_id == update_id:
                stages.append(int(ev.stage))
    assert isinstance(result, ResultEvent)
    return result, stages, time.monotonic() - t0


async def s_delivery(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        layout = os.urandom(1000)
        result, stages, dt = await deliver_and_wait(c, layout, 0x0A0B0C0D, 1)
        check(result.status == Status.OK, f"status {result.status}")
        check(result.digest == hashlib.sha256(layout).digest()[:8], "digest of the reassembled layout")
        check(result.bridge == BRIDGE and result.battery_mv == 2950, f"result {result}")
        check(result.timing.mesh_ms > 0 and result.timing.refresh_ms == 300, f"timing {result.timing}")
        check(stages == [DeliveryStage.BRIDGE_RECEIVED, DeliveryStage.TRANSFERRING,
                         DeliveryStage.REFRESHING], f"stages {stages}")
        # EVT_RESULT carries the bridge's flags and the tag's stored epoch.
        check(result.flags == 0 and result.stored_epoch == 1 and not result.duplicate, f"raw {result.raw}")
        await asyncio.sleep(1.5)  # the bridge's re-sends (none: RESULT_ACK arrived)
        notes.append(f"1000 B layout: stages {stages} then EVT_RESULT OK in {dt * 1000:.0f} ms "
                     f"(mesh_ms {result.timing.mesh_ms})")
        # Chunk 1 lost once at the bridge: LAYOUT_STATUS INCOMPLETE, resent.
        before = await counters(c)
        layout = os.urandom(700)
        result, _, _ = await deliver_and_wait(c, layout, 0xDEAD0001, 2)
        after = await counters(c)
        check(result.status == Status.OK, f"INCOMPLETE path status {result.status}")
        check(after["chunks_resent"] - before["chunks_resent"] == 1, "exactly chunk 1 resent")
        # The largest layout the companion sends.
        layout = os.urandom(4000)
        result, _, dt = await deliver_and_wait(c, layout, 0x0A0B0C0E, 3)
        check(result.status == Status.OK and result.digest == hashlib.sha256(layout).digest()[:8],
              "4000 B layout")
        # The tag answered with its stored ACK: flags bit0 reaches the companion.
        result, _, _ = await deliver_and_wait(c, os.urandom(300), 0xDEAD0003, 4)
        check(result.status == Status.OK and result.flags == RESULT_FLAG_DUPLICATE and result.duplicate
              and result.stored_epoch == 1, f"stored ACK flag {result.raw}")
        notes.append(f"INCOMPLETE round resent 1 chunk; 4000 B layout (27 chunks) OK in {dt * 1000:.0f} ms")
        # Throughput: 12 deliveries queued back to back.
        t0 = time.monotonic()
        jobs = [deliver_and_wait(c, os.urandom(1000), 0x0B000000 + i, 1) for i in range(12)]
        done = await asyncio.gather(*jobs)
        dt = time.monotonic() - t0
        check(all(r.status == Status.OK for r, _, _ in done), "12 deliveries OK")
        final = await counters(c)
        check(final["results"] >= 15, "results counted")
        notes.append(f"12 x 1000 B concurrently (BUSY retried): {dt:.2f} s, "
                     f"busy {final['busy']}, mesh_busy {final['mesh_busy']}")


async def s_lost_status(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        before = await counters(c)
        layout = os.urandom(500)
        with c.subscribe(ResultEvent) as results:
            result, stages, dt = await deliver_and_wait(c, layout, 0xDEAD0002, 1, timeout=25)
            await asyncio.sleep(1.5)
            count = 0
            while True:
                try:
                    ev = await results.get(0.01)
                except TimeoutError:
                    break
                count += isinstance(ev, ResultEvent) and ev.update_id == result.update_id
        after = await counters(c)
        check(result.status == Status.OK, f"status {result.status}")
        check(count == 1, f"{count} EVT_RESULTs for one update_id")
        check(after["commit_resends"] - before["commit_resends"] == 1, "one re-sent commit")
        check(stages == [DeliveryStage.BRIDGE_RECEIVED, DeliveryStage.TRANSFERRING,
                         DeliveryStage.REFRESHING], f"stages {stages}")
        check(dt >= 10.0, f"answered after {dt:.1f} s")
        notes.append(f"LAYOUT_STATUS OK lost: re-commit after 10 s answered DUPLICATE = OK, "
                     f"one EVT_RESULT OK after {dt:.1f} s")


async def s_provision(url: str, notes: list[str]) -> None:
    async with GatewayClient(url, reconnect=False) as c:
        inventory = {i.addr: i for i in await c.get_inventory()}
        assigned = {(a.tag_id, a.epoch) for a in inventory[BRIDGE].assigned}
        check({(0x1111, 1), (0x2222, 1), (0x3333, 2)} <= assigned, f"inventory assigned {assigned}")
        # The bridge's own count (CAPS_STATUS.assigned) rides in the caps map of EVT_BRIDGE_INFO and the
        # inventory, beside the gateway's list: here both know the same tags.
        with c.subscribe(BridgeInfoEvent) as sub:
            await c.get_inventory()
            ev = await sub.get(5)
            while isinstance(ev, BridgeInfoEvent) and ev.info.addr != BRIDGE:
                ev = await sub.get(5)
        assert isinstance(ev, BridgeInfoEvent)
        count = ev.info.caps.assigned_count
        check(count == len({a.tag_id for a in ev.info.assigned}) and count >= 3,
              f"caps assigned_count {count}, gateway list {ev.info.assigned}")
        inventory = {i.addr: i for i in await c.get_inventory()}
        check(inventory[BRIDGE].caps.assigned_count == count, f"inventory caps {inventory[BRIDGE].caps}")
        notes.append(f"the bridge reports {count} assigned tags in its caps (the gateway lists "
                     f"{len(ev.info.assigned)})")
        with c.subscribe(UnprovBeacon) as sub:
            ack = await c.scan_unprov(3, bytes([0xC0]))
            check(ack.status == Status.OK, f"SCAN_UNPROV {ack}")
            beacon = await sub.get(3)
        assert isinstance(beacon, UnprovBeacon)
        check(beacon.uuid[:2] == bytes([0xC0, 0xFF]) and beacon.rssi == -55, f"beacon {beacon}")
        op = c.new_op_id()
        with c.expect(matches(Provisioned, op_id=op)) as waiter:
            ack = await c.provision(beacon.uuid, "desk", op_id=op)
            check(ack.status == Status.ACCEPTED, f"PROVISION {ack}")
            busy = await c.provision(bytes(16))
            check(busy.status == Status.PROVISIONING_ACTIVE, f"second PROVISION {busy}")
            prov = await waiter.wait(10)
        assert isinstance(prov, Provisioned)
        check(prov.status == Status.OK and prov.addr == 4, f"provisioned {prov}")
        op = c.new_op_id()
        with c.expect(matches(NodeConfigured, op_id=op)) as waiter:
            await c.configure_node(4, op_id=op)
            conf = await waiter.wait(15)
        assert isinstance(conf, NodeConfigured)
        check(conf.status == Status.OK, f"configured {conf}")
        nodes = {n.addr: n for n in await c.list_nodes()}
        check(4 in nodes and nodes[4].name == "desk" and nodes[4].configured, f"nodes {nodes}")
        op = c.new_op_id()
        with c.expect(matches(NodeRemoved, op_id=op)) as waiter:
            await c.remove_node(4, op_id=op)
            removed = await waiter.wait(15)
        assert isinstance(removed, NodeRemoved)
        check(removed.status == Status.OK, f"removed {removed}")
        check([n.addr for n in await c.list_nodes()] == [2, 3], "back to two bridges")
        await c.drain_events()
        notes.append("scan -> beacon, PROVISION -> 0x0004 'desk', CONFIGURE_NODE OK, REMOVE_NODE OK")


async def s_reboot(url: str, notes: list[str]) -> None:
    sessions: list[SessionStarted] = []

    async def handler(ev: GatewayEvent) -> None:
        if isinstance(ev, SessionStarted):
            sessions.append(ev)

    c = GatewayClient(url, reconnect=True, request_timeout=1.0)
    c.add_event_handler(handler)
    async with c:
        old = c.boot_id
        ack = await c.reboot()
        check(ack.status == Status.OK, f"REBOOT {ack}")
        await asyncio.sleep(0.3)
        await c.ping()  # the new boot ignores it until HELLO: timeout, resync, new session
        await until(lambda: len(sessions) >= 2, 10, "the new session")
        check(sessions[-1].boot_changed and sessions[-1].previous_boot_id == old, "boot_changed")
        check(c.boot_id != old, "new boot_id")
        check([n.addr for n in await c.list_nodes()] == [2, 3], "the CDB survived")
        notes.append(f"boot_id {old:08x} -> {c.boot_id:08x}, SessionStarted.boot_changed")


SCENARIOS: list[tuple[str, Callable[[str, list[str]], Awaitable[None]]]] = [
    ("HELLO, INFO, PING, LIST_NODES, GET_INVENTORY", s_hello),
    ("credits under load", s_credits),
    ("idempotent retries (op_id)", s_idempotent),
    ("retained events: ACK after handler, re-sent after HELLO", s_retained),
    ("DELIVER_LAYOUT -> mesh -> EVT_RESULT OK", s_delivery),
    ("lost LAYOUT_STATUS OK -> DUPLICATE -> one EVT_RESULT", s_lost_status),
    ("provisioning, configuration, removal", s_provision),
    ("REBOOT -> new boot_id", s_reboot),
]


def start_gateway(exe: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    for line in proc.stdout:
        sys.stdout.write("  gateway| " + line)
        m = re.search(r"pseudotty: (/dev/pts/\d+)", line)
        if m:
            return proc, m.group(1)
        if time.monotonic() > deadline:
            break
    proc.kill()
    raise SystemExit("the gateway did not report its PTY")


async def main(exe: str) -> int:
    proc, url = start_gateway(exe)

    def pump_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write("  gateway| " + line)

    import threading

    threading.Thread(target=pump_output, daemon=True).start()
    await asyncio.sleep(0.5)
    print(f"gateway on {url}")
    rows: list[tuple[str, str, str, float]] = []
    try:
        for name, fn in SCENARIOS:
            notes: list[str] = []
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(fn(url, notes), 120)
                rows.append((name, "PASS", "; ".join(notes), time.monotonic() - t0))
            except Exception as exc:  # noqa: BLE001 - report every failure
                detail = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                rows.append((name, "FAIL", detail, time.monotonic() - t0))
            print(f"{rows[-1][1]} {name} ({rows[-1][3]:.1f} s): {rows[-1][2]}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n| Scenario | Result | Time | Details |\n|---|---|---|---|")
    for name, res, detail, dt in rows:
        print(f"| {name} | {res} | {dt:.1f} s | {detail} |")
    failed = sum(1 for r in rows if r[1] != "PASS")
    print(f"\n{len(rows) - failed}/{len(rows)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1])))
