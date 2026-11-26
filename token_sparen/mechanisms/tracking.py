"""Session overlay bookkeeping used by mechanisms: turn count, last prompt, touched paths.

The overlay is a per-session local file (never a shared store); it holds the last user prompt
and the recently touched file paths so compressors can score relevance.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config, log_error, state_home
from token_sparen.events import Action, Event, SessionCtx
from token_sparen.store import jsonstore, session

PROMPT_MAX_CHARS = 2000
RECENT_PATHS = 20
_PATH_CATEGORIES = frozenset({"read", "edit"})


def overlay_path(event: Event, env: Mapping[str, str] | None = None) -> Path:
    home = state_home(os.environ if env is None else env)
    return session.session_path(home, event.project_key, event.session_id)


def session_ctx(event: Event, overlay: Mapping[str, Any], path: Path) -> SessionCtx:
    turn = overlay.get("turn")
    return SessionCtx(
        session_id=event.session_id,
        project_key=event.project_key,
        turn=turn if isinstance(turn, int) and not isinstance(turn, bool) else 0,
        ctx_pct=None,
        model=event.model,
        overlay_path=path,
    )


def edited_paths(overlay: Mapping[str, Any]) -> list[str]:
    value = overlay.get("edited_paths")
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def last_prompt(overlay: Mapping[str, Any]) -> str | None:
    value = overlay.get("last_prompt")
    return value if isinstance(value, str) else None


def recent_paths(overlay: Mapping[str, Any]) -> list[str]:
    value = overlay.get("recent_paths")
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _update(path: Path, fn: Any, env: Mapping[str, str] | None) -> None:
    try:
        session.update_session(path, fn)
    except (jsonstore.StoreBusy, OSError) as exc:  # bookkeeping is best effort
        log_error(os.environ if env is None else env, "tracking", f"{type(exc).__name__}: {exc}")


def handle_prompt(event: Event, cfg: Config, *, env: Mapping[str, str] | None = None) -> Action:
    """UserPromptSubmit: count the turn and remember the (truncated) prompt."""
    prompt = (event.prompt or "")[:PROMPT_MAX_CHARS]

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        turn = state.get("turn")
        prior = turn if isinstance(turn, int) and not isinstance(turn, bool) else 0
        state["turn"] = prior + 1
        state["last_prompt"] = prompt
        return state

    _update(overlay_path(event, env), apply, env)
    return Action()


def touched_path(event: Event) -> str | None:
    tool = event.tool
    if tool is None or tool.category not in _PATH_CATEGORIES:
        return None
    value = tool.input.get("file_path")
    return value if isinstance(value, str) and value else None


def record_paths(event: Event, *, env: Mapping[str, str] | None = None) -> None:
    """Remember the file a Read/Edit/Write-style tool touched (most recent last, max 20).

    Edited files are also kept apart (``edited_paths``) for the PreCompact summary instructions.
    """
    file_path = touched_path(event)
    if file_path is None:
        return
    edited = event.tool is not None and event.tool.category == "edit"

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        paths = [p for p in recent_paths(state) if p != file_path]
        state["recent_paths"] = [*paths, file_path][-RECENT_PATHS:]
        if edited:
            done = [p for p in edited_paths(state) if p != file_path]
            state["edited_paths"] = [*done, file_path][-RECENT_PATHS:]
        return state

    _update(overlay_path(event, env), apply, env)
