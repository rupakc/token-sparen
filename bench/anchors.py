"""External anchors of the confirmation evidence (M5 Task 10, final tamper-evidence ruling).

The confirmation ledger and its grades are hash-chained (`bench.ledger.record_sha256`), but a
chain only proves that what remains is a consistent prefix: truncating both files and
re-running would leave valid chains. So during confirmation the scheduler appends the current
heads of both chains, with their record counts, to ``anchors.jsonl`` next to the ledger after
every graded task (`record_anchor`; the anchors file is itself append-only and hash-chained).
Task 12's CLI publishes `anchors_to_publish` (commit and push to a ``bench-anchors`` branch of
the public repository) at least once per five-hour window, and the release gate verifies that
every published head lies on the final chains (`verify_anchors`: same record at the same
count, i.e. a prefix) and refuses otherwise.

Residual (disclosed in e4.json as ``anchor_window_operator_trust``): records written after the
last published anchor could still be rewritten by the operator before the next publication.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
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
