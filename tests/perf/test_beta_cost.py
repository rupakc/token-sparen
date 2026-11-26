"""Cost of exact Beta quantiles (used on the decision path)."""

from __future__ import annotations

import pytest
from perfutil import best_of, perf_factor

from token_sparen.learn.beta import Beta

pytestmark = pytest.mark.perf


def test_thousand_quantiles_under_budget() -> None:
    posts = [Beta(1.0 + i % 40, 1.0 + (i * 7) % 40) for i in range(1000)]

    def once() -> object:
        return [p.lcb() + p.ucb() for p in posts[:500]]

    best, _ = best_of(once, 5)
    # The engine needs only a handful of quantiles per hook: budget 100 us/call.
    assert best < 0.100 * perf_factor()
