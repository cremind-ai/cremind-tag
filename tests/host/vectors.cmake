# ctag_test_vectors(<target> <python>): generate the C test vectors of
# protocol/fixtures (gen_vectors.py) for <target>, regenerated whenever a
# fixture, include/ctag/proto_msgs.h or the generator changes. Used by
# tests/host and tests/ztest.

function(ctag_test_vectors target python)
  get_filename_component(root ${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../.. ABSOLUTE)
  set(dir ${CMAKE_CURRENT_BINARY_DIR}/vectors)
  set(names crc32 cobs serial serial_cbor mesh layouts render qr fontpack fragments tag_txn
    enrollment session v2)
  list(TRANSFORM names PREPEND ${dir}/v_ OUTPUT_VARIABLE headers)
  list(TRANSFORM headers APPEND .h)
  file(GLOB fixtures ${root}/protocol/fixtures/*)
  add_custom_command(
    OUTPUT ${headers}
    COMMAND ${python} ${CMAKE_CURRENT_FUNCTION_LIST_DIR}/gen_vectors.py ${dir} --root ${root}
    DEPENDS ${CMAKE_CURRENT_FUNCTION_LIST_DIR}/gen_vectors.py ${fixtures}
            ${root}/include/ctag/proto_msgs.h
    COMMENT "Generating C test vectors from protocol/fixtures"
    VERBATIM)
  target_sources(${target} PRIVATE ${headers})
  target_include_directories(${target} PRIVATE ${dir})
endfunction()
