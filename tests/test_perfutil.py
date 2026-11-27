"""The best-of-rounds p95 helper behind the perf gates."""

from __future__ import annotations

import pytest
from perfutil import best_p95, best_within, p95


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


def test_best_within_stops_at_the_first_round_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import perfutil

    times = iter([0.9, 0.8, 0.01, 0.5, 0.0, 0.0])
    clock = [0.0]
    calls: list[int] = []

    def fn() -> int:
        calls.append(1)
        clock[0] += next(times)
        return len(calls)

    monkeypatch.setattr(perfutil.time, "perf_counter", lambda: clock[0])
    best, res = best_within(fn, 0.1, 2, rounds=3)
    assert (best, res) == (pytest.approx(0.01), 4)  # round 1 over budget, round 2 under: no round 3


def test_best_within_reports_the_fastest_when_every_round_is_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import perfutil

    times = iter([0.9, 0.5, 0.7])
    clock = [0.0]

    def fn() -> None:
        clock[0] += next(times)

    monkeypatch.setattr(perfutil.time, "perf_counter", lambda: clock[0])
    assert best_within(fn, 0.1, 1, rounds=3)[0] == pytest.approx(0.5)


def test_perf_factor_survives_the_env_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest deletes inherited TOKEN_SPAREN_* variables; the factor read at import stays."""
    import perfutil

    monkeypatch.delenv("TOKEN_SPAREN_PERF_FACTOR", raising=False)
    monkeypatch.setattr(perfutil, "_FACTOR_ENV", "1.5")
    assert perfutil.perf_factor() == 1.5
    monkeypatch.setattr(perfutil, "_FACTOR_ENV", None)
    assert perfutil.perf_factor() == 1.0
    monkeypatch.setenv("TOKEN_SPAREN_PERF_FACTOR", "2")
    assert perfutil.perf_factor() == 2.0
