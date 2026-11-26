"""Constrained two-posterior Thompson sampling engine (spec sections 7.1, 7.3-7.6).

Per (point, bucket, arm) the engine keeps two posteriors, savings ``S`` and regret ``R``, each
``prior_for(...)`` + the pooled project and session evidence of the full bucket. Selection:

1. with probability ``floor[tier]`` choose the safe arm outright (propensity floor);
2. otherwise sample ``R~`` and ``S~`` per trimming arm; an arm is feasible iff ``R~ <= tau[tier]``
   (high tier: and its regret ``ucb(0.95) <= tau``, checked before sampling); the safe arm is
   always feasible with ``S == R == 0`` by definition (never sampled, never updated); the
   feasible arm with the largest ``S~`` wins, ties going to the safe arm, then declared order.

So a trimming arm can only win when its plausible regret is under the tier's target: the engine
cannot drift toward aggressive trimming by construction. The logged propensity is a Monte-Carlo
replay of step 2 (``propensity_samples`` draws from an rng seeded by the decision id, so it is
reproducible) mixed with the floor. Rewards update only the session overlay (``evidence``);
the fold into the project store is Task 22. The engine never raises into a hook: on any error
it logs and returns the safe arm with propensity 1.0 and bucket ``error``.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config, log_error
from token_sparen.events import SessionCtx
from token_sparen.learn.beta import Beta, Evidence
from token_sparen.learn.features import backoff_levels, bucket_key, lookup_param
from token_sparen.learn.hierarchy import SESSION_KEY, Kind, StoreChain, prior_for
from token_sparen.learn.policy import Decision, DecisionPoint, StaticPolicy, get_point
from token_sparen.store import jsonstore, session

ERROR_BUCKET = "error"
PENDING_KEY = "pending"
HIGH_UCB_Q = 0.95
MIN_PROPENSITY = 1e-3
_KINDS: tuple[Kind, ...] = ("savings", "regret")
_TAU = {"low": 0.10, "medium": 0.05, "high": 0.01, "none": 1.0}
_FLOOR = {"low": 0.05, "medium": 0.05, "high": 0.02, "none": 0.0}
_EMPTY = Evidence(0.0, 0.0, 0.0, 0.0)

# A trimming arm's sampling parameters: (arm, S.a, S.b, R.a, R.b), in declared order.
Cand = tuple[str, float, float, float, float]


def _is_num(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _draw(cands: list[Cand], tau: float, safe: str, rng: random.Random) -> str:
    """One constrained Thompson draw (no floor)."""
    best, best_s = safe, 0.0
    beta = rng.betavariate
    check = tau < 1.0
    for arm, sa, sb, ra, rb in cands:
        if check and beta(ra, rb) > tau:
            continue
        s = beta(sa, sb)
        if s > best_s:
            best, best_s = arm, s
    return best


class Engine:
    """Policy that learns per bucket; see the module docstring."""

    def __init__(self, stores: StoreChain, cfg: Config, rng: random.Random) -> None:
        self.stores = stores
        self.cfg = cfg
        self.rng = rng
        self._env = {"TOKEN_SPAREN_HOME": str(stores.home)}
        self._static = StaticPolicy({})
        self._points: dict[str, DecisionPoint] = {}
        self._overlay: Path | None = (
            None
            if stores.session_id is None
            else session.session_path(stores.home, stores.project_key, stores.session_id)
        )

    # --- parameters ---------------------------------------------------------------------------

    def _param(self, path: tuple[str, ...], default: float) -> float:
        value = lookup_param(self.cfg.data, ("learn", *path), _is_num)
        return float(value) if _is_num(value) else default  # type: ignore[arg-type]

    def enabled(self) -> bool:
        value = lookup_param(self.cfg.data, ("learn", "enabled"), lambda v: isinstance(v, bool))
        return value is not False

    def _tau(self, point: DecisionPoint) -> float:
        return min(1.0, max(0.0, self._param(("tau", point.tier), _TAU.get(point.tier, 0.0))))

    def _floor(self, point: DecisionPoint) -> float:
        return min(1.0, max(0.0, self._param(("floor", point.tier), _FLOOR.get(point.tier, 1.0))))

    # --- posteriors ---------------------------------------------------------------------------

    def bucket(self, point: DecisionPoint, features: Mapping[str, str]) -> str:
        return bucket_key(features, backoff_levels(point.name, self.stores.params)[0])

    def _evidence(self, point: str, bucket: str, arm: str, kind: Kind) -> Evidence:
        total = _EMPTY
        for scope in ("project", "session"):
            ev = self.stores.get(point, bucket, arm, kind, scope)
            if ev is not None:
                total = total + ev
        return total

    def posteriors(
        self, point: DecisionPoint, features: Mapping[str, str]
    ) -> dict[str, tuple[Beta, Beta]]:
        """``{arm: (S, R)}`` Beta posteriors of the trimming arms (the safe arm has none)."""
        bucket = self.bucket(point, features)
        out: dict[str, tuple[Beta, Beta]] = {}
        for arm in point.arms:
            if arm == point.safe_arm:
                continue
            post = [
                self._evidence(point.name, bucket, arm, kind).posterior(
                    prior_for(point, features, arm, kind, self.stores, child_bucket=bucket)
                )
                for kind in _KINDS
            ]
            out[arm] = (post[0], post[1])
        return out

    def _candidates(self, point: DecisionPoint, features: Mapping[str, str]) -> list[Cand]:
        tau = self._tau(point)
        cands: list[Cand] = []
        for arm, (s, r) in self.posteriors(point, features).items():
            if point.tier == "high" and r.ucb(HIGH_UCB_Q) > tau:
                continue  # high tier: never explore without the regret upper bound clearing
            cands.append((arm, s.a, s.b, r.a, r.b))
        return cands

    def _propensity(
        self, point: DecisionPoint, cands: list[Cand], arm: str, decision_id: str
    ) -> float:
        floor = self._floor(point)
        n = max(1, int(self._param(("propensity_samples",), 64.0)))
        if not cands:
            freq = 1.0 if arm == point.safe_arm else 0.0
        else:
            rng = random.Random(decision_id)  # noqa: S311 - reproducible logging, not crypto
            tau = self._tau(point)
            freq = sum(_draw(cands, tau, point.safe_arm, rng) == arm for _ in range(n)) / n
        p = floor * (arm == point.safe_arm) + (1.0 - floor) * freq
        return min(1.0, max(MIN_PROPENSITY, p))

    def propensity(
        self, point: DecisionPoint, features: Mapping[str, str], arm: str, decision_id: str
    ) -> float:
        """Probability that ``decide`` picks ``arm`` here (deterministic given ``decision_id``)."""
        return self._propensity(point, self._candidates(point, features), arm, decision_id)

    # --- Policy -------------------------------------------------------------------------------

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        if not self.enabled():
            return self._static.decide(point, features, session)
        try:
            return self._decide(point, features, session)
        except Exception as exc:  # noqa: BLE001 - never raise into a hook
            log_error(self._env, "engine", f"decide: {type(exc).__name__}: {exc}")
            name = getattr(point, "name", "?")
            safe = getattr(point, "safe_arm", "off")
            return Decision(uuid.uuid4().hex, str(name), str(safe), 1.0, ERROR_BUCKET)

    def _decide(
        self, point: DecisionPoint, features: Mapping[str, str], ctx: SessionCtx
    ) -> Decision:
        bucket = self.bucket(point, features)
        cands = self._candidates(point, features)
        decision_id = f"{self.rng.getrandbits(128):032x}"
        floor = self._floor(point)
        if floor > 0.0 and self.rng.random() < floor:
            arm = point.safe_arm
        else:
            arm = _draw(cands, self._tau(point), point.safe_arm, self.rng)
        p = self._propensity(point, cands, arm, decision_id)
        self._points[point.name] = point
        if self._overlay is None:
            self._overlay = ctx.overlay_path
        try:
            self._remember(decision_id, point.name, bucket, arm)
        except Exception as exc:  # noqa: BLE001 - the decision stands; it just cannot be rewarded
            log_error(self._env, "engine", f"pending: {type(exc).__name__}: {exc}")
        return Decision(decision_id, point.name, arm, p, bucket)

    def _remember(self, decision_id: str, point: str, bucket: str, arm: str) -> None:
        assert self._overlay is not None
        keep = max(1, int(self._param(("pending_max",), 500.0)))
        entry = [point, bucket, arm, time.time()]

        def fn(state: dict[str, Any]) -> dict[str, Any]:
            pending = state.get(PENDING_KEY)
            if not isinstance(pending, dict):
                pending = {}
            pending.pop(decision_id, None)
            pending[decision_id] = entry
            for old in list(pending)[: max(0, len(pending) - keep)]:
                del pending[old]
            state[PENDING_KEY] = pending
            return state

        jsonstore.update(self._overlay, fn, {}, self._env)

    # --- reward -------------------------------------------------------------------------------

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        """Fold a savings and/or regret observation into the session overlay evidence.

        Values are clamped to [0, 1]; NaN or non-numeric values are ignored and logged. Unknown
        (evicted) decision ids and safe-arm decisions are no-ops. Never raises.
        """
        try:
            self._reward(decision_id, savings, regret)
        except Exception as exc:  # noqa: BLE001 - never raise into a hook
            log_error(self._env, "engine", f"reward: {type(exc).__name__}: {exc}")

    def _pending(self, state: Mapping[str, Any], decision_id: str) -> tuple[str, str, str] | None:
        pending = state.get(PENDING_KEY)
        entry = pending.get(decision_id) if isinstance(pending, dict) else None
        if not isinstance(entry, list) or len(entry) < 3:
            return None
        point, bucket, arm = entry[0], entry[1], entry[2]
        if not all(isinstance(v, str) for v in (point, bucket, arm)):
            return None
        known = self._points.get(point) or get_point(point)
        if known is None:
            log_error(self._env, "engine", f"reward: unknown decision point {point!r}")
            return None
        if arm == known.safe_arm or arm not in known.arms:
            return None  # the safe arm's S and R are 0 by definition
        return point, bucket, arm

    def _reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        values: dict[Kind, float] = {}
        for kind, raw in (("savings", savings), ("regret", regret)):
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, int | float) or math.isnan(raw):
                log_error(self._env, "engine", f"reward: ignored {kind} {raw!r} (NaN/not a number)")
                continue
            values[kind] = min(1.0, max(0.0, float(raw)))  # type: ignore[index]
        if not values or self._overlay is None:
            return
        path = self._overlay
        if self._pending(jsonstore.read(path, {}, self._env), decision_id) is None:
            return  # nothing to update: skip the locked write
        gamma = min(1.0, max(0.0, self._param(("gamma",), 0.98)))
        cap = self._param(("posterior_cap",), 50.0)
        cap = cap if cap > 0.0 else 50.0
        log: Callable[[str], None] = lambda m: log_error(self._env, "engine", m)  # noqa: E731

        def fn(state: dict[str, Any]) -> dict[str, Any]:
            found = self._pending(state, decision_id)
            if found is None:
                return state
            node: dict[str, Any] = state
            for key in (SESSION_KEY, *found):
                child = node.get(key)
                if not isinstance(child, dict):
                    child = node[key] = {}
                node = child
            for kind, x in values.items():
                old = _EMPTY
                if node.get(kind) is not None:
                    try:
                        old = Evidence.from_list(node[kind])
                    except ValueError:
                        log(f"reward: corrupt session evidence {'/'.join(found)}/{kind}, reset")
                node[kind] = old.observe(x, gamma=gamma, cap=cap).to_list()
            return state

        jsonstore.update(path, fn, {}, self._env)
        self.stores.invalidate("session")
