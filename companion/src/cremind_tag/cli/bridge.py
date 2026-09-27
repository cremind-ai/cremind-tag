"""`cremind-tag bridge` — a bridge's own USB/UART maintenance port: info, font packs, flash test.

The maintenance port (docs/protocol.md §1.6) is separate from the mesh: plug the
bridge into this PC to install a font pack (docs/fontpack.md §4) or qualify its
external flash. Mesh-side information about bridges is under `cremind-tag mesh`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from cremind_tag.cli._hardware import (
    JSON_HELP,
    console,
    err_console,
    fail,
    load,
    print_json,
    run,
    status_text,
    table,
)
from cremind_tag.cli._hardware import bridge_url as resolve_url

app = typer.Typer(name="bridge", help="A bridge's maintenance port: info, font packs, flash test.",
                  no_args_is_help=True)

URL_HELP = "Maintenance port (COM9, /dev/ttyACM1, socket://...). Default: hardware.bridge_url."


def _client(url: str) -> Any:
    from cremind_tag.bridge_maint import BridgeMaintClient

    return BridgeMaintClient(url, name="cremind-tag cli")


@app.command()
def info(url: str | None = typer.Option(None, "--url", help=URL_HELP),
         as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Firmware, boot id, capabilities and counters of the bridge."""
    port = resolve_url(url)

    async def main() -> dict[str, Any]:
        async with _client(port) as client:
            details = await client.info()
            status = await client.font_status()
            return {"port": port, "fw": details.fw, "build": details.build, "boot_id": details.boot_id,
                    "caps": dict(details.caps.raw), "counters": dict(details.counters),
                    "fontpack_id": status.fontpack_id.hex() if status.fontpack_id else None,
                    "flash_size": status.flash_size}

    data = run(main)
    if as_json:
        print_json(data)
        return
    t = table(f"Bridge {port}", "Field", "Value")
    t.add_row("firmware", f"{data['fw']} ({data['build']})")
    t.add_row("boot id", f"{data['boot_id']:08x}")
    t.add_row("font pack", data["fontpack_id"] or "[yellow]none[/yellow]")
    t.add_row("flash size", f"{data['flash_size'] // (1 << 20)} MiB" if data["flash_size"] else "-")
    for key, value in sorted(data["caps"].items()):
        t.add_row(f"caps.{key}", str(value))
    for key, value in sorted(data["counters"].items()):
        t.add_row(f"counter {key}", str(value))
    console.print(t)


@app.command()
def status(url: str | None = typer.Option(None, "--url", help=URL_HELP),
           as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """The active font pack (FONT_STATUS)."""
    port = resolve_url(url)

    async def main() -> dict[str, Any]:
        async with _client(port) as client:
            st = await client.font_status()
            return {"fontpack_id": st.fontpack_id.hex() if st.fontpack_id else None, "slot": st.slot,
                    "size": st.size, "flash_size": st.flash_size}

    data = run(main)
    if as_json:
        print_json(data)
        return
    if data["fontpack_id"] is None:
        console.print("No font pack is active on this bridge (cremind-tag bridge fonts-install <pack>).")
    else:
        console.print(f"font pack {data['fontpack_id']} in slot {data['slot']} ({data['size']} bytes), "
                      f"flash {data['flash_size']} bytes")


@app.command("fonts-install")
def fonts_install(pack: Path | None = typer.Argument(None, exists=True, dir_okay=False,
                                                      help="Font pack (.ctfp). Default: hardware.fontpack."),
                  url: str | None = typer.Option(None, "--url", help=URL_HELP),
                  force: bool = typer.Option(False, "--force", help="Install even if this pack is active."),
                  chunk: int | None = typer.Option(None, "--chunk", min=64, help="FONT_DATA bytes per frame.")) -> None:
    """Install a font pack into the inactive slot and activate it (the active pack stays until COMMIT)."""
    from rich.progress import BarColumn, DownloadColumn, Progress, TimeRemainingColumn, TransferSpeedColumn

    config = load()
    port = resolve_url(url, config)
    path = pack or config.hardware.fontpack
    if path is None:
        fail("no font pack: pass a path or set hardware.fontpack")
    data = Path(path).read_bytes()

    async def main() -> Any:
        from cremind_tag.fontpack.format import FontPackError

        with Progress("[progress.description]{task.description}", BarColumn(), DownloadColumn(),
                      TransferSpeedColumn(), TimeRemainingColumn(), console=console) as progress:
            task = progress.add_task(f"installing {Path(path).name}", total=len(data))
            async with _client(port) as client:
                try:
                    return await client.font_install(
                        data, force=force, chunk_size=chunk,
                        progress=lambda done, total: progress.update(task, completed=done))
                except FontPackError as exc:
                    fail(f"{path} is not a valid font pack: {exc}")

    result = run(main)
    if result.skipped:
        console.print(f"font pack {result.fontpack_id.hex()} is already active (use --force to reinstall)")
    else:
        console.print(f"font pack {result.fontpack_id.hex()} active in slot {result.slot}")


@app.command("flash-test")
def flash_test(url: str | None = typer.Option(None, "--url", help=URL_HELP),
               yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
               as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Erase/write/read back at the inactive slot, around 16 MiB and at the last sector."""
    port = resolve_url(url)
    if not yes and not typer.confirm("The test erases sectors outside the active slot (an inactive pack is lost). "
                                     "Continue?"):
        raise typer.Abort()

    async def main() -> Any:
        async with _client(port) as client:
            return await client.flash_test()

    result = run(main)
    if as_json:
        print_json({"status": result.status, "flash_size": result.flash_size,
                    "items": [{"offset": i.offset, "status": i.status} for i in result.items]})
        return
    t = table(f"Flash test ({result.flash_size or 0} bytes)", "Offset", "Status")
    for item in result.items:
        t.add_row(f"{item.offset:#010x}", status_text(item.status))
    console.print(t)
    if not result.ok:
        err_console.print("[yellow]Not every sector passed (BUSY = in use by the active pack, not tested).[/yellow]")
