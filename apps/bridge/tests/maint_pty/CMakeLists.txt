# The bridge's maintenance-port core behind a native_sim pseudo-terminal, for
# driving it with the companion's real client (interop.py).
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_bridge_maint_pty)

set(bridge ${CMAKE_CURRENT_SOURCE_DIR}/../..)
target_sources(app PRIVATE
  ${bridge}/src/core/bflash.c
  ${bridge}/src/core/fontstore.c
  ${bridge}/src/core/maint.c
  src/main.c)
target_include_directories(app PRIVATE ${bridge}/src/core)
