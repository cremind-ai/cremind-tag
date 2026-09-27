"""`cremind-tag tag` — enroll tags over SWD, list them, assign them to bridges, send commands.

`tag assign` is the low-level form of the connector's `assign_tag` command: it
derives ``K_epoch`` from the tag secret (never sent anywhere else), sends
``ASSIGN_TAG`` and, once the bridge confirms, ``UNASSIGN_TAG`` to the previous
bridge. `tag command identify` is a companion-level operation (docs/protocol.md
§5.6): it delivers a new revision showing the tag id as a QR code.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from cremind_tag.cli._hardware import (
    JSON_HELP,
    URL_HELP,
    bridge_fontpack,
    connect_gateway,
    console,
    err_console,
    fail,
    gateway_url,
    load,
    open_db,
    open_secrets,
    print_json,
    record_gateway,
    run,
    status_text,
    table,
)

if TYPE_CHECKING:
    from cremind_tag.gateway import GatewayClient
    from cremind_tag.store import Database, TagRecord

app = typer.Typer(name="tag", help="Enroll tags (J-Link), list them, assign them to bridges.", no_args_is_help=True)

WAIT_HELP = "Seconds to wait for the result (a tag wakes about every 30 s)."


def _tag_id(text: str) -> int:
    from cremind_tag.enroll.hardware import parse_tag_id

    try:
        return parse_tag_id(text)
    except ValueError as exc:
        fail(str(exc))


def _tag_row(tag: TagRecord) -> dict[str, Any]:
    from cremind_tag.enroll.hardware import board_name, panel_name

    return {"tag_id": tag.hw_id, "name": tag.name, "board": board_name(tag.board), "panel": panel_name(tag.panel),
            "width": tag.width, "height": tag.height, "planes": tag.planes, "plane_flags": tag.plane_flags,
            "fw": tag.fw, "epoch": tag.epoch, "bridge_addr": tag.bridge_addr, "last_revision": tag.last_revision,
            "protected": tag.protected, "enrolled_at": tag.enrolled_at}


# -- enroll ------------------------------------------------------------------------------------


@app.command()
def enroll(
    board: str = typer.Option(..., "--board", help="Tag board: laowu_bw, laowu_bwr, sifei_52810, hema_52811, "
                                                   "nrf52dk_tag (or the spec name / id)."),
    panel: str | None = typer.Option(None, "--panel", help="Panel (default: the board's panel), e.g. "
                                                          "uc8176_420_bw, uc8176_420_bwr, none, unverified."),
    firmware: Path | None = typer.Option(None, "--firmware", exists=True, dir_okay=False,
                                         help="Tag firmware (Intel HEX) to flash after a full chip erase."),
    protect: bool = typer.Option(False, "--protect", help="Enable APPROTECT afterwards (irreversible, see docs)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before enabling APPROTECT."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write the UICR image and print the commands only."),
    register: bool = typer.Option(False, "--register", help="With --dry-run: store the secret and the inventory "
                                                            "row so the printed commands can be run by hand."),
    tool: str | None = typer.Option(None, "--tool", help="auto | nrfutil | nrfjprog | jlink (default: config)."),
    serial_number: str | None = typer.Option(None, "--serial-number", help="J-Link probe serial number."),
    name: str = typer.Option("", "--name", help="A name for the tag in the local inventory."),
    tag_id: str | None = typer.Option(None, "--tag-id", help="Use this tag id instead of a random one."),
    out: Path | None = typer.Option(None, "--out", help="Where to write the UICR image (default: data dir)."),
    keep_hex: bool = typer.Option(False, "--keep-hex", help="Keep the UICR image (it contains the secret)."),
    width: int | None = typer.Option(None, "--width", help="Panel width (unverified panels only)."),
    height: int | None = typer.Option(None, "--height", help="Panel height (unverified panels only)."),
    planes: int | None = typer.Option(None, "--planes", help="1 = BW, 2 = BW + red (unverified panels only)."),
    plane_flags: int | None = typer.Option(None, "--plane-flags", help="bit0 plane0 1=white, bit1 plane1 1=red."),
) -> None:
    """Give a tag its identity: random id + secret in UICR, verified, recorded, secret in the OS store."""
    from cremind_tag.enroll import EnrollError, ToolError, enroll_tag

    config = load()
    geometry = None
    if any(v is not None for v in (width, height, planes, plane_flags)):
        if None in (width, height, planes, plane_flags):
            fail("--width, --height, --planes and --plane-flags go together")
        geometry = (width, height, planes, plane_flags)
    if register and not dry_run:
        fail("--register only applies to --dry-run (a real enrollment always registers)")

    def confirm(warning: str) -> bool:
        err_console.print(f"[yellow]{warning}[/yellow]")
        return typer.confirm("Enable APPROTECT on this tag?", default=False)

    with open_db(config) as db:
        try:
            result = enroll_tag(
                board=board, panel=panel, db=db, secrets=open_secrets(config),
                out_dir=out or config.ensure_data_dir() / "enroll", tool=tool or config.hardware.jlink_tool,
                serial_number=serial_number or config.hardware.jlink_serial, firmware=firmware, protect=protect,
                confirm_protect=None if yes else confirm, dry_run=dry_run, register=register,
                tag_id=_tag_id(tag_id) if tag_id else None, name=name, geometry=geometry,  # type: ignore[arg-type]
                keep_hex=keep_hex)
        except (ValueError, EnrollError, ToolError) as exc:
            fail(str(exc))
    t = table("Dry run" if result.dry_run else "Enrolled", "Field", "Value")
    t.add_row("tag id", result.hw_id)
    t.add_row("board / panel", f"{result.board.name} / {result.panel.name}")
    g = result.geometry
    t.add_row("panel geometry", f"{g.width}x{g.height}, {g.planes} plane(s), flags {g.plane_flags:#04x}")
    t.add_row("tool", result.tool)
    t.add_row("registered", "yes" if result.registered else "no")
    if protect:
        t.add_row("APPROTECT", "declined" if result.protect_declined else "enabled" if result.protected else "planned")
    console.print(t)
    for warning in result.warnings:
        err_console.print(f"[yellow]warning:[/yellow] {warning}")
    if result.dry_run:
        console.print("\nCommands:")
        console.print(result.render_plan(), markup=False, highlight=False)
        err_console.print(f"[yellow]{result.hex_path} contains the tag secret: delete it after use.[/yellow]")


# -- inventory -------------------------------------------------------------------------------------


@app.command("list")
def list_tags(as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Enrolled tags in the local inventory."""
    with open_db() as db:
        rows = [_tag_row(t) for t in db.list_tags()]
    if as_json:
        print_json(rows)
        return
    t = table("Tags", "Tag", "Name", "Board", "Panel", "Size", "Epoch", "Bridge", "Rev", "Protected")
    for r in rows:
        t.add_row(r["tag_id"], r["name"], r["board"], r["panel"], f"{r['width']}x{r['height']}x{r['planes']}",
                  str(r["epoch"]), f"{r['bridge_addr']:#06x}" if r["bridge_addr"] else "-", str(r["last_revision"]),
                  "yes" if r["protected"] else "no")
    console.print(t if rows else "No tags enrolled (cremind-tag tag enroll).")


