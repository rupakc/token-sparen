"""v1.0 release gate on the E4 confirmation (M5 Task 10).

``passed`` iff the confirmatory non-inferiority holds (`bench.stats.Summary.gate`: seed 0, lower
bound = min(task-cluster bootstrap, Tango score) > −5 pp after the exclusion rule) AND the
evidence is valid: every recorded ``plugin_hash`` equals the release hash (and every
token-sparen run has one), every token-sparen run was live, paired tasks ≥ the planned size
minus 5 %, one model id and one Claude Code version across the stage, and E2 passed. Cost is
reported, never gated.

``underpowered`` (power step or quota/disk, `bench.stages`) is not a failure: the owner decides
in an owner-signed ``docs/release/gate-decision.toml`` (`version`, `decision` = ship|hold,
`rationale`, `date`, `e4_id`). Invalid evidence (hash, liveness, model/version, E2) still fails:
an owner decision covers missing power, never an invalid measurement.

The gate refuses (`GateRefused`) while one-sided exclusions await their re-runs, and on anything
but a confirmation summary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from bench.power import PowerVerdict
from bench.stats import GateCheck, Summary

TREATMENT = "token-sparen"
REFERENCE = "baseline"
SIZE_TOLERANCE = 0.05  # paired tasks may fall short of the plan by at most 5 %
DECISION_FILE = "docs/release/gate-decision.toml"

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

    def as_e4(self) -> dict[str, Any]:
        """The ``gate`` object of ``e4.json``."""
        return {"verdict": self.verdict, "reasons": list(self.reasons)}


def _validity(confirm: Summary, release_hash: str, e2_ok: bool) -> list[str]:
    problems: list[str] = []
    wrong = sorted(
        {r.plugin_hash for r in confirm.runs if r.plugin_hash not in (None, release_hash)},
        key=str,
    )
    if wrong:
        problems.append(f"plugin_hash differs from the release hash in {len(wrong)} value(s)")
    ts = [r for r in confirm.runs if r.arm == TREATMENT]
    if any(r.plugin_hash is None for r in ts):
        problems.append("token-sparen run(s) without a plugin_hash")
    dead = sum(1 for r in ts if r.live is not True)
    if dead:
        problems.append(f"{dead} token-sparen run(s) not live")
    for what, values in (
        ("model id", {r.model for r in confirm.runs}),
        ("Claude Code version", {r.claude_code_version for r in confirm.runs}),
    ):
        if None in values:
            problems.append(f"run(s) without a recorded {what}")
        if len(values - {None}) != 1:
            problems.append(f"{len(values - {None})} {what}s in the stage (need exactly one)")
    if not e2_ok:
        problems.append("E2 fact-preservation did not pass")
    return problems


def gate(
    confirm: Summary,
    *,
    verdict: PowerVerdict,
    release_hash: str,
    e2_ok: bool,
    planned_tasks: int,
    **kw: Any,
) -> GateResult:
    """Release gate; ``planned_tasks`` = the confirmation size the power step scheduled;
    ``kw`` go to `Summary.gate` (``n_boot``, ``seed``)."""
    if planned_tasks < 1:
        raise ValueError(f"planned_tasks must be >= 1, got {planned_tasks}")
    if confirm.stage != "confirmation":
        raise GateRefused(f"the gate reads the confirmation stage, got {confirm.stage!r}")
    if not any(r.arm == TREATMENT for r in confirm.runs):
        raise GateRefused("no graded token-sparen run in the confirmation")
    pending, _ = confirm.one_sided(TREATMENT, REFERENCE)
    if pending:
        raise GateRefused(f"{len(pending)} one-sided exclusion(s) still await re-runs")
    check = confirm.gate(TREATMENT, REFERENCE, **kw)
    invalid = _validity(confirm, release_hash, e2_ok)
    stat: list[str] = list(check.reasons)
    need = math.ceil(planned_tasks * (1 - SIZE_TOLERANCE) - 1e-9)
    if check.estimate.n < need:
        stat.append(f"{check.estimate.n} paired tasks < {need} (planned {planned_tasks} − 5 %)")
    bound = f"resolved-diff lower bound {check.estimate.lo:+.4f} (margin −0.05)"
    if invalid:
        return GateResult("failed", [*invalid, *stat, bound], check)
    if verdict == "underpowered":
        note = f"underpowered: not failed; the owner decides in {DECISION_FILE}"
        return GateResult(
            "underpowered", [note, *(f"descriptive: {r}" for r in stat), bound], check
        )
    if not check.passed or check.estimate.n < need:
        return GateResult("failed", [*stat, bound], check)
    return GateResult("passed", [*stat, bound], check)
