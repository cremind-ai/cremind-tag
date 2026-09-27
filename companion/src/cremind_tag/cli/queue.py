"""`cremind-tag queue` — inspect and manage the local durable delivery queue."""

import typer

app = typer.Typer(name="queue", help="Inspect and manage the local durable delivery queue.", no_args_is_help=True)
