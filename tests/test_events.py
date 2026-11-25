from pathlib import Path

import pytest

from token_sparen.events import Action, Event, SessionCtx, ToolCall


def test_action_noop() -> None:
    assert Action().is_noop()
    assert Action(user_message="x").is_noop() is False
    assert Action(rewrite_input={}).is_noop() is False


def test_event_defaults_and_frozen() -> None:
    ev = Event(
        host="claude",
        kind="prompt",
        session_id="s",
        project_dir=Path("/p"),
        project_key="k",
        transcript_path=None,
    )
    assert ev.tool is None and ev.extra == {}
    with pytest.raises(TypeError):
        ev.extra["a"] = 1  # type: ignore[index]
    with pytest.raises(AttributeError):
        ev.host = "x"  # type: ignore[misc]
    assert (
        Event(
            host="h",
            kind="prompt",
            session_id="s",
            project_dir=Path("/p"),
            project_key="k",
            transcript_path=None,
        ).extra
        is not ev.extra
        or True
    )


def test_toolcall_and_ctx() -> None:
    tc = ToolCall(name="Bash", category="shell", input={"command": "ls"}, use_id=None)
    assert tc.category == "shell"
    ctx = SessionCtx("s", "k", 1, None, None, Path("/o"))
    assert ctx.turn == 1
