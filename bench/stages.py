"""E4 stages (M5 Task 10): screening, the power-sized confirmation and the output-style rule.

Screening (owner answers): the 44 Mini tasks that have an arm64 image (the 6 sphinx tasks without
one are excluded by platform availability, decided before any run), one seed, ``--model sonnet``
(effort high), every arm. Frozen at ``v0.5.0-screen1``.

Confirmation (ruling M5-B1 and the M5 Task 9 review rulings): seed 0 only; the size comes from
`bench.power.plan` on the screening discordance (conservative ``f_plan``, exclusions simulated at
the screening rates, at least `bench.power.MIN_PLAN_TASKS` tasks); tasks = Mini ∪
`bench.tasks.extend` (seeded, stratified) up to that size; arms ``baseline`` + ``token-sparen``,
then — each only while the projected quota of the whole stage still fits 6 weeks at the 60 %
weekly stop and the disk stays above its 30 GB floor (ruling M5-N2) — the best competitor and
``ts-style`` (descriptive only: the gate reads token-sparen vs baseline). ``underpowered`` when no
size within the pool reaches 80 % power or the two core arms alone do not fit; the owner then
decides (`docs/release/gate-decision.toml`). Frozen at ``v1.0.0-rc1`` after the M6 pre-freeze
tasks.

The output style (`style_rule`) is recommended as an opt-in only from confirmation evidence;
defaults never change here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from bench import power
from bench.power import FlipModel, PowerPlan, PowerVerdict
from bench.quota import QuotaPolicy
from bench.scheduler import DISK_FLOOR_GB
from bench.stats import GATE_SEED, MARGIN, Estimate, Summary
from bench.tasks import Task, extend, load_pool

__all__ = [
    "COMPETITORS",
    "CORE_ARMS",
    "SCREENING",
    "STYLE_ARM",
    "ConfirmationPlan",
    "DiskProjection",
    "PowerVerdict",
    "QuotaProjection",
    "StageSpec",
    "StyleVerdict",
    "best_competitor",
    "confirmation_spec",
    "fits",
    "plan_confirmation",
    "screening_spec",
    "style_rule",
    "style_screen_held",
]

TREATMENT = "token-sparen"
REFERENCE = "baseline"
STYLE_ARM = "ts-style"
CORE_ARMS = (REFERENCE, TREATMENT)
SCREENING_ARMS = (
    "baseline",
    "concise",
    "token-sparen",
    "ts-style",
    "ts-cold",
    "rtk",
    "headroom",
    "tokeneconomizer",
)
SCREENING_OPTIONAL = ("caveman", "context-mode")
COMPETITORS = ("rtk", "headroom", "tokeneconomizer", "caveman", "context-mode")
SCREENING_MODEL = "sonnet"
WEEKS = 6
WEEKLY_STOP_PCT = QuotaPolicy().weekly_stop_pct  # 60 % (owner)
EXTEND_SEED = 0
# float slack for "fits exactly" (e.g. a quota that allows precisely 6 weeks)
_EPS = 1e-9

StyleVerdict = Literal["recommend_opt_in", "keep_off", "not_run"]


@dataclass(frozen=True)
class StageSpec:
    name: str
    arms: tuple[str, ...]
    optional_arms: tuple[str, ...]
    seeds: tuple[int, ...]
    model: str
    tasks: tuple[str, ...]

    @property
    def runs(self) -> int:
        """Runs of the required arms (optional arms only run if quota allows)."""
        return len(self.arms) * len(self.seeds) * len(self.tasks)


def screening_spec(pool: Sequence[Task] | None = None) -> StageSpec:
    """Screening on the Mini tasks of the arm64 pool (pool order)."""
    tasks = load_pool() if pool is None else pool
    return StageSpec(
        name="screening",
        arms=SCREENING_ARMS,
        optional_arms=SCREENING_OPTIONAL,
        seeds=(GATE_SEED,),
        model=SCREENING_MODEL,
        tasks=tuple(t.instance_id for t in tasks if t.mini),
    )


SCREENING = screening_spec()


@dataclass(frozen=True)
class QuotaProjection:
    """From Task 13 calibration: weekly % one run uses, and runs the window caps allow a week."""

    pct_per_run_weekly: float
    runs_per_week: float

    def __post_init__(self) -> None:
        if not self.pct_per_run_weekly > 0 or not self.runs_per_week > 0:
            raise ValueError(f"quota projection needs positive values, got {self}")

    def weekly_runs(self, stop_pct: float = WEEKLY_STOP_PCT) -> float:
        """Runs per week before the weekly stop or the window caps, whichever binds first."""
        return min(self.runs_per_week, stop_pct / self.pct_per_run_weekly)

    def weeks_needed(self, runs: int, stop_pct: float = WEEKLY_STOP_PCT) -> float:
        return runs / self.weekly_runs(stop_pct)


@dataclass(frozen=True)
class DiskProjection:
    """From Task 13 calibration (ruling M5-N2): size of one task image, what stays on disk for
    the whole campaign (the Mini images), free disk now, and workspace growth per run (ledger,
    run dirs, patches, promoted state)."""

    gb_per_image: float
    gb_kept: float
    free_gb: float
    gb_per_run: float = 0.0

    def __post_init__(self) -> None:
        if min(self.gb_per_image, self.gb_kept, self.free_gb, self.gb_per_run) < 0:
            raise ValueError(f"disk projection needs non-negative values, got {self}")

    def headroom(self, runs: int) -> float:
        """Free disk left with the kept images, one live image and the stage's growth."""
        return self.free_gb - self.gb_kept - self.gb_per_image - self.gb_per_run * runs


