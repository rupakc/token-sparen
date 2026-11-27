"""Latency gate: p95 of the in-process PostToolUse hook on 50 KB pytest output, with the
learning engine deciding (decide + pending write, savings reward, regret tracking).

The p95 of one full round (200 calls, fresh state) must be under the budget; up to
``perfutil.P95_ROUNDS`` rounds absorb shared-runner stalls (see ``perfutil.best_p95``)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from perfutil import best_p95, perf_factor
from sampleoutputs import big_pytest

from token_sparen.adapters.claude import entry

CALLS = 200
P95_BUDGET_MS = 50.0


@pytest.mark.perf
def test_hook_latency_p95(tmp_path: Path) -> None:
    text = big_pytest(50_000)

    def round_(k: int) -> list[float]:
        root = tmp_path / f"r{k}"
        proj = root / "proj"
        proj.mkdir(parents=True)
        env = {
            "TOKEN_SPAREN_HOME": str(root / "home"),
            "CLAUDE_PROJECT_DIR": str(proj),
            "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
            "TOKEN_SPAREN_TEST_SEED": "1",
        }
        times: list[float] = []
        for i in range(CALLS):
            payload = {
                "session_id": "perf",
                "cwd": str(proj),
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": f"pytest -v # {i}"},
                "tool_use_id": f"tu-{i}",
                "tool_response": {
                    "stdout": text,
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
            }
            stdin = io.StringIO(json.dumps(payload))
            stdout = io.StringIO()
            t0 = time.perf_counter()
            assert entry.main("PostToolUse", stdin, stdout, env) == 0
            ms = (time.perf_counter() - t0) * 1000
            if "updatedToolOutput" in stdout.getvalue():
                times.append(ms)  # the measured path is the compressing one
        assert len(times) >= CALLS * 0.7  # the engine chose a trimming arm for most calls
        logs = list((root / "home" / "projects").glob("*/decisions.jsonl"))
        recs = [json.loads(x) for x in logs[0].read_text(encoding="utf-8").splitlines()]
        assert len(recs) == CALLS and all(r["bucket"] not in {"static", "error"} for r in recs)
        return times

    budget = P95_BUDGET_MS * perf_factor()
    p95 = best_p95(round_, budget)
    assert p95 < budget, f"p95 {p95:.1f} ms"
