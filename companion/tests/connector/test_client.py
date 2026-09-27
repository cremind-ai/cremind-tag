"""The connector client: credentials, typed responses, error mapping, TLS."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from cremind_tag.connector import (
    Backoff,
    ConnectorAuthError,
    ConnectorClient,
    ConnectorConflict,
    ConnectorNotFound,
    ConnectorRejected,
    ConnectorTlsError,
    ConnectorUnavailable,
    Credential,
    CredentialError,
    CursorExpired,
    normalize_base_url,
    parse_credential,
)
from cremind_tag.sim.harness import run_scenario

pytestmark = pytest.mark.timeout(60)


def fake() -> Any:
    from fake_cremind import FakeCremind  # type: ignore[import-not-found]  # loaded by conftest

    return FakeCremind()


def client_for(cremind: Any, cred: Any) -> ConnectorClient:
    return ConnectorClient(cremind.url, Credential(cred.id, cred.secret), transport=cremind.transport)


# -- credentials ---------------------------------------------------------------------------------


def test_parse_credential_forms() -> None:
    secret = "A" * 43
    for text in (f"CremindTag tagc_{'a' * 26}.{secret}", f"Authorization: CremindTag tagc_{'a' * 26}.{secret}",
                 f"  tagc_{'a' * 26}.{secret}  "):
        cred = parse_credential(text)
        assert cred.credential_id == "tagc_" + "a" * 26 and cred.secret == secret
        assert cred.authorization == f"CremindTag tagc_{'a' * 26}.{secret}"
        assert secret not in repr(cred) and secret not in str(cred)
    for bad in ("", "Bearer abc", "tagc_short.secret", f"tagc_{'a' * 26}.", f"tagc_{'a' * 26}.has space in it"):
        with pytest.raises(CredentialError):
            parse_credential(bad)


def test_base_url_normalisation() -> None:
    assert normalize_base_url("cremind.example.org") == "https://cremind.example.org"
    assert normalize_base_url("http://h:1180/") == "http://h:1180"
    assert normalize_base_url("https://h/prefix/api/tag-connector/v1/") == "https://h/prefix"
    with pytest.raises(ValueError):
        normalize_base_url("ftp://h")


def test_backoff_is_bounded_and_jittered() -> None:
    b = Backoff(1.0, 8.0, jitter=0.2)
    delays = [b.next() for _ in range(8)]
    assert 0.8 <= delays[0] <= 1.2 and all(d <= 8.0 * 1.2 for d in delays) and delays[-1] >= 8.0 * 0.8
    b.reset()
    assert b.next() <= 1.2
    assert Backoff(5.0, 1.0).next() <= 1.2  # the first delay never exceeds the cap


# -- requests and typed responses -------------------------------------------------------------------


def test_whoami_sync_events_and_writes() -> None:
    async def scenario() -> None:
        cremind = fake()
        cred = cremind.add_credential("content", "alice")
        cremind.add_tag("1A2B3C4D", owner="alice", epoch=3, bridge_hw_id="br-" + "0" * 32)
        did = cremind.add_job("alice", "1A2B3C4D", title="Hello", ttl_s=60)
        async with client_for(cremind, cred) as client:
            who = await client.whoami()
            assert (who.kind, who.profile, who.companion_id) == ("content", "alice", cremind.companion_id)
            result = await client.sync(None)
            assert not result.cursor_valid and result.head_seq == 1 and result.profile == "alice"
            job = result.outstanding[0]
            assert (job.delivery_id, job.tag_id, job.tag_hw_id, job.epoch) == (did, 0x1A2B3C4D, "1A2B3C4D", 3)
            assert job.expires_at - job.created_at == dt.timedelta(seconds=60)
            assert result.tags[0].bridge_hw_id == "br-" + "0" * 32 and result.settings.language == "en"
            page = await client.events(0)
            assert page.next_after == page.head_seq == 1 and page.jobs[0].card["title"] == "Hello"
            assert (await client.events(1)).jobs == ()
            assert await client.accepted(1, [did]) == 1
            receipt = {"delivery_id": did, "stage": "displayed", "outcome": "displayed", "at": "2026-09-27T10:00:00Z",
                       "tag_id": "1A2B3C4D", "epoch": 3, "revision": 7, "digest": "00" * 8}
            assert (await client.receipts([receipt])).applied == 1
            again = await client.receipts([receipt])  # idempotent: a terminal outcome is final
            assert again.applied == 0 and again.reasons() == {"terminal": [did]}
            other = await client.receipts([{**receipt, "delivery_id": 999999}, {**receipt, "epoch": 9}])
            assert other.reasons() == {"unknown": [999999], "terminal": [did]}
            assert cremind.delivery(did)["stage"] == "displayed"
        header = [r for r in cremind.requests]
        assert header[0][1] == "/whoami"

    run_scenario(scenario())


def test_error_mapping() -> None:
    async def scenario() -> None:
        cremind = fake()
        hardware = cremind.add_credential("hardware")
        content = cremind.add_credential("content", "alice")
        async with client_for(cremind, content) as client:
            with pytest.raises(ConnectorAuthError) as forbidden:
                await client.inventory({})
            assert forbidden.value.status == 403 and forbidden.value.code == "wrong_credential_kind"
            cremind.expire_cursor_once = True
            with pytest.raises(CursorExpired) as expired:
                await client.events(0)
            assert expired.value.stream_id == cremind.streams["alice"].stream_id and expired.value.oldest_seq == 1
            cremind.fail_next["events"] = [503]
            with pytest.raises(ConnectorUnavailable):
                await client.events(0)
            cremind.revoke(content.id)
            with pytest.raises(ConnectorAuthError) as revoked:
                await client.whoami()
            assert revoked.value.code == "credential_revoked"
        async with ConnectorClient(cremind.url, Credential(hardware.id, "wrong-secret-0123456789"),
                                   transport=cremind.transport) as client:
            with pytest.raises(ConnectorAuthError) as invalid:
                await client.whoami()
            assert invalid.value.code == "invalid_credential"
        async with client_for(cremind, hardware) as client:
            cid = cremind.add_command("identify", {"hw_id": "1A2B3C4D"})
            commands = await client.commands(wait=0)
            assert [c.id for c in commands] == [cid] and commands[0].expires_at is not None
            assert (await client.claim(cid)).status == "claimed"
            with pytest.raises(ConnectorConflict) as conflict:
                await client.claim(cid)
            assert conflict.value.code == "already_claimed" and conflict.value.body["command"]["status"] == "claimed"
            with pytest.raises(ConnectorNotFound):
                await client.claim("nope")
            with pytest.raises(ConnectorRejected):
                await client._request("POST", f"/commands/{cid}/result", json={"status": "maybe"})
            assert (await client.result(cid, "succeeded", {"ok": 1}))["ok"] is True
            assert (await client.result(cid, "succeeded", {"ok": 1}))["ok"] is True  # same status: a no-op
            with pytest.raises(ConnectorConflict):
                await client.result(cid, "failed", error="late")

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with ConnectorClient("https://cremind.test", Credential(hardware.id, hardware.secret),
                                   transport=httpx.MockTransport(broken)) as client:
            with pytest.raises(ConnectorUnavailable):
                await client.whoami()

    run_scenario(scenario())


def test_long_poll_returns_early_when_a_command_is_queued() -> None:
    async def scenario() -> None:
        cremind = fake()
        hardware = cremind.add_credential("hardware")
        async with client_for(cremind, hardware) as client:
            poll = asyncio.create_task(client.commands(wait=10))
            await asyncio.sleep(0.2)
            cremind.add_command("collect_diagnostics", {})
            commands = await asyncio.wait_for(poll, 5)
            assert [c.kind for c in commands] == ["collect_diagnostics"]
            assert ("GET", "/commands", None) in cremind.requests

    run_scenario(scenario())


# -- TLS -------------------------------------------------------------------------------------------


def _certificates(tmp: Path) -> tuple[Path, Path, Path]:
    """A private CA and a server certificate for 127.0.0.1 signed by it."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Cremind CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=5))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=5))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = tmp / "ca.pem", tmp / "server.pem", tmp / "server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return ca_path, cert_path, key_path


