"""`cremind-tag gateway` — talk to the USB/UART gateway: ports, info, counters, reboot, events."""

from __future__ import annotations

import time
from typing import Any

import typer

from cremind_tag.cli._hardware import (
    JSON_HELP,
    URL_HELP,
    connect_gateway,
    console,
    err_console,
    gateway_url,
    load,
    open_db,
    print_json,
    record_gateway,
    run,
    status_text,
    table,
)

app = typer.Typer(name="gateway", help="Talk to the USB/UART gateway: info, counters, reboot.", no_args_is_help=True)


@app.command()
def ports(as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """List serial ports (the configured gateway and bridge ports are marked)."""
    from serial.tools import list_ports

    hardware = load().hardware
    rows: list[dict[str, Any]] = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        vid_pid = f"{port.vid:04x}:{port.pid:04x}" if port.vid is not None and port.pid is not None else ""
        role = {hardware.gateway_url: "gateway", hardware.bridge_url: "bridge"}.get(port.device, "")
        rows.append({"port": port.device, "description": port.description, "usb": vid_pid,
                     "serial_number": port.serial_number or "", "manufacturer": port.manufacturer or "",
                     "configured_as": role})
    if as_json:
        print_json(rows)
        return
    t = table("Serial ports", "Port", "Description", "USB", "Serial number", "Configured as")
    for r in rows:
        t.add_row(r["port"], r["description"], r["usb"], r["serial_number"], r["configured_as"])
    console.print(t if rows else "No serial ports found.")


@app.command()
def info(url: str | None = typer.Option(None, "--url", help=URL_HELP),
         as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """HELLO + INFO: firmware, boot id, capabilities and counters (records the gateway locally)."""
    config = load()
    port = gateway_url(url, config)

    async def main() -> dict[str, Any]:
        async with connect_gateway(port) as client:
            details = await client.info()
            with open_db(config) as db:
                record_gateway(db, port, client)
            return {"port": port, "fw": details.fw, "build": details.build, "boot_id": details.boot_id,
                    "caps": dict(details.caps.raw), "counters": dict(details.counters)}

    data = run(main)
    if as_json:
        print_json(data)
        return
    t = table(f"Gateway {port}", "Field", "Value")
    t.add_row("firmware", f"{data['fw']} ({data['build']})")
    t.add_row("boot id", f"{data['boot_id']:08x}")
    for key, value in sorted(data["caps"].items()):
        t.add_row(f"caps.{key}", str(value))
    console.print(t)
    _print_counters(data["counters"])


def _print_counters(counters: dict[str, int]) -> None:
    t = table("Counters", "Counter", "Value")
    for key, value in sorted(counters.items()):
        t.add_row(key, str(value))
    console.print(t)


@app.command()
def counters(url: str | None = typer.Option(None, "--url", help=URL_HELP),
             as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Link, delivery and mesh counters (crc_errors, overruns, events_dropped, ...)."""
    port = gateway_url(url)

    async def main() -> dict[str, int]:
        async with connect_gateway(port) as client:
            return await client.get_counters()

    data = run(main)
    if as_json:
        print_json(data)
    else:
        _print_counters(data)


@app.command()
def reboot(url: str | None = typer.Option(None, "--url", help=URL_HELP),
           yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")) -> None:
    """Reboot the gateway (queued deliveries and unacknowledged events in its RAM are lost)."""
    port = gateway_url(url)
    if not yes and not typer.confirm("Reboot the gateway? Queued deliveries are lost and will be retried."):
        raise typer.Abort()

    async def main() -> Any:
        from cremind_tag.gateway import GatewayDisconnected

        async with connect_gateway(port) as client:
            try:
                return (await client.reboot()).status
            except GatewayDisconnected:
                return "rebooting (link dropped before the answer)"

    console.print(f"reboot: {status_text(run(main))}")


@app.command()
def events(url: str | None = typer.Option(None, "--url", help=URL_HELP),
           duration: float = typer.Option(0.0, "--duration", "-d", help="Stop after this many seconds (0 = Ctrl-C)."),
           ack: bool = typer.Option(False, "--ack", help="Acknowledge retained events (they are then gone for the "
                                                          "daemon: only when no daemon uses this gateway)."),
           as_json: bool = typer.Option(False, "--json", help="One JSON object per line.")) -> None:
    """Print gateway events as they arrive (retained ones are re-sent after the HELLO)."""
    port = gateway_url(url)
    if ack:
        err_console.print("[yellow]--ack: retained events will be released after they are printed.[/yellow]")

    async def main() -> None:
        from cremind_tag.gateway import GatewayEvent

        client = connect_gateway(port)

        async def printer(event: GatewayEvent) -> None:
            _print_event(event, as_json)

        if ack:
            client.add_event_handler(printer)
        async with client:
            deadline = time.monotonic() + duration if duration > 0 else None
            if ack:
                while deadline is None or time.monotonic() < deadline:
                    await _sleep_until(deadline)
                return
            with client.subscribe() as subscription:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return
                    try:
                        event = await subscription.get(remaining)
                    except TimeoutError:
                        return
                    _print_event(event, as_json)

    run(main)


async def _sleep_until(deadline: float | None) -> None:
    import asyncio

    await asyncio.sleep(1.0 if deadline is None else max(0.0, min(1.0, deadline - time.monotonic())))


def _print_event(event: Any, as_json: bool) -> None:
    import dataclasses

    fields = {f.name: getattr(event, f.name) for f in dataclasses.fields(event) if f.name not in ("raw", "boot_id")}
    if as_json:
        import json

        from cremind_tag.cli._hardware import _json_default

        print(json.dumps({"event": type(event).__name__, "boot_id": event.boot_id, **fields}, default=_json_default),
              flush=True)
        return
    parts = []
    for key, value in fields.items():
        if isinstance(value, bytes):
            value = value.hex()
        elif hasattr(value, "name"):
            value = value.name
        parts.append(f"{key}={value}")
    console.print(f"[bold]{type(event).__name__}[/bold] " + " ".join(parts))
