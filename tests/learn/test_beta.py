from __future__ import annotations

import math
import random
import tomllib
from importlib import resources

import pytest
from hypothesis import given
from hypothesis import strategies as st

from token_sparen.learn.beta import Beta, beta_cdf

PRIOR = Beta(1.0, 1.0, 0.0)
unit = st.floats(min_value=0.0, max_value=1.0)
betas = st.builds(
    Beta,
    st.floats(min_value=1e-3, max_value=100.0),
    st.floats(min_value=1e-3, max_value=100.0),
    st.floats(min_value=0.0, max_value=1e6),
)


def test_fractional_update_mean() -> None:
    post = PRIOR.update(0.25, gamma=1.0, cap=50.0, prior=PRIOR)
    assert post.a == pytest.approx(1.25) and post.b == pytest.approx(1.75)
    assert post.n == 1.0
    assert post.mean == pytest.approx(1.25 / 3.0)


def test_cap_preserves_mean() -> None:
    post = Beta(40.0, 9.5, 49.0)
    new = post.update(1.0, gamma=1.0, cap=50.0, prior=PRIOR)
    expected = 41.0 / 50.5
    assert new.a + new.b == pytest.approx(50.0, abs=1e-9)
    assert new.mean == pytest.approx(expected, abs=1e-9)


def test_heavy_prior_still_learns_toward_data() -> None:
    prior = Beta(30.0, 30.0, 0.0)
    post = prior
    for _ in range(200):
        post = post.update(1.0, gamma=0.98, cap=20.0, prior=prior)
    assert post.a + post.b == pytest.approx(20.0)
    # Fixed point of (discount toward prior 30/30, +1 to a, rescale to cap 20): with
    # s = 20 / (60(1-g) + 20g + 1) = 20/21.8, b = 0.6s / (1 - g s) = 60/11, a = 20 - b = 160/11,
    # so the mean settles at 8/11 (above the prior's 0.5: it learns toward the data).
    assert post.mean == pytest.approx(8 / 11, abs=1e-6)


@pytest.mark.parametrize(
    "gamma,cap",
    [
        (-0.1, 50.0),
        (1.1, 50.0),
        (math.nan, 50.0),
        (0.9, 0.0),
        (0.9, -1.0),
        (0.9, math.inf),
        (0.9, math.nan),
    ],
)
def test_update_rejects_bad_params(gamma: float, cap: float) -> None:
    with pytest.raises(ValueError):
        PRIOR.update(0.5, gamma=gamma, cap=cap, prior=PRIOR)


def test_update_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        PRIOR.update(0.5, 0.9, 50.0, PRIOR)  # type: ignore[misc]


@pytest.mark.parametrize(
    "seq",
    [b"\x01\x02\x03", "123", [True, 1, 0], [1, 1, False], [1, 1, 1j], [1, 1, "2"], [1, 1, None]],
)
def test_from_list_rejects_non_real(seq: object) -> None:
    with pytest.raises(ValueError):
        Beta.from_list(seq)  # type: ignore[arg-type]


def test_quantile_closed_forms() -> None:
    for q in (0.01, 0.05, 0.3, 0.7, 0.95, 0.99):
        assert Beta(1.0, 1.0).quantile(q) == pytest.approx(q, abs=1e-8)
        for k in (2.0, 5.0, 17.0):
            assert Beta(1.0, k).quantile(q) == pytest.approx(1 - (1 - q) ** (1 / k), abs=1e-8)
            assert Beta(k, 1.0).quantile(q) == pytest.approx(q ** (1 / k), abs=1e-8)
    b = Beta(3.5, 11.0)
    assert b.quantile(0.1) == pytest.approx(1 - Beta(11.0, 3.5).quantile(0.9), abs=1e-8)
    # Beta(2,2) cdf = 3x^2 - 2x^3
    assert Beta(2.0, 2.0).quantile(0.5) == pytest.approx(0.5, abs=1e-8)
    assert Beta(2.0, 2.0).quantile(0.15625) == pytest.approx(0.25, abs=1e-8)


def test_lcb_ucb_sides() -> None:
    b = Beta(10.0, 10.0)
    assert b.lcb() < 0.5 < b.ucb()
    for q in (0.5, 0.9):
        with pytest.raises(ValueError):
            b.lcb(q)
    for q in (0.5, 0.1):
        with pytest.raises(ValueError):
            b.ucb(q)
    assert 0.0 <= Beta(1e-3, 1e6).lcb() and Beta(1e6, 1e-3).ucb() <= 1.0


@given(betas, st.floats(min_value=0.001, max_value=0.999))
def test_quantile_ordering(b: Beta, q: float) -> None:
    lo, hi = b.quantile(q / 2), b.quantile((1 + q) / 2)
    assert 0.0 <= lo <= hi <= 1.0


def _check_inverse(b: Beta, p: float, x: float) -> None:
    """``I_x(a, b) == p`` to a tolerance relative in log space (the quantile may sit near 0)."""
    assert 0.0 <= x <= 1.0
    if x == 0.0:  # underflow: the small-x asymptote x^a / (a B(a, b)) puts the root below 1e-308
        log_b = math.lgamma(b.a) + math.lgamma(b.b) - math.lgamma(b.a + b.b)
        assert (math.log(p) + math.log(b.a) + log_b) / b.a < math.log(1e-300)
        return
    cdf = beta_cdf(x, b.a, b.b)
    assert math.log(cdf) == pytest.approx(math.log(p), rel=1e-9, abs=1e-9)


