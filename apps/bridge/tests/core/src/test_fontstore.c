/* Font store: installation, boot validation, slot flip, FLASH_TEST. */
#include <string.h>

#include "common.h"

static uint8_t pack_copy[4096];

ZTEST(bridge_fonts, test_install_and_boot)
{
	uint8_t id[8];
	struct bflash_dir_record r;

	env_fresh(0u);
	zassert_false(fontstore_active_id(&benv.fonts, id));
	/* Odd chunk sizes exercise the 4-byte write carry. */
	zassert_equal(install_pack(fixture_pack, fixture_pack_len, 333u), CTAG_STATUS_OK);
	zassert_true(fontstore_active_id(&benv.fonts, id));
	zassert_mem_equal(id, fixture_pack_id, 8);
	zassert_equal(benv.fonts.active.slot, 0);
	zassert_true(fontstore_has_strike(&benv.fonts, 1, 16));
	zassert_false(fontstore_has_strike(&benv.fonts, 1, 32));
	/* A reset: the directory and the pack are validated again at boot. */
	env_boot(0u);
	zassert_true(fontstore_active_id(&benv.fonts, id));
	/* The next install goes to the other slot and flips the directory. */
	zassert_equal(install_pack(fixture_pack, fixture_pack_len, 1u), CTAG_STATUS_OK);
	zassert_equal(benv.fonts.active.slot, 1);
	zassert_ok(bflash_dir_active(&benv.flash, &r, NULL));
	zassert_equal(r.seq, 2);
	zassert_equal(r.size, fixture_pack_len);
}

ZTEST(bridge_fonts, test_boot_rejects_corrupt_pack)
{
	uint8_t id[8];
	uint32_t base;

	env_fresh(0u);
	install_fixture_pack();
	base = bflash_slot_offset(&benv.flash, benv.fonts.active.slot);
	/* A glyph index byte: the strike's index CRC fails at boot. */
	flash_mem()[base + 304 + 5] ^= 0x10;
	env_boot(0u);
	zassert_true(benv.fonts.has_record);
	zassert_false(fontstore_active_id(&benv.fonts, id), "a corrupt pack is not active");
	zassert_equal(benv.fonts.last_open_status, CTAG_STATUS_CRC_ERROR);
	/* ... and FONT_BEGIN still targets the other slot (the record stays). */
	uint8_t digest[32] = {0};
	uint8_t slot;

	zassert_equal(fontstore_begin(&benv.fonts, 100, digest, fixture_pack_id, &slot),
		      CTAG_STATUS_OK);
	zassert_equal(slot, 1);
	fontstore_abort(&benv.fonts);
}

ZTEST(bridge_fonts, test_install_errors)
{
	uint8_t digest[32];
	uint8_t id[8];
	uint8_t slot;

	env_fresh(0u);
	sha256(fixture_pack, fixture_pack_len, digest);
	/* Order of sim/flash.py: size checks at FONT_BEGIN. */
	zassert_equal(fontstore_begin(&benv.fonts, 0, digest, fixture_pack_id, &slot),
		      CTAG_STATUS_INVALID);
	zassert_equal(fontstore_begin(&benv.fonts, 8u * MIB + 1u, digest, fixture_pack_id, &slot),
		      CTAG_STATUS_TOO_LARGE);
	/* FONT_DATA without FONT_BEGIN, and out of sequence. */
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, 10), CTAG_STATUS_NOT_FOUND);
	zassert_equal(fontstore_begin(&benv.fonts, (uint32_t)fixture_pack_len, digest,
				      fixture_pack_id, &slot),
		      CTAG_STATUS_OK);
	zassert_equal(fontstore_data(&benv.fonts, 10, fixture_pack, 10), CTAG_STATUS_INVALID);
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, 0), CTAG_STATUS_INVALID);
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, fixture_pack_len + 1u),
		      CTAG_STATUS_INVALID);
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, 100), CTAG_STATUS_OK);
	/* Commit before every byte arrived. */
	zassert_equal(fontstore_commit(&benv.fonts, &test_sha, scratch, 512, id),
		      CTAG_STATUS_INCOMPLETE);
	zassert_equal(fontstore_commit(&benv.fonts, &test_sha, scratch, 512, id),
		      CTAG_STATUS_NOT_FOUND, "the install ended at the first commit");
	zassert_false(fontstore_active_id(&benv.fonts, id));

	/* A wrong FONT_BEGIN digest. */
	digest[0] ^= 1;
	zassert_equal(install_pack(fixture_pack, fixture_pack_len, 512), CTAG_STATUS_OK);
	zassert_equal(fontstore_begin(&benv.fonts, (uint32_t)fixture_pack_len, digest,
				      fixture_pack_id, &slot),
		      CTAG_STATUS_OK);
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, fixture_pack_len),
		      CTAG_STATUS_OK);
	zassert_equal(fontstore_commit(&benv.fonts, &test_sha, scratch, 512, id),
		      CTAG_STATUS_DIGEST_MISMATCH);
	zassert_true(fontstore_active_id(&benv.fonts, id), "the active pack is untouched");
	zassert_equal(benv.fonts.active.slot, 0);

	/* A pack whose content hash does not match (right digest of the bytes). */
	memcpy(pack_copy, fixture_pack, fixture_pack_len);
	pack_copy[fixture_pack_len - 1] ^= 0x01; /* bitmap byte: CRCs pass, content hash fails */
	zassert_equal(install_pack(pack_copy, fixture_pack_len, 512),
		      CTAG_STATUS_DIGEST_MISMATCH);
	/* A pack id other than FONT_BEGIN's. */
	sha256(fixture_pack, fixture_pack_len, digest);
	zassert_equal(fontstore_begin(&benv.fonts, (uint32_t)fixture_pack_len, digest,
				      (const uint8_t *)"ABCDEFGH", &slot),
		      CTAG_STATUS_OK);
	zassert_equal(fontstore_data(&benv.fonts, 0, fixture_pack, fixture_pack_len),
		      CTAG_STATUS_OK);
	zassert_equal(fontstore_commit(&benv.fonts, &test_sha, scratch, 512, id),
		      CTAG_STATUS_INVALID);
	zassert_equal(benv.fonts.active.slot, 0);
	zassert_equal(benv.fonts.installs, 1);
}

