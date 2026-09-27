cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_size)

target_sources(app PRIVATE src/tag.c)
target_sources_ifdef(CONFIG_CTAG_SIZE_BRIDGE app PRIVATE src/bridge.c)
