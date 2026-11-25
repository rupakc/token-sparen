from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from token_sparen.config import load_config
from token_sparen.estimate import RATIOS, Prices, Usage, estimate_tokens


def test_ratios_verbatim() -> None:
    assert RATIOS == {"json": 3.2, "code": 3.5, "text": 4.0, "log": 3.6}


def test_estimate_basic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("a" * 320, "json") == 100
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abc", "unknown-kind") == 1


@given(st.text(), st.text(), st.sampled_from(["json", "code", "text", "log"]))
def test_estimate_monotonic(a: str, b: str, kind: str) -> None:
    assert estimate_tokens(a + b, kind) >= estimate_tokens(a, kind)


def test_prices_defaults_from_config(tmp_path: Path) -> None:
    p = Prices.load(load_config(tmp_path, {"TOKEN_SPAREN_HOME": str(tmp_path)}))
    assert (p.input, p.cache_write, p.cache_read, p.output) == (1.0, 1.25, 0.1, 5.0)


def test_cost_weighted() -> None:
    p = Prices(input=1.0, cache_write=1.25, cache_read=0.1, output=5.0)
    u = Usage(input=100, cache_read=1000, cache_write=80, output=10)
    assert u.cost_weighted(p) == 100 + 100 + 100 + 50
