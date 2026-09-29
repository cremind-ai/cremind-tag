# Bridge core logic on native_sim (docs/bridge-firmware.md "Tests"): the same
# sources as apps/bridge/src/core, the flash simulator standing in for the
# external NOR, mocked Bluetooth for the scheduler (lib/sched, linked through
# CONFIG_CTAG_SCHED), and a fake tag built from the tag-side libraries for the
# session.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_bridge_core_test)

set(bridge ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(host ${bridge}/../../tests/host)
include(${host}/vectors.cmake)

target_sources(app PRIVATE
  ${bridge}/src/core/bflash.c
  ${bridge}/src/core/fontstore.c
  ${bridge}/src/core/delivery.c
  ${bridge}/src/core/tagsess.c
  ${bridge}/src/core/maint.c
  src/common.c
  src/test_bflash.c
  src/test_fontstore.c
  src/test_delivery.c
  src/test_sched.c
  src/test_session.c
  src/test_maint.c
  src/test_golden.c)
target_include_directories(app PRIVATE ${bridge}/src/core src)
ctag_test_vectors(app ${PYTHON_EXECUTABLE})
