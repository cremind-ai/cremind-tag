"""`cremind-tag doctor` — check the installation: Cremind reachability, credentials, gateway, J-Link, font pack.

Each check reports ``ok``, ``warn`` or ``fail`` with a one-line explanation;
the command exits 1 when any check fails. Checks:

- configuration file and data directory; the database (``PRAGMA
  integrity_check``, schema version);
- the secret store backend (an OS keyring or the 0600 file);
- Cremind reachability and TLS (an unauthenticated ``whoami`` must answer 401);
- every configured credential (``whoami``: kind, profile, companion);
- the gateway (``HELLO`` + ``INFO``), the bridges' active font pack ids;
- the font pack (format, ``FontSet`` with verified font files) and that every
  bridge reports the same pack id;
- an SWD tool for enrollment (nrfutil / nrfjprog / J-Link).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer

from cremind_tag.cli._hardware import JSON_HELP, console, print_json, table

app = typer.Typer(name="doctor", help="Check the installation: Cremind reachability, credentials, gateway, J-Link, "
                                      "font pack.", invoke_without_command=True)


class Report:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, check: str, status: str, detail: str) -> None:
        self.items.append({"check": check, "status": status, "detail": detail})

    @property
    def failed(self) -> bool:
        return any(i["status"] == "fail" for i in self.items)


def _config_checks(report: Report) -> Any:
    import cremind_tag.connector.settings  # noqa: F401
    import cremind_tag.daemon.settings  # noqa: F401
    from cremind_tag.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        report.add("config", "fail", str(exc))
        return None
    report.add("config", "ok", f"{config.path}{'' if config.path.exists() else ' (not created yet: defaults)'}")
    try:
        data_dir = config.ensure_data_dir()
        probe = data_dir / ".doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add("data dir", "ok", str(data_dir))
    except OSError as exc:
        report.add("data dir", "fail", f"{config.data_dir} is not writable: {exc}")
    return config


def _database_check(report: Report, config: Any) -> None:
    from cremind_tag.daemon.schema import open_database
    from cremind_tag.store import SchemaError

    try:
        with open_database(config.db_path) as db, db.reading() as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = db.schema_version
    except (SchemaError, OSError) as exc:
        report.add("database", "fail", str(exc))
        return
    except Exception as exc:  # sqlite3.DatabaseError and friends
        report.add("database", "fail", f"{config.db_path}: {exc}")
        return
    report.add("database", "ok" if result == "ok" else "fail", f"{config.db_path} schema v{version}, "
               f"integrity {result}")


def _secrets_check(report: Report, config: Any) -> Any:
    from cremind_tag.secrets import SecretStore, SecretStoreError, keyring_usable

    usable, why = keyring_usable()
    try:
        store = SecretStore.open(config.ensure_data_dir(), config.secrets.backend)
    except SecretStoreError as exc:
        report.add("secret store", "fail", str(exc))
        return None
    status = "ok" if store.backend_name == "keyring" or config.secrets.backend == "file" else "warn"
    detail = store.describe() + ("" if usable else f" — keyring: {why}")
    report.add("secret store", status, detail)
    return store


async def _cremind_checks(report: Report, config: Any, secrets: Any) -> None:
    from cremind_tag.connector import (
        ConnectorAuthError,
        ConnectorClient,
        ConnectorError,
        CremindSettings,
        Credential,
        parse_credential,
    )

    settings = config.section(CremindSettings)
    if not settings.url:
        report.add("cremind", "warn", "no server configured (cremind-tag connect server URL)")
        return
    probe = Credential("tagc_" + "a" * 26, "doctor-" + "0" * 20)
    try:
        async with ConnectorClient(settings.url, probe, ca_file=settings.ca_file, timeout=15) as client:
            await client.whoami()
        report.add("cremind", "warn", f"{settings.url} accepted an invalid credential?!")
    except ConnectorAuthError:
        scheme = "TLS verified" if settings.url.startswith("https://") else "plain HTTP (no TLS)"
        report.add("cremind", "ok" if settings.url.startswith("https://") else "warn",
                   f"{settings.url} reachable, {scheme}" + (f", CA {settings.ca_file}" if settings.ca_file else ""))
    except (ConnectorError, ValueError) as exc:
        report.add("cremind", "fail", str(exc))
        return
    ids = ([("hardware", settings.hardware_credential)] if settings.hardware_credential else []) + \
        [("content", c) for c in settings.content_credentials]
    if not ids:
        report.add("credentials", "warn", "none configured (cremind-tag connect add-hardware / add-content)")
    for kind, credential_id in ids:
        value = secrets.get_credential(credential_id) if secrets is not None else None
        if value is None:
            report.add(f"credential {credential_id}", "fail", "secret missing from the secret store")
            continue
        try:
            async with ConnectorClient(settings.url, parse_credential(value), ca_file=settings.ca_file,
                                       timeout=15) as client:
                who = await client.whoami()
        except (ConnectorError, ValueError) as exc:
            report.add(f"credential {credential_id}", "fail", str(exc))
            continue
        if who.kind != kind:
            report.add(f"credential {credential_id}", "fail", f"configured as {kind}, Cremind says {who.kind}")
        else:
            report.add(f"credential {credential_id}", "ok",
                       f"{kind}, companion {who.companion_id}" + (f", profile {who.profile}" if who.profile else ""))


def _fontpack_check(report: Report, pack: Path | None) -> bytes | None:
    if pack is None:
        report.add("font pack", "warn", "none configured (hardware.fontpack or --pack): the daemon cannot compose")
        return None
    from cremind_tag.fontpack.format import FontPack, FontPackError

    try:
        parsed = FontPack(pack.read_bytes())
    except (OSError, FontPackError) as exc:
        report.add("font pack", "fail", f"{pack}: {exc}")
        return None
    try:
        from cremind_tag.fonts.fontset import FontSet

        FontSet.load(pack)
        detail = "font files verified"
    except Exception as exc:
        report.add("font pack", "fail", f"{pack} (pack {parsed.pack_id.hex()}): {exc} — run `cremind-tag fonts fetch`")
        return parsed.pack_id
    report.add("font pack", "ok", f"{pack} (pack {parsed.pack_id.hex()}, {len(parsed.faces)} faces, {detail})")
    return parsed.pack_id


async def _gateway_check(report: Report, url: str | None, pack_id: bytes | None, timeout: float) -> None:
    if not url:
        report.add("gateway", "warn", "none configured (hardware.gateway_url or --gateway)")
        return
    from cremind_tag.cli._hardware import connect_gateway
    from cremind_tag.gateway import GatewayError

    try:
        async with asyncio.timeout(timeout):
            async with connect_gateway(url) as client:
                info = await client.info()
                bridges = await client.get_inventory()
    except (GatewayError, OSError, TimeoutError) as exc:
        report.add("gateway", "fail", f"{url}: {exc or type(exc).__name__}")
        return
    report.add("gateway", "ok", f"{url}: fw {info.fw} ({info.build}), boot {info.boot_id:08x}, "
                                f"{len(bridges)} bridge(s)")
    for bridge in bridges:
        active = bridge.fontpack_id.hex() if bridge.fontpack_id and any(bridge.fontpack_id) else None
        name = f"bridge {bridge.addr:#06x}"
        if active is None:
            report.add(name, "warn", "reports no font pack yet (it answers after its next CAPS)")
        elif pack_id is not None and active != pack_id.hex():
            report.add(name, "fail", f"active pack {active} ≠ the companion's {pack_id.hex()} (install_fontpack)")
        else:
            report.add(name, "ok", f"fw {bridge.fw}, pack {active}")


def _swd_check(report: Report) -> None:
    from cremind_tag.enroll.tools import INSTALL_HINT, detect_tools

    try:
        tools = detect_tools()
    except Exception as exc:
        report.add("swd tool", "warn", f"detection failed: {exc}")
        return
    if tools:
        report.add("swd tool", "ok", ", ".join(f"{t.name} ({t.path})" for t in tools))
    else:
        report.add("swd tool", "warn", f"none found — needed only to enroll tags: {INSTALL_HINT}")


@app.callback()
def doctor(ctx: typer.Context,
           gateway: str | None = typer.Option(None, "--gateway", help="Gateway port or URL (default: config)."),
           pack: Path | None = typer.Option(None, "--pack", help="Font pack (default: hardware.fontpack)."),
           timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Seconds for the gateway check."),
           as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Run every check and print a report (exit 1 when a check fails)."""
    if ctx.invoked_subcommand is not None:
        return
    report = Report()
    config = _config_checks(report)
    if config is not None:
        _database_check(report, config)
        secrets = _secrets_check(report, config)
        asyncio.run(_cremind_checks(report, config, secrets))
        pack_id = _fontpack_check(report, pack or config.hardware.fontpack)
        asyncio.run(_gateway_check(report, gateway or config.hardware.gateway_url, pack_id, timeout))
    _swd_check(report)
    if as_json:
        print_json({"ok": not report.failed, "checks": report.items})
    else:
        t = table("cremind-tag doctor", "Check", "Result", "Detail")
        colors = {"ok": "green", "warn": "yellow", "fail": "red"}
        for item in report.items:
            color = colors[item["status"]]
            t.add_row(item["check"], f"[{color}]{item['status']}[/{color}]", item["detail"])
        console.print(t)
    if report.failed:
        raise typer.Exit(1)


__all__ = ["app"]
