"""Cost of the launcher's re-exec onto a newer Python: the real ``hooks/run.py`` in a subprocess,
end to end (PostToolUse on 50 KB pytest output), direct versus re-exec (forced by the test seed
``TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC=1`` onto this same interpreter, found on a temporary PATH,
the cached search warm). Calls are interleaved in pairs; the measured cost is the per-pair extra
time, p95 best of ``perfutil.P95_ROUNDS`` rounds, budget 60 ms x TOKEN_SPAREN_PERF_FACTOR.

This cost is one extra interpreter start, paid only where the ``python3`` that Claude Code
starts is older than 3.11. Before the launcher searched, those users had no working hooks at
all; with Python >= 3.11 first on PATH as ``python3`` nothing changes (the fast path imports
nothing extra)."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from perfutil import best_p95, perf_factor
from sampleoutputs import big_pytest
from userconf import to_user_config

ROOT = Path(__file__).resolve().parents[2]
CALLS = 30
WARMUP = 3
OVERHEAD_BUDGET_MS = 60.0


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
    return to_user_config(env)  # widening settings go to the temporary user config.toml


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
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3.15").symlink_to(sys.executable)  # the first name the search tries
    plain = _env(tmp_path)
    forced = _env(tmp_path, TOKEN_SPAREN_LAUNCHER_FORCE_REEXEC="1", PATH=str(bindir))
    text = big_pytest(50_000)
    stats: list[tuple[float, float]] = []

    def round_(k: int) -> list[float]:
        extra: list[float] = []
        direct: list[float] = []
        for i in range(CALLS + WARMUP):  # interleaved, so machine load drifts hit both alike
            d = _call(tmp_path, plain, 1000 * k + 2 * i, text)
            r = _call(tmp_path, forced, 1000 * k + 2 * i + 1, text)
            if i >= WARMUP:  # page cache warm, launcher.json written
                extra.append(r - d)
                direct.append(d)
        stats.append((statistics.median(direct), statistics.median(extra)))
        return extra

    budget = OVERHEAD_BUDGET_MS * perf_factor()
    p95 = best_p95(round_, budget)
    assert (tmp_path / "home" / "launcher.json").is_file()  # the search was cached
    direct_med, extra_med = stats[-1]
    print(f"direct median {direct_med:.1f} ms, extra median {extra_med:.1f} ms, p95 {p95:.1f} ms")
    assert p95 < budget, f"re-exec extra p95 {p95:.1f} ms (budget {budget:.0f} ms)"
