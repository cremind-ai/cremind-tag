# Building the firmware

All firmware builds with the pinned nRF Connect SDK **v3.4.1** toolchain image
(`ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1`, run by digest:
`ghcr.io/nrfconnect/sdk-nrf-toolchain@sha256:45b97cad97a9967c52d77d1d1a0f7dd8fe027edd17c05c3eda2eeadc23729418`;
sdk-nrf `v3.4.1` = `b20f8619ba9a`, sdk-zephyr `ncs-v3.4.1`) through
[`tools/build.py`](../tools/build.py). The same script runs in CI, so a local
build and a CI build are the same command line — and give byte-identical
images ([releasing.md → Reproducibility](releasing.md#reproducibility)).

## Prerequisites

| | Windows | Linux |
|---|---|---|
| Docker | Docker Desktop (WSL 2 backend) | Docker Engine |
| Shell | Git Bash | any POSIX shell |
| Python | 3.11+ with PyYAML — the tools environment has both (`uv sync`, [`pyproject.toml`](../pyproject.toml)) | same |

`build.py` needs only the standard library and PyYAML on the host; everything
else runs in the container.

## One-time setup: the NCS workspace volume

The west workspace lives in a Docker volume so it is shared by every build and
never touches the repository:

```bash
python tools/build.py --setup     # west init + west update --narrow --depth=1 into volume ncs-v3.4.1
```

Two volumes are used: `ncs-v3.4.1` (mounted at `/ncs`, the workspace) and
`ctag-build` (mounted at `/build`, the build directories). The repository is
bind-mounted at `/work`. Building on the bind mount is slow, which is why build
directories stay in the volume and only the artifacts are copied back.

## Building

```bash
python tools/build.py --list                       # targets, boards, snippets, RAM targets, app present?
python tools/build.py tag-laowu-bw bridge-nrf52840dk
python tools/build.py --all                        # the whole matrix
python tools/build.py --pristine tag-sifei-52810   # west build -p always (default: -p auto)
python tools/build.py -v gateway-nrf52840dk        # stream the full build output
python tools/build.py --shell                      # interactive shell in the container (cd /ncs)
```

Use the tools environment if `python` lacks PyYAML (`uv run python
tools/build.py …`, or its interpreter: `.venv/Scripts/python.exe` on Windows,
`.venv/bin/python` on Linux).

| Option | Effect |
|---|---|
| `--list` | print the matrix and flag any rule violation |
| `--all` / names | targets to build |
| `--pristine` | rebuild from scratch |
| `--app DIR` | build another app directory (repository-relative) with each target's board, snippets and checks — e.g. a board bring-up app |
| `--allow-resource-miss` | report resource-target misses without failing |
| `--in-container` | already inside the NCS image (CI); `--ncs-dir`/`$NCS_DIR` and `--build-root`/`$CTAG_BUILD_ROOT` locate the workspace and build directories |
| `--out-root DIR` | write artifacts and reports under `DIR` (repository-relative) instead of `build/` (used by `tools/repro_check.py`) |
| `--skip-workspace-check` | skip `west compare` (the sdk-nrf commit is still checked); recorded in metadata.json, refused by `tools/release.py` |
| `--setup` | create or update the workspace volume |
| `--shell` | open a shell in the toolchain container |

Exit status: `0` everything built, verified and within its resource targets;
`1` a build, stack verification or resource target failed; `2` usage error;
`3` nothing failed but a target was skipped because its app does not exist yet.

### What one target build does

0. Once per run: checks the workspace — `nrf` at the pinned sdk-nrf commit and
   a clean `west compare` — and stops with the fix if not
   ([releasing.md → Pins](releasing.md#pins)).
1. `west build --no-sysbuild -p auto -d /build/<target> -b <board> [-S bt-ll-sw-split] <app> -- -DZEPHYR_EXTRA_MODULES=/work -UCONFIG_* [-DEXTRA_CONF_FILE=…] [-DEXTRA_DTC_OVERLAY_FILE=…] -DEXTRA_CPPFLAGS=<prefix maps> -DEXTRA_LDFLAGS=<prefix maps> -DBUILD_VERSION=<sdk-zephyr commit>`
   with `SOURCE_DATE_EPOCH` = the commit time (full log in
   `build/<target>/build.log`). `-UCONFIG_*` works around an NCS v3.4.1 trap,
   see [Troubleshooting](#troubleshooting); the rest makes the image
   independent of paths and clones
   ([releasing.md → Reproducibility](releasing.md#reproducibility)).
2. Copies `zephyr.hex`, `zephyr.bin`, `zephyr.elf`, `zephyr.map`, `.config`,
   `zephyr.dts`, `devicetree_generated.h`, `edt.pickle` to `build/<target>/`
   (gitignored).
3. Runs [`tools/verify_stack.py`](../tools/verify_stack.py) on them
   (`build/<target>/verify.json`).
4. Measures flash and RAM from `zephyr.elf` against the linker regions and the
   target's resource limits, then merges the result into
   `build/memory-report.json` and `build/memory-report.md`.
5. Writes `build/<target>/metadata.json`: git commit and dirty flag, VERSION
   (and whether the app's copy matches), NCS commits, toolchain image and
   compiler, the command, Kconfig/devicetree digests, the verification result,
   memory and every artifact's SHA-256
   ([releasing.md → Build metadata](releasing.md#build-metadata)).

### Running Docker by hand

On Git Bash, `MSYS_NO_PATHCONV=1` is **required**, otherwise the container paths
are rewritten into Windows paths (`build.py` sets it for you):

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  -v ncs-v3.4.1:/ncs -v ctag-build:/build \
  -v "$(pwd):/work" \
  ghcr.io/nrfconnect/sdk-nrf-toolchain@sha256:45b97cad97a9967c52d77d1d1a0f7dd8fe027edd17c05c3eda2eeadc23729418 \
  -c 'cd /ncs && west build --no-sysbuild -d /build/tag-laowu-bw -b laowu_bw/nrf51822 /work/apps/tag -- -DZEPHYR_EXTRA_MODULES=/work'
```

(A hand-made build like this one lacks the reproducibility flags and
metadata.json; use `tools/build.py` for anything you publish.)

(On Windows use the drive path form, e.g. `-v "C:/path/to/cremind-tag:/work"`.)
The image's entrypoint is `bash -c`; its environment is set up by
`BASH_ENV=/opt/non-interactive-setup.sh`.

## The target matrix

[`tools/targets.yaml`](../tools/targets.yaml) is the single list of firmware
targets:

| Target | App | Board | Snippet |
|---|---|---|---|
| gateway-nrf52840dk / gateway-nrf52dk | `apps/gateway` | `nrf52840dk/nrf52840` / `nrf52dk/nrf52832` | `bt-ll-sw-split` |
| gateway-nrf52840dongle | `apps/gateway` | `nrf52840dongle/nrf52840` (keeps the factory USB bootloader) | `bt-ll-sw-split` |
| bridge-nrf52840dk / bridge-nrf52dk | `apps/bridge` | same two DKs | `bt-ll-sw-split` |
| tag-laowu-bw / tag-laowu-bwr | `apps/tag` | `laowu_bw/nrf51822` / `laowu_bwr/nrf51822` | **none** |
| tag-sifei-52810 / tag-hema-52811 | `apps/tag` | `sifei_52810/nrf52810` / `hema_52811/nrf52811` | `bt-ll-sw-split` |
| tag-nrf52dk | `apps/tag` | `nrf52dk/nrf52832` (development tag) | `bt-ll-sw-split` |

Rules enforced by `build.py` (and by `tests/tools`):

- The `bt-ll-sw-split` snippet is applied to **every nRF52 target and never to
  nRF51**: its overlay references `&bt_hci_sdc`, which nRF51 does not have, and
  nRF51 already defaults to the Zephyr controller
  ([firmware-notes](firmware-notes.md) correction 1). The custom nRF52 tag
  boards additionally select the Zephyr controller in their devicetree, so a
  build without the snippet still never gets the SoftDevice Controller.
- Every build is `--no-sysbuild`; boards and bindings come from this repository
  as a Zephyr module (`zephyr/module.yml`: `board_root: .`, `dts_root: .`).
- Warnings are not errors: every image warns *Experimental symbol
  BT_LL_SW_SPLIT is enabled*, and NCS warns that nRF51 is community-maintained.

### Configuration fragments

Zephyr merges these automatically, so most targets need no `extra_conf`:

| File in the app directory | Applies to |
|---|---|
| `prj.conf` | every target of the app |
| `socs/<soc>.conf`, `socs/<soc>.overlay` | every board with that SoC: `nrf51822`, `nrf52810`, `nrf52811`, `nrf52832`, `nrf52840` |
| `boards/<board>.conf`, `boards/<board>.overlay` | one board target, normalised: `laowu_bw_nrf51822`, `nrf52dk_nrf52832`, `nrf52840dk_nrf52840`, … |

Both Laowu boards share `socs/nrf51822.conf` (same 16 KiB SRAM). Use
`extra_conf` / `extra_overlay` in `targets.yaml` (paths relative to the app) only
for fragments that are neither per-SoC nor per-board; `build.py` passes them as
`EXTRA_CONF_FILE` / `EXTRA_DTC_OVERLAY_FILE`, which keeps the automatic merging
above intact (never pass `CONF_FILE` or `DTC_OVERLAY_FILE`: they disable it).

## Tag boards

HWMv2 boards live in [`boards/cremind/`](../boards/cremind/) (vendor `cremind`):

| Board | SoC Kconfig | Flash layout | Notes |
|---|---|---|---|
| `laowu_bw` | `SOC_NRF51822_QFAB` | code 124 KiB, NVS 4 × 1 KiB at `0x1f000` | pins from the matrix; LEDs unconfirmed (none); debug TX P0.06 as disabled `uart0` |
| `laowu_bwr` | `SOC_NRF51822_QFAA` (nRF51802 modelled as QFAA) | code 252 KiB, NVS 4 × 1 KiB at `0x3f000` | LEDs P0.03/04/05; debug TX P0.08 |
| `sifei_52810` | `SOC_NRF52810_QFAA` | code 184 KiB, NVS 2 × 4 KiB at `0x2e000` | **placeholder panel pins**, panel id 255 |
| `hema_52811` | `SOC_NRF52811_QFAA` | code 184 KiB, NVS 2 × 4 KiB at `0x2e000` | SSD1619 2.13" 128 × 250 BWR, panel id 3; pins documented by EPD-nRF5; panel supply EN P0.07; LED P0.18 |

Common to all four:

- `USE_DT_CODE_PARTITION=y` links into `code_partition`, so the linker refuses
  an image that would grow into `storage_partition` (use
  `PARTITION_DEVICE/OFFSET/SIZE(storage_partition)` for NVS).
- 32 kHz: calibrated RC (`K32SRC_RC`, calibration on, `MAX_SKIP=0`) until a
  crystal is verified; no UART console; GPIO and SPI enabled; DC/DC left off.
- `chosen { cremind,panel = &epd; cremind,tag-board = &tag_board; }` — a
  panel node on `spi0` (`cremind,uc8176`; `cremind,ssd1619` on the Hema) and a `cremind,tag-board` node with
  the protocol `board-id` (bindings in [`dts/bindings/`](../dts/bindings/)).
- Panel contract: **firmware must refuse to drive a panel with `panel-id` 255
  (UNVERIFIED)** — no bus init, no pin configuration, every frame refused. The
  Sifei SPI bus is `zephyr,deferred-init`, so the SPI driver is linked
  (memory-fit builds stay honest) but never touches the placeholder pins unless
  firmware calls `device_init()`, which it must not do for panel id 255. The
  Hema bus is deferred for another reason: its SSD1619 driver calls
  `device_init()` only after switching the panel supply (EN) on.
- Runners: `jlink` (default) and `nrfjprog`.

To bring up a Laowu board with serial logs, enable the debug pad in an overlay
and fragment:

```dts
&uart0 { status = "okay"; };
```
```
CONFIG_SERIAL=y
CONFIG_CONSOLE=y
CONFIG_UART_CONSOLE=y
```

## Controller verification (`tools/verify_stack.py`)

Every build must prove it uses only the Zephyr controller. `build.py` runs this
automatically; by hand:

```bash
python tools/verify_stack.py build/tag-laowu-bw --target tag-laowu-bw [--json out.json]
python tools/verify_stack.py /path/to/zephyr/build/dir --soc nrf52832_qfaa --role bridge --zephyr-base /ncs/zephyr
```

| Check | Passes when |
|---|---|
| `kconfig.ll_sw_split` | `CONFIG_BT_LL_SW_SPLIT=y` |
| `kconfig.no_softdevice` | no `CONFIG_BT_LL_SOFTDEVICE*=y` |
| `kconfig.no_mpsl` | no exact `CONFIG_MPSL=y` (integer `CONFIG_MPSL_*` are expected) |
| `kconfig.flash_sync` | `CONFIG_SOC_FLASH_NRF_RADIO_SYNC_TICKER=y`, never `…_SYNC_MPSL=y` |
| `kconfig.ctlr_crypto` | `CONFIG_BT_CTLR_CRYPTO=y` (firmware-notes correction 8) |
| `kconfig.mesh_adv` | bridge/gateway: no `CONFIG_BT_MESH_ADV_LEGACY=y` (correction 4) |
| `kconfig.entropy` | warning only: `CONFIG_ENTROPY_CC3XX=y` (correction 9) |
| `dt.chosen_bt_hci` | chosen `zephyr,bt-hci` compatibles are exactly `['zephyr,bt-hci-ll-sw-split']` |
| `dt.no_sdc_okay` | no okay `nordic,bt-hci-sdc` node |
| `dt.generated_header` | `DT_COMPAT_HAS_OKAY_zephyr_bt_hci_ll_sw_split 1`, no `…_nordic_bt_hci_sdc 1` |
| `map.no_sdc_libs` | no `libsoftdevice_controller*.a` / `libmpsl*.a` in `zephyr.map` |
| `map.no_sdc_symbols` | no linked `sdc_*` / `mpsl_*` symbols |
| `map.ll_symbols` | `lll_init`, `ticker_init`, `radio_isr_set`, `ll_adv_enable` linked |
| `geometry.dt` | devicetree flash and SRAM sizes equal the SoC's exactly |
| `geometry.map` | linker RAM region equals SoC RAM; FLASH region ends inside SoC flash |
| `geometry.storage` | FLASH region ends before `storage_partition` (warning if only the current image fits) |

The devicetree is read through the build's own `edt.pickle` when Zephyr's
`devicetree.edtlib` is importable (inside the container, or with
`--zephyr-base`/`$ZEPHYR_BASE`); otherwise the checks parse
`devicetree_generated.h`. The report names the method that ran. Exit status: `0`
pass (warnings allowed), `1` a check failed, `2` usage error.

## Memory report

`build/memory-report.md` (and `.json`) lists, per target, flash used against the
FLASH region (the code partition on tag boards), the free share against the
15 % headroom target, RAM used after every static allocation including stacks,
and free RAM against the target (nRF51 2 KiB, nRF52810/811 3 KiB, nRF52832
bridge 8 KiB). The numbers are computed from `zephyr.elf` and equal the
linker's `--print-memory-usage` figures. `python tools/gen_hardware_docs.py`
copies them into [the hardware matrix](hardware/matrix.md) and the
qualification reports.

## Flashing with J-Link

Docker Desktop on Windows cannot pass USB devices to containers, so flash from
the host with the artifacts in `build/<target>/`. Connect SWDIO, SWCLK, GND and
VTref (and power the tag from its battery or a bench supply); the J-Link pins
and what to check when the probe cannot reach the chip are in
[enrollment.md → Wiring](enrollment.md#wiring).

Gateways and bridges: `cremind tags tools firmware flash --target <target> --hex
build/<target>/zephyr.hex [--dry-run]` checks the image against its
`metadata.json` first and erases only the pages it covers; tags:
`cremind tags tools tag enroll --firmware`
([releasing.md → First release](releasing.md#first-release-j-link-and-flashing)).
By hand:

J-Link Commander (any OS):

```bash
JLinkExe -device nRF51822_xxAB -if SWD -speed 4000 -autoconnect 1
J-Link> loadfile build/tag-laowu-bw/zephyr.hex
J-Link> r
J-Link> g
J-Link> exit
```

Device names: `nRF51822_xxAB` (laowu_bw), `nRF51822_xxAA` (laowu_bwr),
`nRF52810_xxAA`, `nRF52811_xxAA`, `nRF52832_xxAA`, `nRF52840_xxAA`.

nrfjprog (host install):

```bash
nrfjprog -f NRF51 --program build/tag-laowu-bw/zephyr.hex --sectorerase --verify
nrfjprog -f NRF51 --reset
```

**Never chip-erase an enrolled tag** (`--chiperase`, `erase`): it wipes UICR,
including the enrollment blob in `UICR.CUSTOMER[0..11]`. `--sectorerase` and
`loadfile` only erase the sectors the image covers.

`west flash` works where a west workspace sees the build directory:

- in a T2 workspace from [`west.yml`](../west.yml) on the host:
  `west flash -d <build dir> --runner jlink` (or `--runner nrfjprog`);
- on a Linux host, in the container with the probe passed through:

  ```bash
  docker run --rm --privileged -v /dev/bus/usb:/dev/bus/usb -e ACCEPT_JLINK_LICENSE=1 \
    -v ncs-v3.4.1:/ncs -v ctag-build:/build -v "$(pwd):/work" \
    ghcr.io/nrfconnect/sdk-nrf-toolchain@sha256:45b97cad97a9967c52d77d1d1a0f7dd8fe027edd17c05c3eda2eeadc23729418 \
    -c 'sh /jlink/install.sh && cd /ncs && west flash -d /build/tag-laowu-bw --runner jlink'
  ```

First contact with a stock tag: if the vendor firmware set readback protection
(nRF51 `RBPCONF`) or APPROTECT (nRF52), `nrfjprog --recover` (or J-Link's
unlock prompt) erases the whole chip — the stock firmware cannot be recovered
afterwards. Zephyr's default `CONFIG_NRF_APPROTECT_USE_UICR` copies
`UICR.APPROTECT` into the firmware branch of APPROTECT at boot. On older nRF52
silicon an erased `UICR.APPROTECT` leaves the debug port open; on revisions
with the hardened APPROTECT (on the nRF52811 a build code starting with `B`:
second line of the package marking, e.g. `QFAAB0`) only `0x5A` (HwDisabled)
does, so after a full chip erase such a tag locks at its next reset
([enrollment.md → Recovery](enrollment.md#recovery-and-re-enrollment)). After
flashing, power-cycle the tag before measuring current: the debug interface
stays powered until a power-on reset.

## Flashing the nRF52840 Dongle (USB bootloader)

The nRF52840 Dongle (PCA10059) has no debug probe. It ships with Nordic's nRF5
SDK USB bootloader, and `gateway-nrf52840dongle` keeps it: the image links at
`0x1000` behind the MBR and its settings end below the bootloader at `0xe0000`.
No probe and no soldering:

1. Install [nRF Util](https://www.nordicsemi.com/Products/Development-tools/nRF-Util)
   and its bootloader commands: `nrfutil install nrf5sdk-tools`.
2. Package the image for the bootloader (once per build):

   ```bash
   nrfutil nrf5sdk-tools pkg generate --hw-version 52 --sd-req 0x00 \
     --application build/gateway-nrf52840dongle/zephyr.hex --application-version 1 \
     build/gateway-nrf52840dongle/dfu.zip
   ```

3. Plug the dongle in and press its RESET button (on the side, at the far end
   from the USB plug; push it towards the plug). The red LED fades in and out
   and the bootloader's own serial port appears (`COMx` on Windows,
   `/dev/ttyACM*` on Linux).
4. `nrfutil nrf5sdk-tools dfu usb-serial -pkg build/gateway-nrf52840dongle/dfu.zip -p COMx`
   (the bootloader's port).

The dongle then restarts into the gateway: the bootloader's port goes away and
the gateway's appears ("Cremind Tag gateway", `1209:0002`; `cremind tags tools gateway
ports` lists it). No LED is lit while the gateway runs. nRF Connect for
Desktop's Programmer app does the same from the `.hex`, without the packaging
step. To update, press RESET and repeat step 4: the bootloader stages the new
image right behind the running one, so the mesh network in the settings
survives while both images together fit in the code partition (860 KiB; the
gateway is about 230 KB). Programming the image over SWD instead is not
enough: the bootloader starts only an application it installed itself.

## native_sim tests (twister)

```bash
python tools/build.py --shell
# inside the container:
apt-get update && apt-get install -y make      # missing from the image
west twister -T /work/tests/ztest -p native_sim -p native_sim/native/64 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-out
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Re-running `west build` on an existing directory fails with *malformed string literal in assignment to MBEDTLS_CONFIG_FILE* … *Aborting due to Kconfig warnings* | Without sysbuild, Zephyr's `kconfig.cmake` treats every cached `CONFIG_*` CMake entry as a command-line Kconfig assignment, and nrf_security caches `CONFIG_MBEDTLS_CONFIG_FILE` / `CONFIG_TF_PSA_CRYPTO_*CONFIG_FILE` (`nrf/subsys/nrf_security/configs/config_extra.cmake.in:22-24`). Any re-configure (new CMake arguments, or ninja re-running CMake after a `.conf` edit) then aborts. `build.py` passes `-UCONFIG_*`; by hand, add `-- -UCONFIG_*` or rebuild with `-p always`. |
| Container paths such as `/ncs` turn into `C:/Program Files/Git/ncs` | Git Bash path conversion: set `MSYS_NO_PATHCONV=1` |
| `error: volume ncs-v3.4.1 holds no NCS workspace` | run `python tools/build.py --setup` |
| *Experimental symbol BT_LL_SW_SPLIT is enabled*, *SoC nrf51822 is maintained by the Zephyr community* | expected on every build; not an error |
| `SKIP <target>: application … does not exist yet` (exit 3) | the target's app directory has no `CMakeLists.txt` yet |

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml): Python (the tools
environment: `tests/tools`, `version.py --check`, `codegen.py --check`, the
contract built and verified), host C tests (`tests/host`), the
firmware matrix in the toolchain container with a cached west workspace
(`python3 tools/build.py --in-container --all`; artifacts and the memory report
are uploaded, the report is added to the job summary), and twister on
`native_sim` for `tests/ztest`. Targets whose app does not exist yet are skipped
with a warning. [`.github/workflows/release.yml`](../.github/workflows/release.yml)
runs all of it for a `vX.Y.Z` tag, then builds, checks and packages the release
([releasing.md](releasing.md)).
