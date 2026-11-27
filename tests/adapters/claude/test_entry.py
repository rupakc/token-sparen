"""Fail-open hook entry point, project-key cache and plugin manifest."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import token_sparen
from token_sparen.adapters.claude import entry
from token_sparen.config import Config
from token_sparen.events import Action, Event
from token_sparen.store import jsonstore, keycache, paths

ROOT = Path(__file__).resolve().parents[3]
EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "SessionEnd",
    "PermissionDenied",
]
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"}


@pytest.fixture(autouse=True)
def _handlers() -> Iterator[None]:
    saved = dict(entry.HANDLERS)
    yield
    entry.HANDLERS.clear()
    entry.HANDLERS.update(saved)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    proj = tmp_path / "proj"  # the state root lies outside the project (review M5)
    proj.mkdir(parents=True, exist_ok=True)
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(proj)}
    env.update(extra)
    return env


def _bash_payload(stdout: str = "x") -> str:
    return json.dumps(
        {
            "session_id": "s-1",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_use_id": "tu-1",
            "tool_response": {
                "stdout": stdout,
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
    )


def _run(event: str, payload: str, env: dict[str, str], **kw: Any) -> tuple[int, str]:
    out = io.StringIO()
    rc = entry.main(event, io.StringIO(payload), out, env, **kw)
    return rc, out.getvalue()


def _errors(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "logs" / "errors.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _replace(event: Event, cfg: Config) -> Action:
    return Action(replace_output="short")


def test_default_handlers_are_noop(tmp_path: Path) -> None:
    assert {"pre_tool", "post_tool"} <= set(entry.HANDLERS)
    rc, out = _run("PostToolUse", _bash_payload(), _env(tmp_path))
    assert (rc, out) == (0, "{}\n")


def test_register_and_replace(tmp_path: Path) -> None:
    entry.register("post_tool", _replace)
    rc, out = _run("PostToolUse", _bash_payload(), _env(tmp_path))
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["updatedToolOutput"]["stdout"] == "short"


def test_handle_unregistered_kind_is_noop(tmp_path: Path) -> None:
    ev = entry.ADAPTER.parse("SessionEnd", {"session_id": "s"}, _env(tmp_path))
    entry.HANDLERS.pop("session_end", None)
    assert entry.handle(ev, Config()).is_noop()


def test_entry_exception_is_noop(tmp_path: Path) -> None:
    def boom(event: Event, cfg: Config) -> Action:
        raise RuntimeError("kaboom")

    entry.register("post_tool", boom)
    rc, out = _run("PostToolUse", _bash_payload(), _env(tmp_path))
    assert (rc, out) == (0, "{}\n")
    errs = _errors(tmp_path)
    assert len(errs) == 1 and "kaboom" in errs[0]["error"]


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]", '{"session_id": 3}'])
def test_bad_payload_is_noop(tmp_path: Path, payload: str) -> None:
    env = _env(tmp_path)
    del env["CLAUDE_PROJECT_DIR"]  # and no cwd in payload -> parse error
    assert _run("PostToolUse", payload, env) == (0, "{}\n")
    assert _errors(tmp_path)


def test_unknown_event_is_noop(tmp_path: Path) -> None:
    assert _run("Notification", "{}", _env(tmp_path)) == (0, "{}\n")


def test_unserialisable_action_is_noop(tmp_path: Path) -> None:
    def weird(event: Event, cfg: Config) -> Action:
        return Action(rewrite_input={"x": object()})

    entry.register("pre_tool", weird)
    payload = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {}})
    assert _run("PreToolUse", payload, _env(tmp_path)) == (0, "{}\n")
    assert _errors(tmp_path)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod read-only is not enforced",
)
def test_readonly_home_fail_open(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    try:
        entry.register("post_tool", _replace)
        env = _env(tmp_path, TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE="1.0")
        rc, out = _run("PostToolUse", _bash_payload(), env)
        assert rc == 0
        json.loads(out)  # well-formed whatever happened
        assert out.endswith("\n") and out.count("\n") == 1
    finally:
        home.chmod(0o700)


def test_disable_env_noop(tmp_path: Path) -> None:
    entry.register("post_tool", _replace)
    for value in ("1", "true"):
        rc, out = _run("PostToolUse", _bash_payload(), _env(tmp_path, TOKEN_SPAREN_DISABLE=value))
        assert (rc, out) == (0, "{}\n")


def test_disable_via_config_file_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry.register("post_tool", _replace)
    monkeypatch.setattr(entry, "load_config", lambda d, e: Config(disabled=True))
    assert _run("PostToolUse", _bash_payload(), _env(tmp_path)) == (0, "{}\n")


def test_latency_budget_timeout_is_noop(tmp_path: Path) -> None:
    def slow(event: Event, cfg: Config) -> Action:
        time.sleep(0.5)
        return Action(replace_output="late")

    exits: list[int] = []
    entry.register("post_tool", slow)
    env = _env(tmp_path, TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="50")
    rc, out = _run("PostToolUse", _bash_payload(), env, exit_fn=exits.append)
    assert rc == 0
    assert out == "{}\n"  # exactly one write: the watchdog's no-op, never the late action
    assert exits == [0]
    assert any("latency budget" in e["error"] for e in _errors(tmp_path))


def test_latency_sampled_log(tmp_path: Path) -> None:
    env = _env(tmp_path, TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE="1.0")
    _run("PostToolUse", _bash_payload(), env)
    path = tmp_path / "home" / "logs" / "latency.jsonl"
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["event"] == "PostToolUse" and rec["ms"] >= 0 and rec["outcome"] == "noop"
    env = _env(tmp_path / "b", TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE="0")
    _run("PostToolUse", _bash_payload(), env)
    assert not (tmp_path / "b" / "home" / "logs" / "latency.jsonl").exists()


def test_kill_switch_writes_no_latency_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``TOKEN_SPAREN_DISABLE=1``: the hook is a no-op and writes nothing, not even the sampled
    latency line (found by the M3 integration test)."""
    monkeypatch.setattr(entry, "DEFAULT_SAMPLE_RATE", 1.0)
    rc, out = _run("PostToolUse", _bash_payload(), _env(tmp_path, TOKEN_SPAREN_DISABLE="1"))
    assert rc == 0 and json.loads(out) == {}
    assert not (tmp_path / "home").exists()


