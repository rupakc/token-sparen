"""Quota decisions (M5 Task 7): weekly stop, five-hour wait, per-window run cap, fail safe."""

from __future__ import annotations

import pytest

from bench import quota as qt
from bench.streamjson import RateLimit

NOW = 1_000_000.0
H = 3600.0
POLICY = qt.QuotaPolicy()


def _r(
    kind: str,
    pct: float | None,
    *,
    at: float = NOW - 60,
    resets: float | None = None,
    status: str = "allowed",
    source: str = "owner",
) -> qt.QuotaReading:
    return qt.QuotaReading(at, source, kind, pct, resets, status)  # type: ignore[arg-type]


def _ok() -> list[qt.QuotaReading]:
    return [_r("weekly", 10.0, resets=NOW + 72 * H), _r("five_hour", 10.0, resets=NOW + 2 * H)]


def test_go_when_below_every_limit() -> None:
    assert qt.decide(_ok(), POLICY, NOW, 0) == qt.QuotaDecision(True, "ok", None)


def test_weekly_60_stops_never_sleeps() -> None:
    readings = [*_ok(), _r("weekly", 60.0, resets=NOW + 1 * H)]
    d = qt.decide(readings, POLICY, NOW, 0)
    assert d == qt.QuotaDecision(False, "weekly", None)  # no wait_until: never sleep a reset
    rejected = [*_ok(), _r("weekly", 5.0, resets=NOW + H, status="rejected", source="stream")]
    assert qt.decide(rejected, POLICY, NOW, 0).reason == "weekly"


def test_latest_reading_of_a_kind_wins() -> None:
    readings = [_r("weekly", 70.0, at=NOW - 100), *_ok()]
    assert qt.decide(readings, POLICY, NOW, 0).go


def test_unknown_weekly_fails_safe() -> None:
    only_five = [_r("five_hour", 10.0, resets=NOW + H)]
    assert qt.decide(only_five, POLICY, NOW, 0) == qt.QuotaDecision(False, "weekly", None)
    unknown_pct = [_r("weekly", None, resets=NOW + 9 * H), _r("five_hour", 1.0, resets=NOW + H)]
    assert qt.decide(unknown_pct, POLICY, NOW, 0).reason == "weekly"
    stale = [_r("weekly", 1.0, at=NOW - 6 * H, resets=NOW + 9 * H), _ok()[1]]
    assert qt.decide(stale, POLICY, NOW, 0).reason == "weekly"
    past_reset = [_r("weekly", 1.0, resets=NOW - 1), _ok()[1]]
    assert qt.decide(past_reset, POLICY, NOW, 0).reason == "weekly"


def test_rejected_unknown_kind_is_treated_as_weekly() -> None:
    readings = [*_ok(), _r("unknown", None, status="rejected", source="stream")]
    assert qt.decide(readings, POLICY, NOW, 0) == qt.QuotaDecision(False, "weekly", None)


def test_five_hour_waits_until_reset() -> None:
    readings = [_ok()[0], _r("five_hour", 85.0, resets=NOW + H)]
    assert qt.decide(readings, POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", NOW + H)
    rejected = [_ok()[0], _r("five_hour", None, resets=NOW + 2 * H, status="rejected")]
    assert qt.decide(rejected, POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", NOW + 2 * H)


def test_five_hour_after_reset_counts_as_fresh_window() -> None:
    readings = [_ok()[0], _r("five_hour", 99.0, at=NOW - 3 * H, resets=NOW - 1)]
    assert qt.decide(readings, POLICY, NOW, 0).go


def test_five_hour_unknown_pauses_without_wait() -> None:
    assert qt.decide([_ok()[0]], POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", None)
    old = [_ok()[0], _r("five_hour", 1.0, at=NOW - 6 * H)]
    assert qt.decide(old, POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", None)
    uncalibrated = [_ok()[0], _r("five_hour", None, resets=NOW + H, source="stream")]
    assert qt.decide(uncalibrated, POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", None)
    with_known = [_ok()[0], _r("five_hour", 90.0, at=NOW - 99, resets=NOW + H), uncalibrated[1]]
    assert qt.decide(with_known, POLICY, NOW, 0) == qt.QuotaDecision(False, "five_hour", NOW + H)
    assert qt.decide([*_ok(), uncalibrated[1]], POLICY, NOW, 0).go


def test_window_cap() -> None:
    d = qt.decide(_ok(), POLICY, NOW, POLICY.max_runs_per_window)
    assert d == qt.QuotaDecision(False, "window_cap", NOW + 2 * H)
    assert qt.decide(_ok(), POLICY, NOW, POLICY.max_runs_per_window - 1).go
    assert qt.decide(_ok(), qt.QuotaPolicy(max_runs_per_window=9), NOW, 4).go


def test_window_bounds() -> None:
    start, end = qt.window_bounds(_ok(), NOW)
    assert (start, end) == (NOW + 2 * H - 5 * H, NOW + 2 * H)
    assert qt.window_bounds([], NOW) == (NOW - 5 * H, None)


def test_policy_defaults_are_the_owner_rulings() -> None:
    assert (POLICY.weekly_stop_pct, POLICY.five_hour_stop_pct) == (60.0, 85.0)
    assert POLICY.max_runs_per_window == 4


@pytest.mark.parametrize(
    ("unit", "raw", "pct"), [(None, 0.5, None), ("fraction", 0.5, 50.0), ("percent", 42.0, 42.0)]
)
def test_reading_from_stream_units_calibrated(
    unit: qt.Unit | None, raw: float, pct: float | None
) -> None:
    rl = RateLimit("allowed", "five_hour", NOW + H, raw)
    reading = qt.reading_from_rate_limit(rl, at=NOW, unit=unit)
    assert reading.source == "stream" and reading.kind == "five_hour"
    assert reading.utilization_pct == pct and reading.status == "allowed"


def test_reading_record_round_trip() -> None:
    r = _r("weekly", 12.5, resets=NOW + H)
    assert qt.reading_from_record(qt.reading_record(r)) == r
    assert qt.reading_from_record({"kind": "quota", "quota_kind": "weird"}) is None
