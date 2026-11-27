"""Public E4 report and `e4.json` (M5 Task 11).

`build_report` turns one stage's E4 ledger and grades into ``e4.json`` (valid per
`bench.e4.validate_e4`; M6's README renderer and release gate read it), ``report.md`` and three
SVG figures. Every figure in ``report.md`` is rendered from the same ``e4.json`` document (no
hand-typed number): the published fields plus one additive ``report`` object (exclusions per arm
and repository, per-repository rates, ``grade_oom_repeat`` counts, price-table confirmation,
completeness and the disclosures). ``report.md`` and the SVGs are a pure function of
``e4.json`` (`render_report`, `render_figures`): `lint_report` and `publish` re-render them and
require byte equality, and only then is the generated section exempt from the docs figure lint.
Every run and the gate must match the plan in the ledger header (`PLAN_FIELDS`).

E4 only: `assert_e4_source` refuses anything but an E4 ledger (E1 replay output never becomes a
public figure), and `scan_private` refuses to write when any artefact holds a home path, a
deny-listed word (``TOKEN_SPAREN_DENYLIST``), an e-mail address or an OAuth token.

`publish` copies a finished report into the repository: ``docs/benchmarks/<campaign>-<stage>/``
always, ``docs/evidence/`` (M6 reads its ``e4.json``) only for a confirmation; refused unless
the document validates, re-renders identically and the stage is completely graded.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from bench import svg
from bench.arms import load_arms
from bench.e4 import (
    BASELINE_ARM,
    CONFIRMATORY_METRIC,
    DISCLOSURE_CODES,
    EXCLUSION_CAUSES,
    EXCLUSION_REASONS,
    GATE_ANCHOR_FIELDS,
    GATE_REASON_CODES,
    MARGIN_PP,
    TREATMENT_ARM,
    E4Error,
    load_e4,
    validate_e4,
)
from bench.e4 import SCHEMA_VERSION as E4_SCHEMA_VERSION
from bench.grade import grade_oom_repeat_counts, load_grade_mode, load_grades
from bench.leak import TOKEN_ENV
from bench.ledger import LEDGER_KIND, SCHEMA, Ledger, RunKey
from bench.stats import Estimate, Summary, bca, repo_of, summarise, wilson
from bench.tasks import read_pool_header
from scripts.docs_lint import figure_violations, generated_block

SUITE = "swe-bench-verified-arm64-pool"
STYLE_ARM = "ts-style"
DENYLIST_ENV = "TOKEN_SPAREN_DENYLIST"
FIGURES = ("cost_ratio", "resolved_diff", "pareto")
SCREENING_TITLE = "Screening (descriptive, not a claim)"
MIN_PUBLISH_N_BOOT = 10000
# the confirmation plan in the ledger header every run and the gate must match
PLAN_FIELDS = ("plugin_hash", "model", "claude_code_version")
_PRICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_OAT = re.compile(r"sk-ant-oat[0-9A-Za-z_-]{8,}")
_USERS_POSIX = "/" + "Users/"
_USERS_WIN = "C:" + "\\" + "Users" + "\\"


class NotE4Error(ValueError):
    """The input is not an E4 ledger (only E4 figures may be published)."""


class ReportError(ValueError):
    """The report cannot be built or published."""


class PrivateDataError(ReportError):
    """An artefact holds private data; nothing was written."""


class GateLike(Protocol):
    """Task 10's `GateResult` (read-only view), carrying the plan it was evaluated for."""

    @property
    def verdict(self) -> str: ...

    @property
    def reasons(self) -> Sequence[str]: ...

    @property
    def plugin_hash(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def claude_code_version(self) -> str: ...


# --------------------------------------------------------------------------- guards


def assert_e4_source(ledger: Path) -> None:
    """Raise `NotE4Error` unless the first line of ``ledger`` is the E4 ledger header."""
    try:
        with ledger.open(encoding="utf-8") as fh:
            first = fh.readline()
    except OSError as exc:
        raise NotE4Error(f"not an E4 ledger: cannot read {ledger.name}") from exc
    try:
        head = json.loads(first)
    except json.JSONDecodeError as exc:
        raise NotE4Error(f"not an E4 ledger: {ledger.name} has no JSON header") from exc
    if (
        not isinstance(head, dict)
        or head.get("kind") != LEDGER_KIND
        or head.get("schema") != SCHEMA
    ):
        raise NotE4Error(f"not an E4 ledger (kind {LEDGER_KIND!r}, schema {SCHEMA}): {ledger.name}")


def scan_private(text: str, *, home: Path, deny: Sequence[str], token: str | None) -> list[str]:
    """What kinds of private data ``text`` holds (descriptions only, never the matched text)."""
    hits: list[str] = []
    home_s = str(home).rstrip("/\\")
    if home_s and home_s not in ("", "/") and home_s in text:
        hits.append("home directory path")
    if _USERS_POSIX in text:
        hits.append("/Users/ path")
    if _USERS_WIN.lower() in text.lower():
        hits.append("C:\\Users\\ path")
    lowered = text.lower()
    for i, word in enumerate(w.strip() for w in deny):
        if word and word.lower() in lowered:
            hits.append(f"deny-listed word #{i + 1}")
    if _EMAIL.search(text):
        hits.append("e-mail address")
    if token and token in text:
        hits.append("OAuth token")
    if _OAT.search(text):
        hits.append("OAuth token pattern")
    return hits


def _env_scan(text: str) -> list[str]:
    deny = [w for w in os.environ.get(DENYLIST_ENV, "").split(",") if w.strip()]
    return scan_private(text, home=Path.home(), deny=deny, token=os.environ.get(TOKEN_ENV) or None)


# --------------------------------------------------------------------------- numbers


def _r(x: float) -> float:
    v = round(float(x), 4)
    return 0.0 if v == 0 else v


def _g(x: float) -> str:
    return f"{_r(x):.4g}"


def format_result(row: Mapping[str, Any]) -> str:
    """One estimate as printed in the report: point, 95 % CI and n."""
    lo, hi = row["ci95"]
    return f"{_g(row['estimate'])} (95 % CI [{_g(lo)}, {_g(hi)}], n = {row['n']})"


def _row(
    metric: str,
    arm: str,
    base: str | None,
    est: Estimate,
    method: str,
    *,
    scale: float = 1.0,
    confirmatory: bool = False,
) -> dict[str, Any]:
    lo, hi = _r(scale * est.lo), _r(scale * est.hi)
    row: dict[str, Any] = {
        "metric": metric,
        "arm": arm,
        "baseline": base,
        "estimate": _r(scale * est.point),
        "ci95": [min(lo, hi), max(lo, hi)],
        "n": est.n,
        "method": method,
        "confirmatory": confirmatory,
    }
    if metric == CONFIRMATORY_METRIC and confirmatory:
        row["margin_pp"] = MARGIN_PP
        row["non_inferior"] = row["ci95"][0] > MARGIN_PP
    return row


def _rate(summ: Summary, arm: str, n_boot: int) -> Estimate | None:
    by_task = summ.resolved(arm)
    means = [sum(v.values()) / len(v) for _, v in sorted(by_task.items()) if v]
    if len(means) < 2:
        return None

    def mean(idx: Sequence[int]) -> float:
        return math.fsum(means[i] for i in idx) / len(idx)

    lo, hi = bca(mean, len(means), n_boot=n_boot)
    return Estimate(mean(range(len(means))), lo, hi, len(means), "bca-task-cluster")


def _results(summ: Summary, n_boot: int) -> list[dict[str, Any]]:
    kw: dict[str, Any] = {"n_boot": n_boot}
    gate = summ.gate(TREATMENT_ARM, BASELINE_ARM, **kw)
    out = [
        _row(
            CONFIRMATORY_METRIC,
            TREATMENT_ARM,
            BASELINE_ARM,
            gate.estimate,
            gate.estimate.method + " (exclusion rule applied)",
            scale=100,
            confirmatory=True,
        )
    ]
    others = [a for a in summ.arms if a != BASELINE_ARM]
    pairs = [(a, BASELINE_ARM) for a in others]
    if STYLE_ARM in summ.arms:
        pairs.append((STYLE_ARM, TREATMENT_ARM))
    for arm, base in pairs:
        try:
            est = summ.resolved_diff(arm, base, **kw)
        except ValueError:
            continue
        out.append(
            _row(CONFIRMATORY_METRIC, arm, base, est, est.method + ", mean over seeds", scale=100)
        )
    for seed in summ.seeds:
        n10, n01, n = summ.discordance(TREATMENT_ARM, BASELINE_ARM, seed=seed)
        if n:
            est = summ.mcnemar(TREATMENT_ARM, BASELINE_ARM, seed=seed)
            method = f"conditional exact (McNemar, Clopper-Pearson), seed {seed}"
            out.append(
                _row(CONFIRMATORY_METRIC, TREATMENT_ARM, BASELINE_ARM, est, method, scale=100)
            )
    for arm, base in pairs:
        try:
            est = summ.cost_ratio(arm, base, **kw)
        except ValueError:
            continue
        out.append(_row("cost_ratio_median", arm, base, est, est.method))
    for arm in summ.arms:
        rate = _rate(summ, arm, n_boot)
        if rate is not None:
            out.append(_row("resolved_rate", arm, None, rate, rate.method + ", mean over seeds"))
    return out


# --------------------------------------------------------------------------- document


def _dates(led: Ledger, stage: str) -> dict[str, str]:
    ats: list[float] = []
    for rec in led.records():
        if rec.get("kind") != "end" or not isinstance(rec.get("at"), int | float):
            continue
        try:
            key = RunKey.from_id(str(rec.get("key")))
        except ValueError:
            continue
        if key.stage == stage:
            ats.append(float(rec["at"]))
    if not ats:
        raise ReportError(f"no runs of stage {stage!r} in the ledger")

    def day(t: float) -> str:
        return dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()

    return {"from": day(min(ats)), "to": day(max(ats))}


def _arms(summ: Summary, header: Mapping[str, Any]) -> list[dict[str, Any]]:
    registry = load_arms()
    pins = header.get("pins") if isinstance(header.get("pins"), Mapping) else {}
    out = []
    for name in summ.arms:
        arm = registry.get(name)
        kind = arm.kind if arm is not None else "plugin"
        if arm is not None and arm.uses_frozen_plugin:
            pin: str | None = str(header.get("plugin_ref") or "") or None
        elif isinstance(pins, Mapping) and isinstance(pins.get(name), str):
            pin = str(pins[name])
        else:
            pin = arm.pin if arm is not None else None
        out.append({"name": name, "kind": kind, "pin": pin})
    return out


def _preflight(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    results = doc.get("results", {})
    return {
        str(i): str(r.get("reason"))
        for i, r in sorted(results.items())
        if isinstance(r, Mapping) and r.get("status") == "excluded"
    }


# Fixed prose for every code e4.json may carry (re-review ruling N1): e4.json holds codes and
# numbers only; these templates are the only text the report adds, and they pass the figure lint.
DISCLOSURE_TEXT: Mapping[str, str] = {
    "labels": "",  # stage-dependent, see `disclosure_text`
    "network": "Network access: agents and the grader had network access; egress was not "
    "restricted (the tools need it, and some evaluation scripts install packages with pip).",
    "competitor_defaults": "Competitors ran in their documented default configuration at the "
    "pins listed above.",
    "tokeneconomizer_headless": "TokenEconomizer ({ran}): it does not learn from headless "
    "`claude -p` sessions, so this comparison understates what it does interactively.",
    "caveman_inert": "caveman ({ran}): its documentation starts scripted `claude -p` runs with "
    "caveman off unless `CAVEMAN_DEFAULT_MODE` is set; a run whose liveness probe found an arm "
    "inert is `infra_fail/load` and never graded (counts per arm above).",
    "headroom_proxy": "headroom ({ran}): `headroom wrap` puts a local proxy on the request path, "
    "so the proxy process handles the OAuth token and all API traffic; its egress was not "
    "restricted either.",
    "platform": "Platform: the pool holds only SWE-bench Verified tasks with an arm64 image (the "
    "agent runs in the image it is graded in); the counts of tasks without one are listed "
    "above, and tasks excluded by the grading preflight are listed below.",
}
_LABELS = {
    "confirmation": "Labels: only the token-sparen vs baseline resolved difference on the gate "
    "seed (the release gate's test) is confirmatory; every other number is descriptive.",
    "screening": "Labels: a screening is descriptive only; it describes the screening freeze "
    "(plugin hash above), not a release, and none of its numbers is a claim.",
}
_DISCLOSURE_ARM = {
    "tokeneconomizer_headless": "tokeneconomizer",
    "caveman_inert": "caveman",
    "headroom_proxy": "headroom",
}
GATE_REASON_TEXT: Mapping[str, str] = {
    "plan_hash_not_release": "the plan's frozen plugin hash is not the release hash",
    "plugin_hash_mismatch": "a run's plugin hash differs from the plan's",
    "frozen_run_without_hash": "a frozen-plugin run has no plugin hash",
    "treatment_not_live": "a token-sparen run was not live",
    "model_mismatch": "a run's model id differs from the plan's",
    "claude_code_version_mismatch": "a run's Claude Code version differs from the plan's",
    "e2_failed": "the E2 fact-preservation suite did not pass",
    "clearly_inferior": "clearly inferior: point below the margin and upper bound below zero",
    "underpowered_owner_decides": "underpowered: not failed; the owner decides in the "
    "gate-decision file",
    "lower_bound_not_above_margin": "the lower bound is not above the margin",
    "too_few_paired_tasks": "too few paired tasks for the gate to pass",
    "one_sided_pending": "one-sided exclusions still await re-runs",
    "excess_exclusions_imputed": "the excess of token-sparen-only exclusions was imputed as "
    "failures",
    "outside_plan_ignored": "runs or exclusions on tasks outside the plan were ignored",
}
EXCLUSION_TEXT: Mapping[str, str] = {
    "run_unrunnable": "unrunnable after its retries",
    "run_incomplete": "no final outcome yet (re-run pending)",
    "run_ungraded": "final run without a grade",
    "preflight_build": "grading preflight: the evaluation image did not build",
    "preflight_gold_fail": "grading preflight: the gold patch did not resolve",
    "preflight_flaky_gold": "grading preflight: the gold patch resolved only once of two",
    "preflight_noop_pass": "grading preflight: a no-op patch resolved the task",
    "preflight_harness_error": "grading preflight: harness error",
}


def disclosure_text(code: str, arms: Sequence[str], stage: str) -> str:
    """The fixed prose of a disclosure code (``{ran}`` from the stage's arms)."""
    if code == "labels":
        return _LABELS["confirmation" if stage == "confirmation" else "screening"]
    arm = _DISCLOSURE_ARM.get(code)
    ran = "ran in this stage" if arm in arms else "not run in this stage"
    return DISCLOSURE_TEXT[code].format(ran=ran)


def _exclusion_line(x: Mapping[str, Any]) -> str:
    text = f"- `{x['instance_id']}`: {EXCLUSION_TEXT[x['reason']]}"
    if "arm" in x:
        text += f"; arm `{x['arm']}`, seed {x['seed']}"
    if x.get("cause"):
        text += f"; cause `{x['cause']}`"
    return text


def _inert_runs(led: Ledger, stage: str, arms: Sequence[str]) -> dict[str, int]:
    out = dict.fromkeys(arms, 0)
    for rec in led.records():
        if rec.get("kind") != "end" or not str(rec.get("status", "")).startswith("infra_fail"):
            continue
        sub = str(rec.get("sub") or str(rec.get("status")).partition("/")[2])
        try:
            key = RunKey.from_id(str(rec.get("key")))
        except ValueError:
            continue
        if key.stage == stage and sub == "load":
            out[key.arm] = out.get(key.arm, 0) + 1
    return out


def _platform() -> dict[str, int]:
    head = read_pool_header()
    mini = head.get("mini_without_arm64")
    verified = head.get("verified_without_arm64")
    return {
        "mini_without_arm64": len(mini) if isinstance(mini, list) else 0,
        "verified_without_arm64": int(verified) if isinstance(verified, int) else 0,
    }


def _report_extra(
    summ: Summary,
    led: Ledger,
    grades: Path,
    stage: str,
    mode: str,
    preflight: Mapping[str, str],
    gate_check: Any,
    n_boot: int,
) -> dict[str, Any]:
    header = led.header
    by_repo: dict[str, dict[str, int]] = {}
    for e in summ.exclusions:
        tasks = by_repo.setdefault(repo_of(e.instance_id), {})
        tasks[e.arm] = tasks.get(e.arm, 0) + 1
    for iid in preflight:
        tasks = by_repo.setdefault(repo_of(iid), {})
        tasks["preflight"] = tasks.get("preflight", 0) + 1
    per_repo = []
    for repo, arms in summ.per_repo().items():
        for arm, c in arms.items():
            lo, hi = wilson(c.resolved, c.runs)
            per_repo.append(
                {
                    "repo": repo,
                    "arm": arm,
                    "tasks": c.tasks,
                    "runs": c.runs,
                    "resolved": c.resolved,
                    "estimate": _r(c.resolved / c.runs),
                    "ci95": [_r(lo), _r(hi)],
                    "n": c.runs,
                    "method": "wilson over runs (seeds not independent; descriptive)",
                }
            )
    pending = sum(1 for e in summ.exclusions if e.reason == "incomplete")
    not_graded = sum(1 for e in summ.exclusions if e.cause == "no_grade")
    oom = grade_oom_repeat_counts(grades, stage=stage)
    confirmed = header.get("price_confirmed_on")
    if confirmed is not None:
        try:
            dt.date.fromisoformat(str(confirmed))
        except ValueError as exc:
            raise ReportError("ledger header price_confirmed_on is not an ISO date") from exc
    price_id = str(header.get("price_table_id") or "")
    if not _PRICE_ID.match(price_id):
        raise ReportError("ledger header lacks a machine-readable price_table_id")
    return {
        "n_boot": n_boot,
        "model_ids": sorted({str(r.model) for r in summ.runs}),
        "claude_code_versions": sorted({str(r.claude_code_version) for r in summ.runs}),
        "run_plugin_hashes": sorted({r.plugin_hash for r in summ.runs if r.plugin_hash}),
        "grade_mode": mode,
        "price_table": {
            "version": price_id,
            "confirmed_live_on": None if confirmed is None else str(confirmed),
        },
        "completeness": {
            "complete": pending == 0 and not_graded == 0,
            "pending_keys": pending,
            "not_yet_graded": not_graded,
        },
        "counts": dict(summ.counts),
        "inert_runs_by_arm": _inert_runs(led, stage, summ.arms),
        "platform": _platform(),
        "exclusions_by_arm": {a: summ.excluded_by_arm.get(a, 0) for a in summ.arms},
        "exclusions_by_repo": {r: dict(sorted(a.items())) for r, a in sorted(by_repo.items())},
        "preflight_excluded": len(preflight),
        "grade_oom_repeat": {a: oom.get(a, 0) for a in summ.arms},
        "gate_check": {
            "paired_tasks": gate_check.estimate.n,
            "min_tasks": gate_check.min_tasks,
            "excluded_arm_only": gate_check.excluded_arm,
            "excluded_reference_only": gate_check.excluded_ref,
            "excluded_both": gate_check.excluded_both,
            "imputed": len(gate_check.imputed),
            "pending": gate_check.pending,
        },
        "per_repo": per_repo,
        "disclosures": list(DISCLOSURE_CODES),
    }


REPORT_KEYS = frozenset(
    {
        "n_boot",
        "model_ids",
        "claude_code_versions",
        "run_plugin_hashes",
        "grade_mode",
        "price_table",
        "completeness",
        "counts",
        "inert_runs_by_arm",
        "platform",
        "exclusions_by_arm",
        "exclusions_by_repo",
        "preflight_excluded",
        "grade_oom_repeat",
        "gate_check",
        "per_repo",
        "disclosures",
    }
)


def _plan_problems(summ: Summary, header: Mapping[str, Any], frozen: set[str]) -> list[str]:
    """Runs that differ from the plan in the ledger header (plugin hash, model, version)."""
    plan = {k: header.get(k) for k in PLAN_FIELDS}
    problems: list[str] = []
    for field in PLAN_FIELDS:
        bad = 0
        for r in summ.runs:
            value = getattr(r, field)
            if field == "plugin_hash" and value is None and r.arm not in frozen:
                continue  # a competitor run without the frozen plugin
            bad += value != plan[field]
        if bad:
            problems.append(f"{bad} run(s) whose {field} differ from the ledger header")
    return problems


def _gate_problems(gate: GateLike, header: Mapping[str, Any]) -> list[str]:
    return [
        f"the gate's {field} differs from the ledger header"
        for field in PLAN_FIELDS
        if getattr(gate, field, None) != header.get(field)
    ]


def _document(
    led: Ledger,
    grades: Path,
    *,
    stage: str,
    gate: GateLike | None,
    power: Mapping[str, Any] | None,
    n_boot: int,
    style_rule: str | None,
    preflight: Path | None,
) -> dict[str, Any]:
    header = led.header
    if header.get("stage") != stage:
        raise ReportError(f"the ledger is of stage {header.get('stage')!r}, not {stage!r}")
    plugin_version = header.get("plugin_version")
    if not isinstance(plugin_version, str) or not plugin_version:
        raise ReportError("the ledger header lacks plugin_version (taken from the header only)")
    summ = summarise(
        led.records(),
        load_grades(grades, stage=stage),
        stage=stage,
        excluded=led.excluded_after_retries(stage),
    )
    for core in (BASELINE_ARM, TREATMENT_ARM):
        if core not in summ.arms:
            raise ReportError(f"stage {stage!r} has no {core!r} runs")
    registry = load_arms()
    frozen = {n for n, a in registry.items() if a.uses_frozen_plugin}
    problems = _plan_problems(summ, header, frozen)
    if gate is not None:
        problems += _gate_problems(gate, header)
    if gate is not None:
        unknown = [r for r in gate.reasons if r not in GATE_REASON_CODES]
        if unknown:
            problems.append(f"{len(unknown)} gate reason(s) are not codes of the closed list")
        check = summ.gate(TREATMENT_ARM, n_boot=n_boot)
        if gate.verdict == "passed" and (
            check.pending or check.estimate.n < check.min_tasks or not check.passed
        ):
            problems.append("the gate verdict 'passed' contradicts the report's gate check")
    if problems:
        raise ReportError("refusing to report: " + "; ".join(problems))
    mode, version = load_grade_mode()
    pre = _preflight(preflight)
    campaign = str(header["campaign"])
    exclusions: list[dict[str, Any]] = [
        {
            "instance_id": e.instance_id,
            "reason": f"run_{e.reason}",
            "arm": e.arm,
            "seed": e.seed,
            "cause": e.cause if e.cause in EXCLUSION_CAUSES else "unknown",
        }
        for e in summ.exclusions
    ]
    for iid, why in pre.items():
        code = f"preflight_{why}"
        reason = code if code in EXCLUSION_REASONS else "preflight_harness_error"
        exclusions.append({"instance_id": iid, "reason": reason})
    if style_rule is None:
        style_rule = "not_run" if STYLE_ARM not in summ.arms else "keep_off"
    gate_doc: dict[str, Any] | None = None
    if gate is not None:
        gate_doc = {"verdict": gate.verdict, "reasons": list(gate.reasons)}
        gate_doc |= {f: getattr(gate, f) for f in PLAN_FIELDS}
        gate_doc |= {f: getattr(gate, f, None) for f in GATE_ANCHOR_FIELDS}
    return {
        "schema_version": E4_SCHEMA_VERSION,
        "source": "E4",
        "suite": SUITE,
        "stage": stage,
        "campaign": campaign,
        "n_tasks": len(
            {r.instance_id for r in summ.runs} | {e.instance_id for e in summ.exclusions}
        ),
        "seeds": max(1, len(summ.seeds)),
        "model": str(header["model"]),
        "claude_code_version": str(header["claude_code_version"]),
        "date": _dates(led, stage),
        "plugin_version": plugin_version,
        "plugin_hash": str(header["plugin_hash"]),
        "harness_commit": str(header["harness_commit"]),
        "script": f"python -m bench report --stage {stage} --campaign {campaign}",
        "grader": {
            "name": "swebench",
            "version": version,
            "arch": "amd64" if mode == "amd64-emulation" else "arm64",
        },
        "arms": _arms(summ, header),
        "exclusions": exclusions,
        "power": None if power is None else dict(power),
        "results": _results(summ, n_boot),
        "gate": gate_doc,
        "style_rule": style_rule,
        "report": _report_extra(
            summ, led, grades, stage, mode, pre, summ.gate(TREATMENT_ARM, n_boot=n_boot), n_boot
        ),
    }


# --------------------------------------------------------------------------- rendering


def _label(row: Mapping[str, Any]) -> str:
    return str(row["arm"]) + (f" vs {row['baseline']}" if row.get("baseline") else "")


def _kind(row: Mapping[str, Any], stage: str) -> str:
    if stage != "confirmation":
        return "screening (descriptive)"
    return "confirmatory" if row["confirmatory"] else "descriptive"


def _method(row: Mapping[str, Any]) -> str:
    degenerate = row["ci95"] == [0, 0] and row["estimate"] == 0
    if row["metric"] == CONFIRMATORY_METRIC and not row["confirmatory"] and degenerate:
        return f"{row['method']} (no discordant pairs)"
    return str(row["method"])


def _confirmatory(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    rows: list[Mapping[str, Any]] = [r for r in doc["results"] if r["confirmatory"]]
    return rows[0]


def _headline(doc: Mapping[str, Any]) -> list[str]:
    """The opening sentence: gate verdict, power verdict and the confirmatory estimate."""
    conf = _confirmatory(doc)
    est = f"{_label(conf)} resolved difference in pp {format_result(conf)}"
    if doc["stage"] != "confirmation":
        return [
            f"**{SCREENING_TITLE}.** Release gate: not evaluated. Power: not run. Screening "
            f"estimate (seed 0, gate method): {est}.",
            "",
        ]
    gate, power = doc["gate"], doc["power"]
    verdict = gate["verdict"] if gate else "not evaluated"
    power_v = power["verdict"] if power else "not run"
    return [
        f"**Release gate: {verdict}.** Power: {power_v}. Confirmatory estimate (seed 0): {est}.",
        "",
    ]


def _conclusion(doc: Mapping[str, Any]) -> str:
    conf = _confirmatory(doc)
    head = f"{_label(conf)}, {format_result(conf)}"
    if doc["stage"] != "confirmation":
        return f"- Screening estimate (paired, seed 0, gate method): {head}; not a claim"
    gate = doc["gate"]
    verdict = gate["verdict"] if gate else "not evaluated"
    gc = doc["report"]["gate_check"]
    if verdict == "passed" and gc["pending"] == 0 and gc["paired_tasks"] >= gc["min_tasks"]:
        return f"- Confirmatory result (paired, margin −5 pp): {head}: non-inferior at −5 pp"
    return f"- Confirmatory result (paired, margin −5 pp): {head}: not established ({verdict})"


def _claims(doc: Mapping[str, Any]) -> list[str]:
    rep = doc["report"]
    d, g, price = doc["date"], doc["grader"], rep["price_table"]
    confirmed = (
        f"confirmed against live pricing on {price['confirmed_live_on']}"
        if price["confirmed_live_on"]
        else "not yet confirmed against live pricing"
    )
    power = doc["power"]
    power_line = (
        "not run (screening)"
        if power is None
        else f"{power['verdict']}; required tasks {power['required_tasks']}, seeds "
        f"{power['seeds']}, achieved power `{power['achieved_power']}` (simulated)"
    )
    gate = doc["gate"]
    gate_line = (
        "not evaluated"
        if gate is None
        else gate["verdict"]
        + (
            f" ({'; '.join(GATE_REASON_TEXT[r] for r in gate['reasons'])})"
            if gate["reasons"]
            else ""
        )
    )
    arms = ", ".join(
        f"`{a['name']}` ({a['kind']}{', pin `' + a['pin'] + '`' if a['pin'] else ''})"
        for a in doc["arms"]
    )
    gc = rep["gate_check"]
    comp, plat = rep["completeness"], rep["platform"]
    return [
        "## Claims",
        "",
        f"- Source: E4, suite `{doc['suite']}`, stage {doc['stage']}, campaign `{doc['campaign']}`",
        f"- Tasks: {doc['n_tasks']}; Seeds: {doc['seeds']}",
        f"- Model ids: {', '.join(f'`{m}`' for m in rep['model_ids'])} "
        f"(plan model `{doc['model']}`)",
        "- Claude Code: " + ", ".join(f"`{v}`" for v in rep["claude_code_versions"]),
        f"- Dates: {d['from']} to {d['to']} (UTC)",
        f"- Plugin version: `{doc['plugin_version']}`",
        f"- Plugin hash: `{doc['plugin_hash']}` (runs: "
        + ", ".join(f"`{h[:12]}`" for h in rep["run_plugin_hashes"])
        + ")",
        f"- Harness commit: `{doc['harness_commit']}`; script: `{doc['script']}`",
        f"- Grader: {g['name']} `{g['version']}`, mode `{rep['grade_mode']}`, arch {g['arch']}, "
        f"graded locally; bootstrap resamples `{rep['n_boot']}`",
        f"- Price table `{price['version']}`; {confirmed}",
        f"- Arms (documented defaults): {arms}",
        _conclusion(doc),
        f"- Power: {power_line}",
        f"- Release gate: {gate_line}; paired tasks {gc['paired_tasks']} (minimum "
        f"{gc['min_tasks']}), one-sided exclusions {gc['excluded_arm_only']} arm / "
        f"{gc['excluded_reference_only']} reference, imputed {gc['imputed']}, "
        f"pending {gc['pending']}",
        f"- Style rule: {doc['style_rule']}",
        "- Exclusions per arm: "
        + ", ".join(f"{a} {n}" for a, n in rep["exclusions_by_arm"].items())
        + f"; grading preflight {rep['preflight_excluded']}",
        "- Inert runs per arm (`infra_fail/load`, never graded): "
        + ", ".join(f"{a} {n}" for a, n in rep["inert_runs_by_arm"].items()),
        f"- Platform: Verified tasks without an arm64 image {plat['verified_without_arm64']}, "
        f"of them in Verified Mini {plat['mini_without_arm64']} (not in the pool)",
        "- grade_oom_repeat (graded unresolved: the patch ran grading out of memory every "
        "time): " + ", ".join(f"{a} {n}" for a, n in rep["grade_oom_repeat"].items()),
        f"- Completeness: pending keys {comp['pending_keys']}, final runs not yet graded "
        f"{comp['not_yet_graded']}",
        "",
        "### Exclusions per repository",
        "",
        "| Repository | Excluded tasks (by arm) |",
        "| --- | --- |",
        *(
            f"| {repo} | " + ", ".join(f"{a} {n}" for a, n in arms_n.items()) + " |"
            for repo, arms_n in rep["exclusions_by_repo"].items()
        ),
        "",
        "### Disclosures",
        "",
        *(
            f"- {disclosure_text(code, [a['name'] for a in doc['arms']], doc['stage'])}"
            for code in rep["disclosures"]
        ),
        "",
    ]


def render_report(doc: Mapping[str, Any]) -> str:
    """``report.md`` from an ``e4.json`` document: a pure function of ``doc``."""
    stage = doc["stage"]
    body = _headline(doc) + _claims(doc)
    body += [
        "## Results" if stage == "confirmation" else f"## {SCREENING_TITLE}",
        "",
        "Paired by task; each estimate with its 95 % CI and n (tasks, or task pairs).",
        "",
        "| Metric | Arm vs reference | Label | Estimate | Method |",
        "| --- | --- | --- | --- | --- |",
        *(
            f"| {r['metric']} | {_label(r)} | {_kind(r, stage)} | {format_result(r)} | "
            f"{_method(r)} |"
            for r in doc["results"]
        ),
        "",
        "## Per repository (descriptive)",
        "",
        "| Repository | Arm | Tasks | Resolved rate (runs) |",
        "| --- | --- | --- | --- |",
        *(
            f"| {p['repo']} | {p['arm']} | {p['tasks']} | {format_result(p)} |"
            for p in doc["report"]["per_repo"]
        ),
        "",
        "## Exclusions",
        "",
        *(_exclusion_line(x) for x in doc["exclusions"]),
        *(["- none"] if not doc["exclusions"] else []),
        "",
        "## Figures",
        "",
        "![Median cost ratio vs baseline](figures/cost_ratio.svg)",
        "",
        "![Resolved difference vs baseline, percentage points](figures/resolved_diff.svg)",
        "",
        "![Cost ratio against resolved difference](figures/pareto.svg)",
    ]
    title = f"# token-sparen E4 benchmark report: {doc['campaign']} {stage}"
    return f"{title}\n\n{generated_block(chr(10).join(body))}\n"


def render_figures(doc: Mapping[str, Any]) -> dict[str, str]:
    """The three SVG figures from an ``e4.json`` document: a pure function of ``doc``."""
    rows = doc["results"]
    stage = doc["stage"]

    def forest_rows(metric: str) -> list[svg.Row]:
        return [
            svg.Row(
                _label(r) + (" *" if r["confirmatory"] and stage == "confirmation" else ""),
                r["estimate"],
                r["ci95"][0],
                r["ci95"][1],
                r["n"],
            )
            for r in rows
            if r["metric"] == metric and r.get("baseline") == BASELINE_ARM
        ]

    cost = {
        r["arm"]: r
        for r in rows
        if r["metric"] == "cost_ratio_median" and r["baseline"] == BASELINE_ARM
    }
    diff = {
        r["arm"]: r
        for r in rows
        if r["metric"] == CONFIRMATORY_METRIC
        and r["baseline"] == BASELINE_ARM
        and not r["confirmatory"]
        and "mean over seeds" in r["method"]
    }
    points = [svg.Point(BASELINE_ARM, 1.0, 0.0)] + [
        svg.Point(a, cost[a]["estimate"], diff[a]["estimate"])
        for a in sorted(cost.keys() & diff.keys())
    ]
    note = "" if stage == "confirmation" else "screening, descriptive; "
    star = "* confirmatory; " if stage == "confirmation" else note
    return {
        "cost_ratio": svg.forest(
            forest_rows("cost_ratio_median"),
            ref=1.0,
            title=f"Median cost ratio vs baseline ({note}95 % CI)",
        ),
        "resolved_diff": svg.forest(
            forest_rows(CONFIRMATORY_METRIC),
            ref=float(MARGIN_PP),
            title=f"Resolved difference vs baseline, pp ({star}95 % CI; reference = margin)",
        ),
        "pareto": svg.scatter(
            points, title="Cost ratio (x) against resolved difference in pp (y), descriptive"
        ),
    }


def _rendered(doc: Mapping[str, Any]) -> dict[str, str]:
    return {
        "report.md": render_report(doc),
        **{f"figures/{k}.svg": v for k, v in render_figures(doc).items()},
    }


def lint_report(report_dir: Path) -> list[str]:
    """Problems of a report directory: ``report.md`` and every figure must equal their fresh
    re-render from the directory's ``e4.json`` byte for byte; ``report.md`` is then linted with
    the generated-section allowance only if it does (else every figure in it is flagged)."""
    try:
        doc = load_e4(report_dir / "e4.json")
    except (OSError, E4Error) as exc:
        return [f"e4.json: {exc}"]
    problems: list[str] = []
    expected = _rendered(doc)
    for rel, text in expected.items():
        try:
            actual = (report_dir / rel).read_text(encoding="utf-8")
        except OSError:
            problems.append(f"{rel}: missing")
            continue
        if actual != text:
            problems.append(f"{rel}: differs from its re-render from e4.json")
        if rel == "report.md":
            problems += [
                f"report.md: figure {v!r}" for v in figure_violations(actual, rendered=text)
            ]
    for where, value in _strings(doc, "e4.json"):
        problems += [f"{where}: figure {v!r}" for v in figure_violations(value)]
    return problems


def _strings(node: Any, where: str) -> list[tuple[str, str]]:
    """Every string leaf of a JSON document with its path (each is figure-linted on its own,
    without the generated-section allowance)."""
    if isinstance(node, str):
        return [(where, node)]
    if isinstance(node, Mapping):
        return [s for k, v in node.items() for s in _strings(v, f"{where}.{k}")]
    if isinstance(node, list):
        return [s for i, v in enumerate(node) for s in _strings(v, f"{where}[{i}]")]
    return []


def _published_problems(doc: Mapping[str, Any]) -> list[str]:
    """Publish-time checks on the document itself (the runs and the gate match the plan)."""
    rep = doc.get("report")
    if not isinstance(rep, Mapping) or set(rep) != REPORT_KEYS:
        return ["the report object does not have exactly the generated keys"]
    problems: list[str] = []
    if rep["disclosures"] != list(DISCLOSURE_CODES):
        problems.append("the disclosures are not the full fixed list")
    comp = rep["completeness"]
    if not isinstance(comp, Mapping) or comp.get("complete") is not True:
        problems.append(f"stage {doc['stage']!r} is not completely graded: {comp!r}")
    if not isinstance(rep["n_boot"], int) or rep["n_boot"] < MIN_PUBLISH_N_BOOT:
        problems.append(f"n_boot {rep['n_boot']!r} < {MIN_PUBLISH_N_BOOT}")
    if doc["stage"] == "confirmation" and not rep["price_table"].get("confirmed_live_on"):
        problems.append("a confirmation needs its price table confirmed against live pricing")
    for key, field in (
        ("run_plugin_hashes", "plugin_hash"),
        ("model_ids", "model"),
        ("claude_code_versions", "claude_code_version"),
    ):
        if rep[key] != [doc[field]]:
            problems.append(f"runs whose {field} differ from the plan ({key})")
    gate = doc["gate"]
    if gate is not None:
        problems += [
            f"the gate's {field} differs from the plan"
            for field in PLAN_FIELDS
            if gate.get(field) != doc[field]
        ]
    return problems


# --------------------------------------------------------------------------- entry points


def build_report(
    ledger: Path,
    grades: Path,
    *,
    stage: str,
    out_dir: Path,
    gate: GateLike | None,
    power: Mapping[str, Any] | None,
    n_boot: int = 10000,
    style_rule: str | None = None,
    preflight: Path | None = None,
) -> Path:
    """Write ``e4.json``, ``report.md`` and ``figures/*.svg`` into ``out_dir``; return
    ``out_dir``. ``gate`` = Task 10's `GateResult` (None in screening; it must carry the
    ``plugin_hash``, ``model`` and ``claude_code_version`` it was evaluated for), ``power`` =
    the power plan's ``as_e4()`` (None in screening); ``style_rule`` defaults to ``not_run``
    (no ``ts-style`` runs) or ``keep_off``; ``preflight`` = the grading preflight's
    ``preflight.json`` (its exclusions are disclosed). Nothing is written when the document is
    invalid, a run or the gate differs from the plan in the ledger header, or any artefact
    holds private data."""
    assert_e4_source(ledger)
    doc = _document(
        Ledger(ledger),
        grades,
        stage=stage,
        gate=gate,
        power=power,
        n_boot=n_boot,
        style_rule=style_rule,
        preflight=preflight,
    )
    problems = validate_e4(doc)
    if problems:
        raise ReportError("invalid e4.json: " + "; ".join(problems))
    files = {"e4.json": json.dumps(doc, indent=2) + "\n", **_rendered(doc)}
    hits = sorted({h for text in files.values() for h in _env_scan(text)})
    if hits:
        raise PrivateDataError("refusing to write the report: " + ", ".join(hits))
    for rel, text in files.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return out_dir


def publish(report_dir: Path, repo: Path) -> list[Path]:
    """Copy a built report into ``repo``: ``docs/benchmarks/<campaign>-<stage>/`` (report,
    figures, ``e4.json``) and, for a confirmation only, ``docs/evidence/`` (the same files; M6
    reads ``docs/evidence/e4.json``). Refused (nothing written) unless ``TOKEN_SPAREN_DENYLIST``
    is set, ``e4.json`` validates, the report and figures equal their re-render from it, the
    stage is completely graded with the full bootstrap, every run and the gate match the plan,
    a confirmation's price table was confirmed against live pricing, and no file holds private
    data."""
    if not [w for w in os.environ.get(DENYLIST_ENV, "").split(",") if w.strip()]:
        raise ReportError(f"{DENYLIST_ENV} is empty: refusing to publish (fail closed)")
    doc = load_e4(report_dir / "e4.json")
    problems = lint_report(report_dir) + _published_problems(doc)
    if problems:
        raise ReportError("refusing to publish: " + "; ".join(problems))
    names = ["report.md", "e4.json", *(f"figures/{f}.svg" for f in FIGURES)]
    texts = {n: (report_dir / n).read_text(encoding="utf-8") for n in names}
    hits = sorted({h for text in texts.values() for h in _env_scan(text)})
    if hits:
        raise PrivateDataError("refusing to publish: " + ", ".join(hits))
    dests = [repo / "docs" / "benchmarks" / f"{doc['campaign']}-{doc['stage']}"]
    if doc["stage"] == "confirmation":
        dests.append(repo / "docs" / "evidence")
    targets = {dest / n: texts[n] for dest in dests for n in names}
    for path, text in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(targets)


__all__: Sequence[str] = (
    "GateLike",
    "NotE4Error",
    "PrivateDataError",
    "ReportError",
    "assert_e4_source",
    "build_report",
    "disclosure_text",
    "format_result",
    "generated_block",
    "lint_report",
    "publish",
    "render_figures",
    "render_report",
    "scan_private",
)
