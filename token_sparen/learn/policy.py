"""Decision points, the policy contract and the static (non-learning) policy.

These types are the stable contract between mechanisms and the learning engine: a mechanism
declares a ``DecisionPoint``, asks a ``Policy`` for a ``Decision`` and logs a ``DecisionRecord``
with the propensity of the chosen arm so later off-policy evaluation stays unbiased.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
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


class Policy(Protocol):
    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision: ...


COMPRESSION = DecisionPoint(
    "compression", ("off", "light", "standard", "aggressive"), safe_arm="off", tier="medium"
)
# PreToolUse: rewrite a learned-noisy Bash command to `<cmd> 2>&1 | tail -n K`. Only asked when the
# project store lists the command as a candidate, so it is effectively off until candidates exist.
PRETRIM = DecisionPoint("pretrim", ("off", "bounded"), safe_arm="off", tier="medium")
# PostToolUse(Read): replace a re-read of unchanged, still-in-context content with a pointer.
READ_DEDUPE = DecisionPoint("read_dedupe", ("off", "pointer"), safe_arm="off", tier="low")
_DEFAULT_ARMS = {
    COMPRESSION.name: "standard",
    PRETRIM.name: "bounded",
    READ_DEDUPE.name: "pointer",
}


class StaticPolicy:
    """Always the configured arm (propensity 1.0); unknown or invalid arms fall back to safe."""

    def __init__(self, arms_by_point: dict[str, str]) -> None:
        self._arms = {**_DEFAULT_ARMS, **arms_by_point}

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        arm = self._arms.get(point.name, point.safe_arm)
        if arm not in point.arms:
            arm = point.safe_arm
        return Decision(
            decision_id=uuid.uuid4().hex,
            point=point.name,
            arm=arm,
            propensity=1.0,
            bucket=STATIC_BUCKET,
        )
