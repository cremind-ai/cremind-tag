"""`cremind-tag sim` — run a simulated gateway, bridges and tags for development and tests.

The simulator speaks the real serial protocol on TCP: point any client at
``socket://127.0.0.1:<port>`` (for example ``CREMIND_TAG_GATEWAY_URL``). See
docs/simulator.md for what it models and the ``--fault`` syntax.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from cremind_tag.cli._hardware import console, err_console, fail, load, open_db, open_secrets, table

app = typer.Typer(name="sim", help="Run a simulated gateway, bridges and tags for development and tests.",
                  no_args_is_help=True)


@app.command("run")
def run_sim(
    port: int = typer.Option(7777, "--port", help="Gateway TCP port (socket://HOST:PORT)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Listen address."),
    bridges: int = typer.Option(1, "--bridges", min=0, max=5, help="Provisioned and configured bridges."),
    unprovisioned: int = typer.Option(0, "--unprovisioned", min=0, max=5,
                                      help="Extra bridges waiting to be provisioned (they beacon during a scan)."),
    tags: int = typer.Option(2, "--tags", min=0, max=20, help="Simulated tags."),
    pack: Path | None = typer.Option(None, "--pack", exists=True, dir_okay=False,
                                     help="Font pack installed on every bridge (default: hardware.fontpack)."),
    panel: str = typer.Option("uc8176_420_bw", "--panel", help="Panel of the simulated tags."),
    time_scale: float = typer.Option(10.0, "--time-scale", min=0.1,
                                     help="Simulated seconds per real second (30 s wakes take 3 s at 10)."),
    seed: int = typer.Option(1, "--seed", help="Seed of every random choice (ids, secrets, jitter, faults)."),
    fault: list[str] = typer.Option([], "--fault", help="Fault injection, repeatable (docs/simulator.md)."),
    maint_port_base: int = typer.Option(0, "--maint-port-base",
                                        help="First bridge maintenance port (default: --port + 1)."),
    state: Path | None = typer.Option(None, "--state", help="JSON file keeping the CDB, assignments and tag NVS."),
    assign: bool = typer.Option(True, "--assign/--no-assign", help="Assign the tags round-robin at epoch 1."),
    register: bool = typer.Option(False, "--register", help="Add the simulated bridges and tags (with their "
                                                            "secrets) to the local inventory, for the CLI/daemon."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log simulator events."),
) -> None:
    """Start the simulator and keep it running until Ctrl-C."""
    from cremind_tag.enroll.hardware import parse_panel
    from cremind_tag.sim import (
        Assign,
        BridgeSpec,
        FaultSpecError,
        SimConfig,
        SimFaults,
        Simulator,
        TagSpec,
        parse_fault,
    )

    config = load()
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        panel_id = parse_panel(panel)
    except ValueError as exc:
        fail(str(exc))
    pack_path = pack or config.hardware.fontpack
    pack_bytes = Path(pack_path).read_bytes() if pack_path else None
    if pack_bytes is None:
        err_console.print("[yellow]no font pack: deliveries will end FONTPACK_MISMATCH (--pack)[/yellow]")
    faults = SimFaults()
    for spec in fault:
        try:
            parse_fault(spec, faults)
        except FaultSpecError as exc:
            fail(str(exc))
    try:
        tag_specs = [TagSpec.generate(seed, i, panel=panel_id) for i in range(tags)]
    except ValueError as exc:
        fail(str(exc))
    bridge_specs = [BridgeSpec(name=f"bridge-{i + 1}") for i in range(bridges)]
    bridge_specs += [BridgeSpec(name=f"new-{i + 1}", provisioned=False) for i in range(unprovisioned)]
    assignments = [Assign(t.tag_id, i % bridges, 1) for i, t in enumerate(tag_specs)] if assign and bridges else []
    sim_config = SimConfig(seed=seed, time_scale=time_scale, host=host, gateway_port=port,
                           maintenance_port_base=maint_port_base or port + 1, bridges=bridge_specs, tags=tag_specs,
                           assignments=assignments, fontpack=pack_bytes, faults=faults, state_file=state)

    async def main() -> None:
        sim = Simulator(sim_config)
        await sim.start()
        try:
            if register:
                _register(sim)
            _describe(sim)
            while True:
                await asyncio.sleep(3600)
        finally:
            await sim.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("simulator stopped")
    except OSError as exc:
        fail(f"cannot listen on {host}:{port}: {exc}")


def _describe(sim: object) -> None:
    from cremind_tag.enroll.hardware import panel_name
    from cremind_tag.sim import Simulator

    assert isinstance(sim, Simulator)
    console.print(f"gateway: [bold]{sim.gateway_url}[/bold]  (e.g. CREMIND_TAG_GATEWAY_URL={sim.gateway_url})")
    t = table("Bridges", "#", "Mesh addr", "UUID", "Maintenance port", "Font pack")
    for index, bridge in enumerate(sim.bridges):
        pack = bridge.fontpack_id
        t.add_row(str(index), f"{bridge.addr:#06x}" if bridge.addr else "unprovisioned", bridge.uuid.hex(),
                  bridge.maint.url if bridge.maint.port else "-", pack.hex() if pack else "-")
    console.print(t)
    t = table("Tags", "Tag", "Panel", "Size", "Assigned to")
    assigned = {a.tag_id: a for a in sim.config.assignments}
    for tag in sim.tags.values():
        a = assigned.get(tag.tag_id)
        where = f"{sim.bridges[a.bridge].addr:#06x} epoch {a.epoch}" if a else "-"
        t.add_row(f"{tag.tag_id:08X}", panel_name(tag.spec.panel),
                  f"{tag.spec.width}x{tag.spec.height}x{tag.spec.planes}", where)
    console.print(t)
    console.print(f"time scale {sim.config.time_scale:g}; Ctrl-C to stop")


def _register(sim: object) -> None:
    """Record the simulated hardware as if it had been provisioned and enrolled from this PC."""
    from cremind_tag.cli._hardware import gateway_hw_id
    from cremind_tag.protocol.ids import Board
    from cremind_tag.sim import Simulator
    from cremind_tag.store import BridgeRecord, GatewayRecord, TagRecord

    assert isinstance(sim, Simulator)
    config = load()
    secrets = open_secrets(config)
    url = sim.gateway_url
    with open_db(config) as db:
        db.upsert_gateway(GatewayRecord(gateway_hw_id(url), port=url, boot_id=sim.gateway.boot_id, fw="0.1.0",
                                        build="sim", board=Board.NRF52840DK_GATEWAY))
        for bridge in sim.bridges:
            if bridge.addr is None:
                continue
            pack = bridge.fontpack_id
            db.upsert_bridge(BridgeRecord(bridge.uuid.hex(), addr=bridge.addr, name=bridge.name,
                                          configured=bridge.configured, fw="0.1.0", board=bridge.board,
                                          fontpack_id=pack.hex() if pack else None, flash_size=bridge.flash.size,
                                          gateway_hw_id=gateway_hw_id(url)))
        assigned = {a.tag_id: a for a in sim.config.assignments}
        for index, tag in enumerate(sim.tags.values()):
            if db.tag_exists(tag.tag_id):
                err_console.print(f"[yellow]tag {tag.tag_id:08X} already in the inventory: skipped[/yellow]")
                continue
            ref = secrets.set_tag_secret(tag.tag_id, tag.spec.secret)
            a = assigned.get(tag.tag_id)
            spec = tag.spec
            db.insert_tag(TagRecord(tag.tag_id, spec.board, spec.panel, spec.width, spec.height, spec.planes,
                                    spec.plane_flags, ref, name=f"sim-{index + 1}", fw="0.1.0",
                                    epoch=a.epoch if a else 0,
                                    bridge_addr=sim.bridges[a.bridge].addr if a else None))
    console.print(f"registered {len(sim.tags)} simulated tag(s) in {config.db_path} ({secrets.describe()})")
