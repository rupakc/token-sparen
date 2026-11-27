"""Claude Code payload translation and host-JSON rendering."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from token_sparen.adapters.base import HostAdapter
from token_sparen.adapters.claude import translate
from token_sparen.adapters.claude.translate import ClaudeAdapter, render_tool_output
from token_sparen.events import Action

BASH = {"stdout": "a\nb", "stderr": "warn", "interrupted": True, "isImage": False}
READ = {
    "type": "text",
    "file": {
        "filePath": "/repo/x.py",
        "content": "old",
        "numLines": 1,
        "startLine": 1,
        "totalLines": 1,
    },
}


def _env(tmp_path: Path) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home"), "CLAUDE_PROJECT_DIR": str(tmp_path)}


def _payload(event: str, **extra: Any) -> dict[str, Any]:
    base = {
        "session_id": "s-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/nowhere",
        "hook_event_name": event,
        "permission_mode": "default",
    }
    base.update(extra)
    return base


def test_render_bash_shape() -> None:
    out = render_tool_output("Bash", BASH, "new")
    assert out == {"stdout": "new", "stderr": "", "interrupted": True, "isImage": False}
    assert render_tool_output("Bash", {"stdout": "x"}, "n") == {
        "stdout": "n",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
    }


def test_render_read_shape() -> None:
    orig = copy.deepcopy(READ)
    out = render_tool_output("Read", READ, "new text")
    assert out["file"]["content"] == "new text"
    assert out["file"]["filePath"] == "/repo/x.py"
    assert out["type"] == "text"
    assert READ == orig  # deep copy: original untouched


def test_render_str_shape() -> None:
    assert render_tool_output("mcp__srv__tool", "raw", "new") == "new"


@pytest.mark.parametrize(
    ("tool", "original"),
    [
        ("WebFetch", {"result": "x", "code": 200}),
        ("Grep", ["a", "b"]),
        ("Read", {"type": "image", "file": {"base64": "AAAA", "type": "image/png"}}),
        ("Read", {"type": "text", "file": "not-a-dict"}),
        ("Bash", {"stdout": "x", "isImage": True}),
        ("Bash", ["stdout"]),
        ("Glob", None),
        ("Glob", 42),
    ],
)
def test_unknown_shape_passthrough(tool: str, original: Any) -> None:
    assert render_tool_output(tool, original, "new") is None


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Bash", "shell"),
        ("Read", "read"),
        ("NotebookRead", "read"),
        ("Grep", "search"),
        ("Glob", "search"),
        ("LS", "search"),
        ("WebFetch", "web"),
        ("WebSearch", "web"),
        ("Edit", "edit"),
        ("Write", "edit"),
        ("MultiEdit", "edit"),
        ("NotebookEdit", "edit"),
        ("Task", "agent"),
        ("Agent", "agent"),
        ("mcp__pplx__search", "mcp"),
        ("TodoWrite", "other"),
    ],
)
def test_tool_category(name: str, category: str) -> None:
    assert translate.tool_category(name) == category


def test_adapter_satisfies_protocol() -> None:
    adapter: HostAdapter = ClaudeAdapter()
    assert adapter.name == "claude"
    assert adapter.capabilities == frozenset(
        {"replace_output", "rewrite_input", "block", "inject_context", "statusline"}
    )


def test_parse_post_tool_bash(tmp_path: Path) -> None:
    payload = _payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tu-1",
        tool_response=BASH,
    )
    ev = ClaudeAdapter().parse("PostToolUse", payload, _env(tmp_path))
    assert ev.host == "claude"
    assert ev.kind == "post_tool"
    assert ev.session_id == "s-1"
    assert ev.project_dir == tmp_path
    assert len(ev.project_key) == 16
    assert ev.transcript_path == Path("/tmp/t.jsonl")
    assert ev.tool is not None
    assert ev.tool.name == "Bash" and ev.tool.category == "shell"
    assert dict(ev.tool.input) == {"command": "ls"}
    assert ev.tool.use_id == "tu-1"
    assert ev.output_text == "a\nb\nwarn"
    assert ev.raw_response == BASH
    assert ev.extra["permission_mode"] == "default"


def test_parse_read_and_str_and_failure(tmp_path: Path) -> None:
    env = _env(tmp_path)
    a = ClaudeAdapter()
    ev = a.parse("PostToolUse", _payload("PostToolUse", tool_name="Read", tool_response=READ), env)
    assert ev.output_text == "old"
    ev = a.parse("PostToolUse", _payload("PostToolUse", tool_name="X", tool_response="s"), env)
    assert ev.output_text == "s"
    ev = a.parse("PostToolUse", _payload("PostToolUse", tool_name="X", tool_response=[1]), env)
    assert ev.output_text is None
    ev = a.parse(
        "PostToolUseFailure",
        _payload("PostToolUseFailure", tool_name="Bash", tool_input={}, error="boom"),
        env,
    )
    assert ev.kind == "post_tool_failure"
    assert ev.output_text == "boom"


def test_parse_session_start_and_prompt(tmp_path: Path) -> None:
    a = ClaudeAdapter()
    env = _env(tmp_path)
    ev = a.parse("SessionStart", _payload("SessionStart", source="resume", model="m-1"), env)
    assert ev.kind == "session_start" and ev.model == "m-1" and ev.extra["source"] == "resume"
    assert ev.tool is None
    ev = a.parse("UserPromptSubmit", _payload("UserPromptSubmit", prompt="hi"), env)
    assert ev.kind == "prompt" and ev.prompt == "hi"
    for name, kind in (("PreCompact", "pre_compact"), ("SessionEnd", "session_end")):
        assert a.parse(name, _payload(name), env).kind == kind


def test_parse_project_dir_falls_back_to_cwd(tmp_path: Path) -> None:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    ev = ClaudeAdapter().parse("SessionEnd", _payload("SessionEnd", cwd=str(tmp_path)), env)
    assert ev.project_dir == tmp_path


def test_parse_rejects_unknown_event_and_missing_dir(tmp_path: Path) -> None:
    a = ClaudeAdapter()
    with pytest.raises(ValueError):
        a.parse("Bogus", _payload("Bogus"), _env(tmp_path))
    payload = _payload("SessionEnd")
    del payload["cwd"]
    with pytest.raises(ValueError):
        a.parse("SessionEnd", payload, {"TOKEN_SPAREN_HOME": str(tmp_path)})


def _ev(tmp_path: Path, name: str, **extra: Any) -> Any:
    return ClaudeAdapter().parse(name, _payload(name, **extra), _env(tmp_path))


def test_render_noop_is_empty(tmp_path: Path) -> None:
    assert ClaudeAdapter().render(_ev(tmp_path, "SessionEnd"), Action()) == {}


def test_render_post_tool_replace(tmp_path: Path) -> None:
    ev = _ev(tmp_path, "PostToolUse", tool_name="Bash", tool_input={}, tool_response=BASH)
    out = ClaudeAdapter().render(ev, Action(replace_output="short"))
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": {
                "stdout": "short",
                "stderr": "",
                "interrupted": True,
                "isImage": False,
            },
        }
    }


def test_render_post_tool_unknown_shape_is_noop(tmp_path: Path) -> None:
    ev = _ev(tmp_path, "PostToolUse", tool_name="Grep", tool_input={}, tool_response=["x"])
    assert ClaudeAdapter().render(ev, Action(replace_output="short")) == {}


def test_render_pre_tool_block_and_rewrite(tmp_path: Path) -> None:
    ev = _ev(tmp_path, "PreToolUse", tool_name="Bash", tool_input={"command": "x"})
    a = ClaudeAdapter()
    assert a.render(ev, Action(block_reason="no")) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no",
        }
    }
    out = a.render(ev, Action(rewrite_input={"command": "y"}))
    assert out == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"command": "y"}}
    }
    # block wins over rewrite; a block never carries updatedInput
    out = a.render(ev, Action(block_reason="no", rewrite_input={"command": "y"}))
    assert "updatedInput" not in out["hookSpecificOutput"]


@pytest.mark.parametrize("name", ["SessionStart", "UserPromptSubmit", "PostToolUse"])
def test_render_inject_context_combined(tmp_path: Path, name: str) -> None:
    ev = _ev(tmp_path, name, tool_name="Bash", tool_input={}, tool_response=BASH)
    out = ClaudeAdapter().render(ev, Action(inject_context="ctx", user_message="hello"))
    assert out == {
        "systemMessage": "hello",
        "hookSpecificOutput": {"hookEventName": name, "additionalContext": "ctx"},
    }


def test_render_failure_takes_context_but_never_replaces(tmp_path: Path) -> None:
    """PostToolUseFailure accepts ``additionalContext`` (the loop guard's redirect) but its
    result can never be replaced."""
    ev = _ev(tmp_path, "PostToolUseFailure", tool_name="Bash", tool_input={}, error="boom")
    out = ClaudeAdapter().render(ev, Action(inject_context="ctx", replace_output="new"))
    assert out == {
        "hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": "ctx"}
    }


def test_render_ignores_unsupported_fields(tmp_path: Path) -> None:
    a = ClaudeAdapter()
    ev = _ev(tmp_path, "PreCompact")
    act = Action(inject_context="c", replace_output="r", block_reason="b", rewrite_input={})
    assert a.render(ev, act) == {}
    assert a.render(ev, Action(user_message="m")) == {"systemMessage": "m"}


def test_render_bash_preserves_persisted_output_fields() -> None:
    orig = {**BASH, "persistedOutputPath": "/t/out.txt", "persistedOutputSize": 123456}
    out = render_tool_output("Bash", orig, "new")
    assert out == {
        "stdout": "new",
        "stderr": "",
        "interrupted": True,
        "isImage": False,
        "persistedOutputPath": "/t/out.txt",
        "persistedOutputSize": 123456,
    }


def test_replaceable(tmp_path: Path) -> None:
    ok = _ev(tmp_path, "PostToolUse", tool_name="Bash", tool_input={}, tool_response=BASH)
    assert translate.replaceable(ok)
    img = {**BASH, "isImage": True}
    bad = _ev(tmp_path, "PostToolUse", tool_name="Bash", tool_input={}, tool_response=img)
    assert not translate.replaceable(bad)
    fail = _ev(tmp_path, "PostToolUseFailure", tool_name="Bash", tool_input={}, error="boom")
    assert not translate.replaceable(fail)  # Claude Code only adds context on failures
    assert not translate.replaceable(_ev(tmp_path, "SessionEnd"))


# --- Agent / Task results (documented object shape) ---------------------------------------

AGENT = {
    "status": "completed",
    "agentId": "a-1",
    "content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
    "resolvedModel": "m",
    "modelsUsed": ["m"],
    "totalTokens": 1234,
    "totalDurationMs": 50,
    "totalToolUseCount": 3,
    "usage": {"input_tokens": 1, "output_tokens": 2},
}


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_agent_output_text_joins_text_blocks(tool: str) -> None:
    assert translate.output_text(tool, AGENT) == "part one\npart two"


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_render_agent_shape_keeps_other_fields(tool: str) -> None:
    orig = copy.deepcopy(AGENT)
    out = render_tool_output(tool, AGENT, "short")
    assert out == {**orig, "content": [{"type": "text", "text": "short"}]}
    assert AGENT == orig  # original untouched


@pytest.mark.parametrize(
    "original",
    [
        {**AGENT, "content": []},
        {**AGENT, "content": "text"},
        {**AGENT, "content": [{"type": "image", "source": {}}]},
        {**AGENT, "content": [{"type": "text", "text": "a"}, {"type": "image", "source": {}}]},
        {**AGENT, "content": [{"type": "text", "text": 5}]},
        {k: v for k, v in AGENT.items() if k != "content"},
    ],
)
def test_agent_unsupported_blocks_untouched(original: Any) -> None:
    assert render_tool_output("Agent", original, "new") is None
    assert translate.output_text("Agent", original) is None


def test_agent_shape_only_for_agent_tools() -> None:
    assert render_tool_output("WebFetch", AGENT, "new") is None
    assert translate.output_text("WebFetch", AGENT) is None


def test_agent_post_tool_parse_and_render(tmp_path: Path) -> None:
    ad = ClaudeAdapter()
    ev = ad.parse(
        "PostToolUse",
        _payload("PostToolUse", tool_name="Agent", tool_input={"prompt": "p"}, tool_response=AGENT),
        _env(tmp_path),
    )
    assert ev.output_text == "part one\npart two"
    assert ad.replaceable(ev)
    out = ad.render(ev, Action(replace_output="kept"))
    new = out["hookSpecificOutput"]["updatedToolOutput"]
    assert new["content"] == [{"type": "text", "text": "kept"}]
    assert new["agentId"] == "a-1" and new["usage"] == AGENT["usage"]
