"""§10.5 statistics for E4: BCa task-cluster bootstrap, exact McNemar, non-inferiority.

Every interval resamples *tasks* (clusters): the seeds of one task are not independent, so a
task is drawn with all of its seeds and its per-task value (mean over seeds) is what the
statistic sees. Stdlib only; every resampling takes an explicit seed.

- Cost: `median_cost_ratio` — per-task cost = mean of ``run_cost`` over seeds, ratio arm/base per
  task, point = median ratio, BCa 95 % CI.
- Quality: `resolved_diff` — per-task resolved rate (mean over seeds), point = mean of the paired
  differences, BCa 95 % CI; the confirmatory test is `non_inferior` (``lo > margin``, −5 pp).
- Alongside, per seed: `mcnemar_conditional_exact` (Clopper–Pearson on the discordant pairs,
  mapped to the difference scale; labelled "conditional exact").

`summarise` turns ledger records (Task 7) and grades (Task 8) into a `Summary` that pairs arms
by task and lists exclusions with the reason names published in ``e4.json``.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
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


def _quantile(sorted_values: Sequence[float], p: float) -> float:
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
    return _quantile(ordered, adjusted(z)), _quantile(ordered, adjusted(-z))


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


# --------------------------------------------------------------------------- summaries


@dataclass(frozen=True)
class Exclusion:
    instance_id: str
    reason: str  # unrunnable | incomplete | ungraded


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
    stage: str
    reference: str
    arms: tuple[str, ...]
    seeds: tuple[int, ...]
    tasks: tuple[str, ...]  # tasks with a final, graded outcome for every key seen
    runs: tuple[RunRow, ...]
    exclusions: tuple[Exclusion, ...]
    counts: Mapping[str, int] = field(default_factory=dict)

    def _per_task(self, arm: str, value: Callable[[RunRow], float | None]) -> dict[str, Any]:
        out: dict[str, dict[int, Any]] = {}
        for r in self.runs:
            if r.arm == arm:
                out.setdefault(r.instance_id, {})[r.seed] = value(r)
        return out

    def resolved(self, arm: str) -> dict[str, dict[int, bool]]:
        return self._per_task(arm, lambda r: r.resolved)

    def costs(self, arm: str) -> dict[str, dict[int, float | None]]:
        return self._per_task(arm, lambda r: r.cost)

    def resolved_rate(self, arm: str) -> float:
        means = _task_means(self.resolved(arm))
        return math.fsum(means.values()) / len(means) if means else math.nan

    def resolved_diff(self, arm: str, vs: str | None = None, **kw: Any) -> Estimate:
        return resolved_diff(self.resolved(arm), self.resolved(vs or self.reference), **kw)

    def cost_ratio(self, arm: str, vs: str | None = None, **kw: Any) -> Estimate:
        return median_cost_ratio(self.costs(arm), self.costs(vs or self.reference), **kw)

    def discordance(
        self, arm: str, vs: str | None = None, *, seed: int | None = None
    ) -> tuple[int, int, int]:
        """(n10, n01, n) over (task, seed) pairs; n10 = arm resolved and ``vs`` not."""
        a, b = self.resolved(arm), self.resolved(vs or self.reference)
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
        parts: list[Any] = [raw.get(k) for k in ("campaign", "stage", "arm", "seed", "instance_id")]
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
) -> Summary:
    """Final graded runs of one stage, paired by task.

    ``ledger`` = the ledger records in append order (``kind`` start / end / invalidate / …,
    ``key`` = RunKey id or mapping); ``grades`` = RunKey id → resolved (None = harness error).
    A key's final outcome is its last non-invalidated ``end`` with status ok or agent_fail
    (``invalidate`` with a ``key`` reopens it; with ``from``/``to`` it reopens ends whose ``at``
    lies in that range). A task is excluded from every pairing when any key of it has no final
    outcome (``unrunnable`` after an infra failure, else ``incomplete``) or no grade
    (``ungraded``)."""
    ends: dict[tuple[str, int, str], list[Mapping[str, Any]]] = {}
    key_ids: dict[tuple[str, int, str], str] = {}
    infra_keys: set[tuple[str, int, str]] = set()
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
                ends[k] = [e for e in lst if not (lo <= e.get("at", math.nan) <= hi)]
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
            if rec.get("status") in GRADED_STATUSES:
                ends[k].append(rec)
            elif str(rec.get("status", "")).startswith("infra_fail"):
                counts["infra_fail"] += 1
                infra_keys.add(k)
        elif kind == "invalidate":
            counts["invalidated"] += 1
            ends[k] = []

    reasons: dict[str, str] = {}
    rows: list[RunRow] = []
    for k in sorted(ends):
        arm, seed, iid = k
        if not ends[k]:
            reasons.setdefault(iid, "unrunnable" if k in infra_keys else "incomplete")
            continue
        grade = grades.get(key_ids[k])
        if grade is None:
            reasons.setdefault(iid, "ungraded")
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
    kept = tuple(r for r in rows if r.instance_id not in reasons)
    tasks = tuple(sorted({r.instance_id for r in kept}))
    by_status: dict[str, int] = {}
    for r in kept:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return Summary(
        stage=stage,
        reference=reference,
        arms=tuple(sorted({r.arm for r in kept})),
        seeds=tuple(sorted({r.seed for r in kept})),
        tasks=tasks,
        runs=kept,
        exclusions=tuple(Exclusion(i, r) for i, r in sorted(reasons.items())),
        counts={
            "runs": len(kept),
            "tasks": len(tasks),
            "excluded": len(reasons),
            "ok": by_status.get("ok", 0),
            "agent_fail": by_status.get("agent_fail", 0),
            **counts,
        },
    )
