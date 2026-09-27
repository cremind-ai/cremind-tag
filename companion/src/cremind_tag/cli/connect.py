"""`cremind-tag connect` — connect this companion to Cremind (server URL, hardware and content credentials).

::

    cremind-tag connect server https://cremind.example.org [--ca-file cremind-ca.pem]
    cremind-tag connect add-hardware "CremindTag tagc_….<secret>"   # Settings → Tags → Hardware (admin)
    cremind-tag connect add-content  "CremindTag tagc_….<secret>"   # Settings → Tags → Credentials (per profile)
    cremind-tag connect list | test | remove tagc_…

Credential ids are kept in the config file (``[cremind]``); the secrets go to
the secret store (OS keyring, or the 0600 file fallback) and are never printed.
``add-*`` checks the credential with ``GET whoami`` first (``--no-verify`` to
store it offline).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from cremind_tag.cli._hardware import JSON_HELP, console, err_console, fail, load, open_secrets, print_json, table

if TYPE_CHECKING:
    from cremind_tag.config import Config
    from cremind_tag.connector import Credential, WhoAmI

app = typer.Typer(name="connect", help="Connect this companion to Cremind (server URL, hardware and content "
                                       "credentials).", no_args_is_help=True)


def _cremind(config: Config) -> Any:
    from cremind_tag.connector.settings import CremindSettings

    return config.section(CremindSettings)


def _load() -> Config:
    import cremind_tag.connector.settings  # noqa: F401  - registers [cremind] before the file is read

    return load()


async def _whoami(url: str, credential: Credential, ca_file: Path | None, timeout: float = 20.0) -> WhoAmI:
    from cremind_tag.connector import ConnectorClient

    async with ConnectorClient(url, credential, ca_file=ca_file, timeout=timeout) as client:
        return await client.whoami()


def _run_whoami(url: str, credential: Credential, ca_file: Path | None) -> WhoAmI:
    import asyncio

    from cremind_tag.connector import ConnectorError

    try:
        return asyncio.run(_whoami(url, credential, ca_file))
    except ConnectorError as exc:
        fail(str(exc))
    except ValueError as exc:
        fail(str(exc))


def _parse(value: str) -> Credential:
    from cremind_tag.connector import CredentialError, parse_credential

    try:
        return parse_credential(value)
    except CredentialError as exc:
        fail(str(exc))


@app.command()
def server(url: str = typer.Argument(..., help="Cremind base URL, e.g. https://cremind.example.org:1180"),
           ca_file: Path | None = typer.Option(None, "--ca-file", exists=True, dir_okay=False,
                                               help="PEM bundle of a private CA that signed Cremind's certificate."),
           clear_ca: bool = typer.Option(False, "--clear-ca", help="Forget a previously configured CA file."),
           check: bool = typer.Option(True, "--check/--no-check", help="Check that the URL answers.")) -> None:
    """Set the Cremind server URL (and its CA for a private certificate)."""
    import asyncio

    from cremind_tag.config import ConfigError
    from cremind_tag.connector import normalize_base_url

    config = _load()
    try:
        base = normalize_base_url(url)
        config.set("cremind", "url", base)
        if ca_file is not None:
            config.set("cremind", "ca_file", str(ca_file.resolve()))
        elif clear_ca:
            config.set("cremind", "ca_file", None)
    except (ValueError, ConfigError) as exc:
        fail(str(exc))
    settings = _cremind(config)
    if check:
        from cremind_tag.connector import ConnectorAuthError, ConnectorError, Credential

        probe = Credential("tagc_" + "a" * 26, "probe-" + "0" * 20)  # never valid: a 401 proves reachability
        try:
            asyncio.run(_whoami(base, probe, settings.ca_file, timeout=15.0))
        except ConnectorAuthError:
            pass
        except ConnectorError as exc:
            fail(f"{exc}\n(nothing was saved; fix the URL/CA or pass --no-check)")
    path = config.save()
    console.print(f"Cremind server set to [bold]{base}[/bold]"
                  + (f" (CA {settings.ca_file})" if settings.ca_file else "") + f" in {path}")


def _add(kind: str, value: str, verify: bool) -> None:
    config = _load()
    settings = _cremind(config)
    credential = _parse(value)
    profile = None
    if verify:
        if not settings.url:
            fail("set the server first: cremind-tag connect server URL (or pass --no-verify)")
        who = _run_whoami(settings.url, credential, settings.ca_file)
        if who.kind != kind:
            fail(f"{credential.credential_id} is a {who.kind} credential, not a {kind} one "
                 f"(use `cremind-tag connect add-{who.kind}`)")
        profile = who.profile
    secrets = open_secrets(config)
    secrets.set_credential(credential.credential_id, credential.value)
    if kind == "hardware":
        previous = settings.hardware_credential
        config.set("cremind", "hardware_credential", credential.credential_id)
        if previous and previous != credential.credential_id:
            secrets.delete_credential(previous)
            err_console.print(f"[yellow]replaced hardware credential {previous}[/yellow]")
    else:
        ids = [c for c in settings.content_credentials if c != credential.credential_id]
        config.set("cremind", "content_credentials", [*ids, credential.credential_id])
    config.save()
    console.print(f"added {kind} credential [bold]{credential.credential_id}[/bold]"
                  + (f" (profile {profile})" if profile else "") + f" — secret in {secrets.describe()}")
    console.print("restart the daemon to use it (cremind-tag daemon run)")


@app.command("add-hardware")
def add_hardware(value: str = typer.Argument(..., help="The authorization value Cremind showed once: "
                                                       "'CremindTag tagc_….<secret>'."),
                 verify: bool = typer.Option(True, "--verify/--no-verify", help="Check it with GET whoami first.")
                 ) -> None:
    """Store the companion's hardware credential (one per companion)."""
    _add("hardware", value, verify)


