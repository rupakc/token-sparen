from __future__ import annotations

import random

import pytest

from token_sparen.learn.beta import Beta, beta_cdf
from token_sparen.learn.propensity import win_probabilities

TAU = 0.05

# (savings posterior, regret posterior) per trimming arm, in declared order
CASES: dict[str, list[tuple[Beta, Beta]]] = {
    "cold": [(Beta(2.1, 3.9), Beta(0.4, 19.6))],
    "cold3": [
        (Beta(2.1, 3.9), Beta(0.4, 19.6)),
        (Beta(3.6, 2.4), Beta(0.8, 19.2)),
        (Beta(4.8, 1.2), Beta(1.6, 18.4)),
    ],
    "pointer_singular_at_1": [(Beta(5.4, 0.6), Beta(0.6, 19.4))],
    "warm": [(Beta(20, 40), Beta(1, 60)), (Beta(35, 25), Beta(2, 60)), (Beta(50, 10), Beta(3, 60))],
    "singular_both_ends": [
        (Beta(0.2, 1.8), Beta(0.4, 19.6)),
        (Beta(0.3, 0.25), Beta(0.4, 19.6)),
        (Beta(1.8, 0.2), Beta(0.4, 19.6)),
    ],
    "heavy_close": [
        (Beta(70, 70), Beta(0.3, 120)),
        (Beta(72, 68), Beta(0.3, 120)),
        (Beta(74, 66), Beta(0.3, 120)),
    ],
    "wide_and_narrow": [(Beta(0.7, 1.5), Beta(0.3, 30)), (Beta(80, 60), Beta(0.2, 120))],
    "rare_arm": [(Beta(60, 40), Beta(0.2, 100)), (Beta(10, 40), Beta(0.2, 100))],
}


def _ps(arms: list[tuple[Beta, Beta]]) -> list[tuple[Beta, float]]:
    return [(s, beta_cdf(TAU, r.a, r.b)) for s, r in arms]


def _empirical(arms: list[tuple[Beta, Beta]], n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    counts = [0] * (len(arms) + 1)
    for _ in range(n):
        best, best_s = 0, 0.0
        for i, (s, r) in enumerate(arms, start=1):
            if rng.betavariate(r.a, r.b) > TAU:
                continue
            x = rng.betavariate(s.a, s.b)
            if x > best_s:
                best, best_s = i, x
        counts[best] += 1
    return [c / n for c in counts]


@pytest.mark.parametrize("name", sorted(CASES))
def test_probabilities_sum_to_one(name: str) -> None:
    safe, ps = win_probabilities(_ps(CASES[name]))
    assert safe + sum(ps) == pytest.approx(1.0, abs=1e-6)
    assert all(0.0 <= p <= 1.0 for p in [safe, *ps])


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_100k_empirical_frequencies(name: str) -> None:
    safe, ps = win_probabilities(_ps(CASES[name]))
    emp = _empirical(CASES[name], 100_000, seed=11)
    for exact, freq in zip([safe, *ps], emp, strict=True):
        assert exact == pytest.approx(freq, abs=0.005)


def test_safe_probability_is_product_of_infeasibility() -> None:
    arms = CASES["cold3"]
    safe, _ = win_probabilities(_ps(arms))
    expected = 1.0
    for _, r in arms:
        expected *= 1.0 - beta_cdf(TAU, r.a, r.b)
    assert safe == pytest.approx(expected, rel=1e-12)


def test_edge_cases() -> None:
    assert win_probabilities([]) == (1.0, [])
    safe, ps = win_probabilities([(Beta(2, 2), 0.0), (Beta(3, 1), 1.0)])
    assert safe == 0.0 and ps[0] == 0.0 and ps[1] == pytest.approx(1.0, abs=1e-6)
    safe, ps = win_probabilities([(Beta(2, 2), 1.0), (Beta(2, 2), 1.0)])
    assert ps[0] == pytest.approx(0.5, abs=1e-6) and ps[1] == pytest.approx(0.5, abs=1e-6)


def test_beta_cdf() -> None:
    assert beta_cdf(0.0, 2, 3) == 0.0 and beta_cdf(1.0, 2, 3) == 1.0
    assert beta_cdf(0.5, 2, 2) == pytest.approx(0.5)
    assert beta_cdf(0.3, 1, 1) == pytest.approx(0.3)
    # complement form stays exact near 1
    assert beta_cdf(1 - 1e-12, 2, 2, xc=1e-12) == pytest.approx(1 - 3e-24, abs=1e-15)
    b = Beta(3, 7)
    assert beta_cdf(b.quantile(0.9), 3, 7) == pytest.approx(0.9, abs=1e-9)


def test_degenerate_windows_use_tail_terms() -> None:
    # a or b this small puts the logit window beyond +-700: the tail mass is added analytically
    for arms in (
        [(Beta(0.01, 5.0), 1.0), (Beta(2.0, 2.0), 0.5)],
        [(Beta(5.0, 0.01), 0.7), (Beta(2.0, 2.0), 0.5)],
    ):
        safe, ps = win_probabilities(arms)
        assert safe + sum(ps) == pytest.approx(1.0, abs=1e-6)
    # an arm with S ~ 0 almost never beats a Beta(2, 2) arm that is feasible half the time
    _, ps = win_probabilities([(Beta(0.01, 5.0), 1.0), (Beta(2.0, 2.0), 0.5)])
    assert ps[1] == pytest.approx(0.5, abs=0.01)
