"""Board files under boards/cremind/ agree with hardware/matrix.yaml and the panel contract."""

from __future__ import annotations

import re

import pytest
import yaml
from ctag_tools_helpers import REPO_ROOT

BOARDS = REPO_ROOT / "boards" / "cremind"
MATRIX = yaml.safe_load((REPO_ROOT / "hardware" / "matrix.yaml").read_text(encoding="utf-8"))
TAGS = {t["id"]: t for t in MATRIX["tags"]}
SOC_KCONFIG = {
    "laowu_bw": "SOC_NRF51822_QFAB",
    "laowu_bwr": "SOC_NRF51822_QFAA",
    "sifei_52810": "SOC_NRF52810_QFAA",
    "hema_52811": "SOC_NRF52811_QFAA",
}
NRF52_BOARDS = {"sifei_52810", "hema_52811"}


def board_file(board: str, name: str) -> str:
    return (BOARDS / board / name).read_text(encoding="utf-8")


def dts(board: str) -> str:
    return board_file(board, f"{board}.dts")


def gpio(text: str, prop: str) -> int | None:
    m = re.search(rf"\b{prop} = <&gpio0 (\d+) ", text)
    return int(m[1]) if m else None


def psel(text: str, func: str) -> int | None:
    m = re.search(rf"NRF_PSEL\({func}, 0, (\d+)\)", text)
    return int(m[1]) if m else None


def test_every_matrix_tag_has_a_board():
    assert set(TAGS) == set(SOC_KCONFIG) == {p.name for p in BOARDS.iterdir() if p.is_dir()}


@pytest.mark.parametrize("board", sorted(SOC_KCONFIG))
def test_board_file_set(board):
    for name in (
        "board.yml",
        f"Kconfig.{board}",
        "Kconfig.defconfig",
        f"{board}_defconfig",
        f"{board}.dts",
        f"{board}-pinctrl.dtsi",
        f"{board}.yaml",
        "board.cmake",
    ):
        assert (BOARDS / board / name).is_file(), name
    meta = yaml.safe_load(board_file(board, "board.yml"))["board"]
    assert meta["name"] == board and meta["vendor"] == "cremind"
    assert TAGS[board]["zephyr_board"] == f"{board}/{meta['socs'][0]['name']}"
    assert f"select {SOC_KCONFIG[board]}" in board_file(board, f"Kconfig.{board}")
    twister = yaml.safe_load(board_file(board, f"{board}.yaml"))
    assert twister["identifier"] == TAGS[board]["zephyr_board"]
    assert (twister["flash"], twister["ram"]) == (TAGS[board]["flash_kib"], TAGS[board]["ram_kib"])


@pytest.mark.parametrize("board", sorted(SOC_KCONFIG))
def test_defconfig_rc_oscillator_and_no_console(board):
    text = board_file(board, f"{board}_defconfig")
    for line in (
        "CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC=y",
        "CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC_CALIBRATION=y",
        "CONFIG_CLOCK_CONTROL_NRF_CALIBRATION_MAX_SKIP=0",
        "CONFIG_GPIO=y",
        "CONFIG_SPI=y",
        "CONFIG_UART_CONSOLE=n",
    ):
        assert line in text.splitlines(), line


@pytest.mark.parametrize("board", sorted(SOC_KCONFIG))
def test_ids_and_partitions(board):
    text = dts(board)
    tag = TAGS[board]
    assert f"board-id = <{tag['board_id']}>;" in text
    assert f"panel-id = <{tag['panel']['id']}>;" in text
    assert "panel-verified;" not in text  # nothing is verified on hardware yet
    flash = tag["flash_kib"] * 1024
    storage = 0x1000 if board.startswith("laowu") else 0x2000  # firmware-notes section 9
    assert f"reg = <0x00000000 0x{flash - storage:08x}>;" in text
    assert f"storage_partition: partition@{flash - storage:x} {{" in text
    assert f"reg = <0x{flash - storage:08x} 0x{storage:08x}>;" in text


@pytest.mark.parametrize("board", ["laowu_bw", "laowu_bwr"])
def test_laowu_pins_match_the_matrix(board):
    pins = TAGS[board]["pins"]
    text, pinctrl = dts(board), board_file(board, f"{board}-pinctrl.dtsi")
    assert psel(pinctrl, "SPIM_MOSI") == pins["mosi"]
    assert psel(pinctrl, "SPIM_SCK") == pins["sck"]
    assert psel(pinctrl, "UART_TX") == pins["debug_tx"]
    assert gpio(text, "cs-gpios") == pins["cs"]
    assert gpio(text, "dc-gpios") == pins["dc"]
    assert gpio(text, "reset-gpios") == pins["reset"]
    assert gpio(text, "busy-gpios") == pins["busy"]
    assert gpio(text, "bs-gpios") == pins["bs"]
    assert gpio(text, "wake-gpios") == pins["wake"]
    leds = [int(n) for n in re.findall(r"led_\d+ \{\s*gpios = <&gpio0 (\d+) ", text)]
    assert leds == (TAGS[board]["leds"] if isinstance(TAGS[board]["leds"], list) else [])
    assert 'status = "disabled";\n\tcurrent-speed' in text  # debug UART present but off


@pytest.mark.parametrize("board", sorted(NRF52_BOARDS))
def test_unverified_boards_use_placeholders_safely(board):
    text = dts(board)
    assert TAGS[board]["pins"] == "unverified"
    assert "panel-id = <255>;" in text
    assert "zephyr,deferred-init;" in text
    assert "PLACEHOLDER" in board_file(board, f"{board}-pinctrl.dtsi")
    assert "wake-gpios" not in text


@pytest.mark.parametrize("board", sorted(NRF52_BOARDS))
def test_nrf52_boards_default_to_the_zephyr_controller(board):
    text = dts(board)
    assert "zephyr,bt-hci = &bt_hci_controller;" in text
    assert re.search(r"&bt_hci_sdc \{\s*status = \"disabled\";", text)
