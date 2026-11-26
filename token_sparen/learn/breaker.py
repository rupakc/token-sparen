"""Circuit breaker per (project, decision point) (spec section 7.6).

``projects/<key>/breaker.json``::

    {"points": {<point>: {"ring": [regret, ...], "remaining": int, "notified": bool,
                          "reset": bool, "tripped_at": epoch}},
     "applied": [<fold id>, ...]}

``ring`` holds the last ``[learn] breaker_window`` (50) settled regret values of the point's
*trimming* arms (safe-arm decisions are never tracked), appended by the session fold
(``learn.overlay``) from the overlay's ``regret_outcomes``. Once it holds at least
``breaker_min_obs`` (20) values, a mean above ``2 * tau[tier]`` trips the point: for the next
``breaker_sessions`` (3) folded sessions the engine returns the safe arm for it (propensity 1),
and the next SessionStart shows one user message. When the count reaches 0 the point re-enters:
the project evidence of its trimming arms is deleted (``reset``) so the designed priors apply
again. ``remaining`` is decremented once per *newly* folded session (a re-fold changes nothing).

Everything here fails open: a corrupt or missing file reads as "ok".
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from token_sparen.config import log_error
from token_sparen.learn.features import lookup_param
from token_sparen.learn.policy import DEFAULT_TAU, DecisionPoint
from token_sparen.store import jsonstore
from token_sparen.store.paths import project_dir_for

BREAKER_FILE = "breaker.json"
WINDOW = 50
MIN_OBS = 20
SESSIONS = 3
MESSAGE = (
    "token-sparen: {point} paused for this project after repeated re-reads; "
    "it will retry in {n} sessions."
)


def _valid(value: object) -> bool:
    return num(value) is not None


def num(value: object) -> float | None:
    """``value`` as a float if it is a finite real number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def param(params: Mapping[str, Any] | None, name: str, default: float) -> float:
    value = num(lookup_param(params, ("learn", name), _valid))
    return default if value is None else value


def tau_for(point: DecisionPoint, params: Mapping[str, Any] | None = None) -> float:
    """``[learn.tau] <tier>`` clamped to [0, 1] (the engine's regret target)."""
    value = num(lookup_param(params, ("learn", "tau", point.tier), _valid))
    tau = DEFAULT_TAU.get(point.tier, 0.0) if value is None else value
    return min(1.0, max(0.0, tau))


def _remaining(stats: Mapping[str, Any]) -> int:
    value = stats.get("remaining")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def ring_of(stats: Mapping[str, Any]) -> list[float]:
    raw = stats.get("ring")
    if not isinstance(raw, list):
        return []
    return [min(1.0, max(0.0, v)) for v in map(num, raw) if v is not None]


def check(
    point: DecisionPoint,
    project_stats: Mapping[str, Any],
    *,
    tau: float | None = None,
    params: Mapping[str, Any] | None = None,
) -> Literal["ok", "tripped"]:
    """``tripped`` if the point is paused, or its rolling regret rate over the last
    ``breaker_window`` observations (at least ``breaker_min_obs``) exceeds ``2 * tau``."""
    if not isinstance(project_stats, Mapping):
        return "ok"
    if _remaining(project_stats) > 0:
        return "tripped"
    window = max(1, int(param(params, "breaker_window", WINDOW)))
    min_obs = max(1, int(param(params, "breaker_min_obs", MIN_OBS)))
    ring = ring_of(project_stats)[-window:]
    if len(ring) < min(min_obs, window):
        return "ok"
    limit = 2.0 * (tau_for(point, params) if tau is None else tau)
    return "tripped" if sum(ring) / len(ring) > limit else "ok"


def is_tripped(doc: Mapping[str, Any], point: str) -> bool:
    """Is ``point`` paused in a loaded ``breaker.json`` document?"""
    points = doc.get("points") if isinstance(doc, Mapping) else None
    stats = points.get(point) if isinstance(points, Mapping) else None
    return isinstance(stats, Mapping) and _remaining(stats) > 0


def path_for(home: Path, project_key: str) -> Path:
    return project_dir_for(home, project_key) / BREAKER_FILE


def _points(doc: dict[str, Any]) -> dict[str, Any]:
    points = doc.get("points")
    if not isinstance(points, dict):
        points = doc["points"] = {}
    return points


def _stats(points: dict[str, Any], name: str) -> dict[str, Any]:
    stats = points.get(name)
    if not isinstance(stats, dict):
        stats = points[name] = {}
    return stats


def apply_session(
    doc: dict[str, Any],
    outcomes: list[tuple[DecisionPoint, float]],
    *,
    now: float,
    params: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """One newly folded session, in place: decrement paused points (0 -> re-entry with
    ``reset``), append the trimming-arm outcomes to the rings, then trip points whose rate is
    over the limit. Returns (tripped, reentered) point names."""
    points = _points(doc)
    window = max(1, int(param(params, "breaker_window", WINDOW)))
    sessions = max(1, int(param(params, "breaker_sessions", SESSIONS)))
    reentered: list[str] = []
    for name, stats in list(points.items()):
        if isinstance(stats, dict) and _remaining(stats) > 0:
            stats["remaining"] = _remaining(stats) - 1
            if stats["remaining"] == 0:
                stats["reset"] = True
                stats.pop("notified", None)
                reentered.append(name)
    by_point: dict[str, tuple[DecisionPoint, list[float]]] = {}
    for point, value in outcomes:
        by_point.setdefault(point.name, (point, []))[1].append(value)
    tripped: list[str] = []
    for name, (point, values) in by_point.items():
        stats = _stats(points, name)
        if _remaining(stats) > 0:
            continue  # paused: only safe-arm decisions are being made
        stats["ring"] = [*ring_of(stats), *values][-window:]
        if check(point, stats, params=params) == "tripped":
            stats.update(ring=[], remaining=sessions, notified=False, tripped_at=now)
            tripped.append(name)
    return tripped, reentered


def take_resets(doc: dict[str, Any]) -> list[str]:
    """Points flagged for re-entry reset; clears the flags (in place)."""
    out: list[str] = []
    for name, stats in _points(doc).items():
        if isinstance(stats, dict) and stats.pop("reset", False) is True:
            out.append(name)
    return out


def pending_resets(doc: Mapping[str, Any]) -> list[str]:
    points = doc.get("points") if isinstance(doc, Mapping) else None
    if not isinstance(points, Mapping):
        return []
    return [n for n, s in points.items() if isinstance(s, Mapping) and s.get("reset") is True]


def notices(project_key: str, home: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """User messages for points tripped since the last call (each shown once). Never raises."""
    env = {"TOKEN_SPAREN_HOME": str(home)} if env is None else env
    path = path_for(home, project_key)
    try:
        if not path.exists() or not _unnotified(jsonstore.read(path, {}, env)):
            return []  # lock-free pre-check: nothing to say -> no write
        out: list[str] = []

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            out.clear()
            for name, stats in _points(doc).items():
                if isinstance(stats, dict) and _remaining(stats) > 0 and not stats.get("notified"):
                    stats["notified"] = True
                    out.append(MESSAGE.format(point=name, n=_remaining(stats)))
            return doc

        jsonstore.update(path, fn, {}, env)
        return out
    except Exception as exc:  # noqa: BLE001 - fail open
        log_error(env, "breaker", f"notices: {type(exc).__name__}: {exc}")
        return []


def _unnotified(doc: Mapping[str, Any]) -> bool:
    points = doc.get("points")
    if not isinstance(points, Mapping):
        return False
    return any(
        isinstance(s, Mapping) and _remaining(s) > 0 and not s.get("notified")
        for s in points.values()
    )
