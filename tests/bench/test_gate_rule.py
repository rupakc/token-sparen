"""Exclusion rule of the confirmatory gate (M5 Task 9 re-review rulings, round 3).

One-sided exclusions are re-run first by the scheduler; of what remains after retries only the
exclusions that can move the difference count: k = max(0, |token-sparen-only on tasks the
baseline resolved| − |baseline-only on tasks token-sparen resolved|); k of those token-sparen
exclusions are imputed as token-sparen failures; every other one-sided or task-level exclusion
is dropped. No tolerance."""

from __future__ import annotations

import random
from typing import Any

import pytest

from bench import power, stats

# type-I tolerance: nominal α/2 plus 3 Monte-Carlo SD at 600 simulations
SIMS = 600
TOL = 0.025 + 3 * (0.025 * 0.975 / SIMS) ** 0.5


def _margin_true(
    n: int, rng: random.Random, *, harmed_share: float = 0.05, flaky: float = 0.3
) -> tuple[dict[str, dict[int, bool | None]], dict[str, dict[int, bool | None]], list[str]]:
    """True difference exactly −5 pp: 5 % of tasks deterministically harmed (arm fails,
    baseline resolves), 30 % flaky (p = 0.5 each arm), the rest concordant."""
    arm: dict[str, dict[int, bool | None]] = {}
    base: dict[str, dict[int, bool | None]] = {}
    harmed: list[str] = []
    for i in range(n):
        t = f"t{i}"
        u = rng.random()
        if u < harmed_share:
            a, b = False, True
            harmed.append(t)
        elif u < harmed_share + flaky:
            a, b = rng.random() < 0.5, rng.random() < 0.5
        else:
            a = b = rng.random() < 0.5
        arm[t], base[t] = {0: a}, {0: b}
    return arm, base, harmed


def _excluded_pass_rate(
    n: int, mode: str, *, sims: int = SIMS, seed: int = 0, n_boot: int = 500, noise: float = 0.05
) -> float:
    rng = random.Random(seed)
    passed = 0
    for k in range(sims):
        arm, base, harmed = _margin_true(n, rng)
        ax: set[str] = set()
        bx: set[str] = set()
        if mode == "adv2":  # exactly 2 harmed tasks excluded for token-sparen only
            ax.update(harmed[:2])
        elif mode == "inform":  # token-sparen infra-fails on half of its harmed tasks + 5 % noise
            for t in arm:
                if t in harmed and rng.random() < 0.5:
                    ax.add(t)
                if rng.random() < noise:
                    ax.add(t)
                if rng.random() < noise:
                    bx.add(t)
        for t in ax:
            arm[t] = {0: None}
        for t in bx:
            base[t] = {0: None}
        g = stats.gate_decision(arm, base, arm_excluded=ax, base_excluded=bx, n_boot=n_boot, seed=k)
        passed += g.passed
    return passed / sims


def test_gaming_two_harmed_exclusions_no_longer_pass() -> None:
    """Reviewer's case: 2 harmed tasks excluded for token-sparen only (passed 7.7 % at n = 80
    under the 2-task tolerance)."""
    assert _excluded_pass_rate(80, "adv2") <= TOL


def test_type_one_error_with_informative_exclusions() -> None:
    """Token-sparen infra-fails on half of its harmed tasks (+ 5 % independent exclusions in
    both arms): 5.2 % at n = 80 under the tolerance rule."""
    assert _excluded_pass_rate(80, "inform", seed=1) <= TOL


def test_type_one_error_informative_plus_random_n150() -> None:
    """Re-review 2: informative + 10 % random exclusions per arm at n = 150 (0.033 under the
    round-2 net excess, ≈ +3.5 SD). Deterministic; tolerance nominal + 3 MC SD (1200 sims)."""
    sims = 1200
    rate = _excluded_pass_rate(150, "inform", sims=sims, seed=5, n_boot=300, noise=0.10)
    assert rate <= 0.025 + 3 * (0.025 * 0.975 / sims) ** 0.5


def _all(n: int, value: bool, prefix: str = "t") -> dict[str, dict[int, bool | None]]:
    return {f"{prefix}{i}": {0: value} for i in range(n)}


