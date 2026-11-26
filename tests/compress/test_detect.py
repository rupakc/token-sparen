"""Content-type detection."""

from __future__ import annotations

import importlib
import json
import time
from typing import get_args

import pytest

from token_sparen import config as config_mod
from token_sparen.compress import ContentType, detect
from token_sparen.compress.detect import command_hint, sniff
from token_sparen.events import ToolCall


def _shell(cmd: str) -> ToolCall:
    return ToolCall(name="Bash", category="shell", input={"command": cmd}, use_id="u1")


PLAIN = "just some words\nand more words here\nnothing special at all\n"

FIXTURES: dict[ContentType, str] = {
    "json": json.dumps({"items": [{"id": i, "name": f"n{i}"} for i in range(20)]}, indent=2),
    "diff": (
        "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    ),
    "stacktrace": (
        "Traceback (most recent call last):\n"
        '  File "app.py", line 3, in <module>\n'
        "    main()\n"
        "ValueError: bad value\n"
    ),
    "test_report": (
        "============================= test session starts ==============================\n"
        "collected 3 items\n\ntests/test_a.py ..F\n"
        "========================= 1 failed, 2 passed in 0.12s ==========================\n"
    ),
    "compiler": "src/app.ts(3,7): error TS2322: Type 'string' is not assignable to 'number'.\n",
    "build_log": (
        "Collecting requests\n  Downloading requests-2.0.tar.gz\nSuccessfully installed requests\n"
    ),
    "search": "src/a.py:10:def foo():\nsrc/b.py:22:    foo()\nsrc/c.py:5:import foo\n",
    "pplx": "Some answer text\nwith citations\n",
    "log": (
        "2026-11-25T10:00:00Z INFO start\n"
        "2026-11-25T10:00:01Z INFO step\n"
        "2026-11-25 10:00:02,123 WARN slow\n"
        "continuation line\n"
    ),
    "table": "name,age,city\nann,30,rome\nbob,41,oslo\ncid,22,lima\n",
    "text": PLAIN,
}
detect_mod = importlib.import_module("token_sparen.compress.detect")

HINTS: dict[ContentType, ToolCall | None] = {"pplx": _shell("pplx 'what is x'")}


def test_detect_table() -> None:
    """One fixture per ContentType, and every ContentType is covered."""
    assert set(FIXTURES) == set(get_args(ContentType))
    for ctype, text in FIXTURES.items():
        assert detect(text, HINTS.get(ctype)) == ctype, ctype


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("pytest -q", "test_report"),
        ("FOO=1 BAR=2 pytest tests/", "test_report"),
        ("cd sub && python -m pytest -x", "test_report"),
        ("python3 -m pytest", "test_report"),
        ("npm test", "test_report"),
        ("npm run test", "test_report"),
        ("npx jest --ci", "test_report"),
        ("go test ./...", "test_report"),
        ("cargo test", "test_report"),
        ("tsc --noEmit", "compiler"),
        ("rustc main.rs", "compiler"),
        ("javac A.java", "compiler"),
        ("gcc -O2 a.c", "compiler"),
        ("/usr/bin/clang a.c", "compiler"),
        ("cargo build --release", "compiler"),
        ("mvn compile", "compiler"),
        ("npm install", "build_log"),
        ("npm ci", "build_log"),
        ("pip install -e .", "build_log"),
        ("python -m pip install x", "build_log"),
        ("./gradlew build", "build_log"),
        ("gradle assemble", "build_log"),
        ("docker build -t x .", "build_log"),
        ("rg foo", "search"),
        ("grep -rn foo src", "search"),
        ("pplx ask", "pplx"),
        ("git diff HEAD~1", "diff"),
        ("git show HEAD~1", "diff"),
        ("git show abc1234", "diff"),
        ("git show --format=fuller abc1234 | head -500", "diff"),
        ("git show abc1234 -- src/app.py", "diff"),
        ("git show HEAD:src/app.py", None),
        ("git show abc1234:README.md", None),
        ("git show --stat HEAD", None),
        ("git log -p", "diff"),
        ("git log -p -n 300", "diff"),
        ("git log --patch -5", "diff"),
        ("git log", None),
        ("git log --oneline -p", "diff"),
        ("git stash show -p", "diff"),
        ("git stash show -p stash@{1}", "diff"),
        ("git stash show", None),
        ("git stash list", None),
        ("uv run pytest -q", "test_report"),
        ("sudo time npm i", "build_log"),
        ("npm run build", None),
        ("pytest | tail -5", "test_report"),
        ("ls -la", None),
        ("", None),
        ("cd only", None),
        ("go build", None),
        ("python script.py", None),
        ("'unterminated", None),
    ],
)
def test_command_hint(cmd: str, expected: ContentType | None) -> None:
    assert command_hint(cmd) == expected


def test_hint_wins_over_content() -> None:
    assert detect(PLAIN, _shell("pytest")) == "test_report"
    assert detect(PLAIN, _shell("rg x")) == "search"
    assert detect(PLAIN, ToolCall("Grep", "search", {"pattern": "x"}, None)) == "search"


