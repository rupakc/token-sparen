"""Power step (ruling M5-B1): flip model from screening, simulated gate power, confirmation size."""

from __future__ import annotations

import random
from typing import Any

import pytest

from bench import power, stats

# small simulation budgets keep the suite fast; the defaults (2000 sims, 1000 resamples) are for
# the controller-run power step
FAST: dict[str, Any] = {"n_sim": 300, "n_boot": 300}


def _normal_approx_tasks(f: float, seeds: int) -> float:
    return 0.5 * f / (seeds * (0.05 / 2.80) ** 2)


def test_power_monotone_in_size() -> None:
    values = [power.power(0.3, n, 2, **FAST) for n in (60, 120, 240)]
    assert values[0] < values[1] < values[2]
    assert power.power(0.3, 120, 2, **FAST) == values[1]  # seeded → reproducible
    assert power.power(0.0, 5, 1, **FAST) == 1.0  # nothing flaky: every simulated gate passes


def test_power_matches_normal_approx() -> None:
    want = _normal_approx_tasks(0.3, 3)
    assert want == pytest.approx(157, abs=1)
    got = power.required_tasks(0.3, 3, pool=1000, n_sim=500, n_boot=400)
    assert got is not None
    assert abs(got - want) <= 0.15 * want


def test_power_matches_gate_simulation() -> None:
    """The fast simulated gate agrees with running the public `resolved_diff` gate on data."""
    f, n, s = 0.4, 60, 2
    rng = random.Random(11)
    sims, passed = 150, 0
    for k in range(sims):
        arm: dict[str, dict[int, bool]] = {}
        base: dict[str, dict[int, bool]] = {}
        for i in range(n):
            if rng.random() < f:
                arm[f"t{i}"] = {j: rng.random() < 0.5 for j in range(s)}
                base[f"t{i}"] = {j: rng.random() < 0.5 for j in range(s)}
            else:
                fixed = rng.random() < 0.5
                arm[f"t{i}"] = dict.fromkeys(range(s), fixed)
                base[f"t{i}"] = dict.fromkeys(range(s), fixed)
        passed += stats.non_inferior(stats.resolved_diff(arm, base, n_boot=300, seed=k))
    simulated = power.power(f, n, s, n_sim=600, n_boot=300)
    assert abs(passed / sims - simulated) <= 0.12


def test_type_one_error_at_margin() -> None:
    """True difference exactly at the margin: the gate passes at most α/2 (+ tolerance)."""
    rate = power.power(0.3, 150, 3, true_diff=-0.05, n_sim=600, n_boot=300)
    assert rate <= 0.025 + 0.025
    # at the planning floor (few clusters: the bootstrap is mildly liberal) still within α + tol
    floor = power.power(0.3, power.MIN_PLAN_TASKS, 1, true_diff=-0.05, n_sim=600, n_boot=300)
    assert floor <= 0.05 + 0.025


def test_small_samples_are_liberal_hence_planning_floor() -> None:
    """Documented reason for MIN_PLAN_TASKS: with very few tasks the BCa gate over-rejects."""
    tiny = power.power(0.3, 5, 2, true_diff=-0.05, n_sim=300, n_boot=300)
    assert tiny > 0.1
    assert power.required_tasks(0.0, 1, pool=500, **FAST) == power.MIN_PLAN_TASKS


def test_true_diff_out_of_range() -> None:
    with pytest.raises(ValueError):
        power.power(0.1, 50, 1, true_diff=-0.1)


def test_required_size_none_when_pool_too_small() -> None:
    assert power.required_size(0.5, pool=12, **FAST) is None
    assert power.required_tasks(0.5, 1, pool=12, **FAST) is None


def test_required_size_fewest_runs_ties_more_tasks() -> None:
    assert power.required_size(0.0, pool=50, **FAST) == (50, 1)
    assert power.required_size(0.0, pool=49, **FAST) is None
    size = power.required_size(0.2, pool=600, max_seeds=2, n_sim=150, n_boot=150)
    assert size is not None
    tasks, seeds = size
    assert seeds in (1, 2)
    assert power.power(0.2, tasks, seeds, n_sim=150, n_boot=150) >= 0.8


def _screen(n10: int, n01: int, n: int) -> stats.Summary:
    ledger: list[dict[str, Any]] = []
    grades: dict[str, bool | None] = {}
    for i in range(n):
        iid = f"repo__repo-{i}"
        ts, base = (True, False) if i < n10 else (False, True) if i < n10 + n01 else (True, True)
        for arm, ok in (("baseline", base), ("token-sparen", ts)):
            key = f"c/screening/{arm}/0/{iid}"
            ledger.append({"kind": "end", "key": key, "status": "ok", "run_cost": 1.0})
            grades[key] = ok
    return stats.summarise(ledger, grades, stage="screening")


def test_flip_model_from_screening() -> None:
    m = power.flip_model(_screen(3, 2, 40))
    assert m.n == 40
    assert m.q == pytest.approx(5 / 40)
    assert m.f == pytest.approx(10 / 40)
    assert m.q_hi == pytest.approx(stats.clopper_pearson(5, 40, alpha=0.4)[1])
    assert m.q_hi > m.q
    assert m.f_plan == pytest.approx(min(1.0, 2 * m.q_hi))
    assert power.flip_model(_screen(20, 20, 40)).f == 1.0


def test_plan_uses_e4_power_fields() -> None:
    p = power.plan(power.FlipModel(f=0.0, q=0.0, n=40, q_hi=0.0, f_plan=0.0), pool=50, **FAST)
    assert p.as_e4() == {
        "required_tasks": 50,
        "seeds": 1,
        "achieved_power": 1.0,
        "verdict": "powered",
    }
    short = power.plan(power.FlipModel(f=0.5, q=0.25, n=40, q_hi=0.3, f_plan=0.6), pool=12, **FAST)
    assert short.verdict == "underpowered"
    assert short.required_tasks is None
    assert short.seeds == 3
    assert 0.0 <= short.achieved_power < 0.8
