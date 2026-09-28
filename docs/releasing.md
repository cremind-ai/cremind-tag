# Releasing

A release is one directory, `dist/<version>/`, holding the firmware of every
publishable target, the protocol contract host software pins, the licence
notices and the checksums — tied together by `release.json`. It is built by
[`tools/release.py`](../tools/release.py), normally from the tag's CI run
([`.github/workflows/release.yml`](../.github/workflows/release.yml)), which
uploads it to a **draft** GitHub release. Nothing is published automatically.

The host software is not part of it: Cremind is released on its own, with its
own version, and builds, pins and ships the font packs bridges draw with.

## Versioning

**One version, one file.** `VERSION` at the repository root holds a semantic
version `MAJOR.MINOR.PATCH`, optionally `-alpha.N`, `-beta.N` or `-rc.N`. Each
number must fit in a byte: Zephyr's VERSION file and the tag's CAPS carry them
as bytes. Every other copy is derived from it by
[`tools/version.py`](../tools/version.py):

| Copy | Form | Reaches |
|---|---|---|
| `apps/{gateway,bridge,tag}/VERSION` | Zephyr format (`VERSION_MAJOR = …`, `EXTRAVERSION = rc.1`) | `app_version.h`: `APP_VERSION_MAJOR/MINOR/PATCHLEVEL`, `APP_VERSION_STRING` |

`VERSION` also names the protocol contract of the release
(`cremind-tag-contract-<version>`). It versions the firmware only: host
software has its own version, and compatibility follows the contract's
protocol capabilities (`contract.json` `protocol`) and the font pack
identifiers, never a comparison of application versions.

```sh
python tools/version.py                 # 0.1.0
python tools/version.py --set 0.2.0     # write VERSION, then every copy
python tools/version.py --check         # CI: exit 1 if a copy differs
python tools/version.py --expect-tag v0.2.0   # release CI: the tag must be v<VERSION>
```

What the devices report:

- **Tag** — CAPS `fw_major/fw_minor/fw_patch` = `APP_VERSION_MAJOR/MINOR/PATCHLEVEL`
  (`apps/tag/src/gatt.c`).
- **Gateway** — HELLO/INFO `fw` = `APP_VERSION_STRING`, `build` =
  `APP_BUILD_VERSION` (`apps/gateway/src/main.c`), which Zephyr takes from
  `git describe --abbrev=12 --always` of the checkout.
- **Bridge** — HELLO/INFO `fw` and CAPS `fw_major/minor/patch` currently come
  from `BRIDGE_FW*` constants in `apps/bridge/src/bridge.h`, not from
  `apps/bridge/VERSION`; `build` is `git describe --always --dirty --abbrev=10`
  (`apps/bridge/CMakeLists.txt`). Until the bridge reads `app_version.h`,
  bumping VERSION does not change what a bridge reports.

`protocol/spec.yaml` `spec_version` is the protocol's own version and moves
independently.

