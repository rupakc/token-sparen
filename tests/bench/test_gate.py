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
from bench import grade as gr
from bench import stages
from bench.grade import grades_path
from bench.ledger import (
    CHAIN_FIELD,
    Ledger,
    LedgerError,
    RunKey,
    record_sha256,
    sha256_text,
)
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

    def grade(key: str, resolved: bool | None) -> None:
        grades.append(
            {"kind": "grade", "stage": stage, "key": key, "resolved": resolved,
             "patch_sha256": patch_sha(key)}
        )  # fmt: skip

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
            patch_sha256=patch_sha(key),
        )
        grade(key, r.resolved)
    for e in s.exclusions:
        key = f"c/{stage}/{e.arm}/{e.seed}/{e.instance_id}"
        ph = HASH if e.arm in stages.FROZEN_ARMS else None
        start(key, ph)
        if e.reason == "unrunnable":
            for _ in range(3):
                end(key, status="infra_fail", sub=e.cause or "api", repo="r/r")
        elif e.reason == "ungraded":
            end(key, status="ok", sub="success", run_cost=1.0, repo="r/r", model=MODEL,
                claude_code_version=VERSION, plugin_hash=ph, live=True,
                patch_sha256=patch_sha(key))  # fmt: skip
            if e.cause == "harness_error":
                grade(key, None)
    last: dict[str, Any] = (led.records() or [led.header])[-1]
    with led.path.open("a", encoding="utf-8") as fh:
        for rec in records:
            if stage == "confirmation":
                rec[CHAIN_FIELD] = record_sha256(last)
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            last = rec
    gr._append(grades_path(led), grades)
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


def patch_sha(key: str) -> str:
    return sha256_text(f"patch {key}\n")


def raw_append(led: Ledger, rec: dict[str, Any], *, chain: bool = True) -> None:
    """Write a record straight into the file (no `Ledger.append` checks), chained like the
    ledger would chain it when ``chain`` (what a careful forger could do)."""
    if chain and led.header.get("stage") == "confirmation":
        records = Ledger(led.path).records()
        rec = rec | {CHAIN_FIELD: record_sha256(records[-1] if records else led.header)}
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


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
    assert res.reasons == []
    assert res.as_e4() == {
        "verdict": "passed",
        "reasons": [],
        "plan_sha256": res.plan_sha256,
        "ledger_chain_head": res.ledger_chain_head,
        "grades_chain_head": res.grades_chain_head,
    }
    assert any("resolved-diff" in d for d in res.details)
    # the persisted plan's pins, for the report (Task 11)
    assert (res.plugin_hash, res.model, res.claude_code_version) == (HASH, MODEL, VERSION)
    assert (res.plugin_version, res.price_confirmed_on) == (PLUGIN_VERSION, PRICED)
    assert res.plan_sha256 is not None and len(res.plan_sha256) == 64
    # inferior → failed; cost never enters the gate
    bad = g.gate(ledger=store(_confirm(harmed=10)), **KW)
    assert bad.verdict == "failed" and "lower_bound_not_above_margin" in bad.reasons
    dear = replace(_confirm(), runs=tuple(replace(r, cost=9.0) for r in _confirm().runs))
    assert g.gate(ledger=store(dear), **KW).verdict == "passed"
    # E2 must hold
    e2 = g.gate(ledger=store(_confirm()), **(KW | {"e2_ok": False}))
    assert e2.verdict == "failed" and e2.reasons == ["e2_failed"]
    # every token-sparen run live
    dead = g.gate(ledger=store(_swap(_confirm(), 1, live=None)), **KW)
    assert dead.verdict == "failed" and dead.reasons == ["treatment_not_live"]
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
    assert other.verdict == "failed" and other.reasons == ["plugin_hash_mismatch"]
    missing = g.gate(ledger=store(_swap(_confirm(), 3, plugin_hash=None)), **KW)
    assert missing.verdict == "failed"
    assert g.gate(ledger=store(_swap(_confirm(), 0, plugin_hash="c" * 64)), **KW).verdict == (
        "failed"
    )
    # the plan's frozen plugin is not the release
    rel = g.gate(ledger=store(_confirm()), **(KW | {"release_hash": "d" * 64}))
    assert rel.verdict == "failed" and rel.reasons == ["plan_hash_not_release"]
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(ledger=store(_confirm()), **(KW | {"release_hash": ""}))
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(ledger=store(_confirm()), **(KW | {"release_hash": "A" * 64}))


