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
[companion](docs/companion.md) · [releasing](docs/releasing.md).

## Status

No physical tag, bridge or gateway samples have been measured yet. Every board
is tracked in [`hardware/matrix.yaml`](hardware/matrix.yaml) as documented →
buildable → functional → qualified, or blocked with a measured cause. Final
resource, timing and battery figures come only from qualification reports.

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
