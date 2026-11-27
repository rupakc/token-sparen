"""Beta posterior with fractional, discounted, capped updates (spec section 7.3).

Pure module: no I/O. A ``Beta`` is immutable; ``update`` returns a new instance.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

_MIN_PARAM = 1e-6
_MAX_ITER = 100
_UTOL = 1e-15  # relative tolerance in x (absolute in log x)
_U_UNDERFLOW = math.log(5e-324)  # below the smallest positive float
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


def beta_cdf(x: float, a: float, b: float, *, xc: float | None = None) -> float:
    """Beta(a, b) CDF at ``x``; ``xc`` is ``1 - x`` computed precisely by the caller (keeps the
    upper tail exact when ``x`` is within rounding of 1)."""
    c = 1.0 - x if xc is None else xc
    if x <= 0.0:
        return 0.0
    if c <= 0.0:
        return 1.0
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(_log_front2(x, c, a, b)) * _betacf(x, a, b) / a
    return 1.0 - math.exp(_log_front2(c, x, b, a)) * _betacf(c, b, a) / b


def _log_front2(x: float, c: float, a: float, b: float) -> float:
    """Log of ``x^a (1 - x)^b / B(a, b)`` with the complement ``c = 1 - x`` given."""
    return math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(c)


def _lower_logcdf(u: float, a: float, b: float, log_beta: float) -> tuple[float, float]:
    """``log I_x(a, b)`` at ``x = exp(u)`` below the continued fraction's switch point, and its
    slope in ``u``. Exact in log space: no underflow however small ``x`` (or ``exp(u)``) is."""
    c = -math.expm1(u)  # 1 - x, precise
    cf = max(_betacf(math.exp(u), a, b), _TINY)
    log_cdf = a * u + b * math.log(c) - log_beta + math.log(cf) - math.log(a)
    return log_cdf, a / (cf * c)  # d/du: x * pdf / I = a / (cf * (1 - x))


def _lower_quantile(p: float, a: float, b: float) -> float:
    """Root ``x`` of ``I_x(a, b) = p`` known to lie at or below the switch point
    ``(a + 1) / (a + b + 2)``, solved for ``u = log x`` (relative tolerance in ``x``).

    Safeguarded Newton on ``log I`` (nearly linear in ``u`` for small ``x``, slope ~ ``a``),
    started at the small-x asymptote ``I ~ x^a / (a B(a, b))``; every evaluation narrows a
    bracket, a step leaving it bisects (or, with no lower end yet, steps geometrically down).
    A root below the smallest positive float returns 0.0."""
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    target = math.log(p)
    lo, hi = -math.inf, math.log((a + 1.0) / (a + b + 2.0))
    u = min(hi, (target + math.log(a) + log_beta) / a)
    for _ in range(_MAX_ITER):
        log_cdf, slope = _lower_logcdf(u, a, b, log_beta)
        err = log_cdf - target
        if err == 0.0:
            return math.exp(u)
        if err > 0.0:
            hi = u
        else:
            lo = u
        if hi < _U_UNDERFLOW:
            return 0.0
        if hi - lo <= _UTOL * max(1.0, abs(hi)):
            break
        nxt = u - err / slope
        if not (lo < nxt < hi):
            nxt = 0.5 * (lo + hi) if lo > -math.inf else hi - 2.0 * max(1.0, abs(hi))
        if abs(nxt - u) <= _UTOL * max(1.0, abs(u)):
            return math.exp(nxt)
        u = nxt
    return math.exp(0.5 * (lo + hi) if lo > -math.inf else hi)


def _beta_quantile(q: float, a: float, b: float) -> float:
    """Exact Beta(a, b) quantile, monotone in ``q`` and in [0, 1].

    The root is on the lower side of the switch point ``x0 = (a + 1) / (a + b + 2)`` iff
    ``I_x0(a, b) >= q``; there it is solved in log space. Otherwise, by ``I_x(a, b) =
    1 - I_{1-x}(b, a)``, ``1 - x`` is the lower-side root of the mirrored problem (``x0``
    mirrors to the mirrored switch point), so both tails keep relative precision."""
    x0 = (a + 1.0) / (a + b + 2.0)
    if beta_cdf(x0, a, b, xc=(b + 1.0) / (a + b + 2.0)) >= q:
        return min(1.0, max(0.0, _lower_quantile(q, a, b)))
    return min(1.0, max(0.0, 1.0 - _lower_quantile(1.0 - q, b, a)))


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

    ``es2`` is the discounted sum of squared rewards (scaled with the cap like ``ea``/``eb``), so
    the within-bucket reward variance is available for fractional rewards; omitted (``None``,
    or a 3-element list on disk) it is taken as ``ea``, the Bernoulli case (x in {0, 1}).
    """

    ea: float
    eb: float
    n: float = 0.0
    es2: float | None = None

    def __post_init__(self) -> None:
        if self.es2 is None:
            object.__setattr__(self, "es2", self.ea)
        for v in (self.ea, self.eb, self.n, self.second):
            if not math.isfinite(v):
                raise ValueError(f"non-finite Evidence value: {v!r}")
        if self.ea < 0 or self.eb < 0 or self.n < 0 or self.second < 0:
            raise ValueError(f"invalid Evidence({self.ea!r}, {self.eb!r}, n={self.n!r})")
        if self.second > self.ea * (1.0 + 1e-9) + 1e-12:  # x^2 <= x on [0, 1]
            raise ValueError(f"Evidence es2={self.second!r} exceeds ea={self.ea!r}")

    @property
    def second(self) -> float:
        """``es2`` as a float (always set after construction)."""
        return self.ea if self.es2 is None else self.es2

    @property
    def mean_data(self) -> float | None:
        """Data-only mean ``ea / (ea + eb)``; None without evidence mass."""
        total = self.ea + self.eb
        return None if total <= 0.0 else self.ea / total

    @property
    def var_within(self) -> float | None:
        """Within-bucket reward variance ``es2/w - (ea/w)^2`` (w = ea + eb), >= 0; None at w = 0.

        Equals ``m (1 - m)`` for Bernoulli rewards.
        """
        total = self.ea + self.eb
        if total <= 0.0:
            return None
        m = self.ea / total
        return max(0.0, self.second / total - m * m)

    def observe(self, x: float, *, gamma: float, cap: float) -> Evidence:
        """Discount by ``gamma``, add ``x`` (clamped to [0, 1]) and ``1 - x`` (and ``x^2`` to
        ``es2``), ``n += 1``; if ``ea + eb > cap`` scale all three so ``ea + eb == cap``."""
        if math.isnan(x):
            raise ValueError("reward is NaN")
        if not (0.0 <= gamma <= 1.0):
            raise ValueError(f"gamma must be in [0, 1], got {gamma!r}")
        if not (math.isfinite(cap) and cap > 0.0):
            raise ValueError(f"cap must be finite and > 0, got {cap!r}")
        x = min(1.0, max(0.0, x))
        ea = gamma * self.ea + x
        eb = gamma * self.eb + (1.0 - x)
        es2 = min(ea, gamma * self.second + x * x)
        total = ea + eb
        if total > cap:
            scale = cap / total
            ea, eb, es2 = ea * scale, eb * scale, es2 * scale
        return Evidence(ea, eb, self.n + 1.0, min(ea, es2))

    def __add__(self, other: Evidence) -> Evidence:
        """Pooled evidence of two scopes (e.g. project + session): every field summed; the cap is
        not applied (each operand was capped when observed)."""
        if not isinstance(other, Evidence):
            return NotImplemented
        return Evidence(
            self.ea + other.ea, self.eb + other.eb, self.n + other.n, self.second + other.second
        )

    def scaled(self, factor: float) -> Evidence:
        """Every field (``ea``, ``eb``, ``n``, ``es2``) times ``factor`` (>= 0): the global
        store's weighted contribution and its age discount."""
        if not (math.isfinite(factor) and factor >= 0.0):
            raise ValueError(f"factor must be finite and >= 0, got {factor!r}")
        ea = self.ea * factor
        return Evidence(ea, self.eb * factor, self.n * factor, min(ea, self.second * factor))

    def capped(self, cap: float) -> Evidence:
        """The cap rule of ``observe``: if ``ea + eb > cap`` scale ``ea``, ``eb`` and ``es2`` so
        ``ea + eb == cap`` (mean-preserving); ``n`` (real observations) is unchanged."""
        if not (math.isfinite(cap) and cap > 0.0):
            raise ValueError(f"cap must be finite and > 0, got {cap!r}")
        total = self.ea + self.eb
        if total <= cap:
            return self
        f = cap / total
        ea = self.ea * f
        return Evidence(ea, self.eb * f, self.n, min(ea, self.second * f))

    def posterior(self, prior: Beta) -> Beta:
        return Beta(prior.a + self.ea, prior.b + self.eb, self.n)

    def to_list(self) -> list[float]:
        return [self.ea, self.eb, self.n, self.second]

    @classmethod
    def from_list(cls, seq: Sequence[float]) -> Evidence:
        """Inverse of ``to_list`` (``[ea, eb, n, es2]``; ``[ea, eb, n]`` means es2 = ea); any
        malformed input raises ValueError (same rules as Beta)."""
        if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence) or len(seq) not in (3, 4):
            raise ValueError(f"not an [ea, eb, n(, es2)] list: {seq!r}")
        es2 = _real(seq[3]) if len(seq) == 4 else None
        return cls(_real(seq[0]), _real(seq[1]), _real(seq[2]), es2)