async def _tls_server(cert: Path, key: Path) -> tuple[asyncio.base_events.Server, int]:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            body = json.dumps({"error": "invalid_credential", "detail": "not valid"}).encode()
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


def test_tls_trust_private_ca_and_moved_to_https(tmp_path: Path) -> None:
    ca, cert, key = _certificates(tmp_path)
    probe = Credential("tagc_" + "a" * 26, "b" * 30)

    async def scenario() -> None:
        server, port = await _tls_server(cert, key)
        async with server:
            async with ConnectorClient(f"https://127.0.0.1:{port}", probe, timeout=5) as client:
                with pytest.raises(ConnectorTlsError, match="not trusted"):
                    await client.whoami()
            async with ConnectorClient(f"https://127.0.0.1:{port}", probe, ca_file=ca, timeout=5) as client:
                with pytest.raises(ConnectorAuthError):  # TLS verified with the private CA; the 401 is the server's
                    await client.whoami()
            async with ConnectorClient(f"http://127.0.0.1:{port}", probe, timeout=5) as client:
                with pytest.raises(ConnectorTlsError, match="now serves HTTPS"):
                    await client.whoami()
        with pytest.raises(ConnectorTlsError, match="does not exist"):
            ConnectorClient("https://127.0.0.1:1", probe, ca_file=tmp_path / "missing.pem")

    run_scenario(scenario())


