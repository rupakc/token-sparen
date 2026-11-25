"""E2 fact-preservation suite as a pytest gate."""

from __future__ import annotations

import tomllib
from collections import Counter

import pytest

from evals.e2 import run as e2

CASES = e2.load_cases()


def test_corpus_shape() -> None:
    assert len(CASES) >= 40
    seen = Counter(t for c in CASES for t in c.types)
    for t in (
        "json",
        "diff",
        "stacktrace",
        "test_report",
        "compiler",
        "build_log",
        "search",
        "pplx",
        "log",
        "table",
        "text",
    ):
        assert seen[t] >= 3, t
    big = sum(1 for c in CASES if c.tokens >= 2000)
    assert big * 2 >= len(CASES)
    for c in CASES:
        assert c.needles, c.file
        assert set(c.must_inline) <= set(c.needles), c.file


def test_needles_toml_has_no_unknown_keys() -> None:
    raw = tomllib.loads(e2.NEEDLES.read_text(encoding="utf-8"))
    allowed = {"file", "command", "types", "needles", "must_inline"}
    for c in raw["case"]:
        assert set(c) <= allowed, c["file"]


def test_needle_check_semantics() -> None:
    assert e2.needle_ok("x", "abc x", None, must_inline=True)
    assert not e2.needle_ok("x", "abc", "has x", must_inline=True)
    assert e2.needle_ok("x", "abc", "has x", must_inline=False)
    assert not e2.needle_ok("x", "abc", None, must_inline=False)


def test_e2_all_pass() -> None:
    report = e2.run()
    assert report.failures == []
    assert report.passed == report.cases > 0


def test_e2_reports_ratio() -> None:
    report = e2.run(levels=("standard",))
    assert 0 < report.mean_ratio["standard"] < 0.6


def test_e2_detects_a_dropped_needle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2, "_compress_case", lambda case, level, tmp: ("nothing", None, 1.0))
    report = e2.run(levels=("light",))
    assert report.failures
    assert report.passed < report.cases
