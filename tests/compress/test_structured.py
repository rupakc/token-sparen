"""JSON, diff and stack-trace compressors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from token_sparen.compress import (
    CompressContext,
    Level,
    TextCompressor,
    is_error_line,
    registry,
)
from token_sparen.compress.structured import (
    DiffCompressor,
    JsonCompressor,
    StackTraceCompressor,
)

GOLDEN = Path(__file__).parent / "golden"
LIGHT = Level(head=80, tail=40, keep_ratio=0.6)
STANDARD = Level(head=40, tail=20, keep_ratio=0.35)
AGGRESSIVE = Level(head=15, tail=10, keep_ratio=0.15)
NO_CTX = CompressContext(prompt=None, recent_paths=())


def _golden(name: str) -> tuple[str, str]:
    return (GOLDEN / f"{name}.in").read_text(encoding="utf-8"), (GOLDEN / f"{name}.out").read_text(
        encoding="utf-8"
    )


def test_registered() -> None:
    assert isinstance(registry.for_type("json"), JsonCompressor)
    assert isinstance(registry.for_type("diff"), DiffCompressor)
    assert isinstance(registry.for_type("stacktrace"), StackTraceCompressor)


# ---------------------------------------------------------------- json


def test_json_schema_and_counts() -> None:
    src, want = _golden("json_standard")
    res = JsonCompressor().compress(src, STANDARD, NO_CTX)
    assert res.text == want
    assert res.total_lines == len(json.dumps(json.loads(src), indent=2).split("\n"))
    assert res.kept_lines == len(want.rstrip("\n").split("\n")) - 1  # marker not counted
    assert not res.lossless


def test_json_levels_use_different_k() -> None:
    doc = json.dumps([{"i": i} for i in range(30)])
    comp = JsonCompressor()
    light = comp.compress(doc, LIGHT, NO_CTX).text
    aggr = comp.compress(doc, AGGRESSIVE, NO_CTX).text
    assert "… 20 more items with same keys" in light
    assert "… 28 more items with same keys" in aggr


def test_json_differing_keys_marker() -> None:
    doc = json.dumps([{"a": 1}, {"a": 2}, {"b": 3}, {"c": 4}])
    out = JsonCompressor().compress(doc, AGGRESSIVE, NO_CTX).text
    assert "… 2 more items\n" in out + "\n"
    assert "same keys" not in out


def test_json_small_is_lossless_and_unchanged() -> None:
    doc = '{"a": [1, 2]}'
    res = JsonCompressor().compress(doc, STANDARD, NO_CTX)
    assert res.lossless and res.text == doc


def test_json_many_keys_and_long_strings() -> None:
    doc = json.dumps({f"k{i:03d}": i for i in range(80)} | {"long": "x" * 900})
    out = JsonCompressor().compress(doc, STANDARD, NO_CTX).text
    assert '"k049": 49' in out and '"k050"' not in out
    assert "… 31 more keys" in out  # 81 keys, 50 kept
    doc2 = json.dumps({"s": "y" * 600})
    out2 = JsonCompressor().compress(doc2, STANDARD, NO_CTX).text
    assert "y" * 500 + "…(+100 chars)" in out2


def test_json_never_drops_error_strings() -> None:
    items = [{"id": i, "msg": "fine"} for i in range(20)]
    items[13]["msg"] = "ValueError: bad thing happened"
    items[17]["note"] = "build failed"
    out = JsonCompressor().compress(json.dumps(items), AGGRESSIVE, NO_CTX).text
    assert "ValueError: bad thing happened" in out
    assert "build failed" in out
    keys = {f"k{i}": "ok" for i in range(70)}
    keys["k69"] = "Traceback here"
    out = JsonCompressor().compress(json.dumps(keys), STANDARD, NO_CTX).text
    assert "Traceback here" in out
    long_err = "fatal: " + "z" * 700
    out = JsonCompressor().compress(json.dumps({"e": long_err}), STANDARD, NO_CTX).text
    assert long_err in out


@pytest.mark.parametrize("bad", ['{"a": ', "[1, 2,", "not json at all\n" * 100, ""])
def test_json_invalid_falls_back(bad: str) -> None:
    want = TextCompressor().compress(bad, STANDARD, NO_CTX)
    assert JsonCompressor().compress(bad, STANDARD, NO_CTX) == want


def test_json_deep_nesting_does_not_raise() -> None:
    doc = "[" * 5000 + "]" * 5000
    res = JsonCompressor().compress(doc, STANDARD, NO_CTX)
    assert isinstance(res.text, str)


def test_json_trailing_newline_preserved() -> None:
    doc = json.dumps([{"i": i} for i in range(9)]) + "\n"
    assert JsonCompressor().compress(doc, AGGRESSIVE, NO_CTX).text.endswith("\n")


# ---------------------------------------------------------------- diff


def test_diff_golden() -> None:
    src, want = _golden("diff_standard")
    res = DiffCompressor().compress(src, STANDARD, NO_CTX)
    assert res.text == want
    assert res.total_lines == 19
    assert not res.lossless


def test_diff_commit_headers_and_changed_lines_all_kept() -> None:
    parts = []
    for c in range(3):
        ctx = [f" unchanged line {c}-{i}" for i in range(120)]
        parts.append(
            f"commit {c:040x}\nAuthor: A <a@example.com>\nDate:   Mon Jan 5 10:00:00 2026 +0000\n\n"
            f"    message {c}\n\ndiff --git a/f{c} b/f{c}\n--- a/f{c}\n+++ b/f{c}\n"
            "@@ -1,121 +1,121 @@\n" + "\n".join(ctx[:60] + [f"-old {c}", f"+new {c}"] + ctx[60:])
        )
    text = "\n".join(parts) + "\n"
    for level in (LIGHT, STANDARD):
        out = DiffCompressor().compress(text, level, NO_CTX).text
        assert len(out) < len(text)
        for c in range(3):
            for want in (
                f"commit {c:040x}",
                "Author: A <a@example.com>",
                f"    message {c}",
                f"-old {c}",
                f"+new {c}",
            ):
                assert want in out.split("\n"), want
        assert out.count("Date:   Mon Jan 5 10:00:00 2026 +0000") == 3


def test_diff_light_keeps_more_context() -> None:
    src, _ = _golden("diff_standard")
    out = DiffCompressor().compress(src, LIGHT, NO_CTX).text
    assert " … [1 unchanged lines]" not in out
    assert " import sys" in out


def test_diff_binary_rename_mode_lines_kept() -> None:
    src = (
        "diff --git a/old.txt b/new.txt\nsimilarity index 90%\nrename from old.txt\n"
        "rename to new.txt\nold mode 100644\nnew mode 100755\n"
        "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"
    )
    res = DiffCompressor().compress(src, STANDARD, NO_CTX)
    assert res.text == src and res.lossless


def test_diff_malformed_falls_back() -> None:
    bad = "this is not a diff\n" * 200
    assert DiffCompressor().compress(bad, STANDARD, NO_CTX) == TextCompressor().compress(
        bad, STANDARD, NO_CTX
    )


def test_diff_removed_dashes_line_not_mistaken_for_header() -> None:
    src = "@@ -1,3 +1,2 @@\n ctx\n--- removed text\n+added\n ctx\n"
    out = DiffCompressor().compress(src, STANDARD, NO_CTX).text
    assert "--- removed text" in out and "+added" in out


_TEXT = st.text(alphabet="abc xyz-+@", max_size=8)
_LINE = st.tuples(st.sampled_from([" ", "+", "-"]), _TEXT)
_HUNK = st.lists(_LINE, min_size=1, max_size=40)


def _render(files: list[list[list[tuple[str, str]]]]) -> str:
    out: list[str] = []
    for fi, hunks in enumerate(files):
        out += [f"diff --git a/f{fi} b/f{fi}", f"--- a/f{fi}", f"+++ b/f{fi}"]
        for hunk in hunks:
            old = sum(1 for k, _ in hunk if k != "+")
            new = sum(1 for k, _ in hunk if k != "-")
            out.append(f"@@ -1,{old} +1,{new} @@ ctx")
            out += [k + t for k, t in hunk]
    return "\n".join(out) + "\n"


@settings(max_examples=150, deadline=None)
@given(
    files=st.lists(st.lists(_HUNK, min_size=1, max_size=4), min_size=1, max_size=3),
    level=st.sampled_from([LIGHT, STANDARD, AGGRESSIVE]),
)
def test_diff_keeps_all_changes(files: list[list[list[tuple[str, str]]]], level: Level) -> None:
    src = _render(files)
    out = DiffCompressor().compress(src, level, NO_CTX).text
    out_lines = out.split("\n")
    keep = ("+", "-", "diff --git", "@@")
    want = [ln for ln in src.split("\n") if ln.startswith(keep)]
    got = [ln for ln in out_lines if ln.startswith(keep)]
    assert got == want


# ---------------------------------------------------------------- stacktrace


def test_stacktrace_python_golden() -> None:
    src, want = _golden("stack_python")
    res = StackTraceCompressor().compress(src, STANDARD, NO_CTX)
    assert res.text == want
    assert not res.lossless


def test_stacktrace_keeps_exception_and_project_frames() -> None:
    src, _ = _golden("stack_python")
    out = StackTraceCompressor().compress(src, AGGRESSIVE, NO_CTX).text
    assert "ValueError: boom" in out
    assert "/srv/app/handler.py" in out and "/srv/app/main.py" in out
    assert "  … [4 library frames]" in out


def test_stacktrace_light_keeps_five_edge_frames() -> None:
    src, _ = _golden("stack_python")
    out = StackTraceCompressor().compress(src, LIGHT, NO_CTX).text
    assert "  … [2 library frames]" in out  # 12 frames, edge 5: only e, f collapse
    assert "[4 library frames]" not in out


def test_stacktrace_recent_paths_keep_library_frame() -> None:
    src, _ = _golden("stack_python")
    ctx = CompressContext(prompt=None, recent_paths=("/venv/lib/site-packages/lib/d.py",))
    out = StackTraceCompressor().compress(src, STANDARD, ctx).text
    assert "lib/d.py" in out
    assert "  … [1 library frames]" in out  # c collapsed, d kept, e,f collapsed (2)
    assert "  … [2 library frames]" in out


JAVA = (
    'Exception in thread "main" java.lang.RuntimeException: wrapper\n'
    "\tat com.acme.App.run(App.java:10)\n"
    "\tat java.base/java.util.ArrayList.forEach(ArrayList.java:1)\n"
    "\tat java.base/jdk.internal.reflect.A.invoke(A.java:2)\n"
    "\tat java.base/java.lang.reflect.Method.invoke(Method.java:3)\n"
    "\tat java.base/java.lang.Thread.run(Thread.java:4)\n"
    "\tat com.acme.Main.main(Main.java:5)\n"
    "\tat java.base/java.util.Z.z(Z.java:6)\n"
    "\tat java.base/java.util.Y.y(Y.java:7)\n"
    "\tat java.base/java.util.X.x(X.java:8)\n"
    "Caused by: java.io.IOException: disk\n"
    "\tat com.acme.Io.read(Io.java:20)\n"
    "\t... 3 more\n"
)


def test_stacktrace_java() -> None:
    out = StackTraceCompressor().compress(JAVA, AGGRESSIVE, NO_CTX).text
    assert "Caused by: java.io.IOException: disk" in out
    assert "com.acme.Main.main" in out and "com.acme.Io.read" in out
    assert "\t... 3 more" in out
    assert "  … [2 library frames]" in out  # Method.invoke and Thread.run
    assert "Method.invoke" not in out


JS = (
    "Error: nope\n"
    "    at handler (/app/src/server.js:12:5)\n"
    "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)\n"
    "    at next (/app/node_modules/express/lib/router/route.js:137:13)\n"
    "    at Route.dispatch (/app/node_modules/express/lib/router/route.js:112:3)\n"
    "    at Layer.handle2 (/app/node_modules/express/lib/router/layer.js:96:5)\n"
    "    at trim (/app/node_modules/express/lib/router/index.js:1:1)\n"
    "    at process (/app/node_modules/express/lib/router/index.js:2:1)\n"
    "    at main (/app/src/main.js:3:1)\n"
    "    at last (/app/node_modules/x/y.js:3:1)\n"
)


def test_stacktrace_js_and_multiple_traces() -> None:
    src = "intro line\n" + JS + "between\n" + JS
    out = StackTraceCompressor().compress(src, STANDARD, NO_CTX).text
    assert out.count("Error: nope") == 2
    assert out.count("/app/src/main.js") == 2
    assert out.count("library frames]") == 2
    assert "intro line" in out and "between" in out


def test_stacktrace_without_trace_delegates() -> None:
    bad = "just some output\n" * 300
    assert StackTraceCompressor().compress(bad, STANDARD, NO_CTX) == TextCompressor().compress(
        bad, STANDARD, NO_CTX
    )


def test_stacktrace_surrounding_text_goes_through_text_compressor() -> None:
    noise = "".join(f"line {i} unique{'x' * i}\n" for i in range(300))
    src = noise + JS
    out = StackTraceCompressor().compress(src, AGGRESSIVE, NO_CTX).text
    assert "lines elided]" in out
    assert "Error: nope" in out


def test_error_matcher_sanity() -> None:
    assert is_error_line("ValueError: boom")


# ---------------------------------------------------------------- fix round 1


def _py_trace(middle: str) -> str:
    frames = [f'  File "/venv/site-packages/l{i}.py", line {i}, in f\n    x()\n' for i in range(5)]
    frames.append(f'  File "{middle}", line 7, in mid\n    y()\n')
    frames += [f'  File "/venv/site-packages/m{i}.py", line {i}, in f\n    x()\n' for i in range(5)]
    return "Traceback (most recent call last):\n" + "".join(frames) + "ValueError: boom\n"


@pytest.mark.parametrize("path", ["/srv/app/java.py", "/srv/app/jdk.internal.py"])
def test_stacktrace_project_path_named_java_is_kept(path: str) -> None:
    out = StackTraceCompressor().compress(_py_trace(path), STANDARD, NO_CTX).text
    assert path in out


def test_stacktrace_node_project_file_named_java_is_kept() -> None:
    lines = ["Error: x", "    at a (/app/node_modules/a.js:1:1)"] * 1
    lines += [f"    at f{i} (/app/node_modules/m{i}.js:1:1)" for i in range(5)]
    lines.append("    at mid (/app/java.js:3:4)")
    lines += [f"    at g{i} (/app/node_modules/n{i}.js:1:1)" for i in range(5)]
    out = StackTraceCompressor().compress("\n".join(lines), STANDARD, NO_CTX).text
    assert "/app/java.js:3:4" in out


def test_stacktrace_jvm_library_still_collapsed() -> None:
    src = (
        "java.lang.IllegalStateException: x\n"
        + "\tat com.a.B.c(B.java:1)\n" * 1
        + "".join(f"\tat java.base/java.util.X{i}.m(X.java:{i})\n" for i in range(4))
        + "".join(f"\tat sun.reflect.S{i}.m(S.java:{i})\n" for i in range(4))
        + "".join(f"\tat javax.swing.J{i}.m(J.java:{i})\n" for i in range(4))
        + "\tat com.a.D.e(D.java:2)\n" * 3
    )
    out = StackTraceCompressor().compress(src, STANDARD, NO_CTX).text
    assert "library frames]" in out


def test_json_size_cap_delegates_to_text() -> None:
    doc = json.dumps([{"id": i, "v": "x" * 20} for i in range(60000)])
    assert len(doc) > 2_000_000
    assert JsonCompressor().compress(doc, STANDARD, NO_CTX) == TextCompressor().compress(
        doc, STANDARD, NO_CTX
    )


@pytest.mark.perf
def test_json_2mb_budget() -> None:
    import os
    import time

    factor = float(os.environ.get("TOKEN_SPAREN_PERF_FACTOR", "1.0"))
    doc = json.dumps([{"id": i, "v": "x" * 20} for i in range(45000)])
    assert 1_500_000 < len(doc) <= 2_000_000
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        JsonCompressor().compress(doc, STANDARD, NO_CTX)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.25 * factor, f"{best * 1000:.0f} ms"


def test_diff_error_context_lines_kept() -> None:
    body = [f" ctx{i}" for i in range(10)]
    body[5] = " ERROR: something failed"
    src = "@@ -1,12 +1,12 @@\n" + "\n".join(body) + "\n-a\n+b\n ctx\n"
    out = DiffCompressor().compress(src, AGGRESSIVE, NO_CTX).text
    assert " ERROR: something failed" in out
    assert "unchanged lines]" in out


def test_stacktrace_error_library_frames_kept() -> None:
    lines = ["Traceback (most recent call last):"]
    for i in range(12):
        lines.append(f'  File "/venv/site-packages/l{i}.py", line 1, in f')
        lines.append("    raise RuntimeError('inner')" if i == 6 else "    x()")
    lines.append("ValueError: boom")
    out = StackTraceCompressor().compress("\n".join(lines), STANDARD, NO_CTX).text
    assert "RuntimeError('inner')" in out
    js = ["Error: x"] + [f"    at f{i} (/app/node_modules/m{i}.js:1:1)" for i in range(6)]
    js.insert(4, "    at onError (/app/node_modules/e.js:9:9)")
    js += [f"    at g{i} (/app/node_modules/n{i}.js:1:1)" for i in range(6)]
    assert "onError" in StackTraceCompressor().compress("\n".join(js), STANDARD, NO_CTX).text


def test_diff_huge_hunk_header_does_not_raise() -> None:
    src = "@@ -1," + "9" * 5000 + " +1,1 @@\n ctx\n-a\n+b\n" * 3
    res = DiffCompressor().compress(src, STANDARD, NO_CTX)
    assert "-a" in res.text and "+b" in res.text


@pytest.mark.parametrize("cls", [JsonCompressor, DiffCompressor, StackTraceCompressor])
def test_never_raises(cls: type, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from token_sparen.compress import structured

    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(tmp_path))

    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("x")

    monkeypatch.setattr(structured, "_knob", boom)
    src = (
        '{"a": [1, 2, 3]}'
        if cls is JsonCompressor
        else "diff --git a/a b/a\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    res = cls().compress(src, STANDARD, NO_CTX)
    assert res == TextCompressor().compress(src, STANDARD, NO_CTX)


def test_project_override_changes_json_items(tmp_path: Path) -> None:
    from token_sparen.config import load_config

    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    proj = tmp_path / "proj"
    proj.mkdir()
    doc = json.dumps([{"i": i} for i in range(30)])
    base = CompressContext(prompt=None, recent_paths=(), config=load_config(proj, env))
    assert "… 25 more items" in JsonCompressor().compress(doc, STANDARD, base).text
    (proj / "token-sparen.toml").write_text(
        "[compression.json]\nitems = { standard = 8 }\n", encoding="utf-8"
    )
    over = CompressContext(prompt=None, recent_paths=(), config=load_config(proj, env))
    assert "… 22 more items" in JsonCompressor().compress(doc, STANDARD, over).text
    # no config on the context: packaged defaults, regardless of cwd
    assert "… 25 more items" in JsonCompressor().compress(doc, STANDARD, NO_CTX).text


def test_json_repeated_values_are_shown_verbatim() -> None:
    # Ruling (M1 fix round 1): no `…same` elision; a repeated value reads as itself.
    items = [{"id": i, "role": "maintainer", "team": "platform-infrastructure"} for i in range(4)]
    items += [{"pad": i} for i in range(20)]
    out = JsonCompressor().compress(json.dumps(items), LIGHT, NO_CTX).text
    assert "…same" not in out
    assert out.count('"team": "platform-infrastructure"') == 4
    assert out.count('"role": "maintainer"') == 4
