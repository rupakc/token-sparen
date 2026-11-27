"""Cost of the launcher's re-exec onto a newer Python: the real ``hooks/run.py`` in a subprocess,
end to end (PostToolUse on 50 KB pytest output), direct versus re-exec (forced by the test seed
``TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC=1`` onto this same interpreter, the cached search warm).
Budget: re-exec p95 <= direct p95 + 40 ms (x TOKEN_SPAREN_PERF_FACTOR)."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from perfutil import perf_factor
from sampleoutputs import big_pytest

ROOT = Path(__file__).resolve().parents[2]
CALLS = 40
WARMUP = 3
OVERHEAD_BUDGET_MS = 40.0


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    env.update(
        TOKEN_SPAREN_HOME=str(tmp_path / "home"),
        CLAUDE_PROJECT_DIR=str(tmp_path / "proj"),
        TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE="0",
        TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="5000",
        TOKEN_SPAREN_TEST_SEED="1",
        TOKEN_SPAREN_NO_UV="1",
    )
    env.update(extra)
    return env


def _call(tmp_path: Path, env: dict[str, str], i: int, text: str) -> float:
    proj = tmp_path / "proj"
    payload = json.dumps(
        {
            "session_id": "perf",
            "cwd": str(proj),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"pytest -v # {i}"},
            "tool_use_id": f"tu-{i}",
            "tool_response": {"stdout": text, "stderr": "", "interrupted": False},
        }
    )
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "hooks" / "run.py"), "PostToolUse"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=proj,
        env=env,
        timeout=60,
        check=False,
    )
    ms = (time.perf_counter() - t0) * 1000
    assert proc.returncode == 0 and proc.stdout.strip(), proc.stderr
    return ms


@pytest.mark.perf
def test_reexec_overhead_p95(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    (tmp_path / "home").mkdir()
    plain = _env(tmp_path)
    forced = _env(
        tmp_path, TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1", TOKEN_SPAREN_PYTHON=sys.executable
    )
    text = big_pytest(50_000)
    direct_ms: list[float] = []
    reexec_ms: list[float] = []
    for i in range(CALLS + WARMUP):  # interleaved, so machine load drifts hit both alike
        d = _call(tmp_path, plain, 2 * i, text)
        r = _call(tmp_path, forced, 2 * i + 1, text)
        if i >= WARMUP:  # page cache warm, launcher.json written
            direct_ms.append(d)
            reexec_ms.append(r)
    direct = statistics.quantiles(direct_ms, n=100)[94]
    reexec = statistics.quantiles(reexec_ms, n=100)[94]
    assert (tmp_path / "home" / "launcher.json").is_file()  # the search was cached
    overhead = reexec - direct
    print(f"direct p95 {direct:.1f} ms, re-exec p95 {reexec:.1f} ms, overhead {overhead:.1f} ms")
    assert reexec <= direct + OVERHEAD_BUDGET_MS * perf_factor(), (
        f"re-exec p95 {reexec:.1f} ms vs direct {direct:.1f} ms"
    )
