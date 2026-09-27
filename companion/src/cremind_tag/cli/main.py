"""`cremind-tag` — the Cremind Tag companion CLI.

Each sub-app lives in its own module under `cremind_tag.cli`; this file only
registers them. Keep heavy imports inside command bodies so `--help` stays fast.
"""

import typer

from cremind_tag import __version__
from cremind_tag.cli import bridge, connect, daemon, diag, doctor, fonts, gateway, mesh, preview, queue, sim, tag

app = typer.Typer(
    name="cremind-tag",
    help="Cremind Tag companion: deliver Cremind updates to e-paper tags over a Zephyr mesh.",
    no_args_is_help=True,
)
app.add_typer(bridge.app, name="bridge")
app.add_typer(connect.app, name="connect")
app.add_typer(daemon.app, name="daemon")
app.add_typer(diag.app, name="diag")
app.add_typer(doctor.app, name="doctor")
app.add_typer(fonts.app, name="fonts")
app.add_typer(gateway.app, name="gateway")
app.add_typer(mesh.app, name="mesh")
app.add_typer(preview.app, name="preview")
app.add_typer(queue.app, name="queue")
app.add_typer(sim.app, name="sim")
app.add_typer(tag.app, name="tag")


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Print the version and exit."),
) -> None:
    """Cremind Tag companion."""


def main() -> None:
    app()
