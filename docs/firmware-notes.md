# Firmware notes for the pinned SDK (NCS v3.4.1, sdk-zephyr `ncs-v3.4.1` = Zephyr 4.4.2)

Verified against the pinned sources and by container builds (2026-09-27). Paths
are relative to `zephyr/` (Z) or `nrf/` (N). Nothing here was measured on real
hardware yet; stack sizes in particular must be re-measured with
`CONFIG_THREAD_ANALYZER` on each board during qualification.

## Corrections to plan assumptions

1. **Never apply the `bt-ll-sw-split` snippet on nRF51.** Its overlay references
   `&bt_hci_sdc`, which nRF51 lacks (`undefined node label 'bt_hci_sdc'`). nRF51
   already defaults to the Zephyr controller. Apply the snippet to nRF52 only.
2. `BT_LL_SW_SPLIT` does `select EXPERIMENTAL` (Z `subsys/bluetooth/controller/Kconfig:143-151`):
   every image warns "Experimental symbol BT_LL_SW_SPLIT is enabled", and NCS
   warns "SoC nrf51822 is maintained by the Zephyr community". CI must not treat
   these warnings as errors.
3. **The Zephyr controller never scans and initiates simultaneously.** It clears
   LE-states bits 22/23 (Z `controller/hci/hci.c:1594-1598`), so
   `BT_SCAN_AND_INITIATE_IN_PARALLEL` has no effect. The bridge's
   suspend → create → resume design is mandatory.
4. **Do not use the legacy mesh advertiser on the bridge.** With
   `BT_MESH_ADV_LEGACY` and no GATT server, `bt_mesh_suspend()` can block
   forever (`mesh/adv_legacy.c:178` waits in `bt_mesh_adv_get(K_FOREVER)`;
   `:264` `k_thread_join(..., K_FOREVER)`). Its help text says it is unsupported
   in NCS. Use `BT_MESH_ADV_EXT` (default when `BT_EXT_ADV=y`, which is default
   with mesh); its disable is synchronous (`adv_ext.c:606-642`).
5. **nRF51 at 16 KiB** reaches 2 KiB free RAM only with shrunken hidden
   controller stacks (see `tight` below) and the lean PSA profile (2164 B free;
   1744 B without the stack override). An RC 32 kHz source costs 64–152 B more
   (use `CLOCK_CONTROL_NRF_K32SRC_RC_CALIBRATION_MAX_SKIP=0`: 2076 B free).
6. **Never use the PSA key-derivation API for HKDF on tags**:
   `psa_key_derivation_operation_t` is 1024 B and PSA HKDF used 1544 B of stack
   on Cortex-M0 (QEMU). HKDF built from `psa_mac_compute()` uses 936 B.
7. The pinned trees call nothing "audited". Oberon ships a Cortex-M0 library that
   links on nRF51 (`nrfxlib/crypto/nrf_oberon/lib/cortex-m0/soft-float/liboberon_3.0.20.a`).
   NCS feature tables list the Oberon driver only for nRF52832/833/840
   (N `doc/nrf/security/crypto/crypto_supported_features.rst:190-200`), not
   810/811. The open-source alternative, `CONFIG_PSA_CRYPTO_PROVIDER_MBEDTLS=y`
   (TF-PSA-Crypto from source), also builds on nRF51. Record the provider per
   target in the qualification report.
8. **Keep `CONFIG_BT_CTLR_CRYPTO=y`** (default). `=n` turns on `BT_HOST_CRYPTO`
   (Z `host/Kconfig:226-233`), dragging in nrf_security + PSA + mbedTLS heap
   (~+1.8 KiB RAM). With SMP off and controller crypto on, the host needs no PSA.
9. **nRF52840 default entropy is CryptoCell** (`zephyr,entropy = &cryptocell`,
   Z `dts/arm/nordic/nrf52840.dtsi:15`) which links the closed CC3XX library.
   Set `zephyr,entropy = &rng;` and `CONFIG_ENTROPY_CC3XX=n` (as `nrf_desktop`
   does). Mesh requires PSA (`BT_MESH_CRYPTO_LIB` selects it); in NCS that means
   the Oberon binary unless `PSA_CRYPTO_PROVIDER_MBEDTLS=y`.
10. native_sim/twister in the toolchain image needs `make`
    (`apt-get install -y make`); then `west twister -p native_sim -p native_sim/native/64` works.

## 1. Controller selection and what CI asserts

