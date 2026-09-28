"""The cremind-connect://setup launch link (docs/connect-setup.md §11.4)."""

from __future__ import annotations

import pytest

from cremind_tag.connect.links import LinkError, parse_setup_link, redact, validate_origin

SESSION = "3f2b8c1e-5a4d-4e6f-9a0b-1c2d3e4f5a6b"
TOKEN = "tok_Ab-cd_ef0123456789XYZ"
PIN = "A" * 64


def link(**overrides: str | None) -> str:
    params = {"v": "1", "server": "https://cremind.example.org", "session": SESSION, "token": TOKEN}
    params.update({k: v for k, v in overrides.items() if v is not None})
    for key in [k for k, v in overrides.items() if v is None]:
        params.pop(key, None)
    from urllib.parse import urlencode

    return "cremind-connect://setup?" + urlencode(params)


def test_a_good_link() -> None:
    parsed = parse_setup_link(link())
    assert parsed.version == 1 and parsed.server == "https://cremind.example.org"
    assert parsed.session == SESSION and parsed.token == TOKEN and parsed.pin is None
    assert TOKEN not in repr(parsed)
    assert parse_setup_link(parsed.to_url()) == parsed


def test_pin_port_and_normalisation() -> None:
    parsed = parse_setup_link(link(server="HTTPS://Cremind.Example.ORG:1180", pin=PIN,
                                   session=SESSION.upper()))
    assert parsed.server == "https://cremind.example.org:1180" and parsed.pin == "a" * 64
    assert parsed.session == SESSION


@pytest.mark.parametrize(("origin", "expected"), [
    ("http://localhost:1112", "http://localhost:1112"),
    ("https://cremind.example.org:443", "https://cremind.example.org"),
    ("http://192.168.1.20", "http://192.168.1.20"),
    ("https://[::1]:8443", "https://[::1]:8443"),
    ("http://nas.local:80", "http://nas.local"),
])
def test_origins(origin: str, expected: str) -> None:
    assert validate_origin(origin) == expected


def test_a_trailing_slash_after_setup_and_extra_parameters_are_tolerated() -> None:
    assert parse_setup_link(link().replace("://setup?", "://setup/?")).session == SESSION
    assert parse_setup_link(link() + "&future=1").session == SESSION


@pytest.mark.parametrize(("url", "code"), [
    ("", "empty"),
    ("https://cremind.example.org/?v=1", "wrong_scheme"),
    ("cremind-connect://pair?v=1", "unknown_action"),
    ("cremind-connect://setup/extra?v=1", "unknown_action"),
    (link(v="2"), "unsupported_version"),
    (link(v=None), "malformed"),
    (link() + "#frag", "malformed"),
    (link() + "&token=again1234567890abcd", "malformed"),
    (link(server="ftp://cremind.example.org"), "bad_server"),
    (link(server="https://cremind.example.org/"), "bad_server"),
    (link(server="https://cremind.example.org/api"), "bad_server"),
    (link(server="https://user:pw@cremind.example.org"), "bad_server"),
    (link(server="https://cremind.example.org?x=1"), "bad_server"),
    (link(server="https://cremind.example.org:99999"), "bad_server"),
    (link(server="https://cremind.example.org:"), "bad_server"),
    (link(server="https://bad_host.example"), "bad_server"),
    (link(server="https://"), "bad_server"),
    (link(server=None), "bad_server"),
    (link(server="https://exämple.org"), "bad_server"),
    (link(session="not-a-uuid"), "bad_session"),
    (link(session="urn:uuid:" + SESSION), "bad_session"),
    (link(token="short"), "bad_token"),
    (link(token="has space in it 1234567"), "bad_token"),
    (link(token=None), "bad_token"),
    (link(pin="abc"), "bad_pin"),
    (link(server="http://cremind.example.org", pin="b" * 64), "bad_pin"),
    ("cremind-connect://setup?v=1&server=https%3A%2F%2Fa.example\x00", "malformed"),
    ("cremind-connect://setup?" + "a" * 5000, "too_long"),
])
def test_bad_links_are_refused_with_a_reason(url: str, code: str) -> None:
    with pytest.raises(LinkError) as info:
        parse_setup_link(url)
    assert info.value.code == code
    assert str(info.value)  # a sentence a person can read


def test_redact_hides_the_token() -> None:
    assert TOKEN not in redact(link()) and "session=" in redact(link())
