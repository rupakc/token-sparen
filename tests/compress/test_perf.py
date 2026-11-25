"""Compressor performance: scaling in the default suite, strict budgets under `-m perf`."""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from perfutil import best_of, perf_factor

from token_sparen.compress import CompressContext, Level, TextCompressor

STANDARD = Level(head=40, tail=20, keep_ratio=0.35)
CTX = CompressContext(prompt="disk lorem", recent_paths=("x/kappa.log",))
WORDS = ["alpha", "beta", "gamma", "delta", "omega", "lorem", "ipsum", "kappa", "zeta"]
MB = 1024 * 1024


def _build(size: int, line: Callable[[int, str], str]) -> str:
    rng = random.Random(17)
    base = [" ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 8))) for _ in range(400)]
    chunks: list[str] = []
    total = 0
    i = 0
    while total < size:
        ln = line(i, base[i % 400])
        chunks.append(ln)
        total += len(ln) + 1
        i += 1
    return "\n".join(chunks)


def sparse(i: int, w: str) -> str:
    if i % 50_000 == 25_000:
        return "FATAL: disk on fire"
    return f"{i} {w} \x00 bin\udcff\ud800 data"


def all_errors(i: int, w: str) -> str:
    return f"ERROR: failure {i} in {w}"


def false_positives(i: int, w: str) -> str:
    return f"{i} terror {w} terrorism"


def one_in_five(i: int, w: str) -> str:
    return f"ERROR {i} {w}" if i % 5 == 0 else f"{i} {w}"


def _best(text: str, runs: int) -> float:
    best, res = best_of(lambda: TextCompressor().compress(text, STANDARD, CTX), runs)
    assert isinstance(res.text, str)
    return best


def test_huge_and_binary_output() -> None:
    """10 MB with NUL and lone surrogates: returns str, keeps errors, scales ~linearly."""
    small, big = _build(MB, sparse), _build(10 * MB, sparse)
    res = TextCompressor().compress(big, STANDARD, CTX)
    assert "FATAL: disk on fire" in res.text
    assert res.total_lines == big.count("\n") + 1
    t_small, t_big = _best(small, 3), _best(big, 2)
    factor = perf_factor(3.0)
    assert t_big < 0.2 * factor, f"took {t_big * 1000:.0f} ms"
    assert t_big / t_small <= 15, f"1 MB {t_small * 1000:.0f} ms, 10 MB {t_big * 1000:.0f} ms"


@pytest.mark.perf
@pytest.mark.parametrize("line", [sparse, all_errors, false_positives, one_in_five])
def test_strict_budget_10mb(line: Callable[[int, str], str]) -> None:
    """200 ms is the dev-machine reference; CI sets TOKEN_SPAREN_PERF_FACTOR for slower runners."""
    factor = perf_factor()
    best = _best(_build(10 * MB, line), 5)
    assert best < 0.2 * factor, f"{line.__name__}: {best * 1000:.0f} ms (factor {factor})"
