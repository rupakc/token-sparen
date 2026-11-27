"""Release gate (M5 Task 10): conservative non-inferiority on a valid, frozen, live confirmation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from bench import gate as g
from bench.stats import Exclusion, RunRow, Summary

HASH = "a" * 64
KW: dict[str, Any] = {"release_hash": HASH, "e2_ok": True, "planned_tasks": 100, "n_boot": 200}


def _confirm(n: int = 100, *, harmed: int = 0, **row: Any) -> Summary:
    """Baseline resolves every other task; token-sparen the same except ``harmed`` of them."""
    rows: list[RunRow] = []
    hit = 0
    for i in range(n):
        base = i % 2 == 0
        ts = base
        if base and hit < harmed:
            ts, hit = False, hit + 1
        iid = f"r__r-{i}"
        common: dict[str, Any] = {"model": "claude-x", "claude_code_version": "2.1.0", "live": True}
        rows.append(RunRow("baseline", 0, iid, "r/r", "ok", 1.0, base, **common))
        rows.append(
            RunRow("token-sparen", 0, iid, "r/r", "ok", 0.8, ts, plugin_hash=HASH, **common)
        )
    tasks = tuple(f"r__r-{i}" for i in range(n))
    return Summary(
        "confirmation", "baseline", ("baseline", "token-sparen"), (0,), tasks, tuple(rows), ()
    )


def _swap(s: Summary, index: int, **changes: Any) -> Summary:
    runs = list(s.runs)
    runs[index] = replace(runs[index], **changes)
    return replace(s, runs=tuple(runs))


def test_gate_passes_on_noninferior_valid_run() -> None:
    res = g.gate(_confirm(), verdict="powered", **KW)
    assert res.verdict == "passed", res.reasons
    assert res.check is not None and res.check.passed
    assert res.as_e4() == {"verdict": "passed", "reasons": res.reasons}
    # paired tasks ≥ planned size − 5 %: 95 of 100 is enough, 94 is not
    assert g.gate(_confirm(95), verdict="powered", **KW).verdict == "passed"
    short = g.gate(_confirm(94), verdict="powered", **KW)
    assert short.verdict == "failed" and any("planned" in r for r in short.reasons)
    # inferior → failed; cost never enters the gate
    bad = g.gate(_confirm(harmed=10), verdict="powered", **KW)
    assert bad.verdict == "failed" and any("lower bound" in r for r in bad.reasons)
    dear = replace(_confirm(), runs=tuple(replace(r, cost=9.0) for r in _confirm().runs))
    assert g.gate(dear, verdict="powered", **KW).verdict == "passed"
    # E2 must hold
    e2 = g.gate(_confirm(), verdict="powered", **(KW | {"e2_ok": False}))
    assert e2.verdict == "failed" and any("E2" in r for r in e2.reasons)
    # every token-sparen run live
    dead = g.gate(_swap(_confirm(), 1, live=None), verdict="powered", **KW)
    assert dead.verdict == "failed" and any("live" in r for r in dead.reasons)


def test_gate_fails_on_hash_mismatch() -> None:
    other = g.gate(_swap(_confirm(), 3, plugin_hash="b" * 64), verdict="powered", **KW)
    assert other.verdict == "failed" and any("plugin_hash" in r for r in other.reasons)
    missing = g.gate(_swap(_confirm(), 3, plugin_hash=None), verdict="powered", **KW)
    assert missing.verdict == "failed"
    # a non-plugin arm records no hash: fine; a wrong one on any arm is not
    assert g.gate(_swap(_confirm(), 0, plugin_hash="c" * 64), verdict="powered", **KW).verdict == (
        "failed"
    )


def test_gate_fails_on_mixed_model_or_version() -> None:
    model = g.gate(_swap(_confirm(), 0, model="claude-y"), verdict="powered", **KW)
    assert model.verdict == "failed" and any("model" in r for r in model.reasons)
    version = g.gate(_swap(_confirm(), 5, claude_code_version="2.1.1"), verdict="powered", **KW)
    assert version.verdict == "failed" and any("Claude Code" in r for r in version.reasons)
    unknown = g.gate(_swap(_confirm(), 5, model=None), verdict="powered", **KW)
    assert unknown.verdict == "failed"


def test_gate_underpowered_is_not_failed() -> None:
    weak = _confirm(harmed=10)  # would fail a powered gate
    res = g.gate(weak, verdict="underpowered", **KW)
    assert res.verdict == "underpowered"
    assert any("owner" in r for r in res.reasons)
    assert any("gate-decision.toml" in r for r in res.reasons)
    assert g.gate(_confirm(80), verdict="underpowered", **KW).verdict == "underpowered"
    # invalid evidence is never an owner decision: a hash mismatch still fails
    bad = g.gate(_swap(weak, 3, plugin_hash="b" * 64), verdict="underpowered", **KW)
    assert bad.verdict == "failed"


def test_gate_refuses_pending_reruns_and_wrong_stage() -> None:
    s = _confirm()
    pending = replace(
        s,
        runs=tuple(
            r for r in s.runs if not (r.arm == "token-sparen" and r.instance_id == "r__r-0")
        ),
        exclusions=(Exclusion("r__r-0", "incomplete", "token-sparen", 0, None),),
    )
    with pytest.raises(g.GateRefused, match="re-run"):
        g.gate(pending, verdict="powered", **KW)
    with pytest.raises(g.GateRefused, match="confirmation"):
        g.gate(replace(s, stage="screening"), verdict="powered", **KW)
    with pytest.raises(g.GateRefused, match="token-sparen"):
        g.gate(
            replace(s, runs=tuple(r for r in s.runs if r.arm == "baseline")),
            verdict="powered",
            **KW,
        )
    with pytest.raises(ValueError):
        g.gate(s, verdict="powered", **(KW | {"planned_tasks": 0}))


def test_gate_scope_text() -> None:
    assert "patch" in g.SCOPE and "plugin_hash" in g.SCOPE and "defaults" in g.SCOPE
