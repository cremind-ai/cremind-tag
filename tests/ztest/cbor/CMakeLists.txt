cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_cbor_test)

include(${CMAKE_CURRENT_SOURCE_DIR}/../../host/vectors.cmake)
target_sources(app PRIVATE src/main.c)
ctag_test_vectors(app ${PYTHON_EXECUTABLE})
