/* External flash: geometry, slot directory (power-cut atomicity), pending records. */
#include <string.h>

#include "common.h"

ZTEST(bridge_flash, test_geometry)
{
	struct bflash_geom g;

	/* nRF52840 DK development target: 8 MiB MX25R64, 1 MiB working space
	 * (what `cremind-tag fonts image` assumes for that board). */
	zassert_ok(bflash_geom_compute(8u * MIB, 1u * MIB, &g));
	zassert_equal(g.slot_size, 3584u * 1024u);
	zassert_equal(g.dir_off, 7u * MIB);
	zassert_equal(g.pending_off, 7u * MIB + CTAG_FONTPACK_DIR_SIZE);
	zassert_equal(g.pending_slots, (1u * MIB - 2u * 65536u) / BFLASH_PENDING_SLOT);
	zassert_equal(g.pending_end + BFLASH_SPARE, 8u * MIB);

	/* Production rule: 16 MiB working space. 64 MiB part. */
	zassert_ok(bflash_geom_compute(64u * MIB, 16u * MIB, &g));
	zassert_equal(g.slot_size, 24u * MIB);
	zassert_equal(g.dir_off, 48u * MIB);
	zassert_equal(g.pending_slots, (16u * MIB - 2u * 65536u) / BFLASH_PENDING_SLOT);
	/* A larger working space: the ring is capped. */
	zassert_ok(bflash_geom_compute(128u * MIB, 64u * MIB, &g));
	zassert_equal(g.pending_slots, BFLASH_PENDING_MAX);

	/* Odd sizes round the slots down to 64 KiB. */
	zassert_ok(bflash_geom_compute(24u * MIB + 4096u, 16u * MIB, &g));
	zassert_equal(g.slot_size, 4u * MIB);

	/* A part not larger than the working space has no pack slots. */
	zassert_ok(bflash_geom_compute(16u * MIB, 16u * MIB, &g));
	zassert_equal(g.slot_size, 0u);
	zassert_equal(bflash_geom_compute(8u * MIB, 16u * MIB, &g), -ENOSPC);
	zassert_equal(bflash_geom_compute(8u * MIB, 64u * 1024u, &g), -ENOSPC);
	zassert_equal(bflash_geom_compute(8u * MIB + 1u, 1u * MIB, &g), -EINVAL);
	zassert_equal(bflash_geom_compute(0u, 1u * MIB, &g), -EINVAL);
}

ZTEST(bridge_flash, test_large_part_address_map)
{
	/* The simulated 32 MiB part: slots below 16 MiB, directory and ring above. */
	env_fresh(0u);
	zassert_true(benv.flash_ok);
	zassert_equal(benv.flash.geom.flash_size, 32u * MIB);
	zassert_equal(benv.flash.geom.slot_size, 8u * MIB);
	zassert_equal(benv.flash.geom.dir_off, 16u * MIB);
	zassert_true(benv.flash.geom.pending_end <= 32u * MIB - BFLASH_SPARE);

	/* Reads and writes across the 16 MiB boundary and at the end of the part. */
	uint8_t w[8] __aligned(4) = {1, 2, 3, 4, 5, 6, 7, 8};
	uint8_t r[8];

	zassert_ok(bflash_erase(&benv.flash, 16u * MIB - 4096u, 8192u));
	zassert_ok(bflash_write(&benv.flash, 16u * MIB - 4u, w, sizeof(w)));
	zassert_ok(bflash_read(&benv.flash, 16u * MIB - 4u, r, sizeof(r)));
	zassert_mem_equal(r, w, sizeof(w));
	zassert_ok(bflash_erase(&benv.flash, 32u * MIB - 4096u, 4096u));
	zassert_ok(bflash_write(&benv.flash, 32u * MIB - 8u, w, sizeof(w)));
	zassert_ok(bflash_cached_read(&benv.flash, 32u * MIB - 8u, r, sizeof(r)));
	zassert_mem_equal(r, w, sizeof(w));
	zassert_equal(bflash_read(&benv.flash, 32u * MIB - 4u, r, 8u), -EINVAL);
	/* Writes are 4-byte units at 4-byte offsets. */
	zassert_equal(bflash_write(&benv.flash, 32u * MIB - 4096u + 2u, w, 4u), -EINVAL);
	zassert_equal(bflash_write(&benv.flash, 32u * MIB - 4096u, w, 3u), -EINVAL);
}

