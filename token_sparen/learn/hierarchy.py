"""Empirical-Bayes hierarchical priors over the evidence stores (spec section 7.4).

The stores hold **evidence**, never posteriors (a stored posterior would fold its prior in for
good, and an estimator over stored posteriors would see its own priors). ``StoreChain`` is a
read-only, per-instance cached view of three stores with the inner shape
``{point: {bucket: {arm: {"savings": [ea, eb, n], "regret": [ea, eb, n]}}}}``:

- ``projects/<key>/posteriors.json`` and ``global/posteriors.json``:
  ``{"version": 2, "points": <inner>}`` (version 1 held posteriors: read as empty, logged);
- the session overlay (``projects/<key>/sessions/<id>.json``) under the key ``posteriors``.

``prior_for`` walks the back-off levels (most specific to ``*``) in project scope, then the same
in global scope. The first parent with ``n >= min_parent_obs`` real observations lends the mean
``m = (ea + kappa0 * d) / (ea + eb + kappa0)`` (its data shrunk toward the designed default ``d``
with ``kappa0 = kappa_default`` pseudo-counts; no recursion up the chain). The prior is
``Beta(m * kappa, (1 - m) * kappa, n=0)`` with the point's estimated ``kappa``. Without a
qualifying parent the designed default ``[learn.defaults.<point>.<arm>]`` (hand-set; to be refit
in M4) is used with strength ``kappa_default``. The session scope is not part of the chain: the
session evidence is the child this prior is for.

A mean of exactly 0 (the safe arm's designed default) is represented as ``Beta(EPS, kappa)``
because a Beta needs ``a > 0``; the engine treats safe-arm savings and regret as 0 by definition.
Unknown (undeclared extension) arms default to savings 0, regret 0.5: deliberately conservative.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from token_sparen.config import log_error
from token_sparen.learn.beta import Beta, Evidence
from token_sparen.learn.features import (
    FEATURE_KEYS,
    backoff_keys,
    backoff_levels,
    lookup_param,
)
from token_sparen.learn.policy import DecisionPoint
from token_sparen.store import jsonstore, session
from token_sparen.store.paths import project_dir_for

Kind = Literal["savings", "regret"]
Scope = Literal["session", "project", "global"]
SCHEMA_VERSION = 2
POSTERIORS_FILE = "posteriors.json"
SESSION_KEY = "posteriors"
EPS = 1e-3
DEFAULT_KAPPA = 6.0
_SCOPES: tuple[Scope, ...] = ("session", "project", "global")
_CHAIN: tuple[Scope, ...] = ("project", "global")
_FALLBACK_MEAN: dict[str, float] = {"savings": 0.0, "regret": 0.5}


def _num(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return default
    return float(value)


class StoreChain:
    """Read-only view over the session overlay, project and global evidence stores.

    ``params`` is the merged config dict (``Config.data``); a missing ``learn.*`` key falls back
    to the packaged default. Each file is read at most once per instance; corrupt entries read
    as ``None`` and are logged once per instance.
    """

    def __init__(
        self,
        project_key: str,
        home: Path,
        session_id: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self.project_key = project_key
        self.home = home
        self.session_id = session_id
        self.params: Mapping[str, Any] | None = params
        self._env = {"TOKEN_SPAREN_HOME": str(home)}
        self._docs: dict[Scope, dict[str, Any]] = {}
        self.kappa_cache: dict[tuple[str, str, str], float] = {}
        self._warned = False

    def learn_param(self, name: str, default: float) -> float:
        return _num(lookup_param(self.params, ("learn", name), _valid_num), default)

    def _warn(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            log_error(self._env, "hierarchy", message)

    def _load(self, scope: Scope) -> dict[str, Any]:
        if scope == "session":
            if self.session_id is None:
                return {}
            path = session.session_path(self.home, self.project_key, self.session_id)
            inner = jsonstore.read(path, {}, self._env).get(SESSION_KEY)
        else:
            base = (
                project_dir_for(self.home, self.project_key)
                if scope == "project"
                else self.home / "global"
            )
            doc = jsonstore.read(base / POSTERIORS_FILE, {}, self._env)
            if not doc:
                return {}
            if doc.get("version") != SCHEMA_VERSION:
                self._warn(f"{scope} evidence: unsupported version {doc.get('version')!r}")
                return {}
            inner = doc.get("points")
        if inner is None:
            return {}
        if not isinstance(inner, dict):
            self._warn(f"{scope} evidence: points is not an object")
            return {}
        return inner

    def points(self, scope: Scope) -> dict[str, Any]:
        """The scope's ``{point: {bucket: {arm: ...}}}`` mapping (cached; may be malformed)."""
        if scope not in _SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        doc = self._docs.get(scope)
        if doc is None:
            doc = self._docs[scope] = self._load(scope)
        return doc

    def get(self, point: str, bucket: str, arm: str, kind: Kind, scope: Scope) -> Evidence | None:
        node: Any = self.points(scope).get(point)
        for key in (bucket, arm):
            if not isinstance(node, dict):
                if node is not None:
                    self._warn(f"{scope} evidence: malformed entry under {point!r}")
                return None
            node = node.get(key)
        if node is None:
            return None
        if not isinstance(node, dict):
            self._warn(f"{scope} evidence: malformed {point}/{bucket}/{arm}")
            return None
        raw = node.get(kind)
        if raw is None:
            return None
        try:
            return Evidence.from_list(raw)
        except ValueError:
            self._warn(f"{scope} evidence: corrupt {point}/{bucket}/{arm}/{kind}")
            return None


