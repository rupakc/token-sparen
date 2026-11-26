"""Live statusline: segments, colours, sensor, session binding, compose mode, launcher, latency."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from perfutil import perf_factor

from token_sparen.adapters.claude import statusline
from token_sparen.config import Config, load_config
from token_sparen.mechanisms import governor
from token_sparen.store import keycache, session

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "token_sparen" / "assets" / "statusline.sh"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
    }
    env.update(over)
    return env


def _assistant(mid: str, ctx: int, model: str = "claude-test-1") -> str:
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": ctx - 15,
        "cache_creation_input_tokens": 5,
        "output_tokens": 40,
    }
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}})


def _transcript(tmp_path: Path, lines: list[str], name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _payload(
    tmp_path: Path, transcript: Path, session_id: str = "s-1", **over: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path / "proj"),
        "model": {"id": "claude-test-1", "display_name": "Test"},
        "workspace": {"current_dir": str(tmp_path / "proj"), "project_dir": str(tmp_path / "proj")},
        "cost": {"total_cost_usd": 1.0},
        "context_window": {
            "total_input_tokens": 90_000,
            "total_output_tokens": 10_000,
            "context_window_size": 200_000,
            "used_percentage": 42,
            "remaining_percentage": 58,
        },
    }
    payload.update(over)
    return payload


def _overlay(tmp_path: Path, env: dict[str, str], session_id: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    key = keycache.cached_project_key(proj, env)
    return session.session_path(Path(env["TOKEN_SPAREN_HOME"]), key, session_id)


def _ledger(tmp_path: Path, env: dict[str, str], session_id: str, **values: int) -> None:
    path = _overlay(tmp_path, env, session_id)
    session.update_session(path, lambda st: {**st, **values})


def _plain(line: str) -> str:
    return ANSI.sub("", line)


@pytest.fixture(autouse=True)
def _frozen_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("perf") is None:
        monkeypatch.setattr(governor, "_monotonic", lambda: 0.0)


def test_render_segments_and_colours(tmp_path: Path) -> None:
    env = _env(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 84_000), _assistant("m2", 84_000)])
    _ledger(tmp_path, env, "s-1", trims=12, tokens_removed=40_000, injected_tokens=5_000, regrets=1)
    line = statusline.render(_payload(tmp_path, t), env)
    plain = _plain(line)
    assert "\n" not in line
    assert plain.startswith("⛁ ts 42% ctx")
    assert "burn 168k/500M 0%" in plain
    assert "saved 35k (≈$0.35)" in plain  # 35 000 net x ($1 / 100 000 tokens)
    assert "✂ 12 ↺ 1" in plain
    assert plain.endswith("learn: cold")
    assert " │ " in plain
    assert "\x1b[32m" in line  # green below 50 %

    yellow = statusline.render(_payload(tmp_path, t, context_window={"used_percentage": 60}), env)
    assert "\x1b[33m" in yellow
    red = statusline.render(_payload(tmp_path, t, context_window={"used_percentage": 75}), env)
    assert "\x1b[31m" in red


def test_rate_segment_shows_ewma_and_five_hour_limit(tmp_path: Path) -> None:
    env = _env(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    overlay = _overlay(tmp_path, env, "s-1")
    statusline.render(_payload(tmp_path, t), env)  # warm the governor cache
    session.update_session(overlay, lambda st: {**st, "governor": {**st["governor"], "ewma": 2400}})
    payload = _payload(tmp_path, t, rate_limits={"five_hour": {"used_percentage": 34.4}})
    plain = _plain(statusline.render(payload, env))
    assert "+2.4k/turn 5h 34%" in plain


def test_no_color(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    line = statusline.render(_payload(tmp_path, t), env)
    assert "\x1b[" not in line
    assert line.startswith("⛁ ts 42% ctx")


def test_ascii_glyphs(tmp_path: Path) -> None:
    env = _env(tmp_path, TOKEN_SPAREN_STATUSLINE__ASCII="true", NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    _ledger(tmp_path, env, "s-1", trims=2, regrets=0)
    line = statusline.render(_payload(tmp_path, t), env)
    assert line.isascii()
    assert line.startswith("ts 42% ctx | ")
    assert "x 2 r 0" in line


def test_segments_configurable(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "token-sparen.toml").write_text(
        '[statusline]\nsegments = ["ctx", "learn"]\n', encoding="utf-8"
    )
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    assert statusline.render(_payload(tmp_path, t), env) == "⛁ ts 42% ctx │ learn: cold"


def test_pruned_segment_shows_a_count_only(tmp_path: Path) -> None:
    from token_sparen.mechanisms import prune

    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    assert "⊘" not in statusline.render(_payload(tmp_path, t), env)  # nothing pruned: no segment
    path = _overlay(tmp_path, env, "s-1").parents[1] / prune.PRUNE_FILE
    local = prune.file_key(tmp_path / "proj" / ".claude" / "settings.local.json")
    doc = {
        "files": {local: {"dir": "x", "owned": ["mcp__a", "mcp__srv"]}, "other": {"owned": ["x"]}}
    }
    path.write_text(json.dumps(doc), encoding="utf-8")  # other checkouts are not counted
    assert statusline.render(_payload(tmp_path, t), env).endswith("learn: cold │ ⊘ 2")
    ascii_env = {**env, "TOKEN_SPAREN_STATUSLINE__ASCII": "true"}
    assert statusline.render(_payload(tmp_path, t), ascii_env).endswith("| pruned 2")


def test_dollars_hidden_without_cost(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    _ledger(tmp_path, env, "s-1", tokens_removed=3000)
    payload = _payload(tmp_path, t)
    del payload["cost"]
    line = statusline.render(payload, env)
    assert "saved 3k" in line and "$" not in line


def test_net_subtracts_recovery_reads_and_injection(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    _ledger(
        tmp_path, env, "s-1", tokens_removed=1000, recovery_read_tokens=800, injected_tokens=700
    )
    payload = _payload(tmp_path, t)
    del payload["cost"]
    assert "saved -500" in statusline.render(payload, env)


def test_records_window(tmp_path: Path) -> None:
    env = _env(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    home = Path(env["TOKEN_SPAREN_HOME"])
    home.mkdir(parents=True)
    old = time.time() - 8 * 86400
    (home / "windows.json").write_text(
        json.dumps({"stale": {"window": 5, "model": "x", "ts": old}}), encoding="utf-8"
    )
    payload = _payload(tmp_path, t, model={"id": "claude-big"})
    payload["context_window"]["context_window_size"] = 1_000_000
    statusline.render(payload, env)
    data = json.loads((home / "windows.json").read_text(encoding="utf-8"))
    assert set(data) == {"s-1"}  # the 8-day-old entry was pruned
    entry = data["s-1"]
    assert entry["window"] == 1_000_000 and entry["model"] == "claude-big"
    assert abs(entry["ts"] - time.time()) < 60
    cfg = load_config(tmp_path / "proj", env)
    assert governor.resolve_window(cfg, env, "s-1", None) == 1_000_000


def test_window_not_recorded_without_positive_size(tmp_path: Path) -> None:
    env = _env(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    for size in (None, 0, -1, "200000", True):
        payload = _payload(tmp_path, t)
        payload["context_window"]["context_window_size"] = size
        statusline.render(payload, env)
    assert not (Path(env["TOKEN_SPAREN_HOME"]) / "windows.json").exists()


def test_bound_to_session_id(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    a = _transcript(tmp_path, [_assistant("a1", 1000)], name="a.jsonl")
    b = _transcript(tmp_path, [_assistant("b1", 9000), _assistant("b2", 9000)], name="b.jsonl")
    _ledger(tmp_path, env, "s-a", trims=3, tokens_removed=1000)
    _ledger(tmp_path, env, "s-b", trims=7, tokens_removed=9000)
    os.utime(b, None)  # b is the newest transcript; it must still not leak into s-a
    line = statusline.render(_payload(tmp_path, a, session_id="s-a"), env)
    assert "✂ 3 " in line and "✂ 7" not in line
    assert "burn 1k/" in line
    line_b = statusline.render(_payload(tmp_path, b, session_id="s-b"), env)
    assert "✂ 7 " in line_b and "burn 18k/" in line_b


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "s-1"},
        {"transcript_path": "/x.jsonl"},
        {"session_id": "", "transcript_path": "/x.jsonl"},
        {"session_id": 3, "transcript_path": "/x.jsonl"},
    ],
)
def test_missing_binding_renders_minimal(tmp_path: Path, payload: dict[str, Any]) -> None:
    env = _env(tmp_path)
    assert statusline.render(payload, env) == "⛁ ts"
    assert not (Path(env["TOKEN_SPAREN_HOME"]) / "windows.json").exists()


def test_garbage_fields_never_crash(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    payload = {
        "session_id": "s-1",
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "model": "not-a-dict",
        "cost": {"total_cost_usd": "x"},
        "context_window": {"used_percentage": None, "context_window_size": "big"},
        "rate_limits": [1, 2],
    }
    line = statusline.render(payload, env)
    assert line.startswith("⛁ ts")
    assert "learn: cold" in line


def test_internal_error_renders_minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])

    def boom(*_a: Any, **_k: Any) -> Config:
        raise RuntimeError("boom")

    monkeypatch.setattr(statusline, "load_config", boom)
    assert statusline.render(_payload(tmp_path, t), env) == "⛁ ts"
    log = Path(env["TOKEN_SPAREN_HOME"]) / "logs" / "errors.jsonl"
    assert "boom" in log.read_text(encoding="utf-8")


def test_kill_switch_renders_minimal(tmp_path: Path) -> None:
    env = _env(tmp_path, TOKEN_SPAREN_DISABLE="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    assert statusline.render(_payload(tmp_path, t), env) == "⛁ ts"


def test_learner_state_used_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    monkeypatch.setattr(statusline, "_learner_state", lambda key, env: "adapting")
    assert statusline.render(_payload(tmp_path, t), env).endswith("learn: adapting")


def _run_main(argv: list[str], stdin: str, env: dict[str, str]) -> str:
    out = io.StringIO()
    rc = statusline.main(argv, io.StringIO(stdin), out, env)
    assert rc == 0
    return out.getvalue()


def test_main_plain(tmp_path: Path) -> None:
    env = _env(tmp_path, NO_COLOR="1")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    out = _run_main([], json.dumps(_payload(tmp_path, t)), env)
    assert out.startswith("⛁ ts 42% ctx") and out.endswith("\n") and out.count("\n") == 1
    assert _run_main([], "not json", env) == "⛁ ts\n"


@pytest.mark.skipif(sys.platform == "win32", reason="compose mode runs the original with sh")
def test_compose_runs_original_with_same_stdin(tmp_path: Path) -> None:
    env = {**os.environ, **_env(tmp_path, NO_COLOR="1")}
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    stdin = json.dumps(_payload(tmp_path, t))
    code = "import json,sys; d=json.load(sys.stdin); print('orig', d['session_id']); print('two')"
    original = f'python3 -c "{code}"'
    out = _run_main(["--compose", "--", original], stdin, env)
    assert out.startswith("orig s-1 │ ⛁ ts 42% ctx")
    assert "two" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="compose mode runs the original with sh")
@pytest.mark.parametrize("original", ["echo broken; exit 3", "sleep 5; echo late", "true"])
def test_compose_with_failing_original_shows_only_ours(tmp_path: Path, original: str) -> None:
    env = {**os.environ, **_env(tmp_path, NO_COLOR="1")}
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    started = time.monotonic()
    out = _run_main(["--compose", "--", original], json.dumps(_payload(tmp_path, t)), env)
    assert time.monotonic() - started < 3
    assert out.startswith("⛁ ts 42% ctx")
    assert "broken" not in out and "late" not in out


def _fake_home(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {
        k: v for k, v in os.environ.items() if k not in {"CLAUDE_CONFIG_DIR", "TOKEN_SPAREN_ROOT"}
    }
    # Only HOME is faked: on Windows, Python's expanduser reads USERPROFILE instead, so the
    # HOME-based launcher tests are POSIX-only (CLAUDE_CONFIG_DIR works everywhere).
    env.update(HOME=str(home), TOKEN_SPAREN_HOME=str(home / ".token-sparen"), NO_COLOR="1")
    return env


def _launch(
    args: list[str], env: dict[str, str], stdin: str = "{}"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(LAUNCHER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
        check=False,
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX launcher; Windows launcher planned for v1.1"
)
def test_launcher_missing_plugin_prints_empty(tmp_path: Path) -> None:
    proc = _launch([], _fake_home(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout == "\n"
    assert proc.stderr == ""


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX launcher; Windows launcher planned for v1.1"
)
def test_launcher_missing_plugin_compose_keeps_original(tmp_path: Path) -> None:
    proc = _launch(["--compose", "--", "echo mine; echo two"], _fake_home(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout == "mine\n"


def _plugin_copy(dest: Path) -> Path:
    (dest / "token_sparen").mkdir(parents=True)
    shutil.copytree(ROOT / "token_sparen", dest / "token_sparen", dirs_exist_ok=True)
    return dest


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX launcher; Windows launcher planned for v1.1"
)
def test_launcher_resolves_installed_plugin(tmp_path: Path) -> None:
    env = _fake_home(tmp_path)
    claude = Path(env["HOME"]) / ".claude" / "plugins"
    install = _plugin_copy(claude / "cache" / "market" / "token-sparen" / "0.1.0")
    claude.mkdir(parents=True, exist_ok=True)
    registry = {
        "version": 2,
        "plugins": {"token-sparen@market": [{"scope": "user", "installPath": str(install)}]},
    }
    (claude / "installed_plugins.json").write_text(json.dumps(registry), encoding="utf-8")
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    payload = json.dumps(_payload(tmp_path, t) | {"cwd": str(tmp_path)})
    proc = _launch([], env, payload)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("⛁ ts 42% ctx"), proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_launcher_respects_claude_config_dir(tmp_path: Path) -> None:
    env = _fake_home(tmp_path)
    cfg_dir = tmp_path / "cfgdir"
    install = _plugin_copy(cfg_dir / "plugins" / "cache" / "m" / "token-sparen" / "9.9.9")
    env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    proc = _launch([], env, json.dumps(_payload(tmp_path, t)))
    assert install.is_dir()
    assert proc.stdout.startswith("⛁ ts 42% ctx"), proc.stdout + proc.stderr


def test_render_p95_under_50ms(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lines = [_assistant(f"m{i}", 1000 + i) for i in range(500)]
    t = _transcript(tmp_path, lines)
    payload = _payload(tmp_path, t)
    statusline.render(payload, env)  # warm-up: first full parse, project-key cache
    budget = 0.050 * perf_factor()
    best = float("inf")
    for _ in range(3):  # best of three batches of 100 renders
        times = []
        for i in range(100):
            if i % 10 == 0:
                with t.open("a", encoding="utf-8") as fh:  # keep the transcript growing
                    fh.write(_assistant(f"n{i}-{len(times)}", 2000) + "\n")
            start = time.perf_counter()
            statusline.render(payload, env)
            times.append(time.perf_counter() - start)
        times.sort()
        best = min(best, times[94])
        if best < budget:
            break
    assert best < budget, f"p95 {best * 1000:.1f} ms"


# --------------------------------------------------------------------------- fix round 1


def _deep() -> bytes:
    return b'{"session_id": "s-1", "x": ' + b"[" * 200_000 + b"]" * 200_000 + b"}"


def test_deeply_nested_payload_prints_one_line(tmp_path: Path) -> None:
    env = _env(tmp_path)
    out = io.StringIO()
    stdin = io.TextIOWrapper(io.BytesIO(_deep()), encoding="utf-8")
    assert statusline.main([], stdin, out, env) == 0
    assert out.getvalue() == "⛁ ts\n"


@pytest.mark.skipif(sys.platform == "win32", reason="compose mode runs the original with sh")
def test_compose_forwards_exact_stdin_bytes(tmp_path: Path) -> None:
    env = {**os.environ, **_env(tmp_path, NO_COLOR="1")}
    raw = _deep() + b"\xff\xfe not utf-8 \r\n"
    sink = tmp_path / "got.bin"
    out = io.StringIO()
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    cmd = f"cat > {sink}; echo ok"
    assert statusline.main(["--compose", "--", cmd], stdin, out, env) == 0
    assert out.getvalue() == "ok │ ⛁ ts\n"
    assert sink.read_bytes() == raw


@pytest.mark.skipif(sys.platform == "win32", reason="compose mode runs the original with sh")
def test_compose_deep_payload_slow_original_is_killed(tmp_path: Path) -> None:
    env = {**os.environ, **_env(tmp_path)}
    out = io.StringIO()
    stdin = io.TextIOWrapper(io.BytesIO(_deep()), encoding="utf-8")
    started = time.monotonic()
    statusline.main(["--compose", "--", "sleep 5; echo late"], stdin, out, env)
    assert time.monotonic() - started < 3
    assert out.getvalue() == "⛁ ts\n"
