"""External anchors of the confirmation evidence (M5 Task 10, final tamper-evidence ruling).

The confirmation ledger and its grades are hash-chained (`bench.ledger.record_sha256`), but a
chain only proves that what remains is a consistent prefix: truncating both files and
re-running would leave valid chains. So during confirmation the scheduler appends the current
heads of both chains, with their record counts, to ``anchors.jsonl`` next to the ledger after
every graded task (`record_anchor`; the anchors file is itself append-only and hash-chained).
Task 12's CLI publishes `anchors_to_publish` (commits the whole file at `branch_path` and
pushes the ``bench-anchors`` branch of the public repository) at least once per five-hour window
and once after the campaign. The release gate reads every publication itself from git
(`read_published`; never a caller-supplied list) and refuses (`check_published`) unless each
publication extends the previous one, every anchored head lies on the final chains (same record
at the same count, i.e. a prefix), the last anchor equals the final heads, and no two
consecutive publications are more than five hours apart (commit times).

Trust (re-review N7): commit times and local refs are set by the publisher, so the
authoritative check runs in the release job (CI) against a freshly fetched
``origin/bench-anchors`` with server-side receipts: the ``anchor-receipt`` workflow runs on every
push to that branch, and its runs' ``head_sha`` and ``created_at`` (GitHub API,
`fetch_receipts`) date each publication (`verify_release`, ``python -m bench.anchors
verify-release``). Without receipts the local gate is advisory only.

Coverage (re-review N8): the first publication anchors the plan before any run (its last
anchor ends at the plan record: zero starts, the plan hash), and each later publication adds
at most `RUN_CAP` (the per-window run cap) graded runs; with gaps of at most five hours this
spreads the publications over the whole campaign. Task 12's CLI pushes ONE commit per push
(a receipt names only the pushed tip).

Residual (disclosed in e4.json as ``anchor_window_operator_trust``): records written after the
last published anchor could still be rewritten by the operator before the next publication.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from bench.grade import grades_path
from bench.ledger import (
    CHAIN_FIELD,
    GENESIS,
    GRADED,
    PROTECTED_STAGE,
    Ledger,
    LedgerError,
    _write_line,
    chain_head,
    read_jsonl,
    record_sha256,
)
from bench.quota import QuotaPolicy
from token_sparen.store.jsonstore import locked

ANCHORS_FILE = "anchors.jsonl"
ANCHOR_KIND = "anchor"
LOCK_TIMEOUT_S = 30.0
ANCHOR_KEYS = (
    "ledger_count",
    "ledger_head",
    "grades_count",
    "grades_head",
    "runs_started",  # confirmation starts in the anchored ledger prefix
    "graded_runs",  # confirmation ends with a graded status in it
    "plan_sha256",  # the recorded plan's hash (None before the plan)
)
# re-review N8: at most this many graded runs between consecutive publications (the per-window
# run cap of the quota policy), and the first publication anchors the plan before any run
RUN_CAP = QuotaPolicy().max_runs_per_window


def anchors_path(ledger: Ledger) -> Path:
    return ledger.path.with_name(ANCHORS_FILE)


def _grade_records(ledger: Ledger) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(grades_path(ledger)) if r.get("stage") == PROTECTED_STAGE]


def prefix_heads(records: Sequence[Mapping[str, Any]], *, genesis: str) -> list[str]:
    """``heads[i]`` = the chain head after the first ``i`` records (``heads[0]`` = genesis)."""
    heads = [genesis]
    for rec in records:
        heads.append(record_sha256(rec))
    return heads


def _is_run(rec: Mapping[str, Any], kind: str) -> bool:
    key = rec.get("key")
    return (
        rec.get("kind") == kind
        and isinstance(key, str)
        and key.split("/")[1:2] == [PROTECTED_STAGE]
        and (kind == "start" or rec.get("status") in GRADED)
    )


def prefix_counts(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[int], list[int], list[str | None]]:
    """Per prefix length ``i``: confirmation starts, graded ends, and the plan hash so far."""
    started, graded = [0], [0]
    plan: list[str | None] = [None]
    for rec in records:
        started.append(started[-1] + _is_run(rec, "start"))
        graded.append(graded[-1] + _is_run(rec, "end"))
        sha = rec.get("plan_sha256") if rec.get("kind") == "plan" else None
        plan.append(plan[-1] if sha is None else str(sha))
    return started, graded, plan


def current_heads(ledger: Ledger) -> dict[str, Any]:
    """Counts and verified heads of the ledger and grades chains (`LedgerError` on a break)."""
    grades = _grade_records(ledger)
    records = ledger.records()
    started, graded, plan = prefix_counts(records)
    return {
        "ledger_count": len(records),
        "ledger_head": ledger.chain_head(),
        "grades_count": len(grades),
        "grades_head": chain_head(grades, genesis=GENESIS),
        "runs_started": started[-1],
        "graded_runs": graded[-1],
        "plan_sha256": plan[-1],
    }


def record_anchor(ledger: Ledger, *, at: float | None = None) -> dict[str, Any]:
    """Append the current heads to ``anchors.jsonl`` (hash-chained from `GENESIS`)."""
    if not ledger.chained:
        raise LedgerError("anchors are recorded for a confirmation ledger only")
    rec: dict[str, Any] = {
        "kind": ANCHOR_KIND,
        "at": time.time() if at is None else float(at),
        "campaign": ledger.campaign,
        **current_heads(ledger),
    }
    path = anchors_path(ledger)
    with locked(path, timeout=LOCK_TIMEOUT_S):
        existing = read_jsonl(path)
        rec[CHAIN_FIELD] = record_sha256(existing[-1]) if existing else GENESIS
        _write_line(path, rec)
    return rec


def anchors_to_publish(ledger: Ledger) -> list[dict[str, Any]]:
    """Every local anchor, its own chain verified: what the CLI commits and pushes to the
    ``bench-anchors`` branch (at least once per five-hour window)."""
    records = read_jsonl(anchors_path(ledger))
    chain_head(records, genesis=GENESIS)
    return records


def verify_anchors(ledger: Ledger, anchors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Problems with the published ``anchors`` against the ledger's and grades' final chains:
    every anchored head must lie on them at its record count (a prefix). Empty = consistent."""
    if not anchors:
        return ["no published anchor"]
    problems: list[str] = []
    try:
        chain_head(list(anchors), genesis=GENESIS)
    except LedgerError as exc:
        problems.append(f"published anchors: {exc}")
    records = ledger.records()
    ledger_heads = prefix_heads(records, genesis=record_sha256(ledger.header))
    grade_heads = prefix_heads(_grade_records(ledger), genesis=GENESIS)
    counts = prefix_counts(records)
    for i, a in enumerate(anchors):
        if any(k not in a for k in ANCHOR_KEYS) or a.get("campaign") != ledger.campaign:
            problems.append(f"anchor {i + 1}: malformed or of another campaign")
            continue
        for name, heads in (("ledger", ledger_heads), ("grades", grade_heads)):
            count = a[f"{name}_count"]
            if not isinstance(count, int) or not 0 <= count < len(heads):
                problems.append(f"anchor {i + 1}: the {name} is shorter than anchored")
            elif heads[count] != a[f"{name}_head"]:
                problems.append(f"anchor {i + 1}: the {name} differs from its anchored prefix")
        n = a["ledger_count"]
        if isinstance(n, int) and 0 <= n < len(ledger_heads):
            expect = (counts[0][n], counts[1][n], counts[2][n])
            if expect != (a["runs_started"], a["graded_runs"], a["plan_sha256"]):
                problems.append(f"anchor {i + 1}: its run counts or plan differ from the ledger")
    return problems


