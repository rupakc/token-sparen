"""Budget governor: incremental burn meter, risk/economy levels, advisories, opt-in hard stop."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Event, EventKind, SessionCtx, ToolCall
from token_sparen.mechanisms import governor, tracking
from token_sparen.store import session
from token_sparen.transcript import session_burn

KEY = "0123456789abcdef"


@pytest.fixture(autouse=True)
def _frozen_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Functional tests never depend on machine speed: the parse deadline cannot expire.

    Perf tests keep the real clock (they measure it); tests that need a deadline to expire
    install their own ticking clock.
    """
    if request.node.get_closest_marker("perf") is None:
        monkeypatch.setattr(governor, "_monotonic", lambda: 0.0)


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    env.update(over)
    return env


def _cfg(tmp_path: Path, **over: str) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _assistant(mid: str, ctx: int, model: str = "claude-test-1", **extra: Any) -> str:
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": ctx - 10 - 5,
        "cache_creation_input_tokens": 5,
        "output_tokens": 40,
    }
    rec = {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}, **extra}
    return json.dumps(rec)


def _user(text: str = "hi") -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def _transcript(tmp_path: Path, lines: list[str], name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


def _event(
    tmp_path: Path,
    kind: EventKind,
    transcript: Path | None,
    tool: str | None = None,
    tool_input: Mapping[str, Any] | None = None,
    prompt: str | None = None,
    session_id: str = "s-1",
) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    call = None
    if tool is not None:
        call = ToolCall(tool, tool_category(tool), dict(tool_input or {}), "tu-1")
    return Event(
        host="claude",
        kind=kind,
        session_id=session_id,
        project_dir=proj,
        project_key=KEY,
        transcript_path=transcript,
        tool=call,
        prompt=prompt,
        extra=MappingProxyType({}),
    )


def _ctx(tmp_path: Path, session_id: str = "s-1") -> SessionCtx:
    env = _env(tmp_path)
    ev = _event(tmp_path, "prompt", None, session_id=session_id)
    path = tracking.overlay_path(ev, env)
    return tracking.session_ctx(ev, session.read_session(path), path)


def _state(tmp_path: Path, transcript: Path, cfg: Config | None = None, **env: str) -> Any:
    cfg = cfg or _cfg(tmp_path, **env)
    return governor.state(_ctx(tmp_path), transcript, cfg, _env(tmp_path, **env))


def _prompt(tmp_path: Path, transcript: Path, cfg: Config, **env: str) -> str | None:
    e = _env(tmp_path, **env)
    ev = _event(tmp_path, "prompt", transcript, prompt="go")
    tracking.handle_prompt(ev, cfg, env=e)
    return governor.on_prompt(ev, cfg, env=e).user_message


# --------------------------------------------------------------------------- levels


def test_governor_levels(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_user(), _assistant("m1", 100_000)])
    st = _state(tmp_path, t)
    assert (st.window, st.burn, st.level) == (200_000, 100_000, "ok")
    assert st.ctx_pct == pytest.approx(0.5)
    _append(t, [_assistant("m2", 125_000)])
    st = _state(tmp_path, t)
    assert st.level == "notice" and st.ctx_pct == pytest.approx(0.625)
    assert st.burn == 225_000
    _append(t, [_assistant("m3", 170_000)])
    assert _state(tmp_path, t).level == "advise"
    # economy thresholds (absolute burn) work independently of the window
    t2 = _transcript(tmp_path, [_assistant("e1", 1000)], name="t2.jsonl")
    over = {"TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "1800"}
    ctx = _ctx(tmp_path, "s-econ")
    st = governor.state(ctx, t2, _cfg(tmp_path, **over), _env(tmp_path, **over))
    assert st.level == "notice" and st.economy_pct == pytest.approx(1000 / 1800)
    _append(t2, [_assistant("e2", 500)])
    st = governor.state(ctx, t2, _cfg(tmp_path, **over), _env(tmp_path, **over))
    assert st.level == "advise"
    _append(t2, [_assistant("e3", 500)])
    st = governor.state(ctx, t2, _cfg(tmp_path, **over), _env(tmp_path, **over))
    assert st.level == "advise"  # >= 100 % but hard stop is off by default
    hard = {**over, "TOKEN_SPAREN_GOVERNOR__HARD_STOP": "true"}
    st = governor.state(ctx, t2, _cfg(tmp_path, **hard), _env(tmp_path, **hard))
    assert st.level == "stop"


def test_defaults_packaged() -> None:
    from token_sparen.config import packaged_defaults

    g = packaged_defaults()["governor"]
    assert g["session_budget_tokens"] == 500_000_000
    assert (g["ctx_notice"], g["ctx_advise"]) == (0.60, 0.80)
    assert (g["econ_notice"], g["econ_advise"]) == (0.50, 0.80)
    assert g["hard_stop"] is False
    assert g["windows"] == {"default": 200_000}
    assert packaged_defaults()["boundary"]["min_ctx_pct"] == 0.40


def test_streamed_records_with_same_id_count_once(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 1000), _assistant("m1", 1000)])
    st = _state(tmp_path, t)
    assert st.burn == 1000 == session_burn(t)
    _append(t, [_assistant("m1", 1200), _assistant("m2", 300)])  # same id updated later
    assert _state(tmp_path, t).burn == 1500 == session_burn(t)


