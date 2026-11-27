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
    values = [power.power(0.1, n, 1, n_sim=200, n_boot=200) for n in (80, 160, 320)]
    assert values[0] < values[1] < values[2]
    assert power.power(0.1, 160, 1, n_sim=200, n_boot=200) == values[1]  # seeded → reproducible
    # nothing flaky: every simulated gate passes (Tango with 0 discordant of 80 > -5 pp)
    assert power.power(0.0, 80, 1, **FAST) == 1.0


def test_power_matches_normal_approx() -> None:
    """Brief's check on the bootstrap component; the applied gate needs more (Tango on sparse
    discordant counts is conservative) but stays within 1.5× of the normal approximation."""
    want = _normal_approx_tasks(0.3, 3)
    assert want == pytest.approx(157, abs=1)
    assert power.normal_approx_tasks(0.3, 3) == pytest.approx(want, rel=1e-2)  # z sum 2.8016
    got = power.required_tasks(0.3, 3, pool=1000, n_sim=500, n_boot=400, gate="bootstrap")
    assert got is not None
    assert abs(got - want) <= 0.15 * want
    applied_want = power.normal_approx_tasks(0.1, 1, gate="applied")
    assert applied_want == pytest.approx(157, abs=1)
    applied = power.required_tasks(0.1, 1, pool=1000, n_sim=300, n_boot=300)
    assert applied is not None
    assert applied_want <= applied <= 1.5 * applied_want


def test_power_matches_gate_simulation() -> None:
    """The fast simulated gate agrees with running the public `ni_gate` on simulated data."""
    f, n, s = 0.4, 80, 3
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
        passed += stats.non_inferior(stats.ni_gate(arm, base, n_boot=300, seed=k))
    simulated = power.power(f, n, s, n_sim=600, n_boot=300)
    # MC SDs ≈ 0.04 (150 sims) and 0.02 (600 sims): 0.12 ≈ 2.7 SD of the difference
    assert abs(passed / sims - simulated) <= 0.12


def test_type_one_error_at_margin() -> None:
    """True difference exactly at −5 pp, at the planning floor (80 tasks): the applied gate
    passes at most α/2 = 0.025 plus 3 Monte-Carlo SD (√(0.025·0.975/1000) ≈ 0.005 → 0.040),
    over flaky shares from sparse (0.1: every flaky arm run fails) to all tasks flaky and with
    1 and 3 seeds. Deterministic (seeded)."""
    tolerance = 3 * (0.025 * 0.975 / 1000) ** 0.5
    for f in (0.1, 0.3, 1.0):
        for seeds in (1, 3):
            rate = power.power(
                f, power.MIN_PLAN_TASKS, seeds, true_diff=-0.05, n_sim=1000, n_boot=200
            )
            assert rate <= 0.025 + tolerance, (f, seeds, rate)


def test_small_samples_are_liberal_hence_planning_floor() -> None:
    """Documented reason for MIN_PLAN_TASKS and the Tango term: the bootstrap alone
    over-rejects with few tasks."""
    assert power.MIN_PLAN_TASKS == 80
    tiny = power.power(0.3, 5, 2, true_diff=-0.05, n_sim=300, n_boot=300, gate="bootstrap")
    assert tiny > 0.1
    assert power.power(0.3, 5, 2, true_diff=-0.05, n_sim=300, n_boot=300) < tiny
    assert power.required_tasks(0.0, 1, pool=500, **FAST) == power.MIN_PLAN_TASKS


def test_true_diff_out_of_range() -> None:
    with pytest.raises(ValueError):
        power.power(0.1, 50, 1, true_diff=-0.1)


def test_required_size_none_when_pool_too_small() -> None:
    assert power.required_size(0.5, pool=12, **FAST) is None
    assert power.required_tasks(0.5, 1, pool=12, **FAST) is None
    assert power.required_size(0.5, pool=100, n_sim=100, n_boot=100) is None


def test_required_size_fewest_runs_ties_more_tasks() -> None:
    assert power.required_size(0.0, pool=80, **FAST) == (80, 1)
    assert power.required_size(0.0, pool=79, **FAST) is None
    size = power.required_size(0.1, pool=400, max_seeds=2, n_sim=150, n_boot=150)
    assert size is not None
    tasks, seeds = size
    assert seeds == 1  # the Tango term sees majority outcomes: extra seeds buy little
    assert power.power(0.1, tasks, seeds, n_sim=150, n_boot=150) >= 0.8


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
    p = power.plan(power.FlipModel(f=0.0, q=0.0, n=40, q_hi=0.0, f_plan=0.0), pool=90, **FAST)
    assert p.as_e4() == {
        "required_tasks": 80,
        "seeds": 1,
        "achieved_power": 1.0,
        "verdict": "powered",
    }
    short = power.plan(power.FlipModel(f=0.5, q=0.25, n=40, q_hi=0.3, f_plan=0.6), pool=12, **FAST)
    assert short.verdict == "underpowered"
    assert short.required_tasks is None
    assert short.seeds == 3
    assert 0.0 <= short.achieved_power < 0.8
