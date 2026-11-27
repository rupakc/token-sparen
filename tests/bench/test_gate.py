"""Release gate (M5 Task 10): conservative non-inferiority on a complete, valid, frozen, live
confirmation, bound to the plan persisted in the ledger (review rulings I1/I3/M1)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bench import gate as g
from bench import stages
from bench.ledger import Ledger
from bench.power import FlipModel, PowerPlan
from bench.stats import Exclusion, RunRow, Summary

HASH = "a" * 64
MODEL, VERSION = "claude-x", "2.1.0"
KW: dict[str, Any] = {"release_hash": HASH, "e2_ok": True, "n_boot": 200}


def _tasks(n: int) -> list[str]:
    return [f"r__r-{i}" for i in range(n)]


def make_ledger(
    path: Path,
    tasks: list[str],
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
        spec = stages.StageSpec("confirmation", arms, (), (0,), MODEL, tuple(tasks))
        power = PowerPlan(len(tasks), 1, 0.85, verdict)  # type: ignore[arg-type]
        flip = FlipModel(0.1, 0.05, 44, 0.08, 0.16)
        stages.write_plan(led, stages.ConfirmationPlan(spec, verdict, power, flip, ()))  # type: ignore[arg-type]
    return led


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


@pytest.fixture
def led(tmp_path: Path) -> Ledger:
    return make_ledger(tmp_path, _tasks(100))


def test_gate_passes_on_noninferior_valid_run(led: Ledger) -> None:
    res = g.gate(_confirm(), ledger=led, **KW)
    assert res.verdict == "passed", res.reasons
    assert res.check is not None and res.check.passed
    assert res.as_e4() == {"verdict": "passed", "reasons": res.reasons}
    # inferior → failed; cost never enters the gate
    bad = g.gate(_confirm(harmed=10), ledger=led, **KW)
    assert bad.verdict == "failed" and any("lower bound" in r for r in bad.reasons)
    dear = replace(_confirm(), runs=tuple(replace(r, cost=9.0) for r in _confirm().runs))
    assert g.gate(dear, ledger=led, **KW).verdict == "passed"
    # E2 must hold
    e2 = g.gate(_confirm(), ledger=led, **(KW | {"e2_ok": False}))
    assert e2.verdict == "failed" and any("E2" in r for r in e2.reasons)
    # every token-sparen run live
    dead = g.gate(_swap(_confirm(), 1, live=None), ledger=led, **KW)
    assert dead.verdict == "failed" and any("live" in r for r in dead.reasons)
    # recorded exclusions after retries are complete: task-level ones are dropped, not refused
    ex = _drop(_confirm(), {"r__r-1"})
    ex = replace(
        ex,
        exclusions=tuple(Exclusion("r__r-1", "unrunnable", a, 0, "api") for a in stages.CORE_ARMS),
    )
    assert g.gate(ex, ledger=led, **KW).verdict == "passed"


def test_gate_refuses_incomplete_campaign(led: Ledger) -> None:
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
        g.gate(both, ledger=led, **KW)
    with pytest.raises(g.GateRefused, match="planned"):
        g.gate(_drop(_confirm(), tail), ledger=led, **KW)
    with pytest.raises(g.GateRefused):  # one arm missing one task
        g.gate(_drop(_confirm(), {"r__r-7"}, ("token-sparen",)), ledger=led, **KW)
    ungraded = replace(
        _drop(_confirm(), {"r__r-7"}, ("baseline",)),
        exclusions=(Exclusion("r__r-7", "ungraded", "baseline", 0, "no_grade"),),
    )
    with pytest.raises(g.GateRefused):  # not graded yet
        g.gate(ungraded, ledger=led, **KW)


def test_gate_fails_on_hash_mismatch(led: Ledger, tmp_path: Path) -> None:
    other = g.gate(_swap(_confirm(), 3, plugin_hash="b" * 64), ledger=led, **KW)
    assert other.verdict == "failed" and any("plugin_hash" in r for r in other.reasons)
    missing = g.gate(_swap(_confirm(), 3, plugin_hash=None), ledger=led, **KW)
    assert missing.verdict == "failed"
    assert g.gate(_swap(_confirm(), 0, plugin_hash="c" * 64), ledger=led, **KW).verdict == "failed"
    # the plan's frozen plugin is not the release
    rel = g.gate(_confirm(), ledger=led, **(KW | {"release_hash": "d" * 64}))
    assert rel.verdict == "failed" and any("release" in r for r in rel.reasons)
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(_confirm(), ledger=led, **(KW | {"release_hash": ""}))
    with pytest.raises(ValueError, match="release_hash"):
        g.gate(_confirm(), ledger=led, **(KW | {"release_hash": "A" * 64}))


def test_gate_fails_on_mixed_model_or_version(led: Ledger) -> None:
    model = g.gate(_swap(_confirm(), 0, model="claude-y"), ledger=led, **KW)
    assert model.verdict == "failed" and any("model" in r for r in model.reasons)
    version = g.gate(_swap(_confirm(), 5, claude_code_version="2.1.1"), ledger=led, **KW)
    assert version.verdict == "failed" and any("Claude Code" in r for r in version.reasons)
    unknown = g.gate(_swap(_confirm(), 5, model=None), ledger=led, **KW)
    assert unknown.verdict == "failed"
    # review I3(b): consistent but not the plan's pinned model / version
    wrong = g.gate(_confirm(model="claude-haiku-x"), ledger=led, **KW)
    assert wrong.verdict == "failed" and any("claude-haiku-x" in r for r in wrong.reasons)
    wrong_v = g.gate(_confirm(claude_code_version="9.9.9"), ledger=led, **KW)
    assert wrong_v.verdict == "failed"


def test_gate_ignores_and_flags_tasks_outside_plan(tmp_path: Path) -> None:
    """Review I3(a): substitute or extra tasks never count towards the gate."""
    led = make_ledger(tmp_path, _tasks(100)[:80])
    res = g.gate(_confirm(100), ledger=led, **KW)
    assert res.check is not None and res.check.estimate.n == 80
    assert any("outside the plan" in r for r in res.reasons)
    # a harmed task outside the plan does not move the result
    s = _confirm(100)
    rows = [
        r if r.instance_id != "r__r-90" or r.arm == "baseline" else replace(r, resolved=False)
        for r in s.runs
    ]
    again = g.gate(replace(s, runs=tuple(rows)), ledger=led, **KW)
    assert again.check is not None and again.check.estimate == res.check.estimate


def test_gate_verdict_comes_from_the_plan(tmp_path: Path) -> None:
    """Review I3(c): the caller cannot turn a powered plan's fail into an owner decision."""
    with pytest.raises(TypeError):
        g.gate(_confirm(), verdict="underpowered", ledger=None, **KW)  # type: ignore[call-arg]
    led = make_ledger(tmp_path, _tasks(100))
    assert g.gate(_confirm(harmed=10), ledger=led, **KW).verdict == "failed"


