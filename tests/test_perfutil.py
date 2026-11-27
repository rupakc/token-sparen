"""The best-of-rounds p95 helper behind the perf gates."""

from __future__ import annotations

from perfutil import best_p95, p95


def test_p95_of_a_uniform_sample() -> None:
    assert 94.0 <= p95([float(i) for i in range(1, 101)]) <= 96.0


def test_best_p95_stops_at_the_first_round_under_budget() -> None:
    seen: list[int] = []

    def round_(k: int) -> list[float]:
        seen.append(k)
        return [100.0] * 50 if k == 0 else [10.0] * 50

    assert best_p95(round_, 50.0) == 10.0
    assert seen == [0, 1]


def test_best_p95_reports_the_lowest_when_every_round_is_over() -> None:
    seen: list[int] = []

    def round_(k: int) -> list[float]:
        seen.append(k)
        return [[90.0], [70.0], [80.0]][k] * 50

    assert best_p95(round_, 50.0) == 70.0
    assert seen == [0, 1, 2]
