"""`cremind-tag daemon` — run the delivery daemon (Cremind feed -> screens -> gateway -> receipts)."""

import typer

app = typer.Typer(name="daemon", help="Run the delivery daemon (Cremind feed -> screens -> gateway -> receipts).", no_args_is_help=True)
