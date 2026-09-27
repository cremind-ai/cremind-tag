# Simulated scale and fault test

The plan's acceptance criterion for delivery latency is: *test one gateway, five
bridges and twenty tags, including an intermediate relay and competing radio
traffic; target normal delivery initiation within 60 seconds for at least 95 %
of trials in the qualified topology; report wake, mesh, transfer and refresh
times separately.* Real radio needs hardware (§8 lists the run that remains).
This test exercises the same topology through the companion's
[simulator](simulator.md), driven by the **real** companion daemon
([companion.md](companion.md)) against the stateful fake Cremind of the daemon
tests, with realistic traffic and injected faults, and checks the daemon's
durability invariants on every run.

`tools/sim_scale.py` does the work; `companion/tests/scale` wraps it for pytest.

## 1. Running it

```bash
cd cremind-tag
uv run --project companion python tools/sim_scale.py                        # 200 trials: baseline, then faults
uv run --project companion python tools/sim_scale.py --scenario baseline --trials 50 --seed 7
uv run --project companion python tools/sim_scale.py --json build/scale.json --markdown build/scale.md \
    --daemon-log build/scale.log --keep build/scale-data                    # full report, tables, logs, databases

cd companion
uv run pytest tests/scale -q                            # fast: 20 trials per scenario at time scale 20 (~40 s)
CREMIND_TAG_SCALE_FULL=1 uv run pytest tests/scale -q   # full: 200 trials at time scale 10 (~5 min)
uv run pytest -m "not slow"                             # everything else
```

Both pytest variants fail on any invariant (§3.5); the full one also warns
when the baseline misses the 60 s target, which it does for some seeds (§6.1).

| Option | Default | Meaning |
|---|---|---|
| `--trials` | 200 | content cards Cremind issues (the measured trials); the traffic around them comes on top |
| `--scenario` | `both` | `baseline` (relay + competing traffic, no injected fault), `faults` (the same plus every fault of §3.3), or both |
| `--seed` | 1 | seed of the traffic, the fault schedule, the simulator and the competing-traffic draws |
| `--time-scale` | 10 | simulated seconds per real second (a run of 25 simulated minutes takes 2.5 minutes) |
| `--rate` | 5 | traffic events per simulated minute |
| `--competing` | 1 | competing-traffic level (multiplies the loss rates of §3.2; 0 = off) |
| `--relayed` | 1 | how many of the last bridges sit behind a relay |
| `--adv-window-ms` | protocol (2000) | what-if only: the simulated tags' advertising window (§6.2) |
| `--json`, `--markdown`, `--daemon-log`, `--keep` | – | the full report, the result tables, the daemon's and simulator's INFO log, the run's data directory |

The exit status is 0 when every invariant holds in every scenario and the
baseline meets the target. Everything is seeded: the traffic, the fault
schedule and every random decision of the simulator are reproducible; the
exact interleaving of the event loop is not, so two runs of one seed differ by
a few trials (§6.3).

## 2. What runs

- **Topology.** One gateway, five bridges (`bridge-1` … `bridge-5`, mesh
  addresses 0x0002–0x0006), twenty tags assigned round-robin (four per bridge,
  `bridge-5` serves tags 5, 10, 15 and 20), all 400 × 300 black/white panels
  (UC8176, 4 s refresh), tags waking every 30 s ± 3 s with 2 s advertising
  windows (the protocol constants).
- **The relay.** `bridge-5` reaches the gateway only through `bridge-4`. The
  simulator models no mesh relaying (docs/simulator.md, "not modelled"), so the
  tool wraps `MeshNetwork.send`: every message to or from `bridge-5` takes a
  second hop's latency (15 ms + 12 ms per segment), waits while `bridge-4`'s mesh
  is suspended for a BLE connection (failing after the 3 s lower-transport
  budget, exactly as the simulator treats a suspended destination), and meets
  the competing traffic on both hops. Rebooting `bridge-4` cuts `bridge-5` off.
