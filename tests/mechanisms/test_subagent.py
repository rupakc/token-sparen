"""Subagent shaping: a one-line brief appended to Task/Agent prompts (decision point)."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from userconf import to_user_config

from token_sparen.config import Config, load_config
from token_sparen.events import Event, ToolCall
from token_sparen.learn.policy import SUBAGENT_BRIEF, StaticPolicy
from token_sparen.mechanisms import subagent

KEY = "0123456789abcdef"
BRIEF = "Return conclusions, file paths and line numbers rather than raw dumps."
ON = StaticPolicy({"subagent_brief": "on"})


def _env(tmp_path: Path) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}


def _cfg(tmp_path: Path) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path))


def _event(tmp_path: Path, tool: str, tool_input: Mapping[str, Any]) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind="pre_tool",
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall(tool, tool_category(tool), dict(tool_input), "tu-9"),
        extra=MappingProxyType({}),
    )


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_point_contract() -> None:
    assert SUBAGENT_BRIEF.name == "subagent_brief"
    assert SUBAGENT_BRIEF.arms == ("off", "on")
    assert (SUBAGENT_BRIEF.safe_arm, SUBAGENT_BRIEF.tier) == ("off", "medium")
    assert subagent.brief() == BRIEF


def test_subagent_brief_appended_once(tmp_path: Path) -> None:
    inp = {"description": "find x", "prompt": "Find where parse_config is defined", "n": 1}
    act = subagent.handle_pre_tool(
        _event(tmp_path, "Task", inp), ON, _cfg(tmp_path), env=_env(tmp_path)
    )
    assert act.rewrite_input == {**inp, "prompt": inp["prompt"] + "\n\n" + BRIEF}
    # idempotent: the rewritten prompt is not extended again
    again = subagent.handle_pre_tool(
        _event(tmp_path, "Agent", act.rewrite_input), ON, _cfg(tmp_path), env=_env(tmp_path)
    )
    assert again.is_noop()
    recs = _decisions(tmp_path)
    assert [r["point"] for r in recs] == ["subagent_brief"]
    assert recs[0]["arm"] == "on" and recs[0]["tool_use_id"] == "tu-9"
    assert recs[0]["tokens_after"] > recs[0]["tokens_before"]


def test_default_arm_is_off_and_logged(tmp_path: Path) -> None:
    inp = {"prompt": "Survey the repo"}
    pol = StaticPolicy({})
    act = subagent.handle_pre_tool(
        _event(tmp_path, "Agent", inp), pol, _cfg(tmp_path), env=_env(tmp_path)
    )
    assert act.is_noop()
    recs = _decisions(tmp_path)
    assert len(recs) == 1 and recs[0]["arm"] == "off"
    assert recs[0]["tokens_after"] == recs[0]["tokens_before"]


def test_non_agent_or_bad_prompt_untouched(tmp_path: Path) -> None:
    cfg, env = _cfg(tmp_path), _env(tmp_path)
    for tool, inp in (("Bash", {"prompt": "x"}), ("Task", {"prompt": 3}), ("Agent", {})):
        assert subagent.handle_pre_tool(_event(tmp_path, tool, inp), ON, cfg, env=env).is_noop()
    assert _decisions(tmp_path) == []


def test_disabled(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "TOKEN_SPAREN_SUBAGENT__ENABLED": "false"}
    cfg = load_config(tmp_path / "proj", env)
    act = subagent.handle_pre_tool(_event(tmp_path, "Task", {"prompt": "x"}), ON, cfg, env=env)
    assert act.is_noop()


def test_entry_wiring_rewrites_with_on_policy(tmp_path: Path, monkeypatch: Any) -> None:
    from token_sparen.adapters.claude import entry

    monkeypatch.setattr(entry, "POLICY", ON)
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
        "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000",
    }
    env = to_user_config(env)
    (tmp_path / "proj").mkdir(exist_ok=True)
    payload = {"session_id": "s", "tool_name": "Task", "tool_input": {"prompt": "go"}}
    out = io.StringIO()
    entry.main("PreToolUse", io.StringIO(json.dumps(payload)), out, env)
    spec = json.loads(out.getvalue())["hookSpecificOutput"]
    assert spec["updatedInput"] == {"prompt": "go\n\n" + BRIEF}
    assert "permissionDecision" not in spec


def _agent_post(tmp_path: Path, response: Any, *, widen: bool = True) -> dict[str, Any]:
    """PostToolUse for an Agent result; ``widen`` opts in to plain text (not trimmed by
    default)."""
    from token_sparen.adapters.claude import entry

    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
        "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000",
    }
    if widen:
        env["TOKEN_SPAREN_COMPRESSION__ALLOW_CTYPES"] = "text"
    env = to_user_config(env)  # widening settings live in the user's file (env only narrows)
    (tmp_path / "proj").mkdir(exist_ok=True)
    payload = {
        "session_id": "s",
        "tool_name": "Agent",
        "tool_input": {"prompt": "dump"},
        "tool_use_id": "tu-a",
        "tool_response": response,
    }
    out = io.StringIO()
    entry.main("PostToolUse", io.StringIO(json.dumps(payload)), out, env)
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


def _big() -> str:
    return "\n".join(f"step {i}: scanned module_{i}.py, nothing relevant" for i in range(3000))


def test_oversized_agent_string_result_is_never_compressed(tmp_path: Path) -> None:
    """v1 review I1: a subagent's report is never trimmed by a generic compressor, even with
    plain text allowed."""
    assert _agent_post(tmp_path, _big()) == {}


def test_agent_plain_text_result_untouched_by_default(tmp_path: Path) -> None:
    assert _agent_post(tmp_path, _big(), widen=False) == {}


def test_agent_structured_result_is_never_compressed(tmp_path: Path) -> None:
    # Claude Code's documented Agent result object: its text blocks stay whole too (I1)
    resp = {"content": [{"type": "text", "text": _big()}], "totalDurationMs": 5}
    assert _agent_post(tmp_path, resp) == {}


def test_agent_result_with_non_text_block_is_left_untouched(tmp_path: Path) -> None:
    resp = {"content": [{"type": "text", "text": _big()}, {"type": "image"}], "status": "ok"}
    assert _agent_post(tmp_path, resp) == {}