def test_store_busy_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(event: Event, cfg: Config) -> Action:
        raise jsonstore.StoreBusy("lock")

    entry.register("post_tool", busy)
    assert _run("PostToolUse", _bash_payload(), _env(tmp_path)) == (0, "{}\n")


# --- project-key cache --------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_project_key_cache_avoids_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://example.com/a/b.git")
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    calls: list[Path] = []
    real = paths._origin

    def counting(d: Path) -> str | None:
        calls.append(d)
        return real(d)

    monkeypatch.setattr(paths, "_origin", counting)
    k1 = keycache.cached_project_key(repo, env)
    k2 = keycache.cached_project_key(repo, env)
    assert k1 == k2 == paths.project_key(repo)
    assert len(calls) == 2  # one from the first cached call, one from the direct call above
    cache = json.loads((tmp_path / "home" / "project_keys.json").read_text(encoding="utf-8"))
    assert str(repo) not in json.dumps(cache)  # never a raw path in the shared store
    # changing .git/config invalidates the entry
    _git(repo, "remote", "set-url", "origin", "https://example.com/a/c.git")
    cfg = repo / ".git" / "config"
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    k3 = keycache.cached_project_key(repo, env)
    assert k3 != k1 and len(calls) == 3


def test_project_key_cache_store_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*a: Any, **k: Any) -> Any:
        raise jsonstore.StoreBusy("x")

    monkeypatch.setattr(jsonstore, "update", busy)
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    assert keycache.cached_project_key(tmp_path, env) == paths.project_key(tmp_path)


def test_project_key_cache_corrupt_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "project_keys.json").write_text('{"x": 1}', encoding="utf-8")
    env = {"TOKEN_SPAREN_HOME": str(home)}
    assert keycache.cached_project_key(tmp_path, env) == paths.project_key(tmp_path)


# --- end to end ---------------------------------------------------------------------------


