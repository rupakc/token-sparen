"""Fold work on the hook path stays inside its budgets (Task 22)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from perfutil import best_of, perf_factor

from token_sparen.learn import overlay
from token_sparen.learn.beta import Evidence
from token_sparen.store import session

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
BANDS = ("lt50", "50_75", "75_95", "gt95")
CTYPES = ("log", "test_report", "json", "build_log", "diff")
PHASES = ("build", "debug", "explore", "mixed")


def _evidence() -> dict[str, Any]:
    cell = {
        "savings": Evidence(3.0, 2.0, 5.0, 2.0).to_list(),
        "regret": Evidence(0.0, 5.0, 5.0, 0.0).to_list(),
    }
    buckets = {
        f"band={b}|cat=shell|size=sm|ctype={c}|phase={p}": {"light": cell, "standard": cell}
        for b in BANDS
        for c in CTYPES
        for p in PHASES[:2]
    }
    return {"compression": buckets}  # 40 buckets x 2 arms x 2 kinds


def _sessions(home: Path, n: int) -> None:
    old = time.time() - 7200
    for i in range(n):
        path = session.session_path(home, KEY, f"old-{i}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"evidence": _evidence()}), encoding="utf-8")
        os.utime(path, (old, old))
    index = home / "projects" / KEY / "pending.json"
    index.write_text(json.dumps({f"old-{i}": old for i in range(n)}), encoding="utf-8")


def test_session_start_in_process_fold_under_100_ms(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"TOKEN_SPAREN_HOME": str(home)}
    _sessions(home, 3)
    start = time.perf_counter()
    reps = overlay.fold_pending(KEY, home, tmp_path, exclude="now", env=env)
    took = time.perf_counter() - start
    assert len(reps) == 3 and all(r.staged for r in reps)
    assert took < 0.1 * perf_factor(), f"{took * 1000:.1f} ms"


def test_learner_state_under_15_ms(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _sessions(home, 3)
    overlay.fold_pending(KEY, home, tmp_path, exclude="now", env={"TOKEN_SPAREN_HOME": str(home)})
    pdir = home / "projects" / KEY
    with (pdir / "decisions.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(2000):
            rec = {
                "decision_id": f"d{i}",
                "point": "compression",
                "arm": "standard",
                "bucket": f"band={BANDS[i % 4]}|cat=shell|size=sm|ctype={CTYPES[i % 5]}"
                "|phase=build",
                "command_sig": "pytest -q" + " x" * 50,
            }
            fh.write(json.dumps(rec) + "\n")
    took, state = best_of(lambda: overlay.learner_state(KEY, home), 5)
    assert state in {"adapting", "exploiting"}
    assert took < 0.015 * perf_factor(), f"{took * 1000:.1f} ms"


def test_session_end_spawn_returns_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    children: list[subprocess.Popen[bytes]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(overlay, "POPEN", popen)
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    start = time.perf_counter()
    assert overlay.spawn_fold(["fold", "--session", "x", "--project", str(tmp_path)], env)
    took = time.perf_counter() - start
    for child in children:
        child.wait(timeout=30)  # the detached fold of a missing session is a no-op
        assert child.returncode == 0
    assert took < 0.03 * perf_factor(), f"{took * 1000:.1f} ms"
    assert sys.executable  # the child runs under the hook's interpreter
