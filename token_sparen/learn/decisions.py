"""Append-only decision log (``projects/<key>/decisions.jsonl``) shared by all mechanisms.

Two kinds of line: a decision (``DecisionRecord``) and, once its regret is settled, an outcome
``{"kind": "outcome", "decision_id", "regret", "signal", "ts"}`` (``outcome``). Off-policy
evaluation joins outcomes to decisions by id (``learn.ope.load_log``); readers that want only
decisions skip lines with a ``kind``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from token_sparen.config import Config, log_error, state_home
from token_sparen.learn.policy import DecisionRecord
from token_sparen.store import jsonl, jsonstore, paths

DEFAULT_MAX_BYTES = 20_000_000


FILE = "decisions.jsonl"
OUTCOME = "outcome"


def _limit(cfg: Config) -> int:
    raw = cfg.get("learn.decision_log_max_bytes", DEFAULT_MAX_BYTES)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else DEFAULT_MAX_BYTES


def append(
    project_key: str, rec: DecisionRecord, cfg: Config, env: Mapping[str, str], where: str
) -> None:
    """Log one decision; a lost line is logged as an error and never raised."""
    try:
        path = paths.project_dir_for(state_home(env), project_key) / FILE
        jsonl.append(path, asdict(rec), _limit(cfg), env)
    except (jsonstore.StoreBusy, OSError, ValueError) as exc:
        log_error(env, where, f"decision log: {type(exc).__name__}: {exc}")


def outcome(
    decision_id: str, regret: float, signal: str, ts: float | None = None
) -> dict[str, Any]:
    """One outcome line: the settled regret of ``decision_id`` and the signal that set it
    (``recovery_read``, ``rerun``, ``elided_ask``, ``failure_loop``; 0 at ``expiry``,
    ``session_end``, ``evicted`` or a trimming ``no_op``)."""
    return {
        "kind": OUTCOME,
        "decision_id": decision_id,
        "regret": min(1.0, max(0.0, float(regret))),
        "signal": signal,
        "ts": time.time() if ts is None else ts,
    }


def append_outcomes(
    project_dir: Path,
    records: list[dict[str, Any]],
    cfg: Config,
    env: Mapping[str, str],
    where: str,
) -> None:
    """Log settled outcomes to ``project_dir``'s decision log (one lock); never raises."""
    if not records:
        return
    try:
        jsonl.append_many(project_dir / FILE, records, _limit(cfg), env)
    except (jsonstore.StoreBusy, OSError, ValueError) as exc:
        log_error(env, where, f"decision log: {type(exc).__name__}: {exc}")
