# Tag firmware (`apps/tag`)

The battery e-paper tag: a Zephyr application (NCS v3.4.1, Zephyr Link Layer
only) that wakes, advertises, authenticates the bridge, receives an image as
authenticated records, verifies its SHA-256, refreshes the panel inside the
display transaction of [protocol.md §6](protocol.md#6-tag-display-transaction)
and reports the result. Protocol behaviour is normative in
[protocol.md §5–§6, §9–§10](protocol.md); this page documents how the firmware
implements it, what it measured, and what still has to be verified on a
physical sample.

> Only the SSD1619 panel driver has run on hardware (a bench test on the Hema
> 52811, §6). Memory figures are link results; stack figures are static
> estimates; the UC8176 driver is written from its datasheet and has not driven
> a panel.

## 1. Layout

| File | Role |
|---|---|
| `src/tag_core.c/.h` | Bluetooth-independent protocol core: reassembly (2 record buffers), handshake, records, credits, FRAME_BEGIN decision, incremental frame SHA-256, display transaction, pacing, session timeout. Talks to the platform through the hooks at the end of `tag_core.h` and to the panel through `panel.h`. |
| `src/gatt.c` | 128-bit service (CAPS read, CTRL write+indicate, DATA write-without-response, STATUS notify, two CCCs), connection callbacks, transmit pump. |
| `src/adv.c` | Wake cycle and legacy connectable advertising; the same timer is the session timeout while connected. |
| `src/store_nvs.c` | Raw NVS on `storage_partition` (two entries). |
| `src/enroll_uicr.c` | Enrollment blob in `UICR.CUSTOMER` (0x10001080). |
| `src/panel.h`, `src/panel_uc8176.c` | Panel interface of §6 and the UC8176 profile. |
| `src/battery.c` | VDD measurement. |
| `src/main.c` | Boot, the core work item, platform hooks. |
| `prj.conf`, `socs/*.conf|.overlay`, `boards/nrf52dk_nrf52832.*`, `Kconfig` | Configuration (section 8). |
| `debug/log.conf`, `debug/uart0.overlay` | Optional UART-log build (section 10). |
| `tools/stack_depth.py`, `tools/stack_edges.txt` | Static stack estimator (section 9). |
| `tests/ztest/tag_core/` | native_sim tests of the core (section 11). |

## 2. Execution model

```
               RADIO/RTC/SWI ISRs (ISR stack)          controller RX threads
                              │                         (recv 640/896 B, prio 384/448 B)
                              ▼                                     │ bt_hci_recv
 ┌──────────────────────── system work queue (1280 B nRF51, 1536 B nRF52) ─────────────┐
 │ host RX (BT_RECV_WORKQ_SYS) ─► gatt.c write_value ─► tag_core_rx() (reassembly only) │
 │            │ message complete ─► app_kick()                                          │
 │ core_fn:  panel_poll() ─► tag_core_poll() ─► gatt_pump()                             │
 │ adv_fn:   wake window / session timeout / linger                                    │
 │ init_work (async bt_enable) ─► bt_ready(): crypto, NVS, panel, core, advertising    │
 └──────────────────────────────────────────────────────────────────────────────────────┘
 main thread (512 B): enrollment check, bt_enable(bt_ready), returns
```

- **One cooperative context.** `CONFIG_BT_RECV_WORKQ_SYS=y` on every tag SoC,
  so the GATT callbacks, the core, the BUSY poll and all crypto run on the
  system work queue: the core needs no locking, and no BT RX thread stack is
  allocated. The write callbacks only reassemble; the processing runs in a
  separate work item, so the host RX call chain and the crypto/NVS chain are
  never stacked on each other.
- **Two timers.** `core_work` (delayable) runs the core: kicked by writes, ATT
  completions and the panel; re-armed after 10 ms when the ATT buffers are
  exhausted (`-ENOMEM` from the system work queue) and every 50 ms while a
  refresh runs. `adv_work` is the wake/advertising timer when disconnected
  and the session timeout (20 s) / linger (2 s) when connected.
- **Transmit pump.** The core keeps one ordered queue of whole messages
  (112 bytes) and fragments on demand. CTRL values go out as indications, one
  at a time (the next after the confirmation); STATUS values as notifications
  while ATT buffers last. Because the queue is ordered, AUTH_OK is confirmed
  before the first CREDIT leaves. A subscription missing on CTRL or STATUS
  makes the send fail and the tag disconnects: the bridge must enable CTRL
  indications and STATUS notifications before HELLO.

## 3. State machines

### Link and session

```
          connected                HELLO ok               AUTH ok (epoch stored first)
 DOWN ──────────────▶ IDLE ──────────────▶ CHALLENGED ──────────────────▶ ESTABLISHED
  ▲                    │ HELLO bad           │ AUTH bad (counts toward       │ record MIC/counter bad,
  │                    ▼                     ▼ pacing), STORAGE_ERROR        ▼ malformed, no credit
  └──── disconnect ◀── CLOSING ◀──────────────────────────────────────────── (ERROR{status})
                        (queue drained or 2 s linger; 20 s without progress: silent)
```

Every `ERROR` carries the tag's stored epoch: `ERROR{status, stored_epoch}`
(§5.4; epochs are not secret, the CHALLENGE reports the same value), so a
bridge refused with `STALE_EPOCH` can tell the companion which epoch to
assign above.

The handshake transcript covers the CAPS value the tag serves (§5.4):
`gatt.c`'s `tag_hal_caps()` builds it for the CAPS read and for the core,
which hashes it at HELLO (18 bytes on the handshake's stack, no RAM kept).

| Event | Tag answer | Then |
|---|---|---|
| HELLO malformed / other tag_id / proto / epoch < stored | `ERROR{INVALID / NOT_FOUND / VERSION_MISMATCH / STALE_EPOCH}` (library order, §5.4) | close |
| AUTH malformed or MAC wrong | `ERROR{AUTH_FAILED}`; 3 consecutive → skip the next wake window | close |
| AUTH ok, epoch > stored, NVS write fails | `ERROR{STORAGE_ERROR}` | close |
| AUTH ok | `AUTH_OK`, then `CREDIT{2}` | established |
| CTRL message out of turn (second HELLO, AUTH before HELLO, any CTRL after AUTH_OK) | `ERROR{INVALID}` | close |
| DATA before AUTH_OK | `ERROR{INVALID}` | close |
| record MIC / counter / length wrong | `ERROR{AUTH_FAILED}` | close |
| record without credit (both buffers busy) or unknown type or malformed plaintext | `ERROR{INVALID}` | close |
| fragment rule broken (§5.3) | nothing (as the simulator) | close |
| 20 s without any write (not during a refresh) | nothing | close |

### Frame and display transaction

```
 IDLE ──FRAME_BEGIN accepted──▶ RECEIVING ──FRAME_END, complete, digest ok, panel idle──▶ VALIDATED
   ▲        │ rejected: RESULT     │ PLANE_DATA out of order / write fails: RESULT INVALID / PANEL_ERROR
   │        ▼                      │ FRAME_END early: INCOMPLETE; digest: DIGEST_MISMATCH (never refreshes)
   │                               │ FRAME_ABORT, new FRAME_BEGIN, disconnect: abort_frame(), no RESULT
   │                                                                                     │
   │   REFRESH_INTENT saved ─▶ PROGRESS{REFRESHING} ─▶ commit_refresh() ─▶ REFRESHING ◀──┘
   │                                                                          │ BUSY released / timeout
   └── RESULT + CREDIT ◀── DISPLAYED saved (OK) or INTENT kept with status ◀──┘
```

- The FRAME_BEGIN table is `ctag_txn_frame_begin()` (tested row by row against
  `tag_txn.json`), after one extra rule: **an unusable panel (id 255, or an
  init failure) answers `RESULT UNSUPPORTED`** for every frame and CLEAR, and
  the panel is never touched. The companion treats UNSUPPORTED as a failed
  revision, not a retry.
- The transaction runs from the top of `tag_core_poll()` (state VALIDATED), so
  the NVS write and its garbage collection sit on a shallow stack.
- While a transaction runs no further record is processed; the credit of
  FRAME_END (or CLEAR) follows its RESULT. A second record already reassembled
  waits in the other buffer.
- The tag stays connected through transfer, refresh, persist and RESULT (the
  session timeout re-arms while refreshing). A disconnect during the refresh
  does not stop it: DISPLAYED is persisted, no RESULT is sent, and
  `result_pending` (advertising flag bit 0) stays set until a RESULT is
  queued. The re-delivery is answered from the stored ACK (`RESULT OK` with
  `flags.bit0`), and that RESULT clears `result_pending` like any other
  (as the simulator's `send_result()`): the pending flag means "an outcome
  has not been handed to a bridge yet", and the stored ACK hands it over.
  The flag is RAM only: after a reset the stored ACK still answers the next
  re-delivery.
- `CMD{CLEAR}`: begin_frame, both planes staged with white from the
  plane_flags (plane 0 white, plane 1 not red), digest computed on the way,
  then the same transaction with revision 0. `CMD{IDENTIFY}`, `CMD{REFRESH}`
  and unknown commands answer `RESULT UNSUPPORTED` (revision 0, §10).
  `CMD{SLEEP}` answers OK and enters System OFF after the disconnect only on a
  board whose devicetree marks `wake-verified` (none today), else UNSUPPORTED.

### Wake cycle

Every 30 s ± 3 s (uniform, `sys_rand32_get()`) measured from the previous
window start: if not connected, not refreshing and not paced, update the
manufacturer data (flags from the core and a fresh VDD reading, low 16 bits of
the displayed revision), start legacy connectable advertising (250 ms
interval, `BT_LE_ADV_OPT_CONN`, identity address), stop after 2 s. The first
window opens right after boot. Between windows the kernel is tickless: System
ON idle with the RTC running (plus the LFRC calibration every 4 s on boards
without a verified crystal).

## 4. Persistence

Raw NVS on the board's `storage_partition`, one sector per flash erase block
(1 KiB nRF51, 4 KiB nRF52). A write is atomic (data first, CRC-protected
allocation entry last).

| Id | Entry | Written |
|---|---|---|
| 1 | ctag_txn display record, 60 bytes (§6, normative layout) | REFRESH_INTENT before the refresh; DISPLAYED/failure status after; the boot rule's DISPLAY_STATE_UNKNOWN |
| 2 | `tag_id u32 ‖ stored_epoch u32 ‖ crc32 u32` | once per epoch increase, after AUTH verified and before AUTH_OK is sent |

- The stored epoch needs its own entry: the display record's epoch cannot be
  advanced without faking a displayed frame. At boot the tag uses
  `max(epoch entry, record epoch)`.
- Both entries carry the tag id; an entry of another enrollment is ignored.
- **Corrupt record** (bad length, version, state or CRC, or unreadable
  storage): the tag behaves as if it had no record and reports
  `STORAGE_ERROR` in `CHALLENGE.last_status` until a transaction rewrites the
  record (coordinator ruling on §6). An unreadable epoch entry is reported the
  same way.
- A failed REFRESH_INTENT write answers `RESULT STORAGE_ERROR` and the panel
  never refreshes. A failed DISPLAYED write after a good refresh answers
  `STORAGE_ERROR` (not OK): the RAM record keeps REFRESH_INTENT, like flash.
- Wear: two writes per update (68 bytes each with the allocation entry). On
  the Laowu BW's 4 × 1 KiB sectors a sector fills after ~7 updates and is
  garbage-collected; nRF51 pages endure ≥ 20 000 erases, so roughly half a
  million updates before wear-out (to be confirmed by the endurance test).

## 5. Enrollment and identity

`enroll_uicr.c` parses the 48-byte blob at `NRF_UICR->CUSTOMER[0]` with
`ctag_enroll_parse()` and additionally requires **board == devicetree
`board-id` and panel == devicetree `panel-id`**: an enrollment for other
hardware means the wrong firmware is flashed. Failure = `SECURITY_CONFIG`: Bluetooth
is never enabled, the board's `led0` (if any) blinks ten times, the debug
build logs the reason, the system stays idle. The secret is never copied: the
session reads it in place from UICR, and the parsed copy on main's stack is
wiped with volatile stores.

## 6. Panel drivers (UC8176, SSD1619)

`apps/tag/CMakeLists.txt` links one driver per image, chosen by the compatible of
the chosen `cremind,panel` node: `cremind,ssd1619` → `panel_ssd1619.c`,
otherwise `panel_uc8176.c` (also the virtual panel id 0 of the nRF52 DK). Both
implement `src/panel.h` and keep its rules (refuse panel id 255 before any pin
or bus, reset every frame, stage only, refresh only in `commit_refresh`, poll
BUSY without blocking, idempotent sleep).

### UC8176

From the UC8176 datasheet (UC8176c A0.1, in EPD-nRF5 `docs/datasheets/`, used
as hardware documentation only). OTP waveforms (`REG_EN = 0`); KW mode for one
plane, KWR mode for two.

#### Command sequences

| Step | Sequence | Notes |
|---|---|---|
| `panel_init` (boot) | BS low (if routed), RST inactive (high), DC low, BUSY input disconnected | never for panel id 255 or a devicetree/enrollment mismatch: no pin, no SPI |
| `begin_frame` | BUSY input; RST low 10 ms, high 10 ms; wait BUSY_N high (≤ 500 ms); BTST 0x06 {17 17 17}; PSR 0x00 {KW 0x1F / KWR 0x0F}; TRES 0x61 {W>>8, W&0xF8, H>>8, H&0xFF} = {01 90 01 2C}; CDI 0x50 {see below}; data start of plane 0 (KW: DTM2 0x13, KWR: DTM1 0x10) | hardware reset every frame: restart-safe from zero |
| `write_plane_chunk` | data bytes, DC high; the first chunk of plane 1 (KWR) sends DTM2 0x13 first | stages SRAM only |
| `validate_frame` | every byte of every plane staged and BUSY_N high | a busy controller before DRF is a fault |
| `commit_refresh` | PON 0x04, wait BUSY_N high (≤ 500 ms), DRF 0x12 | **DRF is the only refresh command; DSP 0x11 is never sent** (with data_flag set it starts a refresh) |
| `wait_refresh_complete` | first BUSY poll 50 ms after DRF, then every 50 ms from the core work item, bounded by devicetree `refresh-timeout-ms` (BW 10 s, BWR 30 s) → `REFRESH_TIMEOUT` | non-blocking; `refresh_ms` in RESULT is the elapsed time (±50 ms) |
| `sleep_panel` | POF 0x02, wait BUSY_N (≤ 500 ms; stuck → hardware reset), DSLP 0x07 {A5}; DC low; BUSY input disconnected | only a hardware reset wakes the controller |
| `abort_frame` | = sleep (no refresh) | |

PON is issued only at commit time, so the charge pumps stay off during the
(tens of seconds) transfer. BUSY_N is read as a raw level and interpreted with
`busy-active-high` (absent = low means busy, UC8176).

#### Planes and polarity

Plane bytes pass through unmodified; polarity is expressed in the CDI data
polarity bits (DDX), derived at compile time from the devicetree
`plane-flags`, so the bytes whose SHA-256 the tag verified are exactly the
bytes the controller receives:

| Mode | Plane → command | DDX from plane-flags | Meaning (datasheet CDI tables) | Border (VBD) |
|---|---|---|---|---|
| KW (planes = 1, PSR 0x1F) | plane 0 → DTM2 ("new") | DDX[1] = 1 ("new data only": LUTWB/LUTBW per pixel, old data unused), DDX[0] = bit0 | bit0 = 1: new 1 = white | LUTBW (white): VBD 10 if DDX[0] else 01 |
| KWR (planes = 2, PSR 0x0F) | plane 0 → DTM1 (B/W), plane 1 → DTM2 (red) | DDX[0] = bit0, DDX[1] = !bit1 | bit0 = 1: B/W 1 = white; bit1 = 1: red 1 = red (DDX[1] = 0) | LUTW (white): VBD 01 if DDX[0] else 10 |

With the boards' plane-flags (BW 0x01, BWR 0x03) this gives CDI 0xB7 (KW) and
0x57 (KWR); CDI[3:0] = 0111 (10 hsync, the default). The "new data only" KW
table is chosen because the tag cannot keep the previous frame (15 kB) as OLD
data: every pixel is driven towards its new colour whatever SRAM held.

#### Must be verified on a sample

1. BS low selects 4-wire SPI; SPI mode 0 at 4 MHz is accepted.
2. Reset timing (10 ms low / 10 ms) and BUSY_N release after reset.
3. No refresh starts before DRF (BUSY_N stays high while staging a full frame).
4. Orientation: PSR UD = SHL = 1 puts native row 0 / byte 0 at the panel's
   top-left as `docs/protocol.md` §4.4 assumes; a mirrored or rotated image is
   fixed in PSR (UD/SHL), never in the data.
5. Polarity: white/black (and red) as in the table; a wrong colour is fixed in
   the board's `plane-flags` (which also changes CAPS and the bridge's
   rendering), not in the driver.
6. KW "new data only" (DDX = 11) refreshes a full image cleanly with the OTP
   waveform; if the OTP LUTs for WB/BW are partial-update waveforms, fall back
   to DDX = 01 with DTM1 written as the inverse of the new data (documented
   alternative, costs a second pass).
7. PON/refresh/POF durations against the BUSY bounds; refresh time per panel
   for `refresh-timeout-ms` (the devicetree values are upper bounds).
8. Deep-sleep current with RST high and BUSY disconnected.
9. Flash writes (NVS, incl. garbage collection of a 1 KiB page, ~22 ms) during
   a connection: the Zephyr controller's flash/radio synchronisation must find
   the time slots at the bridge's 30–50 ms interval.

### SSD1619 (Hema 52811)

From the SSD1619A command set and the sequence EPD-nRF5 runs on the same board
(`EPD/SSD16xx.c` with its "Hema213" panel). OTP waveforms, the controller's
internal temperature sensor.

| Step | Sequence | Notes |
|---|---|---|
| `panel_init` (boot) | EN active (`en-gpios`, the panel supply), then the SPI bus (`zephyr,deferred-init`, started by `device_init()`), BS low, RES# inactive, DC low, BUSY disconnected | the supply stays on (deep sleep between refreshes), so CS is never driven high into an unpowered panel |
| `begin_frame` | BUSY input; RES# low 10 ms, high 10 ms; SW RESET 0x12; wait BUSY low (≤ 500 ms); border 0x3C {01}; sensor 0x18 {80}; data entry 0x11 {03} (X+ then Y+); RAM X 0x44 {X0/8, (X0+W−1)/8}; RAM Y 0x45 {0, 0, (H−1)&0xFF, (H−1)>>8}; counters 0x4E {X0/8}, 0x4F {0, 0}; WRITE RAM 0x24 | X0 = `ram-x-offset`: the Hema panel starts one byte (8 columns) into RAM |
| `write_plane_chunk` | data bytes, DC high; the first chunk of plane 1 moves the counters back to the origin and sends WRITE RAM (red) 0x26 | stages RAM only |
| `validate_frame` | every byte of every plane staged and BUSY low | |
| `commit_refresh` | DISPLAY UPDATE CONTROL 1 0x21 {RAM options, 00}; DISPLAY UPDATE CONTROL 2 0x22 {F7}; MASTER ACTIVATION 0x20 | 0xF7: clock and analog on, load temperature and LUT, display, analog and clock off |
| `wait_refresh_complete` | as for the UC8176; BUSY is high while busy (`busy-active-high`) | |
| `sleep_panel` | wait BUSY (stuck → hardware reset), DEEP SLEEP 0x10 {01}; DC low; BUSY disconnected | only a hardware reset wakes the controller |

Planes pass through unmodified. Natively a B/W bit 1 is white and a red bit 1 is
red; DISPLAY UPDATE CONTROL 1 takes the red RAM option in A[7:4] and the B/W
option in A[3:0] (0 normal, 8 inverse, 4 read as 0 for a one-plane panel),
derived from `plane-flags`: the Hema's 0x03 gives 0x00.

Bench check (2026-10-06, a Hema 2.13-inch sample, build code AAA0): a test frame
drawn through this driver (border, origin marker, black and red bars,
checkerboard) appeared complete, in the right colours and with no column
offset; BUSY held 12,577 ms after MASTER ACTIVATION. Held landscape, the tag
shows the native origin at its top-right: Cremind rotation 3.

## 7. Power behaviour

| State | What runs | Notes |
|---|---|---|
| Idle between windows | tickless kernel, RTC; LFRC calibration every 4 s (`MAX_SKIP=0`) | HFXO off; panel in deep sleep, BUSY input disconnected, DC low, RST high |
| Advertising window | 2 s of legacy advertising at 250 ms (8 events) | one VDD sample per window |
| Connected | Zephyr controller at the bridge's interval | crypto on the work queue |
| Transfer | SPI writes per PLANE_DATA record (189 B) | charge pumps off until PON |
| Refresh | PON + DRF, BUSY poll every 50 ms | CPU mostly idle |
| After refresh | POF + DSLP | |
| SECURITY_CONFIG | no Bluetooth at all | |

DC/DC stays off (no board has `dcdc-verified`). Battery: nRF52 through the
Zephyr ADC API on the SAADC's internal VDD input (gain 1/6, 0.6 V reference,
12 bit). nRF51: Zephyr's nRF51 ADC driver cannot select the supply input (it
only maps analog pins, 8 bit), so `CONFIG_APP_BATTERY_NRF51_DIRECT` samples VDD
with the ADC registers (1/3 supply prescaling, 1.2 V band gap, 10 bit), 160 B
of flash (measured: 95,412 B with, 95,252 B without); the ADC node stays
disabled so no driver owns the peripheral. Set it
to `n` to report 0 (unknown; never flagged low). Low battery = below
`CONFIG_APP_LOW_BATTERY_MV` (2400 mV, as the simulator).

