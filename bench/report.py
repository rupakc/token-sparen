"""Public E4 report and `e4.json` (M5 Task 11).

`build_report` turns one stage's E4 ledger and grades into ``e4.json`` (valid per
`bench.e4.validate_e4`; M6's README renderer and release gate read it), ``report.md`` and three
SVG figures. Every figure in ``report.md`` is rendered from the same ``e4.json`` document (no
hand-typed number): the published fields plus one additive ``report`` object (exclusions per arm
and repository, per-repository rates, ``grade_oom_repeat`` counts, price-table confirmation,
completeness and the disclosures). The rendered body sits in a hashed generated section
(`scripts.docs_lint.generated_block`), the only way it passes the docs figure lint.

E4 only: `assert_e4_source` refuses anything but an E4 ledger (E1 replay output never becomes a
public figure), and `scan_private` refuses to write when any artefact holds a home path, a
deny-listed word (``TOKEN_SPAREN_DENYLIST``), an e-mail address or an OAuth token.

`publish` copies a finished report into the repository: ``docs/benchmarks/<campaign>-<stage>/``
always, ``docs/evidence/e4.json`` (what M6 reads) only for a confirmation; refused unless the
document validates and the stage is completely graded.
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
    MARGIN_PP,
    TREATMENT_ARM,
    load_e4,
    validate_e4,
)
from bench.e4 import SCHEMA_VERSION as E4_SCHEMA_VERSION
from bench.grade import grade_oom_repeat_counts, load_grade_mode, load_grades
from bench.leak import TOKEN_ENV
from bench.ledger import LEDGER_KIND, SCHEMA, Ledger, RunKey
from bench.stats import Estimate, Summary, bca, repo_of, summarise, wilson
from scripts.docs_lint import figure_violations, generated_block

SUITE = "swe-bench-verified-arm64-pool"
STYLE_ARM = "ts-style"
DENYLIST_ENV = "TOKEN_SPAREN_DENYLIST"
FIGURES = ("cost_ratio", "resolved_diff", "pareto")
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
    """Task 10's `GateResult` (read-only view)."""

    @property
    def verdict(self) -> str: ...

    @property
    def reasons(self) -> Sequence[str]: ...


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


def _disclosures(arms: Sequence[str], stage: str) -> list[str]:
    def ran(name: str) -> str:
        return "ran in this stage" if name in arms else "not run in this stage"

    out = [
        "Labels: only the token-sparen vs baseline resolved difference on seed 0 (the release "
        "gate's test) is confirmatory; every other number is descriptive.",
        "Network access: agents and the grader had network access; egress was not restricted "
        "(the tools need it, and some evaluation scripts install packages with pip).",
        "Competitors ran in their documented default configuration at the pins listed above.",
        f"TokenEconomizer ({ran('tokeneconomizer')}): it does not learn from headless "
        "`claude -p` sessions, so this comparison understates what it does interactively.",
        f"caveman ({ran('caveman')}): its documentation starts scripted `claude -p` runs with "
        "caveman off unless `CAVEMAN_DEFAULT_MODE` is set; a run whose liveness probe found an "
        "arm inert is `infra_fail/load` and never graded.",
        f"headroom ({ran('headroom')}): `headroom wrap` puts a local proxy on the request path, "
        "so the proxy process handles the OAuth token and all API traffic; its egress was not "
        "restricted either.",
        "Platform: the pool holds only SWE-bench Verified tasks with an arm64 image (the agent "
        "runs in the image it is graded in); tasks without one are not in the pool, and tasks "
        "excluded by the grading preflight are listed below.",
    ]
    if stage == "screening":
        out.insert(
            0,
            "Screening: every number describes the screening freeze (plugin hash above), not a "
            "release; the confirmatory row is labelled so only for its method.",
        )
    return out


