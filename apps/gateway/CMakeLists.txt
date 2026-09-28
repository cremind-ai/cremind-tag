# Cremind Tag gateway firmware (docs/gateway-firmware.md). Built by
# tools/build.py with -DZEPHYR_EXTRA_MODULES=<repository>, which provides lib/.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cremind_gateway)

target_sources(app PRIVATE
  src/main.c
  src/mesh.c
  src/store.c
  src/gw_thread.c
  src/uart_io.c
  src/core/gw_core.c
  src/core/gw_serial.c
  src/core/gw_lane.c
  src/core/gw_delivery.c
  src/core/gw_nodes.c
  src/core/gw_ring.c)
# Protocol v2 (docs/connect-setup.md): secure sessions, ownership, tunnels,
# and the physical factory reset.
target_sources_ifdef(CONFIG_CTAG_GW_SECURE app PRIVATE
  src/reset.c
  src/core/gw_secure.c
  src/core/gw_tunnel.c)
target_include_directories(app PRIVATE src src/core)