def test_no_hint_falls_back_to_sniffing() -> None:
    assert detect(FIXTURES["table"], _shell("ls")) == "table"
    assert detect(FIXTURES["table"], ToolCall("Read", "read", {"file_path": "a"}, None)) == "table"
    assert detect(FIXTURES["table"], ToolCall("Bash", "shell", {"command": 3}, None)) == "table"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@@ -1,2 +1,2 @@\n-a\n+b\n", "diff"),
        ('Exception in thread "main" java.lang.NullPointerException\n', "stacktrace"),
        (
            "TypeError: x is undefined\n    at f (/srv/a.js:4:11)\n    at /srv/b.js:9:3\n",
            "stacktrace",
        ),
        ("Error: boom\n\n    at Object.<anonymous> (/srv/a.js:1:7)\n", "stacktrace"),
        ("TypeError: x\nn\nn\nn\nn\n    at f (/a.js:4:11)\n", "text"),
        ("java.lang.IllegalStateException: x\n\tat com.x.Foo.bar(Foo.java:12)\n", "stacktrace"),
        ("PASS src/a.test.js\nFAIL src/b.test.js\n  ● renders\n", "test_report"),
        ("--- FAIL: TestFoo (0.00s)\nFAIL\nFAIL\tpkg/x\t0.01s\n", "test_report"),
        ("running 3 tests\ntest a ... ok\n\ntest result: ok. 3 passed\n", "test_report"),
        ("error[E0308]: mismatched types\n --> src/main.rs:2:5\n", "compiler"),
        ("main.c:3:5: error: expected ';'\n", "compiler"),
        ("npm ERR! code ENOENT\n", "build_log"),
        ("Step 2/5 : RUN make\n ---> abc\n", "build_log"),
        ("> Task :app:compileJava\nBUILD SUCCESSFUL in 3s\n", "build_log"),
        ("Nov 25 10:00:00 host sshd[1]: ok\nNov 25 10:00:01 host sshd[1]: ok\n", "log"),
        ("[2026-11-25 10:00:00] a\n[2026-11-25 10:00:01] b\n", "log"),
        ("a\tb\tc\n1\t2\t3\n4\t5\t6\n", "table"),
        ("| a | b |\n|---|---|\n| 1 | 2 |\n", "table"),
        ("[1, 2, 3]", "json"),
        ("{not json at all\nreally\n", "text"),
        ("", "text"),
        ("one line, with a comma", "text"),
        ("\x00\udcff binary-ish \ud800 stuff\n", "text"),
    ],
)
def test_content_sniffing(text: str, expected: ContentType) -> None:
    assert detect(text, None) == expected


def test_json_over_4mb_not_parsed() -> None:
    big = "[" + ",".join(["1"] * 2_100_000) + "]"
    assert len(big) > 4 * 1024 * 1024
    assert detect(big, None) != "json"


def test_detect_fast_on_huge_input() -> None:
    text = ("2026-11-25T10:00:00Z INFO " + "x" * 80 + "\n") * 100_000
    start = time.perf_counter()
    assert detect(text, None) == "log"
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize("text", ["[" * 200_000, '{"a":' * 100_000, "[" * 5000 + "]" * 5000])
def test_deeply_nested_json_does_not_raise(text: str) -> None:
    assert detect(text, None) in ("json", "text")


def test_detect_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(config_mod, "log_error", lambda env, where, msg: logged.append(where))

    def boom(text: str) -> ContentType:
        raise RuntimeError("sniff exploded")

    monkeypatch.setattr(detect_mod, "sniff", boom)
    assert detect("anything", None) == "text"
    assert detect("anything", _shell("pytest")) == "test_report"
    bad = ToolCall("Bash", "shell", {"command": "x"}, None)
    monkeypatch.setattr(detect_mod, "command_hint", lambda c: 1 / 0)
    assert detect("anything", bad) == "text"
    assert logged == ["compress.detect", "compress.detect"]


@pytest.mark.parametrize(
    "head",
    [
        "\n" * 4096,
        " \n" * 2048,
        "\t\n" * 2048,
        "\r\n" * 2048,
        " " * 4096,
        "=" * 4096,
        "= " * 2048,
        "a" * 4096,
        "a:" * 2048,
        "  at " * 800,
        "\n" * 2048 + " " * 2048,
        " \t\n\r\f\v" * 700,
    ],
)
def test_sniff_linear_on_whitespace_heavy_heads(head: str) -> None:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        detect(head, None)
        best = min(best, time.perf_counter() - start)
    assert best < 0.005, f"{best * 1000:.1f} ms"


_COMMIT = (
    "commit 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f708192\n"
    "Author: A Dev <dev@example.com>\n"
    "Date:   Mon Jan 5 10:00:00 2026 +0000\n\n"
    "    change the timeout\n\n"
)
_BODY = "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_sniff_commit_header_then_diff_is_diff() -> None:
    assert sniff(_COMMIT + _BODY) == "diff"
    assert sniff("  \n" + _COMMIT + _BODY) == "diff"
    # several commits: the diff starts after the first message
    assert sniff(_COMMIT + _BODY + "\n" + _COMMIT + _BODY) == "diff"


def test_sniff_commit_without_diff_is_not_diff() -> None:
    assert sniff(_COMMIT + "    only a message\n") != "diff"
    assert sniff("commit not-a-sha\n" + _BODY.replace("diff --git", "diff --gat")) != "diff"


def test_sniff_diff_must_start_within_the_first_4kb() -> None:
    long_msg = "".join(f"    line {i} of a long message\n" for i in range(400))
    assert sniff(_COMMIT + long_msg + _BODY) != "diff"
