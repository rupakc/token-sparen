"""§10.5 statistics for E4: task-cluster bootstrap, exact paired intervals, non-inferiority.

Every interval resamples *tasks* (clusters): the seeds of one task are not independent, so a
task is drawn with all of its seeds and its per-task value (mean over seeds) is what the
statistic sees. Stdlib only; every resampling takes an explicit seed.

- Cost: `median_cost_ratio` — per-task cost = mean of ``run_cost`` over seeds, ratio arm/base per
  task, point = median ratio, BCa 95 % CI.
- Quality (descriptive): `resolved_diff` — per-task resolved rate (mean over seeds), point = mean
  of the paired differences, BCa 95 % CI.
- Quality (confirmatory; ruling M5-T9 and its review rulings I1/I2): `gate_decision` on exactly
  one pre-specified seed per task (`GATE_SEED` = 0; other seeds never enter): lower bound =
  min(task-cluster percentile bootstrap, Tango score) > −5 pp, after the exclusion rule
  (task-level exclusions dropped; the excess of token-sparen-only exclusions on tasks the
  baseline resolved over baseline-only exclusions on tasks token-sparen resolved is imputed as
  token-sparen failures; see `gate_decision`).
- Alongside, per seed: `mcnemar_conditional_exact` (Clopper–Pearson on the discordant pairs,
  mapped to the difference scale; labelled "conditional exact").

`summarise` turns ledger records (Task 7) and grades (Task 8) into a `Summary` that pairs arms
by task and lists exclusions per arm, seed and cause."""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from token_sparen.learn.beta import beta_quantile

MARGIN = -0.05
GRADED_STATUSES = frozenset({"ok", "agent_fail"})
_NORMAL = statistics.NormalDist()

PerTask = Mapping[str, Mapping[int, float | None]]
"""instance_id → seed → value (``run_cost`` in USD, or resolved as bool); None = missing."""


@dataclass(frozen=True)
class Estimate:
    point: float
    lo: float
    hi: float
    n: int
    method: str
    dropped: int = 0  # tasks without a value on either side (not paired)


# --------------------------------------------------------------------------- exact intervals


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Two-sided exact (1 − alpha) interval for a binomial proportion k/n."""
    if n < 1 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n and n >= 1, got k={k}, n={n}")
    lo = 0.0 if k == 0 else beta_quantile(alpha / 2, k, n - k + 1)
    hi = 1.0 if k == n else beta_quantile(1 - alpha / 2, k + 1, n - k)
    return lo, hi


def mcnemar_conditional_exact(n10: int, n01: int, n: int, *, alpha: float = 0.05) -> Estimate:
    """Paired difference d = (n10 − n01)/n with the conditional exact interval.

    Conditional on the m = n10 + n01 discordant pairs, n10 ~ Bin(m, p) and d = (m/n)(2p − 1);
    the Clopper–Pearson interval of p maps to d. ``n10`` = arm resolved, reference not."""
    m = n10 + n01
    if min(n10, n01) < 0 or n < 1 or m > n:
        raise ValueError(f"need n10, n01 >= 0 and n10 + n01 <= n, got {n10}, {n01}, {n}")
    point = (n10 - n01) / n
    if m == 0:
        return Estimate(point, 0.0, 0.0, n, "mcnemar-conditional-exact")
    p_lo, p_hi = clopper_pearson(n10, m, alpha)
    scale = m / n
    return Estimate(
        point, scale * (2 * p_lo - 1), scale * (2 * p_hi - 1), n, "mcnemar-conditional-exact"
    )


def non_inferior(e: Estimate, margin: float = MARGIN) -> bool:
    return e.lo > margin


# --------------------------------------------------------------------------- BCa


def quantile(sorted_values: Sequence[float], p: float) -> float:
    pos = min(max(p, 0.0), 1.0) * (len(sorted_values) - 1)
    i = int(pos)
    if i >= len(sorted_values) - 1:
        return float(sorted_values[-1])
    frac = pos - i
    return float(sorted_values[i] + (sorted_values[i + 1] - sorted_values[i]) * frac)


def _acceleration(jack: Sequence[float]) -> float:
    mean = math.fsum(jack) / len(jack)
    dev = [mean - j for j in jack]
    den = math.fsum(d * d for d in dev) ** 1.5
    return 0.0 if den == 0 else math.fsum(d**3 for d in dev) / (6 * den)


def bca_interval(
    theta: float, boots: Sequence[float], jack: Sequence[float], alpha: float
) -> tuple[float, float]:
    """BCa limits from bootstrap replicates and jackknife leave-one-out values.

    Bias correction counts ties at half weight (mid-rank) so discrete statistics are not pushed
    off-centre; the acceleration is invariant to shift and scale of ``jack``."""
    ordered = sorted(boots)
    b = len(ordered)
    less = bisect.bisect_left(ordered, theta)
    equal = bisect.bisect_right(ordered, theta) - less
    p0 = min(max((less + 0.5 * equal) / b, 0.5 / b), 1 - 0.5 / b)
    z0 = _NORMAL.inv_cdf(p0)
    a = _acceleration(jack)

    def adjusted(z: float) -> float:
        w = z0 + z
        den = 1 - a * w
        if den <= 0:
            return 1.0 if w > 0 else 0.0
        return _NORMAL.cdf(z0 + w / den)

    z = _NORMAL.inv_cdf(alpha / 2)
    return quantile(ordered, adjusted(z)), quantile(ordered, adjusted(-z))


def bca(
    stat: Callable[[Sequence[int]], float],
    n: int,
    *,
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Two-sided (1 − alpha) BCa interval of ``stat`` over ``n`` clusters.

    ``stat`` receives a sequence of cluster indices (with repeats in a resample); the full
    sample is ``range(n)``. Resamples draw indices with ``Random(seed).choices``; acceleration
    from the leave-one-cluster-out jackknife."""
    if n < 2:
        raise ValueError(f"BCa needs at least 2 clusters, got {n}")
    rng = random.Random(seed)
    population = range(n)
    theta = stat(population)
    boots = [stat(rng.choices(population, k=n)) for _ in range(n_boot)]
    jack = [stat([j for j in population if j != i]) for i in population]
    return bca_interval(theta, boots, jack, alpha)


