"""Simulated scale and fault test: 1 gateway, 5 bridges (one behind a relay), 20 tags, the real daemon.

Both variants gate on the INVARIANTS of docs/scale-test.md §3.5 (no job lost, one outcome per delivery,
displayed frames equal to the reference render, ...): a violation is a bug whatever the timing.

- The fast variant (20 trials per scenario, time scale 20, ~40 s) runs by default. Latency is asserted only
  for the baseline, with a bound that ~20 trials can carry: at least ``FAST_FLOOR`` of them initiated within
  60 s. The full runs measure about 99 % (docs/scale-test.md §5); if the true share were even 95 %, 15 to
  30 trials would fall below 70 % with probability < 0.001, so a failure is a regression, not noise. The faults
  scenario asserts no latency: its ~20 trials are dominated by the two injected 45 s Cremind outages.
- ``CREMIND_TAG_SCALE_FULL=1`` runs the full 200-trial measurement at time scale 10 (~5 min per scenario)
  and reports its latency without failing on it (a warning when the baseline misses the acceptance
  target, delivery initiation within 60 s for at least 95 % of trials).

tools/sim_scale.py does the work; docs/scale-test.md describes the method and the results.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import warnings
from types import ModuleType
from typing import Any

import pytest

FULL = os.environ.get("CREMIND_TAG_SCALE_FULL") == "1"
FAST_FLOOR = 0.70
"""Fast baseline: the least share of trials initiated within 60 s (see the module docstring)."""


def _invariant_failures(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v["examples"][:3] for k, v in report["invariants"].items() if not v["ok"]}


def fast_floor_miss_probability(trials: int, share: float, floor: float = FAST_FLOOR) -> float:
    """P(fewer than ``floor`` of ``trials`` within the target) when each is within it with ``share``."""
    below = math.ceil(floor * trials)  # a count below this misses the floor
    return sum(math.comb(trials, k) * share**k * (1 - share) ** (trials - k) for k in range(below))


@pytest.mark.slow
@pytest.mark.timeout(3600 if FULL else 900)
@pytest.mark.parametrize("scenario", ["baseline", "faults"])
def test_scale(scenario: str, sim_scale: ModuleType, scale_fonts: Any, tmp_path: Any) -> None:
    cfg = sim_scale.ScaleConfig(trials=200 if FULL else 20, scenario=scenario, seed=1 if FULL else 3,
                                time_scale=10.0 if FULL else 20.0, work_dir=tmp_path / "work")
    report = sim_scale.run_scale(cfg, scale_fonts)
    print(sim_scale.console_summary(report))
    (tmp_path / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    # The gate: every invariant, in every scenario and variant.
    assert not _invariant_failures(report), json.dumps(_invariant_failures(report), default=str)[:2000]
    acceptance = report["acceptance"]
    assert acceptance["population"] >= cfg.trials // 2, acceptance
    if scenario == "faults":
        fired = {k: v["fired"] for k, v in report["faults"].items()}
        for kind in ("bridge_reboot", "gateway_reboot", "usb_replug", "cremind_5xx"):
            assert fired.get(kind), (kind, fired)
    share = acceptance["fraction_within_target"]
    summary = (f"{scenario}: {acceptance['within_target']}/{acceptance['population']} trials initiated within "
               f"{cfg.target_s:.0f} s (target {cfg.target_fraction:.0%}); causes: {report['tail']['causes']}")
    if FULL:
        if scenario == "baseline" and not acceptance["met"]:
            warnings.warn(summary, stacklevel=1)  # the full run reports its latency, it does not fail on it
    elif scenario == "baseline":
        assert share is not None and share >= FAST_FLOOR, summary


def test_fast_floor_is_not_noise() -> None:
    """The fast baseline's latency floor fails by chance with probability < 0.001 for 15..30 trials even if
    only 95 % of trials were within 60 s (the full runs measure about 99 %), while a real regression (60 %)
    fails it most of the time."""
    for trials in range(15, 31):
        assert fast_floor_miss_probability(trials, 0.95) < 1e-3, trials
        assert fast_floor_miss_probability(trials, 0.60) > 0.7, trials


# -- the tool's own pieces (fast) ------------------------------------------------------------


def test_scaled_loop_runs_timers_faster(sim_scale: ModuleType) -> None:
    async def scenario() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        started, real = loop.time(), time.perf_counter()
        await asyncio.sleep(2.0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.sleep(10.0), 1.0)
        return loop.time() - started, time.perf_counter() - real

    simulated, real = sim_scale.run_scaled(scenario, 20.0)
    assert 2.9 <= simulated < 4.0
    assert real < 1.0  # 3 simulated seconds take ~0.15 s


def test_percentiles_and_windows(sim_scale: ModuleType) -> None:
    values = list(range(1, 101))
    assert sim_scale.percentile(values, 50) == 50
    assert sim_scale.percentile(values, 95) == 95
    assert sim_scale.percentile(values, 99) == 99
    assert sim_scale.percentile([], 50) is None
    assert sim_scale.max_in_window([0, 10_000, 59_999, 60_000, 61_000], 60_000.0) == 4  # [10 000, 70 000)
    assert sim_scale.max_in_window([0, 60_000, 120_000], 60_000.0) == 1  # half-open windows
    assert sim_scale.max_in_window([], 60_000.0) == 0


def test_traffic_and_faults_are_reproducible(sim_scale: ModuleType) -> None:
    cfg = sim_scale.ScaleConfig(trials=60, seed=11, scenario="faults")
    first, second = sim_scale.make_traffic(cfg), sim_scale.make_traffic(cfg)
    assert [(a.t, a.op, a.tag, a.kind, a.title) for a in first] == [(a.t, a.op, a.tag, a.kind, a.title) for a in second]
    assert sum(a.trial for a in first) >= 60
    assert {a.group for a in first} >= {"notification", "needs_input", "progress", "outcome", "resolve"}
    assert all(0 <= a.tag < cfg.tags for a in first)
    faults = sim_scale.make_faults(cfg, first[-1].t)
    kinds = {f.kind for f in faults}
    assert kinds >= {"bridge_reboot", "gateway_reboot", "usb_replug", "cremind_5xx", "disconnect", "power_loss",
                     "status_loss"}
    relay = next(f for f in faults if f.kind == "bridge_reboot")
    assert relay.target == cfg.bridges - 2  # the relay of the last bridge
    assert sim_scale.make_faults(sim_scale.ScaleConfig(trials=60, seed=11), first[-1].t) == []
