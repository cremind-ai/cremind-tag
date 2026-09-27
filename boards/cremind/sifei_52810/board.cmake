# SPDX-License-Identifier: MIT

board_runner_args(jlink "--device=nRF52810_xxAA" "--speed=4000")
board_runner_args(nrfjprog "--nrf-family=NRF52")
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
