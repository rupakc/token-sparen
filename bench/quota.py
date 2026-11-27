"""Account-wide quota state and the go / wait / stop decision before every run (M5 Task 7).

Owner rulings: never start a run when the weekly utilization is >= 60 % or could cross it with
the next run (projection: last weekly % + (runs since that reading + 1) x the max observed
per-run weekly use, never below `QuotaPolicy.run_weekly_pct` = 2.5 % until Task 13 has
calibrated `UTILIZATION_UNIT`; rulings N1/N2), or a weekly
limit was rejected (stop, exit 3; never sleep through a weekly reset: the owner decides); a
five-hour reading >= 85 % or a rejected five-hour limit waits until its ``resets_at`` (with
``--wait``, else exit 4); at most ``max_runs_per_window`` runs start per five-hour window.

**Account scope** (fix round 1, ruling I1): the window's run count, the owner's readings and
the stream's readings live in one journal under a fixed bench state directory
(`default_account_dir`: ``$TOKEN_SPAREN_BENCH_STATE`` or ``~/.token-sparen-bench``), shared by
every ledger, stage, campaign and workspace; `Account.hold` is the exclusive account lock a
scheduler holds for its whole life (a second scheduler refuses to start).

**Readings**: the owner's (an operator command after reading ``/usage``) are authoritative.
Stream ``rate_limit_event``s carry a percentage only once Task 13 has calibrated
`UTILIZATION_UNIT` (read at call time, so setting the module attribute takes effect); until
then they are logged and never stop a run by themselves (ruling I2). A *rejected* stream event
is a fact: a rejected weekly limit stops (until its reset, or, without one, until a newer owner
reading or `QuotaPolicy.rate_backoff_s`); a rejected five-hour limit waits until its reset (or
the backoff); a rejection of unknown kind without a reset blocks until a newer owner reading or
the backoff (ruling I3: never forever; a fresh owner reading always supersedes).

**Fail safe**: no usable weekly percentage (none, older than ``max_reading_age_s``, past its
reset) stops like a weekly stop; no usable five-hour percentage pauses without a wait time.
Readings dated in the future (> 5 min) or resetting before they were taken are ignored.
Residual overshoot: the projection uses observed per-run use; a run larger than any seen
before can still pass 60 % by that difference (disclosed in the method).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast, get_args

from bench.ledger import _write_line, read_jsonl
from bench.streamjson import RateLimit
from token_sparen.store.jsonstore import StoreBusy, locked

FIVE_HOURS_S = 5 * 3600.0
FUTURE_SKEW_S = 300.0
ACCOUNT_ENV = "TOKEN_SPAREN_BENCH_STATE"
ACCOUNT_DIRNAME = ".token-sparen-bench"
JOURNAL = "quota.jsonl"
Source = Literal["stream", "owner"]
Kind = Literal["five_hour", "weekly", "unknown"]
Unit = Literal["fraction", "percent"]
Reason = Literal["ok", "five_hour", "weekly", "window_cap"]
# the stream's `utilization` unit: set by the Task 13 calibration; None = unknown (no %)
UTILIZATION_UNIT: Unit | None = None
_FROM_MODULE: Literal["module"] = "module"


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
    rate_backoff_s: float = 3600.0  # a rejection without a reset blocks this long at most
    run_weekly_pct: float = 2.5  # per-run weekly floor until calibrated (ruling N1)


class QuotaDecision(NamedTuple):
    go: bool
    reason: Reason
    wait_until: float | None


def reading_from_rate_limit(
    rl: RateLimit, *, at: float, unit: Unit | None | Literal["module"] = _FROM_MODULE
) -> QuotaReading:
    if unit == _FROM_MODULE:
        unit = UTILIZATION_UNIT  # looked up now: a calibration set at runtime applies
    pct: float | None = None
    if rl.utilization is not None and unit is not None:
        pct = rl.utilization * 100.0 if unit == "fraction" else rl.utilization
    return QuotaReading(at, "stream", rl.kind, pct, rl.resets_at, rl.status)


def reading_record(r: QuotaReading) -> dict[str, Any]:
    """The ``quota`` record of a reading (account journal and ledger log)."""
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


# --- the account journal ----------------------------------------------------------------------


class AccountBusy(RuntimeError):
    """Another scheduler holds the account lock."""


def default_account_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(ACCOUNT_ENV)
    return Path(override) if override else Path.home() / ACCOUNT_DIRNAME


class Account:
    """The account-wide quota journal ``<root>/quota.jsonl`` (readings and run starts) and the
    account lock ``<root>/account.lock``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / JOURNAL

    def _append(self, rec: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
        with locked(self.path, timeout=30.0):
            _write_line(self.path, rec)

    def records(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def readings(self) -> list[QuotaReading]:
        out = (reading_from_record(r) for r in self.records() if r.get("kind") == "quota")
        return [r for r in out if r is not None]

    def starts(self) -> list[float]:
        return [float(r["at"]) for r in self.records() if r.get("kind") == "start"]

    def record_start(self, key_id: str, *, at: float) -> None:
        self._append({"kind": "start", "at": at, "key": key_id})

    def record_reading(self, r: QuotaReading, *, now: float) -> None:
        """Validated: not dated in the future, a reset after the reading, 0-100 %."""
        if not _plausible(r, now):
            raise ValueError("reading dated in the future or resetting before it was taken")
        if r.utilization_pct is not None and not 0.0 <= r.utilization_pct <= 100.0:
            raise ValueError("utilization must be a percentage between 0 and 100")
        self._append(reading_record(r))

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """The exclusive account lock for a scheduler's lifetime (`AccountBusy` if held)."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            cm = locked(self.root / "account", timeout=0)
            cm.__enter__()
        except StoreBusy as exc:
            raise AccountBusy(
                f"another scheduler holds the bench account lock in {self.root}"
            ) from exc
        try:
            yield
        finally:
            cm.__exit__(None, None, None)


# --- the decision -----------------------------------------------------------------------------


def _plausible(r: QuotaReading, now: float) -> bool:
    return r.at <= now + FUTURE_SKEW_S and (r.resets_at is None or r.resets_at >= r.at)


def _latest(readings: Sequence[QuotaReading], kind: str) -> QuotaReading | None:
    pick: QuotaReading | None = None
    for r in readings:
        if r.kind == kind and (pick is None or r.at >= pick.at):
            pick = r
    return pick


def window_bounds(readings: Sequence[QuotaReading], now: float) -> tuple[float, float | None]:
    """(start, end) of the current five-hour window: from the latest five-hour reading whose
    reset is still ahead, else a rolling window of the last five hours with an unknown end."""
    valid = [r for r in readings if _plausible(r, now) and r.resets_at is not None]
    five = _latest(valid, "five_hour")
    if five is not None and five.resets_at is not None and five.resets_at > now:
        return five.resets_at - FIVE_HOURS_S, five.resets_at
    return now - FIVE_HOURS_S, None


def per_run_weekly_pct(
    readings: Sequence[QuotaReading], starts: Sequence[float], policy: QuotaPolicy
) -> float:
    """The per-run weekly % used by the projection. Observed = the max delta per run over
    consecutive known weekly readings of the same weekly window with runs started between them.
    Until calibration (`UTILIZATION_UNIT` unset): max(`QuotaPolicy.run_weekly_pct`, observed),
    so equal readings never make it 0 (ruling N2); after calibration: the max observed, or the
    default while nothing is observed yet."""
    known = sorted(
        (r for r in readings if r.kind == "weekly" and r.utilization_pct is not None),
        key=lambda r: r.at,
    )
    best: float | None = None
    for a, b in zip(known, known[1:], strict=False):
        if a.resets_at is not None and b.at >= a.resets_at:
            continue  # a weekly reset lies between them
        assert a.utilization_pct is not None and b.utilization_pct is not None
        runs = sum(1 for s in starts if a.at < s <= b.at)
        delta = b.utilization_pct - a.utilization_pct
        if runs and delta >= 0:
            best = max(best or 0.0, delta / runs)
    if UTILIZATION_UNIT is None:
        return max(policy.run_weekly_pct, best or 0.0)
    return policy.run_weekly_pct if best is None else best


def _superseded(rej: QuotaReading, readings: Sequence[QuotaReading]) -> bool:
    return any(
        r.source == "owner"
        and r.at > rej.at
        and not r.rejected
        and (rej.kind == "unknown" or r.kind == rej.kind)
        for r in readings
    )


def _rejection(
    readings: Sequence[QuotaReading], policy: QuotaPolicy, now: float
) -> QuotaDecision | None:
    for r in sorted((r for r in readings if r.rejected), key=lambda r: r.at, reverse=True):
        if _superseded(r, readings):
            continue
        until = r.resets_at if r.resets_at is not None else r.at + policy.rate_backoff_s
        if until <= now:
            continue
        if r.kind == "weekly":
            return QuotaDecision(False, "weekly", None)
        return QuotaDecision(False, "five_hour", until)
    return None


def decide(
    readings: Sequence[QuotaReading],
    policy: QuotaPolicy,
    now: float,
    runs_this_window: int,
    *,
    starts: Sequence[float] = (),
) -> QuotaDecision:
    """Go, wait (``wait_until`` set) or stop (weekly: never a wait time) before a run;
    ``starts`` = the account's run start times (projection of weekly use)."""
    readings = [r for r in readings if _plausible(r, now)]
    blocked = _rejection(readings, policy, now)
    if blocked is not None:
        return blocked
    usable = [r for r in readings if not r.rejected and r.utilization_pct is not None]
    weekly = _latest(usable, "weekly")
    if (
        weekly is None
        or weekly.utilization_pct is None
        or now - weekly.at > policy.max_reading_age_s
        or (weekly.resets_at is not None and weekly.resets_at <= now)
    ):
        return QuotaDecision(False, "weekly", None)
    since = sum(1 for s in starts if s > weekly.at)
    projected = weekly.utilization_pct + (since + 1) * per_run_weekly_pct(usable, starts, policy)
    if weekly.utilization_pct >= policy.weekly_stop_pct or projected >= policy.weekly_stop_pct:
        return QuotaDecision(False, "weekly", None)
    five = _latest(usable, "five_hour")
    if five is None or (five.resets_at is None and now - five.at > FIVE_HOURS_S):
        return QuotaDecision(False, "five_hour", None)
    reset_passed = five.resets_at is not None and five.resets_at <= now
    if not reset_passed and five.utilization_pct is not None:
        if five.utilization_pct >= policy.five_hour_stop_pct:
            return QuotaDecision(False, "five_hour", five.resets_at)
    if runs_this_window >= policy.max_runs_per_window:
        _, end = window_bounds(readings, now)
        return QuotaDecision(False, "window_cap", end if end is not None else now + FIVE_HOURS_S)
    return QuotaDecision(True, "ok", None)