def _valid_num(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def kappa(point: DecisionPoint, arm: str, kind: Kind, stores: StoreChain) -> float:
    """Prior strength by method of moments over sibling buckets (cached per StoreChain).

    Siblings: project-scope buckets of the point's most specific level with the same arm and
    kind and ``n >= min_parent_obs``. On their data-only means m_i (mass w_i = ea_i + eb_i):
    ``var_between = var(m_i) - mean(m_i (1 - m_i) / (w_i + 1))`` (sampling noise removed);
    fewer than ``kappa_min_siblings`` -> ``kappa_default``; ``var_between <= 0`` ->
    ``kappa_max`` (indistinguishable buckets pool strongly); else
    ``mean(m_i) (1 - mean(m_i)) / var_between - 1`` clamped to [kappa_min, kappa_max].
    """
    cache_key = (point.name, arm, kind)
    cached = stores.kappa_cache.get(cache_key)
    if cached is not None:
        return cached
    default = stores.learn_param("kappa_default", DEFAULT_KAPPA)
    lo = stores.learn_param("kappa_min", 2.0)
    hi = stores.learn_param("kappa_max", 20.0)
    min_obs = stores.learn_param("min_parent_obs", 5.0)
    min_sib = stores.learn_param("kappa_min_siblings", 3.0)
    full = backoff_levels(point.name, stores.params)[0] or list(FEATURE_KEYS.get(point.name, ()))
    prefixes = tuple(f"{k}=" for k in full)
    buckets = stores.points("project").get(point.name)
    means: list[float] = []
    noise: list[float] = []
    if isinstance(buckets, dict) and prefixes:
        for bucket in buckets:
            parts = bucket.split("|") if isinstance(bucket, str) else []
            if len(parts) != len(prefixes) or not all(
                p.startswith(pre) for p, pre in zip(parts, prefixes, strict=True)
            ):
                continue
            ev = stores.get(point.name, bucket, arm, kind, "project")
            m = None if ev is None or ev.n < min_obs else ev.mean_data
            if ev is not None and m is not None:
                means.append(m)
                noise.append(m * (1.0 - m) / (ev.ea + ev.eb + 1.0))
    value = default
    if len(means) >= max(min_sib, 2):
        m_bar = statistics.fmean(means)
        between = statistics.pvariance(means, m_bar) - statistics.fmean(noise)
        value = hi if between <= 0.0 else m_bar * (1.0 - m_bar) / between - 1.0
        value = min(hi, max(lo, value))
    stores.kappa_cache[cache_key] = value
    return value


def designed_default(
    point: str, arm: str, kind: Kind, params: Mapping[str, Any] | None = None
) -> float:
    """``[learn.defaults.<point>.<arm>] <kind>`` clamped to [0, 1]; unknown -> fallback mean."""
    raw = lookup_param(params, ("learn", "defaults", point, arm, kind), _valid_num)
    return min(1.0, max(0.0, _num(raw, _FALLBACK_MEAN[kind])))


def _from_mean(m: float, k: float) -> Beta:
    return Beta(max(m * k, EPS), max((1.0 - m) * k, EPS), 0.0)


def prior_for(
    point: DecisionPoint,
    features: Mapping[str, str],
    arm: str,
    kind: Kind,
    stores: StoreChain,
) -> Beta:
    """Prior for (point, features, arm, kind): project levels, global levels, designed default."""
    d = designed_default(point.name, arm, kind, stores.params)
    k0 = max(EPS, stores.learn_param("kappa_default", DEFAULT_KAPPA))
    min_obs = stores.learn_param("min_parent_obs", 5.0)
    for scope in _CHAIN:
        for bucket in backoff_keys(point.name, features, stores.params):
            ev = stores.get(point.name, bucket, arm, kind, scope)
            if ev is not None and ev.n >= min_obs:
                m = (ev.ea + k0 * d) / (ev.ea + ev.eb + k0)
                return _from_mean(m, kappa(point, arm, kind, stores))
    return _from_mean(d, k0)