def fits(
    spec: StageSpec,
    proj: QuotaProjection,
    disk: DiskProjection,
    weeks: float = WEEKS,
    *,
    stop_pct: float = WEEKLY_STOP_PCT,
    floor_gb: float = DISK_FLOOR_GB,
) -> bool:
    """The whole stage runs within ``weeks`` at the weekly stop and keeps ≥ ``floor_gb`` free."""
    return (
        proj.weeks_needed(spec.runs, stop_pct) <= weeks + _EPS
        and disk.headroom(spec.runs) >= floor_gb - _EPS
    )


# --------------------------------------------------------------------------- screening rules


def _point(fn: Callable[..., Estimate], *args: Any, n_boot: int) -> float | None:
    """Point estimate, or None when the pair has too few tasks."""
    try:
        return fn(*args, n_boot=n_boot).point
    except ValueError:
        return None


def best_competitor(screen: Summary, *, n_boot: int = 1000) -> str | None:
    """Lowest screening median cost ratio vs baseline among competitors whose resolved-diff point
    vs baseline is ≥ −5 pp (ties by name); None if no competitor qualifies."""
    ranked: list[tuple[float, str]] = []
    for arm in COMPETITORS:
        if arm not in screen.arms:
            continue
        diff = _point(screen.resolved_diff, arm, n_boot=n_boot)
        ratio = _point(screen.cost_ratio, arm, n_boot=n_boot)
        if diff is None or ratio is None or math.isnan(ratio) or diff < MARGIN:
            continue
        ranked.append((ratio, arm))
    return min(ranked)[1] if ranked else None


def style_screen_held(screen: Summary, *, n_boot: int = 1000) -> bool:
    """Screening rule for ``ts-style``: resolved-diff point vs token-sparen ≥ −5 pp and median
    cost ratio vs token-sparen < 1 (points only; descriptive)."""
    if STYLE_ARM not in screen.arms or TREATMENT not in screen.arms:
        return False
    diff = _point(screen.resolved_diff, STYLE_ARM, TREATMENT, n_boot=n_boot)
    ratio = _point(screen.cost_ratio, STYLE_ARM, TREATMENT, n_boot=n_boot)
    return diff is not None and ratio is not None and diff >= MARGIN and ratio < 1


# --------------------------------------------------------------------------- confirmation


@dataclass(frozen=True)
class ConfirmationPlan:
    spec: StageSpec
    verdict: PowerVerdict  # the power plan's verdict, downgraded when the core does not fit
    power: PowerPlan  # the simulation result (its own verdict ignores quota and disk)
    flip: FlipModel
    reasons: tuple[str, ...]

    def as_e4(self) -> dict[str, Any]:
        """The ``power`` object of ``e4.json`` with the stage verdict."""
        return self.power.as_e4() | {"verdict": self.verdict}


