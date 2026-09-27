/*
 * Synthetic excerpt of devicetree_generated.h: Laowu BW (nRF51822 QFAB),
 * Zephyr controller chosen, unrelated zephyr,bt-hci-entropy node okay.
 */
#define DT_N_S_soc_S_memory_20000000_REG_IDX_0_VAL_ADDRESS 536870912 /* 0x20000000 */
#define DT_N_S_soc_S_memory_20000000_REG_IDX_0_VAL_SIZE 16384 /* 0x4000 */
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_REG_IDX_0_VAL_ADDRESS 0 /* 0x0 */
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_REG_IDX_0_VAL_SIZE 131072 /* 0x20000 */
#define DT_N_NODELABEL_storage_partition    DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_1f000
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_1f000_REG_IDX_0_VAL_ADDRESS 126976 /* 0x1f000 */
#define DT_N_S_soc_S_flash_controller_4001e000_S_flash_0_S_partitions_S_partition_1f000_REG_IDX_0_VAL_SIZE 4096 /* 0x1000 */
#define DT_N_S_soc_S_radio_40001000_S_bt_hci_controller_COMPAT_MATCHES_zephyr_bt_hci_ll_sw_split 1
#define DT_N_S_soc_S_radio_40001000_S_bt_hci_controller_P_compatible {"zephyr,bt-hci-ll-sw-split"}
#define DT_N_S_bt_hci_entropy_P_compatible {"zephyr,bt-hci-entropy"}
#define DT_CHOSEN_zephyr_sram                    DT_N_S_soc_S_memory_20000000
#define DT_CHOSEN_zephyr_sram_EXISTS             1
#define DT_CHOSEN_zephyr_flash                   DT_N_S_soc_S_flash_controller_4001e000_S_flash_0
#define DT_CHOSEN_zephyr_flash_EXISTS            1
#define DT_CHOSEN_zephyr_bt_hci                  DT_N_S_soc_S_radio_40001000_S_bt_hci_controller
#define DT_CHOSEN_zephyr_bt_hci_EXISTS           1
#define DT_COMPAT_HAS_OKAY_zephyr_bt_hci_entropy 1
#define DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split 1
#define DT_FOREACH_OKAY_zephyr_bt_hci_entropy(fn) fn(DT_N_S_bt_hci_entropy)
#define DT_FOREACH_OKAY_zephyr_bt_hci_ll_sw_split(fn) fn(DT_N_S_soc_S_radio_40001000_S_bt_hci_controller)