def test_sidechain_and_synthetic_ignored(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        [
            _assistant("m1", 1000),
            _assistant("s1", 9000, isSidechain=True),
            _assistant("x1", 9000, model="<synthetic>"),
            "not json",
            _user(),
        ],
    )
    st = _state(tmp_path, t)
    assert st.burn == 1000 and st.ctx_pct == pytest.approx(1000 / 200_000)


# --------------------------------------------------------------------------- incremental parse


def test_incremental_parse_reads_only_new_bytes(tmp_path: Path) -> None:
    lines = [_user("x" * 400), _assistant("m1", 1000), _assistant("m2", 2000)]
    t = _transcript(tmp_path, lines)
    assert _state(tmp_path, t).burn == 3000
    # rewrite an already parsed line in place (same length, past the head check): a re-parse
    # from 0 would see 9000, the incremental parser keeps its cached burn
    text = t.read_text(encoding="utf-8")
    changed = text.replace(_assistant("m2", 2000), _assistant("m2", 9000))
    assert len(changed) == len(text)
    t.write_text(changed, encoding="utf-8")
    _append(t, [_assistant("m3", 500)])
    st = _state(tmp_path, t)
    assert st.burn == 3500
    cache = session.read_session(_ctx(tmp_path).overlay_path)["governor"]
    assert cache["offset"] == t.stat().st_size


def test_partial_last_line_is_parsed_once_complete(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    line = _assistant("m2", 2000)
    with t.open("a", encoding="utf-8") as fh:
        fh.write(line[:20])
    assert _state(tmp_path, t).burn == 1000
    with t.open("a", encoding="utf-8") as fh:
        fh.write(line[20:] + "\n")
    assert _state(tmp_path, t).burn == 3000


def test_truncation_recomputes_from_zero(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 1000), _assistant("m2", 2000)])
    assert _state(tmp_path, t).burn == 3000
    t.write_text(_assistant("n1", 700) + "\n", encoding="utf-8")  # smaller: rotated/truncated
    assert _state(tmp_path, t).burn == 700 == session_burn(t)


