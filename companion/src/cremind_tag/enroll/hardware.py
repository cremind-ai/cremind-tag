"""Board and panel identities (spec ``boards``/``panels``, hardware/matrix.yaml) and tag id text form.

Names accepted on the command line: the spec enum names in any case with ``-``
or ``_`` (``laowu-bw-nrf51822``), the short ids of hardware/matrix.yaml
(``laowu_bw``, ``sifei_52810``) and plain numbers. Panel geometry and plane
encoding come from the panel profile; ``UNVERIFIED`` panels have no profile and
need explicit geometry.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol.ids import Board, Panel

# Short ids from hardware/matrix.yaml.
BOARD_ALIASES: dict[str, Board] = {
    "nrf52840_gateway": Board.NRF52840DK_GATEWAY,
    "nrf52832_gateway": Board.NRF52832_GATEWAY,
    "nrf52840_bridge": Board.NRF52840_BRIDGE,
    "nrf52832_bridge": Board.NRF52832_BRIDGE,
    "laowu_bw": Board.LAOWU_BW_NRF51822,
    "laowu_bwr": Board.LAOWU_BWR_NRF51802,
    "sifei_52810": Board.SIFEI_NRF52810,
    "hema_52811": Board.HEMA_NRF52811,
    "nrf52dk_tag": Board.NRF52DK_TAG,
}
PANEL_ALIASES: dict[str, Panel] = {
    "bw": Panel.UC8176_420_BW,
    "bwr": Panel.UC8176_420_BWR,
    "uc8176_bw": Panel.UC8176_420_BW,
    "uc8176_bwr": Panel.UC8176_420_BWR,
}

TAG_BOARDS = frozenset(b for b in Board if b >= Board.LAOWU_BW_NRF51822)


@dataclass(frozen=True, slots=True)
class PanelProfile:
    """Native geometry and plane encoding (docs/protocol.md §4.4 "Planes")."""

    width: int
    height: int
    planes: int
    plane_flags: int  # bit0: plane 0 bit 1 = white; bit1: plane 1 bit 1 = red

    @property
    def plane_len(self) -> int:
        return (self.width + 7) // 8 * self.height


PANEL_PROFILES: dict[Panel, PanelProfile] = {
    Panel.UC8176_420_BW: PanelProfile(400, 300, 1, 0x01),
    Panel.UC8176_420_BWR: PanelProfile(400, 300, 2, 0x03),
    Panel.NONE: PanelProfile(400, 300, 1, 0x01),  # dev tag without a display: a virtual 4.2" BW panel
}

# Panel the board ships with (hardware/matrix.yaml); UNVERIFIED needs explicit geometry.
BOARD_PANEL: dict[Board, Panel] = {
    Board.LAOWU_BW_NRF51822: Panel.UC8176_420_BW,
    Board.LAOWU_BWR_NRF51802: Panel.UC8176_420_BWR,
    Board.SIFEI_NRF52810: Panel.UNVERIFIED,
    Board.HEMA_NRF52811: Panel.UNVERIFIED,
    Board.NRF52DK_TAG: Panel.NONE,
}


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def parse_board(name: str | int) -> Board:
    if isinstance(name, int) or str(name).strip().isdigit():
        return Board(int(name))
    key = _norm(str(name))
    if key in BOARD_ALIASES:
        return BOARD_ALIASES[key]
    try:
        return Board[key.upper()]
    except KeyError:
        choices = ", ".join(sorted({*BOARD_ALIASES, *(b.name.lower() for b in Board)}))
        raise ValueError(f"unknown board {name!r} (choose from {choices})") from None


def parse_panel(name: str | int) -> Panel:
    if isinstance(name, int) or str(name).strip().isdigit():
        return Panel(int(name))
    key = _norm(str(name))
    if key in PANEL_ALIASES:
        return PANEL_ALIASES[key]
    try:
        return Panel[key.upper()]
    except KeyError:
        choices = ", ".join(sorted({*PANEL_ALIASES, *(p.name.lower() for p in Panel)}))
        raise ValueError(f"unknown panel {name!r} (choose from {choices})") from None


def board_name(board: int) -> str:
    try:
        return Board(board).name.lower()
    except ValueError:
        return str(board)


def panel_name(panel: int) -> str:
    try:
        return Panel(panel).name.lower()
    except ValueError:
        return str(panel)


def format_tag_id(tag_id: int) -> str:
    """Tag id as the connector API writes it: 8 upper-case hex digits."""
    return f"{tag_id:08X}"


def parse_tag_id(text: str | int) -> int:
    """Accepts ``1A2B3C4D``, ``0x1a2b3c4d`` or a decimal written as ``#439041101``."""
    if isinstance(text, int):
        value = text
    else:
        raw = text.strip()
        if raw.startswith("#"):
            value = int(raw[1:], 10)
        else:
            value = int(raw[2:] if raw.lower().startswith("0x") else raw, 16)
    if not 1 <= value <= 0xFFFFFFFE:
        raise ValueError(f"tag id {text!r} out of range")
    return value
