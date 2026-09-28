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
| `--protocol` | 1 | 2: factory-fresh protocol v2 hardware for Cremind Connect ([Protocol v2](#protocol-v2)) |

`--register` writes simulated secrets into the configured secret store (the OS
keyring by default). Use a separate data directory and `CREMIND_TAG_SECRETS_BACKEND=file`
if that is unwanted. It never registers v2 hardware (v2 devices are paired, not
enrolled).

Bridge maintenance ports accept `cremind-tag bridge ... --url socket://127.0.0.1:7778`
(font installation, `FONT_STATUS`, `FLASH_TEST`).

### Faults

| Spec | Effect |
|---|---|
| `chunk-loss=P` | each `LAYOUT_CHUNK` is dropped at the bridge's access layer with probability P (segments still acknowledged → `LAYOUT_STATUS INCOMPLETE`, resend rounds) |
| `drop-chunks=1,3` | drop those chunk indices once each (deterministic) |
| `result-loss=P` | `DELIVERY_RESULT` / `RESULT_ACK` lost with probability P (bridge retries, gateway de-duplicates) |
| `status-loss=N` | the next N `LAYOUT_STATUS` messages are lost (the gateway repeats `LAYOUT_COMMIT` after 10 s; the bridge answers a repeated commit of an accepted transfer `DUPLICATE`) |
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
`SimBridge.history.clear()` (a replaced bridge that lost its delivery history),
`BridgeSpec(sessions=1, quick_retry=False)` (the scheduling of the simulator
before the §5.2 concurrent sessions and quick retry; `tools/sim_scale.py
--sessions/--quick-retry` compares them).

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
from `CAPS_GET`/`HEALTH_GET` (and `EVT_BRIDGE_INFO`; the caps map carries the
bridge's `max_tags` and its own count of assigned tags, `assigned_count`, as the
gateway firmware does); `EVT_TAG_SEEN`.

**Mesh.** Real vendor PDUs; latency grows with the number of lower-transport
segments; a destination whose mesh is suspended receives the message after it
resumes (or the send fails when the window exceeds the retransmission budget);
access-layer loss and failed sends as faults.

**Bridge.** One layout being assembled (a new `xfer_id` replaces it); §3.3
validation in order; `SUPERSEDED` for an older pending layout; `DUPLICATE`
re-sends the stored result for a displayed revision (with its `stored_epoch`
and `flags`); the assignment table (`ASSIGN_SET`/`ASSIGN_DEL`, `STALE_EPOCH`,
`NO_RESOURCES` beyond `max_tags`: `MAX_TAGS_PER_BRIDGE`, 10 for an nRF52832
board, or `BridgeSpec.max_tags`); result re-sends every `MESH_RESULT_RETRY_MS`
up to `MESH_RESULT_RETRIES` times; `DELIVERY_RESULT` carries the tag's
`stored_epoch` and `flags` (bit0 the tag's stored ACK, bit1 escalated, §3.4);
the §5.2 scheduler (rate limit per rolling minute, per-tag back-off with one
quick retry within `TAG_ADV_WINDOW_MS` after `CONNECT_FAILED`, wait for own
mesh sends, suspend → connect → resume, `suspend_ms`) with the firmware's
concurrent sessions: `SimBridge.max_sessions` (`BridgeSpec.sessions`; default
the board's `CONFIG_CTAG_BRIDGE_SESSIONS`, 2 for the nRF52840 bridge the
simulator builds, 1 for an nRF52832 board) sessions at once, initiated one at a
time, a further one only while every open session waits for its tag's
`RESULT` (the refresh), and sessions streaming at once sharing the connection
events (each paces at one record batch per *n* intervals);
`BridgeSpec.quick_retry=False` turns the retry off (counters `quick_retries`,
`quick_retries_connected`, `quick_retries_failed`, `concurrent_sessions`,
`max_links`, `suspended_ms`); the GATT central
side with the real handshake (the transcript binds the CAPS bytes read),
≤ 4 records per connection event and the tag's credits; external flash with two
font-pack slots, the slot directory, `FONT_*` installation and `FLASH_TEST`.

**Tag.** Wake period with uniform jitter, advertising window and legacy
advertising payload (§5.1); CAPS; handshake checks in §5.4 order (`NOT_FOUND`,
`VERSION_MISMATCH`, `STALE_EPOCH`; a malformed `AUTH` or a wrong `mac_b` is
`AUTH_FAILED`; epoch persisted only after AUTH; every `ERROR` carries the
stored epoch; the transcript binds the CAPS bytes it serves); three consecutive
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
- A `LAYOUT_COMMIT` repeated for a transfer the bridge already validated (its
  `LAYOUT_STATUS` was lost) answers `DUPLICATE` for an accepted transfer, the
  first answer otherwise — never `NOT_FOUND`; the gateway treats `DUPLICATE` like
  `OK`. The bridge remembers only its last transfer, in RAM (a reboot forgets it).
- The gateway emits exactly one `EVT_RESULT` per `update_id` (it remembers the
  last 1024); a later result for the same `update_id` — the bridge's OK after the
  gateway already reported `TIMEOUT` — is acknowledged to the bridge and dropped
  (counter `duplicate_update_results`).
- `DUPLICATE` at the bridge: a displayed revision's stored result is re-sent
  under the new `update_id`; a still-pending revision adopts the new `update_id`;
  a revision that ended without being displayed is accepted again.
- A job whose `FRAME_END` was sent but whose `RESULT` was lost ends
  `DISPLAY_STATE_UNKNOWN` when the next `CHALLENGE` reports the unknown state for
  that `(epoch, revision)`; the companion then re-delivers the same revision and
  the tag repeats the refresh.
- After `FRAME_BEGIN` the bridge waits for that record's credit (or the tag's
  immediate `RESULT`) before streaming plane data.
- `AUTH_FAILED`, `STALE_EPOCH`, `VERSION_MISMATCH` and `NOT_FOUND` from a tag
  before `AUTH_OK` (CAPS, a plaintext `ERROR`, a `CHALLENGE` with another
  `proto`, a wrong `mac_t`) are unauthenticated: they back off like a link
  failure until the same status ends 3 consecutive sessions for the tag and
  epoch (another status restarts the count, `AUTH_OK` and a new assignment
  reset it; counters `unauth_statuses`, `unauth_final`); only then do they end
  the tag's jobs of that epoch, flagged `RESULT_FLAG_ESCALATED` with the tag's
  `stored_epoch` (§10). Link-level failures (`DISCONNECTED`, `TIMEOUT`,
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

## Protocol v2

The simulator runs protocol v2 ([connect-setup.md](connect-setup.md)) so a Cremind
Connect worker can be tested end to end without hardware. v1 stays the default
and is unchanged; v2 is a per-world and per-device switch.

```bash
cremind-tag sim run --protocol 2 --bridges 2 --tags 3 --state sim-v2.json --pack path/to/pack.ctfp
```

starts an unowned gateway, unprovisioned unowned bridges and unowned tags, all
as they leave the factory, and prints each bridge's and tag's **setup code** and
QR text (their labels; also under `"setup_codes"` in the state file, which is
written at start). Nothing is assigned: a worker claims the gateway, pairs the
bridges and tags from their codes and assigns the tags itself. In v2 mode
`--bridges` and `--unprovisioned` both add factory-fresh bridges and
`--assign` has no effect.

Programmatic use:

```python
from cremind_tag.sim import BridgeSpec, SimConfig, Simulator, TagSpec
from cremind_tag.sim.harness import make_config

config = SimConfig(seed=7, time_scale=300, protocol=2, fontpack=pack,
                   bridges=[BridgeSpec(provisioned=False)],            # BridgeSpec(protocol=1): a v1 bridge
                   tags=[TagSpec.generate(7, 0, protocol=2)])           # identity + label from the seed
config = make_config(fontpack=pack, tags=2, bridges=1, protocol=2)      # the same, shorter
async with Simulator(config) as sim:
    sim.setup_codes()        # [{role, name, device_id, short_id, owner_state, code, qr}, ...]
    sim.gateway_identity()   # {device_id, owner_state, gen}
    sim.gateway.secure       # the reference SecureDevice (secure.device); .record is the ownership record
```

`SimConfig.protocol = 2` makes the gateway a v2 gateway and every bridge whose
`BridgeSpec.protocol` is `None` a v2 bridge; a tag is v2 when its `TagSpec` is
(`protocol=2`, with its `DeviceKeys`; its `tag_id` is its `short_id` and it has
no enrollment secret, so it cannot be pre-assigned). `BridgeSpec(labelled=False)`
is a bridge that left the factory without a setup secret (`FACTORY_SETUP`
stores one). `tests/sim/v2host.py` is a minimal host-side v2 driver (the
worker's side of every session is `secure.channel.SecureChannel`); the
`tests/sim/test_v2_*.py` scenarios show every flow.

### What is modelled

Every v2 device runs the reference secure endpoint
(`cremind_tag.secure.device.SecureDevice`): the ownership record, single-use
challenges, Noise IK as responder, the grant rules, setup, root and maintenance
proofs, the release stages. The simulator adds the links around it.

**Serial v2 (gateway, bridge maintenance port).** Plaintext answers only for
`HELLO`, `PING`, `IDENTIFY`, `SECURE_OPEN`, `SECURE_DATA` (plus, on an unowned
bridge, its maintenance catalogue); every other plaintext request answers
`AUTH_REQUIRED`. `SECURE_DATA` frames carry one sealed secure message
(`type | flags | request_id | CBOR`) each, in both directions; answers and
events are sealed right before they are written, so the Noise nonces follow the
wire order, and a message queued for a replaced session is dropped. A frame that
does not decrypt (or arrives without a session) drops the session and answers a
plaintext `SECURE_DATA` response `{status: AUTH_REQUIRED}`. `HELLO` and a reopened
port drop the session; after `SECURE_OPEN` the retained events are re-sent inside
the new session.

**Gateway.** The §4.2 access table inside a session: unowned: `INFO`, `PING`,
`STATUS`, `CLAIM`; owned and the pinned controller: everything (v1 catalogue and
v2); owned and another controller: `INFO`, `PING`, `STATUS`, `RECOVER`; anything
else `NOT_OWNER`. `CLAIM`, `RECOVER`, `RELEASE` and `STATUS` on the reference
device; a lost `CLAIM` answer is reconciled from `STATUS`. `RELEASE` wipes the
CDB, the assignments and the retained events, keeps the generation and reboots
the gateway once its answer is out. `PROVISION` of a v2 bridge needs
`static_oob = identity.static_oob(setup secret, device_id)`. `DISCOVER` → mesh
`DISCOVER` → `EVT_DISCOVERED`. Tunnels: `TUNNEL_OPEN` → mesh `TUNNEL_OPEN`;
`TUNNEL_SEND` messages as ≤ 150-byte `TUNNEL_DATA` fragments under the
one-outstanding segmented-send rule; `TUNNEL_UP` fragments reassembled into
`EVT_TUNNEL` `OPEN` (the endpoint's `ident2`), `DATA`, `CLOSED {status}`. Where the
protocol text leaves a choice, the simulated gateway follows the gateway
firmware (`apps/gateway/src/core/gw_secure.c`, `gw_tunnel.c`).

**Bridge.** The mesh UUID is the `device_id`; the unprovisioned beacon carries OOB
information "on box" (`0x0800`); `CAPS2_STATUS` follows `CAPS_STATUS` (the
inventory's and `EVT_BRIDGE_INFO`'s `caps` gain `device_id`, `gen`,
`owner_state`). One tunnel at a time: `tag_id` 0 ends at the bridge's own
secure endpoint (`ident2` first, then `PairKind` messages: `PAIR` stores `mk`,
`REKEY` moves the controller and `mk`, `RELEASE` answers, then the bridge leaves
the mesh, locked); any other `tag_id` is relayed to the tag, which the bridge
connects to at its next advertisement with the §5.2 initiation rules (its own
mesh sends first, the suspend rate limit, a bounded connection attempt), reading
`IDENT` and relaying `PAIR` both ways. Discovery reports v2 tags advertising
setup mode. Maintenance port v2: an unowned or released bridge answers the v1
catalogue and `FACTORY_SETUP` in plaintext; an owned one answers `FONT_*`,
`FLASH_TEST`, `INFO` and `REBOOT` only in a session that passed `MAINT_AUTH`;
`RECOMMISSION` (USB only; owned: `MAINT_AUTH` and a `MAINT` grant; released: no
grant) returns a fresh setup payload and leaves the mesh.

**Tag.** `tag_id = short_id`; advertising `ver` 2 with `SETUP` (unowned,
released) or `OWNED`; `IDENT` with a challenge drawn once per connection; `PAIR`
(fragmented like `CTRL`, ≤ `PAIR_MSG_MAX`) running the reference endpoint over
`Link.TUNNEL`; frame sessions on `K_epoch` v2 from the root (none before a
pairing: a tag shows nothing until a worker pairs and assigns it); `REKEY` and
`RELEASE` stage 1 make every older key fail; `RELEASE` stage 0/1 as the reference;
three wrong setup proofs in a row end the session and skip the next wake window.

### Rules the simulator had to choose (v2)

- **SECURE_DATA framing.** Every sealed frame, host or device, is
  `type SECURE_DATA, flags 0, {data}`, `request_id 0` (the device accepts any outer
  `request_id` from the host and ignores it); the answer to a sealed request is the
  device's own sealed frame (inner flags `RESPONSE`, the inner `request_id`
  echoed), a sealed event has inner flags `EVENT`. The only outer
  `RESPONSE`-flagged `SECURE_DATA` frame is the plaintext failure answer
  `{status: AUTH_REQUIRED}` (no session, or the frame did not decrypt: the session
  is gone). A sealed message shorter than a secure-message header, or one the host
  flags `RESPONSE` or `EVENT`, gets no answer (the session stays).
- **Events** flow only into a session of the pinned controller of an owned
  gateway (another controller's session could read the owner's results); an
  unowned or foreign session gets answers only. After `CLAIM` or `RECOVER` the
  retained events start flowing in that same session. `EVENT_ACK` is sealed.
- `SECURE_OPEN` with a message 1 that does not verify answers `AUTH_FAILED`; any
  previous session is gone either way. Inside a gateway session the bridge and
  tag messages (`PAIR`, `REKEY`, `MAINT_AUTH`, `RECOMMISSION`, `FACTORY_SETUP`) and
  the link messages answer `UNSUPPORTED`, before the access table.
- **Provisioning a bridge** fails with **`SECURITY_CONFIG`** in `EVT_PROVISIONED`
  (`addr` 0) for a missing or wrong `static_oob` and for a bridge that offers no
  static OOB (v1); the bridge stays unprovisioned and keeps beaconing. The static
  OOB derives from the secret the bridge pairs with now: the fresh one a
  `RECOMMISSION` armed, else the label's (also for an owned bridge that a
  `REMOVE_NODE` took out of the mesh). A locked bridge (released, awaiting
  `RECOMMISSION`) or one without a setup secret sends no beacon, and `PROVISION`
  of it ends `NOT_FOUND`.
- **Tunnels at the gateway.** `TUNNEL_OPEN` answers `OK` with the `tunnel` id
  (idempotent by `op_id`, the id remembered) and `EVT_TUNNEL OPEN` follows when the
  endpoint answers; `duration_s` is 1..255 (the mesh `timeout_s` byte); one tunnel
  per bridge and at most `MAX_BRIDGES` (`BUSY`, not remembered). `TUNNEL_SEND`
  takes one message at a time: `OK` once taken, `BUSY` while the previous one is
  still being sent, `NOT_FOUND` for an unknown tunnel, `INVALID` for an empty and
  `TOO_LARGE` for a longer than `TUNNEL_MSG_MAX` message (a message may go before
  the `OPEN` event: the bridge holds it until the tag is connected).
  `TUNNEL_CLOSE` answers `OK` with no event and sends the mesh `TUNNEL_CLOSE` at
  once (a message still in flight is dropped). The gateway ends a tunnel itself
  (`EVT_TUNNEL CLOSED TIMEOUT` and a mesh `TUNNEL_CLOSE`) when a fragment cannot be
  sent or nothing moved for `duration_s` + 5 s; a mesh `TUNNEL_OPEN` that cannot
  be sent is left to that timer.
- **Tunnels at the bridge.** A new tunnel is closed `BUSY` (`TUNNEL_UP` with the
  close bit) while the bridge holds one or a tag session runs, and no tag session
  starts while a tunnel is open (advert decision `busy_tunnel`). Closing statuses: `TIMEOUT` (idle for `timeout_s`),
  `DISCONNECTED` (the tag dropped the link), `OK` (the worker's `PairKind.CLOSE`, a
  `RELEASE`), `INVALID` (a message a tag cannot take), `UNSUPPORTED` (a tag without
  `IDENT`). A mesh `TUNNEL_CLOSE` ends it silently.
- **Endpoint `PairKind.CLOSE`** (up the tunnel, as a `DATA` message): `AUTH_FAILED`
  (a handshake that does not verify), `AUTH_REQUIRED` (a transport message
  without a session or that does not decrypt; the session is gone), `INVALID`
  (malformed), `LOCKED` (a tag's third wrong proof, then it disconnects). The
  tunnel stays open after an endpoint `CLOSE`: the worker may handshake again. A
  worker's `CLOSE` ends the session and the tunnel: the bridge closes it itself
  (`OK`; a tag drops the link), so a `TUNNEL_CLOSE` right behind it may find the
  tunnel gone already (`NOT_FOUND`, harmless).
- **Sessions are per link on a bridge**: its USB port and its tunnel endpoint each
  keep one; the challenge is the device's. `MAINT_AUTH` and `RECOMMISSION` over a
  tunnel answer `NOT_OWNER` (USB only).
- **DISCOVER**: `duration_s` 0 stops a discovery, above `DISCOVER_MAX_S` is
  `INVALID`; `bridge` 0 is every configured bridge (`ACCEPTED` even when there is
  none). The gateway keeps a window per bridge (`duration_s` + 2 s) and drops a
  `DISCOVERED` outside it. A bridge reports a tag at most once per
  `DISCOVERED_MIN_INTERVAL_MS` and never one it has an assignment for; the gateway
  applies the interval again per (bridge, tag).
- **Gateway `RELEASE`**: the CDB, the assignments and the retained events are
  gone before the answer; after it the gateway reboots (new `boot_id`, the port
  re-enumerates, every RAM state ends), as the firmware does. The bridges of the
  old network keep their mesh state: orphans whose mesh messages the gateway
  ignores and whose addresses it skips when provisioning.
- **Bridge maintenance port**: `PING` is always answered; `FACTORY_SETUP` answers
  `LOCKED` once a secret is stored, `NOT_OWNER` on an owned bridge, `INVALID` for a
  secret that is not 10 bytes, and works in plaintext or inside a session.
- **Tags**: a connection is either a frame session or a pairing session, never
  both; the pairing session ends with the connection. A frame session with a tag
  that holds no root fails `AUTH_FAILED` (it counts towards the v1 three-failures
  pause). A tag `RELEASE` keeps the display record and the stored epoch (epochs
  only grow; a new owner's first assignment must exceed it, which `STALE_EPOCH`
  reports).
- `REKEY` and `RELEASE` of a bridge or tag are accepted from any controller whose
  grant is valid (the reference device's rule; recovery rekeys with a new
  controller). The v2 firmware version reported is 0.2.0.

### What is not modelled (v2)

The mesh provisioning protocol itself (the static OOB value is compared, not
run through the ECDH/confirmation exchange); loss or reordering of tunnel
fragments; the firmware's secure-heap and RAM budgets; physical factory reset;
record CRC corruption and the generation floor; the tag's UICR identity blob.
Challenges, fresh setup secrets and Noise ephemerals come from seeded streams
(a simulated session is predictable by design).

## State file

`--state FILE` (or `SimConfig.state_file`) keeps, as JSON: the gateway CDB and
its assignment bookkeeping, each bridge's provisioning, assignment table (with
`K_epoch`) and delivery history, and each tag's NVS (display record and stored
epoch). It is written after provisioning changes and on exit, and read at start.
Font packs are not stored; they are installed again from `--pack`.

For v2 devices each entry also has `"v2": {"keys", "owner"}` (the identity key
and label secret, and the ownership record) and the file has
`"setup_codes"` (the current labels, informational, never read back). A v2 world
writes the file at start, and again after every ownership change. A stored
bridge or tag identity is only adopted by the same device; a stored gateway
identity replaces the seeded one.