def test_replaced_file_with_new_head_recomputes(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    assert _state(tmp_path, t).burn == 1000
    t.write_text(
        "".join(x + "\n" for x in [_assistant("z9", 4000), _assistant("z8", 4000)]),
        encoding="utf-8",
    )
    assert _state(tmp_path, t).burn == 8000


def test_other_transcript_path_recomputes(tmp_path: Path) -> None:
    a = _transcript(tmp_path, [_assistant("m1", 1000)], name="a.jsonl")
    b = _transcript(tmp_path, [_assistant("m1", 1000), _assistant("m2", 50)], name="b.jsonl")
    assert _state(tmp_path, a).burn == 1000
    assert _state(tmp_path, b).burn == 1050


def test_huge_unparsed_transcript_marks_burn_unknown(tmp_path: Path) -> None:
    lines = [_assistant(f"m{i}", 1000) for i in range(50)] + [_assistant("last", 150_000)]
    t = _transcript(tmp_path, lines)
    over = {"TOKEN_SPAREN_GOVERNOR__MAX_UNPARSED_BYTES": "1000"}
    st = _state(tmp_path, t, **over)
    assert st.burn_unknown is True
    assert st.ctx_pct == pytest.approx(0.75)  # last call still found in the tail
    assert st.level == "notice"
    _append(t, [_assistant("next", 120_000)])
    st = _state(tmp_path, t, **over)
    assert st.burn_unknown is True and st.ctx_pct == pytest.approx(0.6)


def test_catch_up_parse_continues_across_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic deadline: every clock read is 1 s after the previous one, so the 150 ms
    # parse budget expires after exactly one (tiny) chunk per hook on any machine.
    monkeypatch.setattr(governor, "CHUNK", 300)
    ticks = iter(range(10**6))
    monkeypatch.setattr(governor, "_monotonic", lambda: float(next(ticks)))
    t = _transcript(tmp_path, [_assistant(f"m{i}", 170_000) for i in range(20)])
    cfg = _cfg(tmp_path)
    first = _state(tmp_path, t, cfg)
    assert first.catching_up is True and first.burn < 20 * 170_000
    assert _prompt(tmp_path, t, cfg) is None  # no advice from a partial view
    hooks = 1
    for _ in range(100):
        hooks += 1
        st = _state(tmp_path, t, cfg)
        if not st.catching_up:
            break
    assert hooks > 3  # really spread over several hooks
    assert st.burn == session_burn(t) == 20 * 170_000
    assert _prompt(tmp_path, t, cfg) is not None


def _unique(n: int, start: int = 0) -> list[str]:
    return [_assistant(f"u{i}", 1000 + i) for i in range(start, start + n)]


def test_multi_chunk_scan_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governor, "CHUNK", 1000)  # many chunk boundaries split records
    t = _transcript(tmp_path, [x for i in range(300) for x in (_user(f"p{i}"), *_unique(1, i))])
    st = _state(tmp_path, t)
    assert st.catching_up is False
    assert st.burn == session_burn(t)
    cache = session.read_session(_ctx(tmp_path).overlay_path)["governor"]
    assert cache["offset"] == t.stat().st_size
    _append(t, _unique(50, 1000))
    assert _state(tmp_path, t).burn == session_burn(t)


def test_line_longer_than_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governor, "CHUNK", 200)
    big = _assistant("big", 7000, content="x" * 5000)  # a record far longer than a chunk
    t = _transcript(tmp_path, [*_unique(3), big, *_unique(3, 10)])
    st = _state(tmp_path, t)
    assert st.catching_up is False and st.burn == session_burn(t)
    _append(t, [_user("y" * 3000), *_unique(5, 20)])
    assert _state(tmp_path, t).burn == session_burn(t)


def test_line_over_cap_is_skipped_not_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(governor, "CHUNK", 200)
    over = {"TOKEN_SPAREN_GOVERNOR__MAX_UNPARSED_BYTES": "3000"}
    t = _transcript(tmp_path, _unique(2))
    cfg = _cfg(tmp_path, **over)
    assert _state(tmp_path, t, cfg, **over).burn == 2001
    _append(t, [_assistant("huge", 9000, content="x" * 4000), *_unique(1, 50)])
    st = _state(tmp_path, t, cfg, **over)
    assert st.catching_up is False
    assert st.burn == 2001 + 1050  # the oversized record is unparseable, the next one counts
    assert session.read_session(_ctx(tmp_path).overlay_path)["governor"]["offset"] == (
        t.stat().st_size
    )


def test_window_unknown_when_last_call_exceeds_it(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 300_000)])
    st = _state(tmp_path, t)
    assert st.window_unknown is True and st.ctx_level == "ok" and st.level == "ok"
    assert _prompt(tmp_path, t, _cfg(tmp_path)) is None


def test_advised_levels_skip_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 170_000)])
    ev = _event(tmp_path, "post_tool", t, tool="Bash")
    assert governor.on_post_tool(ev, cfg, env=_env(tmp_path)).user_message is not None
    calls: list[int] = []
    real = session.update_session

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(session, "update_session", spy)
    assert governor.on_post_tool(ev, cfg, env=_env(tmp_path)).user_message is None
    assert calls == []  # nothing new to parse, everything already said: no locked write