# --------------------------------------------------------------------------- published branch

ANCHORS_REF = "origin/bench-anchors"
MAX_GAP_S = 5 * 3600  # at least one publication per five-hour window
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def branch_path(campaign: str) -> str:
    """Where the CLI publishes a campaign's anchors on the ``bench-anchors`` branch: the whole
    local ``anchors.jsonl`` (append-only), one commit per publication."""
    return f"anchors/{campaign}.jsonl"


class AnchorError(ValueError):
    """The published anchors cannot be read (``code``: an e4-independent refusal code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Publication:
    commit: str
    at: int  # committer time of the publishing commit (epoch seconds)
    anchors: tuple[dict[str, Any], ...]  # the file as of that commit


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if out.returncode != 0:
        raise AnchorError("anchors_missing", f"git {args[0]}: {out.stderr.strip()[:200]}")
    return out.stdout


def read_published(repo: Path, ref: str, campaign: str) -> list[Publication]:
    """Every commit of ``ref`` (first-parent history, oldest first) that changed the campaign's
    anchors file, with the file's content at that commit; read from git objects only."""
    path = branch_path(campaign)
    log = _git(repo, "log", "--first-parent", "--reverse", "--format=%H %ct", ref, "--", path)
    pubs: list[Publication] = []
    for line in log.splitlines():
        sha, _, ct = line.partition(" ")
        text = _git(repo, "show", f"{sha}:{path}")
        try:
            docs = [json.loads(x) for x in text.splitlines() if x.strip()]
        except ValueError as exc:
            raise AnchorError("anchor_history_rewritten", f"{sha}: not JSON lines") from exc
        pubs.append(Publication(sha, int(ct), tuple(docs)))
    if not pubs:
        raise AnchorError("anchors_missing", f"no published anchors for {campaign} on {ref}")
    return pubs


