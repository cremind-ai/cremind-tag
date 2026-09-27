"""`cremind-tag mesh` — provision bridges into the Zephyr mesh and inspect the network.

Provisioning is PB-ADV without OOB authentication (docs/security.md): provision
only devices whose UUID you selected from `mesh scan`, in a controlled
environment. Outcomes arrive as retained gateway events; these commands wait for
them and record the bridges in the local inventory. They do not acknowledge the
events, so a running daemon still receives them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import typer

from cremind_tag.cli._hardware import (
    JSON_HELP,
    URL_HELP,
    connect_gateway,
    console,
    fail,
    gateway_url,
    load,
    open_db,
    print_json,
    record_gateway,
    run,
    status_text,
    table,
)

if TYPE_CHECKING:
    from cremind_tag.gateway import GatewayClient
    from cremind_tag.store import Database

app = typer.Typer(name="mesh", help="Provision and configure bridges in the Zephyr mesh.", no_args_is_help=True)

TIMEOUT_HELP = "Seconds to wait for the gateway's outcome event."


def _parse_uuid(text: str) -> bytes:
    from cremind_tag.store import normalize_uuid

    try:
        return bytes.fromhex(normalize_uuid(text))
    except ValueError as exc:
        fail(str(exc))


def _parse_addr(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        fail(f"not a mesh address: {text!r}")
    if not 1 <= value <= 0x7FFF:
        fail(f"mesh unicast address out of range: {text}")
    return value


@app.command()
def scan(url: str | None = typer.Option(None, "--url", help=URL_HELP),
         duration: int = typer.Option(10, "--duration", "-d", min=1, max=255, help="Scan time in seconds."),
         uuid_filter: str = typer.Option("", "--filter", help="Hex prefix of the device UUIDs to report."),
         as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Listen for unprovisioned bridges (EVT_UNPROV_BEACON)."""
    port = gateway_url(url)
    prefix = bytes.fromhex(uuid_filter) if uuid_filter else None

    async def main() -> dict[bytes, tuple[int, int]]:
        from cremind_tag.gateway import UnprovBeacon

        found: dict[bytes, tuple[int, int]] = {}
        async with connect_gateway(port) as client:
            with client.subscribe(UnprovBeacon) as beacons:
                ack = await client.scan_unprov(duration, prefix)
                if not ack.ok:
                    fail(f"SCAN_UNPROV answered {ack.status}")
                deadline = time.monotonic() + duration + 1.0
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        beacon = await beacons.get(remaining)
                    except TimeoutError:
                        break
                    assert isinstance(beacon, UnprovBeacon)
                    best = found.get(beacon.uuid)
                    if best is None or beacon.rssi > best[0]:
                        found[beacon.uuid] = (beacon.rssi, beacon.oob)
        return found

    found = run(main)
    if as_json:
        print_json([{"uuid": u.hex(), "rssi": r, "oob": o} for u, (r, o) in found.items()])
        return
    if not found:
        console.print("No unprovisioned bridges heard.")
        return
    t = table("Unprovisioned devices", "UUID", "RSSI", "OOB")
    for u, (rssi, oob) in sorted(found.items(), key=lambda kv: -kv[1][0]):
        t.add_row(u.hex(), str(rssi), f"{oob:#06x}")
    console.print(t)


async def _configure(client: GatewayClient, db: Database, addr: int, relay: bool, ttl: int, timeout: float) -> Any:
    from cremind_tag.gateway import NodeConfigured, matches
    from cremind_tag.protocol.ids import Status

    op = client.new_op_id()
    with client.expect(matches(NodeConfigured, op_id=op)) as waiter:
        ack = await client.configure_node(addr, relay=relay, ttl=ttl, op_id=op)
        if not ack.ok:
            fail(f"CONFIGURE_NODE answered {ack.status}")
        event = await waiter.wait(timeout)
    assert isinstance(event, NodeConfigured)
    if event.status == Status.OK:
        bridge = db.find_bridge(addr=addr)
        if bridge is not None:
            db.update_bridge(bridge.uuid, configured=True)
    return event.status


