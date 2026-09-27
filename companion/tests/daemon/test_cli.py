"""The `connect`, `daemon`, `queue`, `doctor` and `diag` commands against the fake Cremind (real HTTP)
and a simulator running in a thread."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cremind_tag.cli.main import app
from cremind_tag.sim import SimulatorThread
from cremind_tag.sim.harness import make_config

pytestmark = pytest.mark.timeout(180)

REPO = Path(__file__).resolve().parents[3]
DEV_PACK = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dev_fonts: Any) -> Path:
    monkeypatch.setenv("CREMIND_TAG_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CREMIND_TAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CREMIND_TAG_SECRETS_BACKEND", "file")
    monkeypatch.setenv("CREMIND_TAG_DAEMON_ACTIVE_POLL_S", "0.05")
    monkeypatch.setenv("CREMIND_TAG_DAEMON_IDLE_POLL_S", "0.2")
    monkeypatch.setenv("CREMIND_TAG_DAEMON_SCAN_INTERVAL_S", "0.1")
    monkeypatch.setenv("CREMIND_TAG_DAEMON_COMMAND_WAIT_S", "1")
    return tmp_path / "data"


@pytest.fixture()
def server() -> Iterator[Any]:
    from fake_server import FakeCremindServer  # type: ignore[import-not-found]  # loaded by conftest

    with FakeCremindServer() as srv:
        yield srv


@pytest.fixture()
def sim(env: Path) -> Iterator[SimulatorThread]:
    config = make_config(fontpack=DEV_PACK.read_bytes(), seed=81, tags=1)
    with SimulatorThread(config) as thread:
        yield thread


def cli(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(app, list(args), catch_exceptions=False)
    assert result.exit_code == code, result.output
    return result


def cli_json(*args: str) -> Any:
    return json.loads(cli(*args, "--json").stdout)


def test_connect_commands(env: Path, server: Any) -> None:
    hardware = server.call(lambda f: f.add_credential("hardware"))
    content = server.call(lambda f: f.add_credential("content", "alice"))
    cli("connect", "add-content", content.authorization, code=1)  # no server yet
    cli("connect", "server", server.url)
    cli("connect", "add-hardware", content.authorization, code=1)  # wrong kind is refused
    out = cli("connect", "add-hardware", hardware.authorization).stdout
    assert hardware.id in out and hardware.secret not in out
    assert "profile alice" in cli("connect", "add-content", f"Authorization: {content.authorization}").stdout
    listed = cli_json("connect", "list")
    assert listed["url"] == server.url
    assert [(c["credential_id"], c["kind"], c["secret_present"]) for c in listed["credentials"]] == [
        (hardware.id, "hardware", True), (content.id, "content", True)]
    assert all(r["ok"] for r in cli_json("connect", "test"))
    server.call(lambda f: f.revoke(content.id))
    result = CliRunner().invoke(app, ["connect", "test", "--json"])
    assert result.exit_code == 1 and "credential_revoked" in result.stdout
    cli("connect", "remove", content.id)
    assert [c["credential_id"] for c in cli_json("connect", "list")["credentials"]] == [hardware.id]
    config_text = (env.parent / "config.toml").read_text(encoding="utf-8")
    assert hardware.id in config_text and hardware.secret not in config_text
    cli("connect", "server", "http://127.0.0.1:9", code=1)  # unreachable: refused, nothing saved


def test_daemon_once_queue_doctor_diag(env: Path, server: Any, sim: SimulatorThread) -> None:
    from cremind_tag.cli.sim import _register

    sim.call(_register)
    tag = sim.call(lambda s: next(iter(s.tags)))
    bridge_hw = sim.call(lambda s: f"br-{s.bridges[0].uuid.hex()}")
    hardware = server.call(lambda f: f.add_credential("hardware"))
    content = server.call(lambda f: f.add_credential("content", "alice"))
    server.call(lambda f: f.add_tag(f"{tag:08X}", owner="alice", epoch=1, bridge_hw_id=bridge_hw))
    first = server.call(lambda f: f.add_job("alice", f"{tag:08X}", title="From the CLI daemon"))
    cli("connect", "server", server.url)
    cli("connect", "add-hardware", hardware.authorization)
    cli("connect", "add-content", content.authorization)

    out = cli("daemon", "run", "--once", "--gateway", sim.gateway_url, "--pack", str(DEV_PACK),
              "--max-seconds", "60").stdout
    assert "done:" in out
    assert server.call(lambda f: f.delivery(first)["stage"]) == "displayed"
    status = cli_json("daemon", "status")
    assert status["running"] is False and status["queue"]["depth"] == 0
    assert status["daemon"]["credentials"][content.id]["profile"] == "alice"
    assert (env / "logs" / "daemon.log").is_file()

    jobs = cli_json("queue", "list")["jobs"]
    assert [(j["delivery_id"], j["outcome"]) for j in jobs] == [(first, "displayed")]
    shown = cli_json("queue", "show", str(first))
    assert shown["revisions"][0]["state"] == "displayed"
    second = server.call(lambda f: f.add_job("alice", f"{tag:08X}", title="To be cancelled", ttl_s=600))
    cli("daemon", "run", "--once", "--gateway", sim.gateway_url, "--pack", str(DEV_PACK))
    assert server.call(lambda f: f.delivery(second)["stage"]) == "displayed"
    third = server.call(lambda f: f.add_job("alice", f"{tag:08X}", title="Cancelled here"))
    sim.call(lambda s: setattr(s.tag(tag), "out_of_range", True))
    cli("daemon", "run", "--once", "--gateway", sim.gateway_url, "--pack", str(DEV_PACK), "--max-seconds", "5",
        code=1)  # the tag is out of range: not caught up
    cli("queue", "cancel", str(third), "--yes")
    assert "retrying tag" in cli("queue", "retry", "--tag", f"{tag:08X}").stdout
    sim.call(lambda s: setattr(s.tag(tag), "out_of_range", False))
    cli("daemon", "run", "--once", "--gateway", sim.gateway_url, "--pack", str(DEV_PACK))
    assert server.call(lambda f: f.delivery(third)["stage"]) == "cancelled"
    assert "purged" in cli("queue", "purge", "--older-than", "0s", "--yes").stdout

    report = json.loads(CliRunner().invoke(app, ["doctor", "--gateway", sim.gateway_url, "--pack", str(DEV_PACK),
                                                 "--json"]).stdout)
    checks = {c["check"]: c for c in report["checks"]}
    assert checks["cremind"]["status"] == "warn"  # reachable, but plain HTTP
    assert checks[f"credential {content.id}"]["status"] == "ok"
    assert checks["gateway"]["status"] == "ok" and checks["font pack"]["status"] == "ok"
    assert checks["database"]["status"] == "ok"
    assert [c for c in report["checks"] if c["check"].startswith("bridge ")][0]["status"] == "ok"

    archive = env.parent / "diag.zip"
    cli("diag", "collect", "--out", str(archive), "--gateway", sim.gateway_url)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        assert {"config.json", "versions.json", "status.json", "queue.json", "inventory.json",
                "counters.json", "logs/daemon.log"} <= names
        blob = b"".join(z.read(n) for n in names)
    for secret in (hardware.secret, content.secret):
        assert secret.encode() not in blob
    assert json.loads(zipfile.ZipFile(archive).read("counters.json"))["counters"]
