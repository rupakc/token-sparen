"""TextCompressor, levels, relevance and registry."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from userconf import to_user_config

from token_sparen import config as config_mod
from token_sparen.compress import (
    ERROR_PATTERNS,
    CompressContext,
    CompressResult,
    Level,
    TextCompressor,
    is_error_line,
    load_level,
    registry,
    relevance,
    unwrap,
)
from token_sparen.compress.base import Registry, error_line_indices
from token_sparen.config import load_config

LEVELS = {
    "light": Level(head=80, tail=40, keep_ratio=0.6),
    "standard": Level(head=40, tail=20, keep_ratio=0.35),
    "aggressive": Level(head=15, tail=10, keep_ratio=0.15),
}
NO_CTX = CompressContext(prompt=None, recent_paths=())
WORDS = ["alpha", "beta", "gamma", "delta", "omega", "lorem", "ipsum", "kappa", "zeta"]


def _filler(rng: random.Random, n: int) -> list[str]:
    return [" ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 8))) for _ in range(n)]


def _compress(text: str, arm: str = "standard", ctx: CompressContext = NO_CTX) -> CompressResult:
    return TextCompressor().compress(text, LEVELS[arm], ctx)


# --- levels ---------------------------------------------------------------------------------


def test_levels_loaded_from_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    for arm, level in LEVELS.items():
        assert load_level(cfg, arm) == level
    with pytest.raises(ValueError):
        load_level(cfg, "off")


def test_level_override_from_user_config(tmp_path: Path) -> None:
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "h"),
        "TOKEN_SPAREN_COMPRESSION__LEVELS__LIGHT__HEAD": "5",
    }
    env = to_user_config(env)  # head is user-only: a TOKEN_SPAREN_* variable is ignored
    assert load_level(load_config(tmp_path, env), "light") == Level(5, 40, 0.6)


# --- error patterns -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: boom",
        "an error occurred",
        "Traceback (most recent call last):",
        "ValueError: bad",
        "java.lang.IllegalStateException: x",
        "tests/test_x.py::test_a FAILED",
        "fatal: not a git repository",
        "thread 'main' panicked at src/main.rs",
        "panic: runtime error",
        "AssertionError",
        "assert x == 1",
        "✗ should render",
        "FAIL src/app.test.js",
        "--- FAIL: TestFoo (0.00s)",
        "Exception in thread main",
        # v1 review M10: failure wording outside the error/fail vocabulary
        "npm ERR! code ELIFECYCLE",
        "npm ERR! errno 1",
        "Segmentation fault (core dumped)",
        "zsh: segmentation fault  ./a.out",
        "program segfaulted at 0x0",
        "Killed",
        "  Killed: 9",
        "/bin/sh: line 1:  4242 Killed                  make -j8",
        "[1]+  4242 Killed",
        "Out of memory: Killed process 4412 (java)",
        "Killed (signal 9)",
        "cp: cannot open 'x': Permission denied",
        "zsh: permission denied: ./run.sh",
        "Permission denied (publickey).",
        "Error: EACCES: open '/srv/x'",
        "npm WARN checkPermissions EACCES /usr/lib",
        "Access is denied.",
        "access denied for user 'app'",
        "main.o: undefined reference to `vec_push'",
        "bash: cargo: command not found",
    ],
)
def test_error_patterns_match(line: str) -> None:
    assert is_error_line(line)
    assert any(p.search(line) for p in ERROR_PATTERNS)


@pytest.mark.parametrize(
    "line",
    [
        "terrorist cell",
        "all good here",
        "assertive tone",
        "step 3",
        "skilled worker",
        "the job was killed by the user",
        "    Killed the flaky retry loop in the scheduler",  # prose in a git log (review M-a)
        "Killed jobs are retried twice",
        "the permission denied path is covered by tests",
        "docs: explain the Permission denied case",
        "ERRATA",
        "permissions denied-list",
        "segmentational",
    ],
)
def test_error_patterns_word_bounded(line: str) -> None:
    assert not is_error_line(line)


def test_error_patterns_are_compiled() -> None:
    assert ERROR_PATTERNS and all(isinstance(p, re.Pattern) for p in ERROR_PATTERNS)


# --- core guarantees ------------------------------------------------------------------------


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2**32 - 1), index=st.integers(0, 4999))
def test_error_lines_never_dropped(seed: int, index: int) -> None:
    rng = random.Random(seed)
    lines = _filler(rng, 5000)
    lines[index] = "ERROR: boom"
    text = "\n".join(lines)
    for arm in LEVELS:
        res = _compress(text, arm)
        out = res.text.split("\n")
        assert "ERROR: boom" in out
        assert res.total_lines == 5000
        assert res.kept_lines < 5000
        assert not res.lossless
        # two lines of context on each side survive too
        for j in range(max(0, index - 2), min(5000, index + 3)):
            assert lines[j] in res.text


def test_many_error_lines_exceed_budget_but_all_kept() -> None:
    lines = [f"ERROR: failure number {i} in module x{i % 7}" for i in range(0, 3000, 3)]
    body: list[str] = []
    rng = random.Random(1)
    for err in lines:
        body.extend([err, *_filler(rng, 2)])
    res = _compress("\n".join(body), "aggressive")
    for err in lines:
        assert err in res.text
    assert res.kept_lines > int(0.15 * len(body))


def test_fold_repeated_lines() -> None:
    text = "\n".join(f"step {i}" for i in range(1, 501))
    res = _compress(text)
    out = res.text.split("\n")
    assert out == ["step 1  [× 500 similar lines]"]
    assert res.kept_lines == 1
    assert res.total_lines == 500
    assert not res.lossless


def test_fold_requires_three_and_skips_error_lines() -> None:
    rng = random.Random(3)
    filler = _filler(rng, 200)
    block = ["retry 1", "retry 2", "x", "warn 1 done", "warn 2 done", "warn 3 done"]
    errs = ["ERROR code 1", "ERROR code 2", "ERROR code 3"]
    text = "\n".join(filler[:100] + block + errs + filler[100:])
    res = _compress(text)
    assert "warn 1 done  [× 3 similar lines]" in res.text
    assert "retry 1  [×" not in res.text
    for e in errs:
        assert e in res.text.split("\n")


def test_order_preserved() -> None:
    rng = random.Random(7)
    lines = [f"{i:05d} {w}" for i, w in enumerate(_filler(rng, 3000))]
    lines[1500] = "01500 Traceback here"
    res = _compress("\n".join(lines), "standard")
    kept = [ln for ln in res.text.split("\n") if not ln.startswith("…")]
    nums = [int(ln[:5]) for ln in kept]
    assert nums == sorted(nums)
    assert len(set(nums)) == len(nums)
    assert res.kept_lines == len(kept)


def test_elision_marker_lines() -> None:
    rng = random.Random(11)
    lines = _filler(rng, 1000)
    res = _compress("\n".join(lines), "aggressive")
    gaps = [ln for ln in res.text.split("\n") if ln.startswith("… [")]
    assert gaps
    total_elided = sum(int(re.findall(r"\d+", g)[0]) for g in gaps)
    assert total_elided + res.kept_lines == 1000
    assert all(re.fullmatch(r"… \[\d+ lines elided\]", g) for g in gaps)


def test_budget_is_max_of_head_tail_and_ratio() -> None:
    rng = random.Random(5)
    text = "\n".join(f"{i} " + w for i, w in enumerate(_filler(rng, 1000)))
    assert _compress(text, "aggressive").kept_lines == 150
    assert _compress(text, "standard").kept_lines == 350
    small = "\n".join(f"{i} " + w for i, w in enumerate(_filler(rng, 100)))
    assert _compress(small, "aggressive").kept_lines == 25


def test_head_and_tail_kept() -> None:
    rng = random.Random(9)
    lines = [f"L{i} {w}" for i, w in enumerate(_filler(rng, 2000))]
    out = _compress("\n".join(lines), "aggressive").text.split("\n")
    assert out[:15] == lines[:15]
    assert out[-10:] == lines[-10:]


def test_short_text_unchanged_lossless() -> None:
    text = "\n".join(f"line {i} unique{i * 7}" for i in range(60)) + "\n"
    res = _compress(text, "standard")
    assert res == CompressResult(text=text, kept_lines=60, total_lines=60, lossless=True)


def test_trailing_newline_preserved() -> None:
    rng = random.Random(2)
    text = "\n".join(_filler(rng, 500)) + "\n"
    res = _compress(text, "aggressive")
    assert res.text.endswith("\n")
    assert res.total_lines == 500


def test_empty_text() -> None:
    res = _compress("")
    assert res.lossless and res.text == ""


def test_pinned_cover_everything_is_lossless() -> None:
    text = "\n".join(f"error {i} in unit{i}" for i in range(200))
    # digit runs differ but every line is an error line, so nothing folds or elides
    res = _compress(text, "aggressive")
    assert res.lossless
    assert res.text == text


# --- relevance ------------------------------------------------------------------------------


def test_relevance_prefers_prompt_terms() -> None:
    rng = random.Random(13)
    lines = _filler(rng, 2000)
    lines[1000] = "the frobnicator config was loaded from settings"
    lines[1400] = "loaded widget.py successfully"
    text = "\n".join(lines)
    ctx = CompressContext(prompt="why is the frobnicator broken?", recent_paths=("src/widget.py",))
    res = _compress(text, "aggressive", ctx)
    assert lines[1000] in res.text
    assert lines[1400] in res.text
    plain = _compress(text, "aggressive", NO_CTX)
    assert lines[1000] not in plain.text


def test_relevance_score_function() -> None:
    ctx = CompressContext(prompt="Fix the parser bug", recent_paths=("a/b/lexer.py",))
    corpus = relevance.Corpus(["parser fails here", "nothing to see", "lexer.py line 3"], ctx)
    assert corpus.score("parser fails here") > corpus.score("nothing to see") == 0.0
    assert corpus.score("lexer.py line 3") > 0.0
    assert relevance.score("the parser", ctx) > 0.0
    assert relevance.score("unrelated", ctx) == 0.0
    assert relevance.score("x", NO_CTX) == 0.0
    assert relevance.query_terms(NO_CTX) == frozenset()
    assert "lexer" in relevance.query_terms(ctx)


# --- registry -------------------------------------------------------------------------------


class _Fake:
    name = "fake"
    handles = frozenset({"json"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return CompressResult(text="{}", kept_lines=1, total_lines=1, lossless=False)


class _EP:
    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def test_registry_fallback_and_register() -> None:
    reg = Registry(entry_points_fn=lambda: [])
    assert isinstance(reg.for_type("json"), TextCompressor)
    fake = _Fake()
    reg.register(fake)
    assert reg.for_type("json") is fake
    assert isinstance(reg.for_type("diff"), TextCompressor)


def test_registry_module_level_object() -> None:
    assert isinstance(registry, Registry)
    assert registry.for_type("text").name == "text"


def test_registry_entry_points_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        config_mod, "log_error", lambda env, where, msg: logged.append((where, msg))
    )

    def eps() -> list[_EP]:
        calls.append("x")
        return [_EP("bad", RuntimeError("nope")), _EP("cls", _Fake), _EP("junk", 42)]

    reg = Registry(entry_points_fn=eps)
    assert calls == []
    assert isinstance(unwrap(reg.for_type("json")), _Fake)  # extensions run safety-checked
    reg.for_type("text")
    assert calls == ["x"]
    assert len(logged) == 2
    assert all(where == "compress.registry" for where, _ in logged)
    assert "bad" in logged[0][1] and "junk" in logged[1][1]


def test_registry_entry_point_discovery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(config_mod, "log_error", lambda env, where, msg: logged.append(msg))

    def boom() -> list[_EP]:
        raise OSError("metadata broken")

    assert isinstance(Registry(entry_points_fn=boom).for_type("log"), TextCompressor)
    assert logged and "metadata broken" in logged[0]


def test_registry_default_entry_points_group() -> None:
    reg = Registry()
    assert isinstance(reg.for_type("table"), TextCompressor)


# --- properties -----------------------------------------------------------------------------

_PIECES = st.sampled_from(
    [
        "error",
        "ERROR",
        "Error",
        "terror",
        "fail",
        "ERR!",
        "ERR",
        "npm ",
        "!",
        "Killed",
        "killed",
        "sKilled",
        "segfault",
        "Segmentation fault",
        "segmentation",
        " fault",
        "Permission denied",
        "Killed process 4",
        "Killed (signal",
        ": ",
        "\t",
        "  ",
        "9",
        "permission ",
        "denied",
        "EACCES",
        "eacces",
        "Access is denied",
        "access denied",
        "undefined reference",
        "command not found",
        "FAILED",
        "panic",
        "assert",
        "aſſert",
        "✗",
        "ValueError",
        "exceptional",
        "x",
        "1",
        "22",
        "#",
        " ",
        ":",
        "é",
        "\x00",
        "\udcff",
        "step",
        # frame heads judged without their file location (M4 triage)
        '  File "',
        '", line 3',
        "\tat ",
        "    at ",
        "fn (",
        "/errors.js:1:2",
        ")",
        "\u00a0",
        "\r",
    ]
)
_LINE = st.lists(_PIECES, max_size=6).map("".join)


@settings(max_examples=200, deadline=None)
@given(st.lists(_LINE, max_size=60))
def test_anchor_scan_matches_regexes(lines: list[str]) -> None:
    text = "\n".join(lines)
    expected = [i for i, ln in enumerate(text.split("\n")) if is_error_line(ln)]
    assert error_line_indices(text) == expected


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_LINE, min_size=0, max_size=400), st.sampled_from(sorted(LEVELS)))
def test_invariants_on_arbitrary_text(lines: list[str], arm: str) -> None:
    text = "\n".join(lines)
    res = _compress(text, arm, CompressContext(prompt="panic step", recent_paths=("a/x.py",)))
    src = text.removesuffix("\n").split("\n")
    out = res.text.removesuffix("\n").split("\n")
    # every error line survives verbatim, and kept lines appear in original order
    pos = 0
    for i in error_line_indices(text):
        pos = out.index(src[i], pos) + 1
    if res.lossless:
        assert res.text == text
        return
    accounted = 0
    for ln in out:
        gap = re.fullmatch(r"… \[(\d+) lines elided\]", ln)
        fold = re.fullmatch(r".*  \[× (\d+) similar lines\]", ln, flags=re.DOTALL)
        if gap:
            accounted += int(gap.group(1))
        elif fold and ln not in src:
            accounted += int(fold.group(1))
        else:
            accounted += 1
    assert accounted == res.total_lines == len(src)


@pytest.mark.parametrize(
    ("a", "b", "similar"),
    [
        ("step 9", "step 10", True),
        ("#", "1", False),
        ("a1b", "ab", False),
        ("x 12 y 3", "x 4 y 56", True),
        ("x 1", "x 1 ", False),
    ],
)
def test_digit_normalised_equality(a: str, b: str, similar: bool) -> None:
    from token_sparen.compress.text import _neighbour_equal

    assert _neighbour_equal(f"{a}\n{b}".encode()) == bytearray([1 if similar else 0])


def test_budget_larger_than_units_keeps_everything_folded() -> None:
    rng = random.Random(23)
    lines = [f"{i} {w}" for i, w in enumerate(_filler(rng, 50))] + [f"dup {i}" for i in range(50)]
    res = TextCompressor().compress("\n".join(lines), Level(2, 2, 0.99), NO_CTX)
    out = res.text.split("\n")
    assert out[:50] == lines[:50]
    assert out[50:] == ["dup 0  [× 50 similar lines]"]
    assert res.kept_lines == 51 and not res.lossless


@pytest.mark.parametrize(
    "body",
    [
        'head = "x"',
        "keep_ratio = nan",
        "keep_ratio = inf",
        "keep_ratio = -0.1",
        "keep_ratio = 1.5",
        "head = -1",
        "tail = -3",
        "head = 2.5",
        "head = true",
        'keep_ratio = "0.5"',
    ],
)
def test_invalid_level_falls_back_to_defaults(
    tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[str] = []
    monkeypatch.setattr(config_mod, "log_error", lambda env, where, msg: logged.append(where))
    proj = tmp_path / "p"
    proj.mkdir()
    (tmp_path / "h").mkdir()  # user config: the loader validates what it is given
    (tmp_path / "h" / "config.toml").write_text(
        f"[compression.levels.light]\n{body}\n", encoding="utf-8"
    )
    cfg = load_config(proj, {"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    assert load_level(cfg, "light") == LEVELS["light"]
    assert logged == ["compress.level"]


def test_valid_level_edge_values(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "config.toml").write_text(
        "[compression.levels.light]\nhead = 0\ntail = 0\nkeep_ratio = 1.0\n"
    )
    cfg = load_config(proj, {"TOKEN_SPAREN_HOME": str(tmp_path / "h")})
    assert load_level(cfg, "light") == Level(0, 0, 1.0)


def test_error_dense_output_returned_unchanged() -> None:
    text = "\n".join(f"ERROR: failure {i} in unit" for i in range(5000))
    assert _compress(text, "aggressive") == CompressResult(text, 5000, 5000, True)
    rng = random.Random(31)
    lines = _filler(rng, 5000)
    for i in range(0, 5000, 5):
        lines[i] = f"ERROR {i} here"
    text = "\n".join(lines)
    assert _compress(text, "aggressive").text == text


def test_anchor_false_positives_still_compress() -> None:
    rng = random.Random(37)
    lines = [f"terror {w} terrorism failover" for w in _filler(rng, 3000)]
    res = _compress("\n".join(lines), "aggressive")
    assert res.kept_lines == 450 and not res.lossless


def test_relevance_can_spend_whole_budget() -> None:
    rng = random.Random(29)
    lines = [f"frob {w}" for w in _filler(rng, 1000)]
    ctx = CompressContext(prompt="frob", recent_paths=())
    res = _compress("\n".join(lines), "aggressive", ctx)
    assert res.kept_lines == 150


def test_keep_ratio_one_is_lossless() -> None:
    rng = random.Random(41)
    text = "\n".join(f"{i} {w}" for i, w in enumerate(_filler(rng, 300)))
    assert TextCompressor().compress(text, Level(1, 1, 1.0), NO_CTX).lossless