## 8. Configuration and memory

`prj.conf` = the measured fragments of [firmware-notes.md §12](firmware-notes.md)
(base, *min*, *crypto-lean*) plus:

| Setting | Why |
|---|---|
| `BT_RECV_WORKQ_SYS=y` (all SoCs) | one context (section 2); saves the BT RX thread stack on nRF52 |
| `BT_EXT_ADV=n`, `BT_L2CAP_TX_MTU=23`, `BT_MAX_CONN=1`, `BT_SMP=n`, `BT_CTLR_CRYPTO=y` | legacy advertising only, ATT MTU 23, one link, application-layer security |
| `MBEDTLS_PSA_KEY_SLOT_COUNT=1` | `ctag_crypto` imports one volatile key and destroys it after each operation (−72 B) |
| `LTO=y`, `ISR_TABLES_LOCAL_DECLARATION=y` | link-time optimisation: −16 KB flash on nRF51. Not applied to the Zephyr Link Layer libraries (timing-critical code compiled as upstream; `verify_stack.py` keeps finding its symbols) nor to `ctag_session` (keeps the frame structure measured in firmware-libs.md; see section 9) |
| `socs/nrf51822.conf` | *tight*: `APP_TIGHT_CTLR_STACKS` (app Kconfig, default y on nRF51: controller RX 640 B, prio RX 384 B), `BT_BUF_EVT_RX_COUNT=3`; system work queue 1280 B |
| `socs/nrf5281x.conf`, `nrf52832.conf` | system work queue 1536 B; `ADC=y` (+ `&adc` okay in the SoC overlays) |
| `boards/nrf52dk_nrf52832.*` | development tag: tag-board (id 20) and a UC8176 node on the Arduino header; panel-id 0 (NONE, virtual panel) matches the companion's default enrollment; code partition 0–0x7a000, NVS in the DK's storage partition |