def test_net_excess_imputed_on_baseline_resolved_tasks() -> None:
    arm = _all(150, True)
    base = _all(150, True)
    # 3 token-sparen-only exclusions (baseline: 2 resolved, 1 not); baseline-only exclusions:
    # y0 where token-sparen resolved (offsets one), y1 where it failed (cannot offset)
    for t, b in (("x0", True), ("x1", False), ("x2", True)):
        arm[t], base[t] = {0: None}, {0: b}
    arm["y0"], base["y0"] = {0: True}, {0: None}
    arm["y1"], base["y1"] = {0: False}, {0: None}
    # 1 task-level exclusion
    arm["z0"], base["z0"] = {0: None}, {0: None}
    g = stats.gate_decision(
        arm,
        base,
        arm_excluded={"x0", "x1", "x2", "z0"},
        base_excluded={"y0", "y1", "z0"},
        n_boot=300,
    )
    assert (g.excluded_arm, g.excluded_ref, g.excluded_both) == (3, 2, 1)
    assert g.imputed == ("x0",)  # k = |{x0, x2}| − |{y0}| = 1, on a baseline-resolved task
    assert g.estimate.n == 151  # 150 observed + 1 imputed; the other exclusions dropped
    assert g.estimate.point == pytest.approx(-1 / 151)
    assert g.observed.n == 150
    assert g.observed.point == 0.0


def test_no_tolerance_one_excess_is_imputed() -> None:
    arm = _all(200, True)
    base = _all(200, True)
    arm["x0"], base["x0"] = {0: None}, {0: True}
    g = stats.gate_decision(arm, base, arm_excluded={"x0"}, n_boot=300)
    assert g.imputed == ("x0",)
    assert g.estimate.point == pytest.approx(-1 / 201)


def test_exclusions_that_cannot_move_the_difference_are_not_counted() -> None:
    """A token-sparen-only exclusion where the baseline failed carries no discordance (not
    imputed), and a baseline-only exclusion where token-sparen failed cannot offset one that
    does (the round-2 net excess let it cancel)."""
    arm = _all(150, True)
    base = _all(150, True)
    arm["x0"], base["x0"] = {0: None}, {0: False}
    g = stats.gate_decision(arm, base, arm_excluded={"x0"}, n_boot=300)
    assert g.imputed == ()
    assert g.estimate.n == 150
    # 3 harmful token-sparen-only exclusions, 3 baseline-only exclusions on token-sparen failures
    arm, base = _all(150, True), _all(150, True)
    for i in range(3):
        arm[f"x{i}"], base[f"x{i}"] = {0: None}, {0: True}
        arm[f"y{i}"], base[f"y{i}"] = {0: False}, {0: None}
    g = stats.gate_decision(
        arm,
        base,
        arm_excluded={f"x{i}" for i in range(3)},
        base_excluded={f"y{i}" for i in range(3)},
        n_boot=300,
    )
    assert g.imputed == ("x0", "x1", "x2")
    assert g.estimate.point == pytest.approx(-3 / 153)


def test_baseline_excess_is_dropped_not_rewarded() -> None:
    arm = _all(150, True)
    base = _all(150, True)
    for i in range(5):
        arm[f"y{i}"], base[f"y{i}"] = {0: False}, {0: None}
    g = stats.gate_decision(arm, base, base_excluded={f"y{i}" for i in range(5)}, n_boot=300)
    assert g.imputed == ()
    assert g.estimate.n == 150
    assert g.estimate == g.observed
    assert g.passed


def test_pending_one_sided_exclusions_block_the_gate() -> None:
    """A one-sided exclusion not yet re-run to its retry limit (``incomplete``) blocks a pass."""
    ledger: list[dict[str, Any]] = []
    grades: dict[str, bool | None] = {}
    for i in range(120):
        for arm in ("baseline", "token-sparen"):
            key = f"c/confirmation/{arm}/0/r__r-{i}"
            if not (arm == "token-sparen" and i == 0):
                ledger.append({"kind": "end", "key": key, "status": "ok", "at": 1.0})
                grades[key] = True
            else:
                ledger.append({"kind": "start", "key": key, "at": 1.0})
    s = stats.summarise(ledger, grades, stage="confirmation")
    pending, final = s.one_sided("token-sparen")
    assert [e.instance_id for e in pending] == ["r__r-0"]
    assert final == []
    g = s.gate(n_boot=300)
    assert g.pending == 1
    assert not g.passed
    assert any("re-run" in r for r in g.reasons)


def test_power_with_exclusions_matches_gate_simulation() -> None:
    """`power` simulates the exclusion process and the rule; it agrees with running the public
    `gate_decision` on data generated by the same process (true difference 0)."""
    f, n, one_sided, task_level = 0.2, 200, 0.05, 0.05
    rng = random.Random(4)
    sims, passed = 150, 0
    for k in range(sims):
        arm: dict[str, dict[int, bool | None]] = {}
        base: dict[str, dict[int, bool | None]] = {}
        ax: set[str] = set()
        bx: set[str] = set()
        for i in range(n):
            t = f"t{i}"
            if rng.random() < f:
                a, b = rng.random() < 0.5, rng.random() < 0.5
            else:
                a = b = rng.random() < 0.5
            arm[t], base[t] = {0: a}, {0: b}
            if rng.random() < task_level:
                ax.add(t)
                bx.add(t)
            else:
                if rng.random() < one_sided:
                    ax.add(t)
                if rng.random() < one_sided:
                    bx.add(t)
        for t in ax:
            arm[t] = {0: None}
        for t in bx:
            base[t] = {0: None}
        passed += stats.gate_decision(
            arm, base, arm_excluded=ax, base_excluded=bx, n_boot=300, seed=k
        ).passed
    simulated = power.power(
        f, n, 1, n_sim=600, n_boot=300, one_sided_rate=one_sided, task_rate=task_level
    )
    plain = power.power(f, n, 1, n_sim=600, n_boot=300)
    assert abs(passed / sims - simulated) <= 0.12
    assert simulated < plain