def test_gate_fails_on_mixed_model_or_version(store: Store) -> None:
    model = g.gate(ledger=store(_swap(_confirm(), 0, model="claude-y")), **KW)
    assert model.verdict == "failed" and model.reasons == ["model_mismatch"]
    version = g.gate(ledger=store(_swap(_confirm(), 5, claude_code_version="2.1.1")), **KW)
    assert version.verdict == "failed" and version.reasons == ["claude_code_version_mismatch"]
    unknown = g.gate(ledger=store(_swap(_confirm(), 5, model=None)), **KW)
    assert unknown.verdict == "failed"
    # review I3(b): consistent but not the plan's pinned model / version
    wrong = g.gate(ledger=store(_confirm(model="claude-haiku-x")), **KW)
    assert wrong.verdict == "failed" and any("claude-haiku-x" in d for d in wrong.details)
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
    assert res.reasons[0] == "underpowered_owner_decides"
    assert "lower_bound_not_above_margin" in res.reasons
    assert any("gate-decision.toml" in d for d in res.details)
    # invalid evidence is never an owner decision: a hash mismatch still fails
    bad = g.gate(ledger=store(_swap(weak, 3, plugin_hash="b" * 64), verdict="underpowered"), **KW)
    assert bad.verdict == "failed"


def test_gate_clearly_inferior_fails_even_if_underpowered(store: Store) -> None:
    """Ruling M1: point < −5 pp and upper 95 % bound < 0 → failed, never an owner decision."""
    res = g.gate(ledger=store(_confirm(harmed=30), verdict="underpowered"), **KW)
    assert res.verdict == "failed" and res.reasons[0] == "clearly_inferior"


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


def test_reasons_are_codes(store: Store) -> None:
    """Closed vocabulary for e4.json (`bench.e4.GATE_REASON_CODES` once Task 11 merges)."""
    from bench import e4

    assert set(g.REASON_CODES) <= set(e4.GATE_REASON_CODES)
    cases = [
        g.gate(ledger=store(_confirm(harmed=30), verdict="underpowered"), **KW),
        g.gate(ledger=store(_swap(_confirm(harmed=4), 1, live=False)), **(KW | {"e2_ok": False})),
    ]
    for res in cases:
        assert res.reasons and set(res.reasons) <= set(g.REASON_CODES)
    assert len(set(g.REASON_CODES)) == len(g.REASON_CODES)


def _reroll(led: Ledger, keys: list[str], *, append_invalidate: bool = True) -> Ledger:
    """The reviewer's N3 probe: reopen graded token-sparen keys, re-run, regrade resolved."""
    plan = stages.read_plan(led)
    pins = {"plugin_hash": plan.plugin_hash, "plan_sha256": plan.sha256}
    for k in keys:
        if append_invalidate:
            led.append({"kind": "invalidate", "key": k, "reason": "infra"})
        led.append({"kind": "start", "key": k, "attempt": 2} | pins)
        led.append(
            {"kind": "end", "key": k, "attempt": 2, "status": "ok", "sub": "success",
             "run_cost": 0.8, "repo": "r/r", "model": MODEL, "claude_code_version": VERSION,
             "live": True, "patch_sha256": sha256_text(f"re-roll {k}")} | pins
        )  # fmt: skip
    gr._append(
        grades_path(led),
        [
            {"kind": "grade", "stage": "confirmation", "key": k, "resolved": True,
             "patch_sha256": sha256_text(f"re-roll {k}")}
            for k in keys
        ],
    )  # fmt: skip
    return Ledger(led.path)