Snippet `Z snippets/bt-ll-sw-split`: appends `bt-ll-sw-split.conf` (`CONFIG_BT=y`)
and an overlay enabling `&bt_hci_controller`, disabling `&bt_hci_sdc`, and
choosing `zephyr,bt-hci = &bt_hci_controller`. nRF51: `nrf51822.dtsi:9` chooses
`&bt_hci_controller` (`zephyr,bt-hci-ll-sw-split`); no SDC node; `BT_LL_SW_SPLIT`
defaults y. nRF52810/811/832/840 dtsi choose `&bt_hci_sdc` by default
(N `subsys/bluetooth/controller/Kconfig:7-60`: `BT_LL_SOFTDEVICE` default y,
selects `MPSL`).

| Check | Zephyr controller | SDC (control build) |
|---|---|---|
| `.config` `CONFIG_BT_LL_SW_SPLIT=y` | yes | no |
| `.config` `CONFIG_BT_LL_SOFTDEVICE=y` / `_PERIPHERAL=y` | no | yes |
| `.config` exactly `^CONFIG_MPSL=y$` | no | yes |
| flash sync | `CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER=y` | `…_SYNC_MPSL=y` |
| edtlib `edt.chosen_node("zephyr,bt-hci").compats` | `['zephyr,bt-hci-ll-sw-split']` | `['nordic,bt-hci-sdc']` |
| edtlib `edt.compat2okay["nordic,bt-hci-sdc"]` | empty | non-empty |
| `devicetree_generated.h` | `DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split 1` | `DT_COMPAT_HAS_OKAY_nordic_bt_hci_sdc 1` |
| `zephyr.map` `libsoftdevice_controller*.a`, `libmpsl*.a`, `sdc_*`, `mpsl_*` | none | present |
| `zephyr.map` `lll_init`, `ticker_init`, `radio_isr_set`, `ll_adv_enable` | present | absent |

Traps: anchor the MPSL check to `^CONFIG_MPSL=y$` (integer `CONFIG_MPSL_*`
symbols such as `CONFIG_MPSL_WORK_STACK_SIZE` still appear with the Zephyr
controller), and match `CONFIG_SOC_FLASH_NRF_RADIO_SYNC_MPSL=y` exactly
(`…_SYNC_MPSL_TIMESLOT_SESSION_COUNT=0` also appears with the Zephyr
controller). Do not reject every `bt-hci` compatible — `nrf_common.dtsi:35` has an
unrelated `zephyr,bt-hci-entropy` node. Map symbol checks must ignore the
"Discarded input sections" block of `zephyr.map`.

**Re-configuration trap (no sysbuild):** re-running CMake on an existing build
directory (new `-D` arguments or an edited `.conf`) aborts with "malformed
string literal … MBEDTLS_CONFIG_FILE": nrf_security caches `CONFIG_*_CONFIG_FILE`
(N `subsys/nrf_security/configs/config_extra.cmake.in:22-24`) and Zephyr's
`kconfig.cmake` reads every cached `CONFIG_*` back as an assignment.
`tools/build.py` passes `-UCONFIG_*`; manual builds need the same or a pristine
build.

## 2. Controller scheduling (Z `subsys/bluetooth/controller/Kconfig.ll_sw_split`)

- `BT_CTLR_SCHED_ADVANCED` (769-790): default y with `BT_CENTRAL` → on for the bridge.
  Related: `BT_CTLR_CENTRAL_SPACING` (814, 0), `BT_CTLR_CENTRAL_RESERVE_MAX` (833, y),
  `BT_CTLR_PERIPHERAL_RESERVE_MAX` (847, y), `BT_CTLR_ADV_RESERVE_MAX` (470, y with ext adv),
  `BT_CTLR_EVENT_OVERHEAD_RESERVE_MAX` (871, y).
- `BT_CTLR_SCAN_UNRESERVED` (1241-1247): `depends on BT_OBSERVER && !BT_CTLR_LOW_LAT`,
  `default y if BT_MESH` — continuous scanner takes no reservation.
- `BT_CTLR_LOW_LAT` (937): default y on nRF51 (+ `_ULL`, `_ULL_DONE`).
- Also: `BT_CTLR_XTAL_ADVANCED` (738), `BT_CTLR_LLL_PRIO`/`ULL_HIGH_PRIO`/`ULL_LOW_PRIO`
  (890/898/908), `BT_TICKER_EXT_SLOT_WINDOW_YIELD` (1337), `BT_CTLR_JIT_SCHEDULING` (1392),
  `BT_CTLR_CONN_INTERVAL_LOW_LATENCY` (1202).
- Create-while-scanning refusal: controller `ll_sw/ull_central.c:104-107` returns
  `BT_HCI_ERR_CMD_DISALLOWED`; the host fails first: `host/conn.c:4052-4055`
  `bt_conn_le_create()` → `-EAGAIN`; `host/scan.c:503-506` scan start while
  initiating → `-EPERM` (so `bt_mesh_resume()` fails until initiation ends).