Security is untouched by every optimisation: same libraries, same PSA
primitives, same checks; the key-slot and queue sizes follow from the code's
own usage.

### Measured (container build, `tools/build.py`, `build/memory-report.json`)

| Target | Flash used / region (B) | Free | RAM used / region (B) | RAM free (target) | verify_stack |
|---|---|---|---|---|---|
| tag-laowu-bw | 95,508 / 126,976 | 24.8 % | 14,316 / 16,384 | **2,068** (2,048) | pass |
| tag-laowu-bwr | 95,628 / 258,048 | 62.9 % | 14,324 / 16,384 | **2,060** (2,048) | pass |
| tag-sifei-52810 | 100,492 / 188,416 | 46.7 % | 15,520 / 24,576 | 9,056 (3,072) | pass |
| tag-hema-52811 | 100,396 / 188,416 | 46.7 % | 15,520 / 24,576 | 9,056 (3,072) | pass |
| tag-nrf52dk | 100,904 / 499,712 | 79.8 % | 15,640 / 65,536 | 49,896 (none) | pass |

Measured 2026-09-28 after the protocol v1 finalisation (CAPS bound into the
handshake transcript, `ERROR{status, stored_epoch}`): +96 B flash on the
Laowu BW (+96 to +128 B on the others) and **no RAM**: the CAPS value is
built on the handshake's stack (`tag_hal_caps()`), the ERROR is packed into
the transmit queue, and `ctag_session` keeps its 124 bytes. The Laowu BW
keeps its 20 B above the 2 KiB RAM target and 12,421 B of flash below the
15 % headroom limit (107,929 B).