ZTEST(bridge_flash, test_directory_records)
{
	struct bflash_dir_record r = {.seq = 7, .slot = 1, .size = 2087};
	struct bflash_dir_record back;
	uint8_t buf[BFLASH_DIR_LEN];

	memcpy(r.pack_id, fixture_pack_id, 8);
	memset(r.content_hash, 0xA5, sizeof(r.content_hash));
	bflash_dir_encode(&r, buf);
	/* Layout of fontpack.md 3 (companion fonts/image.py slot_dir_record). */
	zassert_equal(buf[0], 'C');
	zassert_equal(buf[1], 'T');
	zassert_equal(buf[2], 'S');
	zassert_equal(buf[3], 'L');
	zassert_equal(ctag_get_le16(&buf[4]), 1);
	zassert_equal(ctag_get_le32(&buf[8]), 7);
	zassert_equal(buf[12], 1);
	zassert_mem_equal(&buf[16], fixture_pack_id, 8);
	zassert_equal(ctag_get_le32(&buf[24]), 2087);
	zassert_true(bflash_dir_decode(buf, 8u * MIB, &back));
	zassert_equal(back.seq, 7);
	buf[30] ^= 1;
	zassert_false(bflash_dir_decode(buf, 8u * MIB, &back), "CRC");
	buf[30] ^= 1;
	zassert_false(bflash_dir_decode(buf, 1000u, &back), "size above the slot");
}

static uint8_t hash[32];

static void activate(uint8_t slot, uint32_t size)
{
	memset(hash, slot, sizeof(hash));
	zassert_ok(bflash_dir_activate(&benv.flash, slot, fixture_pack_id, size, hash));
}

ZTEST(bridge_flash, test_directory_ab)
{
	struct bflash_dir_record r;
	uint8_t which;

	env_fresh(0u);
	zassert_equal(bflash_dir_active(&benv.flash, &r, &which), -ENOENT);
	activate(0, 100);
	zassert_ok(bflash_dir_active(&benv.flash, &r, &which));
	zassert_equal(r.seq, 1);
	zassert_equal(which, 0, "the first record goes to sector A");
	activate(1, 200);
	zassert_ok(bflash_dir_active(&benv.flash, &r, &which));
	zassert_equal(r.seq, 2);
	zassert_equal(r.slot, 1);
	zassert_equal(which, 1, "activation writes the other sector");
	activate(0, 300);
	zassert_ok(bflash_dir_active(&benv.flash, &r, &which));
	zassert_equal(r.seq, 3);
	zassert_equal(which, 0);
	zassert_equal(r.size, 300);
}

/*
 * Power loss at every byte of an activation (fontpack.md 3: "a power loss at
 * any moment leaves the previous record valid"): the erase of the other sector
 * and each of the 64 record bytes.
 */
ZTEST(bridge_flash, test_directory_power_cut)
{
	struct bflash_dir_record r;
	int32_t k;

	for (k = 0; k <= (int32_t)BFLASH_DIR_LEN; k++) {
		env_fresh(0u);
		activate(0, 100);
		activate(1, 200); /* active: seq 2, slot 1, in sector B */
		flash_cut_after(k);
		(void)bflash_dir_activate(&benv.flash, 0, fixture_pack_id, 300, hash);
		flash_cut_after(-1);
		zassert_ok(bflash_dir_active(&benv.flash, &r, NULL), "no active record after cut %d", k);
		if (k < (int32_t)BFLASH_DIR_LEN) {
			zassert_equal(r.seq, 2, "cut after %d bytes", k);
			zassert_equal(r.slot, 1);
			zassert_equal(r.size, 200);
		} else {
			zassert_equal(r.seq, 3, "all 64 bytes written");
			zassert_equal(r.slot, 0);
		}
	}
	/* Power lost before the erase of the target sector even started. */
	env_fresh(0u);
	activate(0, 100);
	activate(1, 200);
	activate(0, 300); /* sector A: seq 3; B: seq 2 */
	flash_cut_after(0);
	zassert_not_equal(bflash_dir_activate(&benv.flash, 1, fixture_pack_id, 400, hash), 0);
	flash_cut_after(-1);
	zassert_ok(bflash_dir_active(&benv.flash, &r, NULL));
	zassert_equal(r.seq, 3);
	zassert_equal(r.size, 300);
}

