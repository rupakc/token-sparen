"""Discrete context features per decision point and their bucket keys (spec section 7.2).

Pure and cheap: no I/O. Each point declares a fixed feature order (``FEATURE_KEYS``); a bucket
key is ``k=v|k=v`` in a level's order, ``*`` for the empty level. Back-off levels (most specific
first) come from ``[learn.backoff.<point>] levels`` in the packaged defaults or given params.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from token_sparen.config import flag_off, flag_on, packaged_defaults
from token_sparen.estimate import estimate_tokens
from token_sparen.events import Event, SessionCtx
from token_sparen.learn.policy import DecisionPoint, StaticPolicy

UNKNOWN = "unk"
ANY_BUCKET = "*"
PHASE_WINDOW = 10  # last tool calls considered
PHASE_MIN_CALLS = 3  # fewer -> "mixed"
DEBUG_SHARE = 0.3  # failed calls
BUILD_SHARE = 0.3  # edits
EXPLORE_SHARE = 0.6  # reads + searches
_FAILED = "!"
_EXPLORE_CATS = frozenset({"read", "search"})

FEATURE_KEYS: dict[str, tuple[str, ...]] = {
    "compression": ("band", "cat", "size", "ctype", "phase"),
    "pretrim": ("ctype", "size"),
    "read_dedupe": ("cat", "size"),
    "subagent_brief": ("phase",),
}


def band(ctx_pct: float | None) -> str:
    """Context fill band; ``ctx_pct`` is a fraction of the window (0.5 == 50 %)."""
    if ctx_pct is None:
        return UNKNOWN
    if ctx_pct < 0.5:
        return "lt50"
    if ctx_pct < 0.75:
        return "50_75"
    return "75_95" if ctx_pct < 0.95 else "gt95"


def size_band(tokens: int) -> str:
    if tokens < 8000:
        return "sm"
    return "md" if tokens < 50000 else "lg"


def phase(recent_cats: Sequence[str]) -> str:
    """Task phase from the last 10 tool categories (a failed call is ``<cat>!``).

    Checked in order: failures >= 30 % -> debug (a failing edit loop is debugging, not
    building), edits >= 30 % -> build, reads + searches >= 60 % -> explore, else mixed.
    """
    window = list(recent_cats)[-PHASE_WINDOW:]
    if len(window) < PHASE_MIN_CALLS:
        return "mixed"
    total = len(window)
    failed = sum(1 for c in window if c.endswith(_FAILED))
    cats = [c.removesuffix(_FAILED) for c in window]
    if failed / total >= DEBUG_SHARE:
        return "debug"
    if cats.count("edit") / total >= BUILD_SHARE:
        return "build"
    if sum(1 for c in cats if c in _EXPLORE_CATS) / total >= EXPLORE_SHARE:
        return "explore"
    return "mixed"


def for_point(
    point: DecisionPoint,
    event: Event,
    session: SessionCtx,
    content_type: str | None,
    *,
    tokens: int | None = None,
) -> dict[str, str]:
    """The point's features in its declared order; unknown (extension) points get ``{}``.

    ``tokens`` is the output size when the caller already
    estimated it; otherwise it is estimated from ``event.output_text``. Values are sanitised
    (``|`` and ``=`` become ``_``) so ``bucket_key`` never raises on the hook path.
    """
    keys = FEATURE_KEYS.get(point.name, ())
    out: dict[str, str] = {}
    for key in keys:
        if key == "band":
            out[key] = band(session.ctx_pct)
        elif key == "cat":
            out[key] = event.tool.category if event.tool is not None else "other"
        elif key == "size":
            n = tokens if tokens is not None else estimate_tokens(event.output_text or "")
            out[key] = size_band(n)
        elif key == "ctype":
            out[key] = content_type or UNKNOWN
        else:  # phase
            out[key] = phase(session.recent_cats)
    return {k: _sanitise(v) for k, v in out.items()}


def _sanitise(value: str) -> str:
    return value.replace("|", "_").replace("=", "_")


def bucket_key(features: Mapping[str, str], levels: Sequence[str]) -> str:
    """Canonical ``k=v|k=v`` in ``levels`` order; ``*`` for no keys; a missing value is unk.

    A key or value containing ``|`` or ``=`` would make keys ambiguous: ValueError.
    """
    if not levels:
        return ANY_BUCKET
    parts = []
    for k in levels:
        v = features.get(k, UNKNOWN)
        if "|" in k or "=" in k or "|" in v or "=" in v:
            raise ValueError(f"feature {k!r}={v!r} contains '|' or '='")
        parts.append(f"{k}={v}")
    return "|".join(parts)


def _valid_levels(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(lv, list) and all(isinstance(k, str) for k in lv) for lv in value)
    )


@functools.lru_cache(maxsize=1)
def packaged_params() -> dict[str, Any]:
    return packaged_defaults()


def _dig(data: Mapping[str, Any] | None, path: Sequence[str]) -> object:
    node: object = data
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def lookup_param(
    params: Mapping[str, Any] | None, path: Sequence[str], valid: Callable[[object], bool]
) -> object:
    """``path`` in ``params`` (merged config) if valid, else in the packaged defaults, else None."""
    for data in (params, packaged_params()):
        value = _dig(data, path)
        if value is not None and valid(value):
            return value
    return None


# A hold on the key point also holds these: ``[learn.hold] compression = true`` rules out every
# output trim, the learned pre-trim arm included.
HOLD_IMPLIES: dict[str, tuple[str, ...]] = {"pretrim": ("compression",)}


def held(params: Mapping[str, Any] | None, point: str) -> bool:
    """``[learn.hold] <point>`` (or a point that implies it, ``HOLD_IMPLIES``) in any on
    spelling (``config.flag_on``); an unrecognised value falls back to the packaged default (the
    subagent brief is held there)."""
    for name in (point, *HOLD_IMPLIES.get(point, ())):
        value = lookup_param(params, ("learn", "hold", name), lambda v: flag_on(v) or flag_off(v))
        if flag_on(value):
            return True
    return False


def static_policy(
    params: Mapping[str, Any] | None, arms: dict[str, str] | None = None
) -> StaticPolicy:
    """The static policy that honours ``[learn.hold]`` of ``params`` (merged config data)."""
    return StaticPolicy(arms or {}, held=lambda name: held(params, name))


def backoff_levels(point: str, params: Mapping[str, Any] | None = None) -> list[list[str]]:
    """Back-off levels, most specific first, always ending in ``[]`` (the ``*`` bucket).

    ``[learn.backoff.<point>] levels`` from ``params`` (merged config), else the packaged
    defaults; neither valid -> ``[full feature set, []]`` (``[[]]`` for an unknown point).
    """
    value = lookup_param(params, ("learn", "backoff", point, "levels"), _valid_levels)
    if isinstance(value, list):
        levels = [list(lv) for lv in value]
    else:
        full = list(FEATURE_KEYS.get(point, ()))
        levels = [full] if full else []
    if not levels or levels[-1]:
        levels.append([])
    return levels


def backoff_keys(
    point: str, features: Mapping[str, str], params: Mapping[str, Any] | None = None
) -> list[str]:
    """Bucket keys from most specific to ``*``."""
    return [bucket_key(features, lv) for lv in backoff_levels(point, params)]
