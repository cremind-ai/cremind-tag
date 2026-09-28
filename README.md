# Cremind Tag

Battery e-paper tags that show what [Cremind](https://github.com/) needs you to
see — questions waiting for an answer, finished and failed tasks, progress,
calendar items, health — delivered over a Zephyr Bluetooth Mesh backbone.

```
Cremind ──HTTPS──▶ PC companion ──USB──▶ nRF52840 gateway ──mesh──▶ powered bridge ──BLE──▶ e-paper tag
```

- **Firmware** (C, Zephyr in nRF Connect SDK v3.4.1): gateway, bridge and tag
  applications using **only Zephyr's Bluetooth Host, Mesh and open-source
  Controller** (`CONFIG_BT_LL_SW_SPLIT`) on every target.
- **Companion** (Python 3.13+): durable queue, Cremind connector, multilingual
  text layout with ICU + HarfBuzz, font-pack builder (FreeType, all Noto
  scripts), enrollment and J-Link flashing, preview, diagnostics.
- **Cremind integration** lives in the Cremind repository (Tags page, `cremind
  tags` CLI, `/api/tags`, connector API).

Start here: [architecture](docs/architecture.md) ·
[protocols](docs/protocol.md) · [security](docs/security.md) ·
[font packs](docs/fontpack.md) · [connector API](docs/connector-api.md) ·
[hardware matrix](docs/hardware/matrix.md) · [building](docs/building.md) ·
[companion](docs/companion.md) · [releasing](docs/releasing.md) ·
[simple setup](docs/connect-setup.md) · [Cremind Connect packaging](docs/connect-packaging.md).

## Status

No physical tag, bridge or gateway samples have been measured yet. Every board
is tracked in [`hardware/matrix.yaml`](hardware/matrix.yaml) as documented →
buildable → functional → qualified, or blocked with a measured cause. Final
resource, timing and battery figures come only from qualification reports.

| Area | State |
|---|---|
| Tag boards (Laowu BW/BWR nRF51822, Sifei 52810, Hema 52811) | **buildable**: secure firmware links within each board's exact memory geometry; Sifei/Hema panel pins still unverified (the firmware never drives them) |
| Bridges (nRF52840, nRF52832) and gateways (nRF52840 DK or Dongle, nRF52832) | **buildable** |
| Stack | every image verified Zephyr Host + Controller only (`tools/verify_stack.py`); all nine targets rebuild byte for byte (`tools/repro_check.py`) |
| End to end | the first vertical slice (Cremind event → companion → gateway → bridge → tag → ACK back in Cremind) passes against a real Cremind with the simulator standing in for the radio (`tools/e2e_slice.py`) |
| Scale (simulated) | 1 gateway, 5 bridges, 20 tags: 98.1 % of deliveries start within 60 s at 5 events/min ([scale test](docs/scale-test.md)); real radio numbers need the hardware run |
| Before shipping | allocate USB VID/PID and a Bluetooth SIG company id (both are test values), verify panel pins, qualify each board ([qualification](docs/qualification/)) |

## Quick start (development)

```bash
# Companion
cd companion && uv sync && uv run pytest -q && uv run cremind-tag --help

# Firmware (Docker, pinned NCS v3.4.1 toolchain; see docs/building.md)
python tools/build.py --list
python tools/build.py gateway-nrf52840dk bridge-nrf52840dk tag-laowu-bw
```

## Licence

MIT (see `LICENSE`). Font packs are derived from Noto fonts and are
distributed under the SIL Open Font License 1.1 with the notices generated into
each pack's `NOTICE`. No code from EPD-nRF5 (GPL-3.0) is used; its hardware
documentation informed the board definitions.
