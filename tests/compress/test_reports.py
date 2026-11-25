"""Test-report, compiler and build-log compressors."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

import pytest

from token_sparen.compress import CompressContext, Level, TextCompressor, is_error_line, registry
from token_sparen.compress.reports import (
    BuildLogCompressor,
    CompilerCompressor,
    TestReportCompressor,
)
from token_sparen.config import Config

FIX = Path(__file__).parent.parent / "fixtures" / "reports"
LIGHT = Level(head=80, tail=40, keep_ratio=0.6)
STANDARD = Level(head=40, tail=20, keep_ratio=0.35)
AGGRESSIVE = Level(head=15, tail=10, keep_ratio=0.15)
NO_CTX = CompressContext(prompt=None, recent_paths=())


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


NOISE = "".join(f"  CC obj/unit{i}.o\n" for i in range(60))  # dropped build chatter


def _run(comp: object, name: str, level: Level = STANDARD, ctx: CompressContext = NO_CTX) -> str:
    text = _fx(name)
    if isinstance(comp, CompilerCompressor):
        text = NOISE + text
    res = comp.compress(text, level, ctx)  # type: ignore[attr-defined]
    return str(res.text)


def _errors_survive(src: str, out: str) -> None:
    out_lines = set(out.split("\n"))
    for line in src.split("\n"):
        if is_error_line(line) and not re.fullmatch(r"=+ (?:FAILURES|ERRORS) =+", line):
            assert line in out_lines, f"error line lost: {line!r}"


def test_registered() -> None:
    assert isinstance(registry.for_type("test_report"), TestReportCompressor)
    assert isinstance(registry.for_type("compiler"), CompilerCompressor)
    assert isinstance(registry.for_type("build_log"), BuildLogCompressor)


# ------------------------------------------------------------ test reports


@pytest.mark.parametrize("level", [LIGHT, STANDARD, AGGRESSIVE])
def test_pytest_failures_preserved(level: Level) -> None:
    out = _run(TestReportCompressor(), "pytest_fail.txt", level)
    for needle in (
        "test_model_roundtrip[json-utf8]",
        "test_cache_eviction",
        "FAILED tests/test_models.py::test_model_roundtrip[json-utf8] - AssertionError",
        "FAILED tests/test_cache.py::test_cache_eviction - KeyError: 'k0'",
        "2 failed, 140 passed, 1 warning in 4.82s",
        "E       AssertionError: assert Model(id=3) == Model(id=4)",
        "E           KeyError: 'k0'",
    ):
        assert needle in out
    _errors_survive(_fx("pytest_fail.txt"), out)
    assert "[138 passed tests elided]" not in out
    assert re.search(r"\[140 passed tests elided\]", out)


def test_pytest_block_lines_depend_on_level() -> None:
    light = _run(TestReportCompressor(), "pytest_fail.txt", LIGHT)
    aggr = _run(TestReportCompressor(), "pytest_fail.txt", AGGRESSIVE)
    assert len(light) > len(aggr)
    assert "lines elided]" in aggr
    assert "step_1 = prepare(model, 1)" in light
    assert "step_7 = prepare(model, 7)" not in aggr


def test_pytest_error_line_beyond_block_lines_kept_with_gap() -> None:
    out = _run(TestReportCompressor(), "pytest_fail.txt", AGGRESSIVE)
    lines = out.split("\n")
    i = lines.index("E           KeyError: 'k0'")
    assert re.match(r"… \[\d+ lines elided\]", lines[i - 1])


def test_all_pass_report_is_one_line_summary() -> None:
    res = TestReportCompressor().compress(_fx("pytest_pass.txt"), STANDARD, NO_CTX)
    body = res.text.rstrip("\n")
    assert len(body.split("\n")) == 1
    assert "142 passed in 3.10s" in body
    assert res.total_lines == len(_fx("pytest_pass.txt").rstrip("\n").split("\n"))


def test_jest_failures_preserved() -> None:
    out = _run(TestReportCompressor(), "jest_fail.txt")
    for needle in (
        "FAIL src/store/cart.test.ts",
        "● Cart › applies discount code",
        "● Cart › rejects expired code",
        "Expected: 90",
        "TypeError: Cannot read properties of undefined (reading 'expires')",
        "Tests:       2 failed, 60 passed, 62 total",
        "Test Suites: 1 failed, 12 passed, 13 total",
    ):
        assert needle in out
    assert "PASS src/components" not in out
    assert "[60 passed tests elided]" in out
    _errors_survive(_fx("jest_fail.txt"), out)


def test_go_failures_preserved() -> None:
    out = _run(TestReportCompressor(), "go_fail.txt")
    for needle in (
        "--- FAIL: TestParseRange (0.00s)",
        "    parse_test.go:41: got 3, want 4",
        "    --- FAIL: TestDial/timeout (1.00s)",
        "        dial_test.go:19: timeout waiting for peer",
        "--- FAIL: TestDial (1.00s)",
        "FAIL\texample.com/demo/parse\t0.012s",
    ):
        assert needle in out
    assert "TestOk7" not in out
    assert "[30 passed tests elided]" in out
    _errors_survive(_fx("go_fail.txt"), out)


def test_cargo_failures_preserved() -> None:
    out = _run(TestReportCompressor(), "cargo_fail.txt")
    for needle in (
        "test mod_b::bad_sum ... FAILED",
        "test mod_b::bad_parse ... FAILED",
        "---- mod_b::bad_sum stdout ----",
        "---- mod_b::bad_parse stdout ----",
        "assertion `left == right` failed",
        "called `Result::unwrap()` on an `Err` value: ParseIntError { kind: InvalidDigit }",
        "test result: FAILED. 22 passed; 2 failed; 1 ignored",
    ):
        assert needle in out
    assert "mod_a::case_3" not in out
    assert "[22 passed tests elided]" in out
    _errors_survive(_fx("cargo_fail.txt"), out)


def test_test_report_unknown_runner_falls_back_to_text() -> None:
    text = "\n".join(f"line {i} of some unrelated output" for i in range(300)) + "\n"
    assert TestReportCompressor().compress(text, STANDARD, NO_CTX) == TextCompressor().compress(
        text, STANDARD, NO_CTX
    )


def test_block_lines_knob_from_config() -> None:
    cfg = Config(data={"compression": {"test_report": {"block_lines": {"standard": 2}}}})
    ctx = CompressContext(prompt=None, recent_paths=(), config=cfg)
    small = _run(TestReportCompressor(), "pytest_fail.txt", STANDARD, ctx)
    default = _run(TestReportCompressor(), "pytest_fail.txt", STANDARD)
    assert len(small) < len(default)
    assert "test_model_roundtrip[json-utf8]" in small


def test_invalid_knob_falls_back_to_default() -> None:
    cfg = Config(data={"compression": {"test_report": {"block_lines": {"standard": "x"}}}})
    ctx = CompressContext(prompt=None, recent_paths=(), config=cfg)
    assert _run(TestReportCompressor(), "pytest_fail.txt", STANDARD, ctx) == _run(
        TestReportCompressor(), "pytest_fail.txt", STANDARD
    )


def test_passing_line_with_error_word_survives() -> None:
    text = _fx("pytest_fail.txt").replace(
        "tests/test_api.py ....", "tests/test_api.py error handling PASSED ....", 1
    )
    out = TestReportCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert "error handling PASSED" in out


# ---------------------------------------------------------------- compiler


def test_tsc_failures_preserved() -> None:
    out = _run(CompilerCompressor(), "tsc.txt")
    for needle in (
        "src/api/client.ts(12,5): error TS2322",
        "src/ui/Panel.tsx(40,13): error TS2339",
        "  Property 'title' is missing in type '{}'.",
        "src/api/client.ts(30,9): error TS7006",
        "warning TS6133",
    ):
        assert needle in out
    assert "[token-sparen: 3 errors, 1 warnings in 2 files]" in out
    # grouped by file in first-seen order: both client.ts diagnostics adjacent
    assert out.index("TS7006") < out.index("TS2339")


def test_rustc_failures_preserved() -> None:
    out = _run(CompilerCompressor(), "rustc.txt")
    for needle in (
        "error[E0382]: borrow of moved value: `v`",
        " --> src/main.rs:8:20",
        '8 |     println!("{:?}", v);',
        "error[E0308]: mismatched types",
        "error: aborting due to 2 previous errors; 1 warning emitted",
        "warning: unused variable: `idx`",
    ):
        assert needle in out
    assert "[token-sparen: 2 errors, 1 warnings in 2 files]" in out


def test_javac_failures_preserved() -> None:
    out = _run(CompilerCompressor(), "javac.txt")
    for needle in (
        "Main.java:5: error: cannot find symbol",
        "  symbol:   variable count",
        "Main.java:9: error: incompatible types: String cannot be converted to int",
        "Util.java:12: warning: [deprecation]",
        "2 errors",
    ):
        assert needle in out
    assert "[token-sparen: 2 errors, 1 warnings in 2 files]" in out


def test_gcc_failures_preserved() -> None:
    out = _run(CompilerCompressor(), "gcc.txt")
    for needle in (
        "util.h:4:10: fatal error: missing.h: No such file or directory",
        "main.c:14:12: error: 'x' undeclared (first use in this function)",
        "      |            ^",
        "make: *** [Makefile:7: main.o] Error 1",
    ):
        assert needle in out
    _errors_survive(_fx("gcc.txt"), out)
    assert "[token-sparen: 2 errors, 2 warnings in 2 files]" in out


def test_compiler_aggressive_counts_warnings_but_keeps_errors() -> None:
    src = _fx("gcc.txt")
    out = _run(CompilerCompressor(), "gcc.txt", AGGRESSIVE)
    assert "unused variable 'tmp'" not in out
    assert "main.c:14:12: error: 'x' undeclared" in out
    assert re.search(r"\[2 warnings elided in main\.c\]", out)
    _errors_survive(src, out)


def test_compiler_warning_with_error_word_survives_aggressive() -> None:
    text = NOISE + "a.c:1:1: warning: handler for Exception is unused\n   1 | x\n" + _fx("gcc.txt")
    out = CompilerCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert "a.c:1:1: warning: handler for Exception is unused" in out


def test_compiler_warnings_knob_override() -> None:
    cfg = Config(data={"compression": {"compiler": {"warnings": {"aggressive": "keep"}}}})
    ctx = CompressContext(prompt=None, recent_paths=(), config=cfg)
    assert "unused variable 'tmp'" in _run(CompilerCompressor(), "gcc.txt", AGGRESSIVE, ctx)


def test_compiler_snippet_cap_keeps_error_lines() -> None:
    body = "\n".join(f"   {i} | code" for i in range(12))
    text = NOISE + f"a.c:3:1: error: boom\n{body}\n      | ^ ValueError here\n"
    out = CompilerCompressor().compress(text, STANDARD, NO_CTX).text
    assert "a.c:3:1: error: boom" in out
    assert "ValueError here" in out
    assert "   11 | code" not in out
    assert "lines elided]" in out


def test_compiler_without_diagnostics_falls_back_to_text() -> None:
    text = "\n".join(f"plain line {i}" for i in range(300)) + "\n"
    assert CompilerCompressor().compress(text, STANDARD, NO_CTX) == TextCompressor().compress(
        text, STANDARD, NO_CTX
    )


# --------------------------------------------------------------- build log


def test_npm_failures_preserved() -> None:
    src = _fx("npm_build.txt")
    out = _run(BuildLogCompressor(), "npm_build.txt")
    for needle in (
        "npm WARN deprecated left-pad@1.0.0",
        "src/index.ts(3,1): error TS2304: Cannot find name 'foo'.",
        "npm ERR! code ELIFECYCLE",
        "npm ERR! detail line 2",
    ):
        assert needle in out
    assert "% fetching" not in out
    _errors_survive(src, out)
    assert len(out) < len(src)


def test_pip_failures_preserved() -> None:
    out = _run(BuildLogCompressor(), "pip_install.txt")
    assert "ERROR: Could not find a version that satisfies the requirement missingpkg==9.9" in out
    assert "ERROR: No matching distribution found for missingpkg==9.9" in out
    assert "Collecting pkg3>=1.0" not in out
    assert "Downloading pkg3" not in out


def test_gradle_failures_preserved() -> None:
    out = _run(BuildLogCompressor(), "gradle_build.txt")
    for needle in (
        "> Task :compileJava",
        "> Task :compileTestJava",
        "> Task :test",
        "FAILED: DemoTest > checks()",
        "org.opentest4j.AssertionFailedError at DemoTest.java:22",
        "BUILD FAILED in 14s",
    ):
        assert needle in out
    assert "Downloading https" not in out
    assert "Fetched artifact" not in out


def test_docker_failures_preserved() -> None:
    src = _fx("docker_build.txt")
    out = _run(BuildLogCompressor(), "docker_build.txt")
    for needle in (
        "#1 [internal] load build definition from Dockerfile",
        "Step 1/4 : FROM python:3.12-slim",
        "Step 2/4 : COPY . /app",
        "#6 [3/4] COPY . /app",
    ):
        assert needle in out
    assert "Pulling fs layer" not in out
    _errors_survive(src, out)


def test_build_log_last_20_lines_kept_and_cr_segments() -> None:
    lines = [f"compiling unit {i}" for i in range(100)]
    lines[10] = "progress 10%\rprogress 50%\rcompiled ok"
    out = BuildLogCompressor().compress("\n".join(lines) + "\n", STANDARD, NO_CTX).text
    out_lines = out.rstrip("\n").split("\n")
    assert out_lines[-20:] == lines[-20:]
    assert "progress 50%" not in out


def test_cr_segment_with_error_survives() -> None:
    text = "start\n" + "Building\rERROR: disk full\rBuilding more\n" + "x\n" * 60
    out = BuildLogCompressor().compress(text, STANDARD, NO_CTX).text
    assert "ERROR: disk full" in out


def test_build_log_warning_count_reported() -> None:
    lines = ["Step 1/2 : RUN make"] + [f"warning: thing {i}" for i in range(7)]
    lines += [f"filler {i}" for i in range(60)]
    out = BuildLogCompressor().compress("\n".join(lines) + "\n", STANDARD, NO_CTX).text
    assert "7 warnings" in out
    assert "warning: thing 3" not in out


# ------------------------------------------------------------ guard + perf


@pytest.mark.parametrize("cls", [TestReportCompressor, CompilerCompressor, BuildLogCompressor])
def test_never_raises_and_falls_back(
    cls: type, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path))
    text = _fx("pytest_fail.txt")

    def boom(self: object, *a: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cls, "_run", boom)
    res = cls().compress(text, STANDARD, NO_CTX)
    assert res == TextCompressor().compress(text, STANDARD, NO_CTX)


@pytest.mark.parametrize("cls", [TestReportCompressor, CompilerCompressor, BuildLogCompressor])
@pytest.mark.parametrize("level", [LIGHT, AGGRESSIVE])
def test_empty_and_garbage_inputs_do_not_raise(cls: type, level: Level) -> None:
    for text in ("", "\n", "x", "\x00\ud800 ERROR\r\r\n", "=" * 100, "a.c:1: error:\n"):
        res = cls().compress(text, level, NO_CTX)
        assert isinstance(res.text, str)


def _big(unit: str, size: int = 1024 * 1024) -> str:
    rng = random.Random(3)
    parts: list[str] = []
    total = 0
    i = 0
    while total < size:
        chunk = unit.replace("{i}", str(i)).replace("{r}", str(rng.randint(0, 99)))
        parts.append(chunk)
        total += len(chunk) + 1
        i += 1
    return "\n".join(parts)


@pytest.mark.perf
@pytest.mark.parametrize(
    ("cls", "unit"),
    [
        (TestReportCompressor, "tests/test_x{i}.py .......F....  [{r}%]\nE   assert {i}"),
        (
            CompilerCompressor,
            "src/f{r}.c:{i}:3: warning: unused 'v{i}'\n   {i} | int v;\n      |     ^",
        ),
        (BuildLogCompressor, "Downloading pkg{i}\n  {r}% done\nplain line {i}"),
    ],
)
def test_one_megabyte_under_100ms(cls: type, unit: str) -> None:
    text = "=== test session starts ===\n" + _big(unit) + "\n=== 1 failed, 5 passed in 1s ===\n"
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        res = cls().compress(text, STANDARD, NO_CTX)
        best = min(best, time.perf_counter() - start)
    assert isinstance(res.text, str)
    assert best < 0.1, f"{cls.__name__}: {best * 1000:.0f} ms"


# ------------------------------------------------------------- fix round 1


@pytest.mark.parametrize("level", [LIGHT, STANDARD, AGGRESSIVE])
def test_javac_column_zero_snippet_kept(level: Level) -> None:
    text = NOISE + (
        "Foo.java:3: error: cannot find symbol\nclass Foo extends Missing {\n"
        "^\n  symbol: class Missing\n  location: package demo\n"
        "Bar.java:7: warning: [unchecked] unchecked call\nList x = new ArrayList();\n^\n"
        "1 error\n1 warning\n"
    )
    out = CompilerCompressor().compress(text, level, NO_CTX).text
    for needle in (
        "class Foo extends Missing {",
        "^",
        "  symbol: class Missing",
        "  location: package demo",
        "1 error",
    ):
        assert needle in out.split("\n")
    assert "[token-sparen: 1 errors, 1 warnings in 2 files]" in out


def test_compiler_no_growth_returns_input_unchanged() -> None:
    text = "a.c:3:1: error: boom\n   3 | x\n      | ^\n"
    res = CompilerCompressor().compress(text, STANDARD, NO_CTX)
    assert res.text == text
    assert res.lossless


def test_tsc_pretty_format() -> None:
    text = NOISE + (
        "src/a.ts:12:5 - error TS2322: Type 'string' is not assignable to type 'number'.\n\n"
        "12     const n: number = 'x';\n       ~\n\n"
        "src/b.ts:4:1 - warning TS6133: 'y' is declared but never used.\n\n"
        "4 const y = 1;\n  ~\n\nFound 1 error in src/a.ts:12\n"
    )
    out = CompilerCompressor().compress(text, STANDARD, NO_CTX).text
    assert "src/a.ts:12:5 - error TS2322" in out
    assert "12     const n: number = 'x';" in out
    assert "src/b.ts:4:1 - warning TS6133" in out
    assert "[token-sparen: 1 errors, 1 warnings in 2 files]" in out


@pytest.mark.parametrize("level", [LIGHT, STANDARD, AGGRESSIVE])
def test_pytest_section_headers_not_reemitted(level: Level) -> None:
    out = _run(TestReportCompressor(), "pytest_fail.txt", level)
    assert "= FAILURES =" not in out
    assert "short test summary info" not in out
    lines = out.split("\n")
    summary = next(i for i, ln in enumerate(lines) if "2 failed, 140 passed" in ln)
    block = next(
        i for i, ln in enumerate(lines) if ln.startswith("_") and "test_cache_eviction" in ln
    )
    short = next(i for i, ln in enumerate(lines) if ln.startswith("FAILED tests/test_cache.py"))
    assert summary < block < short


# ------------------------------------------------------------- fix round 2


@pytest.mark.parametrize("level", [LIGHT, STANDARD, AGGRESSIVE])
@pytest.mark.parametrize(
    "src_line", ["make[i] = 3;", "3 warnings = x;", "Found 2 errors = y;", "1 error = z;"]
)
def test_source_lines_that_look_like_chatter_keep_snippet(level: Level, src_line: str) -> None:
    text = NOISE + (
        f"Foo.java:3: error: cannot find symbol\n{src_line}\n^\n  symbol: variable make\n"
        "  location: class Foo\n"
    )
    out = CompilerCompressor().compress(text, level, NO_CTX).text.split("\n")
    for needle in (src_line, "^", "  symbol: variable make", "  location: class Foo"):
        assert needle in out


@pytest.mark.parametrize(
    "chatter",
    [
        "make[2]: *** [all] Error 2",
        "make: *** [Makefile:7: main.o] Error 1",
        "2 errors",
        "1 warning",
        "3 errors generated.",
        "Found 3 errors in 2 files.",
        "compilation terminated.",
    ],
)
def test_real_chatter_still_ends_diagnostic(chatter: str) -> None:
    text = NOISE + f"a.c:3:1: warning: odd\n   3 | x\n{chatter}\nsrc after\n"
    out = CompilerCompressor().compress(text, AGGRESSIVE, NO_CTX).text
    assert "src after" not in out
