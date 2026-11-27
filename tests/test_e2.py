"""E2 fact-preservation suite as a pytest gate."""

from __future__ import annotations

import os
import tomllib
from collections import Counter
from pathlib import Path

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


def _overlay(**std: object) -> dict[str, object]:
    return {"compression": {"levels": {"standard": std}}}


def test_params_overlay_reaches_compressors() -> None:
    base = e2.run(levels=("standard",))
    kept = e2.run(levels=("standard",), params=_overlay(head=400, tail=400, keep_ratio=0.95))
    assert kept.mean_ratio["standard"] > base.mean_ratio["standard"]
    assert kept.failures == []
    assert "ts-e2-" not in os.environ.get("TOKEN_SPAREN_HOME", "")


def test_inline_rate_per_level() -> None:
    report = e2.run()
    assert set(report.inline_rate) == set(e2.ALL_LEVELS)
    assert all(0.0 < v <= 1.0 for v in report.inline_rate.values())
    more = e2.run(levels=("standard",), params=_overlay(head=400, tail=400, keep_ratio=0.95))
    assert more.inline_rate["standard"] >= report.inline_rate["standard"]
    nothing = e2.run(levels=("standard",), params=_overlay(head=0, tail=0, keep_ratio=0.0))
    assert nothing.inline_rate["standard"] < report.inline_rate["standard"]


def test_inline_rate_excludes_recovery_only_needles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2, "_compress_case", lambda c, lv, tmp: ("x", c.text, 1.0))
    report = e2.run(levels=("light",))
    assert report.inline_rate["light"] == 0.0


@pytest.mark.parametrize(
    "params",
    [_overlay(bogus=1), _overlay(head="many"), {"nope": {"x": 1}}],
)
def test_invalid_overlay_key_raises(params: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="overlay"):
        e2.run(levels=("standard",), params=params)


def test_no_overlay_output_unchanged() -> None:
    a = e2.run(levels=("standard",))
    b = e2.run(levels=("standard",), params={})
    assert e2._table(a, ("standard",)) == e2._table(b, ("standard",))
    assert "inline" not in e2._table(a, ("standard",))
    assert a.rows == b.rows


def test_params_flag_loads_overlay_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "p.toml"
    f.write_text("[compression.levels.standard]\nhead = 400\n", encoding="utf-8")
    assert e2.main(["--params", str(f), "--json"]) == 0
    assert "inline_rate" in capsys.readouterr().out


def test_optional_key_overlay_reaches_guard() -> None:
    from token_sparen.config import OPTIONAL_KEYS

    assert "guards.pretrim_bounded_keep_ratio" in OPTIONAL_KEYS

    def rate(ratio: float) -> float:
        p = {"guards": {"pretrim_bounded_keep_ratio": ratio, "pretrim_lines": 5}}
        rep = e2.run(levels=("pretrim",), params=p)
        assert rep.failures == []
        return rep.inline_rate["pretrim"]

    assert rate(0.0) < rate(0.9)
    for bad in ("x", 2.0):
        with pytest.raises(ValueError, match="overlay"):
            e2.run(levels=("standard",), params={"guards": {"pretrim_bounded_keep_ratio": bad}})


def test_pretrim_runs_on_command_cases_only() -> None:
    report = e2.run(levels=("pretrim",))
    n = sum(1 for c in CASES if c.command)
    assert 0 < n < len(CASES)
    assert report.cases == report.passed == n
    assert {r["case"] for r in report.rows} == {c.file for c in CASES if c.command}


def test_marker_text_is_not_inline() -> None:
    assert e2._inline_text("body\n[token-sparen: kept 1 of 2 lines · x]", "full") == "body"
    assert e2._inline_text("body\nlast", None) == "body\nlast"
    assert e2._inline_text("body\nlast", "full") == "body\nlast"


def test_overlay_uses_user_layer_bounds() -> None:
    from token_sparen.config import validate_overlay

    validate_overlay({"prune": {"lease_sessions": 0}})
    with pytest.raises(ValueError, match="overlay"):
        validate_overlay({"compression": {"levels": {"standard": {"head": 400.0}}}})


def test_optional_key_overlay_is_loaded_by_config(tmp_path: Path) -> None:
    from token_sparen.config import load_config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        e2._toml_dump({"guards": {"pretrim_bounded_keep_ratio": 0.5}})
    )
    cfg = load_config(tmp_path, {"TOKEN_SPAREN_HOME": str(home)})
    assert cfg.get("guards.pretrim_bounded_keep_ratio") == 0.5
