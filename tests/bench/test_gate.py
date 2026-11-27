"""Release gate (M5 Task 10): conservative non-inferiority on a complete, valid, frozen, live
confirmation, read from the ledger and its persisted plan (review rulings I1/I3/M1/N1/N2).

Scenarios are written as a `Summary` for brevity and persisted by `persist` as the ledger
records (plan, start/end with the plan hash, infra failures) and grades the scheduler and the
grader would write; the gate only ever sees the ledger."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bench import gate as g
from bench import stages
from bench.grade import grades_path
from bench.ledger import Ledger
from bench.power import FlipModel, PowerPlan
from bench.stats import Exclusion, RunRow, Summary

HASH = "a" * 64
MODEL, VERSION = "claude-x", "2.1.0"
PLUGIN_VERSION, PRICED = "1.0.0", "2026-11-20"
KW: dict[str, Any] = {"release_hash": HASH, "e2_ok": True, "n_boot": 200}


def _tasks(n: int) -> list[str]:
    return [f"r__r-{i}" for i in range(n)]


def _plan(tasks: Sequence[str], verdict: str, arms: tuple[str, ...]) -> stages.ConfirmationPlan:
    spec = stages.StageSpec("confirmation", arms, (), (0,), MODEL, tuple(tasks))
    power = PowerPlan(len(tasks), 1, 0.85, verdict)  # type: ignore[arg-type]
    flip = FlipModel(0.1, 0.05, 44, 0.08, 0.16)
    return stages.ConfirmationPlan(spec, verdict, power, flip, ())  # type: ignore[arg-type]


def make_ledger(
    path: Path,
    tasks: Sequence[str],
    *,
    verdict: str = "powered",
    stage: str = "confirmation",
    arms: tuple[str, ...] = stages.CORE_ARMS,
    write_plan: bool = True,
) -> Ledger:
    led = Ledger(
        path / "ledger.jsonl",
        header={
            "campaign": "c",
            "stage": stage,
            "harness_commit": "0" * 40,
            "plugin_hash": HASH,
            "plugin_ref": "v1.0.0-rc1",
            "pins": {},
            "model": MODEL,
            "claude_code_version": VERSION,
        },
    )
    if write_plan:
        stages.write_plan(
            led,
            _plan(tasks, verdict, arms),
            plugin_version=PLUGIN_VERSION,
            price_confirmed_on=PRICED,
        )
    return led


def persist(
    path: Path,
    s: Summary,
    *,
    plan_tasks: Sequence[str] | None = None,
    verdict: str = "powered",
    stage: str | None = None,
    write_plan: bool = True,
    plan_after_runs: bool = False,
    run_plan_sha: str | None = None,
) -> Ledger:
    """The ledger (+ grades file) a campaign producing ``s`` leaves behind."""
    stage = stage or s.stage
    seen = list(
        dict.fromkeys([*(r.instance_id for r in s.runs), *(e.instance_id for e in s.exclusions)])
    )
    arms = (*stages.CORE_ARMS, *(a for a in s.arms if a not in stages.CORE_ARMS))
    tasks = list(plan_tasks) if plan_tasks is not None else seen
    early = write_plan and not plan_after_runs and stage == "confirmation"
    led = make_ledger(path, tasks, verdict=verdict, stage=stage, arms=arms, write_plan=early)
    sha = run_plan_sha or (stages.read_plan(led).sha256 if early else None)
    extra = {"plan_sha256": sha} if sha else {}
    at = itertools.count(1)
    records: list[dict[str, Any]] = []
    grades: list[dict[str, Any]] = []

    def start(key: str, plugin_hash: str | None) -> None:
        records.append(
            {"kind": "start", "at": next(at), "key": key, "attempt": 1, "plugin_hash": plugin_hash}
            | extra
        )

    def end(key: str, **fields: Any) -> None:
        records.append({"kind": "end", "at": next(at), "key": key, "attempt": 1} | fields | extra)

    for r in s.runs:
        key = f"c/{stage}/{r.arm}/{r.seed}/{r.instance_id}"
        start(key, r.plugin_hash)
        end(
            key,
            status=r.status,
            sub="success",
            run_cost=r.cost,
            repo=r.repo,
            model=r.model,
            claude_code_version=r.claude_code_version,
            plugin_hash=r.plugin_hash,
            live=r.live,
        )
        grades.append({"kind": "grade", "stage": stage, "key": key, "resolved": r.resolved})
    for e in s.exclusions:
        key = f"c/{stage}/{e.arm}/{e.seed}/{e.instance_id}"
        ph = HASH if e.arm in stages.FROZEN_ARMS else None
        start(key, ph)
        if e.reason == "unrunnable":
            for _ in range(3):
                end(key, status="infra_fail", sub=e.cause or "api", repo="r/r")
        elif e.reason == "ungraded":
            end(key, status="ok", sub="success", run_cost=1.0, repo="r/r", model=MODEL,
                claude_code_version=VERSION, plugin_hash=ph, live=True)  # fmt: skip
            if e.cause == "harness_error":
                grades.append({"kind": "grade", "stage": stage, "key": key, "resolved": None})
    with led.path.open("a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
    with grades_path(led).open("w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in grades)
    led = Ledger(led.path)
    if plan_after_runs:  # what a direct `Ledger.append` could do, bypassing `write_plan`
        doc = stages._plan_doc(
            _plan(tasks, verdict, arms),
            led.header,
            plugin_version=PLUGIN_VERSION,
            price_confirmed_on=PRICED,
        )
        led.append({"kind": "plan", "plan": doc, "plan_sha256": stages._canonical_sha256(doc)})
    return led


Store = Callable[..., Ledger]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    n = itertools.count()
    return lambda s, **kw: persist(tmp_path / f"l{next(n)}", s, **kw)


def _confirm(n: int = 100, *, harmed: int = 0, **row: Any) -> Summary:
    """Baseline resolves every other task; token-sparen the same except ``harmed`` of them."""
    rows: list[RunRow] = []
    hit = 0
    common: dict[str, Any] = {"model": MODEL, "claude_code_version": VERSION, "live": True, **row}
    for i, iid in enumerate(_tasks(n)):
        base = i % 2 == 0
        ts = base
        if base and hit < harmed:
            ts, hit = False, hit + 1
        rows.append(RunRow("baseline", 0, iid, "r/r", "ok", 1.0, base, **common))
        rows.append(
            RunRow("token-sparen", 0, iid, "r/r", "ok", 0.8, ts, plugin_hash=HASH, **common)
        )
    arms = ("baseline", "token-sparen")
    return Summary("confirmation", "baseline", arms, (0,), tuple(_tasks(n)), tuple(rows), ())


def _swap(s: Summary, index: int, **changes: Any) -> Summary:
    runs = list(s.runs)
    runs[index] = replace(runs[index], **changes)
    return replace(s, runs=tuple(runs))


def _drop(s: Summary, tasks: set[str], arms: tuple[str, ...] = stages.CORE_ARMS) -> Summary:
    return replace(
        s, runs=tuple(r for r in s.runs if not (r.instance_id in tasks and r.arm in arms))
    )


def test_gate_passes_on_noninferior_valid_run(store: Store) -> None:
    res = g.gate(ledger=store(_confirm()), **KW)
    assert res.verdict == "passed", res.reasons
    assert res.check is not None and res.check.passed and res.check.estimate.n == 100
    assert res.as_e4() == {"verdict": "passed", "reasons": res.reasons}
    # the persisted plan's pins, for the report (Task 11)
    assert (res.plugin_hash, res.model, res.claude_code_version) == (HASH, MODEL, VERSION)
    assert (res.plugin_version, res.price_confirmed_on) == (PLUGIN_VERSION, PRICED)
    assert res.plan_sha256 is not None and len(res.plan_sha256) == 64
    # inferior → failed; cost never enters the gate
    bad = g.gate(ledger=store(_confirm(harmed=10)), **KW)
    assert bad.verdict == "failed" and any("lower bound" in r for r in bad.reasons)
    dear = replace(_confirm(), runs=tuple(replace(r, cost=9.0) for r in _confirm().runs))
    assert g.gate(ledger=store(dear), **KW).verdict == "passed"
    # E2 must hold
    e2 = g.gate(ledger=store(_confirm()), **(KW | {"e2_ok": False}))
    assert e2.verdict == "failed" and any("E2" in r for r in e2.reasons)
    # every token-sparen run live
    dead = g.gate(ledger=store(_swap(_confirm(), 1, live=None)), **KW)
    assert dead.verdict == "failed" and any("live" in r for r in dead.reasons)
    # exclusions after retries are complete: task-level ones are dropped, not refused
    ex = replace(
        _drop(_confirm(), {"r__r-1"}),
        exclusions=tuple(Exclusion("r__r-1", "unrunnable", a, 0, "api") for a in stages.CORE_ARMS),
    )
    res = g.gate(ledger=store(ex), **KW)
    assert res.verdict == "passed" and res.check is not None and res.check.estimate.n == 99


def test_gate_refuses_incomplete_campaign(store: Store) -> None:
    """Review I1: 5 of 100 planned tasks incomplete in both arms, or never started, used to
    pass under the 5 % size tolerance."""
    tail = set(_tasks(100)[95:])
    both = replace(
        _drop(_confirm(), tail),
        exclusions=tuple(
            Exclusion(t, "incomplete", a, 0, None) for t in sorted(tail) for a in stages.CORE_ARMS
        ),
    )
    with pytest.raises(g.GateRefused, match="5 planned"):
        g.gate(ledger=store(both), **KW)
    with pytest.raises(g.GateRefused, match="planned"):
        g.gate(ledger=store(_drop(_confirm(), tail), plan_tasks=_tasks(100)), **KW)
    with pytest.raises(g.GateRefused):  # one arm missing one task
        g.gate(ledger=store(_drop(_confirm(), {"r__r-7"}, ("token-sparen",))), **KW)
    ungraded = replace(
        _drop(_confirm(), {"r__r-7"}, ("baseline",)),
        exclusions=(Exclusion("r__r-7", "ungraded", "baseline", 0, "no_grade"),),
    )
    with pytest.raises(g.GateRefused):  # not graded yet
        g.gate(ledger=store(ungraded), **KW)


def test_gate_fails_on_hash_mismatch(store: Store) -> None:
    other = g.gate(ledger=store(_swap(_confirm(), 3, plugin_hash="b" * 64)), **KW)
    assert other.verdict == "failed" and any("plugin_hash" in r for r in other.reasons)
    missing = g.gate(ledger=store(_swap(_confirm(), 3, plugin_hash=None)), **KW)
    assert missing.verdict == "failed"
    assert g.gate(ledger=store(_swap(_confirm(), 0, plugin_hash="c" * 64)), **KW).verdict == (
        "failed"
    )
    # the plan's frozen plugin is not the release
    rel = g.gate(ledger=store(_confirm()), **(KW | {"release_hash": "d" * 64}))
    assert rel.verdict == "failed" and any("release" in r for r in rel.reasons)
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(ledger=store(_confirm()), **(KW | {"release_hash": ""}))
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(ledger=store(_confirm()), **(KW | {"release_hash": "A" * 64}))


def test_gate_fails_on_mixed_model_or_version(store: Store) -> None:
    model = g.gate(ledger=store(_swap(_confirm(), 0, model="claude-y")), **KW)
    assert model.verdict == "failed" and any("model" in r for r in model.reasons)
    version = g.gate(ledger=store(_swap(_confirm(), 5, claude_code_version="2.1.1")), **KW)
    assert version.verdict == "failed" and any("Claude Code" in r for r in version.reasons)
    unknown = g.gate(ledger=store(_swap(_confirm(), 5, model=None)), **KW)
    assert unknown.verdict == "failed"
    # review I3(b): consistent but not the plan's pinned model / version
    wrong = g.gate(ledger=store(_confirm(model="claude-haiku-x")), **KW)
    assert wrong.verdict == "failed" and any("claude-haiku-x" in r for r in wrong.reasons)
    wrong_v = g.gate(ledger=store(_confirm(claude_code_version="9.9.9")), **KW)
    assert wrong_v.verdict == "failed"


def test_gate_refuses_runs_outside_the_plan(store: Store) -> None:
    """Review I3(a)/N1: a run on a task outside the plan refuses the gate."""
    with pytest.raises(g.GateRefused, match="outside the plan"):
        g.gate(ledger=store(_confirm(100), plan_tasks=_tasks(100)[:80]), **KW)


def test_plan_appended_after_runs_cannot_cherry_pick(store: Store) -> None:
    """Review N1 probe: runs of 100 tasks with 12 harmed, then a plan listing tasks [24:]
    appended directly (bypassing `write_plan`) turned `failed` into `passed`."""
    harmed = _confirm(harmed=12)
    assert g.gate(ledger=store(harmed), **KW).verdict == "failed"
    late = store(harmed, plan_tasks=_tasks(100)[24:], plan_after_runs=True)
    with pytest.raises(stages.PlanError, match="after"):
        stages.read_plan(late)
    with pytest.raises(g.GateRefused, match="after"):
        g.gate(ledger=late, **KW)
    # runs carrying another plan's hash
    foreign = store(_confirm(), run_plan_sha="f" * 64)
    with pytest.raises(g.GateRefused, match="plan hash"):
        g.gate(ledger=foreign, **KW)


def test_gate_reads_only_the_ledger() -> None:
    """Review N2 / I3(c): no caller-supplied summary or verdict."""
    with pytest.raises(TypeError):
        g.gate(_confirm(), **KW)  # type: ignore[misc]
    with pytest.raises(TypeError):
        g.gate(ledger=None, verdict="underpowered", **KW)  # type: ignore[call-arg, arg-type]


def test_gate_underpowered_is_not_failed(store: Store) -> None:
    weak = _confirm(harmed=4)  # inconclusive: would fail a powered gate
    assert g.gate(ledger=store(weak), **KW).verdict == "failed"
    res = g.gate(ledger=store(weak, verdict="underpowered"), **KW)
    assert res.verdict == "underpowered", res.reasons
    assert any("owner" in r for r in res.reasons)
    assert any("gate-decision.toml" in r for r in res.reasons)
    # invalid evidence is never an owner decision: a hash mismatch still fails
    bad = g.gate(ledger=store(_swap(weak, 3, plugin_hash="b" * 64), verdict="underpowered"), **KW)
    assert bad.verdict == "failed"


def test_gate_clearly_inferior_fails_even_if_underpowered(store: Store) -> None:
    """Ruling M1: point < −5 pp and upper 95 % bound < 0 → failed, never an owner decision."""
    res = g.gate(ledger=store(_confirm(harmed=30), verdict="underpowered"), **KW)
    assert res.verdict == "failed" and any("clearly inferior" in r for r in res.reasons)


def test_gate_refuses_pending_reruns_and_wrong_stage(store: Store) -> None:
    s = _confirm()
    pending = replace(
        _drop(s, {"r__r-0"}, ("token-sparen",)),
        exclusions=(Exclusion("r__r-0", "incomplete", "token-sparen", 0, None),),
    )
    with pytest.raises(g.GateRefused, match="re-run"):
        g.gate(ledger=store(pending), **KW)
    with pytest.raises(g.GateRefused, match="confirmation"):
        g.gate(ledger=store(replace(s, stage="screening")), **KW)
    with pytest.raises(g.GateRefused, match="plan"):
        g.gate(ledger=store(s, write_plan=False), **KW)


def test_gate_scope_text() -> None:
    assert "patch" in g.SCOPE and "plugin_hash" in g.SCOPE and "defaults" in g.SCOPE
