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

Residual (disclosed in e4.json as ``anchor_window_operator_trust``): records written after the
last published anchor could still be rewritten by the operator before the next publication.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.grade import grades_path
from bench.ledger import (
    CHAIN_FIELD,
    GENESIS,
    PROTECTED_STAGE,
    Ledger,
    LedgerError,
    _write_line,
    chain_head,
    read_jsonl,
    record_sha256,
)
from token_sparen.store.jsonstore import locked

ANCHORS_FILE = "anchors.jsonl"
ANCHOR_KIND = "anchor"
LOCK_TIMEOUT_S = 30.0
ANCHOR_KEYS = ("ledger_count", "ledger_head", "grades_count", "grades_head")


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


def current_heads(ledger: Ledger) -> dict[str, Any]:
    """Counts and verified heads of the ledger and grades chains (`LedgerError` on a break)."""
    grades = _grade_records(ledger)
    return {
        "ledger_count": len(ledger.records()),
        "ledger_head": ledger.chain_head(),
        "grades_count": len(grades),
        "grades_head": chain_head(grades, genesis=GENESIS),
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
    ledger_heads = prefix_heads(ledger.records(), genesis=record_sha256(ledger.header))
    grade_heads = prefix_heads(_grade_records(ledger), genesis=GENESIS)
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


def check_published(ledger: Ledger, pubs: Sequence[Publication]) -> list[tuple[str, str]]:
    """(code, detail) problems of the published history against the final chains: each
    publication extends the previous one (append-only); every anchor lies on the final chains;
    the last anchor equals the final heads; consecutive publications at most `MAX_GAP_S`
    apart (commit times)."""
    if not pubs or not pubs[-1].anchors:
        return [("anchors_missing", "no published anchor")]
    problems: list[tuple[str, str]] = []
    final = list(pubs[-1].anchors)
    for p in pubs:
        if not p.anchors or list(p.anchors) != final[: len(p.anchors)]:
            problems.append(("anchor_history_rewritten", f"{p.commit[:12]} is not a prefix"))
    problems += [("anchor_not_on_chain", text) for text in verify_anchors(ledger, final)]
    try:
        heads = current_heads(ledger)
    except LedgerError as exc:
        return [*problems, ("anchor_not_on_chain", str(exc))]
    if any(final[-1].get(k) != heads[k] for k in ANCHOR_KEYS):
        problems.append(("anchor_not_final", "the last published anchor is not the final heads"))
    for a, b in itertools.pairwise(pubs):
        if b.at - a.at > MAX_GAP_S:
            gap = (b.at - a.at) / 3600
            problems.append(("anchor_gap", f"{gap:.1f} h before publication {b.commit[:12]}"))
    return problems
