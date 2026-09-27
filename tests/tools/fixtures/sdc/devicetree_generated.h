/*
 * Synthetic excerpt of devicetree_generated.h: nRF52 DK (nRF52832) with the
 * default SoftDevice Controller chosen.
 */
#define DT_N_S_soc_S_memory_20000000_REG_IDX_0_VAL_SIZE 65536 /* 0x10000 */
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_REG_IDX_0_VAL_SIZE 524288 /* 0x80000 */
#define DT_N_NODELABEL_storage_partition    DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_7a000
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_7a000_REG_IDX_0_VAL_ADDRESS 499712 /* 0x7a000 */
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_7a000_REG_IDX_0_VAL_SIZE 24576 /* 0x6000 */
#define DT_N_S_soc_S_radio_40001000_S_bt_hci_sdc_P_compatible {"nordic,bt-hci-sdc"}
#define DT_CHOSEN_zephyr_sram                    DT_N_S_soc_S_memory_20000000
#define DT_CHOSEN_zephyr_flash                   DT_N_S_soc_S_flash_controller_4001e000_S_flash_0
#define DT_CHOSEN_zephyr_bt_hci                  DT_N_S_soc_S_radio_40001000_S_bt_hci_sdc
#define DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc 1
#define DT_FOREACH_OKAY_nordic_bt_hci_sdc(fn) fn(DT_N_S_soc_S_radio_40001000_S_bt_hci_sdc)
