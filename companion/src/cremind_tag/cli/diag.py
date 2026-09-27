"""`cremind-tag diag` — collect diagnostics into one zip for a bug report.

``cremind-tag diag collect --out diag.zip`` writes:

- ``config.json`` — every registered configuration section (credential *ids*
  only; secrets live in the secret store and are never read here) and the
  secret store backend's description;
- ``versions.json`` — companion, Python, platform and key library versions;
- ``status.json`` — the daemon's last status snapshot;
- ``queue.json`` — queue statistics, recent jobs (ids, kinds, states, stages,
  outcomes — no card text), recent revisions (no layouts) and the outbox
  (kinds, attempts, errors — no payloads);
- ``inventory.json`` — gateways, bridges and tags (secret *references* only);
- ``counters.json`` — the gateway's ``INFO`` counters and cached bridge info
  (``--gateway URL`` or ``hardware.gateway_url``; ``--no-counters`` skips it);
- ``logs/`` — the newest daemon logs (the last ``--log-bytes`` of each file).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import platform
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from typing import Any

import typer

from cremind_tag import __version__
from cremind_tag.cli._hardware import console, load, open_db

app = typer.Typer(name="diag", help="Collect diagnostics.", no_args_is_help=True)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, default=str)


def _versions() -> dict[str, Any]:
    out: dict[str, Any] = {"cremind-tag": __version__, "python": sys.version, "platform": platform.platform()}
    for package in ("httpx", "cryptography", "pyserial", "keyring", "uharfbuzz", "freetype-py", "pyicu-wheels",
                    "pillow", "typer"):
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            out[package] = None
    return out


def _queue(db: Any) -> dict[str, Any]:
    from cremind_tag.daemon.store import QueueStore

    store = QueueStore(db)
    jobs = [{"delivery_id": j.delivery_id, "tag_id": f"{j.tag_id:08X}", "kind": j.kind, "state": j.state,
             "last_stage": j.last_stage, "outcome": j.outcome, "status_code": j.status_code, "revision": j.revision,
             "epoch": j.epoch, "created_at": j.created_at, "expires_at": j.expires_at,
             "detail": (j.detail or "").split(":")[0] or None}
            for j in store.list_jobs(include_finished=True, limit=500)]
    revisions = [{"tag_id": f"{r.tag_id:08X}", "revision": r.revision, "state": r.state, "purpose": r.purpose,
                  "epoch": r.epoch, "bridge_addr": r.bridge_addr, "layout_bytes": len(r.layout),
                  "delivery_ids": list(r.delivery_ids), "pending": len(r.pending_delivery_ids),
                  "attempts": r.attempts, "last_status": r.last_status, "detail": r.detail,
                  "created_at": r.created_at, "finished_at": r.finished_at}
                 for r in store.list_revisions(limit=300)]
    with db.reading() as conn:
        outbox = [dict(r) for r in conn.execute(
            "SELECT id, kind, credential_id, attempts, dead, last_error, created_at FROM outbox ORDER BY id LIMIT 500")]
        views = [dict(r) for r in conn.execute(
            "SELECT tag_id, credential_id, profile, epoch, clear_required, blocked_reason, displayed_revision,"
            " displayed_digest, dirty, stale_jumps FROM tag_views")]
        commands = [dict(r) for r in conn.execute(
            "SELECT command_id, kind, state, error, created_at, updated_at FROM commands ORDER BY created_at DESC"
            " LIMIT 100")]
    return {"stats": store.queue_stats(), "jobs": jobs, "revisions": revisions, "outbox": outbox,
            "tag_views": views, "commands": commands}


def _inventory(db: Any) -> dict[str, Any]:
    return {"gateways": [dataclasses.asdict(g) for g in db.list_gateways()],
            "bridges": [dataclasses.asdict(b) for b in db.list_bridges()],
            "tags": [dataclasses.asdict(t) for t in db.list_tags()]}


async def _counters(url: str, timeout: float) -> dict[str, Any]:
    from cremind_tag.cli._hardware import connect_gateway

    async with asyncio.timeout(timeout):
        async with connect_gateway(url) as client:
            info = await client.info()
            bridges = await client.get_inventory()
    return {"fw": info.fw, "build": info.build, "boot_id": info.boot_id, "counters": dict(info.counters),
            "bridges": [{"addr": b.addr, "fw": b.fw, "fontpack_id": b.fontpack_id.hex() if b.fontpack_id else None,
                         "counters": dict(b.counters), "assigned": [dataclasses.asdict(a) for a in b.assigned]}
                        for b in bridges]}


@app.command()
def collect(out: Path = typer.Option(..., "--out", dir_okay=False, help="Zip file to write."),
            gateway: str | None = typer.Option(None, "--gateway", help="Gateway port or URL for its counters "
                                                                        "(default: hardware.gateway_url)."),
            counters: bool = typer.Option(True, "--counters/--no-counters",
                                          help="Read the gateway's counters (a short HELLO/INFO; stop the daemon "
                                               "first when it holds a serial port)."),
            log_bytes: int = typer.Option(2_000_000, "--log-bytes", min=0, help="Bytes kept from the end of each "
                                                                                  "log file.")) -> None:
    """Write configuration (no secrets), logs, counters and queue statistics to a zip."""
    import cremind_tag.connector.settings  # noqa: F401  - registered sections appear in config.json
    import cremind_tag.daemon.settings  # noqa: F401
    from cremind_tag.daemon import read_status

    config = load()
    files: dict[str, str] = {}
    try:
        from cremind_tag.secrets import SecretStore

        backend = SecretStore.open(config.ensure_data_dir(), config.secrets.backend).describe()
    except Exception as exc:
        backend = f"unavailable: {exc}"
    files["config.json"] = _dump({"path": str(config.path), "sections": config.as_dict(), "secret_store": backend})
    files["versions.json"] = _dump(_versions())
    files["status.json"] = _dump(read_status(config.data_dir))
    with open_db(config) as db:
        files["queue.json"] = _dump(_queue(db))
        files["inventory.json"] = _dump(_inventory(db))
    url = gateway or config.hardware.gateway_url
    if counters and url:
        try:
            files["counters.json"] = _dump(asyncio.run(_counters(url, 10.0)))
        except Exception as exc:
            files["counters.json"] = _dump({"error": str(exc) or type(exc).__name__})
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        log_dir = config.data_dir / "logs"
        for path in sorted(log_dir.glob("*.log*")) if log_dir.is_dir() else []:
            data = path.read_bytes()
            archive.writestr(f"logs/{path.name}", data[-log_bytes:] if log_bytes else b"")
    console.print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} reports"
                  + (", logs" if log_dir.is_dir() else "") + ")")