def test_e2e_run_py_subprocess(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    env.update(_env(tmp_path, CLAUDE_PLUGIN_ROOT=str(ROOT)))
    script = ROOT / "hooks" / "run.py"
    for event in EVENTS:
        proc = subprocess.run(
            [sys.executable, "-I", str(script), event],
            input=_bash_payload(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "{}\n", (event, proc.stdout, proc.stderr)


def test_e2e_run_py_import_error(tmp_path: Path) -> None:
    fake_root = tmp_path / "plugin"
    (fake_root / "hooks").mkdir(parents=True)
    (fake_root / "hooks" / "run.py").write_text(
        (ROOT / "hooks" / "run.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(fake_root / "hooks" / "run.py"), "PostToolUse"],
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=30,
    )
    assert (proc.returncode, proc.stdout) == (0, "{}\n")


# --- plugin manifest ----------------------------------------------------------------------

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_RESERVED = ("claude-", "anthropic-", "cc-plugin-")


def test_plugin_manifest_valid() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "token-sparen"
    assert _KEBAB.match(plugin["name"])
    assert not plugin["name"].startswith(_RESERVED)
    assert plugin["version"] == token_sparen.__version__
    assert plugin["license"] == "MIT"
    for key in ("description", "author", "homepage", "repository"):
        assert plugin[key]
    assert not (ROOT / "bin").exists()

    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["name"] == "token-sparen"
    assert market["owner"]["name"] == "Rupak Chakraborty"
    assert [p["source"] for p in market["plugins"]] == ["./"]
    assert market["plugins"][0]["name"] == "token-sparen"

    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert sorted(hooks) == sorted(EVENTS)
    for event, groups in hooks.items():
        assert len(groups) == 1
        group = groups[0]
        assert ("matcher" in group) == (event in TOOL_EVENTS)
        if event in TOOL_EVENTS:
            assert group["matcher"] == "*"
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert hook["timeout"] == 10
        assert hook["command"] == f'python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/run.py" {event}'


def test_pipeline_wiring_uses_entry_env(tmp_path: Path) -> None:
    from sampleoutputs import big_pytest

    assert {"prompt", "pre_tool", "post_tool", "post_tool_failure"} <= set(entry.HANDLERS)
    env = _env(tmp_path)
    rc, out = _run("PostToolUse", _bash_payload(big_pytest()).replace('"ls"', '"pytest"'), env)
    assert rc == 0
    stdout = json.loads(out)["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
    assert "[token-sparen: kept " in stdout
    assert list((tmp_path / "home" / "projects").glob("*/decisions.jsonl"))


def test_pipeline_wiring_vetoes_failure_events(tmp_path: Path) -> None:
    from sampleoutputs import big_pytest

    payload = json.dumps(
        {
            "session_id": "s-1",
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_use_id": "tu-f",
            "error": big_pytest(),
        }
    )
    assert _run("PostToolUseFailure", payload, _env(tmp_path)) == (0, "{}\n")
    assert not (tmp_path / ".token-sparen").exists()
    assert not list((tmp_path / "home").glob("projects/*/decisions.jsonl"))


def test_plugin_manifest_states_the_minimum_claude_code() -> None:
    """PostToolUse `updatedToolOutput` replaces built-in tool output from Claude Code 2.1.121
    (earlier: MCP tools only), which compression needs; older hosts must not load the plugin."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["engines"] == {"claude-code": ">=2.1.121"}


class _Killed(BaseException):
    """Models ``os._exit`` from the watchdog: the main thread runs no further handler."""


def test_stop_prompt_settles_even_after_overrun(tmp_path: Path) -> None:
    """Review M3 (M4 triage): the loop guard's prompt handler is always-run, so a "stop" settles
    the previous turn's redirect with regret 1 even when another prompt handler overruns."""
    from token_sparen.mechanisms import loopguard, tracking

    env = _env(tmp_path, TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS="50")
    payload = {"session_id": "s-1", "hook_event_name": "UserPromptSubmit", "prompt": "stop"}
    event = entry.ADAPTER.parse("UserPromptSubmit", payload, env)
    path = tracking.overlay_path(event, env)
    agent = {"fps": {}, "files": {}, "decision": ["d-1", "standard", 0.5, "cat=shell"]}
    agent["nudge"] = {"fp": "f" * 10, "n": 3}
    lg = {"turn": 1, "agents": {"": agent}}
    jsonstore.update(path, lambda _: {"turn": 1, loopguard.STATE_KEY: lg}, {}, env)

    def overrun(event: Event, cfg: Config) -> Action:
        time.sleep(0.3)  # the watchdog fires meanwhile and would os._exit the process
        raise _Killed

    chain = entry.HANDLERS["prompt"]
    entry.HANDLERS["prompt"] = [chain[0], overrun, *chain[1:]]  # right after the turn count
    exits: list[int] = []
    rc, out = _run("UserPromptSubmit", json.dumps(payload), env, exit_fn=exits.append)
    assert (rc, out, exits) == (0, "{}\n", [0])
    state = jsonstore.read(path, {})
    assert loopguard.STATE_KEY not in state
    rows = [r for r in state.get("regret_outcomes", []) if r[0] == "d-1"]
    assert rows and rows[0][4] == 1.0 and rows[0][6] == "user_stop"


def test_after_emit_work_runs_after_the_response_and_fails_open(tmp_path: Path) -> None:
    from token_sparen import deferred

    seen: list[str] = []
    out = io.StringIO()

    def boom() -> None:
        raise RuntimeError("late boom")

    def handler(event: Event, cfg: Config) -> Action:
        deferred.after_emit(boom)
        deferred.after_emit(lambda: seen.append(out.getvalue()))
        return Action(replace_output="short")

    entry.register("post_tool", handler)
    assert entry.main("PostToolUse", io.StringIO(_bash_payload()), out, _env(tmp_path)) == 0
    assert seen and "updatedToolOutput" in seen[0]  # the response was written first
    assert any("after emit: RuntimeError: late boom" in e["error"] for e in _errors(tmp_path))


def test_output_style_runs_after_facts_on_session_start() -> None:
    names = [getattr(fn, "__name__", "") for fn in entry.HANDLERS["session_start"]]
    assert names.index("_on_output_style") == names.index("_on_facts") + 1