ZTEST(bridge_flash, test_pending_records)
{
	struct bflash_pending h = {
		.seq = 5, .tag_id = 0x1234, .epoch = 2, .revision = 9, .update_id = 77, .len = 0,
	};
	struct bflash_pending back;
	static uint8_t big[CTAG_LAYOUT_HARD_MAX];
	size_t n = big_layout(big);

	env_fresh(0u);
	memset(h.digest, 0x11, sizeof(h.digest));
	memcpy(h.fontpack_id, fixture_pack_id, 8);
	/* Odd length (tail padding) and the maximum length. */
	h.len = 141;
	zassert_ok(bflash_pending_write(&benv.flash, 0, &h, big));
	zassert_equal(bflash_pending_read(&benv.flash, 0, &back, scratch, sizeof(scratch)), 1);
	zassert_equal(back.len, 141);
	zassert_equal(back.update_id, 77);
	zassert_mem_equal(scratch, big, 141);
	h.len = (uint16_t)n;
	zassert_ok(bflash_pending_write(&benv.flash, 1, &h, big));
	zassert_equal(bflash_pending_read(&benv.flash, 1, &back, scratch, sizeof(scratch)), 1);
	zassert_mem_equal(scratch, big, n);
	/* Consumed: one write of the last word, no erase. */
	zassert_ok(bflash_pending_consume(&benv.flash, 1));
	zassert_equal(bflash_pending_peek(&benv.flash, 1, &back), BFLASH_PENDING_CONSUMED);
	zassert_equal(bflash_pending_read(&benv.flash, 1, &back, scratch, sizeof(scratch)), 0);
	zassert_ok(bflash_pending_consume(&benv.flash, 1), "consuming twice is harmless");
	/* A flipped layout bit fails the CRC. */
	flash_mem()[bflash_pending_offset(&benv.flash, 0) + BFLASH_PENDING_HDR + 10] ^= 0x01;
	zassert_equal(bflash_pending_read(&benv.flash, 0, &back, scratch, sizeof(scratch)), 0);
	/* Torn writes: the header goes last, so a cut anywhere leaves no record. */
	for (int32_t k = 0; k < 141 + 3 + (int32_t)BFLASH_PENDING_HDR; k += 7) {
		h.len = 141;
		flash_cut_after(k);
		(void)bflash_pending_write(&benv.flash, 2, &h, big);
		flash_cut_after(-1);
		zassert_equal(bflash_pending_read(&benv.flash, 2, &back, scratch, sizeof(scratch)), 0,
			      "torn record visible after %d bytes", k);
	}
	zassert_equal(bflash_pending_peek(&benv.flash, benv.flash.geom.pending_slots, &back),
		      -EINVAL);
}

