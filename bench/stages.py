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

import datetime as dt
import hashlib
import json
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from bench import power
from bench.arms import load_arms
from bench.power import FlipModel, PowerPlan, PowerVerdict
from bench.quota import QuotaPolicy
from bench.scheduler import DISK_FLOOR_GB
from bench.stats import GATE_SEED, MARGIN, Estimate, Exclusion, Summary, summarise_ledger
from bench.tasks import Task, extend, load_pool

if TYPE_CHECKING:
    from bench.ledger import Ledger

__all__ = [
    "COMPETITORS",
    "CORE_ARMS",
    "SCREENING",
    "STYLE_ARM",
    "FROZEN_ARMS",
    "ConfirmationPlan",
    "DiskProjection",
    "PlanError",
    "PowerVerdict",
    "QuotaProjection",
    "StagePlan",
    "StageSpec",
    "StyleVerdict",
    "best_competitor",
    "confirmation_spec",
    "fits",
    "plan_confirmation",
    "read_plan",
    "screening_spec",
    "style_rule",
    "style_screen_held",
    "invalid_as_exclusions",
    "is_final_exclusion",
    "missing",
    "restrict",
    "write_plan",
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
# arms that run the stage's frozen plugin: every run of theirs must carry its plugin_hash
FROZEN_ARMS = frozenset(name for name, arm in load_arms().items() if arm.uses_frozen_plugin)
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
    """From Task 13 calibration: weekly % one run attempt uses, and attempts the window caps
    allow a week. ``retry_rate`` = extra attempts per scheduled run (infra-fail retries and the
    one-sided re-runs the gate requires), from the screening ledger; the projection counts
    ``runs × (1 + retry_rate)`` attempts."""

    pct_per_run_weekly: float
    runs_per_week: float
    retry_rate: float = 0.0

    def __post_init__(self) -> None:
        if not self.pct_per_run_weekly > 0 or not self.runs_per_week > 0:
            raise ValueError(f"quota projection needs positive values, got {self}")
        if not self.retry_rate >= 0:
            raise ValueError(f"retry_rate must be >= 0, got {self.retry_rate}")

    def weekly_runs(self, stop_pct: float = WEEKLY_STOP_PCT) -> float:
        """Runs per week before the weekly stop or the window caps, whichever binds first."""
        return min(self.runs_per_week, stop_pct / self.pct_per_run_weekly)

    def weeks_needed(self, runs: int, stop_pct: float = WEEKLY_STOP_PCT) -> float:
        return runs * (1 + self.retry_rate) / self.weekly_runs(stop_pct)


@dataclass(frozen=True)
class DiskProjection:
    """From Task 13 calibration (ruling M5-N2), in GB:

    - ``gb_per_image``: one task image (the live one, pulled while its block runs), including
      the grading container and its layers;
    - ``gb_kept``: images kept for the whole campaign (the Mini images) that are NOT yet on
      disk when ``free_gb`` is measured (images already present are already counted there);
    - ``free_gb``: min(host free disk, Docker VM free disk) when measured — the floor applies
      to both;
    - ``gb_per_run``: workspace growth per run (ledger, run dirs, patches, promoted state);
      required, so a forgotten calibration value cannot silently mean zero."""

    gb_per_image: float
    gb_kept: float
    free_gb: float
    gb_per_run: float

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
    return not _limits(spec, proj, disk, weeks, stop_pct=stop_pct, floor_gb=floor_gb)


def _limits(
    spec: StageSpec,
    proj: QuotaProjection,
    disk: DiskProjection,
    weeks: float = WEEKS,
    *,
    stop_pct: float = WEEKLY_STOP_PCT,
    floor_gb: float = DISK_FLOOR_GB,
) -> list[str]:
    """The limits ``spec`` exceeds, each as a reason naming its numbers (empty: it fits)."""
    out: list[str] = []
    need = proj.weeks_needed(spec.runs, stop_pct)
    if need > weeks + _EPS:
        out.append(
            f"quota: {spec.runs} runs need {need:.1f} weeks at the {stop_pct:.0f} % weekly "
            f"stop (limit {weeks})"
        )
    left = disk.headroom(spec.runs)
    if left < floor_gb - _EPS:
        out.append(f"disk: {spec.runs} runs leave {left:.1f} GB free (floor {floor_gb:.0f} GB)")
    return out


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
    bound = _limits(spec, quota, disk, weeks)
    if bound:
        verdict = "underpowered"
        reasons.extend(f"core arms do not fit: {b}" for b in bound)
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
        over = _limits(trial, quota, disk, weeks)
        if over:
            reasons.extend(f"{arm} not added: {b}" for b in over)
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


# --------------------------------------------------------------------------- persisted plan

PLAN_KIND = "plan"


class PlanError(ValueError):
    """The confirmation plan cannot be written, or the ledger holds no valid one."""


@dataclass(frozen=True)
class StagePlan:
    """The confirmation plan as recorded in the ledger (review ruling I3): what the gate and
    the style rule read, never caller-supplied values."""

    stage: str
    tasks: tuple[str, ...]
    arms: tuple[str, ...]
    seed: int
    model: str
    claude_code_version: str
    plugin_hash: str
    plugin_version: str
    price_confirmed_on: str  # ISO date of the owner's live-pricing check
    verdict: PowerVerdict
    power: Mapping[str, Any]
    reasons: tuple[str, ...]
    sha256: str


def _canonical_sha256(doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _plan_doc(
    plan: ConfirmationPlan,
    header: Mapping[str, Any],
    *,
    plugin_version: str,
    price_confirmed_on: str,
) -> dict[str, Any]:
    spec = plan.spec
    if not plugin_version:
        raise PlanError("the plan needs the frozen plugin's version")
    try:
        dt.date.fromisoformat(price_confirmed_on)
    except (TypeError, ValueError) as exc:
        raise PlanError(
            f"price_confirmed_on must be an ISO date, got {price_confirmed_on!r}"
        ) from exc
    if header.get("stage") != "confirmation" or spec.name != "confirmation":
        raise PlanError("a plan is recorded only in a confirmation ledger, for a confirmation spec")
    if spec.model != header.get("model"):
        raise PlanError(
            f"plan model {spec.model!r} differs from the ledger's {header.get('model')!r}"
        )
    if spec.seeds != (GATE_SEED,):
        raise PlanError(f"confirmation runs seed {GATE_SEED} only, got {spec.seeds}")
    for k in ("plugin_hash", "claude_code_version"):
        if not isinstance(header.get(k), str) or not header[k]:
            raise PlanError(f"the ledger header has no {k}")
    if not spec.tasks or len(set(spec.tasks)) != len(spec.tasks):
        raise PlanError("the plan needs distinct tasks")
    return {
        "stage": spec.name,
        "tasks": list(spec.tasks),
        "arms": list(spec.arms),
        "seed": GATE_SEED,
        "model": spec.model,
        "claude_code_version": header["claude_code_version"],
        "plugin_hash": header["plugin_hash"],
        "plugin_version": plugin_version,
        "price_confirmed_on": price_confirmed_on,
        "verdict": plan.verdict,
        "power": plan.as_e4(),
        "reasons": list(plan.reasons),
    }


def write_plan(
    ledger: Ledger, plan: ConfirmationPlan, *, plugin_version: str, price_confirmed_on: str
) -> StagePlan:
    """Record the confirmation plan before the stage's first run (model, Claude Code version and
    plugin_hash from the ledger header, which pins them; the frozen plugin's version and the
    owner's live-pricing check date from the CLI). Idempotent for the same plan; a different
    plan, or any run already started, is refused. The scheduler then writes the plan hash into
    every confirmation start and end."""
    doc = _plan_doc(
        plan, ledger.header, plugin_version=plugin_version, price_confirmed_on=price_confirmed_on
    )
    digest = _canonical_sha256(doc)
    records = ledger.records()
    if any(r.get("kind") == PLAN_KIND for r in records):
        current = read_plan(ledger)
        if current.sha256 == digest:
            return current
        raise PlanError("a different confirmation plan is already recorded")
    if any(r.get("kind") == "start" for r in records):
        raise PlanError("runs already started: the plan must precede the first start")
    ledger.append({"kind": PLAN_KIND, "plan": doc, "plan_sha256": digest})
    from bench.anchors import record_anchor  # anchors imports grade, which imports ledger

    record_anchor(ledger)  # the plan anchor: the CLI publishes it before the first run
    return read_plan(ledger)


def _run_stage(rec: Mapping[str, Any]) -> str | None:
    key = rec.get("key")
    if rec.get("kind") not in ("start", "end") or not isinstance(key, str):
        return None
    parts = key.split("/")
    return parts[1] if len(parts) == 5 else None


def read_plan(ledger: Ledger) -> StagePlan:
    """The one recorded plan (ruling N1): its content hash verified, its pins equal to the
    header's, recorded before any confirmation run, and every confirmation start/end carrying
    its hash. A plan appended after runs (e.g. listing only the tasks that went well) is refused.

    The hash detects a late or edited plan inside the ledger, not a rewrite of the whole file:
    anchor ``sha256`` outside the ledger before the first run (rc1 freeze commit) and publish it
    with e4.json."""
    records = ledger.records()
    at = [i for i, r in enumerate(records) if r.get("kind") == PLAN_KIND]
    if not at:
        raise PlanError("no confirmation plan in the ledger")
    if len(at) > 1:
        raise PlanError("more than one confirmation plan in the ledger")
    doc, digest = records[at[0]].get("plan"), records[at[0]].get("plan_sha256")
    if not isinstance(doc, Mapping) or _canonical_sha256(doc) != digest:
        raise PlanError("the recorded plan does not match its content hash")
    runs = [(i, r) for i, r in enumerate(records) if _run_stage(r) == "confirmation"]
    if any(i < at[0] for i, _ in runs):
        raise PlanError("the plan was recorded after a confirmation run")
    other = sum(1 for _, r in runs if r.get("plan_sha256") != digest)
    if other:
        raise PlanError(f"{other} confirmation run record(s) carry another or no plan hash")
    header = ledger.header
    for k in ("model", "claude_code_version", "plugin_hash"):
        if doc.get(k) != header.get(k):
            raise PlanError(f"the recorded plan's {k} differs from the ledger header")
    try:
        verdict = doc["verdict"]
        if verdict not in ("powered", "underpowered"):
            raise PlanError(f"bad plan verdict {verdict!r}")
        return StagePlan(
            stage=str(doc["stage"]),
            tasks=tuple(str(t) for t in doc["tasks"]),
            arms=tuple(str(a) for a in doc["arms"]),
            seed=int(doc["seed"]),
            model=str(doc["model"]),
            claude_code_version=str(doc["claude_code_version"]),
            plugin_hash=str(doc["plugin_hash"]),
            plugin_version=str(doc["plugin_version"]),
            price_confirmed_on=str(doc["price_confirmed_on"]),
            verdict=verdict,
            power=dict(doc["power"]),
            reasons=tuple(str(r) for r in doc["reasons"]),
            sha256=str(digest),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError(f"malformed plan record: {exc}") from exc


# --------------------------------------------------------------------------- completeness


def is_final_exclusion(e: Exclusion) -> bool:
    """An exclusion that stays: unrunnable after retries, graded with a harness error, or an
    invalid run (not live / wrong hash). ``incomplete`` and not-yet-graded keys are not."""
    return e.reason in ("unrunnable", "invalid") or (
        e.reason == "ungraded" and e.cause == "harness_error"
    )


def restrict(summary: Summary, tasks: Collection[str]) -> tuple[Summary, int]:
    """``summary`` on ``tasks`` only, and how many runs/exclusions lay outside them."""
    keep = set(tasks)
    runs = tuple(r for r in summary.runs if r.instance_id in keep)
    excl = tuple(e for e in summary.exclusions if e.instance_id in keep)
    outside = len(summary.runs) - len(runs) + len(summary.exclusions) - len(excl)
    kept = tuple(t for t in summary.tasks if t in keep)
    return replace(summary, runs=runs, exclusions=excl, tasks=kept), outside


def missing(summary: Summary, arm: str, tasks: Sequence[str], seed: int) -> list[str]:
    """Planned tasks of ``arm`` at ``seed`` with neither a graded final run nor a final
    exclusion (never started, incomplete, or awaiting grading)."""
    have = {r.instance_id for r in summary.runs if r.arm == arm and r.seed == seed}
    have |= {
        e.instance_id
        for e in summary.exclusions
        if e.arm == arm and e.seed == seed and is_final_exclusion(e)
    }
    return [t for t in tasks if t not in have]


def invalid_as_exclusions(summary: Summary, arm: str, plugin_hash: str) -> Summary:
    """Runs of ``arm`` that are not live or not on ``plugin_hash`` become exclusions (reason
    ``invalid``): never evidence, and imputed by the gate's exclusion rule."""
    bad = [
        r
        for r in summary.runs
        if r.arm == arm and (r.live is not True or r.plugin_hash != plugin_hash)
    ]
    if not bad:
        return summary
    extra = tuple(
        Exclusion(
            r.instance_id,
            "invalid",
            r.arm,
            r.seed,
            "not_live" if r.live is not True else "plugin_hash",
        )
        for r in bad
    )
    ids = {id(r) for r in bad}
    return replace(
        summary,
        runs=tuple(r for r in summary.runs if id(r) not in ids),
        exclusions=summary.exclusions + extra,
    )


# --------------------------------------------------------------------------- output style


def style_rule(*, ledger: Ledger, n_boot: int = 10000, seed: int = 0) -> StyleVerdict:
    """Output-style recommendation (§6.6). Confirmatory status comes from the ledger (review
    ruling I2) and the summary is built from it (`summarise_ledger`, its exclusions and grades;
    ruling N2): its header stage must be ``confirmation`` and it must hold a
    valid plan listing ``ts-style``; never a caller flag. Only live ts-style runs on the plan's
    plugin_hash count (others are exclusions, imputed by the gate rule), on the planned tasks,
    and every planned task needs an outcome in ts-style, baseline and token-sparen.

    ``recommend_opt_in`` iff ts-style passes the confirmatory non-inferiority gate
    (`Summary.gate`, seed 0) vs baseline AND vs token-sparen, and its median cost ratio vs
    token-sparen has an upper bound < 1. Otherwise ``keep_off`` (screening numbers are still
    published as descriptive); ``not_run`` without any ts-style run. Never changes a default."""
    summary = summarise_ledger(ledger, stage=str(ledger.header.get("stage")))
    if not any(r.arm == STYLE_ARM for r in summary.runs):
        return "not_run"
    if summary.stage != "confirmation":
        return "keep_off"
    try:
        plan = read_plan(ledger)
    except PlanError:
        return "keep_off"
    if STYLE_ARM not in plan.arms:
        return "keep_off"
    scoped, _ = restrict(summary, plan.tasks)
    scoped = invalid_as_exclusions(scoped, STYLE_ARM, plan.plugin_hash)
    if any(missing(scoped, a, plan.tasks, plan.seed) for a in (STYLE_ARM, *CORE_ARMS)):
        return "keep_off"
    kw: dict[str, Any] = {"n_boot": n_boot, "seed": seed}
    try:
        ni = all(
            scoped.gate(STYLE_ARM, vs, gate_seed=plan.seed, **kw).passed
            for vs in (REFERENCE, TREATMENT)
        )
        ratio = scoped.cost_ratio(STYLE_ARM, TREATMENT, **kw)
    except (ValueError, ZeroDivisionError):
        return "keep_off"
    return "recommend_opt_in" if ni and ratio.hi < 1 else "keep_off"
