"""E3: off-policy evaluation of a target policy on logged decisions (spec sections 7.8, 10.3).

Rewards per decision (evaluation only), each estimated on its own: ``savings`` the fraction of
the output removed (``(tokens_before - tokens_after) / tokens_before``), ``regret`` the settled
regret value (joined from the log's outcome lines; missing counts as 0 and is reported as
``regret_known``) and the combined objective ``savings - lambda * regret`` with
``lambda = 1 / tau[tier]`` (the old ``ips``/``snips``/``dr`` keys). The safe arm's savings and
regret are 0 by definition. Quality is a constraint, not a weight: ``constraint_ok`` says the
target's estimated regret is within ``tau`` of the point's tier, judged on the larger upper 95 %
bound of IPS and DR (``regret_upper``; SNIPS is conditional on support and not used).

Estimators: IPS and self-normalised IPS with importance weights ``pi(a|x) / p_log(a|x)``
clipped at ``CLIP`` (20; the clipped fraction is reported), and doubly robust with a per
(bucket, arm) mean reward model. 95 % intervals: percentile bootstrap over decisions.

**Support.** The log holds the logging propensity of the chosen arm only (clamped >= 1e-3 by
the engine; 1.0 for static, breaker-paused and held decisions). A decision with a logging
propensity <= 0 is unsupported as a whole (contributes 0, its target mass is unsupported). A
decision whose logging propensity is 1 had no other arm available, so the target's mass on any
other arm (``1 - pi(a_log|x)``) is unsupported; DR does not extrapolate the reward model onto it.
``unsupported_mass`` is the mean over evaluated decisions; estimates cover the supported mass
only and never fill the rest in (SNIPS is reported conditional on support, over fully supported
rows). Caveat: for a stochastic logger the log cannot show which other arms it could have
chosen, so ``unsupported_mass`` is a lower bound there. Evaluating ``engine-snapshot`` on the
logs its own learner was trained on is in-sample (optimistic); hold out later decisions
(``holdout_after``). Ladder-locked arms have target probability exactly 0
(``Engine.action_probs`` is unclamped). Decisions whose context cannot be recovered (bucket
``static`` / ``error`` for an engine target, unknown point) are counted in ``no_context``.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol

from token_sparen.events import SessionCtx
from token_sparen.learn.engine import ERROR_BUCKET, Engine
from token_sparen.learn.features import static_policy
from token_sparen.learn.policy import (
    DEFAULT_TAU,
    STATIC_BUCKET,
    DecisionRecord,
    Policy,
    StaticPolicy,
    get_point,
)

CLIP = 20.0
NOTES = {
    "reward": "savings = fraction of the output's estimated tokens removed; minus regret / tau",
    "support": "unsupported_mass counts target mass the log cannot support (logging propensity "
    "0, or 1 with the target elsewhere); a lower bound for stochastic loggers, which do not log "
    "the arms they could not choose; IPS and DR cover supported mass only; SNIPS is conditional "
    "on support (rows = decisions it covers)",
    "constraint": "constraint_ok = regret_upper <= tau (regret_upper: the larger upper 95 % "
    "bound of the IPS and DR regret estimates); judge a target on savings only when it holds",
    "in_sample": "engine-snapshot on the logs its own learner was trained on is in-sample and "
    "optimistic; use --holdout-after to evaluate only later decisions",
    "regret": "settled regret joins from the log's outcome lines by decision id (recovery read, "
    "re-run, asking for elided content: > 0; window expiry, session end, eviction, a trimming "
    "no-op: 0); regret_known_fraction = evaluated trimming (non-safe-arm) decisions with a "
    "joined outcome (1.0 when none needs one); a decision "
    "still unsettled (or whose outcome was lost) counts as regret 0",
}
N_BOOT = 1000
DETERMINISTIC = 1.0 - 1e-9
_CTX = SessionCtx("ope", "ope", 0, None, None, Path("ope"))


@dataclass(frozen=True)
class Estimate:
    value: float
    ci95: tuple[float, float]
    n: int  # decisions evaluated (target context known)
    clipped_fraction: float
    unsupported_mass: float
    unsupported: int  # decisions with logging propensity <= 0
    no_context: int
    regret_known: int
    needs_outcome: int
    rows: int = -1  # decisions the value is normalised over (-1: all n)
    conditional: bool = False  # SNIPS: value conditional on support (not the target's value)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": round(self.value, 6),
            "ci95": [round(x, 6) for x in self.ci95],
        }
        if self.conditional:
            out["conditional_on_support"] = True
            out["rows"] = self.rows
        return out


class Target(Protocol):
    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None: ...


def bucket_features(bucket: str) -> dict[str, str] | None:
    """Features of an engine bucket key ``k=v|k=v`` (``*`` = none); None for ``static``,
    ``error`` or a malformed key."""
    if bucket in (STATIC_BUCKET, ERROR_BUCKET) or not bucket:
        return None
    if bucket == "*":
        return {}
    out: dict[str, str] = {}
    for part in bucket.split("|"):
        key, sep, value = part.partition("=")
        if not sep or not key:
            return None
        out[key] = value
    return out


class StaticTarget:
    """A ``StaticPolicy``: probability 1 on its arm for the decision's point."""

    def __init__(self, policy: StaticPolicy) -> None:
        self.policy = policy

    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
        point = get_point(rec.point)
        if point is None:
            return None
        return {self.policy.decide(point, {}, _CTX).arm: 1.0}