def test_redirect_to_https_is_a_configuration_error() -> None:
    async def scenario() -> None:
        def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(308, headers={"location": "https://cremind.test/api/tag-connector/v1/whoami"})

        async with ConnectorClient("http://cremind.test", Credential("tagc_" + "a" * 26, "b" * 30),
                                   transport=httpx.MockTransport(redirect)) as client:
            with pytest.raises(ConnectorTlsError, match="https://cremind.test"):
                await client.whoami()

    run_scenario(scenario())


def test_a_handshake_cut_short_is_transient_not_a_tls_error() -> None:
    """Review regression: an EOF or reset during the TLS handshake (a restarting server) stopped the loops."""
    import socket
    import struct

    probe = Credential("tagc_" + "a" * 26, "b" * 30)

    async def scenario() -> None:
        for reset in (False, True):
            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, reset: bool = reset) -> None:
                await reader.read(1)  # the ClientHello started
                sock = writer.get_extra_info("socket")
                if reset and sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.close()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server, ConnectorClient(f"https://127.0.0.1:{port}", probe, timeout=5) as client:
                with pytest.raises(ConnectorUnavailable):
                    await client.whoami()

    run_scenario(scenario())


def test_timestamps_with_and_without_milliseconds() -> None:
    from cremind_tag.connector.models import ReceiptsResult, parse_time

    whole = parse_time("2026-09-27T10:00:00Z")
    milli = parse_time("2026-09-27T10:00:00.123Z")
    assert (milli - whole).total_seconds() == pytest.approx(0.123)
    assert parse_time("2026-09-27T10:00:00.123456+00:00") > milli
    assert parse_time(1790503200123).microsecond == 123000
    assert ReceiptsResult.from_json({"applied": 2}).rejected == ()  # an older Cremind: no `rejected`
    parsed = ReceiptsResult.from_json({"applied": 0, "rejected": [{"delivery_id": 5, "reason": "not_owned"}, "x"]})
    assert parsed.reasons() == {"not_owned": [5]}