def test_graded_runs_cannot_be_rerolled(store: Store) -> None:
    """Review N3 / ruling: in confirmation a graded run is never invalidated or re-run.
    The probe: 12 harmed token-sparen runs invalidated by key, re-run and regraded resolved
    turned `failed` into `passed`."""
    keys = [f"c/confirmation/token-sparen/0/r__r-{i}" for i in range(0, 24, 2)]
    led = store(_confirm(harmed=12))
    assert g.gate(ledger=led, **KW).verdict == "failed"
    # the ledger API refuses up front, by key and by time range
    with pytest.raises(LedgerError, match="graded"):
        led.invalidate(RunKey.from_id(keys[0]), reason="infra")
    with pytest.raises(LedgerError, match="graded"):
        led.invalidate(None, reason="infra", start=0.0, end=1e12)
    # appended directly, the re-roll is refused by the gate
    with pytest.raises(g.GateRefused, match="graded"):
        g.gate(ledger=_reroll(led, keys), **KW)
    # a range invalidation appended directly, too
    led2 = store(_confirm(harmed=12))
    led2.append({"kind": "invalidate", "from": 0.0, "to": 1e12, "reason": "infra"})
    with pytest.raises(g.GateRefused, match="graded"):
        g.gate(ledger=_reroll(led2, keys, append_invalidate=False), **KW)
    # a retry record reopening a graded key, too
    led3 = store(_confirm(harmed=12))
    for k in keys:
        led3.append({"kind": "retry", "key": k, "reason": "one_sided"})
    with pytest.raises(g.GateRefused, match="graded"):
        g.gate(ledger=_reroll(led3, keys, append_invalidate=False), **KW)
    # screening invalidations stay an operator tool
    scr = store(replace(_confirm(), stage="screening"))
    scr.invalidate(RunKey.from_id("c/screening/token-sparen/0/r__r-0"), reason="operator")


def test_infra_only_key_may_be_retried(store: Store) -> None:
    """A key that never had a graded outcome may still be invalidated and re-run."""
    s = replace(
        _drop(_confirm(), {"r__r-1"}, ("token-sparen",)),
        exclusions=(Exclusion("r__r-1", "incomplete", "token-sparen", 0, None),),
    )
    led = store(s)
    plan = stages.read_plan(led)
    k = "c/confirmation/token-sparen/0/r__r-1"
    end = {"kind": "end", "key": k, "attempt": 1, "status": "infra_fail", "sub": "docker"}
    led.append(end | {"plan_sha256": plan.sha256})
    led.invalidate(RunKey.from_id(k), reason="operator: docker restarted")
    res = g.gate(ledger=_reroll(led, [k], append_invalidate=False), **KW)
    assert res.verdict == "passed", res.details


HARMED = [f"c/confirmation/token-sparen/0/r__r-{i}" for i in range(0, 24, 2)]


def _rerun_raw(led: Ledger, keys: list[str]) -> None:
    plan = stages.read_plan(led)
    pins = {"plugin_hash": plan.plugin_hash, "plan_sha256": plan.sha256}
    for k in keys:
        raw_append(led, {"kind": "start", "key": k, "attempt": 2, "at": 9e9} | pins)
        raw_append(
            led,
            {"kind": "end", "key": k, "attempt": 2, "at": 9e9, "status": "ok", "sub": "success",
             "run_cost": 0.8, "repo": "r/r", "model": MODEL, "claude_code_version": VERSION,
             "live": True, "patch_sha256": sha256_text(f"re-roll {k}")} | pins,
        )  # fmt: skip