class EngineTarget:
    """An ``Engine`` snapshot: its exact action probabilities for the logged context."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def probs(self, rec: DecisionRecord) -> Mapping[str, float] | None:
        point = get_point(rec.point)
        if point is not None and not self.engine.enabled():
            # `[learn] enabled = false`: the static arms, holds honoured
            return StaticTarget(static_policy(self.engine.cfg.data)).probs(rec)
        feats = bucket_features(rec.bucket)
        if point is None or feats is None:
            return None
        return self.engine.action_probs(point, feats)


def as_target(target: Target | Policy) -> Target:
    if isinstance(target, Engine):
        return EngineTarget(target)
    if isinstance(target, StaticPolicy):
        return StaticTarget(target)
    if callable(getattr(target, "probs", None)):
        return target  # type: ignore[return-value]
    raise TypeError(f"{type(target).__name__} cannot be evaluated off-policy")


PARTS = ("combined", "savings", "regret")
OBJECTIVE = "savings - regret / tau"


def tau_for(point_name: str, tau: Mapping[str, float] | None = None) -> float:
    """The regret target of a point's tier (``tau`` overrides ``DEFAULT_TAU``; 0 if unknown)."""
    point = get_point(point_name)
    taus = DEFAULT_TAU if tau is None else {**DEFAULT_TAU, **tau}
    return taus.get(point.tier, 0.0) if point is not None else 0.0


def _trims(rec: DecisionRecord) -> bool:
    """A logged non-safe arm: a decision that should get a settled regret outcome."""
    point = get_point(rec.point)
    return point is None or rec.arm != point.safe_arm


def reward(
    rec: DecisionRecord,
    regret: float | None,
    tau: Mapping[str, float] | None = None,
    *,
    part: str = "combined",
) -> float:
    """One part of a decision's reward: ``savings``, ``regret`` or the ``combined``
    ``savings - regret / tau[tier]``; 0 for the safe arm (and an unknown point's savings)."""
    if part not in PARTS:
        raise ValueError(f"unknown reward part {part!r}")
    point = get_point(rec.point)
    if point is not None and rec.arm == point.safe_arm:
        return 0.0
    if part == "regret":
        return regret or 0.0
    before = rec.tokens_before
    savings = (before - rec.tokens_after) / before if before > 0 else 0.0
    savings = min(1.0, max(0.0, savings))
    if part == "savings":
        return savings
    t = tau_for(rec.point, tau)
    lam = 1.0 / t if t > 0 else 0.0
    return savings - lam * (regret or 0.0)


