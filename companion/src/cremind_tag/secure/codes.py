"""Setup codes: the QR / typed label of a v2 bridge or tag (connect-setup.md 2.2).

::

    payload (15 B) = (0x20 | role) u8 | short_id u32le | setup_secret[10]
    code           = base32_crockford(payload) (24 symbols) | check symbol
    display        = XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
    QR text        = "CTAG:" | code

The check symbol is a weighted sum over GF(32) (modulus ``SETUP_CODE_POLY``,
x^5 + x^2 + 1): ``c = sum(alpha^(i+1) * v_i)`` over the 24 data symbols with
``alpha = 2``. Every single mistyped symbol and every swap of two neighbouring
symbols changes ``c``, so the code is refused before anything is sent. A code
is a pairing credential: never log it, never keep it longer than an operation
needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from cremind_tag.protocol.ids import (
    SETUP_CODE_LEN,
    SETUP_CODE_POLY,
    SETUP_PAYLOAD_LEN,
    SETUP_SECRET_LEN,
    NodeRole,
)

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
QR_PREFIX = "CTAG:"
_VERSION_NIBBLE = 0x20
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
_ALIASES = {"O": "0", "I": "1", "L": "1"}
_PAIRABLE_ROLES = (NodeRole.BRIDGE, NodeRole.TAG)


class SetupCodeError(ValueError):
    """A setup code could not be used. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SetupPayload:
    role: NodeRole
    short_id: int
    secret: bytes

    def __post_init__(self) -> None:
        if self.role not in _PAIRABLE_ROLES:
            raise SetupCodeError("setup_code_invalid", "Only bridges and tags have setup codes.")
        if not 0 <= self.short_id <= 0xFFFFFFFF:
            raise SetupCodeError("setup_code_invalid", "The short id is not a u32.")
        if len(self.secret) != SETUP_SECRET_LEN:
            raise SetupCodeError("setup_code_invalid", f"A setup secret is {SETUP_SECRET_LEN} bytes.")

    def pack(self) -> bytes:
        return (bytes([_VERSION_NIBBLE | int(self.role)]) + self.short_id.to_bytes(4, "little")
                + bytes(self.secret))

    @classmethod
    def unpack(cls, payload: bytes) -> SetupPayload:
        if len(payload) != SETUP_PAYLOAD_LEN:
            raise SetupCodeError("setup_code_invalid", "The setup payload has the wrong length.")
        head = payload[0]
        if head & 0xF0 != _VERSION_NIBBLE:
            raise SetupCodeError("setup_code_unsupported", "This label is not for protocol v2 setup.")
        try:
            role = NodeRole(head & 0x0F)
        except ValueError:
            raise SetupCodeError("setup_code_invalid", "The label names an unknown device role.") from None
        return cls(role, int.from_bytes(payload[1:5], "little"), bytes(payload[5:]))

    def code(self) -> str:
        return format_code(self.pack())

    def qr_text(self) -> str:
        return QR_PREFIX + format_code(self.pack(), grouped=False)

    def __repr__(self) -> str:  # never print the secret
        return f"SetupPayload(role={self.role.name}, short_id={self.short_id:08X}, secret=<{len(self.secret)} bytes>)"


def _gf_mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        b >>= 1
        a <<= 1
        if a & 0x20:
            a ^= SETUP_CODE_POLY
    return out


def _check_value(symbols: str) -> int:
    check, weight = 0, 1
    for ch in symbols:
        weight = _gf_mul(weight, 2)
        check ^= _gf_mul(weight, _DECODE[ch])
    return check


def _check_symbol(payload: bytes) -> str:
    return ALPHABET[_check_value(_encode(payload))]


def _encode(payload: bytes) -> str:
    value = int.from_bytes(payload, "big")
    symbols = len(payload) * 8 // 5
    return "".join(ALPHABET[(value >> (5 * (symbols - 1 - i))) & 31] for i in range(symbols))


def format_code(payload: bytes, *, grouped: bool = True) -> str:
    """The 25-symbol code of a 15-byte payload, in groups of five by default."""
    if len(payload) != SETUP_PAYLOAD_LEN:
        raise SetupCodeError("setup_code_invalid", "The setup payload has the wrong length.")
    text = _encode(payload) + _check_symbol(payload)
    if not grouped:
        return text
    return "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def normalize(text: str) -> str:
    """Upper-case, strip the QR prefix, spaces and dashes, apply Crockford aliases."""
    raw = (text or "").strip()
    if raw.upper().startswith(QR_PREFIX):
        raw = raw[len(QR_PREFIX):]
    out = []
    for ch in raw.upper():
        if ch in " -\t\r\n_":
            continue
        out.append(_ALIASES.get(ch, ch))
    return "".join(out)


def parse_code(text: str, *, role: NodeRole | int | None = None) -> SetupPayload:
    """Parse a typed code or QR text. Raises :class:`SetupCodeError`."""
    symbols = normalize(text)
    if len(symbols) != SETUP_CODE_LEN:
        raise SetupCodeError("setup_code_invalid",
                             f"A setup code has {SETUP_CODE_LEN} characters; this one has {len(symbols)}.")
    if any(ch not in _DECODE for ch in symbols):
        raise SetupCodeError("setup_code_invalid", "The setup code contains a character labels never use.")
    value = 0
    for ch in symbols[:-1]:
        value = (value << 5) | _DECODE[ch]
    payload = value.to_bytes(SETUP_PAYLOAD_LEN, "big")
    if _check_symbol(payload) != symbols[-1]:
        raise SetupCodeError("setup_code_invalid", "The setup code does not check out; look for a typo.")
    parsed = SetupPayload.unpack(payload)
    if role is not None and parsed.role != NodeRole(int(role)):
        raise SetupCodeError("setup_code_wrong_role",
                             f"This is a {parsed.role.name.lower()} label, not a {NodeRole(int(role)).name.lower()} label.")
    return parsed