def test_gate_underpowered_is_not_failed(tmp_path: Path) -> None:
    led = make_ledger(tmp_path, _tasks(100), verdict="underpowered")
    weak = _confirm(harmed=4)  # inconclusive: would fail a powered gate
    res = g.gate(weak, ledger=led, **KW)
    assert res.verdict == "underpowered", res.reasons
    assert any("owner" in r for r in res.reasons)
    assert any("gate-decision.toml" in r for r in res.reasons)
    # invalid evidence is never an owner decision: a hash mismatch still fails
    bad = g.gate(_swap(weak, 3, plugin_hash="b" * 64), ledger=led, **KW)
    assert bad.verdict == "failed"


def test_gate_clearly_inferior_fails_even_if_underpowered(tmp_path: Path) -> None:
    """Ruling M1: point < −5 pp and upper 95 % bound < 0 → failed, never an owner decision."""
    led = make_ledger(tmp_path, _tasks(100), verdict="underpowered")
    res = g.gate(_confirm(harmed=30), ledger=led, **KW)
    assert res.verdict == "failed" and any("clearly inferior" in r for r in res.reasons)


def test_gate_refuses_pending_reruns_and_wrong_stage(led: Ledger, tmp_path: Path) -> None:
    s = _confirm()
    pending = replace(
        _drop(s, {"r__r-0"}, ("token-sparen",)),
        exclusions=(Exclusion("r__r-0", "incomplete", "token-sparen", 0, None),),
    )
    with pytest.raises(g.GateRefused, match="re-run"):
        g.gate(pending, ledger=led, **KW)
    with pytest.raises(g.GateRefused, match="confirmation"):
        g.gate(replace(s, stage="screening"), ledger=led, **KW)
    screening = make_ledger(tmp_path / "s", _tasks(100), stage="screening", write_plan=False)
    with pytest.raises(g.GateRefused, match="confirmation"):
        g.gate(s, ledger=screening, **KW)
    no_plan = make_ledger(tmp_path / "n", _tasks(100), write_plan=False)
    with pytest.raises(g.GateRefused, match="plan"):
        g.gate(s, ledger=no_plan, **KW)


def test_gate_scope_text() -> None:
    assert "patch" in g.SCOPE and "plugin_hash" in g.SCOPE and "defaults" in g.SCOPE
