/*
 * Raw NVS on the board's storage_partition (docs/firmware-notes.md 9): no
 * settings subsystem. Two entries: the ctag_txn display record
 * (TAG_STORE_ID_RECORD, 60 bytes) and the authenticated epoch
 * (TAG_STORE_ID_EPOCH, 12 bytes). An NVS write is atomic: the data is written
 * first and the CRC-protected allocation entry last, so a power loss leaves
 * the previous value readable.
 */
#include <errno.h>

#include <zephyr/devicetree.h>
#include <zephyr/kvss/nvs.h>
#include <zephyr/storage/flash_map.h>

#include "app.h"

static struct nvs_fs fs;
static bool mounted;

int store_init(void)
{
	/* One NVS sector per flash erase block: 1 KiB on nRF51, 4 KiB on nRF52. */
	fs.flash_device = PARTITION_DEVICE(storage_partition);
	fs.offset = PARTITION_OFFSET(storage_partition);
	fs.sector_size = DT_PROP(DT_CHOSEN(zephyr_flash), erase_block_size);
	fs.sector_count = PARTITION_SIZE(storage_partition) / fs.sector_size;
	mounted = nvs_mount(&fs) == 0;
	return mounted ? 0 : -EIO;
}

int tag_hal_store_read(uint16_t id, void *buf, size_t len)
{
	return mounted ? (int)nvs_read(&fs, id, buf, len) : -EIO;
}

int tag_hal_store_write(uint16_t id, const void *buf, size_t len)
{
	return mounted ? (int)nvs_write(&fs, id, buf, len) : -EIO;
}
