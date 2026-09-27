# Cremind Tag bridge (docs/bridge-firmware.md). Build with tools/build.py:
#   python tools/build.py bridge-nrf52840dk bridge-nrf52dk
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cremind_tag_bridge)

# Build id for HELLO/INFO: the repository's git describe, when available.
execute_process(
  COMMAND git -c safe.directory=* -C ${CMAKE_CURRENT_SOURCE_DIR} describe --always --dirty --abbrev=10
  OUTPUT_VARIABLE ctag_build_id
  OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET
  RESULT_VARIABLE ctag_git_result)
if(NOT ctag_git_result EQUAL 0 OR ctag_build_id STREQUAL "")
  set(ctag_build_id "unknown")
endif()
zephyr_compile_definitions(CTAG_BRIDGE_BUILD="${ctag_build_id}")

target_include_directories(app PRIVATE src src/core)
target_sources(app PRIVATE
  src/core/bflash.c
  src/core/fontstore.c
  src/core/delivery.c
  src/core/sched.c
  src/core/tagsess.c
  src/core/maint.c
  src/main.c
  src/mesh.c
  src/central.c
  src/maint_port.c
  src/persist.c
  src/identify.c)