How the Laowu BW got there: first complete build 109,568 B flash (86.3 %, 1,639 B
over the 15 % headroom) and 1,828 B RAM free. LTO brought flash to ~92.7 KB;
keeping `ctag_session` and the handlers out of line for the stack budget cost
~2.7 KB back. RAM: one PSA key slot (+72 B), the BUSY poll folded into the core
work item and the session timeout into the advertising timer (two
`k_work_delayable` fewer, +96 B), a 112-byte transmit queue (+16 B).

Largest RAM users on the Laowu BW (bytes): system work queue stack 1280,
`app_core` 1264 (two 205-byte record buffers, 192-byte plaintext, 232-byte
PSA hash operation for the frame digest, 124-byte session, 112-byte queue),
ISR stack 1024, `ticker_user_ops` 660, controller RX stack 640, main stack
512, `mem_pdu_rx` 480, controller prio RX stack 384, HCI RX pool 355,
`bt_dev` 336.

The record buffers are `TAG_RECORD_WIRE_MAX` (205) bytes rather than the
`TAG_RECORD_BUF` (256) budget: the reassembler never writes more, and nothing
visible on the wire changes (CAPS still grants 2 credits).

The debug build (section 10) adds ~11.6 KB flash and ~170 B RAM (Laowu BW:
106,988 B, 1,900 B free) — it fits the flash but not the RAM target.

