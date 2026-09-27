"""`cremind-tag gateway` — talk to the USB/UART gateway: info, counters, reboot."""

import typer

app = typer.Typer(name="gateway", help="Talk to the USB/UART gateway: info, counters, reboot.", no_args_is_help=True)