def plan_confirmation(
    screen: Summary,
    *,
    pool: Sequence[Task],
    quota: QuotaProjection,
    disk: DiskProjection,
    model: str,
    weeks: float = WEEKS,
    extend_seed: int = EXTEND_SEED,
    **power_kw: Any,
) -> ConfirmationPlan:
    """The confirmation stage from the screening summary (see the module docstring).

    ``power_kw`` go to `bench.power.plan` (``n_sim``, ``n_boot``, ``seed``; the dry run uses
    small budgets). Underpowered by pool → the whole pool is scheduled (the best that can be
    run); underpowered by quota or disk → the planned size; either way only the core arms and
    the owner decides."""
    flip = power.flip_model(screen)
    plan = power.plan(flip, pool=len(pool), **power_kw)
    mini = [t for t in pool if t.mini]
    reasons: list[str] = []
    verdict: PowerVerdict = plan.verdict
    if plan.required_tasks is None:
        size = len(pool)
        reasons.append(
            f"no size within the pool of {len(pool)} tasks reaches "
            f"{power.TARGET_POWER:.0%} power (achieved {plan.achieved_power:.2f})"
        )
    else:
        size = min(len(pool), max(plan.required_tasks, len(mini)))
    drawn = extend(pool, mini, max(0, size - len(mini)), seed=extend_seed)
    spec = StageSpec(
        name="confirmation",
        arms=CORE_ARMS,
        optional_arms=(),
        seeds=(GATE_SEED,),
        model=model,
        tasks=tuple(t.instance_id for t in [*mini, *drawn]),
    )
    if not fits(spec, quota, disk, weeks):
        verdict = "underpowered"
        reasons.append(
            f"core arms need {quota.weeks_needed(spec.runs):.1f} weeks of quota at the "
            f"{WEEKLY_STOP_PCT:.0f} % weekly stop (limit {weeks}) or leave "
            f"{disk.headroom(spec.runs):.1f} GB free (floor {DISK_FLOOR_GB:.0f})"
        )
        return ConfirmationPlan(spec, verdict, plan, flip, tuple(reasons))
    if verdict == "underpowered":  # descriptive extras never take quota from a weak core
        return ConfirmationPlan(spec, verdict, plan, flip, tuple(reasons))
    n_boot = int(power_kw.get("n_boot", 1000))
    extras = [best_competitor(screen, n_boot=n_boot)]
    extras.append(STYLE_ARM if style_screen_held(screen, n_boot=n_boot) else None)
    for arm in extras:
        if arm is None:
            continue
        trial = replace(spec, arms=(*spec.arms, arm))
        if not fits(trial, quota, disk, weeks):
            reasons.append(f"{arm} not added: the stage would not fit quota or disk")
            break
        spec = trial
    return ConfirmationPlan(spec, verdict, plan, flip, tuple(reasons))


def confirmation_spec(
    screen: Summary,
    *,
    pool: Sequence[Task],
    quota: QuotaProjection,
    disk: DiskProjection,
    model: str,
    **kw: Any,
) -> tuple[StageSpec, PowerVerdict]:
    """`plan_confirmation` reduced to (spec, verdict)."""
    p = plan_confirmation(screen, pool=pool, quota=quota, disk=disk, model=model, **kw)
    return p.spec, p.verdict


# --------------------------------------------------------------------------- output style


def style_rule(summary: Summary, *, confirmatory: bool, **kw: Any) -> StyleVerdict:
    """Output-style recommendation (§6.6). ``recommend_opt_in`` only from confirmation, iff
    ``ts-style`` passes the confirmatory non-inferiority gate (`Summary.gate`, seed 0) vs
    baseline AND vs token-sparen, and its median cost ratio vs token-sparen has an upper bound
    < 1. Screening-only evidence → ``keep_off`` (numbers still published as descriptive);
    ``not_run`` when the stage has no ts-style run. Never changes a default."""
    if not any(r.arm == STYLE_ARM for r in summary.runs):
        return "not_run"
    if not confirmatory:
        return "keep_off"
    try:
        ni = all(summary.gate(STYLE_ARM, vs, **kw).passed for vs in (REFERENCE, TREATMENT))
        ratio = summary.cost_ratio(STYLE_ARM, TREATMENT, **kw)
    except (ValueError, ZeroDivisionError):
        return "keep_off"
    return "recommend_opt_in" if ni and ratio.hi < 1 else "keep_off"
