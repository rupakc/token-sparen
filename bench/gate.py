"""v1.0 release gate on the E4 confirmation (M5 Task 10 and its review rulings I1/I3/M1).

Inputs come from persisted state: the confirmation plan recorded in the ledger
(`bench.stages.write_plan`: planned tasks, seed, model id, Claude Code version, plugin_hash,
power verdict, with a content hash). Runs and exclusions on tasks outside the plan are ignored
and flagged.

The gate refuses (`GateRefused`) on anything but a complete confirmation: every planned task
needs, in baseline and token-sparen at the plan's seed, a graded final run or an exclusion that
stays after retries (no size tolerance); keys that were never started, are incomplete or await
grading block it, as do one-sided keys awaiting re-runs.

``passed`` iff the confirmatory non-inferiority holds (`bench.stats.Summary.gate`: seed 0, lower
bound = min(task-cluster bootstrap, Tango score) > −5 pp after the exclusion rule) AND the
evidence is valid: the plan's plugin_hash is the release hash, every recorded ``plugin_hash``
equals it and every frozen-plugin run carries it, every token-sparen run was live, every run used
exactly the plan's model id and Claude Code version, and E2 passed. Cost is reported, never
gated.

``failed`` also when the result is clearly inferior (point < −5 pp and upper 95 % bound < 0),
whatever the power verdict (ruling M1). Otherwise an ``underpowered`` plan gives
``underpowered``, which is not a failure: the owner decides in an owner-signed
``docs/release/gate-decision.toml`` (`version`, `decision` = ship|hold, `rationale`, `date`,
`e4_id`). Invalid evidence still fails: an owner decision covers missing power, never an invalid
measurement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from bench.ledger import Ledger
from bench.stages import (
    CORE_ARMS,
    FROZEN_ARMS,
    PlanError,
    StagePlan,
    missing,
    read_plan,
    restrict,
)
from bench.stats import MARGIN, GateCheck, Summary

TREATMENT = "token-sparen"
REFERENCE = "baseline"
DECISION_FILE = "docs/release/gate-decision.toml"
_HASH = re.compile(r"^[0-9a-f]{64}$")

# Ruling N4 (with the M6 Task 9 rulings), stated again in docs/reports/m5-method.md and the
# release skill text.
SCOPE = (
    "The E4 gate applies to minor and major releases. A v1.0.x patch may ship without a new E4 "
    "run only if README Evidence states the measured plugin_hash and version and the patch "
    "CHANGELOG lists the runtime changes; any change to defaults or learning behaviour needs a "
    "new confirmation."
)

GateVerdict = Literal["passed", "failed", "underpowered"]


class GateRefused(ValueError):
    """The gate cannot be decided on this summary (yet)."""


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    reasons: list[str]
    check: GateCheck | None = None
    plan_sha256: str | None = None

    def as_e4(self) -> dict[str, Any]:
        """The ``gate`` object of ``e4.json``."""
        return {"verdict": self.verdict, "reasons": list(self.reasons)}


def _validity(confirm: Summary, plan: StagePlan, release_hash: str, e2_ok: bool) -> list[str]:
    problems: list[str] = []
    if plan.plugin_hash != release_hash:
        problems.append("the plan's frozen plugin_hash is not the release hash")
    wrong = {r.plugin_hash for r in confirm.runs if r.plugin_hash not in (None, plan.plugin_hash)}
    if wrong:
        problems.append(f"plugin_hash differs from the plan's in {len(wrong)} value(s)")
    unhashed = sum(1 for r in confirm.runs if r.arm in FROZEN_ARMS and r.plugin_hash is None)
    if unhashed:
        problems.append(f"{unhashed} frozen-plugin run(s) without a plugin_hash")
    dead = sum(1 for r in confirm.runs if r.arm == TREATMENT and r.live is not True)
    if dead:
        problems.append(f"{dead} token-sparen run(s) not live")
    for what, want, values in (
        ("model id", plan.model, [r.model for r in confirm.runs]),
        (
            "Claude Code version",
            plan.claude_code_version,
            [r.claude_code_version for r in confirm.runs],
        ),
    ):
        off = sorted({str(v) for v in values if v != want})
        if off:
            problems.append(f"run {what}(s) {', '.join(off)} differ from the plan's {want!r}")
    if not e2_ok:
        problems.append("E2 fact-preservation did not pass")
    return problems


def gate(
    confirm: Summary,
    *,
    ledger: Ledger,
    release_hash: str,
    e2_ok: bool,
    n_boot: int = 10000,
    seed: int = 0,
) -> GateResult:
    """Release gate on the confirmation ``confirm`` of ``ledger`` (whose recorded plan gives
    the planned tasks, the seed, the pins and the power verdict); ``n_boot`` and ``seed`` go
    to `Summary.gate`."""
    if not _HASH.match(release_hash):
        raise ValueError(f"release_hash must be 64 lowercase hex characters, got {release_hash!r}")
    if ledger.header.get("stage") != "confirmation" or confirm.stage != "confirmation":
        raise GateRefused(
            f"the gate reads the confirmation stage (ledger {ledger.header.get('stage')!r}, "
            f"summary {confirm.stage!r})"
        )
    try:
        plan = read_plan(ledger)
    except PlanError as exc:
        raise GateRefused(f"confirmation plan: {exc}") from exc
    scoped, outside = restrict(confirm, plan.tasks)
    if not any(r.arm == TREATMENT for r in scoped.runs):
        raise GateRefused("no graded token-sparen run on the planned tasks")
    pending, _ = scoped.one_sided(TREATMENT, REFERENCE, seed=plan.seed)
    if pending:
        raise GateRefused(f"{len(pending)} one-sided exclusion(s) still await re-runs")
    open_tasks = sorted({t for a in CORE_ARMS for t in missing(scoped, a, plan.tasks, plan.seed)})
    if open_tasks:
        raise GateRefused(
            f"{len(open_tasks)} planned task(s) without a final outcome or a final exclusion in "
            f"both arms (not started, incomplete or not graded), e.g. {open_tasks[0]}"
        )
    check = scoped.gate(TREATMENT, REFERENCE, gate_seed=plan.seed, n_boot=n_boot, seed=seed)
    est = check.estimate
    notes = [f"{outside} run(s)/exclusion(s) on tasks outside the plan ignored"] if outside else []
    invalid = _validity(scoped, plan, release_hash, e2_ok)
    stat = list(check.reasons)
    bound = f"resolved-diff {est.point:+.4f}, 95 % bound [{est.lo:+.4f}, {est.hi:+.4f}], n {est.n}"

    def result(verdict: GateVerdict, reasons: list[str]) -> GateResult:
        return GateResult(verdict, [*reasons, *notes, bound], check, plan.sha256)

    if invalid:
        return result("failed", [*invalid, *stat])
    if est.point < MARGIN and est.hi < 0:
        return result("failed", ["clearly inferior: point below −5 pp and upper bound < 0", *stat])
    if plan.verdict == "underpowered":
        note = f"underpowered: not failed; the owner decides in {DECISION_FILE}"
        return result("underpowered", [note, *(f"descriptive: {r}" for r in stat)])
    return result("passed" if check.passed else "failed", stat)
