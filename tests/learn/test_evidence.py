from __future__ import annotations

import math
import statistics

import pytest
from hypothesis import given
from hypothesis import strategies as st

from token_sparen.learn.beta import Beta, Evidence

unit = st.floats(min_value=0.0, max_value=1.0)
evid = st.builds(
    Evidence,
    st.floats(min_value=0.0, max_value=100.0),
    st.floats(min_value=0.0, max_value=100.0),
    st.floats(min_value=0.0, max_value=1e6),
)


@given(st.lists(unit, min_size=1, max_size=40), st.floats(min_value=0.5, max_value=1.0))
def test_observed_evidence_roundtrips_and_variance_bounded(xs: list[float], gamma: float) -> None:
    e = EMPTY
    for x in xs:
        e = e.observe(x, gamma=gamma, cap=20.0)
    assert Evidence.from_list(e.to_list()) == e
    v, m = e.var_within, e.mean_data
    assert v is not None and m is not None and 0.0 <= v <= m * (1 - m) + 1e-9


EMPTY = Evidence(0.0, 0.0, 0.0)


def test_observe_discounts_adds_and_counts() -> None:
    e = Evidence(4.0, 2.0, 6.0).observe(0.25, gamma=0.5, cap=50.0)
    assert e == Evidence(2.25, 1.75, 7.0, 0.5 * 4.0 + 0.0625)


def test_second_moment_and_within_variance() -> None:
    e = Evidence(0.0, 0.0, 0.0, 0.0)
    for x in (0.2, 0.4, 0.6):
        e = e.observe(x, gamma=1.0, cap=1e9)
    assert e.es2 == pytest.approx(0.04 + 0.16 + 0.36)
    assert e.var_within == pytest.approx(statistics.pvariance([0.2, 0.4, 0.6]))
    assert EMPTY.var_within is None


def test_bernoulli_within_variance_is_m_one_minus_m() -> None:
    e = EMPTY
    for x in (1.0, 0.0, 1.0, 1.0):
        e = e.observe(x, gamma=0.9, cap=1e9)
    m = e.mean_data
    assert m is not None and e.var_within == pytest.approx(m * (1 - m))
    assert Evidence(3.0, 1.0, 4.0).var_within == pytest.approx(0.75 * 0.25)  # es2 := ea


def test_cap_scales_second_moment() -> None:
    e = Evidence(30.0, 18.0, 48.0, 20.0).observe(0.5, gamma=1.0, cap=40.0)
    scale = 40.0 / 49.0
    assert e.es2 == pytest.approx((20.0 + 0.25) * scale)


def test_serialisation_has_four_fields_and_reads_three() -> None:
    assert Evidence(3.0, 1.0, 4.0, 2.5).to_list() == [3.0, 1.0, 4.0, 2.5]
    assert Evidence.from_list([3, 1, 4]) == Evidence(3.0, 1.0, 4.0, 3.0)
    assert Evidence.from_list([3, 1, 4, 2.5]) == Evidence(3.0, 1.0, 4.0, 2.5)


def test_observe_cap_preserves_mean() -> None:
    e = Evidence(30.0, 18.0, 48.0).observe(1.0, gamma=1.0, cap=40.0)
    assert e.ea + e.eb == pytest.approx(40.0)
    assert e.mean_data == pytest.approx(31.0 / 49.0)
    assert e.n == 49.0


def test_observe_clamps_and_validates() -> None:
    assert EMPTY.observe(3.0, gamma=1.0, cap=10.0) == Evidence(1.0, 0.0, 1.0)
    assert EMPTY.observe(-3.0, gamma=1.0, cap=10.0) == Evidence(0.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        EMPTY.observe(math.nan, gamma=1.0, cap=10.0)
    with pytest.raises(ValueError):
        EMPTY.observe(0.5, gamma=1.5, cap=10.0)
    with pytest.raises(ValueError):
        EMPTY.observe(0.5, gamma=1.0, cap=0.0)


def test_posterior_and_mean_data() -> None:
    e = Evidence(3.0, 1.0, 4.0)
    assert e.posterior(Beta(1.0, 2.0)) == Beta(4.0, 3.0, 4.0)
    assert e.mean_data == pytest.approx(0.75)
    assert EMPTY.mean_data is None
    assert EMPTY.posterior(Beta(2.0, 3.0)) == Beta(2.0, 3.0, 0.0)


@pytest.mark.parametrize(
    "bad",
    [(-1.0, 0.0, 0.0), (0.0, -0.1, 0.0), (0.0, 0.0, -1.0), (math.nan, 0, 0), (0, math.inf, 0)],
)
def test_invalid_construction(bad: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError):
        Evidence(*bad)


@pytest.mark.parametrize(
    "seq",
    [
        [1, 2],
        "abc",
        None,
        [1, 2, "x"],
        [True, 1, 0],
        [math.nan, 1, 0],
        [-1, 1, 0],
        [1, 1, 1, -0.5],
        [1, 1, 1, 1.5],  # es2 > ea is impossible for x in [0, 1]
        [1, 1, 1, math.inf],
        [1, 1, 1, 1, 1],
    ],
)
def test_from_list_rejects(seq: object) -> None:
    with pytest.raises(ValueError):
        Evidence.from_list(seq)  # type: ignore[arg-type]


@given(evid)
def test_roundtrip(e: Evidence) -> None:
    assert Evidence.from_list(e.to_list()) == e


@given(st.lists(unit, min_size=1, max_size=30), st.floats(min_value=0.5, max_value=1.0))
def test_equivalent_to_beta_update_without_cap(xs: list[float], gamma: float) -> None:
    prior = Beta(2.0, 3.0)
    post, ev = prior, EMPTY
    for x in xs:
        post = post.update(x, gamma=gamma, cap=1e12, prior=prior)
        ev = ev.observe(x, gamma=gamma, cap=1e12)
    got = ev.posterior(prior)
    assert got.a == pytest.approx(post.a) and got.b == pytest.approx(post.b)
    assert got.n == post.n
