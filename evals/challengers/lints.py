"""Constrained linear Thompson sampling (LinTS), an offline challenger to the shipped engine.

Offline only: evaluated through the ``sim`` replay lane (``lints_sim``); never wired to hooks
(LinTS in hooks is deferred until after v1.0, and only with E3 evidence). ``token_sparen``
never imports this package.

Per trimming arm two Bayesian linear models, savings ``S`` and regret ``R``, over a context
vector ``x``: prior ``theta ~ N(0, v^2 / lam * I)``, so after observations ``(x_i, y_i)`` the
posterior is ``N(B^-1 f, v^2 B^-1)`` with ``B = lam * I + sum x_i x_i^T`` and
``f = sum y_i x_i``. ``B^-1`` is kept directly and updated in O(d^2) per observation with the
Sherman-Morrison identity. A draw samples ``x . theta`` for each model, which is exactly
``N(x . mean, v^2 * x . B^-1 x)`` (no d x d factorisation needed).

Selection follows the engine (``learn.engine``): an arm is feasible iff its sampled regret is at
most ``tau`` (unchecked when ``tau >= 1``); the safe arm is always feasible with ``S = R = 0``
and is never updated; the feasible arm with the largest sampled savings wins, ties going to the
safe arm, then declared order. Regret is sampled first, savings only for a feasible arm.

``x`` (``encode``) is the one-hot concatenation of the decision point's features
(``learn.features.FEATURE_KEYS["compression"]``): each feature has a fixed vocabulary whose
fallback slot also takes unknown values, ``DIM = 32``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence

MAX_DIM = 32
KINDS = ("savings", "regret")
# feature -> (values, fallback value for anything else); one slot per value
VOCAB: dict[str, tuple[tuple[str, ...], str]] = {
    "band": (("unk", "lt50", "50_75", "75_95", "gt95"), "unk"),
    "cat": (("shell", "read", "search", "web", "edit", "agent", "mcp", "other"), "other"),
    "size": (("sm", "md", "lg"), "md"),
    "ctype": (
        (
            "json",
            "diff",
            "stacktrace",
            "test_report",
            "compiler",
            "build_log",
            "search",
            "pplx",
            "log",
            "table",
            "text",
            "unk",
        ),
        "unk",
    ),
    "phase": (("debug", "build", "explore", "mixed"), "mixed"),
}
DIM = sum(len(values) for values, _ in VOCAB.values())
assert DIM <= MAX_DIM


def encode(features: Mapping[str, str]) -> list[float]:
    """The one-hot context vector of compression-point features (length ``DIM``)."""
    x: list[float] = []
    for key, (values, fallback) in VOCAB.items():
        value = features.get(key, fallback)
        hot = values.index(value if value in values else fallback)
        x.extend(float(i == hot) for i in range(len(values)))
    return x


class _Linear:
    """One Bayesian linear model: ``B^-1`` and ``f`` (posterior mean ``B^-1 f``)."""

    def __init__(self, dim: int, lam: float) -> None:
        self.binv = [[(1.0 / lam if i == j else 0.0) for j in range(dim)] for i in range(dim)]
        self.f = [0.0] * dim

    def _bx(self, x: Sequence[float]) -> list[float]:
        return [sum(r * xi for r, xi in zip(row, x, strict=True)) for row in self.binv]

    def update(self, x: Sequence[float], y: float) -> None:
        bx = self._bx(x)  # B^-1 is symmetric, so x^T B^-1 = (B^-1 x)^T
        denom = 1.0 + sum(a * b for a, b in zip(x, bx, strict=True))
        for i, bi in enumerate(bx):
            if bi:
                row = self.binv[i]
                scale = bi / denom
                for j, bj in enumerate(bx):
                    row[j] -= scale * bj
        for i, xi in enumerate(x):
            self.f[i] += y * xi

    def mean(self) -> list[float]:
        return self._bx(self.f)

    def sample(self, x: Sequence[float], v: float, rng: random.Random) -> float:
        bx = self._bx(x)
        var = v * v * sum(a * b for a, b in zip(x, bx, strict=True))
        mu = sum(a * b for a, b in zip(x, self._bx(self.f), strict=True))
        return rng.gauss(mu, math.sqrt(max(0.0, var)))


class LinTS:
    """Two linear posteriors per trimming arm with the engine's regret constraint."""

    def __init__(
        self,
        arms: Sequence[str],
        safe_arm: str,
        tau: float,
        dim: int,
        lam: float = 1.0,
        v: float = 0.5,
    ) -> None:
        if safe_arm not in arms or len(set(arms)) != len(arms):
            raise ValueError("arms must be distinct and include the safe arm")
        if not 1 <= dim <= MAX_DIM:
            raise ValueError(f"dim must be in [1, {MAX_DIM}]")
        if not lam > 0 or not v >= 0 or not 0.0 <= tau <= 1.0:
            raise ValueError("need lam > 0, v >= 0 and tau in [0, 1]")
        self.arms, self.safe_arm, self.tau, self.dim, self.lam, self.v = (
            tuple(arms),
            safe_arm,
            tau,
            dim,
            lam,
            v,
        )
        self._models = {
            (arm, kind): _Linear(dim, lam) for arm in arms if arm != safe_arm for kind in KINDS
        }

    def _check(self, x: Sequence[float]) -> None:
        if len(x) != self.dim:
            raise ValueError(f"x must have {self.dim} entries")

    def choose(self, x: Sequence[float], rng: random.Random) -> str:
        """One constrained draw (see the module docstring)."""
        self._check(x)
        best, best_s = self.safe_arm, 0.0
        for arm in self.arms:
            if arm == self.safe_arm:
                continue
            if self.tau < 1.0 and self._models[arm, "regret"].sample(x, self.v, rng) > self.tau:
                continue
            s = self._models[arm, "savings"].sample(x, self.v, rng)
            if s > best_s:
                best, best_s = arm, s
        return best

    def update(self, arm: str, x: Sequence[float], savings: float, regret: float) -> None:
        """One observation of a trimming arm (the safe arm has none by definition)."""
        if (arm, "savings") not in self._models:
            raise ValueError(f"{arm!r} is not a trimming arm")
        self._check(x)
        self._models[arm, "savings"].update(x, float(savings))
        self._models[arm, "regret"].update(x, float(regret))

    def posterior(self, arm: str, kind: str) -> tuple[list[float], list[list[float]]]:
        """``(mean, covariance)`` of the ``kind`` model of ``arm``."""
        model = self._models[arm, kind]
        cov = [[self.v * self.v * c for c in row] for row in model.binv]
        return model.mean(), cov