/* A transfer assembled in place: chunks at their stride, header last. */
ZTEST(bridge_flash, test_pending_assembly)
{
	static uint8_t big[CTAG_LAYOUT_HARD_MAX];
	size_t n = big_layout(big);
	struct bflash_pending h = {.seq = 9, .tag_id = 1, .epoch = 1, .revision = 1,
				   .update_id = 5, .len = (uint16_t)n, .xfer_id = 0xBEEF};
	struct bflash_pending back;
	int i;

	env_fresh(0u);
	memcpy(h.fontpack_id, fixture_pack_id, 8);
	zassert_ok(bflash_pending_erase(&benv.flash, 3));
	for (i = 27; i >= 0; i--) {
		zassert_ok(bflash_pending_put(&benv.flash, 3, (unsigned int)i, &big[i * 150],
					      MIN(150u, n - (size_t)i * 150u)));
	}
	/* NOR is written once per erase: a chunk cannot be written again. */
	zassert_not_equal(bflash_pending_put(&benv.flash, 3, 4, big, 150), 0);
	zassert_equal(bflash_pending_peek(&benv.flash, 3, &back), BFLASH_PENDING_EMPTY);
	zassert_ok(bflash_pending_body(&benv.flash, 3, scratch, n));
	zassert_mem_equal(scratch, big, n);
	zassert_ok(bflash_pending_seal(&benv.flash, 3, &h, big));
	zassert_equal(bflash_pending_check(&benv.flash, 3, &back, scratch, sizeof(scratch)),
		      BFLASH_PENDING_LIVE);
	zassert_equal(back.xfer_id, 0xBEEF);
	zassert_equal(back.len, n);
	zassert_ok(bflash_pending_consume(&benv.flash, 3));
	zassert_equal(bflash_pending_check(&benv.flash, 3, &back, scratch, sizeof(scratch)),
		      BFLASH_PENDING_CONSUMED, "a consumed record is still intact");
	/* A sealed header over other bytes fails the CRC. */
	zassert_ok(bflash_pending_erase(&benv.flash, 4));
	zassert_ok(bflash_pending_put(&benv.flash, 4, 0, big, 150));
	h.len = 150;
	big[0] ^= 1;
	zassert_ok(bflash_pending_seal(&benv.flash, 4, &h, big));
	big[0] ^= 1;
	zassert_equal(bflash_pending_check(&benv.flash, 4, &back, scratch, sizeof(scratch)),
		      BFLASH_PENDING_EMPTY);
	/* Arguments. */
	zassert_equal(bflash_pending_put(&benv.flash, 5, BFLASH_PENDING_CHUNKS, big, 10), -EINVAL);
	zassert_equal(bflash_pending_put(&benv.flash, 5, 0, big, 151), -EINVAL);
	zassert_equal(bflash_pending_put(&benv.flash, 5, 0, big, 0), -EINVAL);
	zassert_equal(bflash_pending_erase(&benv.flash, benv.flash.geom.pending_slots), -EINVAL);
}

ZTEST(bridge_flash, test_read_cache)
{
	uint8_t w[256] __aligned(4);
	uint8_t r[40];
	uint32_t base = 20u * MIB;
	uint32_t hits;

	env_fresh(0u);
	for (size_t i = 0; i < sizeof(w); i++) {
		w[i] = (uint8_t)i;
	}
	zassert_ok(bflash_erase(&benv.flash, base, 4096u));
	zassert_ok(bflash_write(&benv.flash, base, w, sizeof(w)));
	/* Reads that straddle a line boundary. */
	zassert_ok(bflash_cached_read(&benv.flash, base + CONFIG_CTAG_BRIDGE_READ_CACHE_LINE - 7u,
				      r, sizeof(r)));
	zassert_mem_equal(r, &w[CONFIG_CTAG_BRIDGE_READ_CACHE_LINE - 7u], sizeof(r));
	hits = benv.flash.hits;
	zassert_ok(bflash_cached_read(&benv.flash, base + 3u, r, 10u));
	zassert_equal(benv.flash.hits, hits + 1u);
	zassert_mem_equal(r, &w[3], 10u);
	/* Invalidation: rewritten flash is read again. */
	zassert_ok(bflash_erase(&benv.flash, base, 4096u));
	bflash_cache_invalidate(&benv.flash);
	zassert_ok(bflash_cached_read(&benv.flash, base + 3u, r, 10u));
	zassert_equal(r[0], 0xFF);
}

ZTEST_SUITE(bridge_flash, NULL, NULL, NULL, NULL, NULL);
