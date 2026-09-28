# Host tests of the portable firmware libraries against protocol/fixtures.
#
#   cmake -S tests/host -B tests/host/build -G Ninja && cmake --build tests/host/build
#   ctest --test-dir tests/host/build --output-on-failure
#
# -DCMAKE_C_FLAGS=-m32 builds a 32-bit variant where the toolchain supports it.
# The C test vectors are generated from the JSON fixtures at build time
# (gen_vectors.py), so they are rebuilt whenever a fixture changes.

cmake_minimum_required(VERSION 3.20)
project(ctag_host_tests C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

get_filename_component(CTAG_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../.. ABSOLUTE)

option(CTAG_WERROR "Treat warnings as errors" ON)

# Warning set of the libraries and the tests (not of the vendored qrcodegen).
add_library(ctag_host_options INTERFACE)
target_compile_options(ctag_host_options INTERFACE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow -Wundef
  -Wstrict-prototypes -Wmissing-prototypes -Wcast-qual -Wvla
  $<$<BOOL:${CTAG_WERROR}>:-Werror>)

add_subdirectory(${CTAG_ROOT}/lib ${CMAKE_CURRENT_BINARY_DIR}/lib)

find_package(Python3 REQUIRED COMPONENTS Interpreter)
include(vectors.cmake)

set(test_sources
  src/runner.c
  src/support.c
  src/test_common.c
  src/test_frame.c
  src/test_mesh.c
  src/test_layout.c
  src/test_fontpack.c
  src/test_render.c
  src/test_frag.c
  src/test_txn.c
  src/test_enroll.c
  src/test_fuzz.c
  src/test_secure.c)
add_executable(ctag_host_tests ${test_sources})
ctag_test_vectors(ctag_host_tests ${Python3_EXECUTABLE})
target_include_directories(ctag_host_tests PRIVATE src)
target_link_libraries(ctag_host_tests PRIVATE
  ctag_frame ctag_layout ctag_render ctag_frag ctag_txn ctag_enroll ctag_secure ctag_host_options)

# The same suites with a renderer that keeps one QR symbol (as a bridge with
# CONFIG_CTAG_RENDER_QR_SLOTS=1): QR commands beyond it are re-encoded per strip.
add_library(ctag_render_qr1 STATIC ${CTAG_ROOT}/lib/render/render.c ${CTAG_ROOT}/lib/render/fontpack.c)
target_compile_definitions(ctag_render_qr1 PUBLIC CTAG_RENDER_QR_SLOTS=1)
target_link_libraries(ctag_render_qr1 PUBLIC ctag_layout ctag_crc32 ctag_utf8 ctag_qrcodegen
  PRIVATE ctag_host_options)
add_executable(ctag_host_tests_qr1 ${test_sources})
add_dependencies(ctag_host_tests_qr1 ctag_host_tests) # the generated vectors
target_include_directories(ctag_host_tests_qr1 PRIVATE src ${CMAKE_CURRENT_BINARY_DIR}/vectors)
target_link_libraries(ctag_host_tests_qr1 PRIVATE
  ctag_frame ctag_layout ctag_render_qr1 ctag_frag ctag_txn ctag_enroll ctag_secure ctag_host_options)

enable_testing()
add_test(NAME ctag_host_tests COMMAND ctag_host_tests)
add_test(NAME ctag_host_tests_qr1 COMMAND ctag_host_tests_qr1)
