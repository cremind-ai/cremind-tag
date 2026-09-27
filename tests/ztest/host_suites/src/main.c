/*
 * Every tests/host suite as a ztest case. Twister discovers cases by scanning
 * for literal ZTEST() lines, so they are listed here rather than expanded from
 * CTAG_HOST_TESTS; test_suites_listed() keeps the two lists in step.
 */
#include <zephyr/ztest.h>

#include "suites.h"

ZTEST(ctag_host_suites, test_crc32_vectors)
{
	test_crc32_vectors();
}

ZTEST(ctag_host_suites, test_crc32_chaining)
{
	test_crc32_chaining();
}

ZTEST(ctag_host_suites, test_utf8)
{
	test_utf8();
}

ZTEST(ctag_host_suites, test_cobs_vectors)
{
	test_cobs_vectors();
}

ZTEST(ctag_host_suites, test_cobs_decode_only)
{
	test_cobs_decode_only();
}

ZTEST(ctag_host_suites, test_cobs_decode_errors)
{
	test_cobs_decode_errors();
}

ZTEST(ctag_host_suites, test_cobs_buffer_limits)
{
	test_cobs_buffer_limits();
}

ZTEST(ctag_host_suites, test_cobs_stream)
{
	test_cobs_stream();
}

ZTEST(ctag_host_suites, test_serial_frames)
{
	test_serial_frames();
}

ZTEST(ctag_host_suites, test_serial_invalid)
{
	test_serial_invalid();
}

ZTEST(ctag_host_suites, test_serial_rx)
{
	test_serial_rx();
}

ZTEST(ctag_host_suites, test_credits)
{
	test_credits();
}

ZTEST(ctag_host_suites, test_mesh_vectors)
{
	test_mesh_vectors();
}

ZTEST(ctag_host_suites, test_layout_valid)
{
	test_layout_valid();
}

ZTEST(ctag_host_suites, test_layout_invalid)
{
	test_layout_invalid();
}

ZTEST(ctag_host_suites, test_layout_panel)
{
	test_layout_panel();
}

ZTEST(ctag_host_suites, test_layout_iter)
{
	test_layout_iter();
}

ZTEST(ctag_host_suites, test_layout_asm)
{
	test_layout_asm();
}

ZTEST(ctag_host_suites, test_layout_asm_errors)
{
	test_layout_asm_errors();
}

ZTEST(ctag_host_suites, test_fontpack_parse)
{
	test_fontpack_parse();
}

ZTEST(ctag_host_suites, test_fontpack_glyphs)
{
	test_fontpack_glyphs();
}

ZTEST(ctag_host_suites, test_fontpack_corruptions)
{
	test_fontpack_corruptions();
}

ZTEST(ctag_host_suites, test_fontpack_invalid)
{
	test_fontpack_invalid();
}

ZTEST(ctag_host_suites, test_qr_vectors)
{
	test_qr_vectors();
}

ZTEST(ctag_host_suites, test_render_scenarios)
{
	test_render_scenarios();
}

ZTEST(ctag_host_suites, test_render_strips)
{
	test_render_strips();
}

ZTEST(ctag_host_suites, test_render_qr_cache)
{
	test_render_qr_cache();
}

ZTEST(ctag_host_suites, test_render_errors)
{
	test_render_errors();
}

ZTEST(ctag_host_suites, test_frag_vectors)
{
	test_frag_vectors();
}

ZTEST(ctag_host_suites, test_frag_errors)
{
	test_frag_errors();
}

ZTEST(ctag_host_suites, test_frag_continuous)
{
	test_frag_continuous();
}

ZTEST(ctag_host_suites, test_txn_frame_begin)
{
	test_txn_frame_begin();
}

ZTEST(ctag_host_suites, test_txn_boot)
{
	test_txn_boot();
}

ZTEST(ctag_host_suites, test_txn_record)
{
	test_txn_record();
}

ZTEST(ctag_host_suites, test_txn_store)
{
	test_txn_store();
}

ZTEST(ctag_host_suites, test_txn_flow)
{
	test_txn_flow();
}

ZTEST(ctag_host_suites, test_enroll_valid)
{
	test_enroll_valid();
}

ZTEST(ctag_host_suites, test_enroll_invalid)
{
	test_enroll_invalid();
}

ZTEST(ctag_host_suites, test_fuzz_layouts)
{
	test_fuzz_layouts();
}

ZTEST(ctag_host_suites, test_fuzz_fontpack)
{
	test_fuzz_fontpack();
}

ZTEST(ctag_host_suites, test_fuzz_streams)
{
	test_fuzz_streams();
}

#define CTAG_COUNT_TEST(name) +1

ZTEST(ctag_host_suites, test_suites_listed)
{
	zassert_equal(0 CTAG_HOST_TESTS(CTAG_COUNT_TEST), 41);
}

ZTEST_SUITE(ctag_host_suites, NULL, NULL, NULL, NULL, NULL);
