"""Shared helpers for wall-clock budget tests."""

from __future__ import annotations

import gc
import os
import statistics
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

P95_ROUNDS = 3

# Read once, when conftest imports this module: the suite-wide autouse fixture deletes every
# inherited ``TOKEN_SPAREN_*`` variable before each test, so a read inside a test always saw the
# default (CI's perf job sets 1.5 and the strict budgets reported "factor 1.0").
_FACTOR_ENV = os.environ.get("TOKEN_SPAREN_PERF_FACTOR")


def perf_factor(default: float = 1.0) -> float:
    """Budget multiplier for slower runners (env TOKEN_SPAREN_PERF_FACTOR, as set when the
    session started; a test may still set it explicitly)."""
    raw = os.environ.get("TOKEN_SPAREN_PERF_FACTOR", _FACTOR_ENV)
    return float(raw) if raw is not None else default


def best_of(fn: Callable[[], T], n: int) -> tuple[float, T]:
    """Fastest of `n` runs in seconds, with the last result."""
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        res = fn()
        best = min(best, time.perf_counter() - start)
    return best, res


def best_within(
    fn: Callable[[], T], budget: float, n: int, rounds: int = P95_ROUNDS
) -> tuple[float, T]:
    """Fastest single run over up to `rounds` rounds of `n` runs, stopping at the first round
    whose fastest run is under `budget`; returns (seconds, last result).

    The budget is unchanged: one run of the code must finish under it. A round of `n` runs is
    short enough to land entirely inside a runner stall (a docs-only commit measured 202 ms
    against 200 ms); a real regression slows every round and still fails.
    """
    gc.collect()
    best, res = best_of(fn, n)
    for _ in range(rounds - 1):
        if best < budget:
            break
        gc.collect()  # garbage of the previous round is not this round's time
        t, res = best_of(fn, n)
        best = min(best, t)
    return best, res


def p95(times: list[float]) -> float:
    """95th percentile of a latency sample (same estimator in every perf test)."""
    return statistics.quantiles(times, n=100)[94]


def best_p95(
    round_: Callable[[int], list[float]], budget: float, rounds: int = P95_ROUNDS
) -> float:
    """Lowest p95 over up to `rounds` independent rounds (``round_(k)`` returns one round's
    latencies, from fresh state), stopping at the first round under `budget`.

    The budget itself is unchanged: one full round of the code must have a p95 under it. Shared
    CI runners stall in bursts (a noisy neighbour, a hypervisor steal), which only ever adds
    latency to a handful of samples and so lands exactly on the p95 tail; a docs-only commit
    failed at 90 ms against a 19 ms local p95. A real regression slows every round and still
    fails; runner noise rarely hits all of them.
    """
    best = float("inf")
    for k in range(rounds):
        gc.collect()  # garbage of the previous round is not this round's latency
        best = min(best, p95(round_(k)))
        if best < budget:
            break
    return best
