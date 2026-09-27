"""`cremind-tag daemon` — run the delivery daemon (Cremind feed -> screens -> gateway -> receipts).

::

    cremind-tag daemon run [--gateway COM7] [--pack noto.ctfp]      # until Ctrl-C
    cremind-tag daemon run --once                                   # catch up, deliver what is due, exit
    cremind-tag daemon status [--json]

The daemon logs to ``<data dir>/logs/daemon.log`` (rotated) and to stderr, and
writes ``<data dir>/daemon-status.json`` every few seconds for ``status``.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import typer

from cremind_tag.cli._hardware import JSON_HELP, console, err_console, fail, load, open_db, print_json, table

app = typer.Typer(name="daemon", help="Run the delivery daemon (Cremind feed -> screens -> gateway -> receipts).",
                  no_args_is_help=True)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def teardown_logging() -> None:
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_cremind_tag_daemon", False)]:
        root.removeHandler(old)
        old.close()


def setup_logging(data_dir: Path, *, verbose: bool, max_bytes: int) -> Path:
    """Rotating file log (3 × ``max_bytes``) plus stderr; the HTTP client's per-request lines stay at DEBUG."""
    from logging.handlers import RotatingFileHandler

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "daemon.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    teardown_logging()
    file_handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
    console_handler = logging.StreamHandler()
    for handler in (file_handler, console_handler):
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, "_cremind_tag_daemon", True)  # noqa: B010 - marks our handlers for teardown
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
    return path


@app.command("run")
def run_daemon(
    once: bool = typer.Option(False, "--once", help="Catch up with Cremind, deliver what is due, flush receipts, "
                                                    "then exit."),
    gateway: str | None = typer.Option(None, "--gateway", help="Gateway port or URL (default: hardware.gateway_url)."),
    pack: Path | None = typer.Option(None, "--pack", exists=True, dir_okay=False,
                                     help="Font pack the bridges have active (default: hardware.fontpack)."),
    font_cache: Path | None = typer.Option(None, "--font-cache", file_okay=False,
                                           help="Pinned font cache (default: CREMIND_TAG_FONT_CACHE or the "
                                                "checkout's fonts/cache)."),
    max_seconds: float = typer.Option(120.0, "--max-seconds", min=1.0, help="With --once: give up after this long."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Run the delivery daemon."""
    import asyncio

    import cremind_tag.connector.settings  # noqa: F401  - registers [cremind]
    from cremind_tag.daemon import DaemonConfigError, DaemonOptions, DaemonService
    from cremind_tag.daemon.settings import DaemonSettings
    from cremind_tag.secrets import SecretStoreError

    config = load()
    settings = config.section(DaemonSettings)
    log_path = setup_logging(config.ensure_data_dir(), verbose=verbose, max_bytes=settings.log_max_bytes)
    try:
        options = DaemonOptions.from_config(config, gateway_url=gateway, fontpack=pack)
    except (DaemonConfigError, SecretStoreError, ValueError) as exc:
        fail(str(exc))
    options.font_cache = font_cache
    if not options.content_credentials and options.hardware_credential is None:
        err_console.print("[yellow]no Cremind credentials configured (cremind-tag connect …): the daemon only "
                          "talks to the gateway[/yellow]")
    if options.fontpack is None:
        err_console.print("[yellow]no font pack (--pack or hardware.fontpack): screens are held[/yellow]")
    service = DaemonService(options)
    err_console.print(f"logging to {log_path}")

    async def main() -> dict[str, Any] | None:
        if once:
            return await service.run_once(max_s=max_seconds)
        await service.run()
        return None

    try:
        summary = asyncio.run(main())
    except KeyboardInterrupt:
        console.print("daemon stopped")
        return
    except DaemonConfigError as exc:
        fail(str(exc))
    finally:
        teardown_logging()
    if summary is not None:
        queue = summary.get("queue", {})
        console.print(f"done: queue depth {queue.get('depth')}, revisions {queue.get('revisions')}, "
                      f"outbox {queue.get('outbox') or {}}")
        failed = {k: v for k, v in summary.get("credentials", {}).items() if v.get("state") == "stopped"}
        if failed:
            fail("credential(s) stopped: " + "; ".join(f"{k}: {v.get('error')}" for k, v in failed.items()))
        if not summary.get("caught_up"):
            fail(f"not caught up after {max_seconds:.0f}s (see {log_path})")


def _age(text: str | None) -> float | None:
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.UTC) - when).total_seconds()


@app.command()
def status(as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Whether the daemon runs, its connections and credentials, and the queue."""
    from cremind_tag.daemon import QueueStore, read_status
    from cremind_tag.daemon.service import STATUS_EVERY_S

    config = load()
    snapshot = read_status(config.data_dir) or {}
    age = _age(snapshot.get("updated_at"))
    running = bool(snapshot.get("running", True)) and age is not None and age < 5 * STATUS_EVERY_S
    with open_db(config) as db:
        queue = QueueStore(db).queue_stats()
    data = {"running": running, "last_update_age_s": round(age, 1) if age is not None else None,
            "daemon": snapshot, "queue": queue}
    if as_json:
        print_json(data)
        return
    if not snapshot:
        console.print("daemon: [yellow]never ran here[/yellow] (cremind-tag daemon run)")
    elif running:
        console.print(f"daemon: [green]running[/green] (pid {snapshot.get('pid')}, since {snapshot.get('started_at')})")
    else:
        console.print(f"daemon: [yellow]not running[/yellow] (last update {snapshot.get('updated_at')})")
    if snapshot:
        gw = snapshot.get("gateway") or {}
        console.print(f"gateway: {gw.get('url') or '-'} "
                      + ("[green]connected[/green]" if gw.get("connected") else "[yellow]not connected[/yellow]")
                      + (f", boot {gw['boot_id']:08x}" if gw.get("boot_id") else ""))
        console.print(f"font pack: {snapshot.get('fontpack_id') or '[yellow]none[/yellow]'}")
        t = table("Credentials", "Credential", "Kind", "State", "Detail")
        for cid, info in (snapshot.get("credentials") or {}).items():
            state = info.get("state") or "-"
            color = "green" if state == "running" else "red" if state == "stopped" else "yellow"
            t.add_row(cid, info.get("kind") or "-", f"[{color}]{state}[/{color}]",
                      str(info.get("error") or info.get("profile") or info.get("companion_id") or ""))
        console.print(t)
        holds = (snapshot.get("scheduler") or {}).get("holds") or {}
        for tag, reason in holds.items():
            console.print(f"tag {tag}: held ({reason})")
    console.print(f"queue: {queue['depth']} job(s) waiting (oldest {queue['oldest_age_s']} s), "
                  f"revisions {queue['revisions'] or {}}, outbox {queue['outbox'] or {}}")
    for tag, reason in (queue.get("blocked") or {}).items():
        console.print(f"tag {tag}: [red]blocked[/red] ({reason}) — cremind-tag queue retry --tag {tag}")