def test_quantile_tiny_alpha_regression() -> None:
    """Hypothesis case: both quantiles sat near 1e-11 (I_x = 0.5 for each) and were inverted."""
    b, q = Beta(0.03362326622875097, 32.0, 0.0), 0.03515625
    lo, hi = b.quantile(q / 2), b.quantile((1 + q) / 2)
    assert 0.0 <= lo < hi <= 1.0
    _check_inverse(b, q / 2, lo)
    _check_inverse(b, (1 + q) / 2, hi)


@pytest.mark.parametrize("a", [1e-3, 0.01, 0.03, 0.1])
@pytest.mark.parametrize("bb", [1.0, 32.0, 1000.0])
def test_quantile_tiny_alpha_sweep(a: float, bb: float) -> None:
    b = Beta(a, bb)
    ps = [1e-3, 0.01, 0.025, 0.05, 0.2, 0.5, 0.8, 0.95, 0.975, 0.99, 0.999]
    xs = [b.quantile(p) for p in ps]
    assert xs == sorted(xs)
    for p, x in zip(ps, xs, strict=True):
        _check_inverse(b, p, x)
    for q in (0.001, 0.03515625, 0.5, 0.9):
        assert b.lcb(q / 2) <= b.quantile((1 + q) / 2)


@given(betas, unit)
def test_exact_monotonicity_condition(b: Beta, g: float) -> None:
    prior = Beta(2.0, 3.0, 0.0)
    k, pm, m = prior.a + prior.b, prior.mean, b.mean
    new = b.update(1.0, gamma=g, cap=1e12, prior=prior)
    cond = (1 - g) * k * (pm - m) + (1 - m)
    if cond > 1e-9:
        assert new.mean > m - 1e-12
    elif cond < -1e-9:
        assert new.mean < m + 1e-12


def test_discount_forgets_toward_prior() -> None:
    post = PRIOR
    for _ in range(1000):
        post = post.update(1.0, gamma=0.98, cap=50.0, prior=PRIOR)
    assert post.mean > 0.9
    for _ in range(1000):
        post = post.update(0.0, gamma=0.98, cap=50.0, prior=PRIOR)
    assert post.mean < 0.2


def test_update_clamps_and_rejects_nan() -> None:
    hi = PRIOR.update(5.0, gamma=1.0, cap=50.0, prior=PRIOR)
    assert hi.a == pytest.approx(2.0) and hi.b == pytest.approx(1.0)
    lo = PRIOR.update(-3.0, gamma=1.0, cap=50.0, prior=PRIOR)
    assert lo.a == pytest.approx(1.0) and lo.b == pytest.approx(2.0)
    with pytest.raises(ValueError):
        PRIOR.update(math.nan, gamma=1.0, cap=50.0, prior=PRIOR)


def test_frozen() -> None:
    with pytest.raises(AttributeError):
        PRIOR.a = 2.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [
        (0.0, 1.0, 0.0),
        (1.0, -1.0, 0.0),
        (1.0, 1.0, -1.0),
        (math.nan, 1.0, 0.0),
        (1.0, math.inf, 0.0),
    ],
)
def test_invalid_construction(bad: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError):
        Beta(*bad)


@pytest.mark.parametrize(
    "seq", [[1, 2], "abc", None, [1, 2, "x"], [math.nan, 1, 0], [-1, 1, 0], [1, 1, -2]]
)
def test_from_list_rejects(seq: object) -> None:
    with pytest.raises(ValueError):
        Beta.from_list(seq)  # type: ignore[arg-type]


def test_sample_tiny_params() -> None:
    rng = random.Random(1)
    s = Beta(1e-12, 1e-12, 0.0).sample(rng)
    assert 0.0 <= s <= 1.0


def test_defaults_toml_learn_keys() -> None:
    text = resources.files("token_sparen.params").joinpath("defaults.toml").read_text("utf-8")
    learn = tomllib.loads(text)["learn"]
    assert learn["gamma"] == 0.98 and learn["posterior_cap"] == 50


@given(betas, unit)
def test_sample_in_unit_interval(b: Beta, seed_frac: float) -> None:
    s = b.sample(random.Random(int(seed_frac * 1e6)))
    assert 0.0 <= s <= 1.0


@given(betas, unit, unit, st.floats(min_value=1.0, max_value=200.0))
def test_mean_in_unit_and_cap_respected(b: Beta, x: float, g: float, cap: float) -> None:
    prior = Beta(1.0, 1.0, 0.0)
    new = b.update(x, gamma=g, cap=cap, prior=prior)
    assert 0.0 <= new.mean <= 1.0
    assert new.a > 0 and new.b > 0
    assert new.a + new.b <= max(cap, prior.a + prior.b) + 1e-9


@given(betas, unit, st.floats(min_value=3.0, max_value=200.0))
def test_direction_monotonic(b: Beta, x: float, cap: float) -> None:
    prior = Beta(1.0, 1.0, 0.0)
    up = b.update(1.0, gamma=1.0, cap=cap, prior=prior)
    down = b.update(0.0, gamma=1.0, cap=cap, prior=prior)
    assert up.mean >= b.mean - 1e-9
    assert down.mean <= b.mean + 1e-9


@given(betas, unit, st.floats(min_value=3.0, max_value=200.0))
def test_cap_preserves_mean_property(b: Beta, x: float, cap: float) -> None:
    prior = Beta(1.0, 1.0, 0.0)
    unc = b.update(x, gamma=1.0, cap=1e12, prior=prior)
    new = b.update(x, gamma=1.0, cap=cap, prior=prior)
    assert new.mean == pytest.approx(unc.mean, abs=1e-9)
    assert new.a + new.b == pytest.approx(min(cap, unc.a + unc.b), abs=1e-9)


@given(betas)
def test_roundtrip(b: Beta) -> None:
    assert Beta.from_list(b.to_list()) == b
