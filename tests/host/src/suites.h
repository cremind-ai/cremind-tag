/* Every host test; X(name) per test function void name(void). */
#ifndef CTAG_TEST_SUITES_H_
#define CTAG_TEST_SUITES_H_

#define CTAG_HOST_TESTS(X)                                                                         \
	X(test_crc32_vectors)                                                                      \
	X(test_crc32_chaining)                                                                     \
	X(test_utf8)                                                                               \
	X(test_cobs_vectors)                                                                       \
	X(test_cobs_decode_only)                                                                   \
	X(test_cobs_decode_errors)                                                                 \
	X(test_cobs_buffer_limits)                                                                 \
	X(test_cobs_stream)                                                                        \
	X(test_serial_frames)                                                                      \
	X(test_serial_invalid)                                                                     \
	X(test_serial_rx)                                                                          \
	X(test_credits)                                                                            \
	X(test_mesh_vectors)                                                                       \
	X(test_layout_valid)                                                                       \
	X(test_layout_invalid)                                                                     \
	X(test_layout_panel)                                                                       \
	X(test_layout_iter)                                                                        \
	X(test_layout_asm)                                                                         \
	X(test_layout_asm_errors)                                                                  \
	X(test_fontpack_parse)                                                                     \
	X(test_fontpack_glyphs)                                                                    \
	X(test_fontpack_corruptions)                                                               \
	X(test_fontpack_invalid)                                                                   \
	X(test_qr_vectors)                                                                         \
	X(test_render_scenarios)                                                                   \
	X(test_render_strips)                                                                      \
	X(test_render_errors)                                                                      \
	X(test_frag_vectors)                                                                       \
	X(test_frag_errors)                                                                        \
	X(test_frag_continuous)                                                                    \
	X(test_txn_frame_begin)                                                                    \
	X(test_txn_boot)                                                                           \
	X(test_txn_record)                                                                         \
	X(test_txn_store)                                                                          \
	X(test_txn_flow)                                                                           \
	X(test_enroll_valid)                                                                       \
	X(test_enroll_invalid)                                                                     \
	X(test_fuzz_layouts)                                                                       \
	X(test_fuzz_fontpack)                                                                      \
	X(test_fuzz_streams)

#define CTAG_TEST_DECLARE(name) void name(void);
CTAG_HOST_TESTS(CTAG_TEST_DECLARE)

#endif /* CTAG_TEST_SUITES_H_ */
