"""Session overlay bookkeeping used by mechanisms: turn count, last prompt, touched paths,
recent tool categories.

The overlay is a per-session local file (never a shared store); it holds the last user prompt
and the recently touched file paths so compressors can score relevance, and the last tool
categories (``recent_cats``) from which the learning engine infers the task phase. It also keeps
the session's transcript path and project directory (``record_transcript``), which the fold mines
for repository facts (``facts.miners``; never read by a hook).
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from token_sparen.config import Config, log_error, state_home
from token_sparen.events import Action, Event, SessionCtx
from token_sparen.store import jsonstore, paths, session

LAST_ACTIVE_FILE = "last_active.json"
PROMPT_MAX_CHARS = 2000
RECENT_PATHS = 20
RECENT_CATS = 10  # tool calls kept for task-phase inference (learn.features.phase)
FAILED_SUFFIX = "!"  # a failed tool call is recorded as "<category>!"
_PATH_CATEGORIES = frozenset({"read", "edit"})


def _count(state: Mapping[str, Any], key: str) -> int:
    value = state.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def ledger_savings(state: Mapping[str, Any]) -> tuple[int, int]:
    """(gross, net) tokens saved by a session overlay's ledger: gross = ``tokens_removed``; net
    = gross - ``recovery_read_tokens`` - ``injected_tokens`` (the statusline's ``saved``)."""
    gross = _count(state, "tokens_removed")
    return gross, gross - _count(state, "recovery_read_tokens") - _count(state, "injected_tokens")


def overlay_path(event: Event, env: Mapping[str, str] | None = None) -> Path:
    home = state_home(os.environ if env is None else env)
    return session.session_path(home, event.project_key, event.session_id)


def session_ctx(
    event: Event, overlay: Mapping[str, Any], path: Path, *, ctx_pct: float | None = None
) -> SessionCtx:
    """``ctx_pct``: context fill fraction when known (``governor.ctx_pct``; decision points pass
    it so the ``band`` feature is real), else None."""
    turn = overlay.get("turn")
    return SessionCtx(
        session_id=event.session_id,
        project_key=event.project_key,
        turn=turn if isinstance(turn, int) and not isinstance(turn, bool) else 0,
        ctx_pct=ctx_pct,
        model=event.model,
        overlay_path=path,
        recent_cats=recent_cats(overlay),
    )


def recent_cats(overlay: Mapping[str, Any]) -> tuple[str, ...]:
    value = overlay.get("recent_cats")
    if not isinstance(value, list):
        return ()
    return tuple(c for c in value if isinstance(c, str))[-RECENT_CATS:]


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
        _set_transcript(state, event)
        return state

    _update(overlay_path(event, env), apply, env)
    record_active(event, env=env)
    return Action()


TRANSCRIPT_KEY = "transcript"  # read by learn.overlay (fold) only
PROJECT_DIR_KEY = "project_dir"


def _set_transcript(state: dict[str, Any], event: Event) -> bool:
    """Record the transcript path and project dir; True if the overlay changed."""
    if event.transcript_path is None:
        return False
    path, root = str(event.transcript_path), str(event.project_dir)
    if state.get(TRANSCRIPT_KEY) == path and state.get(PROJECT_DIR_KEY) == root:
        return False
    state[TRANSCRIPT_KEY] = path
    state[PROJECT_DIR_KEY] = root
    return True


def record_transcript(event: Event, *, env: Mapping[str, str] | None = None) -> None:
    """SessionStart: remember where the session's transcript lives, for fold-time fact mining."""
    if event.transcript_path is None or event.session_id == "unknown":
        return

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        _set_transcript(state, event)
        return state

    _update(overlay_path(event, env), apply, env)


def last_active_path(home: Path, project_key: str) -> Path:
    return paths.project_dir_for(home, project_key) / LAST_ACTIVE_FILE


def record_active(event: Event, *, env: Mapping[str, str] | None = None) -> None:
    """Remember the most recently active session of this project (SessionStart, prompts).

    ``token-sparen session disable|enable`` without ``--session`` acts on it: the Bash tool's
    environment carries no session id.
    """
    if event.session_id == "unknown":
        return
    env = os.environ if env is None else env
    path = last_active_path(state_home(env), event.project_key)

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": event.session_id, "ts": time.time()}

    try:
        jsonstore.update(path, apply, {})
    except (jsonstore.StoreBusy, OSError) as exc:  # best effort
        log_error(env, "tracking", f"last_active: {type(exc).__name__}: {exc}")


def last_active(home: Path, project_key: str, max_age_s: float) -> tuple[str | None, bool]:
    """(session id, fresh): the last active session and whether it is younger than max_age_s."""
    state = jsonstore.read(last_active_path(home, project_key), {})
    sid, ts = state.get("session_id"), state.get("ts")
    if not isinstance(sid, str) or not sid:
        return None, False
    fresh = isinstance(ts, int | float) and 0 <= time.time() - float(ts) < max_age_s
    return sid, fresh


def touched_path(event: Event) -> str | None:
    tool = event.tool
    if tool is None or tool.category not in _PATH_CATEGORIES:
        return None
    value = tool.input.get("file_path")
    return value if isinstance(value, str) and value else None


def record_paths(event: Event, *, env: Mapping[str, str] | None = None) -> None:
    """Post-tool bookkeeping: the tool category (last 10, failures as ``<cat>!``) and the file a
    Read/Edit/Write-style tool touched (most recent last, max 20).

    Edited files are also kept apart (``edited_paths``) for the PreCompact summary instructions.
    """
    tool = event.tool
    if tool is None:
        return
    cat = tool.category + (FAILED_SUFFIX if event.kind == "post_tool_failure" else "")
    file_path = touched_path(event)
    edited = tool.category == "edit"

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        state["recent_cats"] = [*recent_cats(state), cat][-RECENT_CATS:]
        if file_path is None:
            return state
        paths = [p for p in recent_paths(state) if p != file_path]
        state["recent_paths"] = [*paths, file_path][-RECENT_PATHS:]
        if edited:
            done = [p for p in edited_paths(state) if p != file_path]
            state["edited_paths"] = [*done, file_path][-RECENT_PATHS:]
        return state

    _update(overlay_path(event, env), apply, env)
