"""Pruning enabled with a max-size ``prune.json`` (5000 known / used names, 5000 tool rows,
owned rules): PreToolUse, PostToolUseFailure and SessionStart stay under the hook p95 budget."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from perfutil import best_p95, perf_factor
from userconf import to_user_config

from token_sparen.adapters.claude import entry, settings
from token_sparen.mechanisms import prune
from token_sparen.store import keycache

pytestmark = pytest.mark.perf
CALLS = 60
P95_BUDGET_MS = 50.0


def _store(home: Path, proj: Path, env: dict[str, str]) -> None:
    key = keycache.cached_project_key(proj, env)
    names = [f"mcp__server{i:04d}__tool" for i in range(prune.MAX_NAMES)]
    owned = [f"mcp__pruned{i:02d}" for i in range(40)]
    fkey = prune.file_key(settings.local_settings_path(proj))
    doc: dict[str, Any] = {
        "version": prune.VERSION,
        "enabled": True,
        "seeded": True,
        "pending": True,
        "known": names,
        "used": [n.replace("server", "used") for n in names],
        "always": [],
        "kept": {f"mcp__kept{i}": "attempted use" for i in range(200)},
        "tools": {
            f"mcp__server{i:04d}": {"s": 9, "p": 900, "r": [1.0, 900.0, 900]}
            for i in range(prune.MAX_NAMES)
        },
        "files": {fkey: {"dir": str(proj), "owned": owned, "created": {}, "lease": {}}},
        "applied": [f"f{i}" for i in range(prune.APPLIED_MAX)],
    }
    path = prune.store_path(home, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    local = settings.local_settings_path(proj)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps({"permissions": {"deny": owned}}), encoding="utf-8")


def _p95(event: str, payload: dict[str, Any], env: dict[str, str], budget: float) -> float:
    """Best p95 of up to ``perfutil.P95_ROUNDS`` rounds (see ``perfutil.best_p95``)."""

    def round_(k: int) -> list[float]:
        times: list[float] = []
        for i in range(CALLS):
            body = {"session_id": f"perf-{k}-{i}", "hook_event_name": event, **payload}
            out = io.StringIO()
            t0 = time.perf_counter()
            assert entry.main(event, io.StringIO(json.dumps(body)), out, env) == 0
            times.append((time.perf_counter() - t0) * 1000)
        return times

    return best_p95(round_, budget)


def test_prune_hooks_under_budget_with_a_full_store(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    proj.mkdir()
    env = {
        "TOKEN_SPAREN_HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(proj),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
        "TOKEN_SPAREN_TEST_SEED": "1",
    }
    env = to_user_config(env)  # sample rate 0 widens: user config (env only narrows)
    _store(home, proj, env)
    budget = P95_BUDGET_MS * perf_factor()
    pre = _p95("PreToolUse", {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, env, budget)
    fail = _p95(
        "PostToolUseFailure",
        {"tool_name": "Bash", "tool_input": {"command": "make"}, "error": "Exit code 2"},
        env,
        budget,
    )
    start = _p95("SessionStart", {"source": "startup"}, env, budget)
    assert pre < budget, f"PreToolUse p95 {pre:.1f} ms"
    assert fail < budget, f"PostToolUseFailure p95 {fail:.1f} ms"
    assert start < budget, f"SessionStart p95 {start:.1f} ms"
    # the store was read, not rewritten, and no rule was written by a hook
    deny = json.loads(settings.local_settings_path(proj).read_text(encoding="utf-8"))
    assert len(deny["permissions"]["deny"]) == 40
