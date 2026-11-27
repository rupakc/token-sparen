"""§10.5 statistics: BCa task-cluster bootstrap, exact McNemar, non-inferiority, summaries."""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from typing import Any

import pytest

from bench import stats
from bench.stats import Estimate


def test_clopper_pearson_known_values() -> None:
    lo, hi = stats.clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-4)
    assert hi == pytest.approx(0.8129, abs=1e-4)
    assert stats.clopper_pearson(0, 10)[0] == 0.0
    assert stats.clopper_pearson(10, 10)[1] == 1.0
    lo, hi = stats.clopper_pearson(0, 10)
    assert hi == pytest.approx(1 - 0.025 ** (1 / 10), abs=1e-9)
    with pytest.raises(ValueError):
        stats.clopper_pearson(3, 2)


def test_identical_arms_ratio_one() -> None:
    rng = random.Random(1)
    runs = {f"t{i}": {0: rng.uniform(0.5, 3.0), 1: rng.uniform(0.5, 3.0)} for i in range(30)}
    e = stats.median_cost_ratio(runs, runs, n_boot=500)
    assert (e.point, e.lo, e.hi) == (1.0, 1.0, 1.0)
    assert e.n == 30
    assert e.dropped == 0


def test_cost_ratio_pairs_by_task_and_drops_missing() -> None:
    base = {f"t{i}": {0: 2.0, 1: 4.0} for i in range(20)}  # task mean 3.0
    arm: dict[str, dict[int, float | None]] = {f"t{i}": {0: 1.5, 1: 1.5} for i in range(20)}
    arm["t0"] = {0: None, 1: None}  # no cost at all → dropped
    arm["t1"] = {0: None, 1: 3.0}  # mean over the seeds that have one
    del arm["t2"]  # missing on one side → dropped
    arm["extra"] = {0: 1.0}  # not in the base → dropped
    e = stats.median_cost_ratio(arm, base, n_boot=500)
    assert e.point == pytest.approx(0.5)
    assert e.n == 18
    assert e.dropped == 3
    assert e.lo <= e.point <= e.hi


def test_bca_reproducible_and_covers_normal_mean() -> None:
    def mean_of(xs: Sequence[float]) -> Any:
        return lambda idx: statistics.fmean(xs[i] for i in idx)

    gen = random.Random(7)
    xs = [gen.gauss(0, 1) for _ in range(50)]
    first = stats.bca(mean_of(xs), len(xs), n_boot=500, seed=3)
    assert first == stats.bca(mean_of(xs), len(xs), n_boot=500, seed=3)
    assert first != stats.bca(mean_of(xs), len(xs), n_boot=500, seed=4)

    # Deterministic: every draw and every resample is seeded (no flake possible). Range: BCa for
    # a normal mean at n = 50 undercovers slightly (z instead of t_49, plug-in variance); a
    # 4000-draw reference run of this setup gives ≈ 0.935 (task-9 report). With 400 draws the
    # Monte-Carlo SD is √(0.935·0.065/400) ≈ 0.012, so [0.90, 0.97] is the reference ± 3 SD.
    rng = random.Random(2026)
    covered = 0
    draws = 400
    for k in range(draws):
        sample = [rng.gauss(10.0, 2.0) for _ in range(50)]
        lo, hi = stats.bca(mean_of(sample), 50, n_boot=1000, seed=k)
        covered += lo <= 10.0 <= hi
    assert 0.90 <= covered / draws <= 0.97


def test_bca_needs_two_clusters() -> None:
    with pytest.raises(ValueError):
        stats.bca(lambda idx: 0.0, 1)


def test_resolved_diff_and_rate() -> None:
    base = {f"t{i}": {0: i % 2 == 0} for i in range(40)}
    arm = {f"t{i}": {0: i % 2 == 0 or i < 8} for i in range(40)}
    e = stats.resolved_diff(arm, base, n_boot=500)
    assert e.point == pytest.approx(4 / 40)
    assert e.n == 40
    assert e.method == "bca-task-cluster"
    assert e.lo <= e.point <= e.hi


