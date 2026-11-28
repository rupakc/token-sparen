"""Handler chains: composition rules per event and the built-in mechanism wiring."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from userconf import to_user_config

from token_sparen.adapters.claude import entry
from token_sparen.config import Config
from token_sparen.events import Action, Event


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
    return to_user_config(env)  # widening settings live in the user's file (env only narrows)


def _run(event: str, payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    out = io.StringIO()
    assert entry.main(event, io.StringIO(json.dumps(payload)), out, env) == 0
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


def _pre_event(tmp_path: Path, command: str = "ls") -> Event:
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": command}}
    return entry.ADAPTER.parse("PreToolUse", payload, _env(tmp_path))


def _post_event(tmp_path: Path) -> Event:
    payload = {
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "x", "stderr": "", "interrupted": False, "isImage": False},
    }
    return entry.ADAPTER.parse("PostToolUse", payload, _env(tmp_path))


def test_register_appends_to_chain() -> None:
    before = list(entry.HANDLERS["pre_tool"])

    def fn(event: Event, cfg: Config) -> Action:
        return Action()

    entry.register("pre_tool", fn)
    assert entry.HANDLERS["pre_tool"] == [*before, fn]
    assert entry.HANDLERS["pre_tool"] is not before


def test_pre_tool_first_block_wins(tmp_path: Path) -> None:
    entry.HANDLERS["pre_tool"] = []
    entry.register("pre_tool", lambda e, c: Action(rewrite_input={"command": "ls -1"}))
    entry.register("pre_tool", lambda e, c: Action(block_reason="first"))
    entry.register("pre_tool", lambda e, c: Action(block_reason="second"))
    act = entry.handle(_pre_event(tmp_path), Config())
    assert act.block_reason == "first" and act.rewrite_input is None


def test_pre_tool_first_rewrite_wins_and_is_seen_later(tmp_path: Path) -> None:
    seen: list[Any] = []

    def spy(event: Event, cfg: Config) -> Action:
        assert event.tool is not None
        seen.append(event.tool.input["command"])
        return Action(rewrite_input={"command": "later"}, inject_context="b")

    entry.HANDLERS["pre_tool"] = []
    entry.register("pre_tool", lambda e, c: Action(rewrite_input={"command": "ls -1"}))
    entry.register("pre_tool", spy)
    act = entry.handle(_pre_event(tmp_path), Config())
    assert act.rewrite_input == {"command": "ls -1"}
    assert seen == ["ls -1"]
    assert act.inject_context == "b"


def test_post_tool_first_replace_stops_chain(tmp_path: Path) -> None:
    calls: list[str] = []

    def first(event: Event, cfg: Config) -> Action:
        calls.append("first")
        return Action(inject_context="ctx-a")

    def second(event: Event, cfg: Config) -> Action:
        calls.append("second")
        return Action(replace_output="pointer", inject_context="ctx-b")

    def third(event: Event, cfg: Config) -> Action:
        calls.append("third")
        return Action(replace_output="compressed")

    entry.HANDLERS["post_tool"] = [first, second, third]
    act = entry.handle(_post_event(tmp_path), Config())
    assert act.replace_output == "pointer"
    assert act.inject_context == "ctx-a\n\nctx-b"
    assert calls == ["first", "second"]


def test_always_run_handlers_follow_a_replacement(tmp_path: Path) -> None:
    calls: list[str] = []

    def replace(event: Event, cfg: Config) -> Action:
        calls.append("replace")
        return Action(replace_output="compressed")

    def skipped(event: Event, cfg: Config) -> Action:
        calls.append("skipped")
        return Action()

    def trailer(event: Event, cfg: Config) -> Action:
        calls.append("trailer")
        return Action(replace_output="ignored", inject_context="t")

    entry.HANDLERS["post_tool"] = []
    entry.register("post_tool", replace)
    entry.register("post_tool", skipped)
    entry.register("post_tool", trailer, always=True)
    act = entry.handle(_post_event(tmp_path), Config())
    assert calls == ["replace", "trailer"]
    assert act.replace_output == "compressed" and act.inject_context == "t"


def test_builtin_tracking_runs_after_compression_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from token_sparen.mechanisms import tracking
    from token_sparen.store import session

    env = _env(tmp_path)
    token = entry.HOOK_ENV.set(env)
    try:
        monkeypatch.setattr(
            entry.compression, "handle_post_tool", lambda *a, **k: Action(replace_output="c")
        )
        ev = _post_event(tmp_path)
        assert entry.handle(ev, Config()).replace_output == "c"
        overlay = session.read_session(tracking.overlay_path(ev, env))
        assert overlay["recent_cats"] == ["shell"]
    finally:
        entry.HOOK_ENV.reset(token)


def test_other_kinds_merge_all(tmp_path: Path) -> None:
    ev = entry.ADAPTER.parse("SessionEnd", {"session_id": "s"}, _env(tmp_path))
    entry.HANDLERS["session_end"] = [
        lambda e, c: Action(user_message="a"),
        lambda e, c: Action(),
        lambda e, c: Action(user_message="b"),
    ]
    assert entry.handle(ev, Config()).user_message == "a\nb"


def test_builtin_order() -> None:
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["pre_tool"]]
    # pretrim is post-tool only; the governor's opt-in stop runs before the subagent rewrite
    # pruning recovery runs first: a later block ends the chain, and it never blocks itself
    assert names == [
        "_on_prune_tool",
        "_on_activity",
        "_on_pre_invalidate",
        "_on_governor_stop",
        "_on_guard",
        "_on_subagent",
    ]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["post_tool"]]
    # tracking runs last (its lock wait never eats compression's budget) and always runs
    # regret detection runs before any replacement can end the chain
    assert names == [
        "_on_prune_search",
        "_on_governor_post",
        "_on_regret",
        "_on_read_dedupe",
        "_on_compress",
        "_on_loopguard",
        "_on_activity",
        "_on_track",
    ]
    assert entry.HANDLERS["post_tool"][-1] in entry.ALWAYS_RUN
    assert entry.HANDLERS["post_tool"][-2] in entry.ALWAYS_RUN
    assert entry.HANDLERS["post_tool"][-3] in entry.ALWAYS_RUN  # the loop guard sees every call
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["post_tool_failure"]]
    assert names == [
        "_on_prune_tool",
        "_on_governor_post",
        "_on_regret",
        "_on_compress",
        "_on_loopguard",
        "_on_track",
    ]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["prompt"]]
    assert names == [
        "_on_prompt",
        "_on_governor_prompt",
        "_on_boundary",
        "_on_regret",
        "_on_prune_prompt",
        "_on_loopguard_prompt",
    ]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["pre_compact"]]
    assert names == ["_on_pre_compact", "_on_compact_instructions"]
    assert "session_start" in entry.HANDLERS
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["session_end"]]
    assert names == ["_on_regret", "_on_loopguard_end", "_on_fold_spawn"]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["permission_denied"]]
    assert names == ["_on_prune_tool"]


def test_builtin_mechanism_errors_are_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from token_sparen.mechanisms import guards

    def boom(*a: Any, **k: Any) -> Action:
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(guards, "check", boom)
    env = _env(tmp_path)
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "find / -x"}}
    assert _run("PreToolUse", payload, env) == {}
    errs = (tmp_path / "home" / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "guard exploded" in errs


def test_pipeline_guard_blocks_find_root(tmp_path: Path) -> None:
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "find / -name x"}}
    out = _run("PreToolUse", payload, _env(tmp_path))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "-maxdepth" in out["permissionDecisionReason"]
    payload["tool_input"] = {"command": "find . -name x"}
    assert _run("PreToolUse", payload, _env(tmp_path)) == {}


def _read_payload(path: Path, **inp: Any) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return {
        "session_id": "s-r",
        "tool_name": "Read",
        "tool_input": {"file_path": str(path), **inp},
        "tool_use_id": "tu-r",
        "tool_response": {
            "type": "text",
            "file": {"filePath": str(path), "content": text, "numLines": 1, "totalLines": 1},
        },
    }


def _code(lines: int) -> str:
    return "\n".join(f"def f{i}(a, b):\n    return compute(a, b, {i})" for i in range(lines))


def test_pipeline_read_dedupe_pointer_and_compact(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(_code(800), encoding="utf-8")
    env = _env(  # isolate dedupe; static arms: the pointer is always served (no exploration)
        tmp_path,
        TOKEN_SPAREN_COMPRESSION__MIN_TOKENS="999999999",
        TOKEN_SPAREN_LEARN__ENABLED="false",
        TOKEN_SPAREN_READ_DEDUPE__ENABLED="true",  # opt-in (off by default since v1.0)
    )
    _run("UserPromptSubmit", {"session_id": "s-r", "prompt": "look"}, env)
    assert _run("PostToolUse", _read_payload(path), env) == {}
    _run("UserPromptSubmit", {"session_id": "s-r", "prompt": "again"}, env)
    out = _run("PostToolUse", _read_payload(path), env)
    content = out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
    assert content.startswith("[token-sparen: unchanged since turn 1 ")
    # PreCompact: the earlier read is gone from context, full content again
    _run("PreCompact", {"session_id": "s-r", "trigger": "auto"}, env)
    assert _run("PostToolUse", _read_payload(path), env) == {}
    # an Edit (PreToolUse) invalidates
    edit = {"session_id": "s-r", "tool_name": "Edit", "tool_input": {"file_path": str(path)}}
    assert _run("PreToolUse", edit, env) == {}
    assert _run("PostToolUse", _read_payload(path), env) == {}
    # SessionStart(compact) resets as well
    _run("SessionStart", {"session_id": "s-r", "source": "compact"}, env)
    assert _run("PostToolUse", _read_payload(path), env) == {}
    assert "updatedToolOutput" in _run("PostToolUse", _read_payload(path), env).get(
        "hookSpecificOutput", {}
    )


def test_default_read_untouched_then_reread_deduped(tmp_path: Path) -> None:
    """Quality first: a big Read is shown in full; read de-dupe is off by default (v1 ruling on
    M13), so the re-read is shown in full too; opted in, it becomes a pointer."""
    path = tmp_path / "big.py"
    path.write_text(_code(3000), encoding="utf-8")
    env = _env(tmp_path, TOKEN_SPAREN_LEARN__ENABLED="false")
    assert _run("PostToolUse", _read_payload(path), env) == {}
    assert _run("PostToolUse", _read_payload(path), env) == {}  # default: off
    project = tmp_path / "proj" / "token-sparen.toml"  # a project cannot opt in
    project.write_text("[read_dedupe]\nenabled = true\n", encoding="utf-8")
    narrowed = {**env, "TOKEN_SPAREN_READ_DEDUPE__ENABLED": "true"}  # nor can the environment
    assert _run("PostToolUse", _read_payload(path), narrowed) == {}
    project.unlink()
    env = _env(tmp_path, TOKEN_SPAREN_LEARN__ENABLED="false", TOKEN_SPAREN_READ_DEDUPE__ENABLED="1")
    assert _run("PostToolUse", _read_payload(path), env) == {}
    second = _run("PostToolUse", _read_payload(path), env)["hookSpecificOutput"]
    assert "unchanged since turn" in second["updatedToolOutput"]["file"]["content"]


def test_pipeline_compressed_read_is_not_deduped(tmp_path: Path) -> None:
    path = tmp_path / "big.py"
    path.write_text(_code(3000), encoding="utf-8")
    env = _env(  # static: both reads compressed (file contents and plain text opted in)
        tmp_path,
        TOKEN_SPAREN_LEARN__ENABLED="false",
        TOKEN_SPAREN_COMPRESSION__TRIM_FILE_CONTENTS="true",
        TOKEN_SPAREN_COMPRESSION__ALLOW_CTYPES="text",
    )
    first = _run("PostToolUse", _read_payload(path), env)["hookSpecificOutput"]
    assert "[token-sparen: kept " in first["updatedToolOutput"]["file"]["content"]
    # the model never saw the full file, so a re-read must not become a pointer
    second = _run("PostToolUse", _read_payload(path), env)["hookSpecificOutput"]
    assert "unchanged since turn" not in second["updatedToolOutput"]["file"]["content"]
