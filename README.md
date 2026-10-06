# Cremind Tag firmware

Battery e-paper tags that show what [Cremind](https://github.com/cremind-ai/cremind)
needs you to see — questions waiting for an answer, finished and failed tasks,
progress, calendar items, health — delivered over a Zephyr Bluetooth Mesh backbone.

```
Cremind ──USB──▶ nRF52840 gateway ──mesh──▶ powered bridge ──BLE──▶ e-paper tag
```

This repository holds the firmware and what it takes to develop it:

- **Firmware** (C, Zephyr in nRF Connect SDK v3.4.1): gateway, bridge and tag
  applications using **only Zephyr's Bluetooth Host, Mesh and open-source
  Controller** (`CONFIG_BT_LL_SW_SPLIT`) on every target, with their boards,
  devicetree, firmware libraries and tests.
- **The wire protocol**: [`protocol/spec.yaml`](protocol/spec.yaml), the
  [protocols](docs/protocol.md), [gateway setup](docs/connect-setup.md) and
  [font pack format](docs/fontpack.md), and the golden fixtures. It is
  authoritative here and published as a versioned **contract artifact**
  ([`tools/contract.py`](tools/contract.py)) that host software pins.
- **Build, programming and release tooling** (`tools/`) and the hardware
  qualification records.

The host side — driving gateways over USB, the delivery queue, text layout,
font packs, enrollment and flashing tools, the simulators and the whole-system
tests — is Cremind's hardware runtime (`cremind tags …`). Cremind and this
firmware are versioned independently: compatibility follows the contract's
protocol capabilities and the font pack identifiers. Building, testing and
releasing the firmware never needs Cremind.

Start here: [architecture](docs/architecture.md) ·
[protocols](docs/protocol.md) · [security](docs/security.md) ·
[font packs](docs/fontpack.md) · [hardware matrix](docs/hardware/matrix.md) ·
[building](docs/building.md) · [enrolling tags](docs/enrollment.md) ·
[releasing](docs/releasing.md).

## Status

No physical tag, bridge or gateway samples have been measured yet. Every board
is tracked in [`hardware/matrix.yaml`](hardware/matrix.yaml) as documented →
buildable → functional → qualified, or blocked with a measured cause. Final
resource, timing and battery figures come only from qualification reports.

| Area | State |
|---|---|
| Tag boards (Laowu BW/BWR nRF51822, Sifei 52810, Hema 52811) | **buildable**: secure firmware links within each board's exact memory geometry; the Hema 52811 drives its SSD1619 panel (bench-checked); Sifei panel pins still unverified (the firmware never drives them) |
| Bridges (nRF52840, nRF52832) and gateways (nRF52840 DK or Dongle, nRF52832) | **buildable** |
| Stack | every image verified Zephyr Host + Controller only (`tools/verify_stack.py`); all nine targets rebuild byte for byte (`tools/repro_check.py`) |
| End to end | the first vertical slice (Cremind event → gateway → bridge → tag → ACK back in Cremind) passes against a real Cremind with the simulator standing in for the radio (Cremind's `scripts/tags/e2e_slice.py`) |
| Scale (simulated) | 1 gateway, 5 bridges, 20 tags: 98.1 % of deliveries start within 60 s at 5 events/min ([scale test](https://github.com/cremind-ai/cremind/blob/main/docs/tags/scale-test.md)); real radio numbers need the hardware run |
| Before shipping | allocate USB VID/PID and a Bluetooth SIG company id (both are test values), verify panel pins, qualify each board ([qualification](docs/qualification/)) |

## Quick start (development)

```bash
# Tools (Python, pyproject.toml): their tests, the generated C bindings, the contract
uv sync && uv run pytest -q
uv run python tools/codegen.py --check
uv run python tools/contract.py            # dist/contract/cremind-tag-contract-<version>.tar.gz

# Firmware (Docker, pinned NCS v3.4.1 toolchain; see docs/building.md)
python tools/build.py --list
python tools/build.py gateway-nrf52840dk bridge-nrf52840dk tag-laowu-bw
```

## Licence

MIT (see `LICENSE`). No code from EPD-nRF5 (GPL-3.0) is used; its hardware
documentation informed the board definitions. The font packs bridges draw
with are built and distributed by the host software (derived from Noto fonts,
SIL Open Font License 1.1).
