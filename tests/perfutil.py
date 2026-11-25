"""Shared helpers for wall-clock budget tests."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def perf_factor(default: float = 1.0) -> float:
    """Budget multiplier for slower runners (env TOKEN_SPAREN_PERF_FACTOR)."""
    return float(os.environ.get("TOKEN_SPAREN_PERF_FACTOR", str(default)))


def best_of(fn: Callable[[], T], n: int) -> tuple[float, T]:
    """Fastest of `n` runs in seconds, with the last result."""
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        res = fn()
        best = min(best, time.perf_counter() - start)
    return best, res
