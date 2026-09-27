# Gateway core on native_sim (docs/gateway-firmware.md "Tests"): the same
# sources as apps/gateway/src/core with a mocked backend (mesh, persistence,
# serial output) and a fake clock.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_gateway_core_test)

set(gw ${CMAKE_CURRENT_SOURCE_DIR}/../..)

target_sources(app PRIVATE
  ${gw}/src/core/gw_core.c
  ${gw}/src/core/gw_serial.c
  ${gw}/src/core/gw_lane.c
  ${gw}/src/core/gw_delivery.c
  ${gw}/src/core/gw_nodes.c
  ${gw}/src/core/gw_ring.c
  src/common.c
  src/test_serial.c
  src/test_delivery.c
  src/test_nodes.c
  src/test_ring.c)
target_include_directories(app PRIVATE ${gw}/src/core src)
