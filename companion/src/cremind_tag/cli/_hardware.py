"""Shared plumbing of the hardware commands (`gateway`, `mesh`, `bridge`, `tag`, `sim`).

Heavy modules are imported inside functions so `cremind-tag --help` stays fast.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from cremind_tag.config import Config
    from cremind_tag.gateway import GatewayClient
    from cremind_tag.secrets import SecretStore
    from cremind_tag.store import Database

console = Console()
err_console = Console(stderr=True)

URL_HELP = "Serial port or pyserial URL (COM7, /dev/ttyACM0, socket://127.0.0.1:7777). Default: config."
JSON_HELP = "Print machine-readable JSON."


def fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def load() -> Config:
    from cremind_tag.config import ConfigError, load_config

    try:
        return load_config()
    except ConfigError as exc:
        fail(str(exc))


def open_db(config: Config | None = None) -> Database:
    """The companion database with every schema version this build knows (inventory + delivery queue)."""
    from cremind_tag.daemon.schema import open_database
    from cremind_tag.store import SchemaError

    config = config or load()
    try:
        return open_database(config.ensure_data_dir() / "companion.sqlite3")
    except SchemaError as exc:
        fail(str(exc))


def open_secrets(config: Config | None = None) -> SecretStore:
    from cremind_tag.secrets import SecretStore, SecretStoreError

    config = config or load()
    try:
        return SecretStore.open(config.ensure_data_dir(), config.secrets.backend)
    except SecretStoreError as exc:
        fail(str(exc))


def gateway_url(url: str | None, config: Config | None = None) -> str:
    resolved = url or (config or load()).hardware.gateway_url
    if not resolved:
        fail("no gateway port: pass --url or set hardware.gateway_url (env CREMIND_TAG_GATEWAY_URL)")
    return resolved


def bridge_url(url: str | None, config: Config | None = None) -> str:
    resolved = url or (config or load()).hardware.bridge_url
    if not resolved:
        fail("no bridge maintenance port: pass --url or set hardware.bridge_url (env CREMIND_TAG_BRIDGE_URL)")
    return resolved


def gateway_hw_id(url: str) -> str:
    """Stable id for the inventory: from the USB serial number when the port has one, else from the URL."""
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            if port.device.lower() == url.lower() and port.serial_number:
                return f"gw-{uuid.uuid5(uuid.NAMESPACE_URL, 'usb-serial:' + port.serial_number)}"
    except Exception:
        pass
    return f"gw-{uuid.uuid5(uuid.NAMESPACE_URL, url)}"


def run[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run an async command body; hardware errors become a one-line message and exit code 1."""
    from cremind_tag.gateway import GatewayError
    from cremind_tag.secrets import SecretStoreError
    from cremind_tag.store import NotFoundError

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (GatewayError, SecretStoreError, NotFoundError, TimeoutError, ValueError) as exc:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, TimeoutError):
            message = "timed out waiting for the device"
        fail(message)


def record_gateway(db: Database, url: str, client: GatewayClient) -> None:
    """Remember the connected gateway in the inventory (port, boot id, firmware)."""
    from cremind_tag.store import GatewayRecord

    hello = client.hello_info
    if hello is None:
        return
    board = hello.caps.board if isinstance(hello.caps.board, int) else None
    db.upsert_gateway(GatewayRecord(gateway_hw_id(url), port=url, boot_id=hello.boot_id, fw=hello.fw,
                                    build=hello.build, board=board))


def connect_gateway(url: str, **options: Any) -> GatewayClient:
    """A CLI client: no reconnects, never acknowledges events unless a handler is added."""
    from cremind_tag.gateway import GatewayClient

    options.setdefault("reconnect", False)
    return GatewayClient(url, name="cremind-tag cli", **options)


def print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(_plain(data), indent=2, default=_json_default) + "\n")


def _plain(value: Any) -> Any:
    """Enum members by name (json would write IntEnums as bare numbers), recursively."""
    import enum

    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


async def bridge_fontpack(client: GatewayClient, addr: int, timeout: float = 10.0) -> bytes | None:
    """The active font pack id of a bridge, asking the gateway to refresh its info if needed."""
    from cremind_tag.gateway import BridgeInfoEvent

    def usable(pack: bytes | None) -> bool:
        return bool(pack) and any(pack or b"")

    with client.expect(lambda e: isinstance(e, BridgeInfoEvent) and e.info.addr == addr) as waiter:
        item = next((b for b in await client.get_inventory() if b.addr == addr), None)
        if item is not None and usable(item.fontpack_id):
            return item.fontpack_id
        try:
            event = await waiter.wait(timeout)
        except TimeoutError:
            return None
    assert isinstance(event, BridgeInfoEvent)
    return event.info.fontpack_id if usable(event.info.fontpack_id) else None


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if hasattr(value, "name") and hasattr(value, "value"):  # IntEnum
        return value.name
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses

        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value) if f.name != "raw"}
    return str(value)


def table(title: str | None, *columns: str) -> Table:
    t = Table(title=title, title_justify="left", header_style="bold")
    for column in columns:
        t.add_column(column)
    return t


def status_text(status: Any) -> str:
    name = getattr(status, "name", str(status))
    color = "green" if name in ("OK", "ACCEPTED") else "yellow" if name in ("DUPLICATE", "BUSY") else "red"
    return f"[{color}]{name}[/{color}]"
