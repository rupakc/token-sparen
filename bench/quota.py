"""Quota readings and the go / wait / stop decision before every run (M5 Task 7).

Owner rulings: never start a run when the last weekly reading is >= 60 % or a weekly limit was
rejected (stop, exit 3; never sleep through a weekly reset: the owner decides); a five-hour
reading >= 85 % or a rejected five-hour limit waits until its ``resets_at`` (with ``--wait``,
else exit 4); at most ``max_runs_per_window`` runs start per five-hour window.

Readings come from the stream (``rate_limit_event``s parsed by `bench.streamjson`) and from the
owner (an operator command after reading ``/usage``). The stream's utilization unit is not yet
confirmed (Task 13 calibrates `UTILIZATION_UNIT`); until then stream readings carry no
percentage and only their ``rejected`` status counts. **Fail safe**: an unknown quantity pauses.
No usable weekly reading (none, no percentage, older than ``max_reading_age_s``, or past its
reset) stops like a weekly stop; no usable five-hour reading pauses without a wait time. A
rejected limit of unknown kind is treated as weekly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, cast, get_args

from bench.streamjson import RateLimit

FIVE_HOURS_S = 5 * 3600.0
Source = Literal["stream", "owner"]
Kind = Literal["five_hour", "weekly", "unknown"]
Unit = Literal["fraction", "percent"]
Reason = Literal["ok", "five_hour", "weekly", "window_cap"]
# the stream's `utilization` unit: set by the Task 13 calibration; None = unknown (no %)
UTILIZATION_UNIT: Unit | None = None


@dataclass(frozen=True)
class QuotaReading:
    at: float
    source: Source
    kind: Kind
    utilization_pct: float | None
    resets_at: float | None
    status: str = "allowed"  # the event's status; "rejected" = the limit was hit

    @property
    def rejected(self) -> bool:
        return self.status == "rejected"


@dataclass(frozen=True)
class QuotaPolicy:
    weekly_stop_pct: float = 60.0
    five_hour_stop_pct: float = 85.0
    max_runs_per_window: int = 4
    max_reading_age_s: float = FIVE_HOURS_S  # an older weekly reading is unknown


class QuotaDecision(NamedTuple):
    go: bool
    reason: Reason
    wait_until: float | None


def reading_from_rate_limit(
    rl: RateLimit, *, at: float, unit: Unit | None = UTILIZATION_UNIT
) -> QuotaReading:
    pct: float | None = None
    if rl.utilization is not None and unit is not None:
        pct = rl.utilization * 100.0 if unit == "fraction" else rl.utilization
    return QuotaReading(at, "stream", rl.kind, pct, rl.resets_at, rl.status)


def reading_record(r: QuotaReading) -> dict[str, Any]:
    """The ledger ``quota`` record of a reading."""
    return {
        "kind": "quota",
        "at": r.at,
        "source": r.source,
        "quota_kind": r.kind,
        "utilization_pct": r.utilization_pct,
        "resets_at": r.resets_at,
        "status": r.status,
    }


def reading_from_record(rec: Mapping[str, Any]) -> QuotaReading | None:
    kind, source = rec.get("quota_kind"), rec.get("source")
    if not (isinstance(kind, str) and isinstance(source, str)):
        return None
    if kind not in get_args(Kind) or source not in get_args(Source):
        return None
    pct, resets = rec.get("utilization_pct"), rec.get("resets_at")
    return QuotaReading(
        at=float(rec.get("at", 0.0)),
        source=cast(Source, source),
        kind=cast(Kind, kind),
        utilization_pct=None if pct is None else float(pct),
        resets_at=None if resets is None else float(resets),
        status=str(rec.get("status", "allowed")),
    )


def _latest(readings: Iterable[QuotaReading], kind: str) -> QuotaReading | None:
    pick: QuotaReading | None = None
    for r in readings:
        if r.kind == kind and (pick is None or r.at >= pick.at):
            pick = r
    return pick


def window_bounds(readings: Sequence[QuotaReading], now: float) -> tuple[float, float | None]:
    """(start, end) of the current five-hour window: from the latest five-hour reading whose
    reset is still ahead, else a rolling window of the last five hours with an unknown end."""
    five = _latest((r for r in readings if r.resets_at is not None), "five_hour")
    if five is not None and five.resets_at is not None and five.resets_at > now:
        return five.resets_at - FIVE_HOURS_S, five.resets_at
    return now - FIVE_HOURS_S, None


def decide(
    readings: Sequence[QuotaReading], policy: QuotaPolicy, now: float, runs_this_window: int
) -> QuotaDecision:
    """Go, wait (``wait_until`` set) or stop (weekly: never a wait time) before a run."""
    # a rejected weekly (or unknown-kind) limit stops, whatever its age before its reset
    for r in readings:
        if r.rejected and r.kind in ("weekly", "unknown"):
            if r.resets_at is None or r.resets_at > now:
                return QuotaDecision(False, "weekly", None)
    weekly = _latest((r for r in readings if not r.rejected), "weekly")
    if (
        weekly is None
        or weekly.utilization_pct is None
        or now - weekly.at > policy.max_reading_age_s
        or (weekly.resets_at is not None and weekly.resets_at <= now)
        or weekly.utilization_pct >= policy.weekly_stop_pct
    ):
        return QuotaDecision(False, "weekly", None)
    five = _latest(readings, "five_hour")
    if five is None or (five.resets_at is None and now - five.at > FIVE_HOURS_S):
        return QuotaDecision(False, "five_hour", None)
    reset_passed = five.resets_at is not None and five.resets_at <= now
    if not reset_passed:
        if five.rejected or (
            five.utilization_pct is not None and five.utilization_pct >= policy.five_hour_stop_pct
        ):
            return QuotaDecision(False, "five_hour", five.resets_at)
        if five.utilization_pct is None:
            # an allowed stream event without a calibrated unit: is the previous known one ok?
            known = _latest((r for r in readings if r.utilization_pct is not None), "five_hour")
            if known is None or (known.resets_at is not None and known.resets_at <= now):
                return QuotaDecision(False, "five_hour", None)
            if known.utilization_pct is not None and (
                known.utilization_pct >= policy.five_hour_stop_pct
            ):
                return QuotaDecision(False, "five_hour", known.resets_at)
    if runs_this_window >= policy.max_runs_per_window:
        _, end = window_bounds(readings, now)
        return QuotaDecision(False, "window_cap", end if end is not None else now + FIVE_HOURS_S)
    return QuotaDecision(True, "ok", None)
