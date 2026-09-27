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
CREMIND_TAG_SCALE_FULL=1 uv run pytest tests/scale -q   # full: 200 trials at time scale 10 (~6 min)
uv run pytest -m "not slow"                             # everything else
```

Both pytest variants gate on the **invariants** (§3.5): a violation fails
them whatever the timing. Latency is asserted only by the fast variant and only
for the baseline, with a bound its ~20 trials can carry: at least 70 %
initiated within 60 s (the full runs measure 96–100 %, §5; if the true share
were even 95 %, 15–30 trials would miss 70 % with probability below 0.001, so
a failure is a regression, not noise — `test_fast_floor_is_not_noise` checks
the arithmetic). The faults scenario asserts no latency (its ~20 trials are
dominated by the two injected 45 s Cremind outages), only its invariants and
that every fault fired. The full variant reports its latency without failing
on it (a warning when the baseline misses the target).

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
| `--sessions` | the simulator's (2: the nRF52840 bridge's) | what-if: tag sessions per bridge at once (protocol §5.2; 1 = the scheduling before §6.2) |
| `--quick-retry` / `--no-quick-retry` | the simulator's (on) | what-if: one retry within the tag's window after a failed connection (protocol §5.2) |
| `--json`, `--markdown`, `--daemon-log`, `--keep` | – | the full report, the result tables, the daemon's and simulator's INFO log, the run's data directory |

The exit status is 0 when every invariant holds in every scenario and the
baseline meets the target. Everything is seeded: the traffic, the fault
schedule and every random decision of the simulator are reproducible; the
exact interleaving of the event loop is not, so two runs of one seed differ by
a few trials (§6.3).

## 2. What runs

- **Topology.** One gateway, five bridges (`bridge-1` … `bridge-5`, mesh
  addresses 0x0002–0x0006, nRF52840 bridges: two tag sessions at once and the
  quick retry of protocol §5.2, as the firmware), twenty tags assigned
  round-robin (four per bridge, `bridge-5` serves tags 5, 10, 15 and 20), all
  400 × 300 black/white panels (UC8176, 4 s refresh), tags waking every
  30 s ± 3 s with 2 s advertising windows (the protocol constants).
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
  its bridge, with what became of it — served (possibly by the quick retry of
  a failed connection), or missed because the bridge could not start an
  attempt (`bridge_busy`: another initiation in progress, both session slots
  taken, or an open session streaming; with one session per bridge, any
  session with another tag, held through the 4 s refresh), the connection
  attempts failed, the tag was in its 15 s back-off, or the bridge's
  6-per-minute suspend limit was reached. The tool records why each of the
  tag's advertisements started no attempt.
- **Scheduling and the relay**: per bridge the suspensions (the most in any
  rolling minute, the rate during the traffic, the mesh's suspended share),
  failed connections, quick retries, sessions started beside another one;
  the relay hop's mesh time (`mesh` of the relayed bridge's screens against
  the direct ones) and the messages that waited for the relay's resume.
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
`SUPERSEDED`, results with retries and acks, the protocol §5.2 scheduler (one
initiation at a time, two sessions per bridge with the second initiated only
while the first refreshes, one quick retry after a failed connection, 6
suspends per rolling minute, 15 s per-tag back-off, 1 s connection attempts);
the full GATT session with the real handshake and AES-CCM
records at ≤ 4 records per 40 ms connection event; the tag's wake cycle and
display transaction; the daemon end to end, including SQLite durability, and
Cremind's connector semantics.

Not modelled, and which way it biases the numbers:

| Not modelled | Effect |
|---|---|
| Radio physics: range, collisions, interference, channel maps, supervision timeouts | replaced by the loss/latency model of §3.2; real interference is burstier (correlated losses) — **optimistic** |
| Mesh relaying, TTL, network retransmissions, friend/proxy | the relay is a latency + suspension wrapper; a real relay also forwards other bridges' traffic and has its own queue — **optimistic** for bursts through the relay |
| Radio time shared between the BLE link and the mesh during a session | the simulated mesh keeps full speed while a bridge streams a frame — **optimistic** for mesh latency to a busy bridge |
| Two links on one bridge | two streaming links split the connection events evenly; an initiation beside an idle link costs it nothing; the GATT setup and handshake take no air time — **optimistic** until H19 (bridge-firmware.md §11) measures the controller's scheduling |
| Host speed | composition, SQLite and crypto run at host speed ×10 in simulated time (≈ 18 % of one core at time scale 10) — **pessimistic**, by up to a few seconds per delivery |
| Real refresh and transfer times | the simulator's BW refresh is 4 s ± 5 % and a record costs one 40 ms connection event per 4 records; a BWR panel (15 s) holds a bridge four times longer — not tested here |
| Reboot durations | a bridge is off the mesh for 3 s; a gateway reboot and a USB re-enumeration are instant (real USB enumeration takes 1–3 s) |
| Tag clocks | no RTC drift, so wake phases never slide into each other |
| Cremind | the fake connector: no database latency beyond the 40 ms per request, one profile, one content credential |
| Simulator divergences | the bridge ends a tag's jobs on an unauthenticated pre-AUTH `ERROR` at once (protocol §10 now asks for three consecutive sessions); a rebooted bridge keeps pending tag commands (the firmware keeps only layouts). Neither is exercised here |

## 5. Results

All results are from the final code — the bridge scheduling of protocol §5.2
(two tag sessions per nRF52840 bridge, the second initiated while the first
refreshes; one quick retry of a failed connection inside the tag's window) and
the fixes of §7 — with 200 trials per run, time scale 10, the traffic of §3.1
at 5 events per minute. §6.2 compares the scheduling with the one-session
bridge measured before. The raw reports are the JSON and Markdown files
`tools/sim_scale.py` writes.

### 5.1 Delivery initiation

| Run | within 60 s | initiation p50 / p95 / p99 (s) | end to end (queued → displayed) p50 / p95 / p99 (s) |
|---|---|---|---|
| **baseline**, seed 1 | **169 / 169 = 100 %** (met) | 22.4 / 46.9 / 49.8 | 27.6 / 52.1 / 55.0 |
| baseline, seed 2 | 169 / 176 = 96.0 % (met) | 26.1 / 59.1 / 95.6 | 31.5 / 64.2 / 100.9 |
| baseline, seed 3 | 184 / 187 = 98.4 % (met) | 25.5 / 40.5 / 61.6 | 30.6 / 45.5 / 66.9 |
| baseline, seeds 1–3 pooled | **522 / 532 = 98.1 %** (met) | 24.9 / 47.0 / 86.4 | |
| baseline, six runs of seeds 1–3 pooled (these and the study's, §6.2) | **1057 / 1070 = 98.8 %** (met) | 24.2 / 45.8 / 61.6 | |
| baseline, 2 events per minute, seeds 1–3 | 550 / 551 = 99.8 % | 22.3 / 37.4 / 47.5 | |
| **faults**, seed 1 | 147 / 167 = 88.0 % | 31.4 / 86.4 / 141.9 | 39.6 / 99.6 / 143.5 |
| faults, seed 2 | 133 / 170 = 78.2 % | 31.4 / 116.4 / 150.1 | 39.6 / 124.2 / 155.3 |
| faults, seed 3 | 144 / 181 = 79.6 % | 29.5 / 97.2 / 127.0 | 35.2 / 113.7 / 160.1 |

Every baseline run meets the target, and so does every pooling of them. Runs of
one seed differ by one to three points (event-loop interleavings, §6.4); seed 2
is the hardest (its bursts fill the gateway's queue of four, below).
Initiation from the `transferring` receipt and from the simulator's ground
truth agree within 0.2 s at every percentile; 526 of 532 baseline trials have
their `transferring` receipt (for the others a best-effort stage event was lost
and the ground truth stands in).

### 5.2 Where the time goes (baseline, seed 1, simulated seconds)

| Stage | meaning | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| queued → companion_accepted | Cremind → companion (events poll: 2 s while active, up to 10 s when idle) | 1.9 | 8.3 | 9.1 | 9.6 |
| companion_accepted → gateway_received | compose + `DELIVER_LAYOUT` accepted (`BUSY` while the gateway's queue of 4 is full) | 0.4 | 1.9 | 4.7 | 4.8 |
| gateway_received → bridge_received | gateway queue + mesh transfer | 4.4 | 21.2 | 24.5 | 24.7 |
| bridge_received → transferring | **waiting for the tag's wake** + the bridge's scheduling | 13.7 | 27.0 | 35.6 | 42.2 |
| transferring → refreshing | BLE frame transfer | 1.1 | 1.4 | 1.5 | 1.5 |
| refreshing → displayed | panel refresh + result back to Cremind | 4.1 | 4.5 | 4.6 | 4.9 |

With one session per bridge the wait for the tag took 17.3 / 55.3 / 80.4 s at
p50 / p95 / p99: a missed window cost a whole wake period. Now nearly every
first window after the layout arrives is served, so the wait is the rest of
one wake period (at most about 33 s).

Tag side, one sample per displayed screen (142 screens in the baseline, 132 with faults, seed 1):

| Time (s) | baseline p50 | p95 | p99 | faults p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| wake (layout at the bridge → tag connected) | 11.9 | 26.9 | 29.9 | 12.3 | 28.6 | 43.0 |
| mesh (gateway → bridge transfer) | 2.1 | 5.4 | 12.7 | 2.3 | 12.6 | 16.8 |
| suspend (mesh pause for the connection) | 0.11 | 0.24 | 0.26 | 0.11 | 0.24 | 0.27 |
| transfer (`FRAME_BEGIN` → `RESULT`, includes the refresh) | 5.2 | 5.3 | 5.5 | 5.1 | 5.3 | 5.5 |
| of which BLE transfer (transfer − refresh) | 1.1 | 1.3 | 1.5 | 1.1 | 1.2 | 1.4 |
| refresh | 4.0 | 4.2 | 4.2 | 4.0 | 4.2 | 4.2 |

`wake` no longer saturates its u16 (65.5 s) in any run. `mesh` is about 2 s to
a direct bridge and 3.6–4.3 s through the relay (per-run p50 of the relayed
`bridge-5`; p95 5.6–15 s against 3.2–3.8 s direct): the relay hop adds about
2 s as before, and at most three messages per run waited for the relay's
resume (none timed out). Its p99 of 12–17 s is a lost `LAYOUT_COMMIT` or
`LAYOUT_STATUS` (the 10 s status timeout). Of 175 mesh transfers, 33 (19 %)
carried a screen that a newer one superseded at the bridge before a tag saw it.

### 5.3 Advertising windows, scheduling and the tail

| Run | windows with work waiting | served (of them by a quick retry) | lost: bridge busy | lost: connection failed | other |
|---|---|---|---|---|---|
| baseline, seed 1 | 135 | 98.5 % (6) | 0 | 1 | 1 (rate limit) |
| baseline, seed 2 | 129 | 99.2 % (7) | 1 | 0 | |
| baseline, seed 3 | 164 | 96.3 % (3) | 1 | 2 | 3 (rate limit) |
| faults, seed 1 | 152 | 88.8 % (6) | 1 | 1 | 15 (session failed: disconnects, power losses) |

| Run | suspensions (most in a rolling minute) | busiest bridge, per minute of traffic | mesh suspended (busiest bridge) | failed connections | quick retries (connected) | sessions beside another |
|---|---|---|---|---|---|---|
| baseline, seed 1 | 151 (6) | 2.0 | 0.4 % | 9 | 8 (7) | 11 |
| baseline, seed 2 | 140 (6) | 1.5 | 0.3 % | 8 | 8 (8) | 15 |
| baseline, seed 3 | 178 (6) | 2.0 | 0.5 % | 10 | 7 (5) | 26 |

No bridge exceeded 6 suspensions in any rolling minute (the limit is reached in
bursts; a quick retry counts like any attempt). Trials over 60 s, by cause (the
first window missed while the layout waited at the bridge, or the stage that
alone took over 30 s):

| Run | missed window: bridge busy | missed window: connection failed | missed window: rate limit | gateway queue + mesh > 30 s | Cremind 5xx burst |
|---|---|---|---|---|---|
| baseline, seeds 1–3 (10 misses of 532) | 0 | 2 | 1 | 7 | 0 |
| faults, seeds 1–3 (94 of 518) | 0 | 5 | 0 (2 session failures) | 60 | 27 |

The bridge's scheduling is no longer where the baseline's tail comes from: its
misses are now the gateway's queue and the mesh (seed 2's bursts keep the
gateway's queue of four full; the companion composes each events page as it
arrives, §6.5), plus a few failed connections whose retry failed too. Per
bridge (baseline, seed 1): 39/39, 36/36, 36/36, 30/30 and 28/28 (the relayed
`bridge-5`) within 60 s. No tag starved: every card was displayed, ended in
Cremind, or waits in a footer by design.

### 5.4 Faults (seed 1)

| Fault | injected / fired | cards hit: final stage | measured | p50 / max (s) |
|---|---|---|---|---|
| chunk loss (5 %) | 37 chunks lost | — | 37 `INCOMPLETE` answers, 37 chunks re-sent, no failed delivery | |
| lost `LAYOUT_STATUS` | 10 / 10 | — | 19 commits re-sent, 10 answered `DUPLICATE`, no failed delivery | |
| disconnect mid-transfer | 11 / 9 ‡ | 14: displayed 11, superseded 3 | fault → that screen (or a newer one) displayed | 36.0 / 91.5 |
| power loss during the refresh | 11 / 8 ‡ | 8: displayed 6, superseded 2 | fault → that screen (or a newer one) displayed | 62.0 / 93.1 |
| relay (`bridge-4`) reboot during a transfer | 1 / 1 | 1: displayed | fault → that screen displayed | 25.7 |
| gateway reboot | 1 / 1 | 2 in flight: displayed | fault → displayed | 9.8 / 60.3 |
| USB re-enumeration | 1 / 1 | 13 in flight: displayed 11, superseded 2 | fault → displayed | 8.1 / 30.4 |
| Cremind 5xx, 2 × 45 s | 2 / 2 | 27 queued meanwhile: displayed 20, superseded 7 | queued → displayed | 91.2 / 147.4 |

‡ Armed tag faults that found no transfer or refresh of their tag left to hit
before the traffic ended. A card of an interrupted screen that newer cards then
move into the footer keeps its stage (`refreshing`) in Cremind while it waits
there (§6.5).

Over the two 45 s outages Cremind answered 34 requests with 503: one request
per back-off per loop.

### 5.5 Invariants

Every invariant of §3.5 held in every run reported here, including the sweep of
§6.3 and every run of the §6.2 study (24 baseline runs of four policies, 12
faults runs of two): no accepted job lost, one terminal outcome
per delivery, every displayed frame equal to the reference render (about 135
revisions re-rendered and 20 panels compared per run), no footer card receipted
displayed, receipts in order (about 1 130 per run), cancels respected, at most
6 suspends per rolling minute, no busy loop (idle: 30 scheduler passes, 8
connector requests and 15 serial frames per minute, 2–10 % of one core; at most
173 connector requests in any minute, in the 10-events-per-minute run), nothing
logged at ERROR, and everything settled within 100 s after the traffic (15–40 s
in most runs).

## 6. Findings

### 6.1 The 60 s target: from the edge to met

With one tag session per bridge (the scheduling before this change) the
simulated baseline initiated 90–97 % of trials within 60 s depending on the
seed and the run (93.5 % pooled over six runs of three seeds; 92.7 % in the
study below). Every late trial missed its tag's first advertising window after
the layout reached the bridge, and each miss cost a whole wake period (30 s):

- **Bridge busy (about 70 % of the misses).** The bridge ran one attempt or
  session at a time and held it through the refresh (about 5.3 s per BW screen:
  handshake, 1.1 s transfer, 4 s refresh), while a tag advertises for only 2 s.
  A tag's window was lost whenever another tag of the same bridge was being
  served, which is exactly when broadcasts and flurries give several tags of
  one bridge work at once. BWR panels (15 s refresh) would make it about three
  times worse.
- **Connection failed (about 30 %).** A failed attempt (5 % under the competing
  traffic model) started the 15 s per-tag back-off, which forfeited the rest of
  the 2 s window.

Both are bridge-side rules and cost the battery-powered tag nothing to change
(§6.2): with two sessions per nRF52840 bridge and a quick retry, the baseline
initiates 98.1–99.4 % of trials within 60 s pooled per set of three seeds
(98.8 % over six runs, every run at least 96 %), p95 45.8 s. What remains of
the tail is the gateway leg (§5.3, §6.5).

### 6.2 The bridge's scheduling (study)

`--sessions` and `--quick-retry` (what-if knobs of the simulator's bridge, now
defaulting to the firmware's rules) compare four policies, each over seeds
1–3 at 2 and 5 events per minute (200 trials per run, time scale 10, the
baseline scenario, pooled per policy and rate):

- **base** — one session per bridge, a failed connection backs off 15 s (the
  scheduling before);
- **(a) quick retry** — after `CONNECT_FAILED`, one immediate retry if the
  same tag's advertisement is seen again within its window (2 s), still
  counted by the 6-per-minute suspend limit;
- **(b) two sessions** — while one tag refreshes (its link idle), the bridge
  may initiate a second tag: one initiation at a time, each in its own mesh
  suspend window, links streaming at once sharing the connection events;
- **(c) = (a) + (b)** — adopted: the nRF52840 bridge's firmware and the
  simulator's default (the nRF52832 bridge, with RAM for one session, runs
  (a)).

| Policy | events/min | per seed (1 / 2 / 3) | **within 60 s, pooled** | initiation p50 / p95 / p99 (s) | windows served | most suspends in a rolling minute (busiest bridge's rate) | quick retries (connected) | sessions beside another | relay `mesh` p50 (direct) | trials over 60 s, by cause |
|---|---|---|---|---|---|---|---|---|---|---|
| base | 2 | 95.5 / 95.7 / 98.9 % | 531 / 549 = 96.7 % | 23.4 / 54.5 / 73.8 | 91–92 % | 5 (1.1/min) | – | – | 3.3–4.3 s (2.0–2.1) | window lost to a busy bridge 8, connection failed 9, gateway + mesh 1 |
| (a) | 2 | 98.3 / 99.5 / 96.8 % | 539 / 549 = 98.2 % | 23.4 / 44.3 / 65.5 | 95–99 % | 6 (1.0/min) | 26 (25) | – | 3.5–4.1 s (2.0–2.1) | busy 5, gateway + mesh 5 |
| (b) | 2 | 99.4 / 96.8 / 97.8 % | 538 / 549 = 98.0 % | 24.1 / 47.9 / 73.0 | 94–95 % | 5 (1.1/min) | – | 25 | 3.4–4.0 s (2.0–2.1) | connection failed 10, gateway + mesh 1 |
| **(c)** | 2 | 100 / 99.5 / 100 % | **550 / 551 = 99.8 %** | 22.3 / 37.4 / 47.5 | 99–100 % | 6 (1.0/min) | 26 (25) | 28 | 3.4–4.0 s (2.0) | gateway + mesh 1 |
| base | 5 | 95.9 / 88.1 / 94.1 % | 494 / 533 = 92.7 % | 27.1 / 67.2 / 103.0 | 85–88 % | 6 (1.8/min) | – | – | 3.4–4.3 s (2.1) | busy 25, connection failed 8, gateway + mesh 6 |
| (a) | 5 | 96.4 / 92.8 / 94.0 % | 502 / 532 = 94.4 % | 25.6 / 68.7 / 102.1 | 87–92 % | 6 (1.9/min) | 22 (20) | – | 3.5–4.5 s (2.1–2.2) | busy 26, connection failed 3, rate limit 1 |
| (b) | 5 | 98.9 / 93.7 / 94.0 % | 506 / 530 = 95.5 % | 26.0 / 58.5 / 73.9 | 93–95 % | 6 (1.9/min) | – | 39 | 3.3–4.4 s (2.1–2.2) | connection failed 12, gateway + mesh 11, busy 1 |
| **(c)** | 5 | 100 / 98.9 / 99.5 % | **535 / 538 = 99.4 %** | 23.9 / 44.9 / 56.2 | 96–99 % | 6 (2.1/min) | 22 (21) | 52 | 3.8–4.1 s (2.0–2.1) | rate limit 2, gateway + mesh 1 |
| (c), the final runs of §5 | 5 | 100 / 96.0 / 98.4 % | 522 / 532 = 98.1 % | 24.9 / 47.0 / 86.4 | 96–99 % | 6 (2.0/min) | 23 (20) | 52 | 3.6–4.2 s (2.0–2.2) | gateway + mesh 7, connection failed 2, rate limit 1 |

Each rule removes the misses it targets and nothing else: the quick retry
removes nearly all connection-failure misses (of 25–30 failed connections per
set of three runs, 22–26 got their retry inside the window and 20–25 of those
connected), two sessions remove nearly all bridge-busy ones,
and only both together clear the target with room at 5 events per minute
(pooled 99.4 % and 98.1 % in two sets of runs; 98.8 % over the six). At
5 events per minute alone (a) and (b) reach 94.4 % and 95.5 %: each leaves the
other half of the tail.

What it costs:

- **The mesh.** The number of suspensions is set by the attempts, not the
  sessions: every policy stays within the limit of 6 in any rolling minute
  (the invariant held in all 24 runs) and at about 1–2 per bridge per minute
  of traffic; the mesh is suspended for 0.2–0.5 % of the time on the busiest
  bridge under every policy (suspend windows of 0.1–0.3 s). A
  quick retry is one more attempt (about 8 per run) and counts toward the
  limit like any other; at 5 events per minute the limit then defers an
  attempt now and then (`rate_limited`: 0–38 skipped advertisements per run,
  at most two late trials per set).
- **The relay hop.** Messages to or through the relay wait while its mesh is
  suspended: at most 5 per set of three runs did, none timed out, and the
  relayed bridge's `mesh` time (per-run p50 3.3–4.5 s against 2.0–2.2 s
  direct) is the same under every policy.
- **The tag.** Nothing: the tag's advertising window, wake period and
  handshake are unchanged (the alternative below triples its advertising).
- **The bridge.** A second session on the nRF52840 costs 9.7 KiB of RAM
  (147,670 B stay free, bridge-firmware.md §9); the nRF52832 cannot pay it (one
  session, quick retry only: policy (a), 94.4 % at 5 and 98.2 % at 2 events
  per minute in an all-nRF52832 topology).

Under faults (the scenario of §3.3, seeds 1–6) the scheduling helps as well:
845 of 1,037 → 912 of 1,046 trials within 60 s pooled (81.5 % → 87.2 %), the
wait for the tag's wake at p95 from 30–69 s to 27–30 s per run. Its tail is
now the two 45 s Cremind outages and the gateway leg: after an outage or the
gateway reboot a burst of revisions meets the gateway's single
segmented-send pipeline (a queue of four, `BUSY` beyond), where every lost
`LAYOUT_COMMIT` or `LAYOUT_STATUS` stalls it 10 s, and how those losses fall
around the bursts decides a run: seeds 2 and 3 came out about 3 points lower
than with one session, seeds 1 and 4–6 6 to 16 points higher (§6.5).

**The alternative: a longer advertising window.** `--adv-window-ms` (a
what-if on the simulated tags, measured with the one-session scheduling)
bought the same by making the window outlast a BW session:

| Advertising window | seed 1 | seed 2 | seed 3 | pooled | windows served |
|---|---|---|---|---|---|
| 2 s (protocol) | 95.3 % | 94.3 % | 92.4 % | 93.5 % (6 runs) | 84–87 % |
| 4 s | | | 96.7 % | | 87 % |
| 6 s | 99.4 % | 97.2 % | 96.8 % | 97.8 % | 91–92 % |

At three times the tag's advertising radio time (24 instead of 8 advertising
events per wake), to be weighed on hardware against the battery budget; the
bridge-side rules reach more (98.8 %) at no cost to the tag. Not simulated:
letting the bridge release the link after `FRAME_END` and collect the `RESULT`
at the next wake (the tag already flags a pending result).

### 6.3 Sensitivity (baseline, seed 1 unless named; the one-session scheduling)

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
and made no measurable difference to the fraction. These runs predate the
scheduling of §6.2 and are kept for the effects they isolate; the study's
parallel runs (up to eight at once on a 22-core host) agree with them.

### 6.4 Faults

The durability and ordering invariants hold through every fault, under both
schedulings (twelve faults runs of §6.2). Recovery costs one or two wake
periods: a disconnect mid-transfer is redrawn at the next wake (p50 36 s); a
power loss during the refresh takes two (the next `CHALLENGE` reports
`DISPLAY_STATE_UNKNOWN`, the companion re-delivers the same revision and the
tag redraws at the wake after: p50 62 s). A 45 s Cremind outage delays what is
queued meanwhile by its length plus the back-off. The gateway faults cost
little by themselves (in-flight revisions are re-sent on the new `boot_id`),
but the burst they and the outages release is what the faults scenario's tail
is made of (§6.2, §6.5).

### 6.5 Open items (not changed here)

- **A bridge reset loses results waiting for their `RESULT_ACK`** (RAM only,
  docs/bridge-firmware.md §3; the simulator now does the same, §7). The tag shows
  the screen, but Cremind keeps the delivery at `refreshing` until the companion
  re-sends the revision after `result_timeout_s` — **30 minutes** — and the
  bridge answers it from its history. Consider re-sending a bridge's in-flight
  revisions when it reports a new boot (uptime drop in `HEALTH`/`EVT_BRIDGE_INFO`),
  or a much shorter `result_timeout_s` once the screen was refreshing.
- **`wake_ms` is u16 milliseconds** and saturates at 65.5 s; with the scheduling
  of §6.2 no baseline screen reaches it, but a tag out of range or a failing
  session still does. A u32 or a coarser unit would keep the measurement
  meaningful.
- **The gateway leg is now the tail** (§5.3, §6.2). The gateway sends one
  segmented transfer at a time with a queue of four; a burst (a flurry, a
  broadcast, the release after a Cremind outage or a gateway reboot) waits
  behind it, and every lost `LAYOUT_COMMIT` or `LAYOUT_STATUS` stalls the
  pipeline for the 10 s status timeout. It decides the remaining baseline
  misses (7 of 10 over seeds 1–3) and most of the faults scenario's. Levers:
  the next item; a shorter status timeout for an unsegmented message the
  bridge answers within milliseconds; sending to a different bridge while one
  waits for its status.
- **Wasted mesh transfers.** 15–22 % of transfers carry a screen superseded at
  the bridge before a tag sees it (19 % in the final seed-1 baseline): the
  companion composes as each events page arrives and never cancels a
  superseded revision still queued at the gateway. `CANCEL_DELIVERY` for a
  revision superseded while `sent`, or composing a burst once (a short settle
  delay), would give that time back to the gateway's single segmented-send
  pipeline; it matters under bursts and through relays.
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
  now enforces one `EVT_RESULT` per `update_id`). The simulator still ended a
  tag's jobs on an unauthenticated pre-AUTH `ERROR` at once, where protocol §10 now asks
  for three consecutive sessions (not exercised here). *Since fixed by the
  protocol v1 finalisation (2026-09-28): the simulator counts 3 consecutive
  sessions (`tests/sim/test_tag_report.py`) and both §12 tables were updated.*

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

1. **Topology.** The gateway DK, five nRF52840 bridges (two tag sessions each;
   a second run with nRF52832 bridges, one session) and twenty tags (four per
   bridge, BW panels; a third run with BWR panels), with `bridge-5` placed out of the
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
   bridges' `HEALTH` and INFO counters (`sessions_ok`/`sessions_fail`,
   `connect_failed`, `quick_retries`, `concurrent_sessions`, `rate_limited`,
   `suspend_max_ms`) and the sniffer, the window service rate and its losses
   (bridge busy versus connection failures), and the relay's added mesh time.
5. **What only hardware can show**: real connection-failure and mesh
   segment-loss rates (the model of §3.2 is an assumption), the radio shared
   between a BLE session and the mesh (mesh latency to a bridge that is
   streaming a frame), relay forwarding under bursts, BWR refresh times, USB
   re-enumeration and bridge boot times, tag RTC drift over hours, and the
   current cost of a longer advertising window (§6.2) on the PPK; and the
   scheduling of §6.2 on the real controller: a second initiation beside a
   link whose tag refreshes, two links streaming beside the mesh, and how
   often a failed connection's retry connects (bridge-firmware.md §11
   H19–H20; the simulator treats an idle link as free and splits streaming
   links evenly).
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
