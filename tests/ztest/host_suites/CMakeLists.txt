cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(ctag_host_suites)

# The host-test suites of tests/host, unchanged, on Zephyr (check.h maps
# CHECK() to zassert) and against the libraries as Zephyr builds them.
set(host ${CMAKE_CURRENT_SOURCE_DIR}/../../host)
include(${host}/vectors.cmake)
target_sources(app PRIVATE
  src/main.c
  ${host}/src/support.c
  ${host}/src/test_common.c
  ${host}/src/test_frame.c
  ${host}/src/test_mesh.c
  ${host}/src/test_layout.c
  ${host}/src/test_fontpack.c
  ${host}/src/test_render.c
  ${host}/src/test_frag.c
  ${host}/src/test_txn.c
  ${host}/src/test_enroll.c
  ${host}/src/test_fuzz.c
  ${host}/src/test_secure.c)
target_include_directories(app PRIVATE ${host}/src ${host}/../../lib/third_party/qrcodegen)
ctag_test_vectors(app ${PYTHON_EXECUTABLE})