def _report_extra(
    summ: Summary,
    led: Ledger,
    grades: Path,
    stage: str,
    mode: str,
    preflight: Mapping[str, str],
    gate_check: Any,
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
    return {
        "model_ids": sorted({r.model for r in summ.runs if r.model} or {str(header["model"])}),
        "claude_code_versions": sorted(
            {r.claude_code_version for r in summ.runs if r.claude_code_version}
            or {str(header["claude_code_version"])}
        ),
        "run_plugin_hashes": sorted({r.plugin_hash for r in summ.runs if r.plugin_hash}),
        "grade_mode": mode,
        "price_table": {
            "version": str(header.get("price_table_version") or ""),
            "confirmed_live_on": None if confirmed is None else str(confirmed),
        },
        "completeness": {
            "complete": pending == 0 and not_graded == 0,
            "pending_keys": pending,
            "not_yet_graded": not_graded,
        },
        "counts": dict(summ.counts),
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
        "disclosures": _disclosures(summ.arms, stage),
    }


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
    summ = summarise(
        led.records(),
        load_grades(grades, stage=stage),
        stage=stage,
        excluded=led.excluded_after_retries(stage),
    )
    for core in (BASELINE_ARM, TREATMENT_ARM):
        if core not in summ.arms:
            raise ReportError(f"stage {stage!r} has no {core!r} runs")
    mode, version = load_grade_mode()
    pre = _preflight(preflight)
    campaign = str(header["campaign"])
    plugin_version = str(header.get("plugin_version") or "")
    if not plugin_version:
        plugin_version = str(header.get("plugin_ref") or "unknown").removeprefix("v")
    exclusions = [
        {
            "instance_id": e.instance_id,
            "reason": f"{e.reason} ({e.cause or 'pending re-run'}) for arm {e.arm}, seed {e.seed}",
        }
        for e in summ.exclusions
    ]
    exclusions += [
        {"instance_id": i, "reason": f"grading preflight: {why} ({mode})"} for i, why in pre.items()
    ]
    if style_rule is None:
        style_rule = "not_run" if STYLE_ARM not in summ.arms else "keep_off"
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
        "gate": None if gate is None else {"verdict": gate.verdict, "reasons": list(gate.reasons)},
        "style_rule": style_rule,
        "report": _report_extra(
            summ, led, grades, stage, mode, pre, summ.gate(TREATMENT_ARM, n_boot=n_boot)
        ),
    }


# --------------------------------------------------------------------------- rendering


def _label(row: Mapping[str, Any]) -> str:
    return str(row["arm"]) + (f" vs {row['baseline']}" if row.get("baseline") else "")


def _kind(row: Mapping[str, Any], stage: str) -> str:
    if not row["confirmatory"]:
        return "descriptive"
    return "confirmatory" if stage == "confirmation" else "confirmatory metric (screening)"


def _confirmatory(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    rows: list[Mapping[str, Any]] = [r for r in doc["results"] if r["confirmatory"]]
    return rows[0]


def _claims(doc: Mapping[str, Any]) -> list[str]:
    rep = doc["report"]
    d, g, price = doc["date"], doc["grader"], rep["price_table"]
    conf = _confirmatory(doc)
    verdict = "non-inferior" if conf["non_inferior"] else "not shown non-inferior"
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
        else gate["verdict"] + (f" ({'; '.join(gate['reasons'])})" if gate["reasons"] else "")
    )
    arms = ", ".join(
        f"`{a['name']}` ({a['kind']}{', pin `' + a['pin'] + '`' if a['pin'] else ''})"
        for a in doc["arms"]
    )
    gc = rep["gate_check"]
    comp = rep["completeness"]
    return [
        "## Claims",
        "",
        f"- Source: E4, suite `{doc['suite']}`, stage {doc['stage']}, campaign `{doc['campaign']}`",
        f"- Tasks: {doc['n_tasks']}; Seeds: {doc['seeds']}",
        f"- Model ids: {', '.join(f'`{m}`' for m in rep['model_ids'])} "
        f"(campaign model `{doc['model']}`)",
        "- Claude Code: " + ", ".join(f"`{v}`" for v in rep["claude_code_versions"]),
        f"- Dates: {d['from']} to {d['to']} (UTC)",
        f"- Plugin version: `{doc['plugin_version']}`",
        f"- Plugin hash: `{doc['plugin_hash']}` (runs: "
        + ", ".join(f"`{h[:12]}`" for h in rep["run_plugin_hashes"])
        + ")",
        f"- Harness commit: `{doc['harness_commit']}`; script: `{doc['script']}`",
        f"- Grader: {g['name']} `{g['version']}`, mode `{rep['grade_mode']}`, arch {g['arch']}, "
        "graded locally",
        f"- Price table: {price['version']}; {confirmed}",
        f"- Arms (documented defaults): {arms}",
        f"- Non-inferiority (paired, margin −5 pp): {_label(conf)}, {format_result(conf)}: "
        f"{verdict}",
        f"- Power: {power_line}",
        f"- Release gate: {gate_line}; paired tasks {gc['paired_tasks']} (minimum "
        f"{gc['min_tasks']}), one-sided exclusions {gc['excluded_arm_only']} arm / "
        f"{gc['excluded_reference_only']} reference, imputed {gc['imputed']}, "
        f"pending {gc['pending']}",
        f"- Style rule: {doc['style_rule']}",
        "- Exclusions per arm: "
        + ", ".join(f"{a} {n}" for a, n in rep["exclusions_by_arm"].items())
        + f"; grading preflight {rep['preflight_excluded']}",
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
        *(f"- {line}" for line in rep["disclosures"]),
        "",
    ]


