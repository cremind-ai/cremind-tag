# SPDX-License-Identifier: MIT

board_runner_args(jlink "--device=nRF51822_xxAB" "--speed=4000")
board_runner_args(nrfjprog "--nrf-family=NRF51")
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