class Receipt(NamedTuple):
    """A server-side receipt of one push to ``bench-anchors``: the ``anchor-receipt`` workflow
    run's ``head_sha`` and ``created_at`` (epoch seconds), from the GitHub API."""

    sha: str
    server_time: float


RECEIPT_WORKFLOW = "anchor-receipt.yml"
ANCHORS_BRANCH = "bench-anchors"
API = "https://api.github.com"


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fetch_receipts(
    github_repo: str,
    token: str | None,
    *,
    opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
    max_pages: int = 50,
) -> list[Receipt]:
    """Every push run of the receipt workflow on ``bench-anchors`` (GitHub REST API, paginated;
    in CI with the job's token): the server-side publication times."""
    out: list[Receipt] = []
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {"branch": ANCHORS_BRANCH, "event": "push", "per_page": 100, "page": page}
        )
        url = f"{API}/repos/{github_repo}/actions/workflows/{RECEIPT_WORKFLOW}/runs?{query}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})  # noqa: S310
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with opener(req) as resp:
            runs = json.loads(resp.read().decode("utf-8")).get("workflow_runs", [])
        out += [Receipt(str(r["head_sha"]), _epoch(str(r["created_at"]))) for r in runs]
        if len(runs) < 100:
            break
    return out


def check_history(
    pubs: Sequence[Publication], receipts: Sequence[Receipt] | None
) -> list[tuple[str, str]]:
    """(code, detail) problems of the publication history alone: append-only, the anchors'
    own chain, and the five-hour rule. With ``receipts`` every publishing commit needs a
    server receipt and the gaps use the earliest receipt time per commit (authoritative);
    without, the gaps use commit times (advisory: the publisher sets them)."""
    if not pubs or not pubs[-1].anchors:
        return [("anchors_missing", "no published anchor")]
    problems: list[tuple[str, str]] = []
    final = list(pubs[-1].anchors)
    for p in pubs:
        if not p.anchors or list(p.anchors) != final[: len(p.anchors)]:
            problems.append(("anchor_history_rewritten", f"{p.commit[:12]} is not a prefix"))
    try:
        chain_head(final, genesis=GENESIS)
    except LedgerError as exc:
        problems.append(("anchor_history_rewritten", f"published anchors: {exc}"))
    times: list[float] = [float(p.at) for p in pubs]
    if receipts is not None:
        first: dict[str, float] = {}
        for r in receipts:
            first[r.sha] = min(first.get(r.sha, r.server_time), r.server_time)
        missing = [p.commit for p in pubs if p.commit not in first]
        if missing:
            problems.append(
                ("anchor_without_receipt", f"{len(missing)} publication(s) without a server "
                 f"receipt, e.g. {missing[0][:12]}")
            )  # fmt: skip
            return problems
        times = [first[p.commit] for p in pubs]
    for (_, b), when in zip(itertools.pairwise(pubs), itertools.pairwise(times), strict=True):
        if when[1] - when[0] > MAX_GAP_S:
            gap = (when[1] - when[0]) / 3600
            problems.append(("anchor_gap", f"{gap:.1f} h before publication {b.commit[:12]}"))
    return problems + _coverage(pubs, times)