## 9. Stack budget

`tools/stack_depth.py` reads every function's frame from the disassembly of
the final (LTO) image, follows direct calls and long calls, and the indirect
calls listed in `tools/stack_edges.txt` (device API tables on nRF51 — on nRF52
LTO devirtualised them —, work handlers, GATT/ATT/HCI event dispatch, the
ctag_txn store callbacks):

```bash
python3 /work/apps/tag/tools/stack_depth.py /work/build/tag-laowu-bw/zephyr.elf \
    --edges-file /work/apps/tag/tools/stack_edges.txt --root work_queue_main --root 'on_*'
```

**Estimates** (bytes, deepest chain, excluding the 32-byte exception frame
every thread stack also takes on interrupt entry):

| Chain (system work queue: + work_queue_main 40 + core_fn 32/16) | nRF51 | nRF52 |
|---|---:|---:|
| HELLO: tag_hello (CAPS on the stack) → K_epoch → HKDF → psa_mac_compute → Oberon HMAC | 1000 | 964 |
| AUTH with a new epoch: → NVS write (incl. garbage collection) | 928 | 868 |
| record: FRAME_BEGIN → RESULT sealed (AES-CCM) | 1032 | **1016** |
| refresh: REFRESH_INTENT → NVS write with GC → flash sync → kernel | **1072** | 988 |
| refresh done: DISPLAYED → NVS | 1008 | 920 |
| host RX (rx_work_handler: ATT write, HCI events) | 656 | 600 |
| boot (init_work → bt_ready → boot rule → NVS) | 1064 | 972 |

