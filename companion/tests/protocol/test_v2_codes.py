"""Setup codes (connect-setup.md 2.2) and device identity (2.1)."""

from __future__ import annotations

import pytest

from cremind_tag.protocol.ids import SETUP_CODE_LEN, NodeRole
from cremind_tag.secure import identity
from cremind_tag.secure.codes import ALPHABET, SetupCodeError, SetupPayload, format_code, normalize, parse_code

SECRET = bytes(range(10))


def test_round_trip_grouped_and_qr() -> None:
    p = SetupPayload(NodeRole.TAG, 0x1A2B3C4D, SECRET)
    code = p.code()
    assert len(code) == SETUP_CODE_LEN + 4 and code.count("-") == 4
    assert all(len(g) == 5 for g in code.split("-"))
    assert parse_code(code) == p
    assert parse_code(p.qr_text()) == p
    assert p.qr_text().startswith("CTAG:") and "-" not in p.qr_text()
    assert parse_code(code.lower().replace("-", " ")) == p


def test_payload_layout() -> None:
    raw = SetupPayload(NodeRole.BRIDGE, 0x01020304, SECRET).pack()
    assert raw == bytes([0x22, 0x04, 0x03, 0x02, 0x01]) + SECRET


def test_crockford_aliases() -> None:
    p = SetupPayload(NodeRole.TAG, 0, bytes(10))  # all-zero payload body -> many '0' symbols
    code = format_code(p.pack(), grouped=False)
    assert "0" in code
    assert parse_code(code.replace("0", "O", 3)) == p
    assert normalize("ctag:ab-cd il") == "ABCD11"


@pytest.mark.parametrize("short_id", [0xDEADBEEF, 0, 0x01020304])
def test_every_single_substitution_and_adjacent_swap_is_caught(short_id: int) -> None:
    code = format_code(SetupPayload(NodeRole.TAG, short_id, SECRET).pack(), grouped=False)
    for position in range(SETUP_CODE_LEN):
        for replacement in ALPHABET:
            if replacement == code[position]:
                continue
            with pytest.raises(SetupCodeError):
                parse_code(code[:position] + replacement + code[position + 1:])
    for position in range(SETUP_CODE_LEN - 1):
        a, b = code[position], code[position + 1]
        if a == b:
            continue
        with pytest.raises(SetupCodeError):
            parse_code(code[:position] + b + a + code[position + 2:])


def test_wrong_role_length_and_unknown_characters() -> None:
    p = SetupPayload(NodeRole.BRIDGE, 7, SECRET)
    with pytest.raises(SetupCodeError) as info:
        parse_code(p.code(), role=NodeRole.TAG)
    assert info.value.code == "setup_code_wrong_role"
    with pytest.raises(SetupCodeError) as info:
        parse_code(p.code()[:-2])
    assert info.value.code == "setup_code_invalid"
    with pytest.raises(SetupCodeError):
        parse_code("U" * SETUP_CODE_LEN)


def test_v1_or_gateway_payloads_are_refused() -> None:
    with pytest.raises(SetupCodeError):
        SetupPayload(NodeRole.GATEWAY, 1, SECRET)
    bad = bytes([0x13]) + bytes(14)  # version nibble 1
    with pytest.raises(SetupCodeError) as info:
        SetupPayload.unpack(bad)
    assert info.value.code == "setup_code_unsupported"


def test_repr_never_shows_the_secret() -> None:
    p = SetupPayload(NodeRole.TAG, 1, bytes([0xAB]) * 10)
    assert "ab" not in repr(p).lower().replace("tag", "")


def test_device_id_and_short_id() -> None:
    ik = bytes(range(32))
    dev = identity.device_id(3, ik)
    assert len(dev) == 16
    assert dev != identity.device_id(2, ik)  # the role is bound in
    assert identity.short_id(dev) == int.from_bytes(dev[:4], "little")
    assert identity.short_id(b"\x00\x00\x00\x00" + bytes(12)) == 0x5A5A5A5A
    assert identity.short_id(b"\xff\xff\xff\xff" + bytes(12)) == 0xA5A5A5A5


def test_key_schedule_separates_labels() -> None:
    dev = bytes(16)
    assert identity.k_setup(SECRET, dev) != identity.static_oob(SECRET, dev)
    root = bytes(32)
    assert identity.k_epoch_v2(root, 1, 1) != identity.k_epoch_v2(root, 1, 2)
    assert len(identity.k_epoch_v2(root, 1, 1)) == 16
    h = bytes(32)
    assert identity.root_proof(root, h) != identity.maint_proof(root, h)