- **The companion.** The real `DaemonService` with **production defaults**
  (`DaemonSettings()`: events polled every 2 s while jobs arrive, backing off to
  10 s; retries from 5 s to 600 s; the gateway client's 2 s request timeout), a
  hardware and a content credential, the real SQLite queue (`synchronous=FULL`),
  the real composer (ICU, HarfBuzz, the dev font pack) and previews, over the
  simulator's TCP serial endpoint.
- **Cremind.** The fake of the daemon tests (`companion/tests/daemon/fake_cremind.py`,
  set up by their `Rig`), behind a wrapper that adds 40 ms ± 50 % per request
  and injects 5xx bursts.
- **One clock.** All of it runs on one asyncio event loop whose clock runs
  `--time-scale` times faster than real time (`scaled_loop_factory`): the
  simulator's clock, the daemon's and the fake Cremind's clocks read it, so every
  timer — tag wakes, mesh timings, the daemon's polls, back-offs and timeouts —
  keeps its real meaning, and **every duration below is simulated time**. Host
  work (SQLite commits, composition, rendering, crypto) runs at host speed and
  is stretched by the same factor, which makes the results slightly pessimistic;
  the runs at time scales 5 and 20 (§6.3) bound the effect.

## 3. Method

### 3.1 Traffic

Events arrive as a Poisson process (5 per simulated minute), each picking a
random tag:

| Share | Event | Cards |
|---|---|---|
| 30 % | notification | one card (TTL 30–120 min, 10 % of them 3–7 min); 15 % are cancelled in Cremind 3–40 s later |
| 10 % | broadcast | the same notification to 3–6 random tags at once |
| 20 % | question | a `needs_input` card (priority 90), answered by a `resolved` job 1–5 minutes later |
| 12 % | progress run | a `progress` card, 2–5 updates 30–90 s apart (the profile's 300 s cadence holds them), then a `task_outcome` card replacing it |
| 28 % | flurry | 3–6 notifications to one tag within 4 s (coalescing, and the footer: only four cards fit on a screen); 20 % have one card cancelled 5–30 s later |

A **trial** is one content card: a notification, a broadcast copy, a flurry
card, a question, a progress run's first card or its outcome. Progress updates,
answers and cancellations are measured too and reported per traffic class, but
are not trials. A 200-trial run is about 23 simulated minutes of traffic
(≈ 275 deliveries, ≈ 140 screens displayed).

### 3.2 Competing radio traffic (both scenarios)

On every mesh hop each segment of a segmented message is lost with 3 %
probability; the lower transport retransmits it after 300 ms and gives the
message up (a failed `end` callback, which the gateway retries) after five
losses of one segment. An unsegmented message (`LAYOUT_COMMIT`,
`LAYOUT_STATUS`, `RESULT_ACK`, `TAG_SEEN`, ...) is lost with 2 % probability per
hop and the sender cannot tell (a lost commit or status costs the gateway's
10 s status timeout). Each segment gets up to 6 ms of extra latency, and 5 % of
BLE connection attempts fail although the tag advertises.

### 3.3 Faults (the `faults` scenario)

| Fault | Schedule | How |
|---|---|---|
| chunk loss | all run | 5 % of `LAYOUT_CHUNK`s lost at the bridge's access layer (`INCOMPLETE`, re-sent chunks) |
| lost `LAYOUT_STATUS` | every ~150 s | the next status is lost (the gateway repeats the commit, the bridge answers `DUPLICATE`) |
| disconnect mid-transfer | every ~120 s | a tag with work waiting drops the link after 3–70 `PLANE_DATA` records |
| power loss during the refresh | every ~150 s | a tag with work waiting loses power between `REFRESH_INTENT` and `DISPLAYED` (the panel may or may not have changed) |
| bridge reboot | once, at 35 % | `bridge-4` — the relay — while the gateway transfers a layout to it or through it: off the mesh and not scanning for 3 s, RAM state lost |
| gateway reboot | once, at 55 % | new `boot_id`, queue, idempotency slots and retained events lost, the serial connection dropped |
| USB re-enumeration | once, at 75 % | the serial connection dropped, the gateway keeps its state |
| Cremind 5xx burst | twice, at 20 % and 65 % | 45 s during which every connector request answers 503; half of them were applied before the answer was lost (idempotent re-sends) |

### 3.4 Measurements

- **Stages as Cremind sees them**: the time of each receipt (`at`, the
  companion's clock when it committed the stage), per delivery: queued →
  `companion_accepted` → `gateway_received` → `bridge_received` →
  `transferring` → `refreshing` → `displayed`.
- **Ground truth from the simulator**: hooks on the bridges record when a bridge
  starts transferring a frame and when a tag's `RESULT OK` arrives, per revision;
  a delivery's initiation falls back to it when its `transferring` receipt is
  missing (best-effort stage event lost).
- **Tag side**: the `DELIVERY_RESULT` timing of every displayed screen — `wake`
  (layout validated at the bridge → tag connected), `mesh` (the gateway's
  transfer), `suspend` (the protocol §5.2 mesh pause), `transfer` (`FRAME_BEGIN` →
  `RESULT`, which by definition includes the refresh, docs/bridge-firmware.md),
  `refresh` (the tag's), and `ble_transfer` = `transfer` − `refresh`. The fields
  are u16 milliseconds: 65.53 s means "65.5 s or more".
- **Advertising windows**: every 2 s window in which a tag had work waiting at
  its bridge, with what became of it — served, or missed because the bridge was
  in a session with another tag (one session per bridge at a time, held through
  the 4 s refresh), the connection attempt failed, the tag was in its 15 s
  back-off, or the bridge's 6-per-minute suspend limit was reached.
- **Delivery initiation** = queued → `transferring` (the tag started receiving
  a screen that shows the card). The acceptance population is the trials minus
  cards held in a footer by the screen model (counted in "N more updates
  waiting" before any screen showing them was displayed: newer or
  higher-priority cards had the four places) and minus cards that left the card
  set (cancelled, answered, replaced) within 60 s without starting; a card that
  left later counts as a miss, with the time it waited (censored).

### 3.5 Invariants (checked on every run, also by the fast pytest)

| Invariant | Check |
|---|---|
| no accepted unexpired job lost | every delivery the companion acknowledged has a job row; at the end every delivery Cremind still lists as active is unexpired only if it waits in the footer of the tag's displayed screen |
| one terminal outcome per delivery | no delivery was receipted with two different terminal outcomes |
| displayed frame = reference render | every displayed revision's layout, rendered by `render/reference.py` for the tag's panel, has the frame digest the tag reported; every `displayed` receipt carries it; after the drain every simulated panel shows exactly the digest the companion believes displayed |
| footer cards never receipted displayed | a `displayed` receipt names a revision whose `delivery_ids` (not only its footer) contain the delivery |
| receipts monotonic | per delivery, the first arrival of each receipt never moves the stage backwards or follows a terminal outcome (an identical receipt re-sent after a lost answer is allowed) |
| cancels respected | a card cancelled in Cremind is never receipted `displayed` by a screen composed after the cancel reached the companion |
| bridge suspend rate | at most `BRIDGE_MAX_SUSPENDS_PER_MIN` (6) suspensions of any bridge in any rolling minute |
| no busy loops, no runaway retries | while idle: ≤ 45 scheduler passes, ≤ 30 connector requests, ≤ 120 serial frames per simulated minute; under traffic ≤ 200 passes and requests per minute, a peak of ≤ 300 connector requests in any minute (outages included), no revision sent more than 6 times (12 with faults), ≤ 3 (6) `DELIVER_LAYOUT`s per displayed screen |
| nothing logged at ERROR | no daemon or simulator task failed |
| settled | after the traffic stops everything drains (no pending or sent revision, empty outbox, every active card displayed or waiting in a footer) |

## 4. What is and is not modelled

Modelled, with the real code: the serial protocol, credits, retained events and
op-id idempotency; the gateway's queue of four (`BUSY`), one segmented send at a
time, chunk re-sends and commit repeats; the bridges' validation, history,
`SUPERSEDED`, results with retries and acks, the protocol §5.2 scheduler (one attempt or
session at a time, 6 suspends per rolling minute, 15 s per-tag back-off, 1 s
connection attempts); the full GATT session with the real handshake and AES-CCM
records at ≤ 4 records per 40 ms connection event; the tag's wake cycle and
display transaction; the daemon end to end, including SQLite durability, and
Cremind's connector semantics.

Not modelled, and which way it biases the numbers:

| Not modelled | Effect |
|---|---|
| Radio physics: range, collisions, interference, channel maps, supervision timeouts | replaced by the loss/latency model of §3.2; real interference is burstier (correlated losses) — **optimistic** |
| Mesh relaying, TTL, network retransmissions, friend/proxy | the relay is a latency + suspension wrapper; a real relay also forwards other bridges' traffic and has its own queue — **optimistic** for bursts through the relay |
| Radio time shared between the BLE link and the mesh during a session | the simulated mesh keeps full speed while a bridge streams a frame — **optimistic** for mesh latency to a busy bridge |
| Host speed | composition, SQLite and crypto run at host speed ×10 in simulated time (≈ 18 % of one core at time scale 10) — **pessimistic**, by up to a few seconds per delivery |
| Real refresh and transfer times | the simulator's BW refresh is 4 s ± 5 % and a record costs one 40 ms connection event per 4 records; a BWR panel (15 s) holds a bridge four times longer — not tested here |
| Reboot durations | a bridge is off the mesh for 3 s; a gateway reboot and a USB re-enumeration are instant (real USB enumeration takes 1–3 s) |
| Tag clocks | no RTC drift, so wake phases never slide into each other |
| Cremind | the fake connector: no database latency beyond the 40 ms per request, one profile, one content credential |
| Simulator divergences | the bridge ends a tag's jobs on an unauthenticated pre-AUTH `ERROR` at once (protocol §10 now asks for three consecutive sessions); a rebooted bridge keeps pending tag commands (the firmware keeps only layouts). Neither is exercised here |

## 5. Results

All results are from the final code (the fixes of §7 included): 200 trials per
run, time scale 10, the traffic of §3.1 at 5 events per minute. The raw reports
are the JSON and Markdown files `tools/sim_scale.py` writes.

### 5.1 Delivery initiation

| Run | within 60 s | initiation p50 / p95 / p99 (s) | end to end (queued → displayed) p50 / p95 / p99 (s) |
|---|---|---|---|
| **baseline**, seed 1 | **162 / 170 = 95.3 %** (met) | 22.5 / 59.0 / 88.7 | 27.9 / 64.0 / 93.7 |
| baseline, seed 2 | 166 / 176 = 94.3 % | 26.0 / 63.1 / 72.3 | 31.2 / 68.6 / 77.7 |
| baseline, seed 3 | 170 / 184 = 92.4 % | 26.6 / 66.9 / 86.4 | 31.7 / 69.6 / 86.0 |
| baseline, six runs of seeds 1–3 pooled | **995 / 1064 = 93.5 %** (not met) | | |
| **faults**, seed 1 | 143 / 166 = 86.1 % | 29.5 / 88.1 / 107.1 | 38.8 / 95.6 / 113.0 |
| faults, seed 2 | 126 / 167 = 75.4 % | 31.3 / 135.0 / 209.4 | 41.7 / 155.5 / 264.8 |

Initiation from the `transferring` receipt and from the simulator's ground truth
agree within 0.2 s at every percentile; 168 of 170 baseline trials have their
`transferring` receipt (for the other two a best-effort stage event was lost and
the ground truth stands in).

### 5.2 Where the time goes (baseline, seed 1, simulated seconds)

| Stage | meaning | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| queued → companion_accepted | Cremind → companion (events poll: 2 s while active, up to 10 s when idle) | 2.1 | 7.6 | 9.8 | 10.2 |
| companion_accepted → gateway_received | compose + `DELIVER_LAYOUT` accepted (`BUSY` while the gateway's queue of 4 is full) | 0.4 | 2.1 | 8.8 | 8.8 |
| gateway_received → bridge_received | gateway queue + mesh transfer | 3.7 | 11.9 | 24.2 | 24.2 |
| bridge_received → transferring | **waiting for the tag's wake** + the bridge's scheduling | 17.3 | 55.3 | 80.4 | 80.4 |
| transferring → refreshing | BLE frame transfer | 1.1 | 1.4 | 1.4 | 1.7 |
| refreshing → displayed | panel refresh + result back to Cremind | 4.1 | 4.6 | 4.8 | 4.9 |

Tag side, one sample per displayed screen (136 screens in the baseline, 128 with faults):

| Time (s) | baseline p50 | p95 | p99 | faults p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| wake (layout at the bridge → tag connected) | 12.9 | 55.1 | 65.5\* | 14.0 | 53.7 | 65.5\* |
| mesh (gateway → bridge transfer) | 2.1 | 4.8 | 14.6 | 2.2 | 12.2 | 17.9 |
| suspend (mesh pause for the connection) | 0.11 | 0.24 | 0.26 | 0.11 | 0.25 | 0.26 |
| transfer (`FRAME_BEGIN` → `RESULT`, includes the refresh) | 5.1 | 5.3 | 5.5 | 5.1 | 5.3 | 5.4 |
| of which BLE transfer (transfer − refresh) | 1.1 | 1.2 | 1.3 | 1.1 | 1.2 | 1.3 |
| refresh | 4.0 | 4.2 | 4.2 | 4.0 | 4.2 | 4.2 |

\* u16 saturated: two screens per run waited 65.5 s or more.

`mesh` is about 2 s to a direct bridge and 3.9 s through the relay (per-bridge
p50 1.9–2.2 s, `bridge-5` 3.9 s); its p99 of 12–18 s is a lost `LAYOUT_COMMIT`
or `LAYOUT_STATUS` (the 10 s status timeout). Of 174 mesh transfers, 38 (22 %)
carried a screen that a newer one superseded at the bridge before a tag saw it.

### 5.3 Advertising windows and the tail

| Run | windows with work waiting | served | lost: bridge busy with another tag | lost: connection failed | other |
|---|---|---|---|---|---|
| baseline, seed 1 | 161 | 83.9 % | 16 | 9 | 1 (rate limit) |
| baseline, seed 2 | 145 | 86.9 % | 10 | 9 | |
| baseline, seed 3 | 176 | 86.4 % | 18 | 6 | |
| faults, seed 1 | 173 | 75.1 % | 15 | 8 | 20 (session failed: disconnects, power losses) |

Trials over 60 s, by cause (the first window missed while the layout waited at
the bridge, or the stage that alone took over 30 s):

| Run | missed window: bridge busy | missed window: connection failed | gateway queue + mesh > 30 s | Cremind 5xx burst |
|---|---|---|---|---|
| baseline, seeds 1–3 (32 misses of 530) | 23 | 9 | 0 | 0 |
| faults, seed 1 (23 of 166) | 6 | 1 | 3 | 13 |

Per bridge (baseline, seed 1): 34/36, 35/39, 33/34, 30/31 and 30/30 (the relayed
`bridge-5`) within 60 s. No tag starved: every card was displayed, ended in
Cremind, or waits in a footer by design, and in 19 of the 20 runs every tag's p95
stayed within twice the overall p95 (the tool flags a tag above it; the one flag,
in a reference run, was a tag with two trials, one of which missed two windows:
112.8 s). The most suspensions of any bridge in any rolling minute was 6, the
limit.

### 5.4 Faults (seed 1)

| Fault | injected / fired | cards hit: final stage | measured | p50 / max (s) |
|---|---|---|---|---|
| chunk loss (5 %) | 37 chunks lost | — | 35 `INCOMPLETE` answers, 37 chunks re-sent, no failed delivery | |
| lost `LAYOUT_STATUS` | 10 / 10 | — | 20 commits re-sent, 14 answered `DUPLICATE`, no failed delivery | |
| disconnect mid-transfer | 11 / 11 | 17: displayed 10, superseded 3, cancelled 1, refreshing 3 † | fault → that screen (or a newer one) displayed | 36.4 / 154.0 |
| power loss during the refresh | 11 / 9 ‡ | 12: displayed 8, superseded 1, refreshing 3 † | fault → that screen (or a newer one) displayed | 62.4 / 123.0 |
| relay (`bridge-4`) reboot during a transfer | 1 / 1 | 1: displayed | fault → that screen displayed | 29.5 |
| gateway reboot | 1 / 1 | 2 in flight: displayed | fault → displayed | 9.1 / 61.8 |
| USB re-enumeration | 1 / 1 | 8 in flight: displayed | fault → displayed | 3.7 / 48.9 |
| Cremind 5xx, 2 × 45 s | 2 / 2 | 26 queued meanwhile: displayed 20, superseded 6 | queued → displayed | 88.6 / 142.9 |

† A card of the interrupted screen that newer cards then moved into the footer:
its stage stays `refreshing` in Cremind (stages never go back) while it waits
there (§6.5). ‡ Two armed power losses found no refresh of their tag left to hit.

Over the two 45 s outages Cremind answered 34 requests with 503 (12 receipts,
10 events, 10 heartbeats, 2 syncs): one request per back-off per loop.

### 5.5 Invariants

Every invariant of §3.5 held in every run reported here, including the sweep of
§6.3 and the what-if runs of §6.2: no accepted job lost, one terminal outcome
per delivery, every displayed frame equal to the reference render (about 135
revisions re-rendered and 20 panels compared per run), no footer card receipted
displayed, receipts in order (about 1 130 per run), cancels respected, at most
6 suspends per rolling minute, no busy loop (idle: 30 scheduler passes, 8
connector requests and 15 serial frames per minute, 2–10 % of one core; at most
173 connector requests in any minute, in the 10-events-per-minute run), nothing
logged at ERROR, and everything settled within 100 s after the traffic (15–40 s
in most runs).

## 6. Findings

### 6.1 The 60 s target sits at the edge

At 5 events per minute with broadcasts and flurries, the simulated baseline
initiates 90–97 % of trials within 60 s depending on the seed and the run
(93.5 % pooled over six runs of three seeds). Every late trial missed its tag's first advertising window after the
layout reached the bridge, and each miss costs a whole wake period (30 s).
Nothing else contributes in the baseline: Cremind, composition, the gateway and
the mesh together take 6 s at p50 and 17 s at p95.

- **Bridge busy (about 70 % of the misses).** A bridge runs one attempt or
  session at a time and holds it through the refresh (about 5.3 s per BW screen:
  handshake, 1.1 s transfer, 4 s refresh), while a tag advertises for only 2 s.
  A tag's window is lost whenever another tag of the same bridge is being
  served, which is exactly when broadcasts and flurries give several tags of one
  bridge work at once. BWR panels (15 s refresh) would make it about three times
  worse.
- **Connection failed (about 30 %).** A failed attempt (5 % under the competing
  traffic model) starts the 15 s per-tag back-off, which forfeits the rest of
  the 2 s window.

### 6.2 What-if: a longer advertising window

`--adv-window-ms` (a what-if only: it changes the simulated tags, not the
protocol) shows what the advertising window buys:

| Advertising window | seed 1 | seed 2 | seed 3 | pooled | windows served |
|---|---|---|---|---|---|
| 2 s (protocol) | 95.3 % | 94.3 % | 92.4 % | 93.5 % (6 runs) | 84–87 % |
| 4 s | | | 96.7 % | | 87 % |
| 6 s | 99.4 % | 97.2 % | 96.8 % | 97.8 % | 91–92 % |

With 6 s the window outlasts a BW session, the bridge-busy misses nearly vanish
(p95 initiation 43–53 s), and connection failures are what remains. The cost is
three times the tag's advertising radio time (24 instead of 8 advertising events
per wake), to be weighed on hardware (PPK) against the battery budget. Other
options with the same aim, not simulated: retry a failed connection within the
same window (an attempt is at most 1 s, the window 2 s) instead of forfeiting
it; let the bridge release the link after `FRAME_END` and collect the `RESULT`
at the next wake (the tag already flags a pending result); serve two links at a
time.

### 6.3 Sensitivity (baseline, seed 1 unless named)

| Variation | within 60 s | initiation p50 / p95 / p99 (s) |
|---|---|---|
| reference (two runs) | 95.3 %, 96.5 % | 22.5 / 59.0 / 88.7 |
| time scale 5 | 95.4 % | 22.4 / 58.9 / 89.4 |
| time scale 20 | 95.3 % | 22.4 / 59.5 / 87.8 |
| 2 events per minute | 97.7 % | 22.0 / 44.1 / 60.8 |
| 10 events per minute | 92.6 % | 31.0 / 67.9 / 89.8 |
| no competing traffic | 96.5 % | 19.4 / 53.9 / 88.9 |
| no relay | 95.3 % | 21.8 / 59.5 / 88.7 |
| seed 2 (two runs) | 93.3 %, 94.3 % | 26.0 / 63.1 / 72.3 |
| seed 3 (two runs) | 89.7 %, 92.4 % | 26.6 / 66.9 / 86.4 |

The time scale does not move the result (host speed is not what limits it), so
a 2.5-minute run at time scale 10 stands for 25 simulated minutes. Runs of one
seed differ by 1–3 points (event-loop interleavings differ); seeds differ by up
to 6. The relay adds about 1.8 s of mesh time per delivery to its bridge's tags
and made no measurable difference to the fraction.

### 6.4 Faults

The durability and ordering invariants hold through every fault. Recovery costs
one or two wake periods: a disconnect mid-transfer is redrawn at the next wake
(p50 36 s); a power loss during the refresh takes two (the next `CHALLENGE`
reports `DISPLAY_STATE_UNKNOWN`, the companion re-delivers the same revision and
the tag redraws at the wake after: p50 62 s). A 45 s Cremind outage delays what
is queued meanwhile by its length plus the back-off. The gateway faults cost
little: in-flight revisions are re-sent on the new `boot_id`.

### 6.5 Open items (not changed here)

- **A bridge reset loses results waiting for their `RESULT_ACK`** (RAM only,
  docs/bridge-firmware.md §3; the simulator now does the same, §7). The tag shows
  the screen, but Cremind keeps the delivery at `refreshing` until the companion
  re-sends the revision after `result_timeout_s` — **30 minutes** — and the
  bridge answers it from its history. Consider re-sending a bridge's in-flight
  revisions when it reports a new boot (uptime drop in `HEALTH`/`EVT_BRIDGE_INFO`),
  or a much shorter `result_timeout_s` once the screen was refreshing.
- **`wake_ms` is u16 milliseconds** and saturates at 65.5 s; a missed window or a
  tag out of range routinely exceeds it (two screens per run). A u32 or a coarser
  unit would keep the measurement meaningful.
- **Wasted mesh transfers.** 22 % of transfers carry a screen superseded at the
  bridge before a tag sees it: the companion composes as each events page
  arrives and never cancels a superseded revision still queued at the gateway.
  `CANCEL_DELIVERY` for a revision superseded while `sent`, or composing a burst
  once (a short settle delay), would give that time back to the gateway's single
  segmented-send pipeline; it matters under bursts and through relays.
- **Stages of a card moved into the footer.** A card receipted
  `gateway_received` … `refreshing` as part of a screen that was then superseded
  or interrupted, and pushed into the footer by newer cards, keeps that stage in
  Cremind while it waits (stages never move backwards). Cremind could show such
  cards as waiting when the tag's displayed preview lists them as footer-only.
- **Power loss recovery takes two wake periods** by design (protocol §10: the bridge
  reports `DISPLAY_STATE_UNKNOWN`, the companion re-delivers). The bridge still
  holds the job's layout when the `CHALLENGE` reports the unknown state;
  redrawing in that session would save a wake period and a mesh transfer, at the
  cost of Cremind never seeing the uncertainty.
- **Stale rows** in docs/bridge-firmware.md §12 (the simulator no longer answers
  a repeated commit `NOT_FOUND`) and docs/gateway-firmware.md §12 (the simulator
  now enforces one `EVT_RESULT` per `update_id`). The simulator still ends a
  tag's jobs on an unauthenticated pre-AUTH `ERROR` at once, where protocol §10 now asks
  for three consecutive sessions (not exercised here).

## 7. Bugs found and fixed

Each fix has a regression test that fails without it.

| # | Where | Found as | Fix | Test |
|---|---|---|---|---|
| 1 | daemon `store.py` (`due_outbox`, `next_outbox_ts`), `service.py` (`_queue_idle`) | faults run: 34 receipts out of order (`transferring` after `displayed`). A failed receipts POST backed off only its own rows, and newer rows — the terminal outcome — overtook them | receipts rows go out in commit order: a receipts row waiting for its retry holds back the newer receipts rows (other kinds keep their own back-off, so a failing preview cannot stall the receipts) | `tests/daemon/test_store.py::test_receipts_are_posted_in_order_after_a_failed_post` |
| 2 | daemon `outbox.py` | faults run: 740 requests answered 503 in two 45 s outages (a peak of 450 per minute): each queued row, and each preview replaced through its dedupe key (which restarts its attempts), made its own immediate request | after a transient failure the sender pauses for the back-off as a whole (and for `tls_retry_s` after a TLS error): one request per back-off, reset on success | `tests/daemon/test_sync.py::test_the_outbox_backs_off_as_a_whole_while_cremind_fails` |
| 3 | daemon `content.py` | faults run: 504 `POST sync` in the outages. The error path slept with `_sleep()`, which returns at once while a sync is due, and the sync (at start, after a 410, every `resync_s`) is what failed: a tight loop against a failing Cremind, and with an untrusted certificate about 100 requests a second from the start | the back-off after an error, and the TLS pause, are plain sleeps | `tests/daemon/test_sync.py::test_a_failing_sync_is_retried_with_back_off_not_in_a_loop`, `::test_a_tls_error_on_the_first_sync_waits_tls_retry_s` |
| 4 | daemon `screens.py` | baseline run: a revision with 16 attempts and no failure. Every `BUSY` from a full gateway queue counted as an attempt, and `retry_delay(attempts)` sets the back-off of the next link failure (after 15 `BUSY`s: 600 s instead of 5 s) | `BUSY` and `NO_RESOURCES` defer without counting an attempt | `tests/daemon/test_delivery.py::test_busy_back_pressure_is_not_a_failed_attempt` |
| 5 | daemon `store.py` (`apply_stage`) | baseline run: 16 of 171 trials without a `transferring` receipt. A revision superseded locally while the bridge already transferred it had its `TRANSFERRING`/`REFRESHING` events ignored, although its `OK` is recorded as displayed: Cremind saw `bridge_received` jump to `displayed` | stage events of a superseded revision count (cards that left the set meanwhile have an outcome and get no receipt); now 168 of 170 | `tests/daemon/test_store.py::test_stages_of_a_revision_superseded_while_in_flight_are_receipted` |
| 6 | simulator `bridge.py`, `core.py` | a simulated bridge reset kept re-sending the results waiting for their ack (the tasks outlived `reboot()`), unlike the firmware (RAM only) and the simulator's own description — optimistic about bridge resets | `reboot()` cancels the bridge's RAM work (result re-sends, queued mesh sends, resume recovery) except its own task (`TaskSet.cancel_others`) | `tests/sim/test_delivery.py::test_bridge_reset_drops_results_waiting_for_their_ack` |
| 7 | simulator `gateway.py` | a simulated gateway reboot kept its results de-duplication memories (`(bridge, result_seq)` and the reported `update_id`s), which are RAM on the gateway (docs/gateway-firmware.md §6; "empty RAM state" in docs/simulator.md) | cleared on reboot | `tests/sim/test_rules_10.py::test_a_rebooted_gateway_forgets_which_results_it_reported` |

## 8. The hardware test that remains

The simulator replaces the radio, so these numbers become qualification evidence
only once the same topology has run on hardware:

1. **Topology.** The gateway DK, five bridges and twenty tags (four per bridge,
   BW panels; a second run with BWR panels), with `bridge-5` placed out of the
   gateway's range but within `bridge-4`'s (verified as in
   docs/gateway-firmware.md §13 step 2), the daemon on a PC against a throwaway
   Cremind (`tools/e2e_slice.py` sets one up), an nRF sniffer and a PPK.
2. **Competing traffic.** Busy 2.4 GHz surroundings (sustained Wi-Fi traffic on
   channels overlapping the advertising channels, other BLE advertisers), and an
   idle reference run.
3. **Traffic.** The same schedule (`make_traffic` of `tools/sim_scale.py`, same
   seeds) replayed through Cremind's API (pinned notes to
   `/api/tags/devices/{id}/display`, questions, cancels); a small driver that
   posts the schedule remains to be written.
4. **Measure**, from Cremind's delivery records (`stage_times`, `timing`), the
   tables of §5: initiation and end-to-end percentiles, the fraction within 60 s,
   the stage breakdown, wake/mesh/suspend/transfer/refresh per screen; from the
   bridges' `HEALTH` counters (`sessions_ok`/`sessions_fail`, `connect_failed`,
   `rate_limited`, `suspend_max_ms`) and the sniffer, the window service rate and
   its losses (bridge busy versus connection failures), and the relay's added
   mesh time.
5. **What only hardware can show**: real connection-failure and mesh
   segment-loss rates (the model of §3.2 is an assumption), the radio shared
   between a BLE session and the mesh (mesh latency to a bridge that is
   streaming a frame), relay forwarding under bursts, BWR refresh times, USB
   re-enumeration and bridge boot times, tag RTC drift over hours, and the
   current cost of a longer advertising window (§6.2) on the PPK.
6. **Faults** as in §3.3, by hand: power-cycle `bridge-4` during a transfer and
   while a result is being retried (the result loss of §6.5), reset the gateway
   and unplug its USB, pull a tag's battery during a refresh, shield a tag during
   a transfer, stop Cremind for a minute.
7. **Pass**: at least 200 trials per run; at least 95 % initiated within 60 s in
   the baseline at the agreed traffic rate; Cremind's records show no lost or
   doubly finished delivery and every displayed digest matching the render of
   its layout; no bridge's `suspend_count` above 6 in any minute. Record the
   results in the boards' qualification reports next to the simulated figures
   here.