class RewardModel:
    """Mean reward per (point, bucket, arm), falling back to (point, arm), then 0."""

    def __init__(self) -> None:
        self._fine: dict[tuple[str, str, str], list[float]] = {}
        self._coarse: dict[tuple[str, str], list[float]] = {}

    def observe(self, rec: DecisionRecord, r: float) -> None:
        for table, key in (
            (self._fine, (rec.point, rec.bucket, rec.arm)),
            (self._coarse, (rec.point, rec.arm)),
        ):
            acc = table.setdefault(key, [0.0, 0.0])  # type: ignore[arg-type]
            acc[0] += r
            acc[1] += 1.0

    def predict(self, rec: DecisionRecord, arm: str) -> float:
        point = get_point(rec.point)
        if point is not None and arm == point.safe_arm:
            return 0.0
        acc = self._fine.get((rec.point, rec.bucket, arm)) or self._coarse.get((rec.point, arm))
        return acc[0] / acc[1] if acc else 0.0


def fit_model(
    log: Sequence[DecisionRecord],
    regrets: Mapping[str, float] | None = None,
    tau: Mapping[str, float] | None = None,
    *,
    part: str = "combined",
) -> RewardModel:
    regrets = regrets or {}
    model = RewardModel()
    for rec in log:
        model.observe(rec, reward(rec, regrets.get(rec.decision_id), tau, part=part))
    return model


@dataclass
class _Terms:
    ips: list[float]
    weights: list[float]
    dr: list[float]
    full: list[bool] = field(default_factory=list)  # row has no unsupported target mass
    n: int = 0
    clipped: int = 0
    unsupported: int = 0
    unsupported_mass: float = 0.0
    no_context: int = 0
    regret_known: int = 0  # decisions needing an outcome (non-safe arm) that have one
    needs_outcome: int = 0  # evaluated decisions with a non-safe arm


def _terms(
    log: Sequence[DecisionRecord],
    target: Target,
    regrets: Mapping[str, float],
    model: RewardModel | None,
    clip: float,
    tau: Mapping[str, float] | None,
    part: str = "combined",
) -> _Terms:
    t = _Terms([], [], [])
    for rec in log:
        probs = target.probs(rec)
        if probs is None:
            t.no_context += 1
            continue
        t.n += 1
        known = regrets.get(rec.decision_id)
        if _trims(rec):
            t.needs_outcome += 1
            t.regret_known += known is not None
        pi_a = float(probs.get(rec.arm, 0.0))
        p = rec.propensity
        if not isinstance(p, int | float) or not math.isfinite(p) or p <= 0.0:
            t.unsupported += 1
            t.unsupported_mass += sum(float(v) for v in probs.values())
            t.ips.append(0.0)
            t.weights.append(0.0)
            t.dr.append(0.0)
            t.full.append(False)
            continue
        r = reward(rec, known, tau, part=part)
        w = pi_a / p
        if w > clip:
            w = clip
            t.clipped += 1
        t.ips.append(w * r)
        t.weights.append(w)
        t.full.append(p < DETERMINISTIC or pi_a >= DETERMINISTIC)
        if p >= DETERMINISTIC:  # the logger could choose nothing else here
            t.unsupported_mass += max(0.0, 1.0 - pi_a)
            if model is not None:
                t.dr.append(
                    pi_a * model.predict(rec, rec.arm) + w * (r - model.predict(rec, rec.arm))
                )
        elif model is not None:
            dm = sum(float(q) * model.predict(rec, arm) for arm, q in probs.items())
            t.dr.append(dm + w * (r - model.predict(rec, rec.arm)))
    return t


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _percentiles(samples: list[float]) -> tuple[float, float]:
    if not samples:
        return 0.0, 0.0
    samples.sort()
    last = len(samples) - 1
    return samples[round(0.025 * last)], samples[round(0.975 * last)]


