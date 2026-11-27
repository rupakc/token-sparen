"""v1.0 release gate on the E4 confirmation (M5 Task 10 and its review rulings I1/I3/M1).

Inputs come from persisted state: the confirmation plan recorded in the ledger
(`bench.stages.write_plan`: planned tasks, seed, model id, Claude Code version, plugin_hash,
power verdict, with a content hash, written before the first run; every run record carries
that hash — `bench.stages.read_plan`). The summary is built from the ledger and its grades file,
never passed in. Any run key outside the plan (task, arm or seed) refuses the gate.

The ledger and the confirmation grades are hash-chained (ruling N4): the gate verifies both
chains and returns their heads for e4.json (anchored by the signed release commit). It refuses
(`GateRefused`) on a broken chain, a confirmation key with more than one graded run or a start
after it, more than one definite grade, a grade for another patch or without a run, and when a
graded confirmation run was reopened (invalidate or
retry) — re-running graded runs is outcome-selective (ruling N3) — and on anything but a
complete confirmation: every planned task
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
from dataclasses import dataclass, field
from typing import Any, Literal

from bench.e4 import GATE_REASON_CODES
from bench.grade import grades_chain_head, grades_path
from bench.ledger import GRADED, Ledger, LedgerError, read_jsonl
from bench.stages import (
    CORE_ARMS,
    FROZEN_ARMS,
    PlanError,
    StagePlan,
    missing,
    read_plan,
    restrict,
)
from bench.stats import MARGIN, GateCheck, Summary, summarise_ledger

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

# The codes this gate emits, all from the closed `gate_reason` vocabulary of e4.schema.json
# (`bench.e4.GATE_REASON_CODES`; checked at import). Human-readable text with figures goes to
# `GateResult.details`, never into e4.json.
REASON_CODES: tuple[str, ...] = (
    "plan_hash_not_release",
    "plugin_hash_mismatch",
    "frozen_run_without_hash",
    "treatment_not_live",
    "model_mismatch",
    "claude_code_version_mismatch",
    "e2_failed",
    "clearly_inferior",
    "underpowered_owner_decides",
    "lower_bound_not_above_margin",
    "too_few_paired_tasks",
    "one_sided_pending",
    "excess_exclusions_imputed",
)
if not set(REASON_CODES) <= set(GATE_REASON_CODES):  # pragma: no cover - a schema/code drift
    raise ImportError("bench.gate emits reason codes outside bench.e4.GATE_REASON_CODES")


class GateRefused(ValueError):
    """The gate cannot be decided on this ledger (yet)."""


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    reasons: list[str]  # codes from `REASON_CODES`
    check: GateCheck | None = None
    plan_sha256: str | None = None
    # pins of the persisted plan (what the report publishes; Task 11)
    plugin_hash: str | None = None
    plugin_version: str | None = None
    model: str | None = None  # the exact model id every run recorded
    claude_code_version: str | None = None
    price_confirmed_on: str | None = None
    details: list[str] = field(default_factory=list)  # human-readable, for the operator only
    # heads of the verified hash chains (ruling N4), anchored by the signed release commit
    ledger_chain_head: str | None = None
    grades_chain_head: str | None = None

    def as_e4(self) -> dict[str, Any]:
        """The ``gate`` object of ``e4.json``: codes only, plus the plan hash and both chain
        heads (`bench.e4.GATE_ANCHOR_FIELDS`)."""
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "plan_sha256": self.plan_sha256,
            "ledger_chain_head": self.ledger_chain_head,
            "grades_chain_head": self.grades_chain_head,
        }


Finding = tuple[str, str]  # (code, detail)


def _validity(confirm: Summary, plan: StagePlan, release_hash: str, e2_ok: bool) -> list[Finding]:
    problems: list[Finding] = []
    if plan.plugin_hash != release_hash:
        problems.append(("plan_hash_not_release", "the plan's frozen plugin is not the release"))
    wrong = {r.plugin_hash for r in confirm.runs if r.plugin_hash not in (None, plan.plugin_hash)}
    if wrong:
        problems.append(("plugin_hash_mismatch", f"{len(wrong)} other plugin_hash value(s)"))
    unhashed = sum(1 for r in confirm.runs if r.arm in FROZEN_ARMS and r.plugin_hash is None)
    if unhashed:
        problems.append(("frozen_run_without_hash", f"{unhashed} frozen-plugin run(s) unhashed"))
    dead = sum(1 for r in confirm.runs if r.arm == TREATMENT and r.live is not True)
    if dead:
        problems.append(("treatment_not_live", f"{dead} token-sparen run(s) not live"))
    for code, want, values in (
        ("model_mismatch", plan.model, [r.model for r in confirm.runs]),
        (
            "claude_code_version_mismatch",
            plan.claude_code_version,
            [r.claude_code_version for r in confirm.runs],
        ),
    ):
        off = sorted({str(v) for v in values if v != want})
        if off:
            problems.append(
                (code, f"run value(s) {', '.join(off)} differ from the plan's {want!r}")
            )
    if not e2_ok:
        problems.append(("e2_failed", "E2 fact-preservation did not pass"))
    return problems


def _statistics(check: GateCheck) -> list[Finding]:
    est = check.estimate
    out: list[Finding] = []
    if check.pending:
        out.append(("one_sided_pending", f"{check.pending} one-sided exclusion(s) to re-run"))
    if est.n < check.min_tasks:
        out.append(("too_few_paired_tasks", f"{est.n} paired tasks < {check.min_tasks}"))
    if not est.lo > MARGIN:
        out.append(("lower_bound_not_above_margin", f"lower bound {est.lo:+.4f} <= {MARGIN}"))
    if check.imputed:
        out.append(("excess_exclusions_imputed", f"{len(check.imputed)} imputed as failures"))
    return out


def _outside(key: str, plan: StagePlan) -> bool:
    parts = key.split("/")
    if len(parts) != 5 or parts[1] != "confirmation":
        return False
    return parts[2] not in plan.arms or parts[3] != str(plan.seed) or parts[4] not in plan.tasks


def _rerun_problems(ledger: Ledger, grades: list[dict[str, Any]]) -> list[str]:
    """Ruling N4(a): each confirmation key has at most one graded end and no start after it;
    at most one definite grade, whose patch is the final end's; no grade without a run."""
    ends: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    for rec in ledger.records():
        key = rec.get("key")
        if not isinstance(key, str) or key.split("/")[1:2] != ["confirmation"]:
            continue
        graded = [e for e in ends.get(key, []) if e.get("status") in GRADED]
        if rec.get("kind") == "start" and graded:
            problems.append(f"{key}: started again after its graded run")
        elif rec.get("kind") == "end":
            if rec.get("status") in GRADED and graded:
                problems.append(f"{key}: more than one graded run")
            ends.setdefault(key, []).append(rec)
    finals = {
        k: [e for e in lst if e.get("status") in GRADED][-1]
        for k, lst in ends.items()
        if any(e.get("status") in GRADED for e in lst)
    }
    seen: set[str] = set()
    for rec in grades:
        if rec.get("kind") != "grade" or rec.get("stage") != "confirmation":
            continue
        key = str(rec.get("key"))
        if key not in finals:
            problems.append(f"{key}: a grade without a graded run")
        elif rec.get("patch_sha256") != finals[key].get("patch_sha256"):
            problems.append(f"{key}: a grade for another patch than the run's")
        if rec.get("resolved") is not None:
            if key in seen:
                problems.append(f"{key}: more than one grade")
            seen.add(key)
    return problems


