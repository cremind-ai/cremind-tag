# Gateway interop build for native_sim (README.md): the firmware's core,
# gateway loop and UART glue on a native PTY UART, a simulated mesh behind it.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_gateway_interop)

set(gw ${CMAKE_CURRENT_SOURCE_DIR}/../..)

target_sources(app PRIVATE
  ${gw}/src/core/gw_core.c
  ${gw}/src/core/gw_serial.c
  ${gw}/src/core/gw_lane.c
  ${gw}/src/core/gw_delivery.c
  ${gw}/src/core/gw_nodes.c
  ${gw}/src/core/gw_ring.c
  ${gw}/src/gw_thread.c
  ${gw}/src/uart_io.c
  src/main.c)
# Protocol v2 (v2.conf): the secure layer and tunnels.
target_sources_ifdef(CONFIG_CTAG_GW_SECURE app PRIVATE
  ${gw}/src/core/gw_secure.c
  ${gw}/src/core/gw_tunnel.c)
target_include_directories(app PRIVATE ${gw}/src ${gw}/src/core)