def test_cluster_bootstrap_uses_tasks_not_runs() -> None:
    rng = random.Random(5)
    one = {f"t{i}": {0: rng.random() < 0.6} for i in range(60)}
    base_one = {f"t{i}": {0: rng.random() < 0.6} for i in range(60)}
    # three perfectly correlated seeds per task: the same evidence as one seed, so a task-cluster
    # bootstrap gives the identical interval; a run-level bootstrap would shrink it by ~√3
    three = {t: {s: v[0] for s in range(3)} for t, v in one.items()}
    base_three = {t: {s: v[0] for s in range(3)} for t, v in base_one.items()}
    a = stats.resolved_diff(one, base_one, n_boot=800, seed=9)
    b = stats.resolved_diff(three, base_three, n_boot=800, seed=9)
    assert (a.point, a.lo, a.hi, a.n) == pytest.approx((b.point, b.lo, b.hi, b.n))
    assert b.n == 60  # n counts tasks, not runs


def test_mcnemar_symmetric_when_balanced() -> None:
    e = stats.mcnemar_conditional_exact(6, 6, 50)
    assert e.point == 0.0
    assert e.lo == pytest.approx(-e.hi, abs=1e-12)
    assert e.lo < 0 < e.hi
    assert e.method == "mcnemar-conditional-exact"
    lo_p, hi_p = stats.clopper_pearson(6, 12)
    assert e.hi == pytest.approx((12 / 50) * (2 * hi_p - 1))
    skew = stats.mcnemar_conditional_exact(9, 3, 50)
    assert skew.point == pytest.approx(6 / 50)
    assert skew.lo <= skew.point <= skew.hi
    none = stats.mcnemar_conditional_exact(0, 0, 50)
    assert (none.point, none.lo, none.hi) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        stats.mcnemar_conditional_exact(30, 30, 50)


def test_noninferior_boundary() -> None:
    def est(lo: float) -> Estimate:
        return Estimate(point=0.0, lo=lo, hi=0.1, n=100, method="x")

    assert not stats.non_inferior(est(-0.05))
    assert stats.non_inferior(est(-0.0499))
    assert not stats.non_inferior(est(-0.2))
    assert stats.non_inferior(est(-0.09), margin=-0.1)


# --------------------------------------------------------------------------- summarise


def _key(arm: str, seed: int, iid: str, stage: str = "screening") -> str:
    return "/".join(("c1", stage, arm, str(seed), iid))


def _end(
    arm: str, seed: int, iid: str, status: str = "ok", cost: float | None = 1.0
) -> dict[str, Any]:
    return {
        "kind": "end",
        "key": _key(arm, seed, iid),
        "status": status,
        "run_cost": cost,
        "plugin_hash": "a" * 64,
        "model": "m1",
        "claude_code_version": "2.1.0",
        "live": True,
    }