ZTEST(bridge_fonts, test_session_view_blocks_overwrite)
{
	struct fontstore_view v;
	uint8_t digest[32];
	uint8_t slot;

	env_fresh(0u);
	install_fixture_pack(); /* slot 0 */
	zassert_true(fontstore_view_open(&benv.fonts, &v));
	install_fixture_pack(); /* slot 1 active; the view keeps reading slot 0 */
	zassert_equal(benv.fonts.active.slot, 1);
	sha256(fixture_pack, fixture_pack_len, digest);
	zassert_equal(fontstore_begin(&benv.fonts, (uint32_t)fixture_pack_len, digest,
				      fixture_pack_id, &slot),
		      CTAG_STATUS_BUSY, "slot 0 is still read by a session");
	zassert_true(v.src.has_strike(v.src.ctx, 1, 16), "the view still reads its slot");
	fontstore_view_close(&benv.fonts, &v);
	zassert_equal(fontstore_begin(&benv.fonts, (uint32_t)fixture_pack_len, digest,
				      fixture_pack_id, &slot),
		      CTAG_STATUS_OK);
	zassert_equal(slot, 0);
	fontstore_abort(&benv.fonts);
}

static size_t find(const struct fontstore_test_item *it, size_t n, uint32_t off)
{
	for (size_t i = 0; i < n; i++) {
		if (it[i].offset == off) {
			return i;
		}
	}
	return n;
}

ZTEST(bridge_fonts, test_flash_test)
{
	struct fontstore_test_item it[FONTSTORE_TEST_MAX];
	const uint32_t slot = 8u * MIB;
	size_t n, i;

	env_fresh(0u);
	/* No pack: slot 0 is the inactive one. Positions ascend. */
	n = fontstore_flash_test(&benv.fonts, it, ARRAY_SIZE(it));
	zassert_equal(n, 5);
	zassert_equal(it[0].offset, 0);
	zassert_equal(it[1].offset, slot - 4096u);
	zassert_equal(it[2].offset, 16u * MIB - 4096u, "below the 16 MiB boundary (slot 1)");
	zassert_equal(it[3].offset, 16u * MIB, "the 16 MiB boundary...");
	zassert_equal(it[3].status, CTAG_STATUS_BUSY, "...is directory sector A here");
	zassert_equal(it[4].offset, 32u * MIB - 4096u, "the device's last sector");
	for (i = 0; i < n; i++) {
		if (i != 3) {
			zassert_equal(it[i].status, CTAG_STATUS_OK, "item %zu", i);
		}
	}
	install_fixture_pack(); /* slot 0 active */
	n = fontstore_flash_test(&benv.fonts, it, ARRAY_SIZE(it));
	/* Inactive slot 1 = [8, 16) MiB: its first and last sectors; the last is
	 * also the sector below the 16 MiB boundary (de-duplicated). */
	zassert_equal(n, 4);
	zassert_equal(it[0].offset, slot);
	zassert_equal(it[1].offset, 16u * MIB - 4096u);
	zassert_equal(it[0].status, CTAG_STATUS_OK);
	zassert_equal(it[1].status, CTAG_STATUS_OK);
	zassert_equal(it[2].status, CTAG_STATUS_BUSY);
	zassert_equal(it[3].status, CTAG_STATUS_OK);
	/* The active pack survives. */
	env_boot(0u);
	zassert_true(benv.fonts.valid);
	/* The sector below 16 MiB now lies in the active slot. */
	install_fixture_pack(); /* slot 1 active, slot 0 inactive */
	n = fontstore_flash_test(&benv.fonts, it, ARRAY_SIZE(it));
	i = find(it, n, 16u * MIB - 4096u);
	zassert_true(i < n);
	zassert_equal(it[i].status, CTAG_STATUS_BUSY, "inside the active slot");
	zassert_equal(it[find(it, n, 0)].status, CTAG_STATUS_OK);
	/* An open installation makes every position BUSY. */
	uint8_t digest[32];
	uint8_t s;

	sha256(fixture_pack, fixture_pack_len, digest);
	zassert_equal(fontstore_begin(&benv.fonts, 100, digest, fixture_pack_id, &s), 0);
	n = fontstore_flash_test(&benv.fonts, it, ARRAY_SIZE(it));
	for (i = 0; i < n; i++) {
		zassert_equal(it[i].status, CTAG_STATUS_BUSY);
	}
	fontstore_abort(&benv.fonts);
	zassert_true(benv.fonts.valid);
}

ZTEST_SUITE(bridge_fonts, NULL, NULL, NULL, NULL, NULL);
