# Enrolling tags

Enrollment gives a tag its identity: a random tag id and a 32-byte secret,
packed into the 48-byte enrollment blob ([protocol §9](protocol.md#9-enrollment-blob))
and written to `UICR.CUSTOMER[0..11]` (0x10001080) over SWD. The companion keeps
the secret in the OS credential store and a row in its inventory. Firmware
refuses to advertise until a valid blob is present.

Code: `cremind_tag.enroll` (`tools.py` drives the programmers, `enroll.py`
implements `enroll_tag`, which `cremind-tag tag enroll` calls).

> The tool command lines were implemented from the vendors' documentation and
> tested against a simulated target only. Verify them on the first physical
> sample of each board (see [First-sample checklist](#first-sample-checklist))
> before enrolling in bulk.

## Tools

All three drive a SEGGER J-Link probe (or the on-board J-Link of a Nordic DK)
through SEGGER's J-Link library, so the **SEGGER J-Link Software and
Documentation Pack** is always needed.

| Tool | Status | Detected when |
|---|---|---|
| `nrfutil device` (nRF Util) | preferred | `nrfutil` is on `PATH` **and** `nrfutil device --version` succeeds |
| `nrfjprog` (nRF Command Line Tools) | legacy, still works | `nrfjprog` on `PATH` (Windows: also the default install dir) |
| J-Link Commander | always available with the J-Link pack | `JLink.exe` (Windows) / `JLinkExe` (Linux, macOS) on `PATH`, else the newest `C:\Program Files\SEGGER\JLink*\JLink.exe`, `/opt/SEGGER/JLink*/JLinkExe` |

`--tool auto` (the default, config `hardware.jlink_tool`) takes the first one in
that order; naming a tool forces it and fails if it is missing. With several
probes attached, pass the probe's serial number (`--serial-number`, config
`hardware.jlink_serial`); it becomes `--serial-number` (nrfutil), `--snr`
(nrfjprog) or `-SelectEmuBySN` (J-Link Commander).

### Windows

1. Install the J-Link Software Pack (installs the USB driver; default location
   `C:\Program Files\SEGGER\JLink_V<version>`). The companion finds `JLink.exe`
   there even when it is not on `PATH`.
2. nRF Util: download `nrfutil.exe` from Nordic's nRF Util page, put it on
   `PATH`, then `nrfutil install device` and check with `nrfutil device list`
   (the probe must show up).
3. Optional, legacy: the nRF Command Line Tools installer (`nrfjprog`).

### Linux

1. Install the J-Link Software Pack (`.deb`/`.rpm`, or the `.tgz` into
   `/opt/SEGGER/JLink`). The packages install the udev rules
   `/etc/udev/rules.d/99-jlink.rules`; with the `.tgz`, copy `99-jlink.rules`
   from the pack there yourself. Then `sudo udevadm control --reload-rules &&
   sudo udevadm trigger` and re-plug the probe. `JLinkExe` must run without
   `sudo`.
2. nRF Util: download the `nrfutil` binary, `chmod +x`, put it on `PATH`, then
   `nrfutil install device` and `nrfutil device list`.
3. Optional, legacy: the nRF Command Line Tools tarball (`nrfjprog`).

Docker Desktop cannot pass USB to containers on Windows; enroll from the host.

## Wiring

Connect the probe's **SWDIO**, **SWCLK**, **GND** and **VTref** (VTref senses
the target voltage; tie it to the tag's VDD). The probe does not power the tag
through VTref: power the tag from its batteries or a 3.0 V bench supply, and
never feed a probe's 5 V pin into a 3 V tag. Keep leads short (≈ 10–15 cm); with
long or unshielded leads lower the speed. Pad locations per board are recorded
during [hardware verification](hardware/verification.md). After enrollment,
disconnect the probe and power-cycle the tag: the debug interface stays powered
until a power-on reset.

| Board (`--board`) | SoC | J-Link device | Stock panel |
|---|---|---|---|
| `laowu_bw` (`laowu_bw_nrf51822`) | nRF51822 | `nRF51822_xxAB` | `bw` (UC8176 4.2″ BW) |
| `laowu_bwr` (`laowu_bwr_nrf51802`) | nRF51802 | `nRF51822_xxAA` | `bwr` (UC8176 4.2″ BWR) |
| `sifei_52810` (`sifei_nrf52810`) | nRF52810 | `nRF52810_xxAA` | `unverified` |
| `hema_52811` (`hema_nrf52811`) | nRF52811 | `nRF52811_xxAA` | `unverified` |
| `nrf52dk_tag` | nRF52832 | `nRF52832_xxAA` | `none` (virtual 400×300 BW) |

## The command

```text
cremind-tag tag enroll --board <board> [--panel <panel>] [--firmware <zephyr.hex>]
                       [--protect [--yes]] [--dry-run [--register]]
                       [--tool auto|nrfutil|nrfjprog|jlink] [--serial-number <SN>]
                       [--name <name>] [--tag-id <8 hex>] [--out <dir>] [--keep-hex]
                       [--width W --height H --planes 1|2 --plane-flags F]
```

| Flag | Meaning |
|---|---|
| `--board` | tag board (table above; spec names, short ids or numbers) |
| `--panel` | panel; defaults to the board's stock panel. `unverified` panels (Sifei, Hema) need their geometry given with `--width --height --planes --plane-flags` until the panel is verified |
| `--firmware` | Intel HEX to flash after a **full chip erase**; without it only UICR is rewritten and the application on the tag stays |
| `--protect` | enable APPROTECT after enrollment, after a confirmation (see [APPROTECT](#approtect-trade-off)) |
| `--dry-run` | print every command, write the UICR image and J-Link command files, run nothing, change nothing |
| `--register` | with `--dry-run`: also store the secret and add the inventory row, so the printed commands can be run by hand |
| `--tool`, `--serial-number` | see [Tools](#tools) |
| `--yes` | with `--protect`: do not ask before enabling APPROTECT |
| `--name`, `--tag-id` | a name for the inventory; a chosen tag id instead of a random one (must be unused) |
| `--out`, `--keep-hex` | where the UICR image goes (default `<data dir>/enroll`); keep it after a real run (it holds the secret) |

### What happens

1. Validate board and panel; pick a random tag id in `1..0xFFFFFFFE` that is not
   in the inventory, and 32 random bytes from the OS CSPRNG.
2. Pack the blob and write its Intel HEX image `<TAGID>-uicr.hex` (mode 0600 —
   it contains the secret) to the enrollment directory in the companion's data
   directory.
3. **Store the secret first**, so a crash after programming can never leave a
   tag whose secret exists nowhere else.
4. Program:
   - with `--firmware`: erase the whole chip and program the firmware
     (verified), then program the UICR image without erasing;
   - without: erase only the UICR page, then program the UICR image.
5. Read the 48 bytes at 0x10001080 back; they must equal the blob and parse
   (magic, version, CRC).
6. Add the inventory row (board, panel geometry and plane encoding, secret
   reference). The tag is enrolled from here on.
7. With `--protect`: confirm, enable APPROTECT, mark the row protected.
8. Reset the tag. A failed reset is only a warning — power-cycle the tag.

Any failure before step 6 deletes the stored secret: the tag holds no usable
identity, and running the enrollment again erases it again. The UICR image is
deleted after a real run (also after a failure) unless kept on request.

Commands for `laowu_bw` without `--firmware` (nrfjprog):

```bash
nrfjprog -f NRF51 --eraseuicr
nrfjprog -f NRF51 --program <data dir>/…/1A2B3C4D-uicr.hex --verify
nrfjprog -f NRF51 --memrd 0x10001080 --n 48 --w 32
nrfjprog -f NRF51 --reset
```

With nrfutil the UICR step is one `nrfutil device program --firmware <hex>
--options chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE,verify=VERIFY_READ`
(the image touches only the UICR page); with J-Link Commander each step is a
command file (`r`, `h`, NVMC `ERASEUICR`, `loadfile`, `mem32`, …) that
`--dry-run` lists in full.

## Secrets

- Stored with `keyring` in the OS credential store (Windows Credential Manager,
  macOS Keychain, Secret Service), service `cremind-tag`, key `tag:<TAGID>`.
  Without a usable keyring (headless Linux, containers) or with
  `[secrets] backend = "file"`, a JSON file `<data dir>/secrets.json` created
  with mode 0600 is used. The inventory keeps only a reference such as
  `keyring:tag:1A2B3C4D`.
- Never sent to Cremind or to a bridge. Bridges receive `K_epoch` =
  HKDF(secret, tag id, epoch), derived on demand
  ([protocol §5.4](protocol.md#54-handshake-plaintext-on-ctrl)).
- Never logged; neither is the UICR image. Apart from the tag itself, the
  credential store holds the only copy: losing it means re-enrolling the tag.

## APPROTECT trade-off

Without protection, UICR — and with it the secret — is readable by anyone with
physical SWD access, who can then impersonate that one tag
([security model](security.md#physical-attacks-out-of-scope-for-v1-documented)).

`--protect` enables readback protection after the readback succeeded:
`nrfutil device protection-set All`, `nrfjprog --rbp ALL`, or with J-Link
Commander direct UICR writes through the NVMC (`NVMC.CONFIG` = WEN, then nRF52
`UICR.APPROTECT` (0x10001208) = `0xFFFFFF00`, nRF51 `UICR.RBPCONF` (0x10001004)
= `0xFFFF00FF`, i.e. `PALL` enabled). It takes effect at the next reset and is
**irreversible without a full chip erase**, which also erases the firmware and
the secret: the tag must then be reflashed and re-enrolled. The command asks
for confirmation first; declining skips protection without failing the
enrollment. After protection J-Link Commander may no longer be able to restart
the core; the resulting reset warning is expected.

## Recovery and re-enrollment

- **Re-enroll** = remove the tag from the inventory, then enroll again with
  `--firmware` (full chip erase, new id and secret).
- **Locked chip** (APPROTECT/readback protection, including stock vendor
  firmware that set it): `nrfutil device recover` or
  `nrfjprog -f NRF51|NRF52 --recover` erases the whole chip and unlocks it. The
  vendor firmware cannot be recovered afterwards.
- **nRF52 hardware APPROTECT**: newer nRF52 silicon revisions start with the
  debug port protected unless `UICR.APPROTECT` holds `HwDisabled` (0x5A) *and*
  the firmware opens the software branch at boot. An erased `UICR.APPROTECT`
  therefore counts as protected there after the next reset, so such a tag may
  need `recover` (i.e. full re-enrollment) before it can be reprogrammed even
  without `--protect`. Record the silicon revision during hardware
  verification (on the nRF52811 the hardened revision has a build code
  starting with `B`: the second line of the package marking, e.g. `QFAAB0`).

## First-sample checklist

Confirm on real hardware and correct `tools.py` if needed:

- nrfutil: nRF51 support of `nrfutil device`; that
  `ERASE_RANGES_TOUCHED_BY_FIRMWARE` erases UICR for a UICR-only image; the
  output of `nrfutil device read` (text or JSON — the parser accepts word dumps,
  byte dumps, byte/word lists and hex strings); `protection-set All`.
- nrfjprog: `--eraseuicr` on nRF51 parts.
- J-Link Commander: `mem32 <addr>, 0x0C` count parsing; `Sleep`; that
  `loadfile` programs UICR after the NVMC `ERASEUICR` sequence; that `erase`
  includes UICR; failure detection (`-ExitOnError 1` plus output markers); the
  reset after protection.
- On nRF52 revisions with hardware APPROTECT: whether an unprotected tag stays
  debuggable after a power cycle (see above).
