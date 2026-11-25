"""Claude Code hook JSON <-> canonical Event / Action."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from token_sparen.events import Action, Event, EventKind, ToolCall, ToolCategory
from token_sparen.store.keycache import cached_project_key

EVENT_KINDS: dict[str, EventKind] = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "PostToolUseFailure": "post_tool_failure",
    "PreCompact": "pre_compact",
    "SessionEnd": "session_end",
}
_CATEGORIES: dict[str, ToolCategory] = {
    "Bash": "shell",
    "Read": "read",
    "NotebookRead": "read",
    "Grep": "search",
    "Glob": "search",
    "LS": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Edit": "edit",
    "Write": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Task": "agent",
    "Agent": "agent",
}
_BASH_KEEP = ("persistedOutputPath", "persistedOutputSize")
_INJECT_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse"})
_EXTRA_KEYS = (
    "hook_event_name",
    "permission_mode",
    "source",
    "trigger",
    "custom_instructions",
    "reason",
    "error",
    "is_interrupt",
    "agent_id",  # set inside a subagent: its context is separate from the main agent's
    "agent_type",
)


def tool_category(name: str) -> ToolCategory:
    if name.startswith("mcp__"):
        return "mcp"
    return _CATEGORIES.get(name, "other")


def _read_file(original: Any) -> dict[str, Any] | None:
    """The ``file`` dict of a text Read response, else None."""
    if not isinstance(original, dict) or original.get("type", "text") != "text":
        return None
    file = original.get("file")
    if isinstance(file, dict) and isinstance(file.get("content"), str):
        return file
    return None


def _is_bash(original: Any) -> bool:
    return (
        isinstance(original, dict)
        and isinstance(original.get("stdout"), str)
        and not original.get("isImage", False)
    )


def output_text(tool_name: str, response: Any) -> str | None:
    """Plain text of a tool response for the shapes we can also render back, else None."""
    if isinstance(response, str):
        return response
    if tool_name == "Bash" and _is_bash(response):
        stderr = response.get("stderr")
        text: str = response["stdout"]
        return f"{text}\n{stderr}" if isinstance(stderr, str) and stderr else text
    if tool_name == "Read":
        file = _read_file(response)
        return None if file is None else str(file["content"])
    return None


def render_tool_output(tool_name: str, original: Any, new_text: str) -> Any | None:
    """A replacement in the same shape as ``original``, or None (= do not replace)."""
    if isinstance(original, str):
        return new_text
    if tool_name == "Bash" and _is_bash(original):
        out: dict[str, Any] = {
            "stdout": new_text,
            "stderr": "",
            "interrupted": bool(original.get("interrupted", False)),
            "isImage": False,
        }
        for key in _BASH_KEEP:  # the full output Claude Code saved stays reachable
            if key in original:
                out[key] = original[key]
        return out
    if tool_name == "Read" and _read_file(original) is not None:
        read = copy.deepcopy(original)
        read["file"]["content"] = new_text
        return read
    return None


def replaceable(event: Event) -> bool:
    """Whether Claude Code can replace this event's tool output (PostToolUse only).

    PostToolUseFailure supports only ``additionalContext``, so a failure result cannot be
    replaced there; mechanisms must not do (or account for) work the host would discard.
    """
    if event.kind != "post_tool" or event.tool is None:
        return False
    return render_tool_output(event.tool.name, event.raw_response, "") is not None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ClaudeAdapter:
    name = "claude"
    capabilities = frozenset(
        {"replace_output", "rewrite_input", "block", "inject_context", "statusline"}
    )

    def parse(self, event_name: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> Event:
        kind = EVENT_KINDS.get(event_name)
        if kind is None:
            raise ValueError(f"unsupported Claude Code event: {event_name!r}")
        raw_dir = _str(env.get("CLAUDE_PROJECT_DIR")) or _str(payload.get("cwd"))
        if raw_dir is None:
            raise ValueError("no project dir (CLAUDE_PROJECT_DIR or cwd)")
        project_dir = Path(raw_dir)
        tool: ToolCall | None = None
        name = _str(payload.get("tool_name"))
        if name is not None:
            raw_input = payload.get("tool_input")
            tool = ToolCall(
                name=name,
                category=tool_category(name),
                input=dict(raw_input) if isinstance(raw_input, dict) else {},
                use_id=_str(payload.get("tool_use_id")),
            )
        response = payload.get("tool_response")
        text: str | None = None
        if kind == "post_tool" and name is not None:
            text = output_text(name, response)
        elif kind == "post_tool_failure":
            text = _str(payload.get("error"))
        transcript = _str(payload.get("transcript_path"))
        return Event(
            host=self.name,
            kind=kind,
            session_id=_str(payload.get("session_id")) or "unknown",
            project_dir=project_dir,
            project_key=cached_project_key(project_dir, env),
            transcript_path=Path(transcript) if transcript else None,
            tool=tool,
            output_text=text,
            raw_response=response,
            prompt=_str(payload.get("prompt")),
            model=_str(payload.get("model")),
            extra=MappingProxyType({k: payload[k] for k in _EXTRA_KEYS if k in payload}),
        )

    def render(self, event: Event, action: Action) -> dict[str, Any]:
        hook_event = next((h for h, k in EVENT_KINDS.items() if k == event.kind), "")
        out: dict[str, Any] = {}
        specific: dict[str, Any] = {}
        if event.kind == "pre_tool":
            if action.block_reason is not None:
                specific["permissionDecision"] = "deny"
                specific["permissionDecisionReason"] = action.block_reason
            elif action.rewrite_input is not None:
                specific["updatedInput"] = action.rewrite_input
        if event.kind == "post_tool" and action.replace_output is not None and event.tool:
            new = render_tool_output(event.tool.name, event.raw_response, action.replace_output)
            if new is not None:
                specific["updatedToolOutput"] = new
        if action.inject_context and hook_event in _INJECT_EVENTS:
            specific["additionalContext"] = action.inject_context
        if action.user_message:
            out["systemMessage"] = action.user_message
        if specific:
            out["hookSpecificOutput"] = {"hookEventName": hook_event, **specific}
        return out