def test_second_graded_run_without_invalidate_is_refused(store: Store) -> None:
    """Review N4(a): a second start/end appended for a graded key (no invalidate record),
    regraded resolved, turned `failed` into `passed`."""
    led = store(_confirm(harmed=12))
    assert g.gate(ledger=led, **KW).verdict == "failed"
    with pytest.raises(LedgerError, match="graded"):  # the API refuses the second start
        led.append({"kind": "start", "key": HARMED[0], "attempt": 2})
    _rerun_raw(led, HARMED)
    gr._append(
        grades_path(led),
        [
            {"kind": "grade", "stage": "confirmation", "key": k, "resolved": True,
             "patch_sha256": sha256_text(f"re-roll {k}")}
            for k in HARMED
        ],
    )  # fmt: skip
    with pytest.raises(g.GateRefused, match="after its graded run"):
        g.gate(ledger=Ledger(led.path), **KW)
    # a second graded end alone (no start) too
    led2 = store(_confirm(harmed=12))
    end = next(r for r in led2.records() if r["kind"] == "end" and r["key"] == HARMED[0])
    raw_append(led2, {k: v for k, v in end.items() if k != CHAIN_FIELD} | {"at": 9e9})
    with pytest.raises(g.GateRefused, match="more than one graded run"):
        g.gate(ledger=Ledger(led2.path), **KW)


def test_second_grade_is_refused(store: Store) -> None:
    """Review N4(b): a second definite grade for the same key and patch (last one wins in
    `load_grades`) turned `failed` into `passed`."""
    led = store(_confirm(harmed=12))
    gr._append(
        grades_path(led),
        [
            {"kind": "grade", "stage": "confirmation", "key": k, "resolved": True,
             "patch_sha256": patch_sha(k)}
            for k in HARMED
        ],
    )  # fmt: skip
    with pytest.raises(g.GateRefused, match="more than one grade"):
        g.gate(ledger=led, **KW)


def test_grade_must_match_the_final_patch(store: Store) -> None:
    led = store(_confirm())
    other = [{"kind": "grade", "stage": "confirmation", "key": "c/confirmation/x/0/r__r-0",
              "resolved": True, "patch_sha256": patch_sha("x")}]  # fmt: skip
    gr._append(grades_path(led), other)
    with pytest.raises(g.GateRefused, match="grade"):
        g.gate(ledger=led, **KW)
    led2 = store(_confirm())
    path = grades_path(led2)
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    path.write_text("", encoding="utf-8")
    gr._append(path, [first | {"patch_sha256": "0" * 64}] + [json.loads(x) for x in lines[1:]])
    with pytest.raises(g.GateRefused, match="patch"):
        g.gate(ledger=led2, **KW)


def test_ledger_and_grades_are_hash_chained(store: Store) -> None:
    """Ruling N4(b): every confirmation record carries the sha256 of the previous one; the gate
    verifies both chains and returns their heads for e4.json."""
    led = store(_confirm())
    res = g.gate(ledger=led, **KW)
    assert res.ledger_chain_head == led.chain_head()
    assert res.grades_chain_head == gr.grades_chain_head(grades_path(led))
    assert res.as_e4()["ledger_chain_head"] == res.ledger_chain_head
    assert res.as_e4()["plan_sha256"] == res.plan_sha256
    # appending moves the head
    led.append({"kind": "pause", "reason": "weekly"})
    assert Ledger(led.path).chain_head() != res.ledger_chain_head
    # an edited record breaks the chain
    lines = led.path.read_text(encoding="utf-8").splitlines()
    i = next(n for n, x in enumerate(lines) if '"kind": "end"' in x and "token-sparen" in x)
    lines[i] = lines[i].replace('"run_cost": 0.8', '"run_cost": 0.7')
    led.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(g.GateRefused, match="chain"):
        g.gate(ledger=Ledger(led.path), **KW)
    # a deleted grade line breaks the grades chain
    led2 = store(_confirm())
    path = grades_path(led2)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:3] + lines[4:]) + "\n", encoding="utf-8")
    with pytest.raises(g.GateRefused, match="chain"):
        g.gate(ledger=led2, **KW)
    # an unchained record appended straight into the ledger
    led3 = store(_confirm())
    raw_append(led3, {"kind": "pause", "reason": "weekly"}, chain=False)
    with pytest.raises(g.GateRefused, match="chain"):
        g.gate(ledger=Ledger(led3.path), **KW)
