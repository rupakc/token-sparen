"""Power step (ruling M5-B1): size the confirmation from the screening discordance.

Flip model: a share ``f`` of tasks is "flaky" (resolved with p = 0.5 in each arm,
independently); every other task is deterministic (same outcome in both arms). A flaky pair is
discordant half the time, so the screening discordant share q (seed 0) gives f = min(1, 2q).
Planning uses the conservative one-sided 80 % Clopper–Pearson upper bound of q.

`power` simulates the confirmatory gate actually applied (`bench.stats.gate_decision`: one
pre-specified seed per task, min of the task-cluster percentile bootstrap and the Tango score
lower bound > margin) under a true difference of 0 (or ``true_diff``). Review ruling I1: extra
seeds never enter the gate, so confirmation plans with 1 seed. `plan` inflates the task count
by the screening exclusion rate (review ruling I2). Field names of `PowerPlan.as_e4` are the
``power`` object of ``bench/e4.schema.json``.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any, Literal

from bench.stats import (
    GATE_SEED,
    MARGIN,
    Summary,
    clopper_pearson,
    gate_bound,
    gate_min_tasks,
    quantile,
)

PLANNING_CONFIDENCE = 0.8
TARGET_POWER = 0.8
MAX_SEEDS = 1  # review ruling I1: the gate uses one seed; more seeds buy no gate power
MIN_TASKS = 2
# Smallest confirmation the planner proposes, in paired tasks after exclusions (ruling M5-T9).
# The bootstrap alone is liberal with few tasks (type-I at the margin ≈ 0.47 at 5 tasks), and
# the applied gate cannot pass at all below `bench.stats.gate_min_tasks()` = 73 paired tasks
# (Tango lower bound with zero discordant pairs is −0.0500 at 73, −0.0493 at 74).
MIN_PLAN_TASKS = 80

PowerVerdict = Literal["powered", "underpowered"]
Gate = Literal["applied", "bootstrap"]


@dataclass(frozen=True)
class FlipModel:
    f: float  # share of flaky tasks (point estimate)
    q: float  # discordant share of token-sparen vs baseline pairs in screening (seed 0)
    n: int  # screening pairs
    q_hi: float  # one-sided 80 % Clopper–Pearson upper bound of q
    f_plan: float  # min(1, 2 q_hi): the conservative f used for planning
    exclusion_rate: float = 0.0  # screening share of tasks excluded for either arm (seed 0)


def flip_model(
    screen: Summary,
    *,
    arm: str = "token-sparen",
    vs: str | None = None,
    confidence: float = PLANNING_CONFIDENCE,
) -> FlipModel:
    """From the screening pairs at `GATE_SEED` only (tasks are the clusters; other seeds would
    not be independent pairs)."""
    n10, n01, n = screen.discordance(arm, vs, seed=GATE_SEED)
    if n == 0:
        raise ValueError(f"no paired {arm} runs at seed {GATE_SEED} in the screening summary")
    m = n10 + n01
    q = m / n
    # the upper limit of a two-sided (1 − 2(1 − c)) interval is the one-sided c upper bound
    q_hi = clopper_pearson(m, n, alpha=2 * (1 - confidence))[1]
    return FlipModel(
        f=min(1.0, 2 * q),
        q=q,
        n=n,
        q_hi=q_hi,
        f_plan=min(1.0, 2 * q_hi),
        exclusion_rate=screen.exclusion_rate(arm, vs),
    )


def _arm_p(f: float, true_diff: float) -> float:
    if true_diff == 0:
        return 0.5
    if f <= 0 or abs(true_diff) > 0.5 * f:
        raise ValueError(f"|true_diff| must be <= f/2 (only flaky tasks differ), got {true_diff}")
    return 0.5 + true_diff / f


def power(
    f: float,
    n_tasks: int,
    seeds: int = 1,
    *,
    margin: float = MARGIN,
    alpha: float = 0.05,
    n_sim: int = 2000,
    seed: int = 0,
    n_boot: int = 1000,
    true_diff: float = 0.0,
    gate: Gate = "applied",
) -> float:
    """Share of simulated confirmations that pass the gate (lower bound > ``margin``).

    ``gate="applied"`` is `bench.stats.ni_gate`: min(task-cluster percentile bootstrap, Tango
    score) on the gate seed; ``"bootstrap"`` is the percentile component alone. Only the gate
    seed is simulated: ``seeds`` (≥ 1) is accepted for the interface, extra seeds are
    descriptive and cannot change the result. Deterministic tasks are concordant pairs; their
    outcome does not matter (both components use only the discordant cells and n). Resampling
    is the same ``choices`` scheme as `bench.stats.percentile`."""
    if not 0 <= f <= 1 or n_tasks < MIN_TASKS or seeds < 1:
        raise ValueError(f"need 0 <= f <= 1, n_tasks >= 2, seeds >= 1 (f={f}, n={n_tasks})")
    p_arm = _arm_p(f, true_diff)
    rng = random.Random(seed)
    passed = 0
    for _ in range(n_sim):
        values = [0] * n_tasks
        for i in range(n_tasks):
            if rng.random() < f:
                values[i] = (rng.random() < p_arm) - (rng.random() < 0.5)
        if any(values):
            boots = sorted(sum(rng.choices(values, k=n_tasks)) for _ in range(n_boot))
            p_lo = quantile(boots, alpha / 2) / n_tasks
            p_hi = quantile(boots, 1 - alpha / 2) / n_tasks
        else:
            p_lo = p_hi = 0.0
        if gate == "applied":
            n10, n01 = values.count(1), values.count(-1)
            lo = gate_bound(p_lo, p_hi, (0, n10, n01, n_tasks - n10 - n01), alpha)[0]
        else:
            lo = p_lo
        passed += lo > margin
    return passed / n_sim


def normal_approx_tasks(
    f: float, *, margin: float = MARGIN, alpha: float = 0.05, target: float = TARGET_POWER
) -> float:
    """Tasks needed by the normal approximation for one seed: var·(z_{1−α/2} + z_target)² /
    margin², var = f · 0.5 (difference of two Bernoulli(0.5) on flaky tasks)."""
    nd = statistics.NormalDist()
    z = nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(target)
    return f * 0.5 * z * z / (margin * margin)


def required_tasks(
    f: float,
    seeds: int = 1,
    *,
    pool: int,
    target: float = TARGET_POWER,
    min_tasks: int = MIN_PLAN_TASKS,
    **kw: Any,
) -> int | None:
    """Fewest tasks in [min_tasks, pool] whose simulated power reaches ``target``.

    Gallops from the normal approximation, then bisects (power is assumed monotone in the
    task count; simulations share their seed, so neighbouring sizes use common draws)."""
    margin = kw.get("margin", MARGIN)
    alpha = kw.get("alpha", 0.05)
    floor = max(MIN_TASKS, min_tasks, gate_min_tasks(margin, alpha))
    if pool < floor:
        return None
    cache: dict[int, bool] = {}

    def reaches(n: int) -> bool:
        if n not in cache:
            cache[n] = power(f, n, seeds, **kw) >= target
        return cache[n]

    guess = normal_approx_tasks(f, margin=margin, alpha=alpha, target=target)
    start = min(max(floor, math.ceil(guess)), pool)
    if reaches(start):
        hi = start
        while True:
            if hi == floor:
                return floor
            lo = max(floor, int(hi / 1.25))
            if not reaches(lo):
                break
            hi = lo
    else:
        lo = start
        while True:
            if lo == pool:
                return None
            hi = min(pool, math.ceil(lo * 1.25) + 1)
            if reaches(hi):
                break
            lo = hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if reaches(mid):
            hi = mid
        else:
            lo = mid
    return hi


def required_size(
    f: float,
    *,
    pool: int,
    max_seeds: int = MAX_SEEDS,
    target: float = TARGET_POWER,
    **kw: Any,
) -> tuple[int, int] | None:
    """(tasks, seeds) with the fewest runs reaching ``target``. Extra seeds never enter the gate
    (review ruling I1), so more seeds only add runs: the answer always has 1 seed."""
    if max_seeds < 1:
        raise ValueError(f"max_seeds must be >= 1, got {max_seeds}")
    tasks = required_tasks(f, 1, pool=pool, target=target, **kw)
    return None if tasks is None else (tasks, 1)


@dataclass(frozen=True)
class PowerPlan:
    required_tasks: int | None  # tasks to schedule (inflated for the screening exclusion rate)
    seeds: int
    achieved_power: float  # at the expected paired n after exclusions
    verdict: PowerVerdict
    expected_paired: int | None = None

    def as_e4(self) -> dict[str, Any]:
        """The ``power`` object of ``e4.json``."""
        return {
            "required_tasks": self.required_tasks,
            "seeds": self.seeds,
            "achieved_power": self.achieved_power,
            "verdict": self.verdict,
        }


def plan(
    model: FlipModel,
    *,
    pool: int,
    max_seeds: int = MAX_SEEDS,
    target: float = TARGET_POWER,
    **kw: Any,
) -> PowerPlan:
    """Confirmation size from the conservative ``f_plan``: the paired n reaching ``target``
    (≥ `MIN_PLAN_TASKS`) is found within the pool's expected post-exclusion size, then scheduled
    as ceil(n / (1 − exclusion_rate)) tasks. ``underpowered`` when no such n fits (then the
    power of the whole pool's expected paired n is reported). The quota check (6 weeks at the
    60 % weekly stop) is Task 10's and may still downgrade the verdict."""
    keep = 1.0 - model.exclusion_rate
    usable = math.floor(pool * keep + 1e-9) if keep > 0 else 0
    size = required_size(model.f_plan, pool=usable, max_seeds=max_seeds, target=target, **kw)
    if size is None:
        achieved = power(model.f_plan, usable, 1, **kw) if usable >= MIN_TASKS else 0.0
        return PowerPlan(None, 1, achieved, "underpowered", None)
    paired, seeds = size
    scheduled = min(pool, math.ceil(paired / keep - 1e-9))
    achieved = power(model.f_plan, paired, seeds, **kw)
    return PowerPlan(scheduled, seeds, achieved, "powered", paired)