@app.command()
def show(tag: str = typer.Argument(..., help="Tag id (8 hex digits)."),
         as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """One tag's inventory row (the secret itself is never shown)."""
    config = load()
    tag_id = _tag_id(tag)
    with open_db(config) as db:
        record = db.find_tag(tag_id)
        if record is None:
            fail(f"tag {tag_id:08X} is not enrolled here")
        row = _tag_row(record)
        bridge = db.find_bridge(addr=record.bridge_addr) if record.bridge_addr else None
    row["secret_ref"] = record.secret_ref
    row["secret_present"] = open_secrets(config).has_tag_secret(tag_id)
    row["bridge"] = {"uuid": bridge.uuid, "name": bridge.name} if bridge else None
    if as_json:
        print_json(row)
        return
    t = table(f"Tag {row['tag_id']}", "Field", "Value")
    for key, value in row.items():
        t.add_row(key, str(value))
    console.print(t)


# -- assignment ------------------------------------------------------------------------------------


async def _ensure_bridge(client: GatewayClient, db: Database, url: str, addr: int) -> None:
    """The inventory needs the bridge row (foreign key); fetch it from the gateway's CDB if missing."""
    from cremind_tag.cli._hardware import gateway_hw_id
    from cremind_tag.store import BridgeRecord

    if db.find_bridge(addr=addr) is not None:
        return
    node = next((n for n in await client.list_nodes() if n.addr == addr), None)
    if node is None:
        fail(f"the gateway has no bridge at {addr:#06x} (cremind-tag mesh nodes)")
    record_gateway(db, url, client)
    db.upsert_bridge(BridgeRecord(node.uuid.hex(), addr=addr, name=node.name, elements=node.elements,
                                  configured=node.configured, gateway_hw_id=gateway_hw_id(url)))


@app.command()
def assign(tag: str = typer.Argument(..., help="Tag id (8 hex digits)."),
           bridge: str = typer.Option(..., "--bridge", help="Unicast address of the bridge (e.g. 0x0002)."),
           epoch: int = typer.Option(..., "--epoch", min=1, max=0xFFFFFFFF,
                                     help="Assignment epoch (> the current one to move the tag)."),
           unassign_previous: bool = typer.Option(True, "--unassign-previous/--keep-previous",
                                                  help="Remove the key from the previous bridge afterwards."),
           timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait for each bridge."),
           url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """Low-level assignment: derive K_epoch, ASSIGN_TAG, then UNASSIGN_TAG on the previous bridge."""
    config = load()
    port = gateway_url(url, config)
    tag_id = _tag_id(tag)
    try:
        addr = int(bridge, 0)
    except ValueError:
        fail(f"not a mesh address: {bridge!r}")

    async def main() -> None:
        from cremind_tag.gateway import AssignResult, matches
        from cremind_tag.protocol.ids import Status

        secrets = open_secrets(config)
        with open_db(config) as db:
            record = db.get_tag(tag_id)
            if epoch < record.epoch:
                fail(f"epoch {epoch} is older than the tag's current epoch {record.epoch}")
            key = secrets.k_epoch(tag_id, epoch, record.secret_ref)
            async with connect_gateway(port) as client:
                await _ensure_bridge(client, db, port, addr)
                op = client.new_op_id()
                with client.expect(matches(AssignResult, op_id=op)) as waiter:
                    ack = await client.assign_tag(addr, tag_id, epoch, key, op_id=op)
                    if not ack.ok:
                        fail(f"ASSIGN_TAG answered {ack.status}" + (f": {ack.text}" if ack.text else ""))
                    result = await waiter.wait(timeout)
                assert isinstance(result, AssignResult)
                console.print(f"assign {tag_id:08X} -> {addr:#06x} epoch {epoch}: {status_text(result.status)}")
                if result.status != Status.OK:
                    fail("the bridge refused the assignment; the inventory is unchanged")
                db.set_assignment(tag_id, addr, epoch)
                previous = record.bridge_addr
                if unassign_previous and previous and previous != addr and record.epoch:
                    op = client.new_op_id()
                    with client.expect(matches(AssignResult, op_id=op)) as waiter:
                        ack = await client.unassign_tag(previous, tag_id, record.epoch, op_id=op)
                        if ack.ok:
                            done = await waiter.wait(timeout)
                            assert isinstance(done, AssignResult)
                            console.print(f"unassign from {previous:#06x}: {status_text(done.status)}")
                        else:
                            err_console.print(f"[yellow]UNASSIGN_TAG on {previous:#06x}: {ack.status}[/yellow]")

    run(main)


# -- commands --------------------------------------------------------------------------------------


def identify_layout(tag_id: int, width: int, height: int) -> bytes:
    """A frame + the tag id as a QR code (no font strikes needed)."""
    from cremind_tag.protocol.ids import Color, QrEcc
    from cremind_tag.protocol.layout import Layout, Progress, Qr, Rect, encode_layout, qr_code

    text = f"CTAG-{tag_id:08X}".encode("ascii")
    modules = qr_code(text, QrEcc.MEDIUM).get_size()
    scale = max(1, min(8, (min(width, height) - 48) // modules))
    side = modules * scale
    commands = (Rect(0, 0, width, height, 6, Color.BLACK),
                Rect(12, 12, width - 24, height - 24, 2, Color.BLACK),
                Qr((width - side) // 2, (height - side) // 2, scale, QrEcc.MEDIUM, Color.BLACK, text),
                Progress(24, height - 36, width - 48, 12, 1, 1, Color.BLACK))
    return encode_layout(Layout(width, height, 0, Color.WHITE, commands))


@app.command()
def command(action: str = typer.Argument(..., help="clear | identify"),
            tag: str = typer.Argument(..., help="Tag id (8 hex digits)."),
            timeout: float = typer.Option(120.0, "--timeout", help=WAIT_HELP),
            url: str | None = typer.Option(None, "--url", help=URL_HELP)) -> None:
    """clear: white screen (TAG_COMMAND CLEAR). identify: show the tag id as a QR code (a new revision)."""
    action = action.lower()
    if action not in ("clear", "identify"):
        fail("action must be clear or identify")
    config = load()
    port = gateway_url(url, config)
    tag_id = _tag_id(tag)

    async def main() -> Any:
        from cremind_tag.gateway import ResultEvent, matches
        from cremind_tag.protocol.ids import TagCommand

        with open_db(config) as db:
            record = db.get_tag(tag_id)
            if not record.bridge_addr or not record.epoch:
                fail(f"tag {tag_id:08X} is not assigned (cremind-tag tag assign)")
            async with connect_gateway(port) as client:
                update_id = client.new_op_id()
                with client.expect(matches(ResultEvent, update_id=update_id)) as waiter:
                    if action == "clear":
                        ack = await client.tag_command(bridge=record.bridge_addr, tag_id=tag_id, epoch=record.epoch,
                                                       cmd=TagCommand.CLEAR, op_id=update_id)
                    else:
                        pack = await bridge_fontpack(client, record.bridge_addr)
                        if pack is None:
                            fail(f"bridge {record.bridge_addr:#06x} reports no active font pack")
                        revision = db.allocate_revision(tag_id)
                        ack = await client.deliver_layout(
                            bridge=record.bridge_addr, tag_id=tag_id, epoch=record.epoch, revision=revision,
                            update_id=update_id, fontpack_id=pack,
                            layout=identify_layout(tag_id, record.width, record.height))
                    if not ack.ok:
                        fail(f"{ack.msg.name} answered {ack.status}")
                    console.print(f"{action}: {status_text(ack.status)}, waiting for the tag ...")
                    result = await waiter.wait(timeout)
                assert isinstance(result, ResultEvent)
                return result.status

    console.print(f"{action} {tag_id:08X}: {status_text(run(main))}")