def _bootstrap_mean(terms: list[float], n_boot: int, seed: int) -> tuple[float, float]:
    if not terms:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(terms)
    return _percentiles([sum(rng.choices(terms, k=n)) / n for _ in range(n_boot)])


def _estimate(t: _Terms, value: float, ci: tuple[float, float]) -> Estimate:
    n = t.n
    return Estimate(
        value=value,
        ci95=ci,
        n=n,
        clipped_fraction=t.clipped / n if n else 0.0,
        unsupported_mass=t.unsupported_mass / n if n else 0.0,
        unsupported=t.unsupported,
        no_context=t.no_context,
        regret_known=t.regret_known,
        needs_outcome=t.needs_outcome,
    )


def ips(
    log: Sequence[DecisionRecord],
    target: Target | Policy,
    *,
    regrets: Mapping[str, float] | None = None,
    clip: float = CLIP,
    n_boot: int = N_BOOT,
    seed: int = 0,
    tau: Mapping[str, float] | None = None,
    part: str = "combined",
) -> Estimate:
    """Inverse propensity scoring with clipped weights."""
    t = _terms(log, as_target(target), regrets or {}, None, clip, tau, part)
    return _estimate(t, _mean(t.ips), _bootstrap_mean(t.ips, n_boot, seed))


def snips(
    log: Sequence[DecisionRecord],
    target: Target | Policy,
    *,
    regrets: Mapping[str, float] | None = None,
    clip: float = CLIP,
    n_boot: int = N_BOOT,
    seed: int = 0,
    tau: Mapping[str, float] | None = None,
    part: str = "combined",
) -> Estimate:
    """Self-normalised IPS, **conditional on support**: sum(w r) / sum(w) over the decisions
    whose target mass is fully supported (a deterministic logger's row where the target also
    puts mass elsewhere is left out, so unsupported mass is never filled in with the supported
    rows' value). ``rows`` says how many decisions it covers; logs from a stochastic logger
    cannot show which other arms it could choose, so their rows always count as supported."""
    t = _terms(log, as_target(target), regrets or {}, None, clip, tau, part)
    pairs = [(x, w) for x, w, ok in zip(t.ips, t.weights, t.full, strict=True) if ok]

    def ratio(sample: Sequence[tuple[float, float]]) -> float:
        den = sum(w for _, w in sample)
        return sum(x for x, _ in sample) / den if den > 0 else 0.0

    rng = random.Random(seed)
    boots = [ratio(rng.choices(pairs, k=len(pairs))) for _ in range(n_boot)] if pairs else []
    est = _estimate(t, ratio(pairs), _percentiles(boots))
    return dataclasses.replace(est, rows=len(pairs), conditional=True)


def doubly_robust(
    log: Sequence[DecisionRecord],
    target: Target | Policy,
    model: RewardModel,
    *,
    regrets: Mapping[str, float] | None = None,
    clip: float = CLIP,
    n_boot: int = N_BOOT,
    seed: int = 0,
    tau: Mapping[str, float] | None = None,
    part: str = "combined",
) -> Estimate:
    """Doubly robust: model value under the target plus the weighted residual of the logged arm
    (no model term on unsupported mass)."""
    t = _terms(log, as_target(target), regrets or {}, model, clip, tau, part)
    return _estimate(t, _mean(t.dr), _bootstrap_mean(t.dr, n_boot, seed))


_FIELDS = {f.name for f in fields(DecisionRecord)}


def _record(row: Mapping[str, Any]) -> DecisionRecord | None:
    data = {k: v for k, v in row.items() if k in _FIELDS}
    try:
        rec = DecisionRecord(**data)
    except TypeError:
        return None
    ok = (
        isinstance(rec.decision_id, str)
        and isinstance(rec.point, str)
        and isinstance(rec.arm, str)
        and isinstance(rec.bucket, str)
        and all(
            isinstance(v, int | float) and not isinstance(v, bool)
            for v in (rec.propensity, rec.tokens_before, rec.tokens_after)
        )
    )
    return rec if ok else None


