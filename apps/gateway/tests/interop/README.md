# Gateway interop test (native_sim + the companion's GatewayClient)

This builds the gateway firmware's serial side for `native_sim` — the real
`src/core/` (serial server, idempotency, retained events, delivery engine,
node bookkeeping), the real gateway loop (`src/gw_thread.c`) and the real
UART glue (`src/uart_io.c`) on a native PTY UART (`zephyr,native-pty-uart`) —
with the Bluetooth Mesh replaced by a small simulated network
(`src/main.c`). `interop.py` then drives it with the companion's unmodified
`cremind_tag.gateway.GatewayClient` over the PTY, exactly as the companion
drives a USB or UART gateway.

## Running

From the repository root on the host (Git Bash on Windows needs
`MSYS_NO_PATHCONV=1`; use the `C:/...` form of the path):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v ctag-build:/build \
  -v "$(pwd):/work" ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 \
  -c 'sh /work/apps/gateway/tests/interop/run.sh'
```

`run.sh` installs `make` (missing from the image) and `cbor2`/`pyserial`,
builds `/build/gw-interop` (`west build --no-sysbuild -b native_sim`), starts
`zephyr.exe` (real-time pacing, `CONFIG_NATIVE_SIM_SLOWDOWN_TO_REAL_TIME`),
reads the PTY path from its `uart connected to pseudotty: /dev/pts/N` line and
runs the scenarios with `PYTHONPATH=/work/companion/src`. Exit status 0 means
every scenario passed; the script prints a Markdown results table.

## The simulated network

| Behaviour | How |
|---|---|
| Bridges `0x0002` ("hall") and `0x0003` | provisioned and configured at start; answer `CAPS_GET`, `HEALTH_GET`, `ASSIGN_SET`/`ASSIGN_DEL`, `TAG_CMD` |
| One unprovisioned device (`c0ff5a…`) | beacons every second (`EVT_UNPROV_BEACON`, RSSI −55); PB-ADV provisioning gives it `0x0004` |
| Radio | every mesh send goes through a 32-deep queue drained after 10 ms; a segmented send reports its end; a full queue refuses the send (`-ENOBUFS`, the gateway retries) |
| Layout transfer | the library's bridge-side assembler (`ctag_layout_asm`: chunk sizes, `SHA-256(layout)[0:16]`); a repeated commit of an accepted transfer answers `DUPLICATE` (protocol §10) |
| Results | `DELIVERY_STAGE` `TRANSFERRING`, `REFRESHING`, then `DELIVERY_RESULT OK` with `digest = SHA-256(layout)[0:8]`, re-sent every 500 ms until `RESULT_ACK` |
| `tag_id 0xDEAD0001` | chunk 1 of its first transfer is lost once → `LAYOUT_STATUS INCOMPLETE` → the gateway resends exactly that chunk |
| `tag_id 0xDEAD0002` | the first `LAYOUT_STATUS OK` is lost and the result held 11 s → the gateway re-commits after 10 s → `DUPLICATE` |
| Configuration client | every step answered (relay state, TTL echoed); a node reset makes the device unprovisioned again |
| `REBOOT` | the core restarts with a new `boot_id` (the PTY stays open, as a UART does); the CDB survives |

## Scenarios

| Scenario | Checks |
|---|---|
| HELLO, INFO, PING, LIST_NODES, GET_INVENTORY | `caps` (role, credits 4, max_frame 4096, max_bridges 5, max_tags 20), fw/build, `boot_id`, counters, node list with names, `EVT_BRIDGE_INFO`, inventory with CAPS, `FONT_STATUS` → `UNSUPPORTED` |
| credits under load | 60 PINGs + 20 INFOs at once: no credit violation, no overrun, no resynchronisation, no timeout |
| idempotent retries (op_id) | the same `op_id` twice (ASSIGN_TAG, DELIVER_LAYOUT): the second answers the remembered status with `detail = DUPLICATE`, no new work, one `EVT_RESULT` |
| retained events | a handler that fails once: the gateway keeps the event (`retained` counter) until the handler succeeds, then the ACK releases it; an event nobody acknowledged is re-sent after the next session's HELLO with the same `seq`, `op_id` and `boot_id` |
| DELIVER_LAYOUT → EVT_RESULT OK | stages `BRIDGE_RECEIVED`, `TRANSFERRING`, `REFRESHING`, one `EVT_RESULT OK` with the layout's digest; the INCOMPLETE path; a 4000-byte layout (27 chunks); 12 deliveries at once with BUSY retried |
| lost LAYOUT_STATUS OK | re-commit after 10 s, `DUPLICATE` treated as OK, exactly one `EVT_RESULT` |
| provisioning | inventory lists the assignments made above; scan → beacon; `PROVISION` → `EVT_PROVISIONED` at `0x0004` with its name; a second `PROVISION` meanwhile → `PROVISIONING_ACTIVE`; `CONFIGURE_NODE` → `EVT_NODE_CONFIGURED OK`; `REMOVE_NODE` → `EVT_NODE_REMOVED OK` |
| REBOOT | answered, then a new boot: the client's next request times out, it re-HELLOs, sees a new `boot_id` and reports `SessionStarted.boot_changed`; the node list survives |

What this does **not** cover: the Zephyr mesh stack itself (provisioning
PDUs, the configuration client's messages, segmentation, `send_cb` timing),
USB CDC ACM enumeration and the nRF UART driver — those are hardware tests
(docs/gateway-firmware.md §13).