- A second central connection: `ll_create_connection()` otherwise refuses only
  a peer that is already connected (`BT_HCI_ERR_CONN_ALREADY_EXISTS`), so with
  `BT_MAX_CONN=2` the bridge initiates beside an open connection (one
  initiation at a time: the scan/initiator set is single); with
  `BT_CTLR_SCHED_ADVANCED` the new central's events are placed beside the
  existing ones (`BT_CTLR_CENTRAL_SPACING`, 0 = the computed reservation).
  The nRF52840 bridge holds two tag sessions this way (bridge-firmware §4).
- `BT_BUF_EVT_RX_COUNT` must exceed `BT_BUF_ACL_TX_COUNT` (a host build
  assertion): the nRF52840 bridge's 14 ACL TX buffers need 16 event buffers.

## 3. Mesh

- `int bt_mesh_suspend(void)` / `int bt_mesh_resume(void)` (Z `include/zephyr/bluetooth/mesh/main.h:647,656`).
  Suspend (`mesh/main.c:441-501`): `-EINVAL` not provisioned, `-EBUSY` provisioning
  link active, `-EALREADY`; scan-disable errors propagate (transient `-EBUSY`
  possible). Stops, in order: scanning, heartbeat publication, beacons, periodic
  model publication, delayable messages (completed `-ENODEV`), provisionee,
  advertiser. Leaves segmented-TX retransmit timers (they retry after resume),
  RX/discard timers (10 s), IV update, RPL, settings, CDB. New adv buffers are
  refused while suspended (`adv.c:95-98`). Thread context only; blocks.
  Resume (`main.c:516-574`): advertiser, scanning (same params), heartbeat,
  beacons, publication; if the scan restart fails the node is marked suspended
  again (539-544) — e.g. `-EPERM` while still initiating.
- Mesh scanner: `adv.c:383-402` passive, interval = window = 30 ms,
  `bt_mesh_scan_cb`. The host delivers every report to mesh then to every
  `bt_le_scan_cb_register()` listener (`host/scan.c:665-685`), so the bridge sees
  tags' `ADV_IND`s. Passive → no scan responses. The app must never start/stop
  scanning itself.
- Advertiser backend: `choice BT_MESH_ADV` defaults to `BT_MESH_ADV_EXT` when
  `BT_EXT_ADV`. NCS overrides with ext adv (N `subsys/bluetooth/mesh/Kconfig:56-87`):
  `BT_EXT_ADV_MAX_ADV_SET` 5, `RELAY_ADV_SETS` 2, `RELAY_RETRANSMIT_COUNT` **0**,
  `RELAY_BUF_COUNT` 10.
- Kconfig (`mesh/Kconfig`): `RELAY` n (484); `NETWORK_TRANSMIT_COUNT/INTERVAL` 2/20;
  `DEFAULT_TTL` 7; `TX_SEG_MAX`/`RX_SEG_MAX` default 3, range 1–32; `TX/RX_SEG_MSG_COUNT` 1;
  `SEG_BUFS` 64; `CDB` n with `CDB_NODE/SUBNET/APP_KEY_COUNT` 8/1/1; `PROVISIONER`
  depends on `CDB`, `PROV`, `PB_ADV`; `CFG_CLI` n, `CFG_CLI_TIMEOUT` 5000.
- Crypto: `BT_MESH_CRYPTO_LIB` selects `PSA_CRYPTO` + CMAC/ECB/CCM/HMAC/SHA-256/ECDH/P-256.
  With `BT_SETTINGS`, NCS enables `BT_MESH_SECURE_STORAGE`: every key incl. each
  CDB device key is a persistent PSA key → size `MBEDTLS_PSA_KEY_SLOT_COUNT`
  (NCS default 32) for the gateway's node count.
- 160-byte vendor params: `BT_MESH_TX_SDU_MAX = 12 × SEG_MAX` minus MIC; 3 + 160 =
  163 B → 14 segments (4-B MIC) / 15 (8-B MIC); `SEG_MAX=16` works. A second
  concurrent segmented send returns `-EBUSY` while `TX_SEG_MSG_COUNT=1`.
