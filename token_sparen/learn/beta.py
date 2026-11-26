"""Beta posterior with fractional, discounted, capped updates (spec section 7.3).

Pure module: no I/O. A ``Beta`` is immutable; ``update`` returns a new instance.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

_MIN_PARAM = 1e-6
_TOL = 1e-10
_MAX_ITER = 60
_CF_ITER = 300
_CF_EPS = 1e-14
_TINY = 1e-300


def _check_q(q: float) -> None:
    if not (0.0 < q < 1.0):  # also rejects NaN
        raise ValueError(f"q must be in (0, 1), got {q!r}")


def _betacf(x: float, a: float, b: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, _CF_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _CF_EPS:
            break
    return h


def _log_front(x: float, a: float, b: float) -> float:
    return (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )


def _betainc(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a, b) for 0 < x < 1."""
    front = math.exp(_log_front(x, a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(x, a, b) / a
    return 1.0 - front * _betacf(1.0 - x, b, a) / b


def _beta_quantile(q: float, a: float, b: float) -> float:
    """Exact Beta(a, b) quantile: safeguarded Newton on I_x, bisection fallback."""
    lo, hi = 0.0, 1.0
    x = a / (a + b)
    for _ in range(_MAX_ITER):
        if not (lo < x < hi):
            x = 0.5 * (lo + hi)
        err = _betainc(x, a, b) - q
        if err > 0:
            hi = x
        else:
            lo = x
        if hi - lo < _TOL:
            break
        pdf = math.exp(_log_front(x, a, b)) / (x * (1.0 - x))
        nxt = x - err / pdf if pdf > 0.0 else 0.5 * (lo + hi)
        if abs(nxt - x) < _TOL and lo < nxt < hi:
            return nxt
        x = nxt
    return min(1.0, max(0.0, 0.5 * (lo + hi) if hi - lo < _TOL else x))


def _real(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"not a real number: {v!r}")
    return float(v)


@dataclass(frozen=True)
class Beta:
    """Beta(a, b) posterior.

    ``n`` is the cumulative count of real observations (not an effective sample size; the
    effective sample size is ``a + b - prior mass``).

    Update algebra: with prior mass K, prior mean pm, current mean m, and discount gamma, an
    observation x = 1 raises the mean iff ``(1 - gamma) * K * (pm - m) + (1 - m) >= 0``. With
    gamma = 1 (no discount) x = 1 never lowers the mean and x = 0 never raises it; with
    gamma < 1 a posterior far above a weak-but-heavy prior can drift down toward it. The cap
    rescale preserves the mean, so it never changes this condition.
    """

    a: float
    b: float
    n: float = 0.0

    def __post_init__(self) -> None:
        for v in (self.a, self.b, self.n):
            if not math.isfinite(v):
                raise ValueError(f"non-finite Beta parameter: {v!r}")
        if self.a <= 0 or self.b <= 0 or self.n < 0:
            raise ValueError(f"invalid Beta({self.a!r}, {self.b!r}, n={self.n!r})")

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def update(self, x: float, *, gamma: float, cap: float, prior: Beta) -> Beta:
        """Observe fractional reward ``x`` (clamped to [0, 1]; NaN raises ValueError).

        1. Discount the excess over ``prior`` by ``gamma`` (forgetting toward the prior).
        2. Add ``x`` to ``a`` and ``1 - x`` to ``b``; ``n += 1``.
        3. If ``a + b > cap``, scale the whole vector so ``a + b == cap`` exactly. The mean is
           preserved exactly, and the long-run weight of the prior is independent of ``cap``
           (do not change this to scale only the excess over the prior). Valid for any
           ``cap > 0``, including a cap below the prior's own mass.

        Requires ``0 <= gamma <= 1`` and a finite ``cap > 0``.
        """
        if math.isnan(x):
            raise ValueError("reward is NaN")
        if not (0.0 <= gamma <= 1.0):
            raise ValueError(f"gamma must be in [0, 1], got {gamma!r}")
        if not (math.isfinite(cap) and cap > 0.0):
            raise ValueError(f"cap must be finite and > 0, got {cap!r}")
        x = min(1.0, max(0.0, x))
        a = prior.a + gamma * (self.a - prior.a) + x
        b = prior.b + gamma * (self.b - prior.b) + (1.0 - x)
        total = a + b
        if total > cap:
            scale = cap / total
            a, b = a * scale, b * scale
        return Beta(max(a, _MIN_PARAM), max(b, _MIN_PARAM), self.n + 1.0)

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(max(self.a, _MIN_PARAM), max(self.b, _MIN_PARAM))

    def quantile(self, q: float) -> float:
        """Exact Beta quantile (inverse regularized incomplete beta), ``q`` in (0, 1)."""
        _check_q(q)
        return _beta_quantile(q, max(self.a, _MIN_PARAM), max(self.b, _MIN_PARAM))

    def lcb(self, q: float = 0.05) -> float:
        """Lower confidence bound: ``quantile(q)``; ``q`` must be < 0.5."""
        _check_q(q)
        if q >= 0.5:
            raise ValueError(f"lcb needs q < 0.5, got {q!r}")
        return self.quantile(q)

    def ucb(self, q: float = 0.95) -> float:
        """Upper confidence bound: ``quantile(q)``; ``q`` must be > 0.5."""
        _check_q(q)
        if q <= 0.5:
            raise ValueError(f"ucb needs q > 0.5, got {q!r}")
        return self.quantile(q)

    def to_list(self) -> list[float]:
        return [self.a, self.b, self.n]

    @classmethod
    def from_list(cls, seq: Sequence[float]) -> Beta:
        """Inverse of ``to_list``; any malformed input raises ValueError.

        Rejects str/bytes, bools and non-real numbers.
        """
        if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence) or len(seq) != 3:
            raise ValueError(f"not a [a, b, n] triple: {seq!r}")
        return cls(_real(seq[0]), _real(seq[1]), _real(seq[2]))


@dataclass(frozen=True)
class Evidence:
    """Discounted, capped observation counts of one (point, bucket, arm, kind), without a prior.

    Stored instead of a posterior so the prior stays swappable at decision time and data-only
    means are available (an estimator over stored posteriors would see its own priors). With no
    cap binding, ``observe`` then ``posterior(prior)`` equals ``Beta.update`` with that prior
    (discount toward the prior == discount of the evidence). The cap here bounds the evidence
    mass only, mean-preserving.
    """

    ea: float
    eb: float
    n: float = 0.0

    def __post_init__(self) -> None:
        for v in (self.ea, self.eb, self.n):
            if not math.isfinite(v):
                raise ValueError(f"non-finite Evidence value: {v!r}")
        if self.ea < 0 or self.eb < 0 or self.n < 0:
            raise ValueError(f"invalid Evidence({self.ea!r}, {self.eb!r}, n={self.n!r})")

    @property
    def mean_data(self) -> float | None:
        """Data-only mean ``ea / (ea + eb)``; None without evidence mass."""
        total = self.ea + self.eb
        return None if total <= 0.0 else self.ea / total

    def observe(self, x: float, *, gamma: float, cap: float) -> Evidence:
        """Discount by ``gamma``, add ``x`` (clamped to [0, 1]) and ``1 - x``, ``n += 1``; if
        ``ea + eb > cap`` scale both so the sum is ``cap`` (mean preserved)."""
        if math.isnan(x):
            raise ValueError("reward is NaN")
        if not (0.0 <= gamma <= 1.0):
            raise ValueError(f"gamma must be in [0, 1], got {gamma!r}")
        if not (math.isfinite(cap) and cap > 0.0):
            raise ValueError(f"cap must be finite and > 0, got {cap!r}")
        x = min(1.0, max(0.0, x))
        ea = gamma * self.ea + x
        eb = gamma * self.eb + (1.0 - x)
        total = ea + eb
        if total > cap:
            scale = cap / total
            ea, eb = ea * scale, eb * scale
        return Evidence(ea, eb, self.n + 1.0)

    def posterior(self, prior: Beta) -> Beta:
        return Beta(prior.a + self.ea, prior.b + self.eb, self.n)

    def to_list(self) -> list[float]:
        return [self.ea, self.eb, self.n]

    @classmethod
    def from_list(cls, seq: Sequence[float]) -> Evidence:
        """Inverse of ``to_list``; any malformed input raises ValueError (same rules as Beta)."""
        if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence) or len(seq) != 3:
            raise ValueError(f"not a [ea, eb, n] triple: {seq!r}")
        return cls(_real(seq[0]), _real(seq[1]), _real(seq[2]))