def test_summarise_pairs_counts_and_exclusions() -> None:
    tasks = ["django__django-1", "django__django-2", "sphinx-doc__sphinx-3", "astropy__astropy-4"]
    ledger: list[dict[str, Any]] = [{"kind": "e4-ledger", "schema": 1, "stage": "screening"}]
    grades: dict[str, bool | None] = {}
    for iid in tasks:
        for arm in ("baseline", "token-sparen"):
            ledger.append({"kind": "start", "key": _key(arm, 0, iid)})
            ledger.append(_end(arm, 0, iid, cost=2.0 if arm == "baseline" else 1.0))
            grades[_key(arm, 0, iid)] = arm == "baseline" or iid != "django__django-1"
    # an infra failure then a retry that ends ok: only the final counts
    ledger.insert(3, _end("token-sparen", 0, "django__django-2", status="infra_fail"))
    # astropy: token-sparen never reached a final outcome (unrunnable) → whole task excluded
    ledger = [
        r
        for r in ledger
        if not (r["kind"] == "end" and r["key"] == _key("token-sparen", 0, "astropy__astropy-4"))
    ]
    ledger.append(_end("token-sparen", 0, "astropy__astropy-4", status="infra_fail"))
    # sphinx baseline: graded with a harness error → excluded
    grades[_key("baseline", 0, "sphinx-doc__sphinx-3")] = None
    # an invalidated end reopens its key; a later end replaces it
    ledger.append({"kind": "invalidate", "key": _key("baseline", 0, "django__django-2")})
    ledger.append(_end("baseline", 0, "django__django-2", cost=4.0))
    # another stage's records are ignored
    ledger.append(
        {**_end("baseline", 0, "django__django-1"), "key": _key("baseline", 0, "x", "confirmation")}
    )

    s = stats.summarise(ledger, grades, stage="screening")
    assert s.tasks == ("django__django-1", "django__django-2")
    assert set(s.exclusions) == {
        stats.Exclusion("astropy__astropy-4", "unrunnable", "token-sparen", 0, "unknown"),
        stats.Exclusion("sphinx-doc__sphinx-3", "ungraded", "baseline", 0, "harness_error"),
    }
    assert s.excluded_by_arm == {"baseline": 1, "token-sparen": 1}
    assert s.excluded("token-sparen") == {"astropy__astropy-4"}
    # astropy: token-sparen-only; sphinx: baseline-only → 2 one-sided of 4 tasks, per arm 2/8
    assert s.exclusion_rates("token-sparen") == (pytest.approx(2 / 8), 0.0)
    assert s.arms == ("baseline", "token-sparen")
    assert s.seeds == (0,)
    assert s.counts["tasks"] == 2
    assert s.counts["excluded"] == 2
    assert s.counts["infra_fail"] == 2
    assert s.counts["invalidated"] == 1
    assert s.counts["runs"] == 6  # every graded final run, also of tasks excluded for the other arm
    assert s.resolved("token-sparen") == {
        "django__django-1": {0: False},
        "django__django-2": {0: True},
        "sphinx-doc__sphinx-3": {0: True},
    }
    assert s.costs("baseline")["django__django-2"] == {0: 4.0}
    assert s.resolved_rate("baseline") == 1.0
    assert s.discordance("token-sparen") == (0, 1, 2)
    assert s.mcnemar("token-sparen", seed=0).method == "mcnemar-conditional-exact"
    assert s.cost_ratio("token-sparen", n_boot=200).point == pytest.approx((0.5 + 0.25) / 2)
    assert s.resolved_diff("token-sparen", n_boot=200).point == pytest.approx(-0.5)
    repos = s.per_repo()
    assert set(repos) == {"django/django", "sphinx-doc/sphinx", "astropy/astropy"}
    assert repos["django/django"]["token-sparen"] == stats.RepoCounts(tasks=2, runs=2, resolved=1)
    assert {r.plugin_hash for r in s.runs} == {"a" * 64}


def test_exclusion_cause_from_ledger_sub_and_missing_grade() -> None:
    ledger = [
        {**_end("token-sparen", 0, "a-1", status="infra_fail"), "sub": "load"},
        _end("baseline", 0, "a-1"),
        _end("token-sparen", 0, "a-2"),
    ]
    s = stats.summarise(ledger, {_key("baseline", 0, "a-1"): True}, stage="screening")
    assert set(s.exclusions) == {
        stats.Exclusion("a-1", "unrunnable", "token-sparen", 0, "load"),
        stats.Exclusion("a-2", "ungraded", "token-sparen", 0, "no_grade"),
    }


def test_range_invalidate_requires_at() -> None:
    ledger = [_end("baseline", 0, "a-1"), {"kind": "invalidate", "from": 0.0, "to": 1.0}]
    with pytest.raises(ValueError, match="'at'"):
        stats.summarise(ledger, {}, stage="screening")
    timed = [{**_end("baseline", 0, "a-1"), "at": 0.5}, {"kind": "invalidate", "from": 0, "to": 1}]
    s = stats.summarise(timed, {_key("baseline", 0, "a-1"): True}, stage="screening")
    assert s.runs == ()
    assert s.exclusions[0].reason == "incomplete"


