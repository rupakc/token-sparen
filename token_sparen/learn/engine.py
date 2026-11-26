"""Constrained two-posterior Thompson sampling engine (spec sections 7.1, 7.3-7.6).

Per (point, bucket, arm) the engine keeps two posteriors, savings ``S`` and regret ``R``, each
``prior_for(...)`` + the pooled project and session evidence of the full bucket. Selection:

1. with probability ``floor[tier]`` choose the safe arm outright (propensity floor);
2. otherwise sample ``R~`` and ``S~`` per trimming arm; an arm is feasible iff ``R~ <= tau[tier]``
   (high tier: and its regret ``ucb(0.95) <= tau``, checked before sampling); the safe arm is
   always feasible with ``S == R == 0`` by definition (never sampled, never updated); the
   feasible arm with the largest ``S~`` wins, ties going to the safe arm, then declared order.

So a trimming arm can only win when its plausible regret is under the tier's target: the engine
cannot drift toward aggressive trimming by construction. Before sampling, the safe-exploration
ladder and (high tier) the user opt-in and ucb gate remove arms deterministically. The logged
propensity is exact (``learn.propensity``: numerical integral of the win probability) mixed with
the floor, so off-policy estimates stay unbiased. Decision ids are uuid4. Rewards update only
the session overlay (``evidence``); ``learn.overlay.fold`` merges them into the project store.
A point paused by the project's circuit breaker (``learn.breaker``) gets the safe arm with
propensity 1. The engine never raises into a hook: on any error it logs and returns the safe
arm, propensity 1.0, bucket ``error``.
"""

from __future__ import annotations

import math
import os
import random
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import ENV_PREFIX, Config, flag_off, log_error
from token_sparen.events import SessionCtx
from token_sparen.learn import pending
from token_sparen.learn.beta import Beta, Evidence, beta_cdf
from token_sparen.learn.features import backoff_keys, backoff_levels, bucket_key, lookup_param
from token_sparen.learn.hierarchy import SESSION_KEY, Kind, StoreChain, prior_for
from token_sparen.learn.policy import (
    DEFAULT_TAU,
    Decision,
    DecisionPoint,
    StaticPolicy,
    get_point,
)
from token_sparen.learn.propensity import win_probabilities
from token_sparen.store import jsonstore, session

ERROR_BUCKET = "error"
PENDING_KEY = "pending"
HIGH_UCB_Q = 0.95
MIN_PROPENSITY = 1e-3
_KINDS: tuple[Kind, ...] = ("savings", "regret")
_TAU = DEFAULT_TAU
_FLOOR = {"low": 0.05, "medium": 0.05, "high": 0.02, "none": 0.0}
_EMPTY = Evidence(0.0, 0.0, 0.0, 0.0)

# A trimming arm's sampling parameters: (arm, S.a, S.b, R.a, R.b), in declared order.
Cand = tuple[str, float, float, float, float]


