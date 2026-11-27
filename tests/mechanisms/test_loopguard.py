"""Failure-loop guard (spec 6.11; decision point ``loopguard``). Synthetic events only."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Action, Event, SessionCtx, ToolCall
from token_sparen.learn import regret
from token_sparen.learn.policy import Decision, DecisionPoint, StaticPolicy, get_point
from token_sparen.mechanisms import loopguard, tracking
from token_sparen.store import session

KEY = "0123456789abcdef"
SID = "s-1"
TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/work/{d}/pkg/mod.py", line {n}, in run\n'
    "    value = parse(data)\n"
    "ValueError: invalid literal for int() with base 10: '{q}'\n"
)


class Rec:
    """A learning policy double: fixed arm, records decisions and rewards."""

    def __init__(self, arm: str) -> None:
        self.arm = arm
        self.decisions: list[Decision] = []
        self.rewards: list[tuple[str, float | None, float | None]] = []

    def decide(
        self, point: DecisionPoint, features: Mapping[str, str], session: SessionCtx
    ) -> Decision:
        d = Decision(uuid.uuid4().hex, point.name, self.arm, 0.5, "cat=shell")
        self.decisions.append(d)
        return d

    def reward(self, decision_id: str, savings: float | None, regret: float | None) -> None:
        self.rewards.append((decision_id, savings, regret))


def _env(tmp_path: Path) -> dict[str, str]:
    return {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "proj").mkdir(exist_ok=True)
    return load_config(tmp_path / "proj", _env(tmp_path))


def _event(
    tmp_path: Path,
    kind: str,
    *,
    tool: str = "Bash",
    tool_input: Mapping[str, Any] | None = None,
    text: str | None = None,
    prompt: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    call = None
    if kind in ("post_tool", "post_tool_failure"):
        call = ToolCall(tool, tool_category(tool), dict(tool_input or {}), uuid.uuid4().hex)
    return Event(
        host="claude",
        kind=kind,  # type: ignore[arg-type]
        session_id=SID,
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=call,
        output_text=text,
        prompt=prompt,
        extra=MappingProxyType(dict(extra or {})),
    )


def _fail(tmp_path: Path, n: int = 12, d: str = "a", q: str = "x1") -> Event:
    return _event(tmp_path, "post_tool_failure", text=TRACE.format(d=d, n=n, q=q))


def _other_fail(tmp_path: Path, i: int) -> Event:
    return _event(tmp_path, "post_tool_failure", text=f"KeyError{'Z' * i}: missing thing {i}\n")


def _ok(tmp_path: Path) -> Event:
    return _event(tmp_path, "post_tool", tool="Grep", tool_input={"pattern": "x"}, text="hit")


def _prompt(tmp_path: Path, policy: Any, text: str = "next task") -> Action:
    ev = _event(tmp_path, "prompt", prompt=text)
    cfg = _cfg(tmp_path)
    tracking.handle_prompt(ev, cfg, env=_env(tmp_path))
    return loopguard.on_prompt(ev, policy, cfg, env=_env(tmp_path))


def _tool(tmp_path: Path, policy: Any, ev: Event) -> Action:
    return loopguard.on_tool(ev, policy, _cfg(tmp_path), env=_env(tmp_path))


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _overlay(tmp_path: Path) -> dict[str, Any]:
    return session.read_session(session.session_path(tmp_path / "home", KEY, SID))


# --- fingerprint ----------------------------------------------------------------------------


def test_fingerprint_normalises_numbers_and_paths() -> None:
    a = loopguard.fingerprint(TRACE.format(d="alpha", n=12, q="x1"))
    b = loopguard.fingerprint(TRACE.format(d="beta/gamma", n=987, q="other value"))
    assert a == b and len(a) == 10 and all(c in "0123456789abcdef" for c in a)
    hexes = "error at 0xdeadbeef in build 3f9a2c1d7e: see src/a.c:12:7\n"
    assert loopguard.fingerprint(hexes) == loopguard.fingerprint(
        "Error at 0x1234 in build 0a0b0c0d0e: see lib/zz/b.c:99:3\n"
    )


def test_fingerprint_separates_exception_types_and_error_lines() -> None:
    base = loopguard.fingerprint(TRACE.format(d="a", n=1, q="x"))
    other = TRACE.format(d="a", n=1, q="x").replace("ValueError", "TypeError")
    assert loopguard.fingerprint(other) != base
    assert loopguard.fingerprint("error: cannot find module foo\n") != loopguard.fingerprint(
        "error: permission denied\n"
    )
    assert loopguard.fingerprint("") == loopguard.fingerprint("   \n")  # never raises
    assert len(loopguard.fingerprint("x" * 2_000_000)) == 10


def test_point_contract() -> None:
    p = loopguard.LOOPGUARD
    assert (p.name, p.arms, p.safe_arm, p.tier) == (
        "loopguard",
        ("patient", "standard", "eager"),
        "patient",
        "medium",
    )
    assert get_point("loopguard") == p
    assert loopguard.ARM_K == {"patient": 5, "standard": 3, "eager": 2}


# --- triggering -----------------------------------------------------------------------------


@pytest.mark.parametrize(("arm", "k"), [("eager", 2), ("standard", 3), ("patient", 5)])
def test_triggers_at_k_for_arm(tmp_path: Path, arm: str, k: int) -> None:
    pol = Rec(arm)
    _prompt(tmp_path, pol)
    for i in range(1, k):
        assert _tool(tmp_path, pol, _fail(tmp_path, n=i)).inject_context is None
    act = _tool(tmp_path, pol, _fail(tmp_path, n=k))
    fp = loopguard.fingerprint(TRACE.format(d="a", n=1, q="x1"))
    assert act.inject_context == (
        f"[token-sparen: this error has repeated {k}× this turn ({fp}). Consider stepping "
        "back: re-read the error, check assumptions, or try a different approach.]"
    )
    assert act.replace_output is None and act.user_message is None
    assert len(pol.decisions) == 1  # decided once, at the first repeat
    recs = [r for r in _decisions(tmp_path) if r.get("point") == "loopguard"]
    assert len(recs) == 1 and recs[0]["arm"] == arm and recs[0]["propensity"] == 0.5


def test_static_policy_is_patient(tmp_path: Path) -> None:
    pol = StaticPolicy({})
    _prompt(tmp_path, pol)
    acts = [_tool(tmp_path, pol, _fail(tmp_path, n=i)) for i in range(5)]
    assert [a.inject_context is not None for a in acts] == [False] * 4 + [True]


def test_one_redirect_per_turn(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    assert _tool(tmp_path, pol, _fail(tmp_path)).inject_context is not None
    # a different error looping afterwards gets no second model-facing redirect
    later = [_tool(tmp_path, pol, _other_fail(tmp_path, 1)) for _ in range(4)]
    assert all(a.inject_context is None for a in later)
    assert len(pol.decisions) == 1


def test_second_trigger_user_only(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    third = _tool(tmp_path, pol, _fail(tmp_path))
    assert third.inject_context is None
    assert third.user_message == (
        "token-sparen: the agent is repeating the same error (3×); you may want to intervene."
    )
    fourth = _tool(tmp_path, pol, _fail(tmp_path))
    assert fourth.is_noop()  # the user is told once per turn


def test_resets_on_new_prompt(tmp_path: Path) -> None:
    pol = Rec("standard")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _fail(tmp_path)).is_noop()  # count restarted at 1
    assert _tool(tmp_path, pol, _fail(tmp_path)).is_noop()
    assert len(pol.decisions) == 2  # a fresh decision for the new turn
    assert _tool(tmp_path, pol, _fail(tmp_path)).inject_context is not None
    # and a turn that never triggered logged no decision
    assert len([r for r in _decisions(tmp_path) if r.get("point") == "loopguard"]) == 1


def test_burst_of_failures_triggers(tmp_path: Path) -> None:
    pol = Rec("patient")
    _prompt(tmp_path, pol)
    acts = [_tool(tmp_path, pol, _other_fail(tmp_path, i)) for i in range(8)]
    assert [a.inject_context is not None for a in acts] == [False] * 7 + [True]
    assert "repeated 8×" in str(acts[-1].inject_context)


def test_successful_output_is_never_fingerprinted(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    ev = _event(tmp_path, "post_tool", text=TRACE.format(d="a", n=1, q="x"))
    assert all(_tool(tmp_path, pol, ev).is_noop() for _ in range(4))
    assert pol.decisions == []


def test_disabled(tmp_path: Path) -> None:
    pol = Rec("eager")
    cfg = Config(data={"loopguard": {"enabled": False}})
    for _ in range(3):
        assert loopguard.on_tool(_fail(tmp_path), pol, cfg, env=_env(tmp_path)).is_noop()
    assert pol.decisions == []


def test_min_k_raises_the_arm(tmp_path: Path) -> None:
    pol = Rec("eager")
    cfg = Config(data={"loopguard": {"min_k": 4}})
    env = _env(tmp_path)
    acts = [loopguard.on_tool(_fail(tmp_path), pol, cfg, env=env) for _ in range(4)]
    assert [a.inject_context is not None for a in acts] == [False] * 3 + [True]


# --- edit-revert oscillation ----------------------------------------------------------------


def _edit(tmp_path: Path, old: str, new: str, path: str = "src/a.py") -> Event:
    inp = {"file_path": path, "old_string": old, "new_string": new}
    return _event(tmp_path, "post_tool", tool="Edit", tool_input=inp, text="ok")


def _write(tmp_path: Path, content: str, path: str = "src/b.py") -> Event:
    inp = {"file_path": path, "content": content}
    return _event(tmp_path, "post_tool", tool="Write", tool_input=inp, text="ok")


def test_edit_revert_oscillation(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2")).is_noop()
    assert _tool(tmp_path, pol, _edit(tmp_path, "y = 1", "y = 2")).is_noop()
    act = _tool(tmp_path, pol, _edit(tmp_path, "x = 2", "x = 1"))  # reverted
    assert act.inject_context is not None and "repeated 2×" in act.inject_context
    assert act.replace_output is None


def test_edit_of_another_file_is_not_oscillation(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2", "src/a.py"))
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = 2", "x = 1", "src/c.py")).is_noop()


def test_write_returning_to_an_earlier_hash(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).is_noop()
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).is_noop()  # same content again: no-op
    assert _tool(tmp_path, pol, _write(tmp_path, "v2")).is_noop()
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).inject_context is not None


# --- reward ---------------------------------------------------------------------------------


def _nudged(tmp_path: Path, pol: Rec) -> str:
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    assert _tool(tmp_path, pol, _fail(tmp_path)).inject_context is not None
    return pol.decisions[0].decision_id


def test_reward_loop_broken(tmp_path: Path) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    for _ in range(5):
        _tool(tmp_path, pol, _ok(tmp_path))
    assert pol.rewards == [(did, 1.0, None)]
    _prompt(tmp_path, pol, "thanks, continue")
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


def test_reward_loop_not_broken(tmp_path: Path) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    _tool(tmp_path, pol, _ok(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    assert pol.rewards == [(did, 0.0, None)]


def test_turn_end_settles_open_window_as_broken(tmp_path: Path) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    _tool(tmp_path, pol, _ok(tmp_path))
    _prompt(tmp_path, pol, "ok")
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


@pytest.mark.parametrize(
    "text",
    ["STOP please", "undo that", "Revert it", "that's wrong", "No, don't do that", "that’s wrong"],
)
def test_regret_from_user_stop_words(tmp_path: Path, text: str) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    _prompt(tmp_path, pol, text)
    assert (did, None, 1.0) in pol.rewards
    outs = [r for r in _decisions(tmp_path) if r.get("kind") == "outcome"]
    assert outs and outs[-1]["decision_id"] == did and outs[-1]["regret"] == 1.0


@pytest.mark.parametrize("text", ["stopwatch helper next", "nothing", "no problem, do it"])
def test_no_regret_without_stop_words(tmp_path: Path, text: str) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    _prompt(tmp_path, pol, text)
    assert (did, None, 0.0) in pol.rewards


def test_interrupt_is_regret(tmp_path: Path) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    ev = _event(tmp_path, "post_tool_failure", text="interrupted", extra={"is_interrupt": True})
    _tool(tmp_path, pol, ev)
    assert (did, None, 1.0) in pol.rewards
    _prompt(tmp_path, pol, "go on")
    assert [r for r in pol.rewards if r[2] is not None] == [(did, None, 1.0)]  # paid once


def test_session_end_settles(tmp_path: Path) -> None:
    pol = Rec("eager")
    did = _nudged(tmp_path, pol)
    ev = _event(tmp_path, "session_end")
    loopguard.on_session_end(ev, pol, _cfg(tmp_path), env=_env(tmp_path))
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


def test_nudge_counts_as_injected_tokens(tmp_path: Path) -> None:
    pol = Rec("eager")
    _nudged(tmp_path, pol)
    assert _overlay(tmp_path).get("injected_tokens", 0) > 0


# --- regret detector wiring -----------------------------------------------------------------


def test_loop_calls_regret_on_loop(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    ov = session.session_path(tmp_path / "home", KEY, SID)
    ctx = SessionCtx(SID, KEY, 1, None, None, ov)
    det = regret.RegretDetector(_cfg(tmp_path), _env(tmp_path))
    for did, ctype in (("d-trace", "stacktrace"), ("d-json", "json")):
        det.track(
            ctx,
            did,
            point="compression",
            bucket="*",
            arm="standard",
            recovery_id=None,
            command_sig="pytest -q",
            full="",
            kept="",
            ctype=ctype,
        )
    _tool(tmp_path, pol, _fail(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    table = regret.read_table(ov)
    assert table["d-trace"]["best"] == 0.5 and table["d-json"]["best"] == 0.0


# --- bounded state --------------------------------------------------------------------------


def test_state_is_bounded(tmp_path: Path) -> None:
    pol = Rec("patient")
    cfg = Config(data={"loopguard": {"burst": 10_000}})
    _prompt(tmp_path, pol)
    for i in range(80):
        loopguard.on_tool(_other_fail(tmp_path, i), pol, cfg, env=_env(tmp_path))
        loopguard.on_tool(
            _edit(tmp_path, f"a{i}", f"b{i}", f"f{i}.py"), pol, cfg, env=_env(tmp_path)
        )
    state = _overlay(tmp_path)[loopguard.STATE_KEY]
    assert len(state["fps"]) <= loopguard.MAX_FINGERPRINTS == 50
    assert len(state["files"]) <= loopguard.MAX_FINGERPRINTS


# --- hook delivery (Claude Code adapter) ----------------------------------------------------


def _hook(
    tmp_path: Path, name: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    from token_sparen.adapters.claude import entry

    monkeypatch.setattr(entry, "POLICY", Rec("eager"))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    env = {**_env(tmp_path), "CLAUDE_PROJECT_DIR": str(proj)}
    base = {"session_id": SID, "cwd": str(proj), "hook_event_name": name}
    out = io.StringIO()
    entry.main(name, io.StringIO(json.dumps({**base, **payload})), out, env, exit_fn=lambda _: 0)
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


def test_failure_hook_delivers_additional_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook(tmp_path, "UserPromptSubmit", {"prompt": "fix it"}, monkeypatch)
    fail = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_use_id": "tu-1",
        "error": TRACE.format(d="a", n=3, q="z"),
    }
    assert _hook(tmp_path, "PostToolUseFailure", fail, monkeypatch) == {}
    out = _hook(tmp_path, "PostToolUseFailure", {**fail, "tool_use_id": "tu-2"}, monkeypatch)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUseFailure"
    assert spec["additionalContext"].startswith("[token-sparen: this error has repeated 2×")
    assert "updatedToolOutput" not in spec


def test_post_tool_output_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hook(tmp_path, "UserPromptSubmit", {"prompt": "fix it"}, monkeypatch)

    def edit(old: str, new: str, tid: str) -> dict[str, Any]:
        return {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/a.py", "old_string": old, "new_string": new},
            "tool_use_id": tid,
            "tool_response": {"filePath": "src/a.py", "oldString": old, "newString": new},
        }

    _hook(tmp_path, "PostToolUse", edit("a", "b", "t1"), monkeypatch)
    out = _hook(tmp_path, "PostToolUse", edit("b", "a", "t2"), monkeypatch)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse" and "additionalContext" in spec
    assert "updatedToolOutput" not in spec
