# Simulator

`cremind-tag sim` runs a gateway, a mesh of bridges and battery tags in one
process. The gateway speaks the real serial protocol (docs/protocol.md §1) on a
TCP socket, so the companion — CLI, daemon, tests — talks to it exactly as it
talks to a USB gateway, through pyserial's `socket://` URL. Every message on
every hop uses the real codecs: serial frames and CBOR, mesh vendor PDUs packed
with the generated `protocol.msgs`, GATT fragments at `ATT_VALUE_MAX`, the
handshake, AES-CCM records and the tag transaction decisions of
`protocol.tag_txn`. Bridges render with `render/reference.py`, so the frame
digest a simulated delivery reports is the digest a real bridge must produce.

The simulator is a development and test tool, not a model of radio physics: it
is useful for protocol behaviour, durability and failure handling, and says
nothing about range, timing margins or power.

## Running it

```bash
cremind-tag sim run --pack path/to/pack.ctfp --bridges 2 --tags 3 --time-scale 20
# gateway: socket://127.0.0.1:7777 — point the companion at it:
export CREMIND_TAG_GATEWAY_URL=socket://127.0.0.1:7777      # PowerShell: $env:CREMIND_TAG_GATEWAY_URL=...
cremind-tag gateway info
cremind-tag mesh nodes
```

| Option | Default | Meaning |
|---|---|---|
| `--port`, `--host` | 7777, 127.0.0.1 | gateway listen address |
| `--bridges` | 1 | provisioned and configured bridges (addresses from 0x0002) |
| `--unprovisioned` | 0 | extra bridges that beacon during `mesh scan` and can be provisioned |
| `--tags` | 2 | tags (ids and secrets derived from the seed) |
| `--panel` | `uc8176_420_bw` | panel of every simulated tag (`uc8176_420_bwr` for two planes) |
| `--pack` | config `hardware.fontpack` | font pack installed on every bridge; without one every delivery ends `FONTPACK_MISMATCH` |
| `--time-scale` | 10 | simulated seconds per real second (a 30 s wake takes 3 s at 10) |
| `--seed` | 1 | seed of every random choice (ids, secrets, nonces, jitter, fault draws) |
| `--fault SPEC` | – | fault injection, repeatable (below) |
| `--maint-port-base` | `--port` + 1 | first bridge maintenance port (bridge *i* listens on base + *i*) |
| `--state FILE` | – | JSON state: the gateway CDB, bridge assignments and delivery history, tag NVS |
| `--assign/--no-assign` | assign | assign tags round-robin to the bridges at epoch 1 |
| `--register` | off | add the simulated gateway, bridges and tags (with their secrets) to the local inventory and secret store, so `tag assign`, `tag command` and the daemon work against the simulator |

`--register` writes simulated secrets into the configured secret store (the OS
keyring by default). Use a separate data directory and `CREMIND_TAG_SECRETS_BACKEND=file`
if that is unwanted.

Bridge maintenance ports accept `cremind-tag bridge ... --url socket://127.0.0.1:7778`
(font installation, `FONT_STATUS`, `FLASH_TEST`).

### Faults

| Spec | Effect |
|---|---|
| `chunk-loss=P` | each `LAYOUT_CHUNK` is dropped at the bridge's access layer with probability P (segments still acknowledged → `LAYOUT_STATUS INCOMPLETE`, resend rounds) |
| `drop-chunks=1,3` | drop those chunk indices once each (deterministic) |
| `result-loss=P` | `DELIVERY_RESULT` / `RESULT_ACK` lost with probability P (bridge retries, gateway de-duplicates) |
| `send-fail=P` | a mesh send's `end` callback reports failure (gateway retries 3 times, then `TIMEOUT`) |
| `suspend-fail=P` | `bt_mesh_suspend()` fails (`MESH_SUSPEND_FAILED`, per-tag back-off) |
| `resume-fail=N` | the next N `bt_mesh_resume()` calls fail (session aborted, recovery, reboot after 5 s) |
| `connect-fail=P` | a connection attempt fails although the tag advertises (`CONNECT_FAILED`) |
| `power-loss=TAG[:N]` | the next N refreshes of TAG lose power between `REFRESH_INTENT` and `DISPLAYED` (the panel may or may not have changed) |
| `disconnect=TAG@R` | TAG drops the link after R `PLANE_DATA` records (once) |
| `auth-fail=TAG[:N]` | the next N AUTH checks on TAG fail as if `mac_b` were wrong |
| `refresh-timeout=TAG[:N]` | the next N refreshes of TAG never release BUSY (`REFRESH_TIMEOUT`) |

