# Verifying a physical sample

Every board in [the hardware matrix](matrix.md) starts from documentation only.
Before a board can become *functional*, one physical sample is checked against
the seven items below and the evidence is recorded in its
[qualification report](../qualification/). Each item unlocks a specific change
in the board definition (`boards/cremind/<board>/`); until then the firmware
treats the feature as absent.

Tools: a J-Link (or any SWD probe), a multimeter with continuity mode, a logic
analyser (≥ 8 channels, ≥ 24 MS/s), a bench supply or source-measure unit
(Nordic PPK2 or similar), a magnifier, and — for boards still running the vendor
firmware — nothing that erases the chip until items 1–3 are recorded.

Register addresses below are for reading over SWD, e.g. with J-Link Commander
(`JLinkExe -device <device> -if SWD -speed 4000 -autoconnect 1`, then
`mem32 <address> <count>`) or `nrfjprog --memrd <address> --n <bytes>`.

## 1. Actual MCU variant and memory geometry

1. Read the package marking (e.g. `N51822 QFAB`, `N51802 QFAA`, `N52810 QFAA`,
   `N52811 QFAA`) and photograph it.
2. Read the factory information (FICR):
   - nRF51: `CODEPAGESIZE` `0x10000010` × `CODESIZE` `0x10000014` = flash bytes;
     `NUMRAMBLOCK` `0x10000034` × `SIZERAMBLOCKS` `0x10000038` = RAM bytes;
     `CONFIGID` `0x1000005C` (bits 15:0 = HWID, which identifies the variant and
     revision in Nordic's nRF51 compatibility matrix).
   - nRF52: `INFO.PART` `0x10000100`, `INFO.VARIANT` `0x10000104` (ASCII, e.g.
     `AAB0`), `INFO.PACKAGE` `0x10000108`, `INFO.RAM` `0x1000010C` (KiB),
     `INFO.FLASH` `0x10000110` (KiB).
3. Compare with `hardware/matrix.yaml` (`flash_kib`, `ram_kib`) and with the
   SoC selected in `Kconfig.<board>` / the `soc` of the target in
   `tools/targets.yaml`. `verify_stack.py` enforces the geometry at build time;
   this step proves the geometry matches the silicon.
4. If the chip is read-protected (nRF51 `RBPCONF` at `0x10001004`, nRF52
   APPROTECT), FICR is still readable on nRF51 but not over a locked nRF52
   access port; record that the vendor firmware is protected before recovering.

Unlocks: the SoC selection is confirmed; a mismatch means a new board or a
corrected `soc`.

## 2. Display pin mapping and signal polarity

1. With power off, trace every panel FPC contact that carries a logic signal
   (BS1, BUSY, RES#, D/C#, CS#, SCL, SDA) to an MCU pad with continuity mode and
   record the GPIO number. Record which panel supply pins are switched (see 6).
2. With the vendor firmware still present, capture one full refresh on the
   logic analyser: SCK, MOSI, CS, D/C, RST, BUSY (and BS if routed). Confirm:
   CS active low; D/C high for data bytes; RST pulses low; BUSY is **low while
   busy** on UC8176 (then `busy-active-high` stays absent); BS1 is held low
   (4-wire SPI).
3. Note the SPI clock rate and mode used by the vendor firmware.
4. Compare with `pins` in `hardware/matrix.yaml` and the board devicetree.

Unlocks: real pins in `<board>-pinctrl.dtsi` and the panel node; on Sifei
this replaces every `*_PLACEHOLDER_*` value and removes `zephyr,deferred-init`
from the SPI bus (together with item 3). The Hema keeps a deferred bus: its
driver starts it after switching the panel supply on.

## 3. Panel model, resolution, colour planes and connector orientation

1. Photograph the FPC label and the panel's rear marking; look up the panel
   and its controller.
2. From the step-2 capture: identify the controller commands (UC8176: `0x10`
   DTM1 and `0x13` DTM2) and count bytes per data transfer —
   `ceil(width / 8) × height` per plane (15 000 bytes for 400×300). Two data
   transfers with different content indicate a red plane.
3. Measure the active area and note the connector orientation (pin 1) and the
   image orientation the vendor firmware uses.
4. With a bring-up build, draw a test pattern (corner markers, one row per
   plane) and confirm width, height, orientation and `plane-flags` (which bit
   value is white in plane 0 and red in plane 1).

Unlocks: `width`, `height`, `planes`, `plane-flags`, a real `panel-id` (instead
of 255, see `protocol/spec.yaml` `panels`) and finally `panel-verified` once
items 2 and 3 both pass.

## 4. Oscillators

1. High-frequency crystal: read its marking (`16.000` or `32.000`).
   - nRF51 supports 16 or 32 MHz; the chip must be told which through
     `UICR.XTALFREQ` (`0x10001008`: `0xFF` = 16 MHz, `0x00` = 32 MHz). A
     mismatch shifts the radio frequency and breaks Bluetooth. Record the
     vendor value before any erase.
   - nRF52 always uses 32 MHz.
2. 32.768 kHz crystal: look for a crystal and two load capacitors on the XL1/XL2
   pins (nRF51 QFN48: P0.27/XL1, P0.26/XL2; nRF52810/811 QFN48: P0.00/XL1,
   P0.01/XL2). If present, confirm with a build that selects
   `CONFIG_CLOCK_CONTROL_NRF_K32SRC_XTAL=y`: the LF clock must start (the
   kernel timer stalls if it does not).
3. With the RC oscillator (default), measure the calibration cost as part of
   the idle current in item 7.

Unlocks: `hfxo-verified`; with a crystal, `lfxo-verified` and the board
defconfig switches from the calibrated RC to `K32SRC_XTAL` (better timing,
lower idle current).

## 5. Battery arrangement and supply voltage

1. Record the cell type, count and arrangement (series/parallel) and the
   holder.
2. Measure the open-circuit voltage and the voltage at the MCU VDD during a
   panel refresh (minimum under load).
3. Identify any regulator, reverse-polarity protection or series resistance
   between the battery and VDD, and whether VDD is the raw battery voltage
   (nRF51 range 1.8–3.6 V; nRF52810/811 1.7–3.6 V).

Unlocks: the low-battery threshold and battery-voltage measurement method for
the board.

## 6. Power switches, external storage, wake sources and battery sensing

1. Power switches: find transistors or load switches in the panel supply and
   map their gate to a GPIO (continuity + logic analyser during a refresh).
2. External storage: look for an 8-pin SOIC/USON flash footprint and whether it
   is populated; record the part and its SPI pins.
3. Wake sources: button, reed switch, NFC antenna/field detect. Map each to a
   GPIO and its idle level. With a bring-up build, enter System OFF with the
   pin configured for SENSE and confirm a wake.
4. Battery sensing: a resistor divider to an analog pin, or none (then the
   internal VDD measurement is used).
5. DC/DC: check for the DC/DC inductor (and, on nRF51, its capacitor) on the
   DCC pin; without it the DC/DC regulator must stay off.

Unlocks: `wake-gpios` + `wake-verified` (required before `CMD{SLEEP}` enters
System OFF), `dcdc-verified` (then `&reg { regulator-initial-mode =
<NRF5X_REG_MODE_DCDC>; };` on nRF52, or `nrf_power_dcdcen_set()` on nRF51 —
firmware-notes section 6), and devicetree nodes for switches, storage and the
sensing channel.

## 7. Sleep current with development equipment disconnected

1. Flash the firmware under test, then **disconnect the probe and power-cycle**:
   after SWD access the debug interface stays powered (hundreds of µA to mA)
   until a power-on reset. SWD lines left connected can also back-feed.
2. Supply the tag from a source-measure unit at the nominal battery voltage
   (PPK2 in source mode), not from the batteries.
3. Record the average over at least 5 minutes of normal operation (several
   30-second advertising windows), the idle floor between windows, the
   advertising window, a connected transfer and a refresh (peak and duration).
4. Repeat with the panel disconnected if the idle current is unexpectedly high,
   to separate board leakage from firmware.

Unlocks: the power table of the qualification report and the battery-life
estimate.

## Recording evidence

For every item fill the checklist row in the board's qualification report
(result, evidence link — photo, capture file, measurement log — and date), then
change the board files in the same pull request as the evidence. Update the
board's `status` in `hardware/matrix.yaml` and regenerate the matrix page with
`python tools/gen_hardware_docs.py`.