- Config client (`cfg_cli.h`): `bt_mesh_cfg_cli_app_key_add(net_idx, addr, key_net_idx, key_app_idx, app_key[16], *status)` (842);
  `bt_mesh_cfg_cli_mod_app_bind_vnd(net_idx, addr, elem_addr, mod_app_idx, mod_id, cid, *status)` (938),
  which returns `-EINVAL` for `cid` 0xFFFF (`CID_NVAL`, its marker for a SIG model; `cfg_cli.c`
  1565) — `MESH_COMPANY_ID` — so the gateway builds Model App Bind (`0x803D`: element, app
  index, company, model, all LE16) itself; a status with company 0xFFFF reads like a SIG
  model's;
  `bt_mesh_cfg_cli_relay_set(net_idx, addr, new_relay, new_transmit, *status, *transmit)` (773);
  `bt_mesh_cfg_cli_ttl_set(net_idx, addr, val, *ttl)` (623);
  `bt_mesh_cfg_cli_net_transmit_set(net_idx, addr, val, *transmit)` (728);
  `bt_mesh_cfg_cli_node_reset(net_idx, addr, bool *status)` (502);
  `bt_mesh_cfg_cli_comp_data_get` (523); `bt_mesh_cfg_cli_timeout_get/_set` (1591/1597);
  `BT_MESH_MODEL_CFG_CLI(cli_data)` (349). NULL response pointers = async via
  `struct bt_mesh_cfg_cli_cb`; only one synchronous request at a time.
  `BT_MESH_TRANSMIT(count, int_ms)` (`access.h:453`).
- CDB (`cdb.h`): `bt_mesh_cdb_create(const uint8_t key[16])` (118, `-EALREADY` when
  loaded from settings); `bt_mesh_cdb_node_alloc(uuid, addr, num_elem, net_idx)` (159,
  no UUID de-dup); `_node_get` (206), `_node_del(node, bool store)` (182), `_node_store` (212),
  `_node_foreach` (265), `_free_addr_get` (169); subnet/app key alloc/get/del/store/import/export
  (276-415). `struct bt_mesh_cdb_node` has `uuid[16]`, `addr`, `net_idx`, `num_elem`,
  `struct bt_mesh_key dev_key` (PSA handle), flags incl. `BT_MESH_CDB_NODE_CONFIGURED`.
- Provisioning: `bt_mesh_provision_adv(uuid[16], net_idx, addr, attention_duration)`
  (`main.h:497`; the provisioner allocates the CDB node, `provisioner.c:286-298`).
  `struct bt_mesh_prov` `unprovisioned_beacon(uuid[16], oob_info, uint32_t *uri_hash)`
  (248, fires per beacon — de-duplicate), `node_added(net_idx, uuid, addr, num_elem)` (312).
- Models (`access.h`/`msg.h`): `BT_MESH_MODEL_OP_3(b0, cid)` (213);
  `struct bt_mesh_model_op { opcode; ssize_t len; int (*func)(const struct bt_mesh_model *, struct bt_mesh_msg_ctx *, struct net_buf_simple *); }`;
  `BT_MESH_LEN_EXACT`/`BT_MESH_LEN_MIN`; `BT_MESH_MODEL_VND_CB(_company, _id, _op, _pub, _user_data, _cb)` (372);
  `bt_mesh_model_send(model, ctx, msg, const struct bt_mesh_send_cb *cb, void *cb_data)` (797);
  `struct bt_mesh_send_cb { void (*start)(uint16_t duration, int err, void *); void (*end)(int err, void *); }` (764);
  `BT_MESH_MODEL_BUF_DEFINE` (`msg.h:72`); `BT_MESH_MSG_CTX_INIT_APP` (133);
  composition = `struct bt_mesh_comp` (977; no `BT_MESH_COMP` macro).
- Settings: `CONFIG_FLASH`, `FLASH_MAP`, `NVS`, `SETTINGS`, `BT_SETTINGS`; order
  `bt_enable` → `bt_mesh_init` → `settings_load()`. Backend defaults to ZMS if ZMS is on, else NVS.
- Samples: Z `samples/bluetooth/mesh_provisioner` (closest to the gateway);
  `nrf/samples/bluetooth/mesh/chat` (vendor model reference). No sample combines
  mesh + central + Zephyr controller.