@app.command("add-content")
def add_content(value: str = typer.Argument(..., help="The authorization value Cremind showed once: "
                                                      "'CremindTag tagc_….<secret>'."),
                verify: bool = typer.Option(True, "--verify/--no-verify", help="Check it with GET whoami first.")
                ) -> None:
    """Add a content credential (one per profile whose updates this companion delivers)."""
    _add("content", value, verify)


def _rows(config: Config) -> list[dict[str, Any]]:
    settings = _cremind(config)
    secrets = open_secrets(config)
    rows = []
    for kind, ids in (("hardware", [settings.hardware_credential] if settings.hardware_credential else []),
                      ("content", list(settings.content_credentials))):
        for credential_id in ids:
            rows.append({"credential_id": credential_id, "kind": kind,
                         "secret_present": secrets.get_credential(credential_id) is not None})
    return rows


@app.command("list")
def list_credentials(as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """The configured server and credentials (never the secrets)."""
    config = _load()
    settings = _cremind(config)
    rows = _rows(config)
    if as_json:
        print_json({"url": settings.url, "ca_file": str(settings.ca_file) if settings.ca_file else None,
                    "credentials": rows})
        return
    console.print(f"server: {settings.url or '[yellow]not set[/yellow]'}"
                  + (f"  (CA {settings.ca_file})" if settings.ca_file else ""))
    t = table("Credentials", "Credential", "Kind", "Secret")
    for r in rows:
        t.add_row(r["credential_id"], r["kind"], "stored" if r["secret_present"] else "[red]missing[/red]")
    console.print(t if rows else "No credentials (cremind-tag connect add-hardware / add-content).")


@app.command()
def remove(credential_id: str = typer.Argument(..., help="Credential id (tagc_…).")) -> None:
    """Forget a credential here (revoke it in Cremind as well)."""
    config = _load()
    settings = _cremind(config)
    found = False
    if settings.hardware_credential == credential_id:
        config.set("cremind", "hardware_credential", None)
        found = True
    if credential_id in settings.content_credentials:
        config.set("cremind", "content_credentials", [c for c in settings.content_credentials if c != credential_id])
        found = True
    deleted = open_secrets(config).delete_credential(credential_id)
    if not found and not deleted:
        fail(f"{credential_id} is not configured here")
    config.save()
    console.print(f"removed {credential_id}; revoke it in Cremind too if it is no longer needed")


@app.command()
def test(as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """``GET whoami`` with every configured credential."""
    import asyncio

    from cremind_tag.connector import ConnectorError, CredentialError, parse_credential

    config = _load()
    settings = _cremind(config)
    if not settings.url:
        fail("no server configured: cremind-tag connect server URL")
    secrets = open_secrets(config)
    results = []
    for row in _rows(config):
        entry: dict[str, Any] = {**row, "ok": False}
        value = secrets.get_credential(row["credential_id"])
        if value is None:
            entry["error"] = "secret missing from the secret store"
        else:
            try:
                who = asyncio.run(_whoami(settings.url, parse_credential(value), settings.ca_file))
                entry.update(ok=who.kind == row["kind"], profile=who.profile, companion_id=who.companion_id,
                             server_time=who.server_time)
                if who.kind != row["kind"]:
                    entry["error"] = f"Cremind says this is a {who.kind} credential"
            except (ConnectorError, CredentialError, ValueError) as exc:
                entry["error"] = str(exc)
        results.append(entry)
    if as_json:
        print_json(results)
    else:
        t = table(f"whoami @ {settings.url}", "Credential", "Kind", "Result", "Profile / companion")
        for r in results:
            t.add_row(r["credential_id"], r["kind"], "[green]OK[/green]" if r["ok"] else f"[red]{r.get('error')}[/red]",
                      str(r.get("profile") or r.get("companion_id") or ""))
        console.print(t if results else "No credentials configured.")
    if not results or not all(r["ok"] for r in results):
        raise typer.Exit(1)
