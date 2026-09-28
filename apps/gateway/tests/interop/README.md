# Gateway interop test (native_sim + the companion's GatewayClient)

This builds the gateway firmware's serial side for `native_sim` — the real
`src/core/` (serial server, idempotency, retained events, delivery engine,
node bookkeeping), the real gateway loop (`src/gw_thread.c`) and the real
UART glue (`src/uart_io.c`) on a native PTY UART (`zephyr,native-pty-uart`) —
with the Bluetooth Mesh replaced by a small simulated network
(`src/main.c`). `interop.py` then drives it with the companion's unmodified
`cremind_tag.gateway.GatewayClient` over the PTY, exactly as the companion
drives a USB or UART gateway.

A second build with `v2.conf` (`CONFIG_CTAG_GW_SECURE`, protocol v2,
docs/connect-setup.md) adds `src/core/gw_secure.c`, `gw_tunnel.c` and
`lib/secure`; `interop_v2.py` drives it as a Connect worker would, with the
companion's reference `cremind_tag.secure` modules (`SecureChannel`, grants,
identity) and a small serial client of its own (the companion's
`GatewayClient` speaks v1 and is not modified). See "Protocol v2" below.

## Running

From the repository root on the host (Git Bash on Windows needs
`MSYS_NO_PATHCONV=1`; use the `C:/...` form of the path):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v ncs-v3.4.1:/ncs -v ctag-build:/build \
  -v "$(pwd):/work" ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.1 \
  -c 'sh /work/apps/gateway/tests/interop/run.sh'
```

`run.sh` installs `make` (missing from the image) and
`cbor2`/`pyserial`/`cryptography`, builds `/build/gw-interop` (`west build
--no-sysbuild -b native_sim`) and `/build/gw-interop-v2`
(`-DEXTRA_CONF_FILE=v2.conf`), starts each `zephyr.exe` (real-time pacing,
`CONFIG_NATIVE_SIM_SLOWDOWN_TO_REAL_TIME`), reads the PTY path from its `uart
connected to pseudotty: /dev/pts/N` line and runs the scenarios with
`PYTHONPATH=/work/companion/src`. `CTAG_INTEROP=v1` or `v2` (`docker run -e`)
runs one variant only. Exit status 0 means every scenario passed; each script
prints a Markdown results table.

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
| `DELIVERY_RESULT.stored_epoch`, `flags` | every result reports the tag's stored epoch = the delivery's epoch; `tag_id 0xDEAD0003`'s result is the tag's stored ACK (`flags` bit0), which `EVT_RESULT` must carry to the companion |
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

## Protocol v2 (`v2.conf`, `interop_v2.py`)

The same network, plus:

| Behaviour | How |
|---|---|
| Gateway identity and records | a fixed identity key (`gw_ik`, the same bytes in `interop_v2.py`); the ownership record and the generation floor stay in RAM across simulated reboots; `RELEASE` empties the network (the next boot has no nodes) |
| The unprovisioned device | a v2 bridge: UUID = its `device_id`, setup secret `"NEWBIE-SEC"`; PB-ADV posts `GW_EVT_PROV_AUTH` (capabilities with static OOB) and adds the node only with `static_oob = HKDF(secret, "cremind-tag/v2/mesh-oob", device_id)`; any other value closes the link without the node |
| `CAPS_GET` | `CAPS_STATUS` and then `CAPS2_STATUS` (`device_id`, `gen`, `owner_state`) |
| `DISCOVER` | every bridge answers two `DISCOVERED` for tag `0x13572468` (`flags` SETUP): the gateway rate-limits the second |
| Bridge `0x0002`'s secure endpoint | `lib/secure` in role BRIDGE (factory secret `"SIM-BRIDGE"`) behind `TUNNEL_OPEN tag_id 0`: its `ident2` goes up first, then `kind | body` messages (Noise handshake, sealed transport, close) in `TUNNEL_UP` fragments; a tunnel to a tag closes `NOT_FOUND`, any other `BUSY` |

| Scenario | Checks |
|---|---|
| plaintext layer | HELLO; IDENTIFY (proto 2, role, `ik`, the `device_id` of connect-setup.md §2.1, UNOWNED gen 0, no `authority_id`, a fresh challenge each time); INFO, LIST_NODES, REBOOT, STATUS in plaintext → `AUTH_REQUIRED`; PING answered; SECURE_DATA without a session → plaintext `AUTH_REQUIRED`; SECURE_OPEN with garbage → `AUTH_FAILED` |
| SECURE_OPEN + CLAIM | Noise IK with worker A; unowned: LIST_NODES, GET_COUNTERS, RECOVER → `NOT_OWNER`, INFO OK; a grant for another controller → `GRANT_INVALID`; CLAIM → gen 1; the same grant again → `GRANT_INVALID` (single-use challenge); STATUS: OWNED, `controller_match`, `owner`, `authority_id`; LIST_NODES |
| access table | worker B: STATUS without `owner`; INFO OK; LIST_NODES, GET_INVENTORY, SCAN_UNPROV, CLAIM → `NOT_OWNER`; RECOVER → gen 2, B pinned; A → `NOT_OWNER` |
| sealed answers and events | DELIVER_LAYOUT → sealed `EVT_RESULT OK` (digest), EVENT_ACK; the v2 counters in INFO |
| DISCOVER | one `EVT_DISCOVERED` per bridge (0x0002, 0x0003), duplicates rate-limited (counters), 121 s → `INVALID` |
| tunnel + PAIR | TUNNEL_OPEN → tunnel id (the same `op_id` → `DUPLICATE`, a second tunnel → `BUSY`); `EVT_TUNNEL OPEN` carries the bridge's `ident2`; Noise IK through the tunnel (`Link.TUNNEL` prologue); STATUS; PAIR with the setup proof → gen 1 and a `proof_d` that checks; MAINT_AUTH through the tunnel → `NOT_OWNER`; TUNNEL_CLOSE; a tunnel to 0x0003 → `EVT_TUNNEL CLOSED BUSY` |
| PROVISION with static OOB | no `static_oob` → `INVALID`; a wrong one → `EVT_PROVISIONED SECURITY_CONFIG`, addr 0; the derived one → `0x0004` |
| decrypt failure | a tampered SECURE_DATA → plaintext `AUTH_REQUIRED`, the session ends (a well-sealed one is refused too); a new session works; `decrypt_failures` 1 |
| RELEASE | → gen + 1, reboot, UNOWNED at that generation; CLAIM again on an empty network |

What this does **not** cover: the Zephyr mesh stack itself (provisioning
PDUs, the static OOB exchange, the configuration client's messages,
segmentation, `send_cb` timing), USB CDC ACM enumeration, the nRF UART driver,
the factory-reset button and settings storage — those are hardware tests
(docs/gateway-firmware.md §13).
