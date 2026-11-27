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

    rng = random.Random(2026)
    covered = 0
    draws = 400
    for k in range(draws):
        sample = [rng.gauss(10.0, 2.0) for _ in range(50)]
        lo, hi = stats.bca(mean_of(sample), 50, n_boot=1000, seed=k)
        covered += lo <= 10.0 <= hi
    assert 0.93 <= covered / draws <= 0.97


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
    assert {(x.instance_id, x.reason) for x in s.exclusions} == {
        ("astropy__astropy-4", "unrunnable"),
        ("sphinx-doc__sphinx-3", "ungraded"),
    }
    assert s.arms == ("baseline", "token-sparen")
    assert s.seeds == (0,)
    assert s.counts["tasks"] == 2
    assert s.counts["excluded"] == 2
    assert s.counts["infra_fail"] == 2
    assert s.counts["invalidated"] == 1
    assert s.counts["runs"] == 4
    assert s.resolved("token-sparen") == {
        "django__django-1": {0: False},
        "django__django-2": {0: True},
    }
    assert s.costs("baseline")["django__django-2"] == {0: 4.0}
    assert s.resolved_rate("baseline") == 1.0
    assert s.discordance("token-sparen") == (0, 1, 2)
    assert s.mcnemar("token-sparen", seed=0).method == "mcnemar-conditional-exact"
    assert s.cost_ratio("token-sparen", n_boot=200).point == pytest.approx((0.5 + 0.25) / 2)
    assert s.resolved_diff("token-sparen", n_boot=200).point == pytest.approx(-0.5)
    repos = s.per_repo()
    assert set(repos) == {"django/django"}
    assert repos["django/django"]["token-sparen"] == stats.RepoCounts(tasks=2, runs=2, resolved=1)
    assert {r.plugin_hash for r in s.runs} == {"a" * 64}


def test_repo_of_instance_id() -> None:
    assert stats.repo_of("scikit-learn__scikit-learn-12345") == "scikit-learn/scikit-learn"
    assert stats.repo_of("fake-1") == "fake"
