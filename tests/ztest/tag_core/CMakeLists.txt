cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_tag_core_test)

# The tag firmware's protocol core, unchanged, with fake hooks (src/fakes.c).
set(tag_src ${CMAKE_CURRENT_SOURCE_DIR}/../../../apps/tag/src)
include(${CMAKE_CURRENT_SOURCE_DIR}/../../host/vectors.cmake)
target_sources(app PRIVATE src/main.c src/fakes.c ${tag_src}/tag_core.c)
target_include_directories(app PRIVATE src ${tag_src})
ctag_test_vectors(app ${PYTHON_EXECUTABLE})