def _regret_value(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return min(1.0, max(0.0, float(value)))
    return None


def load_log(paths: Iterable[Path]) -> tuple[list[DecisionRecord], dict[str, float]]:
    """Decision records and their settled regret, joined by decision id: outcome lines
    (``{"kind": "outcome", "decision_id", "regret", ...}``, ``learn.decisions``) anywhere in the
    given logs (several per id: the largest), or a ``"regret"`` field on the decision line.
    Outcomes of decisions not in the logs are dropped; bad lines are skipped silently (no line
    content is ever echoed)."""
    log: list[DecisionRecord] = []
    regrets: dict[str, float] = {}
    outcomes: dict[str, float] = {}
    for path in paths:
        try:
            fh = Path(path).open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("kind") == "outcome":
                    did, got = row.get("decision_id"), _regret_value(row.get("regret"))
                    if isinstance(did, str) and got is not None:
                        outcomes[did] = max(got, outcomes.get(did, 0.0))
                    continue
                rec = _record(row)
                if rec is None:
                    continue
                log.append(rec)
                value = _regret_value(row.get("regret"))
                if value is not None:
                    regrets[rec.decision_id] = value
    for rec in log:
        if rec.decision_id in outcomes:
            joined = outcomes[rec.decision_id]
            regrets[rec.decision_id] = max(joined, regrets.get(rec.decision_id, 0.0))
    return log, regrets


def evaluate(
    log: Sequence[DecisionRecord],
    target: Target | Policy,
    *,
    regrets: Mapping[str, float] | None = None,
    n_boot: int = N_BOOT,
    seed: int = 0,
    tau: Mapping[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregates per decision point: n, support, clipping, and the IPS/SNIPS/DR estimates of
    savings, regret and the combined objective (the top-level ``ips``/``snips``/``dr``), the
    point's ``tau`` and ``constraint_ok`` (``regret_upper`` <= ``tau``)."""
    regrets = regrets or {}
    tgt = as_target(target)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted({r.point for r in log}):
        rows = [r for r in log if r.point == name]
        kw: dict[str, Any] = {"regrets": regrets, "n_boot": n_boot, "seed": seed, "tau": tau}
        parts: dict[str, dict[str, Estimate]] = {}
        for part in PARTS:
            model = fit_model(rows, regrets, tau, part=part)
            parts[part] = {
                "ips": ips(rows, tgt, part=part, **kw),
                "snips": snips(rows, tgt, part=part, **kw),
                "dr": doubly_robust(rows, tgt, model, part=part, **kw),
            }
        e_ips, e_snips, e_dr = (parts["combined"][k] for k in ("ips", "snips", "dr"))
        t = tau_for(name, tau)
        upper = max(parts["regret"][k].ci95[1] for k in ("ips", "dr"))
        out[name] = {
            "logged": len(rows),
            "n": e_ips.n,
            "no_context": e_ips.no_context,
            "unsupported": e_ips.unsupported,
            "unsupported_mass": round(e_ips.unsupported_mass, 6),
            "clipped_fraction": round(e_ips.clipped_fraction, 6),
            "regret_known_fraction": (
                round(e_ips.regret_known / e_ips.needs_outcome, 6) if e_ips.needs_outcome else 1.0
            ),
            "ips": e_ips.to_dict(),
            "snips": e_snips.to_dict(),
            "dr": e_dr.to_dict(),
            **{p: {k: e.to_dict() for k, e in parts[p].items()} for p in PARTS},
            "objective": OBJECTIVE,
            "tau": t,
            "regret_upper": round(upper, 6),
            "constraint_ok": upper <= t,
        }
    return out
