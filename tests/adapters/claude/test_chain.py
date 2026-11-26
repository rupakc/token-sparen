"""Handler chains: composition rules per event and the built-in mechanism wiring."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    env.update(extra)
    return env


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
    assert names == ["_on_pre_invalidate", "_on_governor_stop", "_on_guard", "_on_subagent"]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["post_tool"]]
    assert names == ["_on_track", "_on_governor_post", "_on_read_dedupe", "_on_compress"]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["prompt"]]
    assert names == ["_on_prompt", "_on_governor_prompt", "_on_boundary"]
    names = [getattr(f, "__name__", "") for f in entry.HANDLERS["pre_compact"]]
    assert names == ["_on_pre_compact", "_on_compact_instructions"]
    assert "session_start" in entry.HANDLERS


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
    env = _env(tmp_path, TOKEN_SPAREN_COMPRESSION__MIN_TOKENS="999999999")  # isolate dedupe
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


def test_pipeline_compressed_read_is_not_deduped(tmp_path: Path) -> None:
    path = tmp_path / "big.py"
    path.write_text(_code(3000), encoding="utf-8")
    env = _env(tmp_path)
    first = _run("PostToolUse", _read_payload(path), env)["hookSpecificOutput"]
    assert "[token-sparen: kept " in first["updatedToolOutput"]["file"]["content"]
    # the model never saw the full file, so a re-read must not become a pointer
    second = _run("PostToolUse", _read_payload(path), env)["hookSpecificOutput"]
    assert "unchanged since turn" not in second["updatedToolOutput"]["file"]["content"]
