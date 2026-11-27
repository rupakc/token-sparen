"""Lock contention on one session overlay: tracking writes it on every PostToolUse.

The p95 of one full round (PROCS lined-up updates on a fresh file) must be under the budget; up
to ``perfutil.P95_ROUNDS`` rounds absorb shared-runner stalls (see ``perfutil.best_p95``). With
only PROCS samples a round's p95 is its slowest few waits, so one stalled worker decided it."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

import pytest
from perfutil import best_p95, perf_factor

from token_sparen.store import session

pytestmark = pytest.mark.perf
PROCS = 10
P95_BUDGET_S = 0.020


def _one(args: tuple[str, float]) -> float:
    path, start_at = args
    while time.time() < start_at:  # line the workers up so they contend
        pass
    t0 = time.perf_counter()

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        state["n"] = int(state.get("n", 0)) + 1
        return state

    session.update_session(Path(path), apply)
    return time.perf_counter() - t0


def test_parallel_overlay_updates_p95(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    with ctx.Pool(PROCS) as pool:

        def round_(k: int) -> list[float]:
            path = tmp_path / f"r{k}" / "sessions" / "s.json"
            path.parent.mkdir(parents=True)
            pool.map(_one, [(str(path), 0.0)] * PROCS)  # warm the workers (imports)
            start_at = time.time() + 0.3
            waits = pool.map(_one, [(str(path), start_at)] * PROCS)
            assert session.read_session(path)["n"] == 2 * PROCS
            return waits

        budget = P95_BUDGET_S * perf_factor()
        p95 = best_p95(round_, budget)
    assert p95 < budget, f"p95 {p95 * 1e3:.1f} ms"