def test_burn_unknown_suppresses_economy(tmp_path: Path) -> None:
    lines = [_assistant(f"m{i}", 1000) for i in range(50)]
    t = _transcript(tmp_path, lines)
    over = {
        "TOKEN_SPAREN_GOVERNOR__MAX_UNPARSED_BYTES": "1000",
        "TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "10",
        "TOKEN_SPAREN_GOVERNOR__HARD_STOP": "true",
    }
    cfg = _cfg(tmp_path, **over)
    assert _state(tmp_path, t, cfg, **over).level == "ok"
    ev = _event(tmp_path, "pre_tool", t, tool="Read", tool_input={"file_path": "x"})
    assert governor.check_pre_tool(ev, cfg, env=_env(tmp_path, **over)).block_reason is None


def test_missing_transcript_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ev = _event(tmp_path, "prompt", tmp_path / "nope.jsonl", prompt="x")
    assert governor.on_prompt(ev, cfg, env=_env(tmp_path)).is_noop()
    ev = _event(tmp_path, "prompt", None, prompt="x")
    assert governor.on_prompt(ev, cfg, env=_env(tmp_path)).is_noop()


# --------------------------------------------------------------------------- burn meter


def test_per_turn_ewma(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    t = _transcript(tmp_path, [_user()])
    _prompt(tmp_path, t, cfg)  # turn 1 starts at burn 0
    _append(t, [_assistant("a", 1000)])
    _prompt(tmp_path, t, cfg)  # turn 1 burned 1000 -> ewma 1000
    st = governor.cached(session.read_session(_ctx(tmp_path).overlay_path))
    assert st is not None and st.per_turn_ewma == pytest.approx(1000)
    _append(t, [_assistant("b", 2000), _assistant("c", 2000)])
    _prompt(tmp_path, t, cfg)  # turn 2 burned 4000 -> 0.3 * 4000 + 0.7 * 1000
    st = governor.cached(session.read_session(_ctx(tmp_path).overlay_path))
    assert st is not None and st.per_turn_ewma == pytest.approx(1900)
    assert st.last_turn_burn == 4000


# --------------------------------------------------------------------------- window resolution


def test_window_resolution(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 100_000, model="claude-big-9")])
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    assert _state(tmp_path, t).window == 200_000
    # model-family table from config (fnmatch patterns)
    (home / "config.toml").write_text(
        '[governor.windows]\n"claude-big-*" = 400000\n', encoding="utf-8"
    )
    assert _state(tmp_path, t).window == 400_000
    # host env beats the table
    st = _state(tmp_path, t, CLAUDE_CODE_MAX_CONTEXT_TOKENS="250000")
    assert st.window == 250_000
    # the statusline-recorded window for this session wins
    (home / "windows.json").write_text(json.dumps({"s-1": {"window": 1_000_000}}), "utf-8")
    st = _state(tmp_path, t, CLAUDE_CODE_MAX_CONTEXT_TOKENS="250000")
    assert st.window == 1_000_000 and st.ctx_pct == pytest.approx(0.1)


def test_window_bad_values_fall_back(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "windows.json").write_text(json.dumps({"s-1": {"window": "big"}}), "utf-8")
    assert _state(tmp_path, t, CLAUDE_CODE_MAX_CONTEXT_TOKENS="lots").window == 200_000
    (home / "windows.json").write_text(json.dumps({"s-1": 300_000}), "utf-8")
    assert _state(tmp_path, t).window == 300_000


# --------------------------------------------------------------------------- advisories