| Stack | nRF51 size | worst estimate + 32 | margin | nRF52 size | worst + 32 | margin |
|---|---:|---:|---:|---:|---:|---:|
| System work queue | 1280 | 1104 | 176 | 1536 | 1048 | 488 |
| Main (SYS_INIT device init, then main) | 512 | 432 | 80 | 512 | 328 | 184 |
| Controller RX (`BT_CTLR_RX_STACK_SIZE`) | 640 | ~370 | ~270 | 896 | ~385 | ~510 |
| Controller prio RX (`BT_CTLR_RX_PRIO_STACK_SIZE`) | 384 | ~370 | **~15** | 448 | ~385 | ~60 |
| ISR, idle | 1024, 128 | not estimated (controller ISRs nest by priority) | | same | | |

How the work-queue budget was met: the first build merged every record
handler into one 376-byte frame (LTO inlining) and the transcript's 232-byte
PSA hash operation into the handshake handler, for an estimated 1,560 B.
Handlers with large locals are now out of line (`FRAME` in tag_core.c),
`ctag_session` is excluded from LTO, the boot loads are split from the boot
save, and the display transaction runs from the top of `tag_core_poll()`.

Unresolved edges the tool lists are small (ticker job triggers, PSA key-slot
wipe hooks, clock on/off notifications). The controller priority RX thread's
figure is the host's priority event handlers (command complete 256 B) under
`bt_hci_recv` — the "tight" 384 B was already flagged by firmware-notes §12 as
needing on-target measurement; it is the first thing to check with
`CONFIG_THREAD_ANALYZER` on a sample.