`TAG` is the 8-hex-digit tag id. Probabilities are drawn from the component's
seeded stream.

## Programmatic use

```python
from cremind_tag.sim import Simulator, SimConfig, BridgeSpec, TagSpec, Assign
from cremind_tag.sim.harness import SimHarness, make_config, run_scenario

# Full control:
tag = TagSpec.generate(seed=7, index=0)                     # deterministic id + secret
config = SimConfig(seed=7, time_scale=200, fontpack=pack_bytes,
                   bridges=[BridgeSpec()], tags=[tag], assignments=[Assign(tag.tag_id, bridge=0, epoch=1)])
async with Simulator(config) as sim:
    url = sim.gateway_url                                   # socket://127.0.0.1:<ephemeral>
    sim.tag(tag.tag_id).faults.power_loss = 1               # arm a fault at any time
    sim.gateway.pause_deliveries()                          # fill the queue -> BUSY
    sim.tag(tag.tag_id).out_of_range = True                 # keep work pending at the bridge

# Simulator + connected GatewayClient + an ACK-gating event recorder:
async def scenario():
    async with SimHarness(fontpack=pack_bytes, tags=2, seed=3) as h:
        ack = await h.deliver(h.tag_ids[0], layout, revision=1)
        result = await h.wait_result(ack.update_id)         # ResultEvent
run_scenario(scenario(), timeout=60)
```

`SimulatorThread(config)` runs the simulator on its own event loop in a
background thread (`with SimulatorThread(cfg) as t: t.gateway_url`), for
blocking callers and CLI tests. Test hooks: `DeviceEndpoint.drop_responses`
(lose the next N serial answers), `SimTag.out_of_range`, `SimGateway.pause_deliveries()`,
`SimBridge.history.clear()` (a replaced bridge that lost its delivery history).

## What is modelled

**Serial link (gateway and maintenance ports).** COBS framing and the §1.1
checks with their counters; the device answers nothing on a connection before a
HELLO. Credits: `caps.credits` receive buffers, `overruns` when a request
arrives with all buffers busy, `credit_violations` when the host sent without
credit; grants ride on the next frame. HELLO resets the session and every
retained event is re-sent after its answer. Retained events carry `seq` from 1
per boot in a ring of `SERIAL_EVENT_RETAIN` (`events_dropped` on overflow);
non-retained events that cannot be sent are dropped (`events_discarded`).
`REBOOT` answers, then drops the connection (like USB re-enumeration) and comes
back with a new `boot_id` and empty RAM state.

**Gateway.** Idempotency slots for the last `SERIAL_IDEMPOTENCY_SLOTS` op ids;
the CDB (persisted in the state file); provisioning of unprovisioned bridges
(`SCAN_UNPROV` beacons, `PROVISION`, `CONFIGURE_NODE`, `REMOVE_NODE`,
`LIST_NODES`, `MAX_BRIDGES`); a bounded delivery queue (`BUSY`); §3.2: one
outstanding segmented send gateway-wide, 3 retries of a failed send, 150-byte
chunks, `INCOMPLETE` answered by resending exactly the missing chunks (3 rounds),
commit re-sent after 10 s without `LAYOUT_STATUS`; results de-duplicated by
`(bridge, result_seq)` and always acknowledged; `CANCEL_DELIVERY`; `GET_INVENTORY`
from `CAPS_GET`/`HEALTH_GET` (and `EVT_BRIDGE_INFO`); `EVT_TAG_SEEN`.

**Mesh.** Real vendor PDUs; latency grows with the number of lower-transport
segments; a destination whose mesh is suspended receives the message after it
resumes (or the send fails when the window exceeds the retransmission budget);
access-layer loss and failed sends as faults.

**Bridge.** One layout being assembled (a new `xfer_id` replaces it); §3.3
validation in order; `SUPERSEDED` for an older pending layout; `DUPLICATE`
re-sends the stored result for a displayed revision; the assignment table
(`ASSIGN_SET`/`ASSIGN_DEL`, `STALE_EPOCH`, `MAX_TAGS_PER_BRIDGE`); result
re-sends every `MESH_RESULT_RETRY_MS` up to `MESH_RESULT_RETRIES` times; the §5.2
scheduler (rate limit per rolling minute, per-tag back-off, wait for own mesh
sends, suspend → connect → resume, `suspend_ms`); the GATT central side with the
real handshake, ≤ 4 records per connection event and the tag's credits;
external flash with two font-pack slots, the slot directory, `FONT_*`
installation and `FLASH_TEST`.