def test_cost_ratio_needs_two_tasks_message() -> None:
    with pytest.raises(ValueError, match="cost in both arms"):
        stats.median_cost_ratio({"a": {0: 1.0}}, {"a": {0: 0.0}})


def test_repo_of_instance_id() -> None:
    assert stats.repo_of("scikit-learn__scikit-learn-12345") == "scikit-learn/scikit-learn"
    assert stats.repo_of("fake-1") == "fake"


# --------------------------------------------------------------------------- gate (ruling M5-T9)


def test_wilson_known_values() -> None:
    # Newcombe (1998, single proportion) example 81/263, Wilson score: 0.2553 to 0.3662
    assert stats.wilson(81, 263) == pytest.approx((0.2553, 0.3662), abs=1e-4)
    assert stats.wilson(0, 10)[0] == 0.0
    assert stats.wilson(10, 10)[1] == pytest.approx(1.0)


def test_newcombe_paired_published_values() -> None:
    # Armitage & Berry (1994, p. 138) tubercle bacilli, Newcombe interval as published by
    # StatsDirect: both 20, first only 12, second only 2, neither 16 → 0.056156 to 0.329207
    e = stats.newcombe_paired(20, 12, 2, 16)
    assert e.point == pytest.approx(10 / 50)
    assert (e.lo, e.hi) == pytest.approx((0.056156, 0.329207), abs=1e-6)
    assert e.method == "newcombe-hybrid-score"
    flipped = stats.newcombe_paired(20, 2, 12, 16)
    assert (flipped.lo, flipped.hi) == pytest.approx((-0.329207, -0.056156), abs=1e-6)
    same = stats.newcombe_paired(30, 0, 0, 20)
    assert same.point == 0.0
    assert same.lo < 0 < same.hi


def test_tango_paired_published_values() -> None:
    # Fagerland, Lydersen & Laake (2014) airway example, cells (++, +-, -+, --) = (1, 1, 7, 12);
    # asymptotic score (Tango) as printed by R ratesci::rdpairci: -0.5172 to -0.0260;
    # the same data give Newcombe/MOVER-NW -0.5069 to -0.0256 there
    e = stats.tango_paired(1, 1, 7, 12)
    assert e.point == pytest.approx(-6 / 21)
    assert (e.lo, e.hi) == pytest.approx((-0.5172, -0.0260), abs=1e-4)
    assert e.method == "tango-score"
    nc = stats.newcombe_paired(1, 1, 7, 12)
    assert (nc.lo, nc.hi) == pytest.approx((-0.5069, -0.0256), abs=1e-4)
    mirrored = stats.tango_paired(12, 7, 1, 1)
    assert (mirrored.lo, mirrored.hi) == pytest.approx((0.0260, 0.5172), abs=1e-4)
    none = stats.tango_paired(40, 0, 0, 40)  # no discordant pair: symmetric around 0
    assert none.lo == pytest.approx(-none.hi)
    assert none.lo < 0
    assert stats.tango_paired(0, 5, 0, 0).hi == 1.0
    with pytest.raises(ValueError):
        stats.tango_paired(0, -1, 0, 3)


def _one_seed(n: int, p: float, rng: random.Random) -> dict[str, dict[int, bool]]:
    return {f"t{i}": {0: rng.random() < p} for i in range(n)}