- **Static OOB provisioning (protocol v2, verified 2026-09-28).** Kconfig
  (`subsys/bluetooth/mesh/Kconfig:369-378`): `BT_MESH_ECDH_P256_HMAC_SHA256_AES_CCM`
  (default y) and `BT_MESH_OOB_AUTH_REQUIRED` ("OOB authentication mandates to
  use HMAC SHA256", depends on it; it sets the OOB-required bit of the
  *provisionee's* capabilities). `CMAC_AES128_AES_CCM` stays on by default.
  Provisioner (`provisioner.c`): on the Capabilities PDU `prov_capabilities()`
  fills `struct bt_mesh_dev_capabilities {elem_count, algorithms,
  pub_key_type, oob_type, output_size, output_actions, input_size,
  input_actions}`, calls the application's `bt_mesh_prov.capabilities`
  callback, then `prov_check_method()`; for static OOB that check only tests
  `caps->oob_type` (`BT_MESH_STATIC_OOB_AVAILABLE` = BIT(0),
  `BT_MESH_OOB_AUTH_REQUIRED` = BIT(1)). The method is chosen from the
  callback: `bt_mesh_auth_method_set_static(const uint8_t *static_val, uint8_t
  size)` (`provisioner.c:759`), `bt_mesh_auth_method_set_input(action, size)`
  (`:739`). The provisioner prefers HMAC-SHA256 when the device offers it.
  A failed provisioning (the device's Provisioning Failed, a confirmation
  mismatch) reaches the application only as `link_close` without
  `node_added`: no reason code.
- **Wiping a network**: `bt_mesh_cdb_clear()` (`cdb.h:126`) removes every
  node, subnet and app key of the CDB (and their settings);
  `bt_mesh_reset()` (`main.h:629`) unprovisions the local node. The gateway
  then reboots so the next `bt_mesh_cdb_create()` starts a new network.

## 4. BLE central (bridge)

- `bt_conn_le_create(peer, create_param, conn_param, **conn)` (`conn.h:1882`);
  `create_param.timeout` is in **10 ms units** (0 → `CONFIG_BT_CREATE_CONN_TIMEOUT` s).
  `BRIDGE_CONN_ATTEMPT_MS = 1000` → `timeout = 100`. `BT_CONN_LE_CREATE_PARAM_INIT(opt, interval, window)` (1822).
- Cancel: `bt_conn_disconnect()` on the connecting conn (`conn.c:1931-1936`);
  `connected()` then fires with `BT_HCI_ERR_UNKNOWN_CONN_ID`.
- GATT client: `bt_gatt_discover` (1902), `bt_gatt_subscribe` (2304),
  `bt_gatt_write_without_response_cb(conn, handle, data, len, sign, func, user_data)` (2114),
  `bt_gatt_write` (2087), `bt_gatt_read` (2038).
- A discovery keeps `params->uuid` until it completes, so the UUID must be
  static: `BT_UUID_GATT_CCC` is `BT_UUID_DECLARE_16()` (`uuid.h:128`, 911), a
  compound literal on the caller's stack. Passed from a step that returns
  before the Find Information responses arrive, no CCC ever matched — every
  setup ended `UNSUPPORTED` on hardware (found 2026-10-06; the gateway's and
  the bridge's `central.c` use a static `ccc_uuid`).
- Buffers: `BT_MAX_CONN`, `BT_BUF_ACL_TX_COUNT` (also sizes controller TX, `ull_conn.c:134-145`),
  `BT_L2CAP_TX_BUF_COUNT` (2–255), `BT_ATT_TX_COUNT`, `BT_BUF_ACL_RX_COUNT_EXTRA`.
  There is **no** `BT_BUF_ACL_RX_COUNT`, `BT_CTLR_TX_BUFFERS` or `BT_ATT_TX_MAX`;
  `BT_CONN_TX_MAX` is deprecated. `BT_GATT_CACHING` needs PSA — keep off.

## 5. Crypto for the tag session

- `NRF_SECURITY` `def_bool y` when `PSA_CRYPTO_PROVIDER_CUSTOM || BUILD_WITH_TFM`
  (N `subsys/nrf_security/Kconfig:21-24`). **Set `CONFIG_PSA_CRYPTO=y` explicitly**
  on nRF51/810/811. `PSA_CRYPTO_DRIVER_OBERON` default y without CRACEN.
- Symbols: `PSA_WANT_ALG_HMAC`, `PSA_WANT_ALG_SHA_256`, `PSA_WANT_ALG_CCM`,
  `PSA_WANT_KEY_TYPE_AES`, `PSA_WANT_AES_KEY_SIZE_128`, `PSA_WANT_KEY_TYPE_HMAC`
  (`PSA_WANT_ALG_HKDF`/`PSA_WANT_KEY_TYPE_DERIVE` not needed with HMAC-built HKDF).
  8-byte CCM tag: `PSA_ALG_AEAD_WITH_SHORTENED_TAG(PSA_ALG_CCM, 8)`.
- TinyCrypt was removed (Z `release-notes-4.3.rst:113`). `CRYPTO_NRF_ECB` depends
  on `!HAS_BT_CTLR`.
- Randomness: controller implements `bt_rand()` with `lll_csrand_get()`;
  `sys_csrand_get()` works (`HARDWARE_DEVICE_CS_GENERATOR`). Nonces from
  `sys_csrand_get()`; `PSA_WANT_GENERATE_RANDOM=n` on tags.
- Stack high-water (qemu_cortex_m0, Oberon): `psa_crypto_init` 196 B; HKDF via
  3 × `psa_mac_compute` 936 B; multi-part HMAC 752 B; SHA-256 streaming 376 B;
  CCM-8 enc+dec 192 B 1192 B (incl. 408 B test buffers). Op structs: MAC 352 B,
  AEAD 344 B, hash 232 B.
- **Protocol v2 crypto does not use PSA**: `lib/secure` runs the verified
  HACL* and Noise* (firmware-libs.md `ctag_secure`). Kernel pieces it uses:
  `sys_heap_init/alloc/free` and `sys_heap_usable_size()`
  (`include/zephyr/sys/sys_heap.h:219`, to wipe a whole block on free) for the
  KaRaMeL allocator, and `sys_csrand_get()` (`include/zephyr/random/random.h:68`)
  for ephemerals, challenges and identity keys.
- **native_sim and KaRaMeL**: native_sim in this SDK builds against the host
  C library (`CONFIG_EXTERNAL_LIBC=y`) and defines `__linux__`, so KaRaMeL's
  `lowstar_endianness.h` includes `<endian.h>`, whose `htole64()` & co. glibc
  defines only with `_DEFAULT_SOURCE`: without it the vendored code links
  against undefined functions. `lib/CMakeLists.txt` compiles `ctag_secure` and
  its vendored sources with `-U__linux__ -D_DEFAULT_SOURCE` on
  `CONFIG_ARCH_POSIX`; the nRF targets take the header's generic
  `__BYTE_ORDER__` branch.

## 6. Boards (HWMv2)

- Reference sets: `boards/nordic/nrf51dk/` (board.yml, `Kconfig.nrf51dk` selecting
  `SOC_NRF51822_QFAC`, `Kconfig.defconfig`, `_defconfig`, `.dts`, `-pinctrl.dtsi`,
  `.yaml`, `board.cmake`, `pre_dt_board.cmake`) and `boards/nordic/nrf52dk/`
  (`nrf52dk_nrf52810{.dts,-pinctrl.dtsi,_defconfig,.yaml}` including `nrf52810_qfaa.dtsi`).
  nRF52811 exists only as `nrf52840dk/nrf52811`.
- SoCs: `SOC_NRF51822_QFAA` (256K/16K), `QFAB` (128K/16K), `QFAC` (256K/32K);
  `SOC_NRF52810_QFAA`, `SOC_NRF52811_QFAA`. **nRF51802 is not a Zephyr SoC**:
  model it as `SOC_NRF51822_QFAA`.
- Out-of-tree boards via `zephyr/module.yml` `build: settings: board_root: .`
  (+ `dts_root`); with sysbuild the board root must come from a module.
- 32 kHz: choice `CLOCK_CONTROL_NRF_SOURCE` (Z `drivers/clock_control/Kconfig.nrf:33-64`),
  `CLOCK_CONTROL_NRF_K32SRC_RC`, `_RC_CALIBRATION` (y), `_CALIBRATION_PERIOD` 4000,
  `_MAX_SKIP` 1 (0 drops the temperature sensor), accuracy 500 ppm (nRF52) / 250 ppm (nRF51).
- DC/DC: nRF52 `&reg { regulator-initial-mode = <NRF5X_REG_MODE_DCDC>; };`
  (`SOC_DCDC_NRF52X` deprecated). nRF51: no Zephyr mechanism — call
  `nrf_power_dcdcen_set(NRF_POWER, true)` from the app, only once the board's
  inductor is verified.

## 7. USB CDC ACM (nRF52840 gateway / bridge maintenance)

Legacy `USB_DEVICE_STACK` is deprecated (removal in 4.5). Use device_next:

```dts
&zephyr_udc0 { cdc_acm_uart0: cdc_acm_uart0 { compatible = "zephyr,cdc-acm-uart"; }; };
```
```
CONFIG_USB_DEVICE_STACK_NEXT=y
CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT=y
CONFIG_SERIAL=y
CONFIG_UART_LINE_CTRL=y
```
Then `DEVICE_DT_GET(DT_NODELABEL(cdc_acm_uart0))` is a normal UART. Replace the
default VID/PID (0x2fe3/0x0004) via the `CDC_ACM_SERIAL_*` Kconfig. Explicit
alternative: `samples/subsys/usb/common/sample_usbd_init.c`.

## 8. External flash (bridge)

- DK node `mx25r64` (`nrf52840dk_nrf52840.dts:250-274`): `nordic,qspi-nor`,
  `sck-frequency = <8000000>`, `jedec-id = [c2 28 17]`, `size = <67108864>` (bits).
- > 16 MiB: QSPI needs both `address-size-32;` and `enter-4byte-addr = <0x01>`
  (build assert `nrf_qspi_nor.c:188-194`); SPI NOR: `enter-4byte-addr` or
  `use-4b-addr-opcodes`. The QSPI driver supports instance 0 only.
- API: `flash_read/write/erase`, `flash_get_page_info_by_offs`,
  `flash_get_write_block_size`. Erase needs 4 KiB alignment; drivers use 64 KiB
  block erases when possible; page layout defaults to 65536.

## 9. Tag storage

NVS/ZMS headers moved to `zephyr/kvss/{nvs,zms}.h` (old path warns). Use raw NVS
with one id (no settings subsystem), optionally `NVS_DATA_CRC`; a single write is
atomic (data first, CRC-protected ATE last, `kvss/nvs/nvs.c:611-632`). ≥ 2 sectors:
2 KiB on nRF51 (1 KiB pages), 8 KiB on nRF52810/811. ~3.0 KB code. Partitions use
`compatible = "zephyr,mapped-partition"`; prefer `PARTITION_OFFSET/SIZE/DEVICE(label)`
(Z `include/zephyr/storage/flash_map.h:401-505`) over deprecated `FIXED_PARTITION_*`.

## 10. zcbor 0.9.1

`ZCBOR_STATE_E(name, num_backups, payload, size, elem_count)`,
`ZCBOR_STATE_D(name, num_backups, payload, size, elem_count, n_flags)`;
encode `zcbor_map_start_encode(state, max)`, `zcbor_map_end_encode`,
`zcbor_uint32_put`, `zcbor_uint64_put`, `zcbor_bstr_encode_ptr`, `zcbor_tstr_put_lit`;
decode `zcbor_map_start_decode`/`_end_decode`, `zcbor_uint32_decode`,
`zcbor_bstr_decode(state, struct zcbor_string *)`, `zcbor_array_at_end`,
`zcbor_any_skip(state, NULL)`. Encoded length = `state->payload - buf`.
**`CONFIG_ZCBOR_CANONICAL=y` is required for definite-length maps**; emit keys
sorted (zcbor does not check order).

## 11. SPI/GPIO for panels

| SoC | SPI | Compatible |
|---|---|---|
| nRF51 | `spi0`, `spi1` (share addresses with `i2c0/1`) | `nordic,nrf-spi` |
| nRF52810 | `spi0` | `nordic,nrf-spim` |
| nRF52811 | `spi0`, `spi1` (`spi1` shares with `i2c0`) | `nordic,nrf-spim` |

Pinctrl `NRF_PSEL(SPIM_SCK|SPIM_MOSI|SPIM_MISO, port, pin)` (also for legacy SPI).
`SPI_DT_SPEC_GET`'s delay argument is deprecated — use DT
`spi-cs-setup-delay-ns` / `spi-cs-hold-delay-ns`. Use `gpio_dt_spec`.

## 12. Measured tag builds (container, `--no-sysbuild`, no real hardware)

Test app: legacy connectable peripheral, one 128-bit service with the four
characteristics, 2 × 256 B reassembly buffers, 200 B plaintext buffer,
work-item pipeline, SPI + DC/CS/RST/BUSY GPIOs, one NVS record, SMP off,
logging off, ATT MTU 23, one connection. nRF51 modelled as Laowu BW
(16 KiB SRAM, 128 KiB flash). "Free" = RAM left after all static allocations
including stacks; heap 0.

| Build | nRF51 (16K/128K) flash / RAM (free) | nRF52810 | nRF52811 |
|---|---|---|---|
| baseline `prj.conf` | RAM overflow by 744 B | 110584 / 21224 (3352) | 110184 / 21224 |
| `min` | 91620 / 14024 (2360) | 93388 / 15320 (9256) | 93004 / 15320 |
| `min` + default PSA | 107892 / 15800 (584) | 109116 / 17096 | — |
| `min` + lean PSA | 103132 / 14384 (2000) | 104740 / 15680 | 104356 / 15680 |
| **nRF51 final**: min + tight + lean + `SYSTEM_WORKQUEUE_STACK_SIZE=1280` | **103132 / 14220 (2164)** | — | — |
| **810/811 final**: min + lean + `SYSTEM_WORKQUEUE_STACK_SIZE=1536` | — | **104740 / 16192 (8384)** | **104356 / 16192** |
| **810/811 safe**: + `BT_TX_PROCESSOR_THREAD=y`, `BT_RX_STACK_SIZE=1200` | — | **104740 / 17776 (6800)** | 104356 / 17776 |
| + LFRC calibration | 104540 / 14372 (2012 ✗) | 106256 / 16344 | — |
| + LFRC, `MAX_SKIP=0` | 103956 / 14308 (2076) | — | — |

nRF51 final flash: 103132 of 126976 B (128 KiB − 4 KiB NVS) = 81.2 % → ~4.8 KB
margin before the 15 % headroom target for the real panel driver and protocol.
Crypto cost: default PSA +15.7–16.3 KB flash, +1776 B RAM (16 key slots 1156 B,
CTR_DRBG 348 B, hash op 232 B, mutexes 72 B); lean PSA +11.4–11.5 KB, +360 B.

On nRF51 the host processes RX on the system work queue (`BT_RECV_WORKQ_SYS`)
without a TX processor thread, so the system work queue carries host RX/TX and
all crypto: never below 1200 B.

### Fragments that produced the smallest working builds

`prj.conf` base: `BT`, `BT_PERIPHERAL`, `BT_DEVICE_NAME="CTag"`, `BT_MAX_CONN=1`,
`BT_SMP=n`, `LOG=n`, `PRINTK=n`, `CONSOLE=n`, `UART_CONSOLE=n`, `SERIAL=n`,
`GPIO`, `SPI`, `FLASH`, `FLASH_MAP`, `NVS`.

`min`:
```
CONFIG_MAIN_STACK_SIZE=512
CONFIG_ISR_STACK_SIZE=1024
CONFIG_IDLE_STACK_SIZE=128
CONFIG_HW_STACK_PROTECTION=n
CONFIG_TIMESLICING=n
CONFIG_ASSERT=n
CONFIG_BT_ASSERT=n
CONFIG_HEAP_MEM_POOL_SIZE=0
CONFIG_CBPRINTF_NANO=y
CONFIG_BOOT_BANNER=n
CONFIG_NCS_BOOT_BANNER=n
CONFIG_NUM_PREEMPT_PRIORITIES=8
CONFIG_BT_TX_PROCESSOR_THREAD=n
CONFIG_BT_RX_STACK_SIZE=800
CONFIG_BT_BUF_EVT_RX_COUNT=4
CONFIG_BT_BUF_EVT_DISCARDABLE_COUNT=1
CONFIG_BT_BUF_ACL_TX_COUNT=2
CONFIG_BT_BUF_ACL_RX_COUNT_EXTRA=1
CONFIG_BT_L2CAP_TX_BUF_COUNT=2
CONFIG_BT_ATT_TX_COUNT=2
CONFIG_BT_GATT_SERVICE_CHANGED=n
CONFIG_BT_GATT_READ_MULTIPLE=n
CONFIG_BT_GATT_READ_MULT_VAR_LEN=n
CONFIG_BT_PHY_UPDATE=n
CONFIG_BT_DATA_LEN_UPDATE=n
CONFIG_BT_HCI_VS=n
CONFIG_BT_GAP_AUTO_UPDATE_CONN_PARAMS=n
CONFIG_BT_CTLR_LE_ENC=n
CONFIG_BT_CTLR_CONN_PARAM_REQ=n
CONFIG_BT_CTLR_EXT_REJ_IND=n
CONFIG_BT_CTLR_PER_INIT_FEAT_XCHG=n
CONFIG_BT_CTLR_MIN_USED_CHAN=n
CONFIG_BT_CTLR_CHAN_SEL_2=n
CONFIG_BT_CTLR_PRIVACY=n
CONFIG_BT_CTLR_FILTER_ACCEPT_LIST=n
CONFIG_BT_CTLR_DTM_HCI=n
CONFIG_BT_CTLR_ADVANCED_FEATURES=y
CONFIG_BT_CTLR_ASSERT_OVERHEAD_START=n
```

`tight` (nRF51 only): `CONFIG_BT_BUF_EVT_RX_COUNT=3` plus, in the **application
Kconfig before `source "Kconfig.zephyr"`** (hidden symbols; the earliest default wins):
```
config BT_CTLR_RX_STACK_SIZE
	int
	default 640 if APP_TIGHT_CTLR_STACKS

config BT_CTLR_RX_PRIO_STACK_SIZE
	int
	default 384 if APP_TIGHT_CTLR_STACKS
```

`crypto-lean`:
```
CONFIG_PSA_CRYPTO=y
CONFIG_PSA_WANT_ALG_HMAC=y
CONFIG_PSA_WANT_ALG_SHA_256=y
CONFIG_PSA_WANT_ALG_CCM=y
CONFIG_PSA_WANT_KEY_TYPE_AES=y
CONFIG_PSA_WANT_AES_KEY_SIZE_128=y
CONFIG_PSA_WANT_KEY_TYPE_HMAC=y
CONFIG_PSA_WANT_GENERATE_RANDOM=n
CONFIG_MBEDTLS_THREADING_C=n
CONFIG_MBEDTLS_PSA_KEY_SLOT_COUNT=2
```
(`PSA_WANT_AES_KEY_SIZE_192/256=n` are re-selected by the Oberon core.)

Top nRF51 RAM users: `sys_work_q_stack` 1280, `z_interrupt_stacks` 1024,
`ticker_user_ops` 660, controller `recv_thread_stack` 640, `z_main_stack` 512,
app `rec_buf` 512, `mem_pdu_rx` 480, `prio_recv_thread_stack` 384, HCI RX pool
355, `bt_dev` 336, `conn_pool` 304, PSA hash op 232, app plaintext 200.

Experiment sources (scratch, not part of the repo): `tagperiph/` with all `.conf`
fragments and overlays, `cryptostack/` stack probe.
