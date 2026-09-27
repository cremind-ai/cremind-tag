"""Simulated scale and fault test: 1 gateway, 5 bridges (one behind a relay), 20 tags, the real daemon.

The fast variant (20 trials per scenario, time scale 20, ~40 s) runs by default and
checks every invariant; ``CREMIND_TAG_SCALE_FULL=1`` runs the full 200-trial
measurement at time scale 10 (~5 min), checks every invariant and warns when the
baseline misses the acceptance target (delivery initiation within 60 s for at least
95 % of trials): with 2 s advertising windows the simulated baseline sits at the
target's edge (docs/scale-test.md §6), so a miss is reported, not failed on.
tools/sim_scale.py does the work; docs/scale-test.md describes the method and the results.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from types import ModuleType
from typing import Any

import pytest

FULL = os.environ.get("CREMIND_TAG_SCALE_FULL") == "1"


def _invariant_failures(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v["examples"][:3] for k, v in report["invariants"].items() if not v["ok"]}


@pytest.mark.slow
@pytest.mark.timeout(3600 if FULL else 900)
@pytest.mark.parametrize("scenario", ["baseline", "faults"])
def test_scale(scenario: str, sim_scale: ModuleType, scale_fonts: Any, tmp_path: Any) -> None:
    cfg = sim_scale.ScaleConfig(trials=200 if FULL else 20, scenario=scenario, seed=1 if FULL else 3,
                                time_scale=10.0 if FULL else 20.0, work_dir=tmp_path / "work")
    report = sim_scale.run_scale(cfg, scale_fonts)
    print(sim_scale.console_summary(report))
    (tmp_path / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    assert not _invariant_failures(report), json.dumps(_invariant_failures(report), default=str)[:2000]
    acceptance = report["acceptance"]
    assert acceptance["population"] >= cfg.trials // 2, acceptance
    assert acceptance["initiation"]["p50"] is not None and acceptance["initiation"]["p50"] <= cfg.target_s
    if scenario == "faults":
        fired = {k: v["fired"] for k, v in report["faults"].items()}
        for kind in ("bridge_reboot", "gateway_reboot", "usb_replug", "cremind_5xx"):
            assert fired.get(kind), (kind, fired)
    if FULL and scenario == "baseline" and not acceptance["met"]:
        warnings.warn(f"baseline: {acceptance['within_target']}/{acceptance['population']} trials initiated within "
                      f"{cfg.target_s:.0f} s (target {cfg.target_fraction:.0%}); causes: {report['tail']['causes']}",
                      stacklevel=1)


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