Because the gateway's and bridge's build strings are `git describe` output, the
same commit builds identically only from clones that see the same tags: the
release workflow checks out with full history, and a rebuild to verify a
release must use a clone that has the release tag (see
[Verifying a download](#verifying-a-download)).

## Pins

| Pin | Value | Where |
|---|---|---|
| Toolchain image | `ghcr.io/nrfconnect/sdk-nrf-toolchain@sha256:45b97cad97a9967c52d77d1d1a0f7dd8fe027edd17c05c3eda2eeadc23729418` (tag `v3.4.1`, linux/amd64) | `IMAGE` in `tools/build.py`, every `container:` and `CTAG_TOOLCHAIN_IMAGE` in `ci.yml` and `release.yml` |
| nRF Connect SDK | sdk-nrf `v3.4.1` = commit `b20f8619ba9a5530f8c34b0a130d829947cfe55d` (annotated tag object `f34326924aaa`) | `west.yml`, `NCS_REVISION` / `NCS_SDK_NRF_COMMIT` in `tools/build.py` |
| Zephyr | sdk-zephyr `ncs-v3.4.1` = `33fa6a7aac6a4401d16a67cb9f27a3483fa02dd6` | follows from sdk-nrf's manifest; recorded per build |

Builds never run the mutable tag. `tests/tools/test_build_provenance.py` fails
when a workflow names another image than `tools/build.py`, or when `west.yml`
and `NCS_REVISION` disagree.

Before every build, `tools/build.py` checks the west workspace: the manifest
repository `nrf` must be checked out at `NCS_SDK_NRF_COMMIT`, and `west compare`
must report no project off its manifest revision and no local change. A
mismatch stops the build with the fix (`west update`, or recreate the
`ncs-v3.4.1` volume with `python tools/build.py --setup`).
`--skip-workspace-check` skips only `west compare` (dev iterations); such a
build is recorded as unchecked and `tools/release.py` refuses it.

**Moving a pin.** Toolchain: resolve the digest
(`docker buildx imagetools inspect ghcr.io/nrfconnect/sdk-nrf-toolchain:<tag>`),
change `IMAGE_DIGEST` and the four workflow lines together. NCS: change
`west.yml`, `NCS_REVISION`, `NCS_SDK_NRF_COMMIT` (`git ls-remote
https://github.com/nrfconnect/sdk-nrf 'refs/tags/<tag>^{}'`), the CI cache key
and the volume name, then re-run everything in
[firmware-notes.md](firmware-notes.md).

## Build metadata

Every target build writes `build/<target>/metadata.json` next to its artifacts:

| Field | Content |
|---|---|
| `target`, `app`, `board`, `soc`, `role`, `hardware`, `hardware_status`, `board_id`, `release` | the target and its board's status in `hardware/matrix.yaml` |
| `version`, `app_version`, `version_matches` | `VERSION`, the app's copy, whether they agree |
| `git` | `commit`, `describe`, `dirty` (tracked changes or untracked files), `changed_files`, `commit_time` |
| `ncs` | `revision`, `sdk_nrf_commit`, `zephyr_commit`, `compared` (`west compare` ran), `clean` |
| `toolchain` | `image` (from `CTAG_TOOLCHAIN_IMAGE`, set by build.py's `docker run` and the workflows), `digest`, `matches_pin`, `compiler` (`arm-zephyr-eabi-gcc (Zephyr SDK 1.0.1) 14.3.0`) |
| `build` | `built_at`, `pristine`, the exact `west build` `command`, `source_date_epoch` |
| `inputs` | `kconfig_sha256` (the `CONFIG_` lines of `.config`), `dts_sha256` (`zephyr.dts` without comments), `devicetree_header_sha256` (the `#define`s of `devicetree_generated.h`): comments name checkout paths, so they are left out and the digests are comparable across machines |
| `verify_stack` | `ok`, `dt_method`, `counts`, `failed`, `warnings` |
| `memory`, `resources`, `status` | as in `build/memory-report.json` |
| `artifacts` | SHA-256 and size of `zephyr.hex`, `.bin`, `.elf`, `.map`, `.config`, `zephyr.dts`, … |

## Reproducibility

**Guarantee.** For one commit and VERSION, the pinned toolchain image and the
pinned workspace, `zephyr.hex`, `zephyr.bin` and `zephyr.elf` are byte-identical
whatever the checkout path, build directory, machine or time of the build; the
contract archive is byte-identical for the same commit; the release archive is
byte-identical for the same `dist/<version>/` content.
`metadata.json`, `zephyr.map` and `build.log` are not covered: they record
paths and times on purpose.

How builds get there (`repro_cmake_args` in `tools/build.py`):

- `-ffile-prefix-map` for the workspace (`/ncs`), this repository
  (`/cremind-tag`) and the build directory (`/build/<target>`), at compile time
  and at the LTO link, so `__FILE__` and the DWARF of `zephyr.elf` never carry
  a real path. Zephyr's own `BUILD_OUTPUT_STRIP_PATHS` maps only the app,
  `ZEPHYR_BASE` and the west top directory — not `lib/` (a Zephyr module from
  this repository) and not the build directory.
- `BUILD_VERSION` = the sdk-zephyr commit (12 digits) instead of `git describe`
  in the workspace, which depends on whether the clone has tags.
- `SOURCE_DATE_EPOCH` = the commit time (GCC's `__DATE__`/`__TIME__`).
- `safe.directory=*` in the build's git environment, so Zephyr's and NCS's own
  `git describe`/`rev-parse` give the same answer whoever owns the checkout.

Measured before the flags: a tag image was already identical across checkout
paths, but its ELF differed (DWARF `DW_AT_comp_dir` and source paths); with
them the ELF is identical too. `zephyr.dts`, `.config` and
`devicetree_generated.h` differ in path comments only, hence the digests of
their content.

**How it is checked.** [`tools/repro_check.py`](../tools/repro_check.py)
snapshots the checkout once (so concurrent edits cannot fake a difference),
copies the snapshot to a second path at another depth, and builds each copy
pristine into its own build root. The two builds' `zephyr.hex` and `zephyr.bin`
must match byte for byte; `zephyr.elf` and the input digests are compared and
reported. When images differ, the report lists the differing address ranges,
the ELF section and symbol at each, path strings present in only one image,
date/time strings, `git describe` strings and GNU build ids, with the fix to
apply.

```sh
python tools/repro_check.py tag-laowu-bw bridge-nrf52840dk      # host: runs in the toolchain container
python tools/repro_check.py --release-targets
```

The report is `build/repro/report.json`; differing builds are kept under
`build/repro/<target>/{a,b}/`. The release workflow runs the check on every
release target and will not draft a release if it fails.

## What goes into a release

A target is published when its board is **`qualified`** in
[`hardware/matrix.yaml`](../hardware/matrix.yaml), or when it is explicitly
marked `release: true` in [`tools/targets.yaml`](../tools/targets.yaml) and its
board is `buildable` or `functional`. Development targets without a hardware
entry (`tag-nrf52dk`) need the mark too. `blocked` and `documented` boards are
never published, whatever the mark.

| Status | Meaning for a release |
|---|---|
| `qualified` | qualification report complete (power, endurance, faults): published |
| `functional` | flashed and exercised on a sample: published only with `release: true` |
| `buildable` | links within the exact memory geometry and passes `verify_stack`; **never run on hardware**: published only with `release: true`, for bring-up |
| `development` | no hardware entry (a DK standing in for a board): published only with `release: true` |
| `blocked`, `documented` | never published |

`release.json` records each published target's `hardware_status`,
`release_status` and `qualified`, and every left-out target with the reason.
`cremind tags tools firmware verify` warns about every image that is not qualified,
and the draft's notes say which images are only buildable.

`python tools/release.py --list-targets` prints the current selection. On top of
eligibility, each target's `metadata.json` must show: status `ok`,
`verify_stack` passed, resources met, a pristine build, the current commit,
`VERSION` in the app, a compared workspace at the pinned sdk-nrf commit and the
pinned toolchain — and its files must still hash as recorded.

## Release steps

1. **Decide the content.** Update board statuses in `hardware/matrix.yaml`
   (`python tools/gen_hardware_docs.py`) and the `release:` marks in
   `tools/targets.yaml`.
2. **Set the version** and commit it:

   ```sh
   python tools/version.py --set 0.2.0
   git commit -am "Release 0.2.0"
   ```
3. **Trial run (optional, local).** From a clean tree:

   ```sh
   python tools/build.py --pristine $(python tools/release.py --list-targets)
   python tools/repro_check.py --release-targets
   uv run python tools/release.py
   ```

   `--allow-dirty` packages a dirty tree for a look (never for a release);
   `--targets a,b` picks targets; `--out DIR` writes `DIR/<version>/`;
   `--build` runs `build.py --pristine` first.
4. **Tag and push:** `git tag -a v0.2.0 -m "Cremind Tag 0.2.0" && git push origin v0.2.0`.
5. **CI** ([`release.yml`](../.github/workflows/release.yml)):
   `gates` (the whole CI workflow: tool tests, the generated C bindings, the
   contract, host C tests, the firmware matrix, twister) → `version` (tag =
   `v<VERSION>`, every copy in sync, the target list) → `firmware` (pristine
   builds in the pinned container, full git history) and `reproducibility`
   (two checkouts, two build roots) → `package` (`tools/release.py`,
   `sha256sum -c`, workflow artifact `release-dist`, then the draft release).
   The NCS workspace comes from the CI cache (`ncs-v3.4.1-narrow-depth1-v1`).
6. **Review the draft** on GitHub: notes, statuses, assets. Publish it by hand.
   Re-running the workflow updates a draft (`--clobber`) but fails on a
   published release: a published version is never changed — fix forward with
   a new patch version.
7. **Pin the contract in Cremind** when the protocol, the fixtures or the
   hardware tables changed (a change on Cremind's side, released with
   Cremind): download `cremind-tag-contract-<v>.tar.gz` and its `.sha256` from
   the release, then in a Cremind checkout `python scripts/tags/pin_contract.py
   cremind-tag-contract-<v>.tar.gz` and run its protocol tests
   (`tests/tags/runtime/protocol`).

`workflow_dispatch` runs the same pipeline on any ref and keeps the result as
the `release-dist` workflow artifact; with `draft_release` it also creates or
updates the draft `v<VERSION>` targeting that commit (the tag is created when
the draft is published).

## Artifact layout

```text
dist/
  cremind-tag-<v>.tar.gz          the directory below (sorted, owner 0, mtime = commit time, gzip without timestamp)
  cremind-tag-<v>.tar.gz.sha256
  RELEASE_NOTES-<v>.md            the draft's body
  <v>/
    release.json                  manifest (below)
    SHA256SUMS                    every other file of <v>/ (sha256sum -c)
    THIRD_PARTY_NOTICES.txt       Nayuki QR (MIT), firmware SDK components per target
    LICENSE                       MIT
    firmware/
      memory-report.md, memory-report.json
      <target>/<target>-<v>.hex .bin .elf .map .config .dts
               <target>-<v>.metadata.json .verify.json .memory.json
    contract/
      cremind-tag-contract-<v>.tar.gz          the protocol contract (tools/contract.py)
      cremind-tag-contract-<v>.tar.gz.sha256
```

`release.json` (`schema: cremind-tag/release@2`): `version`, `tag`, `git`
(`commit`, `describe`, `dirty`, `commit_time`), `source_date_epoch`, `ncs`
(`revision`, `sdk_nrf_commit`), `toolchain` (`image`, `digest`), `firmware[]`
(`target`, `app`, `role`, `board`, `soc`, `jlink_device`, `family`, `hardware`,
`board_id`, `hardware_status`, `release_status`, `qualified`, `version`,
`files`, `sha256`, `verify_stack`, `memory`, `resources`, `inputs`, `flash` —
the command that flashes it), `excluded_targets[]`, `contract` (`name`,
`version`, `digest`, `protocol` capabilities, `archive`, `sha256`).

Uploaded to the draft individually: the archive and its `.sha256`,
`release.json`, `SHA256SUMS`, `THIRD_PARTY_NOTICES.txt`, the contract archive
and its `.sha256`, and each target's `.hex` and `.metadata.json`. Everything
else (ELF, map, memory reports) is in the archive.

## The protocol contract

`tools/contract.py` builds `cremind-tag-contract-<version>` from a commit:
`contract.json` (schema `cremind-tag/contract@1`: the version, the protocol
capabilities — `spec_version`, `proto_version`, `secure_proto_version`,
`fontpack_version` — the source repository and revision, the SHA-256 of every
file and one `digest` over them), `spec.yaml`, `fixtures/`,
`tests/conversation.h`, `hardware/matrix.yaml` and `hardware/targets.yaml`,
and the normative `docs/`. The archive is deterministic; a build from
uncommitted inputs is refused. CI builds and checks it on every change.

```sh
uv run python tools/contract.py                          # dist/contract/
uv run python tools/contract.py --check dist/contract/cremind-tag-contract-0.1.0
```

Host software never reads a checkout of this repository: it pins a released
contract and tests its own bindings, reference implementation and hardware
tables against it (Cremind: `scripts/tags/pin_contract.py`).

## First release: J-Link and flashing

Every board is programmed over SWD with a SEGGER J-Link: the on-board J-Link of
the nRF52840 DK and nRF52 DK, an external J-Link (or a DK's debug-out header)
for tags. Install once, on the host (Docker Desktop cannot pass USB through on
Windows):

1. **SEGGER J-Link Software and Documentation Pack** — always needed (driver,
   udev rules on Linux; `JLink.exe`/`JLinkExe`). Details per OS in
   [enrollment.md → Tools](enrollment.md#tools).
2. **nRF Util** with its device command (`nrfutil install device`) —
   preferred; check with `nrfutil device list`.
3. Optionally the legacy **nRF Command Line Tools** (`nrfjprog`).

`--tool auto` (the default, config `hardware.jlink_tool`) takes nrfutil, then
nrfjprog, then J-Link Commander; `--snr` picks a probe when several are
attached. Always look at `--dry-run` first: it verifies the image and prints
the exact commands (and writes J-Link command files) without touching the
board.

```sh
cremind tags tools firmware list dist/0.1.0
cremind tags tools firmware info --soc nrf52840_qiaa                  # does the probe see the chip?
cremind tags tools firmware verify --hex dist/0.1.0/firmware/gateway-nrf52840dk/gateway-nrf52840dk-0.1.0.hex
```

- **Gateway** (nRF52840 DK): plug the DK's J-Link USB port, then

  ```sh
  cremind tags tools firmware flash --target gateway-nrf52840dk \
      --hex dist/0.1.0/firmware/gateway-nrf52840dk/gateway-nrf52840dk-0.1.0.hex --dry-run
  cremind tags tools firmware flash --target gateway-nrf52840dk --hex …/gateway-nrf52840dk-0.1.0.hex
  ```

  Only the pages the image covers are erased (`--erase touched`, the default),
  so the mesh network and the gateway's keys survive an update; `--erase all`
  wipes them and asks first. Then connect the nRF USB port (CDC ACM) and run
  `cremind tags tools gateway info`.
- **Bridge** (nRF52840 DK): the same with `--target bridge-nrf52840dk`. The
  bridge also needs a font pack in its external flash: Cremind installs it
  over the maintenance port (`cremind tags tools bridge fonts-install`), or for
  the DK's 8 MiB part a development image Cremind builds
  (`cremind tags tools fonts image`) is flashed with `nrfjprog -f NRF52
  --program <image>/flash.hex --qspisectorerase --verify`
  ([fonts](https://github.com/cremind-ai/cremind/blob/main/docs/tags/fonts.md#flash-sizing)).
- **Tags**: never `firmware flash` — `cremind tags tools tag enroll --board laowu_bw
  --firmware dist/0.1.0/firmware/tag-laowu-bw/tag-laowu-bw-0.1.0.hex` erases
  the chip, programs the image and writes the tag's identity to UICR
  ([enrollment.md](enrollment.md)). `firmware flash` refuses tag images and
  prints this command.

`cremind tags tools firmware info --hex <image>` also compares the device's flash with
an image without programming it (`nrfutil device fw-verify`, `nrfjprog
--verify`, J-Link `verifybin`). Like enrollment, these command lines follow the
vendors' documentation and were exercised against mocked tools only: confirm
them on the first sample ([enrollment.md → First-sample
checklist](enrollment.md#first-sample-checklist); for `firmware`, also
`fw-verify`, `--verify` and `verifybin`).

## Verifying a download

1. Check the archive, then every file in it:

   ```sh
   sha256sum -c cremind-tag-0.1.0.tar.gz.sha256
   tar xzf cremind-tag-0.1.0.tar.gz && cd cremind-tag-0.1.0
   sha256sum -c SHA256SUMS
   ```

   On Windows: `Get-FileHash -Algorithm SHA256 <file>` and compare with the
   line in `SHA256SUMS`.
2. Check what you are about to flash: `cremind tags tools firmware verify --hex
   firmware/<target>/<target>-0.1.0.hex` (checksum against `release.json` and
   `SHA256SUMS`, target, board, SoC range, `verify_stack`, qualification).
   `firmware flash` runs the same checks and flashes nothing if one fails.
3. Check the provenance in `release.json` against this page's
   [pins](#pins): `toolchain.digest`, `ncs.sdk_nrf_commit`, `git.commit` (the
   tag's commit), `git.dirty: false`.
4. Rebuild and compare (the strongest check):

   ```sh
   git clone https://github.com/cremind-ai/cremind-tag && cd cremind-tag && git checkout v0.1.0
   python tools/build.py --setup                 # once: the pinned NCS workspace
   python tools/build.py --pristine gateway-nrf52840dk
   sha256sum build/gateway-nrf52840dk/zephyr.hex # = sha256 of gateway-nrf52840dk-0.1.0.hex in release.json
   uv run python tools/contract.py               # sha256 = release.json contract.sha256
   ```

   Clone with tags (a plain `git clone` has them): the gateway's and bridge's
   build strings come from `git describe`.

The checksums prove integrity against the release's own `SHA256SUMS`, which
lives in the same GitHub release. Releases are not signed yet; until they are,
the rebuild in step 4 is what ties an image to the source.
