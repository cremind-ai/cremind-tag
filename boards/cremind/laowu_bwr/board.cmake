# SPDX-License-Identifier: MIT

# nRF51802 shares the nRF51822 QFAA flash geometry and debug interface.
board_runner_args(jlink "--device=nRF51822_xxAA" "--speed=4000")
board_runner_args(nrfjprog "--nrf-family=NRF51")
include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
include(${ZEPHYR_BASE}/boards/common/nrfjprog.board.cmake)
