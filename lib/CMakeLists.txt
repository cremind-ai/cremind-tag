# Cremind Tag firmware libraries (docs/firmware-libs.md).
#
# Under Zephyr this directory is added by the module's CMakeLists.txt and each
# library is a zephyr_library enabled by its CONFIG_CTAG_* option. Anywhere
# else (tests/host) every portable library becomes a plain static library; an
# INTERFACE target named ctag_host_options, when defined by the includer,
# supplies the host warning flags.

set(CTAG_LIB_DIR ${CMAKE_CURRENT_LIST_DIR})

# ctag_library(<name> <option> SOURCES <files...> [DEPENDS <libs...>])
function(ctag_library name option)
  cmake_parse_arguments(arg "" "" "SOURCES;DEPENDS" ${ARGN})
  if(COMMAND zephyr_library_named)
    if(${option})
      zephyr_library_named(${name})
      zephyr_library_sources(${arg_SOURCES})
    endif()
  else()
    add_library(${name} STATIC ${arg_SOURCES})
    target_include_directories(${name} PUBLIC ${CTAG_LIB_DIR}/../include)
    target_link_libraries(${name} PUBLIC ${arg_DEPENDS})
    if(TARGET ctag_host_options)
      target_link_libraries(${name} PRIVATE ctag_host_options)
    endif()
  endif()
endfunction()

ctag_library(ctag_crc32 CONFIG_CTAG_CRC32 SOURCES common/crc32.c)
ctag_library(ctag_utf8 CONFIG_CTAG_UTF8 SOURCES common/utf8.c)
ctag_library(ctag_frame CONFIG_CTAG_FRAME
  SOURCES frame/cobs.c frame/serial.c
  DEPENDS ctag_crc32)
ctag_library(ctag_layout CONFIG_CTAG_LAYOUT
  SOURCES layout/layout.c layout/assembler.c)
ctag_library(ctag_frag CONFIG_CTAG_FRAG SOURCES frag/frag.c)
ctag_library(ctag_txn CONFIG_CTAG_TXN SOURCES txn/txn.c DEPENDS ctag_crc32)
ctag_library(ctag_enroll CONFIG_CTAG_ENROLL SOURCES enroll/enroll.c DEPENDS ctag_crc32)

# The vendored QR generator is built verbatim, with the compiler's default
# warnings rather than the libraries' stricter set.
set(qrcodegen_dir ${CTAG_LIB_DIR}/third_party/qrcodegen)
if(COMMAND zephyr_library_named)
  if(CONFIG_CTAG_RENDER)
    zephyr_library_named(ctag_render)
    zephyr_library_sources(render/render.c render/fontpack.c ${qrcodegen_dir}/qrcodegen.c)
    zephyr_library_include_directories(${qrcodegen_dir})
  endif()
  if(CONFIG_CTAG_CBOR)
    zephyr_library_named(ctag_cbor)
    zephyr_library_sources(cbor/cbor.c)
  endif()
  if(CONFIG_CTAG_SESSION)
    zephyr_library_named(ctag_session)
    zephyr_library_sources(session/session.c)
    zephyr_library_sources_ifdef(CONFIG_CTAG_CRYPTO_PSA session/crypto_psa.c)
  endif()
else()
  add_library(ctag_qrcodegen STATIC ${qrcodegen_dir}/qrcodegen.c)
  target_include_directories(ctag_qrcodegen PUBLIC ${qrcodegen_dir})
  ctag_library(ctag_render CONFIG_CTAG_RENDER
    SOURCES render/render.c render/fontpack.c
    DEPENDS ctag_layout ctag_crc32 ctag_utf8 ctag_qrcodegen)
endif()