def test_advise_once_per_level(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    t = _transcript(tmp_path, [_user(), _assistant("m1", 50_000)])
    assert _prompt(tmp_path, t, cfg) is None
    _append(t, [_assistant("m2", 130_000)])
    msg = _prompt(tmp_path, t, cfg)
    assert msg is not None and msg.startswith("token-sparen:")
    assert "65 %" in msg and "/compact" in msg and "\n" not in msg
    assert _prompt(tmp_path, t, cfg) is None  # same level: once per session
    _append(t, [_assistant("m3", 170_000)])
    msg = _prompt(tmp_path, t, cfg)
    assert msg is not None and "85 %" in msg and "/compact" in msg
    assert _prompt(tmp_path, t, cfg) is None
    # a later drop and re-rise does not repeat a level in the same session
    _append(t, [_assistant("m4", 20_000), _assistant("m5", 175_000)])
    assert _prompt(tmp_path, t, cfg) is None


def test_advise_skips_notice_when_jumping_straight_to_advise(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 190_000)])
    msg = _prompt(tmp_path, t, cfg)
    assert msg is not None and "95 %" in msg
    _append(t, [_assistant("m2", 10_000), _assistant("m3", 125_000)])
    assert _prompt(tmp_path, t, cfg) is None  # notice was implied by advise


