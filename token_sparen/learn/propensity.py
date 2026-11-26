"""Exact win probabilities of the constrained Thompson draw (spec section 7.6, 7.8).

For trimming arms i with savings posterior ``S_i`` and feasibility probability
``P_i = P(R~_i <= tau)`` (regret drawn independently), the draw picks arm i with probability

    p_i = P_i * integral_0^1 f_{S_i}(s) * prod_{j != i} [1 - P_j + P_j F_{S_j}(s)] ds

and the safe arm (S == 0, always feasible) with ``prod_j (1 - P_j)`` (``S~ > 0`` almost surely).
The integral is taken in logit space, ``s = sigmoid(y)``, where every Beta density is smooth and
log-concave with exponential tails (no endpoint singularities, also for a or b < 1); the
trapezoid rule on a uniform grid is then spectrally accurate. Per arm i the grid covers its own
window (mode +- 6 sd + 20/a resp. 20/b) with step
``min(0.7, 0.7 * sd)`` over arm i and every arm whose window overlaps it, so narrow neighbours'
CDF steps are resolved. Mass outside a window clipped to +-700 is added as a tail term. Other
arms' CDFs are evaluated only inside their own windows (0 or 1 outside).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from token_sparen.learn.beta import Beta, beta_cdf

_LIMIT = 700.0
_TAIL = 20.0  # ~ln(5e8): tail mass beyond a window <~ 1e-9
_SDS = 6.0
_MAX_STEP = 0.7  # the logit-space density has poles at +-i*pi: error ~ exp(-2 pi^2 / h)
_STEP_PER_SD = 0.7


def _trigamma(x: float) -> float:
    acc = 0.0
    while x < 6.0:
        acc += 1.0 / (x * x)
        x += 1.0
    inv = 1.0 / x
    inv2 = inv * inv
    return acc + inv + inv2 / 2.0 + inv2 * inv * (1.0 / 6.0 - inv2 * (1.0 / 30.0 - inv2 / 42.0))


def _log_sigmoid(y: float) -> float:
    return -(max(0.0, -y) + math.log1p(math.exp(-abs(y))))


class _Arm:
    __slots__ = ("a", "b", "p", "lbeta", "lo", "hi", "sd")

    def __init__(self, s: Beta, p: float) -> None:
        self.a, self.b, self.p = s.a, s.b, p
        self.lbeta = math.lgamma(s.a) + math.lgamma(s.b) - math.lgamma(s.a + s.b)
        mode = math.log(s.a / s.b)
        self.sd = math.sqrt(_trigamma(s.a) + _trigamma(s.b))
        self.lo = max(-_LIMIT, mode - _SDS * self.sd - _TAIL / s.a)
        self.hi = min(_LIMIT, mode + _SDS * self.sd + _TAIL / s.b)

    def density(self, y: float) -> float:
        """Density of logit(S) at y."""
        return math.exp(self.a * _log_sigmoid(y) + self.b * _log_sigmoid(-y) - self.lbeta)

    def cdf(self, y: float) -> float:
        if y <= self.lo and self.lo > -_LIMIT:
            return 0.0
        if y >= self.hi and self.hi < _LIMIT:
            return 1.0
        return beta_cdf(math.exp(_log_sigmoid(y)), self.a, self.b, xc=math.exp(_log_sigmoid(-y)))

    def g(self, y: float) -> float:
        """This arm's factor in another arm's product: P(it does not beat s = sigmoid(y))."""
        return 1.0 - self.p + self.p * self.cdf(y)


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def win_probabilities(arms: Sequence[tuple[Beta, float]]) -> tuple[float, list[float]]:
    """``(p_safe, [p_i])`` for trimming arms given as ``(S posterior, P(R~ <= tau))``."""
    safe = 1.0
    for _, p in arms:
        safe *= 1.0 - _clamp01(p)
    live = [_Arm(s, _clamp01(p)) if p > 0.0 else None for s, p in arms]
    out: list[float] = []
    for i, arm in enumerate(live):
        if arm is None:
            out.append(0.0)
            continue
        others = [o for j, o in enumerate(live) if j != i and o is not None]
        near = [o for o in others if o.hi > arm.lo and o.lo < arm.hi]
        step = min(_MAX_STEP, _STEP_PER_SD * min([arm.sd, *(o.sd for o in near)]))
        k = max(8, math.ceil((arm.hi - arm.lo) / step))
        h = (arm.hi - arm.lo) / k
        total = 0.0
        for n in range(k + 1):
            y = arm.lo + n * h
            w = h * (0.5 if n in (0, k) else 1.0)
            d = arm.density(y)
            if d * w < 1e-18:
                continue
            prod = d * w
            for o in near:
                prod *= o.g(y)
            total += prod
        far = 1.0
        for o in others:
            if o not in near:  # entirely below (factor 1 - P + P = 1) or above (1 - P) arm i
                far *= 1.0 if o.hi <= arm.lo else 1.0 - o.p
        if arm.lo <= -_LIMIT:  # tail mass below the clipped window
            tail = math.exp(_log_sigmoid(arm.lo))
            lo_mass = beta_cdf(tail, arm.a, arm.b, xc=math.exp(_log_sigmoid(-arm.lo)))
            gl = 1.0
            for o in near:
                gl *= o.g(arm.lo)
            total += lo_mass * gl
        if arm.hi >= _LIMIT:
            hi_mass = 1.0 - beta_cdf(
                math.exp(_log_sigmoid(arm.hi)), arm.a, arm.b, xc=math.exp(_log_sigmoid(-arm.hi))
            )
            gh = 1.0
            for o in near:
                gh *= o.g(arm.hi)
            total += hi_mass * gh
        out.append(_clamp01(arm.p * far * total))
    return _clamp01(safe), out
