"""Canonical, host-independent event and action types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

ToolCategory = Literal["shell", "read", "search", "web", "edit", "agent", "mcp", "other"]
EventKind = Literal[
    "session_start",
    "prompt",
    "pre_tool",
    "post_tool",
    "post_tool_failure",
    "pre_compact",
    "session_end",
    "statusline",
]


@dataclass(frozen=True)
class ToolCall:
    name: str
    category: ToolCategory
    input: Mapping[str, Any]
    use_id: str | None


@dataclass(frozen=True)
class Event:
    host: str
    kind: EventKind
    session_id: str
    project_dir: Path
    project_key: str
    transcript_path: Path | None
    tool: ToolCall | None = None
    output_text: str | None = None
    raw_response: Any = None
    prompt: str | None = None
    model: str | None = None
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass
class Action:
    replace_output: str | None = None
    rewrite_input: dict[str, Any] | None = None
    block_reason: str | None = None
    inject_context: str | None = None
    user_message: str | None = None
    # PreCompact only: instructions for the compaction summary (Claude Code: plain-text stdout).
    compact_instructions: str | None = None

    def is_noop(self) -> bool:
        return (
            self.replace_output is None
            and self.rewrite_input is None
            and self.block_reason is None
            and self.inject_context is None
            and self.user_message is None
            and self.compact_instructions is None
        )


@dataclass
class SessionCtx:
    """Per-hook view of the session overlay; `turn` counts UserPromptSubmit events."""

    session_id: str
    project_key: str
    turn: int
    ctx_pct: float | None
    model: str | None
    overlay_path: Path
    # The session's last tool categories, oldest first; a failed call is "<category>!".
    recent_cats: tuple[str, ...] = ()