def _coverage(pubs: Sequence[Publication], times: Sequence[float]) -> list[tuple[str, str]]:
    """Re-review N8: the first publication anchors the plan before any run (zero starts, a
    plan hash), and each later one adds at most `RUN_CAP` graded runs. Re-review N9 (time
    floor): a publication received at ``t`` anchors at most ``RUN_CAP × (ceil((t − t_plan) /
    5 h) + 1)`` graded runs (the windows the span can touch, the first possibly opening right
    after the plan), so a campaign cannot be published after the fact in rapid pushes.
    ``times`` = receipt times (commit times when advisory)."""
    out: list[tuple[str, str]] = []
    head = pubs[0].anchors[-1] if pubs[0].anchors else {}
    if head.get("runs_started") != 0 or head.get("graded_runs") != 0 or not head.get("plan_sha256"):
        out.append(("anchor_first_not_plan", "the first publication is not the plan before runs"))
    for a, b in itertools.pairwise(pubs):
        before, after = a.anchors[-1].get("graded_runs"), b.anchors[-1].get("graded_runs")
        if not isinstance(before, int) or not isinstance(after, int) or after - before > RUN_CAP:
            out.append(
                ("anchor_too_many_runs", f"{b.commit[:12]} adds more than {RUN_CAP} graded runs")
            )
    for p, t in zip(pubs, times, strict=True):
        runs = p.anchors[-1].get("graded_runs") if p.anchors else None
        allowed = RUN_CAP * (max(0, math.ceil((t - times[0]) / MAX_GAP_S)) + 1)
        if not isinstance(runs, int) or runs > allowed:
            out.append(
                ("anchor_too_fast", f"{p.commit[:12]} anchors {runs} graded runs, more than "
                 f"{allowed} possible since the plan was received")
            )  # fmt: skip
            break
    return out


def check_published(
    ledger: Ledger, pubs: Sequence[Publication], receipts: Sequence[Receipt] | None = None
) -> list[tuple[str, str]]:
    """`check_history`, plus: every anchor lies on the final chains and the last anchor equals
    the final heads."""
    problems = check_history(pubs, receipts)
    if any(code == "anchors_missing" for code, _ in problems):
        return problems
    final = list(pubs[-1].anchors)
    problems += [("anchor_not_on_chain", text) for text in verify_anchors(ledger, final)]
    try:
        heads = current_heads(ledger)
    except LedgerError as exc:
        return [*problems, ("anchor_not_on_chain", str(exc))]
    if any(final[-1].get(k) != heads[k] for k in ANCHOR_KEYS):
        problems.append(("anchor_not_final", "the last published anchor is not the final heads"))
    first = pubs[0].anchors[-1] if pubs[0].anchors else {}
    n = first.get("ledger_count")
    records = ledger.records()
    if not isinstance(n, int) or not 0 < n <= len(records) or records[n - 1].get("kind") != "plan":
        problems.append(("anchor_first_not_plan", "the first publication does not end at the plan"))
    return problems


def verify_release(
    evidence: Mapping[str, Any],
    pubs: Sequence[Publication],
    receipts: Sequence[Receipt],
) -> list[tuple[str, str]]:
    """The release job's authoritative check (no ledger there): the freshly fetched
    publication history with server receipts, and its last anchor equal to the chain heads the
    published e4.json carries (`bench.e4.GATE_ANCHOR_FIELDS`)."""
    problems = check_history(pubs, receipts)
    if any(code == "anchors_missing" for code, _ in problems):
        return problems
    gate = evidence.get("gate") or {}
    last = pubs[-1].anchors[-1]
    if last.get("campaign") != evidence.get("campaign"):
        problems.append(("anchor_not_final", "the last anchor is of another campaign"))
    for field, key in (("ledger_chain_head", "ledger_head"), ("grades_chain_head", "grades_head")):
        if gate.get(field) is None or gate.get(field) != last.get(key):
            problems.append(("anchor_not_final", f"e4.json gate.{field} is not the last anchor"))
    if pubs[0].anchors and pubs[0].anchors[-1].get("plan_sha256") != gate.get("plan_sha256"):
        problems.append(("anchor_first_not_plan", "the first publication is not e4.json's plan"))
    return problems


def main(argv: list[str] | None = None) -> int:
    """``python -m bench.anchors verify-release --evidence E --repo R --ref REF
    --github-repo OWNER/NAME``: the release job's check (token from ``GH_TOKEN``)."""
    parser = argparse.ArgumentParser(prog="python -m bench.anchors")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify-release")
    v.add_argument("--evidence", type=Path, required=True)
    v.add_argument("--repo", type=Path, default=Path("."))
    v.add_argument("--ref", default=ANCHORS_REF)
    v.add_argument("--github-repo", required=True)
    args = parser.parse_args(argv)
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    if evidence.get("stage") != "confirmation":
        print("not confirmation evidence: no anchors to verify")
        return 0
    try:
        pubs = read_published(args.repo, args.ref, str(evidence["campaign"]))
    except AnchorError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    receipts = fetch_receipts(args.github_repo, os.environ.get("GH_TOKEN"))
    problems = verify_release(evidence, pubs, receipts)
    for code, detail in problems:
        print(f"{code}: {detail}", file=sys.stderr)
    if not problems:
        print(f"anchors verified: {len(pubs)} publication(s) with server receipts")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