# --------------------------------------------------------------------------- paired estimates


def _task_means(runs: PerTask) -> dict[str, float]:
    out: dict[str, float] = {}
    for task, by_seed in runs.items():
        values = [float(v) for v in by_seed.values() if v is not None]
        if values:
            out[task] = math.fsum(values) / len(values)
    return out


def _paired(
    arm: PerTask, base: PerTask
) -> tuple[list[str], dict[str, float], dict[str, float], int]:
    a, b = _task_means(arm), _task_means(base)
    tasks = sorted(set(a) & set(b))
    return tasks, a, b, len(set(arm) | set(base)) - len(tasks)


def median_cost_ratio(
    arm: PerTask, base: PerTask, *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0
) -> Estimate:
    """Median over tasks of mean-cost(arm)/mean-cost(base); a task without a positive base cost
    or without any cost on either side is dropped and counted in ``dropped``."""
    tasks, a, b, dropped = _paired(arm, base)
    kept = [t for t in tasks if b[t] > 0]
    dropped += len(tasks) - len(kept)
    ratios = [a[t] / b[t] for t in kept]
    if len(ratios) < 2:
        raise ValueError(f"need >= 2 tasks with a cost in both arms (base > 0), got {len(ratios)}")
    lo, hi = bca(
        lambda idx: statistics.median([ratios[i] for i in idx]),
        len(ratios),
        n_boot=n_boot,
        alpha=alpha,
        seed=seed,
    )
    return Estimate(
        statistics.median(ratios), lo, hi, len(ratios), "bca-task-cluster-median-ratio", dropped
    )


def resolved_diff(
    arm: PerTask, base: PerTask, *, n_boot: int = 10000, alpha: float = 0.05, seed: int = 0
) -> Estimate:
    """Mean over tasks of resolved-rate(arm) − resolved-rate(base), each the mean over seeds."""
    tasks, a, b, dropped = _paired(arm, base)
    diffs = [a[t] - b[t] for t in tasks]

    def mean_diff(idx: Sequence[int]) -> float:
        return math.fsum(diffs[i] for i in idx) / len(idx)

    lo, hi = bca(mean_diff, len(diffs), n_boot=n_boot, alpha=alpha, seed=seed)
    return Estimate(mean_diff(range(len(diffs))), lo, hi, len(diffs), "bca-task-cluster", dropped)


