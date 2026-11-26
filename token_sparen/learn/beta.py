"""Beta posterior with fractional, discounted, capped updates (spec section 7.3).

Pure module: no I/O. A ``Beta`` is immutable; ``update`` returns a new instance.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

_MIN_PARAM = 1e-6
_NORMAL = NormalDist()


def _check_q(q: float) -> None:
    if not (0.0 < q < 1.0):  # also rejects NaN
        raise ValueError(f"q must be in (0, 1), got {q!r}")


@dataclass(frozen=True)
class Beta:
    """Beta(a, b) with ``n`` real observations seen (not pseudo-counts)."""

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

    def update(self, x: float, gamma: float, cap: float, prior: Beta) -> Beta:
        """Observe fractional reward ``x`` (clamped to [0, 1]; NaN raises ValueError).

        1. Discount the excess over ``prior`` by ``gamma`` (forgetting toward the prior).
        2. Add ``x`` to ``a`` and ``1 - x`` to ``b``; ``n += 1``.
        3. If ``a + b > cap``, rescale so ``a + b == cap`` exactly. The scale is applied to the
           whole vector, so the posterior mean is preserved exactly (the excess over the prior
           absorbs the adjustment). If the prior alone has ``a + b >= cap`` the posterior is
           pinned to the prior (only ``n`` grows).
        """
        if math.isnan(x):
            raise ValueError("reward is NaN")
        x = min(1.0, max(0.0, x))
        a = prior.a + gamma * (self.a - prior.a) + x
        b = prior.b + gamma * (self.b - prior.b) + (1.0 - x)
        n = self.n + 1.0
        if prior.a + prior.b >= cap:
            return Beta(prior.a, prior.b, n)
        total = a + b
        if total > cap:
            scale = cap / total
            a, b = a * scale, b * scale
        return Beta(a, b, n)

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(max(self.a, _MIN_PARAM), max(self.b, _MIN_PARAM))

    def _std(self) -> float:
        s = self.a + self.b
        return math.sqrt(self.a * self.b / (s * s * (s + 1.0)))

    def lcb(self, q: float = 0.05) -> float:
        """Lower q-quantile, normal approximation, clamped to [0, 1]."""
        _check_q(q)
        return min(1.0, max(0.0, self.mean + _NORMAL.inv_cdf(q) * self._std()))

    def ucb(self, q: float = 0.95) -> float:
        """Upper q-quantile, normal approximation, clamped to [0, 1]."""
        _check_q(q)
        return min(1.0, max(0.0, self.mean + _NORMAL.inv_cdf(q) * self._std()))

    def to_list(self) -> list[float]:
        return [self.a, self.b, self.n]

    @classmethod
    def from_list(cls, seq: Sequence[float]) -> Beta:
        """Inverse of ``to_list``; any malformed input raises ValueError."""
        try:
            a, b, n = (float(v) for v in seq)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"not a [a, b, n] triple: {seq!r}") from exc
        return cls(a, b, n)