def test_plan_simulates_screening_exclusion_rates() -> None:
    clean = power.FlipModel(f=0.1, q=0.05, n=44, q_hi=0.1, f_plan=0.2)
    leaky = power.FlipModel(
        f=0.1, q=0.05, n=44, q_hi=0.1, f_plan=0.2, one_sided_rate=0.05, task_rate=0.05
    )
    kw: dict[str, Any] = {"n_sim": 150, "n_boot": 150}
    a = power.plan(clean, pool=1000, **kw)
    b = power.plan(leaky, pool=1000, **kw)
    assert a.required_tasks is not None
    assert b.required_tasks is not None
    assert b.required_tasks > a.required_tasks
    assert b.achieved_power == power.power(
        0.2, b.required_tasks, 1, one_sided_rate=0.05, task_rate=0.05, rate=leaky.rate, **kw
    )
    assert b.achieved_power >= 0.8


def test_flip_model_screening_exclusion_rates() -> None:
    ledger: list[dict[str, Any]] = []
    grades: dict[str, bool | None] = {}
    for i in range(20):
        for arm in ("baseline", "token-sparen"):
            key = f"c/screening/{arm}/0/r__r-{i}"
            one_sided = arm == "token-sparen" and i < 2
            task_level = i in (18, 19)
            if one_sided or task_level:
                ledger.append({"kind": "end", "key": key, "status": "infra_fail", "sub": "api"})
            else:
                ledger.append({"kind": "end", "key": key, "status": "ok"})
                grades[key] = i % 2 == 0
    m = power.flip_model(stats.summarise(ledger, grades, stage="screening"))
    assert m.task_rate == pytest.approx(2 / 20)
    assert m.one_sided_rate == pytest.approx(2 / (2 * 20))  # per arm
    assert m.rate == pytest.approx(8 / 16)  # baseline resolved rate on paired tasks


def test_summarise_ledger_uses_excluded_after_retries(tmp_path: Any) -> None:
    """The exclusion source is `Ledger.excluded_after_retries`: a key with infra failures but
    retries left is pending (``incomplete``), not an exclusion the gate imputes."""
    from bench.grade import grades_path
    from bench.ledger import Ledger, RunKey

    header = {
        "campaign": "c",
        "stage": "confirmation",
        "harness_commit": "0" * 40,
        "plugin_hash": "a" * 64,
        "plugin_ref": "v1.0.0-rc1",
        "pins": {},
        "model": "m",
        "claude_code_version": "2.0.0",
    }
    led = Ledger(tmp_path / "ledger.jsonl", header=header)
    grade_lines = []
    for i in range(100):
        for arm in ("baseline", "token-sparen"):
            key = RunKey("c", "confirmation", arm, 0, f"r__r-{i}").id
            if arm == "token-sparen" and i < 2:
                attempts = 3 if i == 0 else 1  # r-0 unrunnable after retries, r-1 still pending
                for _ in range(attempts):
                    led.append({"kind": "end", "key": key, "status": "infra_fail", "sub": "api"})
                continue
            led.append({"kind": "end", "key": key, "status": "ok", "run_cost": 1.0})
            grade_lines.append(
                {"kind": "grade", "stage": "confirmation", "key": key, "resolved": True}
            )
    import json

    grades_path(led).write_text(
        "".join(json.dumps(g) + "\n" for g in grade_lines), encoding="utf-8"
    )
    s = stats.summarise_ledger(led, stage="confirmation")
    by_reason = {(e.instance_id, e.reason, e.cause) for e in s.exclusions}
    assert by_reason == {("r__r-0", "unrunnable", "api"), ("r__r-1", "incomplete", None)}
    pending, final = s.one_sided()
    assert [e.instance_id for e in pending] == ["r__r-1"]
    assert [e.instance_id for e in final] == ["r__r-0"]
    g = s.gate(n_boot=200)
    assert g.pending == 1
    assert g.imputed == ("r__r-0",)
    assert not g.passed
