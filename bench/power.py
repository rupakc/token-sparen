"""Power step (ruling M5-B1): size the confirmation from the screening discordance.

Flip model: a share ``f`` of tasks is "flaky" (resolved with p = 0.5 in each arm and seed,
independently); every other task is deterministic (same outcome in both arms). With one seed a
flaky pair is discordant half the time, so the screening discordant share q gives f = min(1, 2q).
Planning uses the conservative one-sided 80 % Clopper–Pearson upper bound of q.

`power` simulates the confirmatory gate itself — task-cluster BCa lower bound of the resolved
difference > margin — under a true difference of 0 (or ``true_diff``), and `required_size`
picks the (tasks, seeds) with the fewest runs that reaches the target power within the pool.
Field names of `PowerPlan.as_e4` are the ``power`` object of ``bench/e4.schema.json``.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any, Literal

from bench.stats import MARGIN, Summary, bca_interval, clopper_pearson

PLANNING_CONFIDENCE = 0.8
TARGET_POWER = 0.8
MAX_SEEDS = 3
MIN_TASKS = 2
# Smallest confirmation the planner proposes: with few task clusters the BCa gate is liberal
# (simulated type-I error at the margin ≈ 0.47 at 5 tasks, ≈ 0.05 at 50, ≈ α/2 from ~80 on;
# tests/bench/test_power.py), so sizes below this are never planned.
MIN_PLAN_TASKS = 50

PowerVerdict = Literal["powered", "underpowered"]


@dataclass(frozen=True)
class FlipModel:
    f: float  # share of flaky tasks (point estimate)
    q: float  # discordant share of token-sparen vs baseline pairs in screening
    n: int  # screening pairs
    q_hi: float  # one-sided 80 % Clopper–Pearson upper bound of q
    f_plan: float  # min(1, 2 q_hi): the conservative f used for planning


def flip_model(
    screen: Summary,
    *,
    arm: str = "token-sparen",
    vs: str | None = None,
    confidence: float = PLANNING_CONFIDENCE,
) -> FlipModel:
    n10, n01, n = screen.discordance(arm, vs)
    if n == 0:
        raise ValueError(f"no paired {arm} runs in the screening summary")
    m = n10 + n01
    q = m / n
    # the upper limit of a two-sided (1 − 2(1 − c)) interval is the one-sided c upper bound
    q_hi = clopper_pearson(m, n, alpha=2 * (1 - confidence))[1]
    return FlipModel(f=min(1.0, 2 * q), q=q, n=n, q_hi=q_hi, f_plan=min(1.0, 2 * q_hi))


def _arm_p(f: float, true_diff: float) -> float:
    if true_diff == 0:
        return 0.5
    if f <= 0 or abs(true_diff) > 0.5 * f:
        raise ValueError(f"|true_diff| must be <= f/2 (only flaky tasks differ), got {true_diff}")
    return 0.5 + true_diff / f


def power(
    f: float,
    n_tasks: int,
    seeds: int,
    *,
    margin: float = MARGIN,
    alpha: float = 0.05,
    n_sim: int = 2000,
    seed: int = 0,
    n_boot: int = 1000,
    true_diff: float = 0.0,
) -> float:
    """Share of simulated confirmations whose BCa lower bound of the resolved difference
    exceeds ``margin``. Same gate as `bench.stats.resolved_diff` + `non_inferior` (task
    resamples via ``choices``, mid-rank bias correction, jackknife acceleration), with the
    per-task differences kept as integers (units of 1/seeds) for speed."""
    if not 0 <= f <= 1 or n_tasks < MIN_TASKS or seeds < 1:
        raise ValueError(f"need 0 <= f <= 1, n_tasks >= 2, seeds >= 1 (f={f}, n={n_tasks})")
    p_arm = _arm_p(f, true_diff)
    rng = random.Random(seed)
    scale = n_tasks * seeds
    passed = 0
    for _ in range(n_sim):
        values = [0] * n_tasks
        for i in range(n_tasks):
            if rng.random() < f:
                if p_arm == 0.5:
                    x = rng.getrandbits(seeds).bit_count()
                else:
                    x = sum(rng.random() < p_arm for _ in range(seeds))
                values[i] = x - rng.getrandbits(seeds).bit_count()
        total = sum(values)
        if not any(values):
            lo = 0.0
        else:
            boots = [sum(rng.choices(values, k=n_tasks)) for _ in range(n_boot)]
            jack = [total - v for v in values]
            lo = bca_interval(total, boots, jack, alpha)[0] / scale
        passed += lo > margin
    return passed / n_sim


def normal_approx_tasks(
    f: float, seeds: int, *, margin: float = MARGIN, alpha: float = 0.05, target: float = 0.8
) -> float:
    """Tasks needed by the normal approximation: var·(z_{1−α/2} + z_target)² / margin²,
    var = f · 0.5 / seeds (difference of two Bin(seeds, 0.5)/seeds on flaky tasks)."""
    nd = statistics.NormalDist()
    z = nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(target)
    return f * 0.5 / seeds * z * z / (margin * margin)


def required_tasks(
    f: float,
    seeds: int,
    *,
    pool: int,
    target: float = TARGET_POWER,
    min_tasks: int = MIN_PLAN_TASKS,
    **kw: Any,
) -> int | None:
    """Fewest tasks in [min_tasks, pool] whose simulated power reaches ``target`` at ``seeds``.

    Gallops from the normal approximation, then bisects (power is assumed monotone in the
    task count; simulations share their seed, so neighbouring sizes use common draws)."""
    floor = max(MIN_TASKS, min_tasks)
    if pool < floor:
        return None
    cache: dict[int, bool] = {}

    def reaches(n: int) -> bool:
        if n not in cache:
            cache[n] = power(f, n, seeds, **kw) >= target
        return cache[n]

    margin = kw.get("margin", MARGIN)
    alpha = kw.get("alpha", 0.05)
    guess = normal_approx_tasks(f, seeds, margin=margin, alpha=alpha, target=target)
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
    """(tasks, seeds) with the fewest runs reaching ``target``; ties → more tasks."""
    best: tuple[int, int] | None = None
    for seeds in range(1, max_seeds + 1):
        tasks = required_tasks(f, seeds, pool=pool, target=target, **kw)
        if tasks is None:
            continue
        if best is None or (tasks * seeds, -tasks) < (best[0] * best[1], -best[0]):
            best = (tasks, seeds)
    return best


@dataclass(frozen=True)
class PowerPlan:
    required_tasks: int | None
    seeds: int
    achieved_power: float
    verdict: PowerVerdict

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
    """Confirmation size from the conservative ``f_plan``; ``underpowered`` when no size within
    the pool reaches ``target`` (then the power of the whole pool at ``max_seeds`` is reported).
    The quota check (6 weeks at the 60 % weekly stop) is Task 10's and may still downgrade the
    verdict."""
    size = required_size(model.f_plan, pool=pool, max_seeds=max_seeds, target=target, **kw)
    if size is None:
        achieved = power(model.f_plan, pool, max_seeds, **kw) if pool >= MIN_TASKS else 0.0
        return PowerPlan(None, max_seeds, achieved, "underpowered")
    tasks, seeds = size
    return PowerPlan(tasks, seeds, power(model.f_plan, tasks, seeds, **kw), "powered")
