"""The hardware CLI (`gateway`, `mesh`, `bridge`, `tag`) against a simulator running in a thread."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cremind_tag.cli.main import app
from cremind_tag.fontpack.format import FontPack
from cremind_tag.sim import BridgeSpec, SimulatorThread
from cremind_tag.sim.harness import make_config
from cremind_tag.daemon.schema import open_database

pytestmark = pytest.mark.timeout(180)

REPO = Path(__file__).resolve().parents[3]
PACK = REPO / "protocol" / "fixtures" / "fontpack_test.ctfp"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CREMIND_TAG_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CREMIND_TAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CREMIND_TAG_SECRETS_BACKEND", "file")
    return tmp_path / "data"


@pytest.fixture()
def sim(env: Path) -> Iterator[SimulatorThread]:
    config = make_config(fontpack=PACK.read_bytes(), seed=71, tags=1)
    config.bridges.append(BridgeSpec(provisioned=False, name="spare"))
    with SimulatorThread(config) as thread:
        yield thread


def cli(*args: str) -> Any:
    result = CliRunner().invoke(app, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def cli_json(*args: str) -> Any:
    return json.loads(cli(*args, "--json").stdout)


def test_gateway_and_mesh_commands(sim: SimulatorThread, env: Path) -> None:
    url = sim.gateway_url
    info = cli_json("gateway", "info", "--url", url)
    assert info["boot_id"] == sim.call(lambda s: s.gateway.boot_id) and info["caps"]["credits"] == 4
    assert "hellos" in cli_json("gateway", "counters", "--url", url)
    nodes = cli_json("mesh", "nodes", "--url", url)
    assert [n["configured"] for n in nodes] == [True]
    with open_database(env / "companion.sqlite3") as db:
        assert [b.addr for b in db.list_bridges()] == [nodes[0]["addr"]]
        assert len(db.list_gateways()) == 1

    spare = sim.call(lambda s: s.bridge(1).uuid.hex())
    found = cli_json("mesh", "scan", "--url", url, "--duration", "2")
    assert [f["uuid"] for f in found] == [spare]
    out = cli("mesh", "provision", spare, "--name", "spare", "--url", url).stdout
    assert "provisioned" in out and "OK" in out
    with open_database(env / "companion.sqlite3") as db:
        bridge = db.get_bridge(uuid=spare)
        assert bridge.configured and bridge.name == "spare"
    assert "OK" in cli("mesh", "identify", hex(bridge.addr or 0), "--url", url).stdout
    assert "OK" in cli("mesh", "remove", hex(bridge.addr or 0), "--yes", "--url", url).stdout
    with open_database(env / "companion.sqlite3") as db:
        assert db.find_bridge(uuid=spare) is None


def test_tag_commands(sim: SimulatorThread, env: Path) -> None:
    from cremind_tag.cli.sim import _register

    url = sim.gateway_url
    sim.call(_register)
    tags = cli_json("tag", "list")
    assert len(tags) == 1 and tags[0]["epoch"] == 1
    tag_id = tags[0]["tag_id"]
    shown = cli_json("tag", "show", tag_id)
    assert shown["secret_present"] and "secret" not in {k for k in shown if k != "secret_ref" and k != "secret_present"}
    addr = hex(shown["bridge_addr"])

    out = cli("tag", "assign", tag_id, "--bridge", addr, "--epoch", "2", "--url", url).stdout
    assert "OK" in out
    assert cli_json("tag", "show", tag_id)["epoch"] == 2
    assert "OK" in cli("tag", "command", "identify", tag_id, "--url", url).stdout
    assert cli_json("tag", "show", tag_id)["last_revision"] == 1
    assert "OK" in cli("tag", "command", "clear", tag_id, "--url", url).stdout
    shown_tag = int(tag_id, 16)
    assert sim.call(lambda s: s.tag(shown_tag).nvs.stored_epoch) == 2
    assert sim.call(lambda s: s.tag(shown_tag).stats["refreshes"]) == 2


def test_bridge_maintenance_commands(sim: SimulatorThread) -> None:
    url = sim.call(lambda s: s.bridge_url(0))
    status = cli_json("bridge", "status", "--url", url)
    assert status["fontpack_id"] == FontPack(PACK.read_bytes()).pack_id.hex()
    assert "already active" in cli("bridge", "fonts-install", str(PACK), "--url", url).stdout
    assert "active in slot 1" in cli("bridge", "fonts-install", str(PACK), "--url", url, "--force").stdout
    result = cli_json("bridge", "flash-test", "--url", url, "--yes")
    assert {item["status"] for item in result["items"]} <= {"OK", "BUSY"}
    assert cli_json("bridge", "info", "--url", url)["flash_size"] == 64 << 20


def test_enroll_dry_run(env: Path, tmp_path: Path) -> None:
    out = cli("tag", "enroll", "--board", "laowu_bwr", "--dry-run", "--tool", "nrfjprog", "--tag-id", "1A2B3C4D",
              "--out", str(tmp_path / "enroll")).stdout
    assert "1A2B3C4D" in out and "nrfjprog" in out
    hex_file = tmp_path / "enroll" / "1A2B3C4D-uicr.hex"
    assert hex_file.exists()
    assert cli_json("tag", "list") == []  # a dry run registers nothing


def test_missing_gateway_url_is_a_clear_error(env: Path) -> None:
    result = CliRunner().invoke(app, ["gateway", "info"])
    assert result.exit_code == 1 and "no gateway port" in result.output
