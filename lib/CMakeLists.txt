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

# Protocol v2 secure layer (docs/firmware-libs.md "ctag_secure"): the vendored
# Noise* IK and HACL* sources (lib/third_party/noise_ik/README.md) built
# verbatim with the KaRaMeL configuration header force-included, and
# lib/secure with the libraries' warning set (the vendored headers are
# system headers there).
set(noise_dir ${CTAG_LIB_DIR}/third_party/noise_ik)
set(hacl_dir ${CTAG_LIB_DIR}/third_party/hacl)
set(ctag_secure_vendor_sources
  ${noise_dir}/IK.c
  ${noise_dir}/Noise_IK.c
  ${hacl_dir}/Hacl_Chacha20.c
  ${hacl_dir}/Hacl_Chacha20Poly1305_32.c
  ${hacl_dir}/Hacl_Poly1305_32.c
  ${hacl_dir}/Hacl_Curve25519_51.c
  ${hacl_dir}/Hacl_Ed25519.c
  ${hacl_dir}/Hacl_Hash_SHA2.c
  ${hacl_dir}/Hacl_Streaming_SHA2.c
  ${hacl_dir}/Hacl_HMAC.c
  ${hacl_dir}/Hacl_Hash_SHA1.c
  ${hacl_dir}/Hacl_Hash_Blake2.c)
set(ctag_secure_vendor_includes
  ${noise_dir}
  ${hacl_dir}
  ${hacl_dir}/karamel/include
  ${hacl_dir}/karamel/krmllib/dist/minimal)
# Warnings the generated code is known to raise (Noise*'s own Makefile.basic
# disables the same ones).
set(ctag_secure_vendor_options
  -include ${CTAG_LIB_DIR}/secure/ctag_krml.h
  -Wno-unused-variable -Wno-unused-but-set-variable -Wno-unused-parameter
  -Wno-unused-function -Wno-infinite-recursion)
set(ctag_secure_sources
  secure/hacl_glue.c
  secure/crypto.c
  secure/noise.c
  secure/keys.c
  secure/grant.c
  secure/record.c
  secure/message.c
  secure/endpoint.c)

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
  if(CONFIG_CTAG_SECURE)
    # native_sim: KaRaMeL's lowstar_endianness.h takes <endian.h> for any
    # __linux__ compiler, and the embedded C library's <endian.h> has no
    # htole64() & co.; without __linux__ it uses its generic __BYTE_ORDER__
    # branch, as on the nRF targets (with the host glibc, __GLIBC__ still
    # selects <endian.h>, whose macros _DEFAULT_SOURCE enables).
    zephyr_library_named(ctag_secure_vendor)
    zephyr_library_sources(${ctag_secure_vendor_sources})
    zephyr_library_compile_options(${ctag_secure_vendor_options})
    target_include_directories(ctag_secure_vendor SYSTEM PRIVATE ${ctag_secure_vendor_includes})
    if(CONFIG_ARCH_POSIX)
      zephyr_library_compile_options(-U__linux__ -D_DEFAULT_SOURCE)
    endif()
    zephyr_library_named(ctag_secure)
    zephyr_library_sources(${ctag_secure_sources})
    zephyr_library_include_directories(secure)
    target_include_directories(ctag_secure SYSTEM PRIVATE ${ctag_secure_vendor_includes})
    if(CONFIG_ARCH_POSIX)
      zephyr_library_compile_options(-U__linux__ -D_DEFAULT_SOURCE)
    endif()
  endif()
else()
  add_library(ctag_qrcodegen STATIC ${qrcodegen_dir}/qrcodegen.c)
  target_include_directories(ctag_qrcodegen PUBLIC ${qrcodegen_dir})
  ctag_library(ctag_render CONFIG_CTAG_RENDER
    SOURCES render/render.c render/fontpack.c
    DEPENDS ctag_layout ctag_crc32 ctag_utf8 ctag_qrcodegen)
  # Host: the struct-based uint128 of 32-bit targets (KRML_VERIFIED_UINT128),
  # the glibc endian macros under -std=c99 (_DEFAULT_SOURCE) and a bounded
  # pool for the KaRaMeL allocator (CTAG_SECURE_HEAP_SIZE, hacl_glue.c).
  add_library(ctag_secure_vendor STATIC ${ctag_secure_vendor_sources})
  target_include_directories(ctag_secure_vendor SYSTEM PUBLIC ${ctag_secure_vendor_includes})
  target_compile_options(ctag_secure_vendor PRIVATE ${ctag_secure_vendor_options})
  target_compile_definitions(ctag_secure_vendor PUBLIC KRML_VERIFIED_UINT128 _DEFAULT_SOURCE)
  ctag_library(ctag_secure CONFIG_CTAG_SECURE
    SOURCES ${ctag_secure_sources}
    DEPENDS ctag_crc32 ctag_secure_vendor)
  target_include_directories(ctag_secure PRIVATE ${CTAG_LIB_DIR}/secure)
endif()
