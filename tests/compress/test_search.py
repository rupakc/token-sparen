"""Search (grep/rg), `pplx` and log compressors."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from perfutil import best_of, perf_factor

from token_sparen.compress import CompressContext, Level, TextCompressor, is_error_line, registry
from token_sparen.compress.search import LogCompressor, PplxCompressor, SearchCompressor

LIGHT = Level(head=80, tail=40, keep_ratio=0.6)
STANDARD = Level(head=40, tail=20, keep_ratio=0.35)
AGGRESSIVE = Level(head=15, tail=10, keep_ratio=0.15)
NO_CTX = CompressContext(prompt=None, recent_paths=())


def test_registered() -> None:
    assert isinstance(registry.for_type("search"), SearchCompressor)
    assert isinstance(registry.for_type("pplx"), PplxCompressor)
    assert isinstance(registry.for_type("log"), LogCompressor)


# ---------------------------------------------------------------- search


def _grep(files: int, per: int) -> str:
    return "\n".join(
        f"src/mod{f}.py:{n}:    value = compute({n})"
        for f in range(files)
        for n in range(1, per + 1)
    )


def test_search_keeps_every_file_name() -> None:
    text = _grep(30, 20) + "\n"
    res = SearchCompressor().compress(text, AGGRESSIVE, NO_CTX)
    assert len(res.text) < len(text) / 3
    for f in range(30):
        assert f"src/mod{f}.py" in res.text
    assert not res.lossless
    assert res.total_lines == 600


@pytest.mark.parametrize(("level", "kept"), [(LIGHT, 20), (STANDARD, 8), (AGGRESSIVE, 3)])
def test_search_per_file_by_arm(level: Level, kept: int) -> None:
    res = SearchCompressor().compress(_grep(2, 30), level, NO_CTX)
    lines = res.text.split("\n")
    assert sum(ln.startswith("src/mod0.py:") for ln in lines) == kept
    assert f"  … {30 - kept} more matches in src/mod0.py" in lines
    assert res.kept_lines == 2 * kept


def test_search_ranks_by_relevance_but_keeps_line_order() -> None:
    lines = [f"a.py:{n}:    filler {n}" for n in range(1, 21)]
    lines[16] = "a.py:17:    def parse_widget(x): pass"
    lines[11] = "a.py:12:    widget = parse_widget()"
    ctx = CompressContext(prompt="fix parse_widget", recent_paths=())
    res = SearchCompressor().compress("\n".join(lines), AGGRESSIVE, ctx)
    out = res.text.split("\n")
    nums = [int(ln.split(":")[1]) for ln in out if ln.startswith("a.py:")]
    assert 17 in nums and 12 in nums
    assert nums == sorted(nums)
    assert len(nums) == 3


def test_search_error_matches_survive_beyond_per_file() -> None:
    lines = [f"a.py:{n}:    ok {n}" for n in range(1, 41)]
    for n in (30, 35, 39):
        lines[n - 1] = f"a.py:{n}:    raise ValueError('bad {n}')"
    res = SearchCompressor().compress("\n".join(lines), AGGRESSIVE, NO_CTX)
    for n in (30, 35, 39):
        assert f"bad {n}" in res.text
    assert all(is_error_line(ln) for ln in res.text.split("\n") if "ValueError" in ln)


def test_search_vimgrep_columns() -> None:
    text = "\n".join(f"x/y.rs:{n}:{n + 3}:let a = {n};" for n in range(1, 31))
    res = SearchCompressor().compress(text, AGGRESSIVE, NO_CTX)
    assert "  … 27 more matches in x/y.rs" in res.text
    assert res.text.count("x/y.rs:") == 3


def test_search_context_lines_and_separators() -> None:
    blocks = []
    for n in range(10, 100, 10):
        blocks += [f"a.py-{n - 1}-before", f"a.py:{n}:hit {n}", f"a.py-{n + 1}-after", "--"]
    text = "\n".join(blocks[:-1])
    ctx = CompressContext(prompt="hit", recent_paths=())
    out = SearchCompressor().compress(text, AGGRESSIVE, ctx).text.split("\n")
    kept_hits = [ln for ln in out if ln.startswith("a.py:")]
    assert len(kept_hits) == 3
    for hit in kept_hits:
        n = int(hit.split(":")[1])
        assert f"a.py-{n - 1}-before" in out
        assert f"a.py-{n + 1}-after" in out
    assert sum(ln.startswith("a.py-") for ln in out) == 6
    assert "--" in out
    assert "  … 6 more matches in a.py" in out


def test_search_binary_notice_and_unparsed_lines_kept() -> None:
    text = _grep(1, 20) + "\nBinary file img/logo.png matches\nrg: some stderr line"
    out = SearchCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert "Binary file img/logo.png matches" in out
    assert "rg: some stderr line" in out


def test_search_interleaved_files_grouped_in_first_seen_order() -> None:
    text = "\n".join(f"{f}.py:{n}:x {n}" for n in range(1, 11) for f in ("b", "a"))
    out = SearchCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert out.index("b.py:") < out.index("a.py:")
    assert "more matches in b.py" in out and "more matches in a.py" in out


def test_search_file_list_is_lossless() -> None:
    text = "\n".join(f"src/file{i}.py" for i in range(50)) + "\n"
    res = SearchCompressor().compress(text, AGGRESSIVE, NO_CTX)
    assert res.text == text and res.lossless


def test_search_small_input_unchanged() -> None:
    text = "a.py:1:x\na.py:2:y\n"
    res = SearchCompressor().compress(text, STANDARD, NO_CTX)
    assert res.text == text and res.lossless and res.kept_lines == 2


def test_search_config_override(tmp_path: Path) -> None:
    from token_sparen.config import load_config

    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "token-sparen.toml").write_text(
        "[compression.search]\nper_file = { standard = 5 }\n", encoding="utf-8"
    )
    ctx = CompressContext(prompt=None, recent_paths=(), config=load_config(proj, env))
    out = SearchCompressor().compress(_grep(1, 30), STANDARD, ctx).text
    assert out.count("src/mod0.py:") == 5


# ------------------------------------------------------------------ pplx


def _result(i: int, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": f"Title {i}",
        "url": f"https://example.com/{i}",
        "date": "2026-01-02",
        "snippet": f"Snippet text {i} about widgets",
        "summary": f"Summary text {i} about gadgets and other unrelated things",
    }
    base.update(extra)
    return base


def test_pplx_dedupes_snippet_summary() -> None:
    near = "Rust 1.80 stabilises LazyCell and LazyLock for lazy initialisation in std"
    doc = [
        _result(1, snippet=near, summary=near + "."),
        _result(
            2, snippet="short bit", summary="a longer summary that contains a short bit inside"
        ),
        _result(3),
    ]
    out = PplxCompressor().compress(json.dumps(doc), LIGHT, NO_CTX).text
    assert out.count("LazyCell") == 1
    assert "short bit inside" in out
    assert out.count("short bit") == 1
    assert "Snippet text 3 about widgets" in out  # distinct snippets stay
    assert "Summary text 3" in out


def test_pplx_top_k_then_url_list() -> None:
    doc = {"results": [_result(i) for i in range(12)], "usage": {"tokens": 5}}
    text = json.dumps(doc, indent=2)
    res = PplxCompressor().compress(text, STANDARD, NO_CTX)
    out = res.text
    assert "[1] Title 0 — https://example.com/0 (2026-01-02)" in out
    assert "[5] Title 4" in out and "[6]" not in out
    tail = [ln for ln in out.split("\n") if ln.startswith("… 7 more results:")]
    assert tail
    for i in range(5, 12):
        assert f"https://example.com/{i}" in tail[0]
    assert "usage" not in out and "tokens" not in out
    assert not res.lossless and res.total_lines == len(text.split("\n"))


@pytest.mark.parametrize(("level", "shown"), [(LIGHT, 10), (STANDARD, 5), (AGGRESSIVE, 3)])
def test_pplx_arm_sizes(level: Level, shown: int) -> None:
    out = PplxCompressor().compress(json.dumps([_result(i) for i in range(20)]), level, NO_CTX).text
    assert f"[{shown}] Title" in out and f"[{shown + 1}]" not in out
    assert f"… {20 - shown} more results:" in out


def test_pplx_web_results_key_and_unknown_keys_dropped() -> None:
    doc = {"web_results": [_result(1, junk="DROPME", source="web")]}
    out = PplxCompressor().compress(json.dumps(doc, indent=4), LIGHT, NO_CTX).text
    assert "DROPME" not in out and "junk" not in out
    assert "web" in out and "Title 1" in out


def test_pplx_missing_fields() -> None:
    doc = [{"url": "https://u.example/1"}, {"title": "Only title"}, {"snippet": "only snippet"}]
    out = PplxCompressor().compress(json.dumps(doc * 3, indent=2), LIGHT, NO_CTX).text
    assert "https://u.example/1" in out and "Only title" in out and "only snippet" in out


def test_pplx_invalid_json_falls_back() -> None:
    text = "\n".join(f"not json line {i}" for i in range(300))
    res = PplxCompressor().compress(text, STANDARD, NO_CTX)
    assert res == TextCompressor().compress(text, STANDARD, NO_CTX)


@pytest.mark.parametrize("text", ["42", '{"a": 1}', "[1, 2, 3]", "[]", '{"results": []}'])
def test_pplx_unexpected_shapes_fall_back(text: str) -> None:
    res = PplxCompressor().compress(text, STANDARD, NO_CTX)
    assert res == TextCompressor().compress(text, STANDARD, NO_CTX)


def test_pplx_no_growth() -> None:
    text = '[{"a":1}]'
    res = PplxCompressor().compress(text, STANDARD, NO_CTX)
    assert res.text == text and res.lossless


def test_pplx_nonstring_values_do_not_raise() -> None:
    doc = [{"title": 5, "url": None, "date": 1.5, "snippet": ["x"], "summary": {"a": 1}}] * 4
    assert isinstance(PplxCompressor().compress(json.dumps(doc, indent=2), LIGHT, NO_CTX).text, str)


# ------------------------------------------------------------------- log


def _log(n: int, error_at: tuple[int, ...] = (), warn_at: tuple[int, ...] = ()) -> str:
    out = []
    for i in range(n):
        ts = f"2026-11-25T10:{i // 60 % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z"
        if i in error_at:
            out.append(f"{ts} ERROR request {i} blew up")
        elif i in warn_at:
            out.append(f"{ts} WARN slow request {i}")
        else:
            out.append(f"{ts} INFO heartbeat ok")
    return "\n".join(out) + "\n"


def test_log_keeps_errors_and_tail() -> None:
    text = _log(500, error_at=(100, 250), warn_at=(300,))
    res = LogCompressor().compress(text, STANDARD, NO_CTX)
    lines = res.text.split("\n")
    assert any("request 100 blew up" in ln for ln in lines)
    assert any("request 250 blew up" in ln for ln in lines)
    assert any("WARN slow request 300" in ln for ln in lines)
    orig = text.rstrip("\n").split("\n")
    for ln in orig[-30:]:
        assert ln in lines
    assert any(ln.startswith("… [") and "lines elided]" in ln for ln in lines)
    assert len(res.text) < len(text) / 5
    assert res.total_lines == 500 and not res.lossless


def test_log_tail_knob() -> None:
    text = _log(200)
    out = LogCompressor().compress(text, STANDARD, NO_CTX).text.split("\n")
    assert out[-2] == text.split("\n")[-2]
    assert out[-31] == text.split("\n")[-31]
    assert out[-32].startswith("… [")


@pytest.mark.parametrize(
    "stamps",
    [
        ["2026-11-25T10:00:0{}Z", "2026-11-25 10:00:0{},123", "2026-11-25T10:00:0{}.5+02:00"],
        ["Nov 25 10:00:0{}", "Nov  5 10:00:0{}", "Dec 25 10:00:0{}"],
        ["[170000000{}.123]", "[170000000{}]", "[1700000009.5]"],
        ["10:00:0{}.123", "10:00:0{}.456", "11:01:0{}.999"],
    ],
)
def test_log_timestamp_formats_fold(stamps: list[str]) -> None:
    body = [f"{stamps[i % len(stamps)].format(i % 10)} WARN tick" for i in range(200)]
    text = "\n".join(["start of run", *body, "end"]) + "\n"
    out = LogCompressor().compress(text, STANDARD, NO_CTX).text
    assert "[× 171 similar lines]" in out
    assert out.rstrip("\n").endswith("end")
    assert len(out.split("\n")) < 40


def test_log_fold_marker_on_kept_level_runs() -> None:
    text = "\n".join(f"2026-11-25T10:00:{i:02d}Z WARN disk at {i}%" for i in range(50)) + "\n"
    text = "boot\n" * 40 + text
    out = LogCompressor().compress(text, STANDARD, NO_CTX).text
    assert "[× 50 similar lines]" in out or "similar lines" in out


def test_log_error_lines_are_never_folded() -> None:
    text = "\n".join(f"2026-11-25T10:00:{i:02d}Z ERROR retry {i} failed" for i in range(40))
    text = "x\n" * 100 + text + "\n" + "tail\n" * 40
    out = LogCompressor().compress(text, STANDARD, NO_CTX).text.split("\n")
    for i in range(40):
        assert f"2026-11-25T10:00:{i:02d}Z ERROR retry {i} failed" in out


def test_log_level_words() -> None:
    text = "".join(
        f"{k} 2026 {lvl}: thing {i}\n"
        for i, (k, lvl) in enumerate(
            (a, b) for a in "abcdefghij" for b in ("INFO", "WARNING", "CRITICAL", "debug")
        )
    )
    out = LogCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert "WARNING: thing 1" in out and "CRITICAL: thing 2" in out


def test_log_small_unchanged() -> None:
    text = _log(10)
    res = LogCompressor().compress(text, STANDARD, NO_CTX)
    assert res.text == text and res.lossless


def test_log_fallback_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path))
    text = _log(200)

    def boom(self: object, *a: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(LogCompressor, "_run", boom)
    assert LogCompressor().compress(text, STANDARD, NO_CTX) == TextCompressor().compress(
        text, STANDARD, NO_CTX
    )


@pytest.mark.parametrize("cls", [SearchCompressor, PplxCompressor, LogCompressor])
def test_garbage_inputs_do_not_raise(cls: type) -> None:
    for text in ("", "\n", "x", "\x00\ud800 ERROR\r\r\n", "--\n--\n", "a:b:c\n" * 3, "-1-\n"):
        assert isinstance(cls().compress(text, AGGRESSIVE, NO_CTX).text, str)


# ------------------------------------------------------------------ perf


def _big(unit: str, size: int = 1024 * 1024) -> str:
    rng = random.Random(5)
    parts: list[str] = []
    total = 0
    i = 0
    while total < size:
        chunk = (
            unit.replace("{i}", str(i))
            .replace("{f}", str(i // 40))
            .replace("{r}", str(rng.randint(0, 99)))
        )
        parts.append(chunk)
        total += len(chunk) + 1
        i += 1
    return "\n".join(parts) + "\n"


@pytest.mark.perf
def test_search_budget_1mb() -> None:
    text = _big("src/pkg/mod{f}.py:{i}:    result = compute_{r}(value, option) + 1")
    ctx = CompressContext(prompt="compute value", recent_paths=())
    best, res = best_of(lambda: SearchCompressor().compress(text, STANDARD, ctx), 3)
    assert res.text != text
    assert best < 0.1 * perf_factor(), f"{best * 1000:.0f} ms"


@pytest.mark.perf
def test_pplx_budget_1mb() -> None:
    doc = {"results": [_result(i) for i in range(1, 6000)]}
    text = json.dumps(doc, indent=2)
    assert len(text) > 1024 * 1024 * 0.9
    best, res = best_of(lambda: PplxCompressor().compress(text, STANDARD, NO_CTX), 3)
    assert "[1] Title 1" in res.text
    assert best < 0.1 * perf_factor(), f"{best * 1000:.0f} ms"


@pytest.mark.perf
@pytest.mark.parametrize(
    "unit",
    [
        "2026-11-25T10:00:{r}.{i}Z INFO request {i} served in {r} ms",
        "Nov 25 10:00:{r} host app[{r}]: INFO heartbeat",
        "[1700000{i}.123] INFO tick",
    ],
)
def test_log_budget_1mb(unit: str) -> None:
    text = _big(unit)
    best, res = best_of(lambda: LogCompressor().compress(text, STANDARD, NO_CTX), 3)
    assert res.text != text
    assert best < 0.1 * perf_factor(), f"{best * 1000:.0f} ms"


# ------------------------------------------------- pplx real CLI schemas


def _hit(i: int, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "url": f"https://example.com/{i}",
        "title": f"Hit {i}",
        "domain": "example.com",
        "snippet": f"Snippet {i} about widgets",
        "summary": f"Summary {i} about gadgets and unrelated things",
        "date": "2026-03-04",
        "last_updated": "2026-05-06",
        "trust": {"level": "high", "name": "Trusted", "description": "LONG-TRUST-DESC"},
    }
    base.update(extra)
    return base


def test_pplx_search_hits_schema() -> None:
    doc = {"total": 40, "hits": [_hit(i) for i in range(1, 9)], "answer": "forty-two"}
    res = PplxCompressor().compress(json.dumps(doc, indent=2), STANDARD, NO_CTX)
    out = res.text.split("\n")
    assert out[0] == "pplx: showing 5 of 40 hits"
    assert "answer: forty-two" in out
    assert "[1] Hit 1 — https://example.com/1 (2026-03-04) · trust: high" in out
    assert "[6]" not in res.text
    assert "… 3 more results: " + ", ".join(f"https://example.com/{i}" for i in (6, 7, 8)) in out
    for dropped in ("domain", "last_updated", "2026-05-06", "LONG-TRUST-DESC", "Trusted"):
        assert dropped not in res.text


def _snippets_doc() -> dict[str, object]:
    return {
        "results": [
            {"url": f"https://p.example/{i}", "text": f"line {i}\n" * 3, "tokens_count": 10 + i}
            for i in range(1, 8)
        ]
        + [{"url": "https://p.example/bad", "error": "HTTP 503 fetch failed"}],
        "saved_to": "/tmp/out/full.json",
        "truncated": True,
    }


def test_pplx_snippets_schema_keeps_errors_and_headers() -> None:
    res = PplxCompressor().compress(json.dumps(_snippets_doc(), indent=2), AGGRESSIVE, NO_CTX)
    out = res.text.split("\n")
    assert "saved_to: /tmp/out/full.json" in out
    assert "truncated: true" in out
    assert "[1] https://p.example/1 (11 tok)" in out
    assert "    line 1" in out
    assert "[8] https://p.example/bad — error: HTTP 503 fetch failed" in out
    assert "[4]" not in res.text
    tail = next(ln for ln in out if ln.startswith("… 4 more results:"))
    assert "bad" not in tail and "https://p.example/7" in tail


def test_pplx_snippet_text_goes_through_text_compressor() -> None:
    body = "\n".join(f"page {chr(97 + i % 26)}{chr(97 + i // 26 % 26)} row" for i in range(400))
    doc = {"results": [{"url": "https://p.example/1", "text": body, "tokens_count": 900}]}
    out = PplxCompressor().compress(json.dumps(doc), AGGRESSIVE, NO_CTX).text
    assert "lines elided]" in out and len(out) < len(body)


def test_pplx_tail_placeholder_without_url_or_title() -> None:
    doc = [_result(i) for i in range(4)] + [{"summary": "no id"}, {"summary": "no id either"}]
    out = PplxCompressor().compress(json.dumps(doc, indent=2), AGGRESSIVE, NO_CTX).text
    assert "… 3 more results: https://example.com/3, #5, #6" in out


def test_pplx_keeps_unknown_scalar_keys_verbatim() -> None:
    doc = {"model": "sonar", "cost": 0.5, "flag": False, "nest": {"a": 1}, "hits": [_hit(1)]}
    out = PplxCompressor().compress(json.dumps(doc, indent=2), LIGHT, NO_CTX).text
    assert "model: sonar" in out and "cost: 0.5" in out and "flag: false" in out
    assert "nest" not in out
