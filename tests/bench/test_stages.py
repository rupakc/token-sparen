"""Stages (M5 Task 10): screening, power-sized confirmation with quota/disk extras, style."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bench import power, stages
from bench.ledger import Ledger
from bench.stats import Exclusion, RunRow, Summary
from bench.tasks import Task, arm64_image, load_pool
from tests.bench.test_gate import HASH, make_ledger

# small simulation budgets keep the suite fast (the controller-run power step uses the defaults)
FAST: dict[str, Any] = {"n_sim": 60, "n_boot": 120}
ROOMY_QUOTA = stages.QuotaProjection(pct_per_run_weekly=0.01, runs_per_week=1e6)
ROOMY_DISK = stages.DiskProjection(gb_per_image=5.0, gb_kept=100.0, free_gb=1000.0, gb_per_run=0.0)


def _pool(n: int, mini: int = 44) -> list[Task]:
    out = []
    for i in range(n):
        repo = ("alpha/one", "beta/two", "gamma/three")[i % 3]
        iid = repo.replace("/", "__") + f"-{1000 + i}"
        created = f"2021-01-{1 + i % 28:02d}T00:00:{i % 60:02d}Z"
        out.append(Task(iid, repo, "0" * 40, created, arm64_image(iid), mini=i < mini))
    return out


def _summary(
    stage: str,
    outcomes: Mapping[str, Sequence[bool]],
    costs: Mapping[str, Sequence[float]],
    *,
    exclusions: Sequence[Exclusion] = (),
) -> Summary:
    rows = []
    for arm, res in outcomes.items():
        for i, ok in enumerate(res):
            rows.append(
                RunRow(
                    arm, 0, f"r__r-{i}", "r/r", "ok", costs[arm][i], ok, model="m", live=True,
                    plugin_hash=HASH if arm in stages.FROZEN_ARMS else None,
                )
            )  # fmt: skip
    tasks = tuple(sorted({r.instance_id for r in rows}))
    return Summary(
        stage, "baseline", tuple(sorted(outcomes)), (0,), tasks, tuple(rows), tuple(exclusions)
    )


def _screen(n: int = 44, **arms: tuple[float, int]) -> Summary:
    """Every arm resolves the baseline pattern (every other task) except ``drop`` of them, at
    ``ratio`` × the baseline cost (costs vary by task so the intervals are not degenerate)."""
    base = [i % 2 == 0 for i in range(n)]
    base_cost = [1.0 + 0.01 * i for i in range(n)]
    spec: dict[str, tuple[float, int]] = {"baseline": (1.0, 0), "token-sparen": (0.8, 0)}
    spec.update({k.replace("_", "-"): v for k, v in arms.items()})
    outcomes: dict[str, list[bool]] = {}
    costs: dict[str, list[float]] = {}
    for arm, (ratio, drop) in spec.items():
        res = list(base)
        hit = 0
        for i in range(n):
            if hit < drop and res[i]:
                res[i] = False
                hit += 1
        outcomes[arm] = res
        costs[arm] = [c * ratio * (1 + 0.001 * (i % 3)) for i, c in enumerate(base_cost)]
    return _summary("screening", outcomes, costs)


SCREEN = _screen(
    rtk=(0.6, 0),  # eligible, ties tokeneconomizer on cost → rtk by name
    tokeneconomizer=(0.6, 0),
    headroom=(0.4, 5),  # cheapest but resolved-diff point −5/44 < −0.05 → not eligible
    ts_style=(0.6, 0),  # vs token-sparen: same resolved, ratio 0.75 < 1 → screening rule held
)


def test_screening_spec() -> None:
    s = stages.SCREENING
    assert s.name == "screening"
    assert s.arms == (
        "baseline", "concise", "token-sparen", "ts-style", "ts-cold", "rtk", "headroom",
        "tokeneconomizer",
    )  # fmt: skip
    assert s.optional_arms == ("caveman", "context-mode")
    assert s.seeds == (0,) and s.model == "sonnet"
    mini = [t.instance_id for t in load_pool() if t.mini]
    assert s.tasks == tuple(mini) and len(s.tasks) == 44  # Mini minus the 6 without arm64 images
    assert s.runs == 8 * 44


def test_size_from_power() -> None:
    pool = _pool(300)
    spec, verdict = stages.confirmation_spec(
        SCREEN, pool=pool, quota=ROOMY_QUOTA, disk=ROOMY_DISK, model="m-1", **FAST
    )
    plan = power.plan(power.flip_model(SCREEN), pool=len(pool), **FAST)
    assert verdict == "powered" and plan.verdict == "powered"
    assert plan.required_tasks is not None and plan.required_tasks >= power.MIN_PLAN_TASKS
    assert len(spec.tasks) == plan.required_tasks
    mini = [t.instance_id for t in pool if t.mini]
    assert spec.tasks[: len(mini)] == tuple(mini)  # Mini first, then the seeded extension
    assert len(set(spec.tasks)) == len(spec.tasks)
    assert spec.name == "confirmation" and spec.seeds == (0,) and spec.model == "m-1"
    full = stages.plan_confirmation(
        SCREEN, pool=pool, quota=ROOMY_QUOTA, disk=ROOMY_DISK, model="m-1", **FAST
    )
    assert full.spec == spec and full.power.as_e4()["required_tasks"] == len(spec.tasks)
    assert full.as_e4() == full.power.as_e4() | {"verdict": "powered"}
    # deterministic: the same screening, pool and seed give the same task list
    again, _ = stages.confirmation_spec(
        SCREEN, pool=pool, quota=ROOMY_QUOTA, disk=ROOMY_DISK, model="m-1", **FAST
    )
    assert again == spec


def test_confirmation_core_arms_then_quota_extras() -> None:
    pool = _pool(300)
    kw: dict[str, Any] = {"pool": pool, "disk": ROOMY_DISK, "model": "m", **FAST}
    spec, verdict = stages.confirmation_spec(SCREEN, quota=ROOMY_QUOTA, **kw)
    assert verdict == "powered"
    assert spec.arms == ("baseline", "token-sparen", "rtk", "ts-style")
    assert spec.optional_arms == ()
    n = len(spec.tasks)

    def quota_for(arms: int) -> stages.QuotaProjection:  # exactly `arms` arms fit 6 weeks
        return stages.QuotaProjection(pct_per_run_weekly=0.01, runs_per_week=n * arms / 6)

    assert stages.confirmation_spec(SCREEN, quota=quota_for(3), **kw)[0].arms == (
        "baseline", "token-sparen", "rtk",
    )  # fmt: skip
    core, v2 = stages.confirmation_spec(SCREEN, quota=quota_for(2), **kw)
    assert core.arms == ("baseline", "token-sparen") and v2 == "powered"
    # the 60 % weekly stop bounds the runs per week: 60 / 0.6 = 100 runs a week
    slow = stages.QuotaProjection(pct_per_run_weekly=60 / (n * 3 / 6), runs_per_week=1e6)
    assert stages.confirmation_spec(SCREEN, quota=slow, **kw)[0].arms[-1] == "rtk"
    # no eligible competitor → ts-style still considered; failed style screening → not added
    no_comp = _screen(headroom=(0.4, 5), ts_style=(0.6, 0))
    assert stages.confirmation_spec(no_comp, quota=ROOMY_QUOTA, **kw)[0].arms == (
        "baseline", "token-sparen", "ts-style",
    )  # fmt: skip
    dear_style = _screen(rtk=(0.6, 0), ts_style=(0.9, 0))  # 0.9/0.8 > 1 vs token-sparen
    assert stages.best_competitor(dear_style, n_boot=50) == "rtk"
    assert not stages.style_screen_held(dear_style, n_boot=50)
    assert stages.confirmation_spec(dear_style, quota=ROOMY_QUOTA, **kw)[0].arms == (
        "baseline", "token-sparen", "rtk",
    )  # fmt: skip
    worse_style = _screen(ts_style=(0.5, 4))  # 4/44 below token-sparen → rule not held
    assert not stages.style_screen_held(worse_style, n_boot=50)
    assert stages.best_competitor(SCREEN, n_boot=50) == "rtk"


def test_underpowered_when_pool_or_quota_short() -> None:
    small = _pool(70)  # below the 80-task planning floor: no size reaches 80 %
    spec, verdict = stages.confirmation_spec(
        SCREEN, pool=small, quota=ROOMY_QUOTA, disk=ROOMY_DISK, model="m", **FAST
    )
    assert verdict == "underpowered"
    assert spec.arms == ("baseline", "token-sparen")
    assert len(spec.tasks) == 70  # the whole pool is the best that can be run
    plan = stages.plan_confirmation(
        SCREEN, pool=small, quota=ROOMY_QUOTA, disk=ROOMY_DISK, model="m", **FAST
    )
    assert plan.as_e4()["verdict"] == "underpowered"
    assert any("pool" in r for r in plan.reasons)

    tight = stages.QuotaProjection(pct_per_run_weekly=0.01, runs_per_week=10)
    spec, verdict = stages.confirmation_spec(
        SCREEN, pool=_pool(300), quota=tight, disk=ROOMY_DISK, model="m", **FAST
    )
    assert verdict == "underpowered"
    assert spec.arms == ("baseline", "token-sparen")  # no extras when the core does not fit
    plan = stages.plan_confirmation(
        SCREEN, pool=_pool(300), quota=tight, disk=ROOMY_DISK, model="m", **FAST
    )
    assert plan.as_e4()["verdict"] == "underpowered" and plan.power.verdict == "powered"
    assert any("quota" in r for r in plan.reasons)


def test_disk_projection_in_sizing() -> None:
    spec = replace(stages.SCREENING, arms=("baseline", "token-sparen"), tasks=("a", "b"))
    assert spec.runs == 4
    # free − kept − one live image − growth ≥ 30 GB
    ok = stages.DiskProjection(gb_per_image=10, gb_kept=60, free_gb=100, gb_per_run=0)
    assert stages.fits(spec, ROOMY_QUOTA, ok)
    assert not stages.fits(spec, ROOMY_QUOTA, replace(ok, free_gb=99.9))
    assert not stages.fits(spec, ROOMY_QUOTA, replace(ok, gb_per_run=0.1))
    assert stages.fits(spec, ROOMY_QUOTA, replace(ok, free_gb=100.5, gb_per_run=0.1))
    assert stages.fits(spec, stages.QuotaProjection(0.01, 4 / 6), ok)
    assert not stages.fits(spec, stages.QuotaProjection(0.01, 4 / 6), ok, weeks=5)

    pool = _pool(300)
    kw: dict[str, Any] = {"pool": pool, "quota": ROOMY_QUOTA, "model": "m", **FAST}
    full, _ = stages.confirmation_spec(SCREEN, disk=ROOMY_DISK, **kw)
    n = len(full.tasks)
    # workspace growth: enough disk for the core arms only
    per_run = stages.DiskProjection(
        gb_per_image=5, gb_kept=100, free_gb=135.5 + n * 2 * 0.01, gb_per_run=0.01
    )
    core, verdict = stages.confirmation_spec(SCREEN, disk=per_run, **kw)
    assert core.arms == ("baseline", "token-sparen") and verdict == "powered"
    # not even the core fits above the 30 GB floor → underpowered
    full_disk = stages.DiskProjection(gb_per_image=5, gb_kept=100, free_gb=130, gb_per_run=0)
    assert stages.confirmation_spec(SCREEN, disk=full_disk, **kw)[1] == "underpowered"


def test_projection_validation() -> None:
    with pytest.raises(ValueError):
        stages.QuotaProjection(pct_per_run_weekly=0, runs_per_week=10)
    with pytest.raises(ValueError):
        stages.QuotaProjection(pct_per_run_weekly=1, runs_per_week=-1)
    with pytest.raises(ValueError):
        stages.DiskProjection(gb_per_image=-1, gb_kept=0, free_gb=10, gb_per_run=0)
    q = stages.QuotaProjection(pct_per_run_weekly=2.5, runs_per_week=100)
    assert q.weekly_runs() == 24  # 60 % stop / 2.5 % per run
    assert q.weeks_needed(48) == 2


def _confirm(n: int = 100, **arms: tuple[float, int]) -> Summary:
    return replace(_screen(n, **arms), stage="confirmation")


def _tasks(n: int) -> list[str]:
    return [f"r__r-{i}" for i in range(n)]


FLIP = power.FlipModel(0.1, 0.05, 44, 0.08, 0.16)


def _power(n: int) -> power.PowerPlan:
    return power.PowerPlan(n, 1, 0.85, "powered")


def test_style_needs_confirmation_evidence(tmp_path: Path) -> None:
    kw: dict[str, Any] = {"n_boot": 200}
    led = make_ledger(tmp_path, _tasks(100), arms=(*stages.CORE_ARMS, "ts-style"))
    screen_led = make_ledger(tmp_path / "s", _tasks(44), stage="screening", write_plan=False)
    # screening only: never recommended, even with excellent descriptive numbers
    assert stages.style_rule(SCREEN, ledger=screen_led, **kw) == "keep_off"
    good = _confirm(ts_style=(0.6, 0))
    assert stages.style_rule(good, ledger=led, **kw) == "recommend_opt_in"
    # review I2: confirmatory status comes from the ledger's stage record, never the caller
    assert stages.style_rule(good, ledger=screen_led, **kw) == "keep_off"
    assert stages.style_rule(replace(good, stage="screening"), ledger=led, **kw) == "keep_off"
    with pytest.raises(TypeError):
        stages.style_rule(good, confirmatory=True)  # type: ignore[call-arg]
    # not cheaper than token-sparen (upper bound ≥ 1) → keep off
    assert stages.style_rule(_confirm(ts_style=(0.8, 0)), ledger=led, **kw) == "keep_off"
    # inferior vs baseline and token-sparen → keep off
    assert stages.style_rule(_confirm(ts_style=(0.6, 12)), ledger=led, **kw) == "keep_off"
    # not run in this stage
    assert stages.style_rule(_confirm(), ledger=led, **kw) == "not_run"
    assert stages.style_rule(_confirm(), ledger=screen_led, **kw) == "not_run"


def test_style_counts_only_live_hash_verified_runs(tmp_path: Path) -> None:
    """Review I2: a non-live or unhashed ts-style run is excluded (imputed by the gate rule),
    never counted as evidence; planned tasks without a ts-style outcome keep it off."""
    kw: dict[str, Any] = {"n_boot": 200}
    led = make_ledger(tmp_path, _tasks(100), arms=(*stages.CORE_ARMS, "ts-style"))
    good = _confirm(ts_style=(0.6, 0))

    def spoil(s: Summary, **changes: Any) -> Summary:
        return replace(
            s, runs=tuple(replace(r, **changes) if r.arm == "ts-style" else r for r in s.runs)
        )

    assert stages.style_rule(spoil(good, live=False), ledger=led, **kw) == "keep_off"
    assert stages.style_rule(spoil(good, plugin_hash=None), ledger=led, **kw) == "keep_off"
    assert stages.style_rule(spoil(good, plugin_hash="b" * 64), ledger=led, **kw) == "keep_off"
    # a few spoiled runs on resolved tasks are imputed as ts-style failures
    runs = list(good.runs)
    spoiled = 0
    for i, r in enumerate(runs):
        if r.arm == "ts-style" and r.resolved and spoiled < 6:
            runs[i], spoiled = replace(r, live=False), spoiled + 1
    assert stages.style_rule(replace(good, runs=tuple(runs)), ledger=led, **kw) == "keep_off"
    # incomplete ts-style evidence (a planned task without a run) → keep off
    short = replace(
        good,
        runs=tuple(r for r in good.runs if not (r.arm == "ts-style" and r.instance_id == "r__r-3")),
    )
    assert stages.style_rule(short, ledger=led, **kw) == "keep_off"


def test_plan_persisted_with_content_hash(tmp_path: Path) -> None:
    """Review I3: the confirmation plan is written to the ledger at confirmation start."""
    led = make_ledger(tmp_path, _tasks(90))
    plan = stages.read_plan(led)
    assert plan.tasks == tuple(_tasks(90)) and plan.seed == 0 and plan.verdict == "powered"
    assert plan.model == "claude-x" and plan.claude_code_version == "2.1.0"
    assert plan.plugin_hash == HASH and len(plan.sha256) == 64
    assert plan.arms == stages.CORE_ARMS and plan.power["required_tasks"] == 90
    # idempotent for the same plan, refused for a different one
    spec = stages.StageSpec(
        "confirmation", stages.CORE_ARMS, (), (0,), "claude-x", tuple(_tasks(90))
    )
    same = stages.ConfirmationPlan(spec, "powered", _power(90), FLIP, ())
    assert stages.write_plan(led, same) == plan
    with pytest.raises(stages.PlanError, match="different"):
        stages.write_plan(led, replace(same, spec=replace(spec, tasks=tuple(_tasks(91)))))
    # tampering is detected
    lines = led.path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"r__r-5"', '"r__r-500"')
    led.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(stages.PlanError, match="hash"):
        stages.read_plan(Ledger(led.path))


def test_plan_refused_after_runs_or_on_mismatch(tmp_path: Path) -> None:
    led = make_ledger(tmp_path, _tasks(90), write_plan=False)
    spec = stages.StageSpec(
        "confirmation", stages.CORE_ARMS, (), (0,), "other-model", tuple(_tasks(90))
    )
    plan = stages.ConfirmationPlan(spec, "powered", _power(90), FLIP, ())
    with pytest.raises(stages.PlanError, match="model"):
        stages.write_plan(led, plan)
    led.append({"kind": "start", "key": "c/confirmation/baseline/0/r__r-0", "attempt": 1})
    with pytest.raises(stages.PlanError, match="start"):
        stages.write_plan(led, replace(plan, spec=replace(spec, model="claude-x")))
    with pytest.raises(stages.PlanError, match="no confirmation plan"):
        stages.read_plan(led)
    screen = make_ledger(tmp_path / "s", _tasks(44), stage="screening", write_plan=False)
    with pytest.raises(stages.PlanError, match="confirmation"):
        stages.write_plan(screen, replace(plan, spec=replace(spec, model="claude-x")))


def test_quota_retry_inflation_and_bound_reason() -> None:
    """Minors M2/M3: retries inflate the projection; the reason names the binding limit."""
    q = stages.QuotaProjection(pct_per_run_weekly=2.5, runs_per_week=100, retry_rate=0.5)
    assert q.weeks_needed(48) == 3  # 72 attempts at 24 a week
    pool = _pool(300)
    tight = stages.QuotaProjection(pct_per_run_weekly=0.01, runs_per_week=10)
    plan = stages.plan_confirmation(
        SCREEN, pool=pool, quota=tight, disk=ROOMY_DISK, model="m", **FAST
    )
    assert any("quota" in r for r in plan.reasons) and not any("disk" in r for r in plan.reasons)
    small_disk = stages.DiskProjection(gb_per_image=5, gb_kept=100, free_gb=130, gb_per_run=0)
    plan = stages.plan_confirmation(
        SCREEN, pool=pool, quota=ROOMY_QUOTA, disk=small_disk, model="m", **FAST
    )
    assert any("disk" in r for r in plan.reasons) and not any("quota" in r for r in plan.reasons)