## 10. Build, flash, debug

```bash
uv run python tools/build.py tag-laowu-bw tag-laowu-bwr \
    tag-sifei-52810 tag-hema-52811 tag-nrf52dk
```

Artifacts land in `build/<target>/` (`zephyr.hex`, `zephyr.elf`, map,
`.config`, `verify.json`); the memory report in `build/memory-report.*`.

**Flash** (never chip-erase an enrolled tag, see [building.md](building.md#flashing-with-j-link)):

```bash
JLinkExe -device nRF51822_xxAB -if SWD -speed 4000 -autoconnect 1
J-Link> loadfile build/tag-laowu-bw/zephyr.hex
J-Link> r
J-Link> g
```

Enroll with the companion (`cremind tags tools tag enroll`), which writes the blob to
UICR; the board and panel in the blob must match the firmware (section 5).

**Debug build** (UART log on the debug TX pad: BW P0.06, BWR P0.08; DK VCOM):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v ctag-build:/build \
  -v "C:/path/to/cremind-tag:/work" ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 \
  -c 'cd /ncs && west build --no-sysbuild -p always -d /build/tag-laowu-bwr-debug -b laowu_bwr/nrf51822 \
      /work/apps/tag -- -DZEPHYR_EXTRA_MODULES=/work -DEXTRA_CONF_FILE=debug/log.conf \
      -DEXTRA_DTC_OVERLAY_FILE=debug/uart0.overlay'
```

For stack measurement add `CONFIG_THREAD_ANALYZER=y`,
`CONFIG_THREAD_ANALYZER_AUTO=y` (and the thread names) to a copy of
`debug/log.conf`.

Stack estimates after any code change:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v "C:/path/to/cremind-tag:/work" \
  ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 -c 'cd /work && python3 apps/tag/tools/stack_depth.py \
  build/tag-laowu-bw/zephyr.elf --edges-file apps/tag/tools/stack_edges.txt \
  --root work_queue_main --root "bg_thread_main*" --root recv_thread --root prio_recv_thread'
```

## 11. Tests

[`tests/ztest/tag_core`](../tests/ztest/tag_core) builds `tag_core.c`
unchanged with fakes for the panel, NVS, clock, battery and nonce hooks
(`src/fakes.c`), PSA from Mbed TLS, one key slot:

```bash
python tools/build.py --shell
apt-get update && apt-get install -y make
west twister -T /work/tests/ztest/tag_core -p native_sim -p native_sim/native/64 \
  -x ZEPHYR_EXTRA_MODULES=/work --outdir /build/twister-tag-core
```

| Suite | What |
|---|---|
| `tag_core_conv` (15) | Conversations scripted by [`gen_conversation.py`](https://github.com/cremind-ai/cremind/blob/main/scripts/tags/gen_conversation.py) with the companion's reference (`protocol/session.py`, `fragments.py`, `msgs.py`) and replayed byte for byte, every ATT value the tag sends compared (the tag serves the CAPS of the frame's panel; every ERROR carries the stored epoch): happy path (two records in flight), two planes, duplicate (stored ACK), stale revision, revision conflict, digest mismatch (no refresh), AUTH failure, a relayed CAPS (plane_flags rewritten on the bridge's read: AUTH fails, the panel is never touched), disconnect mid-transfer then restart from offset 0, power loss between REFRESH_INTENT and DISPLAYED → boot rule → CHALLENGE flag and DISPLAY_STATE_UNKNOWN → re-delivery → OK, epoch stored only after AUTH, IDENTIFY/REFRESH/SLEEP → UNSUPPORTED, CLEAR (white planes, revision 0), unverified panel refuses frames, refresh timeout keeps REFRESH_INTENT. Frames are `render.json` scenarios with plane bytes. |
| `tag_core_fixtures` (8) | `session.json`: exact CHALLENGE and AUTH_OK from the fixture CAPS/HELLO/AUTH and state, the fixture records decrypted and staged, every B2T tampered record → ERROR{AUTH_FAILED}, bad mac_b, the fixture's relayed-CAPS AUTH → ERROR{AUTH_FAILED, 2}, the fixture's ERROR{STALE_EPOCH, 4} byte for byte; `tag_txn.json`: every FRAME_BEGIN row through a live C bridge (the "older epoch" row is refused earlier, at HELLO, with STALE_EPOCH), every boot-rule row incl. the CHALLENGE flag; `render.json`: every scenario with plane bytes transferred and verified against its frame digest. |
| `tag_core_rules` (16) | 3 AUTH failures skip one window; session timeout (never during a refresh); fragment violation closes silently; record without credit; CTRL after AUTH_OK; DATA before AUTH; malformed plaintext (each ERROR with the stored epoch); corrupt record → STORAGE_ERROR and no-record behaviour; REFRESH_INTENT write failure (no refresh); epoch write failure (ERROR{STORAGE_ERROR} with the epoch kept); panel begin/commit failures; FRAME_ABORT, out-of-order PLANE_DATA, early FRAME_END; disconnect during a refresh completes and persists; the stored ACK of the re-delivery clears `result_pending`, as does any RESULT; advertising flags. |

Result (2026-09-28): 39/39 cases on `native_sim` and on `native_sim/native/64`.

After a protocol change, regenerate the conversations with the host's
reference implementation — in a Cremind checkout next to this one — and
commit the header here; the contract carries it (`tests/conversation.h`), and
Cremind's tests check its generator against the pinned copy:

```bash
python scripts/tags/gen_conversation.py --out ../cremind-tag/tests/ztest/tag_core/src/conversation.h
```

## 12. First-sample bring-up checklist

1. Read the chip: `nrfjprog --readcode` / J-Link `mem` of FICR (variant, flash
   and RAM size) and UICR; note whether the vendor enabled readback
   protection (recovering erases everything, vendor firmware included).
2. Probe the panel connector against the devicetree pins (MOSI, SCK, CS, DC,
   RST, BUSY, BS) and the LED/wake pins; check BS is routed or strapped low.
3. Flash the **debug build** of the matching target (UART log on the debug TX
   pad) plus `CONFIG_THREAD_ANALYZER`; without an enrollment it must log
   `SECURITY_CONFIG` and not advertise (BWR/DK: LED blinks).
4. Enroll with the companion (board and panel ids of this firmware); the tag
   must advertise within a second of reset, then every 30 s ± 3 s for 2 s at
   250 ms (sniffer), with the manufacturer data of §5.1.
5. With a bridge: CAPS read, handshake at the assigned epoch, a wrong-key
   bridge three times (the next window is skipped).
6. Deliver a test layout (a checkerboard with a white border and a black
   top-left marker): check orientation, polarity (and red on the BWR), and
   that the panel does not flash before FRAME_END. Fix orientation in PSR
   (UD/SHL), polarity in the board `plane-flags`.
7. Measure the refresh time (RESULT.refresh_ms) and set `refresh-timeout-ms`
   with margin; check PON/POF complete within 500 ms.
8. Reset the tag during a refresh: the next CHALLENGE reports
   DISPLAY_STATE_UNKNOWN with flag bit 0, the re-delivery refreshes and ACKs.
9. Read the thread analyzer after handshake, a full transfer, a refresh and
   an NVS garbage collection (≥ 8 updates): compare with section 9, especially
   the controller priority RX thread (384 B).
10. Measure VDD against a meter (battery_mv) and the currents of section 7
    with the debugger disconnected and a power-on reset.
11. Record everything in `docs/qualification/<board>.md`, set the board's
    `panel-verified` / `lfxo-verified` / `wake-verified` devicetree properties
    for what was confirmed, and move the board to *functional*.

## 13. Open items

- Only the Hema 52811 panel has run on hardware (§6 SSD1619 bench check). The
  UC8176 sequences, VDD reading, radio/flash coexistence and all stack sizes
  are unverified.
- Sifei 52810: placeholder pins, panel id 255; the firmware links the full
  UC8176 driver (so the fit is honest) but never drives it.
- `CMD{SLEEP}` (System OFF) is implemented for boards with `wake-verified` only;
  none has it.
