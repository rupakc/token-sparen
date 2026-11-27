"""Decision points, the policy contract and the static (non-learning) policy.

These types are the stable contract between mechanisms and the learning engine: a mechanism
declares a ``DecisionPoint``, asks a ``Policy`` for a ``Decision`` and logs a ``DecisionRecord``
with the propensity of the chosen arm so later off-policy evaluation stays unbiased.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from token_sparen.events import SessionCtx

Tier = Literal["low", "medium", "high", "none"]
STATIC_BUCKET = "static"


@dataclass(frozen=True)
class DecisionPoint:
    name: str
    arms: tuple[str, ...]
    safe_arm: str
    tier: Tier


@dataclass(frozen=True)
class Decision:
    decision_id: str
    point: str
    arm: str
    propensity: float
    bucket: str


@dataclass(frozen=True)
class DecisionRecord:
    """One line of ``projects/<key>/decisions.jsonl``."""

    decision_id: str
    point: str
    arm: str
    propensity: float
    bucket: str
    tokens_before: int
    tokens_after: int
    recovery_id: str | None
    tool_use_id: str | None
    command_sig: str
    ts: float
    ctype: str | None = None
    tool_name: str | None = None
    tokens_full: int | None = None  # persisted full output size, when it differed from preview
    host: str | None = None  # originating host ("cli" for `token-sparen run`)


class Policy(Protocol):
    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision: ...


COMPRESSION = DecisionPoint(
    "compression", ("off", "light", "standard", "aggressive"), safe_arm="off", tier="medium"
)
# PostToolUse: a learned-noisy Bash command's output gets a tail-biased compression level. Only
# asked when the project store lists the command as a candidate; the command is never rewritten.
PRETRIM = DecisionPoint("pretrim", ("off", "bounded"), safe_arm="off", tier="medium")
# PostToolUse(Read): replace a re-read of unchanged, still-in-context content with a pointer.
READ_DEDUPE = DecisionPoint("read_dedupe", ("off", "pointer"), safe_arm="off", tier="low")
# PreToolUse(Task|Agent): append a one-line "conclusions, not dumps" brief to the subagent prompt.
SUBAGENT_BRIEF = DecisionPoint("subagent_brief", ("off", "on"), safe_arm="off", tier="medium")
# Post-tool (failures, edits): how many repeats of one error within a turn before one redirect
# (k = 5 / 3 / 2; ``mechanisms.loopguard``). The most patient arm intervenes least.
LOOPGUARD = DecisionPoint(
    "loopguard", ("patient", "standard", "eager"), safe_arm="patient", tier="medium"
)
_TIERS: frozenset[str] = frozenset({"low", "medium", "high", "none"})
# Regret target per tier (``[learn.tau]`` overrides): the engine's feasibility bound and the
# circuit breaker's reference (trips above 2 x tau).
DEFAULT_TAU: dict[str, float] = {"low": 0.10, "medium": 0.05, "high": 0.01, "none": 1.0}
_BUILTIN_POINTS: dict[str, DecisionPoint] = {
    p.name: p for p in (COMPRESSION, PRETRIM, READ_DEDUPE, SUBAGENT_BRIEF, LOOPGUARD)
}
_POINTS: dict[str, DecisionPoint] = dict(_BUILTIN_POINTS)


def validate_point(point: object) -> DecisionPoint:
    """``point`` if it is a well-formed DecisionPoint, else TypeError/ValueError."""
    if not isinstance(point, DecisionPoint):
        raise TypeError(f"{type(point).__name__} is not a DecisionPoint")
    if not isinstance(point.name, str) or not point.name:
        raise ValueError("decision point name must be a non-empty str")
    arms = point.arms
    if not isinstance(arms, tuple) or not arms or not all(isinstance(a, str) for a in arms):
        raise ValueError(f"{point.name}: arms must be a non-empty tuple of str")
    if point.safe_arm not in arms:
        raise ValueError(f"{point.name}: safe_arm {point.safe_arm!r} is not one of its arms")
    if point.tier not in _TIERS:
        raise ValueError(f"{point.name}: tier {point.tier!r} is not one of {sorted(_TIERS)}")
    return point


def register_point(point: DecisionPoint) -> None:
    """Add an extension decision point. Built-in names can never be replaced."""
    validate_point(point)
    if point.name in _BUILTIN_POINTS:
        raise ValueError(f"{point.name!r} is a built-in decision point")
    existing = _POINTS.get(point.name)
    if existing is not None and existing != point:
        raise ValueError(f"decision point {point.name!r} is already registered differently")
    _POINTS[point.name] = point


def get_point(name: str) -> DecisionPoint | None:
    return _POINTS.get(name)


def points() -> dict[str, DecisionPoint]:
    """Every known decision point by name (built-ins and registered extensions)."""
    return dict(_POINTS)


_DEFAULT_ARMS = {
    COMPRESSION.name: "standard",
    PRETRIM.name: "off",
    READ_DEDUPE.name: "pointer",
    SUBAGENT_BRIEF.name: "off",
}


class StaticPolicy:
    """Always the configured arm (propensity 1.0); unknown or invalid arms fall back to safe.

    ``held(point_name)`` (``learn.features.held`` over the merged config, when given): a held
    point always gets its safe arm, as with the Engine, so ``[learn.hold]`` also binds when
    learning is disabled or the engine fails."""

    def __init__(
        self, arms_by_point: dict[str, str], *, held: Callable[[str], bool] | None = None
    ) -> None:
        self._arms = {**_DEFAULT_ARMS, **arms_by_point}
        self._held = held

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        arm = self._arms.get(point.name, point.safe_arm)
        if self._held is not None and self._held(point.name):
            arm = point.safe_arm
        if arm not in point.arms:
            arm = point.safe_arm
        return Decision(
            decision_id=uuid.uuid4().hex,
            point=point.name,
            arm=arm,
            propensity=1.0,
            bucket=STATIC_BUCKET,
        )
