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
Decisions awaiting a reward live in a sidecar (``pending_path``), not in the overlay.
M4 option ``[learn] ladder_retry_decisions`` (0 = off, today's behaviour): after that many
decisions of a bucket whose next harsher arm is locked only because the safer arm is not yet
established, the ladder re-opens that arm for one decision (a probe; counts in the pending
sidecar, per session); the draw and its exact propensity use the same ladder state.
A point paused by the project's circuit breaker (``learn.breaker``) gets the safe arm with
propensity 1. The engine never raises into a hook: on any error it logs and returns the safe
arm, propensity 1.0, bucket ``error``.
"""

from __future__ import annotations

import math
import random
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config, flag_off, flag_on, log_error
from token_sparen.events import SessionCtx
from token_sparen.learn import pending
from token_sparen.learn.beta import Beta, Evidence, beta_cdf
from token_sparen.learn.features import (
    backoff_keys,
    backoff_levels,
    bucket_key,
    held,
    lookup_param,
    static_policy,
)
from token_sparen.learn.hierarchy import SESSION_KEY, Kind, StoreChain, prior_for
from token_sparen.learn.policy import (
    DEFAULT_TAU,
    Decision,
    DecisionPoint,
    get_point,
)
from token_sparen.learn.propensity import win_probabilities
from token_sparen.store import jsonstore, session

ERROR_BUCKET = "error"
PENDING_KEY = "pending"
LADDER_WAIT_KEY = "ladder_wait"  # pending sidecar: {point: {bucket: decisions since locked}}
HIGH_UCB_Q = 0.95
GATE_Q = 0.9  # natural-rate gate: lcb_0.9(arm savings) > ucb_0.9(natural rate)
MIN_PROPENSITY = 1e-3
_KINDS: tuple[Kind, ...] = ("savings", "regret")
_TAU = DEFAULT_TAU
_FLOOR = {"low": 0.05, "medium": 0.05, "high": 0.02, "none": 0.0}
_EMPTY = Evidence(0.0, 0.0, 0.0, 0.0)

# A trimming arm's sampling parameters: (arm, S.a, S.b, R.a, R.b), in declared order.
Cand = tuple[str, float, float, float, float]


def pending_path(overlay: Path) -> Path:
    """Decisions awaiting their reward: a sidecar of the session overlay (a subdirectory, so
    session scans of ``sessions/*.json`` never see it). Up to ``pending_max`` (500) entries stay
    out of the overlay that every hook parses and rewrites several times."""
    return overlay.parent / "pending" / overlay.name


def _is_num(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _truthy(value: object) -> bool:
    return flag_on(value)


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
        del env  # kept for callers; opt-ins are read from the trusted config layers only
        self._optin: dict[str, bool] = {}
        self.cfg = cfg
        self.rng = rng
        self._env = {"TOKEN_SPAREN_HOME": str(stores.home)}
        self._static = static_policy(cfg.data)
        self._points: dict[str, DecisionPoint] = {}
        self.last_explored = False  # the last ``choose`` took a gated point's exploration branch
        # ladder retry (``[learn] ladder_retry_decisions``, off by default): the counts read once
        # from the pending sidecar, and the ladder state of the last ``_ladder`` call
        self._waits: dict[str, Any] | None = None
        self._last_lock: tuple[str, str, str] | None = None  # (point, bucket, locked|reopened)
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
        locked: str | None = None
        for arm in posts:
            if prev is not None and not (
                posts[prev][1].mean <= tau and self._established_n(point, features, prev) >= need
            ):
                # retry only while the safer arm is merely unestablished, never known risky
                locked = arm if posts[prev][1].mean <= tau else None
                break
            out.append(arm)
            prev = arm
        self._last_lock = None
        retry = self._param(("ladder_retry_decisions",), 0.0)
        if retry > 0 and locked is not None:
            bucket = self.bucket(point, features)
            if self._wait(point.name, bucket) >= retry:
                out.append(locked)  # probe the next harsher arm for this one decision
                self._last_lock = (point.name, bucket, "reopened")
            else:
                self._last_lock = (point.name, bucket, "locked")
        return out

    def _wait(self, point: str, bucket: str) -> int:
        """Decisions of ``(point, bucket)`` since its next harsher arm was locked (or last
        re-opened), from this session's pending sidecar (read once per engine): the count is per
        session, so a short session re-opens less often, never more."""
        if self._waits is None:
            self._waits = {}
            if self._overlay is not None:
                doc = jsonstore.read(pending_path(self._overlay), {}, self._env)
                table = doc.get(LADDER_WAIT_KEY) if isinstance(doc, dict) else None
                if isinstance(table, dict):
                    self._waits = table
        row = self._waits.get(point)
        n = row.get(bucket) if isinstance(row, dict) else None
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0

    def eligible(self, point: DecisionPoint, features: Mapping[str, str]) -> list[str]:
        """Trimming arms allowed to compete (ladder, then the high-tier opt-in and ucb gate)."""
        return [c[0] for c in self._candidates(point, features)]

    def opted_in(self, point: DecisionPoint) -> bool:
        """High-tier points learn only with ``[learn.optin] <point> = true`` in the merged config
        *and* set in the user's ``~/.token-sparen/config.toml``: never a project file, a session
        override or a ``TOKEN_SPAREN_*`` variable (a repository's committed Claude Code settings
        can set those)."""
        cached = self._optin.get(point.name)
        if cached is not None:
            return cached
        ok = _truthy(self.cfg.get(f"learn.optin.{point.name}")) and self._user_optin(point.name)
        if not ok and _truthy(self.cfg.get(f"learn.optin.{point.name}")):
            log_error(
                self._env,
                "engine",
                f"learn.optin.{point.name} ignored: set it in ~/.token-sparen/config.toml (not in "
                "a project file, a session override or a TOKEN_SPAREN_* variable)",
            )
        self._optin[point.name] = ok
        return ok

    def _user_optin(self, name: str) -> bool:
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
        return held(self.cfg.data, point.name)

    def _candidates(self, point: DecisionPoint, features: Mapping[str, str]) -> list[Cand]:
        return self._arms(point, features)[0]

    def _arms(
        self, point: DecisionPoint, features: Mapping[str, str]
    ) -> tuple[list[Cand], list[str]]:
        """``(candidates, explore)``: the arms competing in the Thompson draw and, for a
        ``natural_gate`` point, the ladder arms its floor branch explores (uniformly). A gated arm
        competes only when ``lcb_0.9`` of its savings posterior (its outcome rate when it
        intervened) exceeds the natural-rate bar of its bucket (``StoreChain.natural_bar``:
        ``ucb_0.9``, never looser than the pooled one while the bucket is sparse); the draw then
        applies the usual regret constraint (sampled regret <= tau)."""
        if self.held(point) or (point.tier == "high" and not self.opted_in(point)):
            return [], []
        tau = self._tau(point)
        posts = self.posteriors(point, features)
        ladder = self._ladder(point, features, posts)
        gated = point.natural_gate
        bar = (
            self.stores.natural_bar(point.name, self.bucket(point, features), GATE_Q)
            if gated
            else 0.0
        )
        cands: list[Cand] = []
        for arm in ladder:
            s, r = posts[arm]
            if point.tier == "high" and r.ucb(HIGH_UCB_Q) > tau:
                continue  # high tier: never explore without the regret upper bound clearing
            if gated and not s.lcb(1.0 - GATE_Q) > bar:
                continue  # no better than leaving the loop alone (yet)
            cands.append((arm, s.a, s.b, r.a, r.b))
        return cands, (list(ladder) if point.natural_gate else [])

    def _propensity(
        self,
        point: DecisionPoint,
        cands: list[Cand],
        arm: str,
        explore: list[str] | None = None,
    ) -> float:
        """Exact probability that ``decide`` picks ``arm`` (floor mixed with the sampling branch;
        see ``learn.propensity``); 0 for an ineligible arm. For a ``natural_gate`` point the floor
        branch picks uniformly among ``explore`` (the safe arm if there is none). ``decide`` logs
        it clamped to [1e-3, 1]."""
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
        if explore:
            low = floor * (arm in explore) / len(explore)
        else:
            low = floor * (arm == point.safe_arm)
        p = low + (1.0 - floor) * branch.get(arm, 0.0)
        return min(1.0, max(0.0, p))

    def propensity(self, point: DecisionPoint, features: Mapping[str, str], arm: str) -> float:
        """Probability that ``decide`` picks ``arm`` for these features (exact)."""
        cands, explore = self._arms(point, features)
        return self._propensity(point, cands, arm, explore)

    def action_probs(self, point: DecisionPoint, features: Mapping[str, str]) -> dict[str, float]:
        """Exact (unclamped) probability of every arm of ``point`` as ``decide`` would choose
        for these features: ladder-locked and filtered arms get exactly 0; a point paused by
        the circuit breaker gives the safe arm 1. Used by off-policy evaluation."""
        if self.stores.tripped(point.name):
            return {a: float(a == point.safe_arm) for a in point.arms}
        cands, explore = self._arms(point, features)
        return {a: self._propensity(point, cands, a, explore) for a in point.arms}

    def choose(self, point: DecisionPoint, features: Mapping[str, str]) -> tuple[str, float]:
        """One draw ``(arm, logged propensity)`` with no side effects (no pending entry, no
        store write): ``decide`` uses it, and offline replay uses it as a frozen snapshot."""
        self.last_explored = False
        self._last_lock = None
        if self.stores.tripped(point.name):
            return point.safe_arm, 1.0  # circuit breaker: paused for this project
        cands, explore = self._arms(point, features)
        floor = self._floor(point)
        if floor > 0.0 and self.rng.random() < floor:
            if explore:  # a gated point explores an intervening arm (its safe arm is the default)
                arm = explore[int(self.rng.random() * len(explore)) % len(explore)]
                self.last_explored = True
            else:
                arm = point.safe_arm
        else:
            arm = _draw(cands, self._tau(point), point.safe_arm, self.rng)
        p = self._propensity(point, cands, arm, explore)
        return arm, min(1.0, max(MIN_PROPENSITY, p))

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
        arm, p = self.choose(point, features)
        lock = self._last_lock
        self._points[point.name] = point
        if self._overlay is None:
            self._overlay = ctx.overlay_path
        try:
            self._remember(decision_id, point.name, bucket, arm, lock)
        except Exception as exc:  # noqa: BLE001 - the decision stands; it just cannot be rewarded
            log_error(self._env, "engine", f"pending: {type(exc).__name__}: {exc}")
        return Decision(decision_id, point.name, arm, p, bucket)

    def _remember(
        self,
        decision_id: str,
        point: str,
        bucket: str,
        arm: str,
        lock: tuple[str, str, str] | None = None,
    ) -> None:
        """Add the pending entry; with the ladder retry on (``lock`` set) also count the
        decision toward re-opening the locked arm, or restart the count after a probe."""
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
            if lock is not None:
                waits = state.get(LADDER_WAIT_KEY)
                waits = waits if isinstance(waits, dict) else {}
                row = waits.get(lock[0])
                row = row if isinstance(row, dict) else {}
                n = row.get(lock[1])
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
                row[lock[1]] = 0 if lock[2] == "reopened" else n + 1
                waits[lock[0]] = row
                state[LADDER_WAIT_KEY] = waits
                self._waits = waits  # this engine's later decisions see the new count
            return state

        jsonstore.update(pending_path(self._overlay), fn, {}, self._env)

    # --- reward -------------------------------------------------------------------------------

    def forget(self, decision_id: str) -> None:
        """Drop a pending decision that will never be observed (e.g. a loop-guard decision whose
        redirect was never delivered: it must add no evidence, not a regret 0). Never raises."""
        if self._overlay is None:
            return

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            table = doc.get(PENDING_KEY)
            if isinstance(table, dict):
                table.pop(decision_id, None)
            return doc

        try:
            side = pending_path(self._overlay)
            if decision_id in (jsonstore.read(side, {}, self._env).get(PENDING_KEY) or {}):
                jsonstore.update(side, fn, {}, self._env)
        except Exception as exc:  # noqa: BLE001 - never raise into a hook
            log_error(self._env, "engine", f"forget: {type(exc).__name__}: {exc}")

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
        side = pending_path(path)
        found = self._pending(jsonstore.read(side, {}, self._env), decision_id)
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

        fresh: dict[Kind, float] = {}
        where: list[str] = []

        def mark(doc: dict[str, Any]) -> dict[str, Any]:
            """Claim the kinds first (pending sidecar lock), so a racing or repeated reward can
            never add the same evidence twice; a crash before the evidence write loses one
            observation, never doubles it."""
            fresh.clear()
            found = self._pending(doc, decision_id)
            if found is None:
                return doc
            *at, entry = found
            done = _received(entry)
            fresh.update({k: x for k, x in values.items() if k not in done})
            if not fresh:
                return doc
            where[:] = at
            while len(entry) < 4:
                entry.append(time.time())
            del entry[4:]
            entry.append(sorted(done | set(fresh)))
            return doc

        jsonstore.update(side, mark, {}, self._env)
        if not fresh:
            return

        def fn(state: dict[str, Any]) -> dict[str, Any]:
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
