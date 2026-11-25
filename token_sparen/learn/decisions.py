"""Append-only decision log (``projects/<key>/decisions.jsonl``) shared by all mechanisms."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from token_sparen.config import Config, log_error, state_home
from token_sparen.learn.policy import DecisionRecord
from token_sparen.store import jsonl, jsonstore, paths

DEFAULT_MAX_BYTES = 20_000_000


def append(
    project_key: str, rec: DecisionRecord, cfg: Config, env: Mapping[str, str], where: str
) -> None:
    """Log one decision; a lost line is logged as an error and never raised."""
    raw = cfg.get("learn.decision_log_max_bytes", DEFAULT_MAX_BYTES)
    limit = raw if isinstance(raw, int) and not isinstance(raw, bool) else DEFAULT_MAX_BYTES
    try:
        path = paths.project_dir_for(state_home(env), project_key) / "decisions.jsonl"
        jsonl.append(path, asdict(rec), limit, env)
    except (jsonstore.StoreBusy, OSError, ValueError) as exc:
        log_error(env, where, f"decision log: {type(exc).__name__}: {exc}")