def gate(
    *,
    ledger: Ledger,
    release_hash: str,
    e2_ok: bool,
    n_boot: int = 10000,
    seed: int = 0,
) -> GateResult:
    """Release gate on the confirmation ``ledger``: its recorded plan gives the planned tasks,
    the seed, the pins and the power verdict, and the summary is built from it
    (`summarise_ledger`: its runs, exclusions after retries and grades; ruling N2), never
    supplied by the caller. ``n_boot`` and ``seed`` go to `Summary.gate`."""
    if not _HASH.match(release_hash):
        raise ValueError(f"release_hash must be 64 lowercase hex characters, got {release_hash!r}")
    if ledger.header.get("stage") != "confirmation":
        raise GateRefused(
            f"the gate reads the confirmation stage, ledger is {ledger.header.get('stage')!r}"
        )
    try:
        plan = read_plan(ledger)
    except PlanError as exc:
        raise GateRefused(f"confirmation plan: {exc}") from exc
    try:
        ledger_head = ledger.chain_head()
        grades_head = grades_chain_head(grades_path(ledger))
        grades = read_jsonl(grades_path(ledger))
    except LedgerError as exc:
        raise GateRefused(f"tamper check: {exc} (ledger or grades hash chain)") from exc
    rerun = _rerun_problems(ledger, grades)
    if rerun:
        raise GateRefused(
            f"{len(rerun)} re-run/re-grade problem(s), e.g. {rerun[0]}: a graded confirmation "
            "run is never re-run or re-graded (ruling N4)"
        )
    reopened = ledger.reopened_graded("confirmation")
    if reopened:
        raise GateRefused(
            f"{len(reopened)} graded confirmation run(s) were reopened (invalidate/retry) and "
            f"re-run, e.g. {reopened[0]}: graded runs are never re-run (ruling N3)"
        )
    stray = sorted(
        {
            str(r["key"])
            for r in ledger.records()
            if r.get("kind") in ("start", "end") and _outside(str(r.get("key", "")), plan)
        }
    )
    if stray:
        raise GateRefused(f"{len(stray)} run key(s) outside the plan, e.g. {stray[0]}")
    confirm = summarise_ledger(ledger, stage="confirmation")
    scoped, outside = restrict(confirm, plan.tasks)
    if outside:
        raise GateRefused(f"{outside} run(s)/exclusion(s) on tasks outside the plan")
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
    invalid = _validity(scoped, plan, release_hash, e2_ok)
    stat = _statistics(check)
    bound = f"resolved-diff {est.point:+.4f}, 95 % bound [{est.lo:+.4f}, {est.hi:+.4f}], n {est.n}"

    def result(verdict: GateVerdict, findings: list[Finding], *extra: str) -> GateResult:
        return GateResult(
            verdict,
            [code for code, _ in findings],
            check,
            plan.sha256,
            plugin_hash=plan.plugin_hash,
            plugin_version=plan.plugin_version,
            model=plan.model,
            claude_code_version=plan.claude_code_version,
            price_confirmed_on=plan.price_confirmed_on,
            ledger_chain_head=ledger_head,
            grades_chain_head=grades_head,
            details=[*extra, *(f"{code}: {text}" for code, text in findings), bound],
        )

    if invalid:
        return result("failed", [*invalid, *stat])
    if est.point < MARGIN and est.hi < 0:
        inferior = ("clearly_inferior", "point below −5 pp and upper bound < 0")
        return result("failed", [inferior, *stat])
    if plan.verdict == "underpowered":
        note = ("underpowered_owner_decides", f"not failed; the owner decides in {DECISION_FILE}")
        return result("underpowered", [note, *stat], "statistical findings are descriptive")
    return result("passed" if check.passed else "failed", stat)