**Tag.** Wake period with uniform jitter, advertising window and legacy
advertising payload (§5.1); CAPS; handshake checks (`STALE_EPOCH`,
`AUTH_FAILED`, epoch persisted only after AUTH); three consecutive
authentication failures skip the next window; credits (two buffers); the
FRAME_BEGIN table; offset/order checks and the incremental digest; the §6
transaction (`REFRESH_INTENT` → refresh → `DISPLAYED` → `RESULT`); boot recovery
to `DISPLAY_STATE_UNKNOWN`; `CMD CLEAR`; a panel-specific refresh time (BW 4 s,
BWR 15 s) and a linear battery drain.

## Rules the simulator had to choose

The protocol text leaves these open; the simulator implements them and the
firmware should do the same unless the protocol document says otherwise.

- A repeated `op_id` returns the remembered status with `detail = DUPLICATE`
  (§1.4); transient refusals (`BUSY`, `NO_RESOURCES`, `PROVISIONING_ACTIVE`) are
  not remembered, so a retry with the same `op_id` can succeed.
- A `LAYOUT_STATUS` other than `OK`, `INCOMPLETE` or `DUPLICATE` ends the
  delivery: the gateway emits its `EVT_RESULT` with that status (zero digest).
- `DUPLICATE` at the bridge: a displayed revision's stored result is re-sent
  under the new `update_id`; a still-pending revision adopts the new `update_id`;
  a revision that ended without being displayed is accepted again.
- A job whose `FRAME_END` was sent but whose `RESULT` was lost ends
  `DISPLAY_STATE_UNKNOWN` when the next `CHALLENGE` reports the unknown state for
  that `(epoch, revision)`; the companion then re-delivers the same revision and
  the tag repeats the refresh.
- After `FRAME_BEGIN` the bridge waits for that record's credit (or the tag's
  immediate `RESULT`) before streaming plane data.
- `AUTH_FAILED`, `STALE_EPOCH`, `VERSION_MISMATCH` and `NOT_FOUND` from a tag end
  the tag's jobs of that epoch; link-level failures (`DISCONNECTED`, `TIMEOUT`,
  `CONNECT_FAILED`, `MESH_*`) are retried after the per-tag back-off and never
  produce a result on their own.
- A successful `CLEAR` resets the bridge's history for the tag to revision 0,
  mirroring the tag, so a later delivery of the previously shown revision is
  drawn again rather than answered from history.
- `TAG_COMMAND IDENTIFY`/`REFRESH` answer `UNSUPPORTED` from the tag: they are
  companion-level (a new revision, §5.6); `cremind-tag tag command identify` does that.
- `ASSIGN_DEL` of an absent assignment answers `OK` (idempotent).
- `FLASH_TEST` reports `BUSY` for a test offset inside the active slot or the
  directory (not tested); the directory sits at the start of the working space
  (docs/fontpack.md §3).
- The bridge keeps counting `result_seq` across reboots (persisted), so the
  gateway's `(bridge, result_seq)` de-duplication never swallows a new result.

## What is not modelled

- Radio: propagation, range, collisions, interference, channel maps, PHY,
  connection-event timing beyond the 4-records pacing, supervision timeouts, ATT
  MTU negotiation, long-read timing. Every bridge hears every tag with a fixed
  per-pair RSSI.
- Mesh internals: relaying, TTL, network/IV index, sequence numbers, replay
  protection, segmentation and reassembly details, friend/proxy/LPN,
  provisioning PDUs and keys, configuration-client messages (provisioning and
  configuration are timed state changes).
- Hardware: USB/UART throughput and electrical behaviour, flash timing and wear,
  the panel controller and SPI, real refresh and power figures, the tag's RTC
  drift, memory limits.
- Time: simulated durations shorter than the host's timer period are stretched
  to it, and rendering and cryptography run at host speed, so timings reported in
  results (`transfer_ms`, `suspend_ms`, ...) are indicative only, and less so at
  high time scales. On Windows the simulator requests 1 ms timer resolution while
  it runs. Decisions are reproducible for a seed; exact interleavings are not.
- Security: nonces come from seeded streams, so a simulated session is
  predictable by design.

## State file

`--state FILE` (or `SimConfig.state_file`) keeps, as JSON: the gateway CDB and
its assignment bookkeeping, each bridge's provisioning, assignment table (with
`K_epoch`) and delivery history, and each tag's NVS (display record and stored
epoch). It is written after provisioning changes and on exit, and read at start.
Font packs are not stored; they are installed again from `--pack`.
