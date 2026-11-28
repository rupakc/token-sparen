"""Task-boundary detection (advice to /compact) and PreCompact summary instructions."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from userconf import to_user_config

from token_sparen.config import Config, load_config
from token_sparen.events import Action, Event, EventKind, ToolCall
from token_sparen.mechanisms import boundary, governor, tracking
from token_sparen.store import session

KEY = "0123456789abcdef"


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home"), **over}


def _cfg(tmp_path: Path, **over: str) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _transcript(tmp_path: Path, ctx: int) -> Path:
    usage = {"input_tokens": ctx, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    rec = {"type": "assistant", "message": {"id": f"m{ctx}", "model": "m", "usage": usage}}
    path = tmp_path / "t.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return path


def _event(
    tmp_path: Path,
    kind: EventKind,
    transcript: Path | None = None,
    prompt: str | None = None,
    tool: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    call = None if tool is None else ToolCall(tool, tool_category(tool), tool_input or {}, "t")
    return Event(
        host="claude",
        kind=kind,
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=transcript,
        tool=call,
        prompt=prompt,
        extra=MappingProxyType({}),
    )


def _prompt(tmp_path: Path, text: str, ctx: int = 120_000, **over: str) -> str | None:
    """One UserPromptSubmit through tracking -> governor -> boundary, as the entry chain does."""
    env = _env(tmp_path, **over)
    cfg = _cfg(tmp_path, **over)
    ev = _event(tmp_path, "prompt", _transcript(tmp_path, ctx), prompt=text)
    tracking.handle_prompt(ev, cfg, env=env)
    governor.on_prompt(ev, cfg, env=env)
    return boundary.on_prompt(ev, cfg, env=env).user_message


TOPIC_A = [
    "Fix the failing test in `parse_config` inside src/config_loader.py",
    "Now make parse_config handle missing keys, see src/config_loader.py",
    "Add a test for loadDefaults and parse_config in tests/test_config_loader.py",
]
TOPIC_B = "Write a README section about `deploy_pipeline` and the release_checklist.md"


def test_identifiers() -> None:
    ids = boundary.identifiers(
        "Look at src/app/main_loop.py and `Widget` then fetchUserData, user_id, a_b, x.y"
    )
    assert {"main_loop.py", "widget", "fetchuserdata", "user_id"} <= ids
    assert "a_b" not in ids  # shorter than 4 chars
    assert boundary.identifiers("yes please go ahead") == set()


def test_containment() -> None:
    assert boundary.containment({"a", "b"}, {"b", "c", "d", "e"}) == pytest.approx(1 / 2)
    assert boundary.containment(set(), {"a"}) == 0.0


def test_known_identifiers_against_large_history_do_not_trigger(tmp_path: Path) -> None:
    # reviewer probe: every identifier of the prompt is known, but the 3-prompt union is large
    # (Jaccard 3/21 = 0.143 would have advised /compact); containment is 3/3
    history = [
        "Refactor alpha_one beta_two gamma_three delta_four epsilon_five sigma_six tau_seven",
        "Then zeta_eight eta_nine theta_ten iota_eleven kappa_twelve lambda_thirteen mu_fourteen",
        "Also nu_fifteen xi_sixteen omicron_seventeen pi_eighteen rho_nineteen phi_twenty chi_x21",
    ]
    for text in history:  # unrelated to each other, so build them up at low context
        assert _prompt(tmp_path, text, ctx=10_000) is None
    assert _prompt(tmp_path, "Now rename alpha_one, theta_ten and phi_twenty") is None
    assert _prompt(tmp_path, TOPIC_B) is not None  # a true shift still triggers


def test_no_boundary_advice_when_window_unknown(tmp_path: Path) -> None:
    for text in TOPIC_A:
        _prompt(tmp_path, text)
    assert _prompt(tmp_path, TOPIC_B, ctx=300_000) is None  # last call > window: unknown


def test_boundary_triggers_on_topic_shift(tmp_path: Path) -> None:
    for text in TOPIC_A:
        assert _prompt(tmp_path, text) is None
    msg = _prompt(tmp_path, TOPIC_B)
    assert msg is not None and msg.startswith("token-sparen:")
    assert "/compact" in msg and "60 %" in msg and "\n" not in msg


def test_detect_is_pure(tmp_path: Path) -> None:
    for text in TOPIC_A:
        _prompt(tmp_path, text)
    env = _env(tmp_path)
    ev = _event(tmp_path, "prompt")
    path = tracking.overlay_path(ev, env)
    sctx = tracking.session_ctx(ev, session.read_session(path), path)
    before = session.read_session(path)
    assert boundary.detect(TOPIC_B, sctx, _cfg(tmp_path), ctx_pct=0.6) is True
    assert boundary.detect(TOPIC_B, sctx, _cfg(tmp_path), ctx_pct=0.3) is False
    assert boundary.detect(TOPIC_A[0], sctx, _cfg(tmp_path), ctx_pct=0.6) is False
    assert session.read_session(path) == before


def test_no_trigger_same_topic(tmp_path: Path) -> None:
    for text in [*TOPIC_A, "parse_config still fails in src/config_loader.py"]:
        assert _prompt(tmp_path, text) is None


def test_no_trigger_when_context_small(tmp_path: Path) -> None:
    for text in TOPIC_A:
        _prompt(tmp_path, text, ctx=10_000)
    assert _prompt(tmp_path, TOPIC_B, ctx=60_000) is None  # 30 % < min_ctx_pct 40 %


def test_no_trigger_before_turn_three(tmp_path: Path) -> None:
    _prompt(tmp_path, TOPIC_A[0])
    assert _prompt(tmp_path, TOPIC_B) is None  # turn 2


def test_no_trigger_without_identifiers(tmp_path: Path) -> None:
    for text in TOPIC_A:
        _prompt(tmp_path, text)
    assert _prompt(tmp_path, "ok, carry on please") is None


def test_recent_paths_count_as_context(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for name in ("deploy_pipeline.py", "release_checklist.md"):
        ev = _event(tmp_path, "post_tool", tool="Read", tool_input={"file_path": f"/r/{name}"})
        tracking.record_paths(ev, env=env)
    for text in TOPIC_A:
        _prompt(tmp_path, text)
    # the "new" topic was just being read about: overlap with recent paths, no advice
    assert _prompt(tmp_path, "Update deploy_pipeline.py and release_checklist.md") is None


def test_boundary_rate_limit(tmp_path: Path) -> None:
    for text in TOPIC_A:
        _prompt(tmp_path, text)
    assert _prompt(tmp_path, TOPIC_B) is not None  # turn 4
    topics = [f"Look into module_{c}{c}{c}{c}_handler.py now" for c in "cdefghijklmnop"]
    got = [_prompt(tmp_path, t) for t in topics[:9]]  # turns 5..13: every prompt shifts topic
    assert got == [None] * 9
    assert _prompt(tmp_path, topics[9]) is not None  # turn 14 = 10 turns later


def test_boundary_disabled(tmp_path: Path) -> None:
    over = {"TOKEN_SPAREN_BOUNDARY__ENABLED": "false"}
    for text in TOPIC_A:
        _prompt(tmp_path, text, **over)
    assert _prompt(tmp_path, TOPIC_B, **over) is None


def test_boundary_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.store import jsonstore

    for text in TOPIC_A:
        _prompt(tmp_path, text)

    def busy(*a: Any, **k: Any) -> Any:
        raise jsonstore.StoreBusy("x")

    monkeypatch.setattr(session, "update_session", busy)
    ev = _event(tmp_path, "prompt", _transcript(tmp_path, 120_000), prompt=TOPIC_B)
    assert boundary.on_prompt(ev, _cfg(tmp_path), env=_env(tmp_path)).user_message is None


# --------------------------------------------------------------------------- PreCompact


def test_precompact_keeps_recovery_paths(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for tool, name in (("Edit", "a_mod.py"), ("Read", "only_read.py"), ("Write", "b_new.py")):
        ev = _event(tmp_path, "post_tool", tool=tool, tool_input={"file_path": f"/w/{name}"})
        tracking.record_paths(ev, env=env)
    act = boundary.on_pre_compact(_event(tmp_path, "pre_compact"), _cfg(tmp_path), env=env)
    text = act.compact_instructions
    assert text is not None
    assert "token-sparen:" in text and "full output" in text and "token-sparen show" in text
    assert "/w/a_mod.py" in text and "/w/b_new.py" in text
    assert "only_read.py" not in text
    assert len(text) < 10_000


def test_precompact_without_markers_or_edits_is_noop(tmp_path: Path) -> None:
    act = boundary.on_pre_compact(_event(tmp_path, "pre_compact"), _cfg(tmp_path))
    assert act.is_noop()


def test_precompact_with_markers_only(tmp_path: Path) -> None:
    env = _env(tmp_path)
    ev = _event(tmp_path, "pre_compact")
    session.update_session(tracking.overlay_path(ev, env), lambda s: {**s, "trims": 2})
    text = boundary.on_pre_compact(ev, _cfg(tmp_path), env=env).compact_instructions
    assert text is not None and "full output" in text and "files edited" not in text


def test_precompact_instructions_have_balanced_backticks() -> None:
    assert boundary.INSTRUCTIONS.count("`") % 2 == 0
    assert "``" not in boundary.INSTRUCTIONS


def test_render_text_only_for_pre_compact(tmp_path: Path) -> None:
    from token_sparen.adapters.claude.translate import ClaudeAdapter

    a = ClaudeAdapter()
    act = Action(compact_instructions="keep markers")
    assert a.render_text(_event(tmp_path, "pre_compact"), act) == "keep markers"
    assert a.render_text(_event(tmp_path, "prompt"), act) is None
    assert a.render_text(_event(tmp_path, "pre_compact"), Action()) is None
    assert not act.is_noop()


def test_precompact_composes_with_read_dedupe_via_entry(tmp_path: Path) -> None:
    from token_sparen.adapters.claude import entry

    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
        "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000",
    }
    env = to_user_config(env)
    (tmp_path / "proj").mkdir(exist_ok=True)

    def run(name: str, payload: dict[str, Any]) -> str:
        out = io.StringIO()
        entry.main(name, io.StringIO(json.dumps({"session_id": "s-c", **payload})), out, env)
        return out.getvalue()

    run("UserPromptSubmit", {"prompt": "one"})
    run("UserPromptSubmit", {"prompt": "two"})
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "e_file.py")}}
    run("PostToolUse", {**edit, "tool_response": "ok"})
    text = run("PreCompact", {"trigger": "manual", "custom_instructions": None})
    assert not text.lstrip().startswith("{")  # plain text: Claude Code uses it as instructions
    assert "full output" in text and (tmp_path / "e_file.py").as_posix() in text.replace("\\", "/")
    overlay = json.loads(next((tmp_path / "home").rglob("s-c.json")).read_text(encoding="utf-8"))
    assert overlay["last_compact_turn"] == 2 and overlay["reads"] == {}


def test_precompact_disabled_kill_switch_is_json_noop(tmp_path: Path) -> None:
    from token_sparen.adapters.claude import entry

    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "TOKEN_SPAREN_DISABLE": "1",
    }
    out = io.StringIO()
    entry.main("PreCompact", io.StringIO(json.dumps({"session_id": "s"})), out, env)
    assert out.getvalue() == "{}\n"
