from __future__ import annotations

import math
import random
import tomllib
from importlib import resources

import pytest
from hypothesis import given
from hypothesis import strategies as st

from token_sparen.learn.beta import Beta

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


def test_cap_below_prior_mass_keeps_prior() -> None:
    prior = Beta(30.0, 30.0, 0.0)
    new = prior.update(1.0, gamma=0.98, cap=50.0, prior=prior)
    assert (new.a, new.b, new.n) == (30.0, 30.0, 1.0)


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


def test_bounds() -> None:
    b = Beta(10.0, 10.0, 20.0)
    assert 0.0 <= b.lcb() < 0.5 < b.ucb() <= 1.0
    assert b.lcb(0.5) == pytest.approx(0.5) == b.ucb(0.5)
    assert Beta(1e6, 1e-3, 0.0).ucb() == 1.0
    assert Beta(1e-3, 1e6, 0.0).lcb() == 0.0
    for q in (0.0, 1.0, -0.1, 1.5, math.nan):
        with pytest.raises(ValueError):
            b.lcb(q)
        with pytest.raises(ValueError):
            b.ucb(q)


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