def test_ni_gate_is_min_of_bootstrap_and_tango_on_seed_zero() -> None:
    rng = random.Random(3)
    arm = _one_seed(80, 0.6, rng)
    base = _one_seed(80, 0.6, rng)
    diffs = [int(arm[t][0]) - int(base[t][0]) for t in sorted(arm)]
    boot_lo, boot_hi = stats.percentile(
        lambda idx: statistics.fmean(diffs[i] for i in idx), 80, n_boot=500, seed=1
    )
    n10, n01 = diffs.count(1), diffs.count(-1)
    n11 = sum(arm[t][0] and base[t][0] for t in arm)
    tango = stats.tango_paired(n11, n10, n01, 80 - n11 - n10 - n01)
    g = stats.ni_gate(arm, base, n_boot=500, seed=1)
    assert g.lo == pytest.approx(min(boot_lo, tango.lo))
    assert g.hi == pytest.approx(max(boot_hi, tango.hi))
    assert g.point == pytest.approx(statistics.fmean(diffs))
    assert g.n == 80
    assert g.method == stats.GATE_METHOD
    assert stats.non_inferior(g) == (g.lo > -0.05)


def test_extra_seeds_cannot_change_the_gate() -> None:
    """Review ruling I1: only the pre-specified seed 0 enters the gate."""
    rng = random.Random(8)
    arm1 = _one_seed(90, 0.6, rng)
    base1 = _one_seed(90, 0.6, rng)
    one = stats.gate_decision(arm1, base1, n_boot=400, seed=2)
    for extra in (1, 2):  # 2- and 3-seed inputs, adversarial extra seeds
        arm = {t: {**v, **dict.fromkeys(range(1, extra + 1), False)} for t, v in arm1.items()}
        base = {t: {**v, **dict.fromkeys(range(1, extra + 1), True)} for t, v in base1.items()}
        many = stats.gate_decision(arm, base, n_boot=400, seed=2)
        assert many == one
        assert stats.resolved_diff(arm, base, n_boot=50).point != one.estimate.point


def test_gate_min_tasks() -> None:
    assert stats.gate_min_tasks() == 73
    assert stats.tango_paired(73, 0, 0, 0).lo > -0.05 >= stats.tango_paired(72, 0, 0, 0).lo
    # identical arms below the minimum cannot pass, at the minimum they can
    same = {f"t{i}": {0: i % 2 == 0} for i in range(72)}
    small = stats.gate_decision(same, same, n_boot=200)
    assert not small.passed
    assert any("cannot pass" in r for r in small.reasons)
    same73 = {f"t{i}": {0: i % 2 == 0} for i in range(73)}
    assert stats.gate_decision(same73, same73, n_boot=200).passed


def test_summary_gate_uses_seed_zero_exclusions_per_arm() -> None:
    ledger: list[dict[str, Any]] = []
    grades: dict[str, bool | None] = {}
    for i in range(100):
        for arm in ("baseline", "token-sparen", "rtk"):
            ledger.append(_end(arm, 0, f"r__r-{i}"))
            grades[_key(arm, 0, f"r__r-{i}")] = True
    # token-sparen unrunnable (after retries) on 4 tasks at seed 0: net excess 4 → imputed
    for i in range(4):
        ledger.append({"kind": "invalidate", "key": _key("token-sparen", 0, f"r__r-{i}")})
        ledger.append({**_end("token-sparen", 0, f"r__r-{i}", status="infra_fail"), "sub": "api"})
    # a competitor's exclusions never cost the confirmatory pair
    for i in range(50, 60):
        ledger.append({"kind": "invalidate", "key": _key("rtk", 0, f"r__r-{i}")})
    s = stats.summarise(ledger, grades, stage="screening")
    pending, final = s.one_sided()
    assert pending == []
    assert [e.cause for e in final] == ["api"] * 4
    g = s.gate(n_boot=300)
    assert (g.excluded_arm, g.excluded_ref, g.excluded_both) == (4, 0, 0)
    assert g.imputed == tuple(f"r__r-{i}" for i in range(4))
    assert g.observed.n == 96
    assert g.estimate.n == 100
    assert g.estimate.point == pytest.approx(-4 / 100)
    assert g.passed == stats.non_inferior(g.estimate)
    assert s.excluded_by_arm == {"rtk": 10, "token-sparen": 4}
    assert s.resolved_diff("token-sparen", n_boot=100).n == 96