# --------------------------------------------------------------------------- the applied gate
#
# Ruling M5-T9: the task-cluster bootstrap alone is liberal with few tasks, so the confirmatory
# lower bound is min(percentile bootstrap lower bound clustered by task, a paired score lower
# bound); pass iff min > −5 pp. Of the two scores the ruling allowed, Tango is used: at 80 tasks
# with sparse one-sided discordance Newcombe's hybrid score let 8.8 % of margin-true
# confirmations pass (nominal 2.5 %), Tango 2.0 % (task-9 report).
# Review ruling I1: exactly one pre-specified seed per task enters the gate (majority outcomes
# over several seeds shrink the difference and broke the type-I guarantee); other seeds are
# descriptive only. Review ruling I2 and its re-review ruling: exclusions are attributed to an
# arm; one-sided ones are re-run first, then only the arm's excess of exclusions that can move
# the difference is imputed (`gate_decision`).

GATE_SEED = 0
GATE_METHOD = "min(bootstrap-percentile-task-cluster, tango-score), seed 0"


def percentile(
    stat: Callable[[Sequence[int]], float],
    n: int,
    *,
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Two-sided (1 − alpha) percentile interval over ``n`` clusters (same resampling as `bca`)."""
    if n < 2:
        raise ValueError(f"the bootstrap needs at least 2 clusters, got {n}")
    rng = random.Random(seed)
    population = range(n)
    boots = sorted(stat(rng.choices(population, k=n)) for _ in range(n_boot))
    return quantile(boots, alpha / 2), quantile(boots, 1 - alpha / 2)


def wilson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval (no continuity correction) for k/n."""
    if n < 1 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n and n >= 1, got k={k}, n={n}")
    z = _NORMAL.inv_cdf(1 - alpha / 2)
    centre = k + z * z / 2
    half = z * math.sqrt(z * z / 4 + k * (n - k) / n)
    den = n + z * z
    return max(0.0, (centre - half) / den), min(1.0, (centre + half) / den)


def newcombe_paired(n11: int, n10: int, n01: int, n00: int, *, alpha: float = 0.05) -> Estimate:
    """Newcombe (1998) hybrid score interval for p1 − p2 of paired binary data.

    p1 = (n11 + n10)/n (arm), p2 = (n11 + n01)/n (reference); Wilson limits of both margins are
    combined with the correlation φ = N'/√((n11+n10)(n01+n00)(n11+n01)(n10+n00)), N' = n11·n00 −
    n10·n01, reduced by n/2 when positive (Newcombe's correction; φ = 0 if a margin is empty)."""
    n = n11 + n10 + n01 + n00
    if min(n11, n10, n01, n00) < 0 or n < 1:
        raise ValueError(f"need non-negative cells and n >= 1, got {(n11, n10, n01, n00)}")
    x1, x2 = n11 + n10, n11 + n01
    p1, p2 = x1 / n, x2 / n
    l1, u1 = wilson(x1, n, alpha)
    l2, u2 = wilson(x2, n, alpha)
    margins = x1 * (n01 + n00) * x2 * (n10 + n00)
    if margins == 0:
        phi = 0.0
    else:
        cross: float = n11 * n00 - n10 * n01
        if cross > 0:
            cross = max(cross - n / 2, 0.0)
        phi = cross / math.sqrt(margins)
    dl1, du1, dl2, du2 = p1 - l1, u1 - p1, p2 - l2, u2 - p2
    theta = p1 - p2
    lo = theta - math.sqrt(max(0.0, dl1 * dl1 - 2 * phi * dl1 * du2 + du2 * du2))
    hi = theta + math.sqrt(max(0.0, du1 * du1 - 2 * phi * du1 * dl2 + dl2 * dl2))
    return Estimate(theta, max(-1.0, lo), min(1.0, hi), n, "newcombe-hybrid-score")


def _tango_pi(n10: int, n01: int, n: int, delta: float) -> float:
    """Restricted MLE of P(arm no, reference yes) given p1 − p2 = delta (Tango 1998)."""
    a = 2 * n
    b = -n10 - n01 + (2 * n - n10 + n01) * delta
    c = -n01 * delta * (1 - delta)
    return (math.sqrt(max(0.0, b * b - 4 * a * c)) - b) / (2 * a)


def _tango_z(n10: int, n01: int, n: int, delta: float) -> float:
    num = n10 - n01 - n * delta
    var = n * (2 * _tango_pi(n10, n01, n, delta) + delta - delta * delta)
    if var <= 0:
        return math.copysign(math.inf, num) if num else 0.0
    return num / math.sqrt(var)


def tango_paired(n11: int, n10: int, n01: int, n00: int, *, alpha: float = 0.05) -> Estimate:
    """Tango (1998) asymptotic score interval for p1 − p2 of paired binary data: the deltas
    whose restricted-MLE score statistic |Z(delta)| <= z_{1−α/2} (Z decreases in delta; each
    limit found by bisection to 1e-12). Only the discordant cells and n enter."""
    n = n11 + n10 + n01 + n00
    if min(n11, n10, n01, n00) < 0 or n < 1:
        raise ValueError(f"need non-negative cells and n >= 1, got {(n11, n10, n01, n00)}")
    z = _NORMAL.inv_cdf(1 - alpha / 2)
    point = (n10 - n01) / n

    def solve(target: float, lo: float, hi: float) -> float:
        # Z(lo) - target > 0 >= Z(hi) - target
        for _ in range(100):
            mid = 0.5 * (lo + hi)
            if _tango_z(n10, n01, n, mid) - target > 0:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-12:
                break
        return 0.5 * (lo + hi)

    edge = 1 - 1e-13
    lo = -1.0 if _tango_z(n10, n01, n, -edge) <= z else solve(z, -edge, point)
    hi = 1.0 if _tango_z(n10, n01, n, edge) >= -z else solve(-z, point, edge)
    return Estimate(point, lo, hi, n, "tango-score")


def gate_bound(
    percentile_lo: float, percentile_hi: float, cells: tuple[int, int, int, int], alpha: float
) -> tuple[float, float]:
    """(min, max) of the percentile and Tango limits — shared by `ni_gate` and the power
    simulation so the plan uses the gate actually applied. The published interval is this union
    (it need not be centred on the point; `GATE_METHOD` labels it)."""
    nc = tango_paired(*cells, alpha=alpha)
    return min(percentile_lo, nc.lo), max(percentile_hi, nc.hi)


def gate_min_tasks(margin: float = MARGIN, alpha: float = 0.05) -> int:
    """Fewest paired tasks with which the gate can pass at all: with zero discordant pairs the
    Tango lower bound is −g(n) and must exceed ``margin`` (74 at −5 pp, α = 0.05)."""
    n = 2
    while tango_paired(n, 0, 0, 0, alpha=alpha).lo <= margin:
        n += 1
    return n


def _gate_outcomes(
    runs: Mapping[str, Mapping[int, bool | None]], gate_seed: int
) -> dict[str, bool]:
    return {t: bool(v[gate_seed]) for t, v in runs.items() if v.get(gate_seed) is not None}


def ni_gate(
    arm: Mapping[str, Mapping[int, bool | None]],
    base: Mapping[str, Mapping[int, bool | None]],
    *,
    gate_seed: int = GATE_SEED,
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Estimate:
    """The confirmatory interval (method `GATE_METHOD`) on seed ``gate_seed`` only: tasks with
    that seed's outcome in both arms; point = arm − base resolved rate; other seeds ignored."""
    a, b = _gate_outcomes(arm, gate_seed), _gate_outcomes(base, gate_seed)
    tasks = sorted(a.keys() & b.keys())
    dropped = len(a.keys() | b.keys()) - len(tasks)
    diffs = [int(a[t]) - int(b[t]) for t in tasks]
    p_lo, p_hi = percentile(
        lambda idx: math.fsum(diffs[i] for i in idx) / len(idx),
        len(diffs),
        n_boot=n_boot,
        alpha=alpha,
        seed=seed,
    )
    n10 = sum(d > 0 for d in diffs)
    n01 = sum(d < 0 for d in diffs)
    n11 = sum(a[t] and b[t] for t in tasks)
    cells = (n11, n10, n01, len(tasks) - n11 - n10 - n01)
    lo, hi = gate_bound(p_lo, p_hi, cells, alpha)
    return Estimate(math.fsum(diffs) / len(diffs), lo, hi, len(diffs), GATE_METHOD, dropped)


@dataclass(frozen=True)
class GateCheck:
    """Confirmatory decision (re-review ruling of M5 Task 9). ``passed`` = no one-sided
    exclusion still pending re-runs AND the lower bound of ``estimate`` > margin."""

    estimate: Estimate  # decisive: observed pairs + the imputed excess
    observed: Estimate  # observed pairs only (descriptive)
    excluded_arm: int  # tasks excluded for the arm only (gate seed, after retries)
    excluded_ref: int  # tasks excluded for the reference only
    excluded_both: int  # task-level exclusions (dropped)
    imputed: tuple[str, ...]  # arm-only, reference-resolved tasks imputed as arm failures
    pending: int  # one-sided exclusions not yet re-run to their retry limit
    min_tasks: int
    passed: bool
    reasons: tuple[str, ...]


def gate_decision(
    arm: Mapping[str, Mapping[int, bool | None]],
    base: Mapping[str, Mapping[int, bool | None]],
    *,
    arm_excluded: Collection[str] = (),
    base_excluded: Collection[str] = (),
    pending: int = 0,
    gate_seed: int = GATE_SEED,
    margin: float = MARGIN,
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> GateCheck:
    """Exclusion rule (re-review rulings, round 3). ``*_excluded`` = tasks whose gate-seed key
    of that arm has no graded final outcome after the scheduler's re-runs. Task-level exclusions
    (both arms) are dropped. Of the one-sided ones only those that can move the difference
    count: k = max(0, |arm-only where the reference resolved| − |reference-only where the arm
    resolved|), and k of the former are imputed as arm failures (first k by task id); every
    other one-sided exclusion is dropped (a reference excess is never rewarded). No tolerance.
    ``pending`` > 0 (one-sided exclusions still to re-run) blocks a pass."""
    arm_x, base_x = set(arm_excluded), set(base_excluded)
    arm_only, base_only = arm_x - base_x, base_x - arm_x
    a_out = {t: v for t, v in _gate_outcomes(arm, gate_seed).items() if t not in arm_x}
    b_out = {t: v for t, v in _gate_outcomes(base, gate_seed).items() if t not in base_x}
    kw: dict[str, Any] = {"gate_seed": gate_seed, "n_boot": n_boot, "alpha": alpha, "seed": seed}
    arm_runs = {t: {gate_seed: v} for t, v in a_out.items()}
    base_runs = {t: {gate_seed: v} for t, v in b_out.items()}
    observed = ni_gate(arm_runs, base_runs, **kw)
    harmful = sorted(t for t in arm_only if b_out.get(t) is True)
    offsets = sum(1 for t in base_only if a_out.get(t) is True)
    imputed = tuple(harmful[: max(0, len(harmful) - offsets)])
    estimate = observed
    if imputed:
        arm_runs.update({t: {gate_seed: False} for t in imputed})
        estimate = ni_gate(arm_runs, base_runs, **kw)
    min_tasks = gate_min_tasks(margin, alpha)
    reasons: list[str] = []
    if pending:
        reasons.append(f"{pending} one-sided exclusion(s) still to re-run")
    if estimate.n < min_tasks:
        reasons.append(f"{estimate.n} paired tasks < {min_tasks}: the gate cannot pass")
    if not non_inferior(estimate, margin):
        reasons.append(f"lower bound {estimate.lo:.4f} <= margin {margin}")
    if imputed:
        reasons.append(f"excess of {len(imputed)} arm-only exclusion(s) imputed as failures")
    return GateCheck(
        estimate=estimate,
        observed=observed,
        excluded_arm=len(arm_only),
        excluded_ref=len(base_only),
        excluded_both=len(arm_x & base_x),
        imputed=imputed,
        pending=pending,
        min_tasks=min_tasks,
        passed=not pending and non_inferior(estimate, margin),
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------------------- summaries


@dataclass(frozen=True)
class Exclusion:
    """One run key without a graded final outcome. ``reason``: ``unrunnable`` (infra failures,
    no final), ``incomplete`` (no final, no infra failure), ``ungraded`` (final, no grade);
    ``cause``: the last infra ``sub`` (e.g. ``api``, ``load``), ``harness_error`` (grade None)
    or ``no_grade`` (key absent from the grades); None for ``incomplete``."""

    instance_id: str
    reason: str
    arm: str = ""
    seed: int = 0
    cause: str | None = None


@dataclass(frozen=True)
class RunRow:
    arm: str
    seed: int
    instance_id: str
    repo: str
    status: str
    cost: float | None
    resolved: bool
    plugin_hash: str | None = None
    model: str | None = None
    claude_code_version: str | None = None
    live: bool | None = None


@dataclass(frozen=True)
class RepoCounts:
    tasks: int
    runs: int
    resolved: int


def repo_of(instance_id: str) -> str:
    """``owner__name-123`` → ``owner/name`` (SWE-bench instance ids)."""
    head = instance_id.rsplit("-", 1)[0]
    return head.replace("__", "/")


@dataclass(frozen=True)
class Summary:
    """``runs`` = every graded final run; a task leaves a pairing (arm, vs) when any key of
    either arm is excluded (other arms' exclusions do not cost the pair); ``tasks`` = tasks
    without any exclusion; ``excluded_by_arm`` = tasks with ≥ 1 excluded key of that arm."""

    stage: str
    reference: str
    arms: tuple[str, ...]
    seeds: tuple[int, ...]
    tasks: tuple[str, ...]
    runs: tuple[RunRow, ...]
    exclusions: tuple[Exclusion, ...]
    counts: Mapping[str, int] = field(default_factory=dict)
    excluded_by_arm: Mapping[str, int] = field(default_factory=dict)

    def _per_task(self, arm: str, value: Callable[[RunRow], Any]) -> dict[str, Any]:
        out: dict[str, dict[int, Any]] = {}
        for r in self.runs:
            if r.arm == arm:
                out.setdefault(r.instance_id, {})[r.seed] = value(r)
        return out

    def excluded(self, arm: str, *, seed: int | None = None) -> set[str]:
        """Tasks with an excluded key of ``arm`` (at ``seed``, or at any seed)."""
        return {
            e.instance_id
            for e in self.exclusions
            if e.arm == arm and (seed is None or e.seed == seed)
        }

    def _pair(
        self, arm: str, vs: str | None, value: Callable[[RunRow], Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ref = vs or self.reference
        out = self.excluded(arm) | self.excluded(ref)
        a = {t: v for t, v in self._per_task(arm, value).items() if t not in out}
        b = {t: v for t, v in self._per_task(ref, value).items() if t not in out}
        return a, b

    def resolved(self, arm: str) -> dict[str, dict[int, bool]]:
        return self._per_task(arm, lambda r: r.resolved)

    def costs(self, arm: str) -> dict[str, dict[int, float | None]]:
        return self._per_task(arm, lambda r: r.cost)

    def resolved_rate(self, arm: str) -> float:
        means = _task_means(self.resolved(arm))
        return math.fsum(means.values()) / len(means) if means else math.nan

    def resolved_diff(self, arm: str, vs: str | None = None, **kw: Any) -> Estimate:
        return resolved_diff(*self._pair(arm, vs, lambda r: r.resolved), **kw)

    def gate(
        self,
        arm: str = "token-sparen",
        vs: str | None = None,
        *,
        gate_seed: int = GATE_SEED,
        **kw: Any,
    ) -> GateCheck:
        """The confirmatory decision (`gate_decision`) on seed ``gate_seed`` with this stage's
        exclusions of the two arms at that seed; one-sided ``incomplete`` keys are pending."""
        ref = vs or self.reference
        pending, _ = self.one_sided(arm, vs, seed=gate_seed)
        waiting = {(e.arm, e.instance_id) for e in pending}  # not imputed while re-runs are due
        arm_x = {t for t in self.excluded(arm, seed=gate_seed) if (arm, t) not in waiting}
        ref_x = {t for t in self.excluded(ref, seed=gate_seed) if (ref, t) not in waiting}
        return gate_decision(
            self.resolved(arm),
            self.resolved(ref),
            arm_excluded=arm_x,
            base_excluded=ref_x,
            pending=len(pending),
            gate_seed=gate_seed,
            **kw,
        )

    def cost_ratio(self, arm: str, vs: str | None = None, **kw: Any) -> Estimate:
        return median_cost_ratio(*self._pair(arm, vs, lambda r: r.cost), **kw)

    def one_sided(
        self, arm: str = "token-sparen", vs: str | None = None, *, seed: int = GATE_SEED
    ) -> tuple[list[Exclusion], list[Exclusion]]:
        """One-sided exclusions of the pair at ``seed`` (the other arm has no exclusion there):
        (pending, final). Pending = ``incomplete`` keys the scheduler must still re-run; final =
        ``unrunnable`` / ``ungraded`` after retries — what the gate's exclusion rule sees."""
        ref = vs or self.reference
        a, b = self.excluded(arm, seed=seed), self.excluded(ref, seed=seed)
        one = (a - b) | (b - a)
        rows = sorted(
            (e for e in self.exclusions if e.seed == seed and e.arm in (arm, ref)),
            key=lambda e: (e.instance_id, e.arm),
        )
        pending = [e for e in rows if e.instance_id in one and e.reason == "incomplete"]
        final = [e for e in rows if e.instance_id in one and e.reason != "incomplete"]
        return pending, final

    def exclusion_rates(
        self, arm: str = "token-sparen", vs: str | None = None, *, seed: int = GATE_SEED
    ) -> tuple[float, float]:
        """(one-sided rate per arm, task-level rate) over the tasks seen for the pair at
        ``seed`` — the power plan simulates exclusions at these rates."""
        ref = vs or self.reference
        a, b = self.excluded(arm, seed=seed), self.excluded(ref, seed=seed)
        seen = {r.instance_id for r in self.runs if r.arm in (arm, ref) and r.seed == seed}
        seen |= a | b
        if not seen:
            return 0.0, 0.0
        one_sided = len((a - b) | (b - a)) / (2 * len(seen))
        return one_sided, len(a & b) / len(seen)

    def discordance(
        self, arm: str, vs: str | None = None, *, seed: int | None = None
    ) -> tuple[int, int, int]:
        """(n10, n01, n) over (task, seed) pairs; n10 = arm resolved and ``vs`` not."""
        a, b = self._pair(arm, vs, lambda r: r.resolved)
        n10 = n01 = n = 0
        for task in a.keys() & b.keys():
            for s in a[task].keys() & b[task].keys():
                if seed is None or s == seed:
                    n += 1
                    n10 += a[task][s] and not b[task][s]
                    n01 += b[task][s] and not a[task][s]
        return n10, n01, n

    def mcnemar(
        self, arm: str, vs: str | None = None, *, seed: int, alpha: float = 0.05
    ) -> Estimate:
        return mcnemar_conditional_exact(*self.discordance(arm, vs, seed=seed), alpha=alpha)

    def per_repo(self) -> dict[str, dict[str, RepoCounts]]:
        acc: dict[str, dict[str, tuple[set[str], int, int]]] = {}
        for r in self.runs:
            tasks, runs, ok = acc.setdefault(r.repo, {}).get(r.arm, (set(), 0, 0))
            tasks.add(r.instance_id)
            acc[r.repo][r.arm] = (tasks, runs + 1, ok + r.resolved)
        return {
            repo: {arm: RepoCounts(len(t), n, ok) for arm, (t, n, ok) in sorted(arms.items())}
            for repo, arms in sorted(acc.items())
        }


def _parse_key(raw: Any) -> tuple[str, str, int, str] | None:
    """(stage, arm, seed, instance_id) from a RunKey id ``campaign/stage/arm/seed/iid`` or dict."""
    if isinstance(raw, Mapping):
        names = ("campaign", "stage", "arm", "seed", "instance_id")
        parts: list[Any] = [raw.get(k) for k in names]
    elif isinstance(raw, str):
        parts = list(raw.split("/", 4))
    else:
        return None
    if len(parts) != 5 or any(p is None for p in parts):
        return None
    try:
        return str(parts[1]), str(parts[2]), int(parts[3]), str(parts[4])
    except (TypeError, ValueError):
        return None


def _key_id(stage: str, arm: str, seed: int, iid: str, raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    return "/".join((str(raw["campaign"]), stage, arm, str(seed), iid))


def _field(rec: Mapping[str, Any], name: str) -> Any:
    if name in rec:
        return rec[name]
    metrics = rec.get("metrics")
    return metrics.get(name) if isinstance(metrics, Mapping) else None


def summarise(
    ledger: Iterable[Mapping[str, Any]],
    grades: Mapping[str, bool | None],
    *,
    stage: str,
    reference: str = "baseline",
    excluded: Iterable[Mapping[str, Any]] | None = None,
) -> Summary:
    """Final graded runs of one stage.

    ``ledger`` = the ledger records in append order (Task 7 shapes: ``kind`` start / end /
    invalidate / …, ``key`` = RunKey id or mapping, ``status``, ``sub``, ``at``, ``run_cost``,
    ``plugin_hash``, ``model``, ``claude_code_version``, ``live``, ``repo``); ``grades`` =
    RunKey id → resolved (None = harness error). A key's final outcome is its last
    non-invalidated ``end`` with status ok or agent_fail (``invalidate`` with a ``key`` reopens
    it; with ``from``/``to`` it reopens ends whose ``at`` lies in that range — an end without
    ``at`` then raises). Every key without a graded final outcome is an `Exclusion`.

    ``excluded`` = `bench.ledger.Ledger.excluded_after_retries` (``key``, ``cause``): when given,
    only those keys are ``unrunnable`` and any other key without a final outcome is
    ``incomplete`` (pending re-runs); without it, a key with any infra failure counts as
    unrunnable. `summarise_ledger` wires the ledger, its exclusions and its grades."""
    ends: dict[tuple[str, int, str], list[Mapping[str, Any]]] = {}
    key_ids: dict[tuple[str, int, str], str] = {}
    infra_sub: dict[tuple[str, int, str], str] = {}
    hashes: dict[tuple[str, int, str], str] = {}
    counts = {"infra_fail": 0, "invalidated": 0}
    for rec in ledger:
        kind = rec.get("kind")
        if kind == "invalidate" and "key" not in rec:
            lo, hi = rec.get("from"), rec.get("to")
            if lo is None or hi is None:
                continue
            counts["invalidated"] += 1
            for k, lst in ends.items():
                if any(e.get("at") is None for e in lst):
                    raise ValueError(f"range invalidate, but an end of {key_ids[k]} has no 'at'")
                ends[k] = [e for e in lst if not lo <= e["at"] <= hi]
            continue
        parsed = _parse_key(rec.get("key"))
        if parsed is None or parsed[0] != stage:
            continue
        k = parsed[1:]
        key_ids.setdefault(k, _key_id(stage, *k, rec["key"]))
        ends.setdefault(k, [])
        if kind == "start" and rec.get("plugin_hash"):
            hashes[k] = str(rec["plugin_hash"])
        elif kind == "end":
            status = str(rec.get("status", ""))
            if status in GRADED_STATUSES:
                ends[k].append(rec)
            elif status.startswith("infra_fail"):
                counts["infra_fail"] += 1
                infra_sub[k] = str(rec.get("sub") or status.partition("/")[2] or "unknown")
        elif kind == "invalidate":
            counts["invalidated"] += 1
            ends[k] = []

    if excluded is not None:
        given: dict[tuple[str, int, str], str] = {}
        for ex in excluded:
            parsed_ex = _parse_key(ex.get("key"))
            if parsed_ex is not None and parsed_ex[0] == stage:
                given[parsed_ex[1:]] = str(ex.get("cause") or "unknown")
        infra_sub = given
    exclusions: list[Exclusion] = []
    rows: list[RunRow] = []
    for k in sorted(ends):
        arm, seed, iid = k
        if not ends[k]:
            if k in infra_sub:
                exclusions.append(Exclusion(iid, "unrunnable", arm, seed, infra_sub[k]))
            else:
                exclusions.append(Exclusion(iid, "incomplete", arm, seed, None))
            continue
        kid = key_ids[k]
        grade = grades.get(kid)
        if grade is None:
            cause = "harness_error" if kid in grades else "no_grade"
            exclusions.append(Exclusion(iid, "ungraded", arm, seed, cause))
            continue
        end = ends[k][-1]
        cost = end.get("run_cost", _field(end, "run_cost"))
        live = _field(end, "live")
        rows.append(
            RunRow(
                arm=arm,
                seed=seed,
                instance_id=iid,
                repo=str(end.get("repo") or repo_of(iid)),
                status=str(end["status"]),
                cost=None if cost is None else float(cost),
                resolved=bool(grade),
                plugin_hash=_field(end, "plugin_hash") or hashes.get(k),
                model=_field(end, "model"),
                claude_code_version=_field(end, "claude_code_version"),
                live=None if live is None else bool(live),
            )
        )
    excluded_tasks = {e.instance_id for e in exclusions}
    tasks = tuple(sorted({r.instance_id for r in rows} - excluded_tasks))
    by_arm: dict[str, set[str]] = {}
    for e in exclusions:
        by_arm.setdefault(e.arm, set()).add(e.instance_id)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return Summary(
        stage=stage,
        reference=reference,
        arms=tuple(sorted({r.arm for r in rows} | set(by_arm))),
        seeds=tuple(sorted({r.seed for r in rows})),
        tasks=tasks,
        runs=tuple(rows),
        exclusions=tuple(exclusions),
        counts={
            "runs": len(rows),
            "tasks": len(tasks),
            "excluded": len(excluded_tasks),
            "ok": by_status.get("ok", 0),
            "agent_fail": by_status.get("agent_fail", 0),
            **counts,
        },
        excluded_by_arm={a: len(t) for a, t in sorted(by_arm.items())},
    )


def summarise_ledger(
    ledger: Any,
    *,
    stage: str,
    reference: str = "baseline",
    grades: Mapping[str, bool | None] | None = None,
) -> Summary:
    """`summarise` over a `bench.ledger.Ledger`: its records, `excluded_after_retries(stage)`
    as the exclusion source and (unless ``grades`` is given) the stage's grades from
    `bench.grade.load_grades` next to the ledger."""
    from bench.grade import grades_path, load_grades

    if grades is None:
        grades = load_grades(grades_path(ledger), stage=stage)
    return summarise(
        ledger.records(),
        grades,
        stage=stage,
        reference=reference,
        excluded=ledger.excluded_after_retries(stage),
    )