def render_report(doc: Mapping[str, Any]) -> str:
    """``report.md`` from an ``e4.json`` document (every figure comes from ``doc``)."""
    stage = doc["stage"]
    body = _claims(doc)
    body += [
        "## Results",
        "",
        "Paired by task; each estimate with its 95 % CI and n (tasks, or task pairs).",
        "",
        "| Metric | Arm vs reference | Label | Estimate | Method |",
        "| --- | --- | --- | --- | --- |",
        *(
            f"| {r['metric']} | {_label(r)} | {_kind(r, stage)} | {format_result(r)} | "
            f"{r['method']} |"
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
        *(f"- `{x['instance_id']}`: {x['reason']}" for x in doc["exclusions"]),
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
    rows = doc["results"]

    def forest_rows(metric: str) -> list[svg.Row]:
        return [
            svg.Row(
                f"{_label(r)} ({_kind(r, doc['stage'])})",
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
    return {
        "cost_ratio": svg.forest(
            forest_rows("cost_ratio_median"),
            ref=1.0,
            title="Median cost ratio vs baseline (95 % CI)",
        ),
        "resolved_diff": svg.forest(
            forest_rows(CONFIRMATORY_METRIC),
            ref=float(MARGIN_PP),
            title="Resolved difference vs baseline, pp (95 % CI; reference = margin)",
        ),
        "pareto": svg.scatter(
            points, title="Cost ratio (x) against resolved difference in pp (y), descriptive"
        ),
    }


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
    ``out_dir``. ``gate`` = Task 10's `GateResult` (None in screening), ``power`` = the
    power plan's ``as_e4()`` (None in screening); ``style_rule`` defaults to ``not_run`` (no
    ``ts-style`` runs) or ``keep_off``; ``preflight`` = the grading preflight's
    ``preflight.json`` (its exclusions are disclosed). Nothing is written when the document is
    invalid or any artefact holds private data."""
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
    files = {
        "e4.json": json.dumps(doc, indent=2) + "\n",
        "report.md": render_report(doc),
        **{f"figures/{k}.svg": v for k, v in render_figures(doc).items()},
    }
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
    figures, ``e4.json``) and, for a confirmation only, ``docs/evidence/e4.json``. Refused
    (nothing written) unless ``e4.json`` validates, the stage is completely graded, a
    confirmation's price table was confirmed against live pricing, the report passes the figure
    lint and no file holds private data."""
    doc = load_e4(report_dir / "e4.json")
    rep = doc.get("report")
    comp = rep.get("completeness") if isinstance(rep, Mapping) else None
    if not isinstance(comp, Mapping) or comp.get("complete") is not True:
        raise ReportError(f"stage {doc['stage']!r} is not completely graded: {comp!r}")
    price = rep.get("price_table") if isinstance(rep, Mapping) else None
    if doc["stage"] == "confirmation" and not (
        isinstance(price, Mapping) and price.get("confirmed_live_on")
    ):
        raise ReportError("a confirmation needs its price table confirmed against live pricing")
    names = ["report.md", "e4.json", *(f"figures/{f}.svg" for f in FIGURES)]
    texts = {n: (report_dir / n).read_text(encoding="utf-8") for n in names}
    if figure_violations(texts["report.md"], allow_generated=True):
        raise ReportError("report.md has figures outside its generated section")
    hits = sorted({h for text in texts.values() for h in _env_scan(text)})
    if hits:
        raise PrivateDataError("refusing to publish: " + ", ".join(hits))
    dest = repo / "docs" / "benchmarks" / f"{doc['campaign']}-{doc['stage']}"
    targets = {dest / n: texts[n] for n in names}
    if doc["stage"] == "confirmation":
        targets[repo / "docs" / "evidence" / "e4.json"] = texts["e4.json"]
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
    "format_result",
    "publish",
    "render_figures",
    "render_report",
    "scan_private",
)