def test_economy_advisories(tmp_path: Path) -> None:
    over = {"TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "10000"}
    cfg = _cfg(tmp_path, **over)
    t = _transcript(tmp_path, [_assistant("m1", 1000)])
    assert _prompt(tmp_path, t, cfg, **over) is None
    _append(t, [_assistant("m2", 4500)])
    msg = _prompt(tmp_path, t, cfg, **over)
    assert msg is not None and msg.startswith("token-sparen:") and "55 %" in msg
    _append(t, [_assistant("m3", 3000)])
    msg = _prompt(tmp_path, t, cfg, **over)
    assert msg is not None and "85 %" in msg and "left at the current pace" in msg
    assert _prompt(tmp_path, t, cfg, **over) is None


def test_post_tool_advisory(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    t = _transcript(tmp_path, [_assistant("m1", 170_000)])
    ev = _event(tmp_path, "post_tool", t, tool="Bash", tool_input={"command": "ls"})
    msg = governor.on_post_tool(ev, cfg, env=_env(tmp_path)).user_message
    assert msg is not None and "85 %" in msg
    assert governor.on_post_tool(ev, cfg, env=_env(tmp_path)).user_message is None


def test_advisory_fails_open_on_busy_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from token_sparen.store import jsonstore

    def busy(*a: Any, **k: Any) -> Any:
        raise jsonstore.StoreBusy("x")

    monkeypatch.setattr(session, "update_session", busy)
    t = _transcript(tmp_path, [_assistant("m1", 170_000)])
    assert _prompt(tmp_path, t, _cfg(tmp_path)) is None
    logged = (tmp_path / "home" / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "governor" in logged


def test_disabled_governor(tmp_path: Path) -> None:
    over = {"TOKEN_SPAREN_GOVERNOR__ENABLED": "false"}
    t = _transcript(tmp_path, [_assistant("m1", 190_000)])
    assert _prompt(tmp_path, t, _cfg(tmp_path, **over), **over) is None


# --------------------------------------------------------------------------- hard stop


def _over_budget(tmp_path: Path, **extra: str) -> tuple[Path, Config, dict[str, str]]:
    over = {"TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "1000", **extra}
    t = _transcript(tmp_path, [_assistant("m1", 1500)])
    return t, _cfg(tmp_path, **over), _env(tmp_path, **over)


def test_hard_stop_off_by_default(tmp_path: Path) -> None:
    t, cfg, env = _over_budget(tmp_path)
    for tool in ("Read", "Bash", "WebFetch", "WebSearch", "Task", "Agent"):
        ev = _event(tmp_path, "pre_tool", t, tool=tool)
        assert governor.check_pre_tool(ev, cfg, env=env).is_noop()


def test_hard_stop_blocks_context_adding_tools(tmp_path: Path) -> None:
    t, cfg, env = _over_budget(tmp_path, TOKEN_SPAREN_GOVERNOR__HARD_STOP="true")
    stop = ("Read", "Bash", "WebFetch", "WebSearch", "Task", "Agent", "Grep", "Glob", "LS")
    for tool in (*stop, "mcp__srv__fetch_docs"):
        ev = _event(tmp_path, "pre_tool", t, tool=tool)
        reason = governor.check_pre_tool(ev, cfg, env=env).block_reason
        assert reason is not None and reason.startswith("token-sparen:")
        assert "hard_stop = false" in reason and "session_budget_tokens" in reason
        assert "~/.token-sparen/config.toml" in reason and "token-sparen.toml" in reason
        assert "run /token-sparen" not in reason


def test_hard_stop_never_blocks_edits(tmp_path: Path) -> None:
    t, cfg, env = _over_budget(tmp_path, TOKEN_SPAREN_GOVERNOR__HARD_STOP="true")
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite", "AskUserQuestion"):
        ev = _event(tmp_path, "pre_tool", t, tool=tool, tool_input={"file_path": "x"})
        assert governor.check_pre_tool(ev, cfg, env=env).is_noop()


def test_hard_stop_below_budget_allows(tmp_path: Path) -> None:
    over = {
        "TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "100000",
        "TOKEN_SPAREN_GOVERNOR__HARD_STOP": "true",
    }
    t = _transcript(tmp_path, [_assistant("m1", 90_000)])
    ev = _event(tmp_path, "pre_tool", t, tool="Read")
    act = governor.check_pre_tool(ev, _cfg(tmp_path, **over), env=_env(tmp_path, **over))
    assert act.is_noop()


def test_hard_stop_via_entry(tmp_path: Path) -> None:
    import io

    from token_sparen.adapters.claude import entry

    t = _transcript(tmp_path, [_assistant("m1", 1500)])
    env = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
        "TOKEN_SPAREN_GOVERNOR__SESSION_BUDGET_TOKENS": "1000",
        "TOKEN_SPAREN_GOVERNOR__HARD_STOP": "true",
        "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000",
    }
    (tmp_path / "proj").mkdir(exist_ok=True)
    payload = {
        "session_id": "s-h",
        "transcript_path": str(t),
        "tool_name": "Read",
        "tool_input": {"file_path": str(tmp_path / "x.py")},
    }
    out = io.StringIO()
    entry.main("PreToolUse", io.StringIO(json.dumps(payload)), out, env)
    spec = json.loads(out.getvalue())["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    payload = {**payload, "tool_name": "Edit"}
    out = io.StringIO()
    entry.main("PreToolUse", io.StringIO(json.dumps(payload)), out, env)
    assert json.loads(out.getvalue()) == {}


# --------------------------------------------------------------------------- perf


@pytest.mark.perf
def test_governor_warm_update_on_100mb_transcript(tmp_path: Path) -> None:
    from perfutil import best_of, perf_factor

    filler = json.dumps({"type": "user", "message": {"content": "x" * 4000}})
    block = "".join(f"{filler}\n{_assistant(f'w{i}', 1000)}\n" for i in range(200))
    t = tmp_path / "big.jsonl"
    with t.open("w", encoding="utf-8") as fh:
        while fh.tell() < 100_000_000:
            fh.write(block)
    cfg = _cfg(tmp_path)
    env = _env(tmp_path)
    ctx = _ctx(tmp_path)
    governor.state(ctx, t, cfg, env)  # first contact: bounded (burn_unknown)
    n = [0]

    def warm() -> Any:
        n[0] += 1
        _append(t, [_user("next"), _assistant(f"new{n[0]}", 5000)])
        return governor.state(ctx, t, cfg, env)

    best, st = best_of(warm, 5)
    assert st.ctx_pct == pytest.approx(5000 / 200_000)
    assert best < 0.050 * perf_factor()


@pytest.mark.perf
def test_governor_catch_up_parse_is_bounded(tmp_path: Path) -> None:
    import time

    filler = json.dumps({"type": "user", "message": {"content": "x" * 4000}})
    block = "".join(f"{filler}\n{_assistant(f'w{i}', 1000)}\n" for i in range(200))
    t = tmp_path / "mid.jsonl"
    with t.open("w", encoding="utf-8") as fh:
        while fh.tell() < 40_000_000:
            fh.write(block)
    from perfutil import perf_factor

    cfg = _cfg(tmp_path)
    env = _env(tmp_path)
    start = time.perf_counter()
    governor.state(_ctx(tmp_path), t, cfg, env)
    # parse budget (150 ms) plus overlay I/O; never near the 400 ms hook cap
    assert time.perf_counter() - start < 0.3 * perf_factor()
