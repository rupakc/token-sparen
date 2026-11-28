"""Latency gate for ``[learn] savings_reward = "counterfactual"``: it compresses the same output
with every trimming arm of the point to find the largest removal. The default gate is relative,
robust to runner speed: in the same process, on the same 50 KB pytest output (learning engine
deciding), the counterfactual hook p95 may be at most ``MAX_RATIO`` x the default-reward hook p95
(best of ``perfutil.P95_ROUNDS`` interleaved rounds for each). The absolute 50 ms budget is checked
only with ``TOKEN_SPAREN_PERF_STRICT=1``: enabling counterfactual in a tuning campaign requires that
strict check to pass on the target machine."""

from __future__ import annotations

import gc
import io
import json
import os
import time
from pathlib import Path

import pytest
from perfutil import P95_ROUNDS, p95, perf_factor
from sampleoutputs import big_pytest

from token_sparen.adapters.claude import entry

CALLS = 200
P95_BUDGET_MS = 50.0
MAX_RATIO = 1.6
# Read at import: the suite's autouse fixture deletes TOKEN_SPAREN_* variables before each test.
STRICT = os.environ.get("TOKEN_SPAREN_PERF_STRICT") == "1"


@pytest.mark.perf
def test_counterfactual_reward_hook_p95(tmp_path: Path) -> None:
    text = big_pytest(50_000)

    def round_(k: int, counterfactual: bool) -> list[float]:
        root = tmp_path / f"r{k}{'c' if counterfactual else 'd'}"
        proj = root / "proj"
        proj.mkdir(parents=True)
        env = {
            "TOKEN_SPAREN_HOME": str(root / "home"),
            "CLAUDE_PROJECT_DIR": str(proj),
            "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
            "TOKEN_SPAREN_TEST_SEED": "1",
        }
        if counterfactual:
            env["TOKEN_SPAREN_LEARN__SAVINGS_REWARD"] = "counterfactual"
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
        assert len(times) >= CALLS * 0.7
        return times

    best_d = best_c = float("inf")
    for k in range(P95_ROUNDS):
        gc.collect()
        best_d = min(best_d, p95(round_(k, False)))
        gc.collect()
        best_c = min(best_c, p95(round_(k, True)))
        if best_c <= MAX_RATIO * best_d:
            break
    assert best_c <= MAX_RATIO * best_d, (
        f"counterfactual p95 {best_c:.1f} ms vs default {best_d:.1f} ms "
        f"(ratio {best_c / best_d:.2f} > {MAX_RATIO})"
    )
    print(f"counterfactual/default p95 ratio {best_c / best_d:.2f} ({best_c:.1f}/{best_d:.1f} ms)")
    if STRICT:
        budget = P95_BUDGET_MS * perf_factor()
        assert best_c < budget, f"strict: counterfactual p95 {best_c:.1f} ms >= {budget:.1f} ms"
