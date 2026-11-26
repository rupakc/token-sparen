"""Latency gate: p95 of the in-process PostToolUse hook on 50 KB pytest output."""

from __future__ import annotations

import io
import json
import statistics
import time
from pathlib import Path

import pytest
from perfutil import perf_factor
from sampleoutputs import big_pytest

from token_sparen.adapters.claude import entry

CALLS = 200
P95_BUDGET_MS = 50.0


@pytest.mark.perf
def test_hook_latency_p95(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(proj),
        "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
    }
    text = big_pytest(50_000)
    times: list[float] = []
    replaced = 0
    for i in range(CALLS):
        payload = {
            "session_id": "perf",
            "cwd": str(proj),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"pytest -v # {i}"},
            "tool_use_id": f"tu-{i}",
            "tool_response": {"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
        }
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        t0 = time.perf_counter()
        assert entry.main("PostToolUse", stdin, stdout, env) == 0
        times.append((time.perf_counter() - t0) * 1000)
        replaced += "updatedToolOutput" in stdout.getvalue()
    assert replaced == CALLS  # the measured path is the compressing one
    p95 = statistics.quantiles(times, n=100)[94]
    assert p95 < P95_BUDGET_MS * perf_factor(), f"p95 {p95:.1f} ms"