@app.command()
def provision(uuid: str = typer.Argument(..., help="Device UUID from `mesh scan` (hex)."),
              name: str = typer.Option("", "--name", help="Name kept in the gateway's CDB."),
              configure: bool = typer.Option(True, "--configure/--no-configure", help="Configure right after."),
              relay: bool = typer.Option(True, "--relay/--no-relay"),
              ttl: int = typer.Option(5, "--ttl", min=1, max=127),
              timeout: float = typer.Option(60.0, "--timeout", help=TIMEOUT_HELP),
              url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """Provision one selected bridge (PB-ADV, no OOB), then configure it."""
    config = load()
    port = gateway_url(url, config)
    device = _parse_uuid(uuid)

    async def main() -> None:
        from cremind_tag.gateway import Provisioned, matches
        from cremind_tag.protocol.ids import Status
        from cremind_tag.store import BridgeRecord

        with open_db(config) as db:
            async with connect_gateway(port) as client:
                record_gateway(db, port, client)
                op = client.new_op_id()
                with client.expect(matches(Provisioned, op_id=op)) as waiter:
                    ack = await client.provision(device, name or None, op_id=op)
                    if not ack.ok:
                        fail(f"PROVISION answered {ack.status}" + (f": {ack.text}" if ack.text else ""))
                    event = await waiter.wait(timeout)
                assert isinstance(event, Provisioned)
                if event.status != Status.OK:
                    fail(f"provisioning failed: {event.status}")
                from cremind_tag.cli._hardware import gateway_hw_id

                db.upsert_bridge(BridgeRecord(device.hex(), addr=event.addr, name=name, elements=event.elements,
                                              gateway_hw_id=gateway_hw_id(port)))
                console.print(f"provisioned {device.hex()} at address {event.addr:#06x}")
                if configure:
                    status = await _configure(client, db, event.addr, relay, ttl, timeout)
                    console.print(f"configure: {status_text(status)}")

    run(main)


@app.command("configure")
def configure_cmd(addr: str = typer.Argument(..., help="Unicast address (e.g. 0x0002)."),
                  relay: bool = typer.Option(True, "--relay/--no-relay"),
                  ttl: int = typer.Option(5, "--ttl", min=1, max=127),
                  timeout: float = typer.Option(60.0, "--timeout", help=TIMEOUT_HELP),
                  url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """App key + model binding + relay + TTL on a provisioned bridge."""
    config = load()
    port = gateway_url(url, config)
    address = _parse_addr(addr)

    async def main() -> Any:
        with open_db(config) as db:
            async with connect_gateway(port) as client:
                return await _configure(client, db, address, relay, ttl, timeout)

    console.print(f"configure {address:#06x}: {status_text(run(main))}")


@app.command()
def remove(addr: str = typer.Argument(..., help="Unicast address of the bridge."),
           yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
           timeout: float = typer.Option(60.0, "--timeout", help=TIMEOUT_HELP),
           url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """Reset a bridge and delete it from the CDB (its tag assignments are lost)."""
    config = load()
    port = gateway_url(url, config)
    address = _parse_addr(addr)
    if not yes and not typer.confirm(f"Remove bridge {address:#06x} from the mesh?"):
        raise typer.Abort()

    async def main() -> Any:
        from cremind_tag.gateway import NodeRemoved, matches
        from cremind_tag.protocol.ids import Status

        with open_db(config) as db:
            async with connect_gateway(port) as client:
                op = client.new_op_id()
                with client.expect(matches(NodeRemoved, op_id=op)) as waiter:
                    ack = await client.remove_node(address, op_id=op)
                    if not ack.ok:
                        fail(f"REMOVE_NODE answered {ack.status}")
                    event = await waiter.wait(timeout)
                assert isinstance(event, NodeRemoved)
                if event.status == Status.OK:
                    db.delete_bridge(addr=address)
                return event.status

    console.print(f"remove {address:#06x}: {status_text(run(main))}")


@app.command()
def nodes(url: str | None = typer.Option(None, "--url", help=URL_HELP),
          as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """The gateway's CDB and each bridge's last known info (the local inventory is updated)."""
    config = load()
    port = gateway_url(url, config)

    async def main() -> list[dict[str, Any]]:
        from cremind_tag.cli._hardware import gateway_hw_id
        from cremind_tag.store import BridgeRecord

        rows = []
        with open_db(config) as db:
            async with connect_gateway(port) as client:
                record_gateway(db, port, client)
                node_list = await client.list_nodes()
                inventory = {b.addr: b for b in await client.get_inventory()}
            for node in node_list:
                info = inventory.get(node.addr)
                pack = info.fontpack_id.hex() if info and info.fontpack_id and any(info.fontpack_id) else None
                db.upsert_bridge(BridgeRecord(node.uuid.hex(), addr=node.addr, name=node.name, elements=node.elements,
                                              configured=node.configured, fw=info.fw if info else None,
                                              fontpack_id=pack, flash_size=info.flash_size if info else None,
                                              gateway_hw_id=gateway_hw_id(port)))
                rows.append({"addr": node.addr, "uuid": node.uuid.hex(), "name": node.name,
                             "configured": node.configured, "last_seen_s": node.last_seen_s,
                             "fw": info.fw if info else None, "fontpack_id": pack,
                             "assigned": [{"tag_id": f"{a.tag_id:08X}", "epoch": a.epoch}
                                          for a in (info.assigned if info else ())]})
        return rows

    rows = run(main)
    if as_json:
        print_json(rows)
        return
    t = table("Mesh nodes", "Addr", "Name", "UUID", "Configured", "FW", "Font pack", "Tags", "Seen")
    for r in rows:
        t.add_row(f"{r['addr']:#06x}", r["name"], r["uuid"], "yes" if r["configured"] else "[yellow]no[/yellow]",
                  r["fw"] or "-", r["fontpack_id"] or "-", ", ".join(a["tag_id"] for a in r["assigned"]) or "-",
                  f"{r['last_seen_s']} s ago" if r["last_seen_s"] is not None else "-")
    console.print(t if rows else "No provisioned bridges.")


@app.command()
def identify(addr: str = typer.Argument(..., help="Unicast address of the bridge."),
             url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """Make a bridge blink/beep so you can find it."""
    port = gateway_url(url)
    address = _parse_addr(addr)

    async def main() -> Any:
        async with connect_gateway(port) as client:
            return (await client.identify_node(address)).status

    console.print(f"identify {address:#06x}: {status_text(run(main))}")
