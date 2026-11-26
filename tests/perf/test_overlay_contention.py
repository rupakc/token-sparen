"""Lock contention on one session overlay: tracking writes it on every PostToolUse."""

from __future__ import annotations

import multiprocessing as mp
import statistics
import time
from pathlib import Path
from typing import Any

import pytest
from perfutil import perf_factor

from token_sparen.store import session

pytestmark = pytest.mark.perf
PROCS = 10


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
    path = tmp_path / "sessions" / "s.json"
    path.parent.mkdir(parents=True)
    ctx = mp.get_context("spawn")
    with ctx.Pool(PROCS) as pool:
        pool.map(_one, [(str(path), 0.0)] * PROCS)  # warm the workers (imports)
        start_at = time.time() + 0.3
        waits = pool.map(_one, [(str(path), start_at)] * PROCS)
    assert session.read_session(path)["n"] == 2 * PROCS
    p95 = statistics.quantiles(waits, n=20)[-1]
    assert p95 < 0.020 * perf_factor()