def _is_num(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in {"1", "true"}


def _received(entry: list[Any]) -> set[str]:
    """Reward kinds a pending entry ``[point, bucket, arm, ts, [kinds]]`` already received."""
    kinds = entry[4] if len(entry) > 4 else None
    return {k for k in kinds if isinstance(k, str)} if isinstance(kinds, list) else set()


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

    def __init__(
        self,
        stores: StoreChain,
        cfg: Config,
        rng: random.Random,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.stores = stores
        self._os_env: Mapping[str, str] = os.environ if env is None else env
        self._optin: dict[str, bool] = {}
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
        """``[learn] enabled`` unless switched off in any spelling (``config.flag_off``)."""
        return not flag_off(self.cfg.get("learn.enabled", True))

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

    def _established_n(self, point: DecisionPoint, features: Mapping[str, str], arm: str) -> float:
        """Max regret observation count of ``arm``: pooled full bucket, or any back-off level in
        project or global scope."""
        bucket = self.bucket(point, features)
        best = self._evidence(point.name, bucket, arm, "regret").n
        for scope in ("project", "global"):
            for key in backoff_keys(point.name, features, self.stores.params):
                ev = self.stores.get(point.name, key, arm, "regret", scope)
                if ev is not None and ev.n > best:
                    best = ev.n
        return best

    def _ladder(
        self,
        point: DecisionPoint,
        features: Mapping[str, str],
        posts: Mapping[str, tuple[Beta, Beta]],
    ) -> list[str]:
        """Trimming arms the safe-exploration ladder allows, in declared order: the first is
        always eligible; each next one only while every safer one is eligible and the next-safer
        one is established (``n >= ladder_obs`` and regret posterior mean <= tau)."""
        tau = self._tau(point)
        need = self._param(("ladder_obs",), 20.0)
        out: list[str] = []
        prev: str | None = None
        for arm in posts:
            if prev is not None and not (
                posts[prev][1].mean <= tau and self._established_n(point, features, prev) >= need
            ):
                break
            out.append(arm)
            prev = arm
        return out

    def eligible(self, point: DecisionPoint, features: Mapping[str, str]) -> list[str]:
        """Trimming arms allowed to compete (ladder, then the high-tier opt-in and ucb gate)."""
        return [c[0] for c in self._candidates(point, features)]

    def opted_in(self, point: DecisionPoint) -> bool:
        """High-tier points learn only with ``[learn.optin] <point> = true`` in the merged config
        *and* set by the user (``~/.token-sparen/config.toml`` or the env var), never a project."""
        cached = self._optin.get(point.name)
        if cached is not None:
            return cached
        ok = _truthy(self.cfg.get(f"learn.optin.{point.name}")) and self._user_optin(point.name)
        if not ok and _truthy(self.cfg.get(f"learn.optin.{point.name}")):
            log_error(
                self._env,
                "engine",
                f"learn.optin.{point.name} ignored: set it in ~/.token-sparen/config.toml or "
                f"{ENV_PREFIX}LEARN__OPTIN__{point.name.upper()}, not in a project file",
            )
        self._optin[point.name] = ok
        return ok

    def _user_optin(self, name: str) -> bool:
        if _truthy(self._os_env.get(f"{ENV_PREFIX}LEARN__OPTIN__{name.upper()}")):
            return True
        try:
            text = (self.stores.home / "config.toml").read_text(encoding="utf-8")
            user = tomllib.loads(text)
        except (OSError, ValueError):
            return False
        section = (
            user.get("learn", {}).get("optin") if isinstance(user.get("learn"), dict) else None
        )
        return isinstance(section, dict) and _truthy(section.get(name))

    def held(self, point: DecisionPoint) -> bool:
        """``[learn.hold] <point> = true``: the point is decided here (bucket, breaker, pending,
        propensity 1) but never explores a trimming arm, e.g. the subagent brief, which spec 6.7
        keeps off until the live benchmark shows non-inferior quality."""
        value = lookup_param(
            self.cfg.data, ("learn", "hold", point.name), lambda v: isinstance(v, bool)
        )
        return value is True

    def _candidates(self, point: DecisionPoint, features: Mapping[str, str]) -> list[Cand]:
        if self.held(point) or (point.tier == "high" and not self.opted_in(point)):
            return []
        tau = self._tau(point)
        posts = self.posteriors(point, features)
        cands: list[Cand] = []
        for arm in self._ladder(point, features, posts):
            s, r = posts[arm]
            if point.tier == "high" and r.ucb(HIGH_UCB_Q) > tau:
                continue  # high tier: never explore without the regret upper bound clearing
            cands.append((arm, s.a, s.b, r.a, r.b))
        return cands

    def _propensity(self, point: DecisionPoint, cands: list[Cand], arm: str) -> float:
        """Exact probability that ``decide`` picks ``arm`` (floor mixed with the sampling branch;
        see ``learn.propensity``); 0 for an ineligible arm. ``decide`` logs it clamped to
        [1e-3, 1]."""
        tau = self._tau(point)
        safe, ps = win_probabilities(
            [
                (Beta(sa, sb), 1.0 if tau >= 1.0 else beta_cdf(tau, ra, rb))
                for _, sa, sb, ra, rb in cands
            ]
        )
        branch = dict(zip((c[0] for c in cands), ps, strict=True))
        branch[point.safe_arm] = safe
        floor = self._floor(point)
        p = floor * (arm == point.safe_arm) + (1.0 - floor) * branch.get(arm, 0.0)
        return min(1.0, max(0.0, p))

    def propensity(self, point: DecisionPoint, features: Mapping[str, str], arm: str) -> float:
        """Probability that ``decide`` picks ``arm`` for these features (exact)."""
        return self._propensity(point, self._candidates(point, features), arm)

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
        decision_id = uuid.uuid4().hex
        if self.stores.tripped(point.name):
            arm, p = point.safe_arm, 1.0  # circuit breaker: paused for this project
        else:
            cands = self._candidates(point, features)
            floor = self._floor(point)
            if floor > 0.0 and self.rng.random() < floor:
                arm = point.safe_arm
            else:
                arm = _draw(cands, self._tau(point), point.safe_arm, self.rng)
            p = min(1.0, max(MIN_PROPENSITY, self._propensity(point, cands, arm)))
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
        (evicted) decision ids and safe-arm decisions are no-ops. Each decision takes at most one
        savings and one regret reward; a repeat of a kind is ignored and logged. Callers must
        report ``regret=0.0`` explicitly when the regret window expires without regret (Task 21):
        silence is not evidence. Never raises.
        """
        try:
            self._reward(decision_id, savings, regret)
        except Exception as exc:  # noqa: BLE001 - never raise into a hook
            log_error(self._env, "engine", f"reward: {type(exc).__name__}: {exc}")

    def _pending(
        self, state: Mapping[str, Any], decision_id: str
    ) -> tuple[str, str, str, list[Any]] | None:
        """``(point, bucket, arm, entry)`` of a rewardable pending decision, else None."""
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
        return point, bucket, arm, entry

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
        found = self._pending(jsonstore.read(path, {}, self._env), decision_id)
        if found is None:
            return  # nothing to update: skip the locked write
        repeat = sorted(k for k in values if k in _received(found[3]))
        if repeat:
            log_error(
                self._env, "engine", f"reward: {decision_id} already rewarded {repeat}, ignored"
            )
            for k in repeat:
                del values[k]
            if not values:
                return
        gamma = min(1.0, max(0.0, self._param(("gamma",), 0.98)))
        cap = self._param(("posterior_cap",), 50.0)
        cap = cap if cap > 0.0 else 50.0
        log: Callable[[str], None] = lambda m: log_error(self._env, "engine", m)  # noqa: E731

        def fn(state: dict[str, Any]) -> dict[str, Any]:
            found = self._pending(state, decision_id)
            if found is None:
                return state
            *where, entry = found
            done = _received(entry)
            fresh = {k: x for k, x in values.items() if k not in done}  # a racing reward won
            if not fresh:
                return state
            while len(entry) < 4:
                entry.append(time.time())
            del entry[4:]
            entry.append(sorted(done | set(fresh)))
            node: dict[str, Any] = state
            for key in (SESSION_KEY, *where):
                child = node.get(key)
                if not isinstance(child, dict):
                    child = node[key] = {}
                node = child
            for kind, x in fresh.items():
                old = _EMPTY
                if node.get(kind) is not None:
                    try:
                        old = Evidence.from_list(node[kind])
                    except ValueError:
                        log(f"reward: corrupt session evidence {'/'.join(where)}/{kind}, reset")
                node[kind] = old.observe(x, gamma=gamma, cap=cap).to_list()
            return state

        jsonstore.update(path, fn, {}, self._env)
        self.stores.invalidate("session")
        pending.add_overlay(path, self._env)  # the session now has evidence awaiting a fold
