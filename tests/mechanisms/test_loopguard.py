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
NUDGE = (
    "[token-sparen: this error has repeated {n}× this turn ({fp}). Consider stepping back: "
    "re-read the error, check assumptions, or try a different approach.]"
)
OSC = (
    "[token-sparen: the same edit has been made and undone {n}× this turn ({fp}). Consider "
    "stepping back: re-read the goal, check assumptions, or try a different approach.]"
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
        inp = dict(tool_input or ({"command": "pytest -q"} if tool == "Bash" else {}))
        call = ToolCall(tool, tool_category(tool), inp, uuid.uuid4().hex)
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


def _fail(
    tmp_path: Path, n: int = 12, d: str = "a", q: str = "x1", agent: str | None = None
) -> Event:
    extra = {} if agent is None else {"agent_id": agent}
    return _event(tmp_path, "post_tool_failure", text=TRACE.format(d=d, n=n, q=q), extra=extra)


def _other_fail(tmp_path: Path, i: int) -> Event:
    return _event(tmp_path, "post_tool_failure", text=f"Exit code 1\nKeyError: 'missing_{i}'\n")


def _ok(tmp_path: Path, agent: str | None = None) -> Event:
    extra = {} if agent is None else {"agent_id": agent}
    inp = {"pattern": "x"}
    return _event(tmp_path, "post_tool", tool="Grep", tool_input=inp, text="hit", extra=extra)


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


def _lg_records(tmp_path: Path) -> list[dict[str, Any]]:
    return [r for r in _decisions(tmp_path) if r.get("point") == "loopguard"]


def _overlay(tmp_path: Path) -> dict[str, Any]:
    return session.read_session(session.session_path(tmp_path / "home", KEY, SID))


def _fp(text: str, tool: str = "Bash", target: str = "pytest") -> str | None:
    return loopguard.fingerprint(text, tool=tool, target=target)


# --- fingerprint ----------------------------------------------------------------------------


def test_fingerprint_normalises_numbers_and_paths() -> None:
    """Volatile parts (directories, line numbers, line:col, hex ids, durations) are ignored."""
    a = _fp(TRACE.format(d="alpha", n=12, q="x1"))
    b = _fp(TRACE.format(d="beta/gamma", n=987, q="x1"))
    assert a is not None and a == b and len(a) == 10
    assert all(c in "0123456789abcdef" for c in a)
    hexes = "error: bad object 0xdeadbeef in build 3f9a2c1d7e at /tmp/q/a.c:12:7 after 0.31s\n"
    assert _fp(hexes) == _fp(
        "Error: bad object 0x1234 in build 0a0b0c0d0e at /var/zz/a.c:99:3 after 12.5s\n"
    )


@pytest.mark.parametrize(
    ("one", "two"),
    [
        (TRACE.format(d="a", n=1, q="x"), TRACE.format(d="a", n=1, q="x").replace("Value", "Type")),
        (
            "ModuleNotFoundError: No module named 'foo'",
            "ModuleNotFoundError: No module named 'bar'",
        ),
        ("KeyError: 'user_id'", "KeyError: 'email'"),
        (
            "AttributeError: 'Foo' object has no attribute 'bar'",
            "AttributeError: 'Baz' object has no attribute 'qux'",
        ),
        (
            "FAIL src/a.test.ts\n  ● sums › adds\n    Expected: 5\n    Received: 3\n",
            "FAIL src/b.test.ts\n  ● sums › adds\n    Expected: 5\n    Received: 3\n",
        ),
        (
            "src/a.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'.",
            "src/b.ts(9,1): error TS2322: Type 'Foo' is not assignable to type 'Bar'.",
        ),
        (
            "Exit code 1\nE       assert 3 == 5\ntests/test_x.py:3: AssertionError\n"
            "1 failed in 0.03s",
            "Exit code 1\nE       assert 4 == 5\ntests/test_x.py:3: AssertionError\n"
            "1 failed in 0.04s",
        ),
        ("error: cannot find module foo", "error: permission denied"),
    ],
)
def test_fingerprint_keeps_distinct_failures_apart(one: str, two: str) -> None:
    a, b = _fp(one), _fp(two)
    assert a is not None and b is not None and a != b
    assert _fp(one) == a  # a true repeat still matches


def test_fingerprint_edit_failure_in_different_files() -> None:
    text = "<tool_use_error>String to replace not found in file.\nString: foo</tool_use_error>"
    a = _fp(text, tool="Edit", target="src/a.py")
    b = _fp(text, tool="Edit", target="src/b.py")
    assert a is not None and b is not None and a != b
    assert _fp(text, tool="Edit", target="src/a.py") == a


def test_fingerprint_mixes_tool_and_command_head() -> None:
    text = "error: something went wrong"
    assert _fp(text, target="grep") != _fp(text, target="pytest")
    assert _fp(text, tool="Bash", target="x") != _fp(text, tool="WebFetch", target="x")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "Exit code 1",
        "Exit code 1\n",
        "Exit code 2\nsrc/a.py\nsrc/b.py\n",
        "<tool_use_error>Exit code 1</tool_use_error>",
        "Traceback (most recent call last):\n",
    ],
)
def test_uninformative_failure_has_no_fingerprint(text: str) -> None:
    assert _fp(text) is None


def test_fingerprint_is_bounded() -> None:
    assert _fp("x" * 2_000_000) is None
    fp = _fp("x" * 2_000_000 + "\nValueError: boom\n")
    assert fp is not None and len(fp) == 10


@pytest.mark.parametrize(
    ("cmd", "head"),
    [
        ("pytest -q tests/a.py", "pytest"),
        ("cd sub && PYTHONPATH=. pytest tests/b.py -x", "pytest"),
        ("git diff --exit-code", "git diff"),
        ("npm test -- --watch=false", "npm test"),
        ("/usr/bin/grep -rn foo .", "grep foo"),
        ("python -m pytest", "python pytest"),
    ],
)
def test_command_head(cmd: str, head: str) -> None:
    assert loopguard.command_head(cmd) == head


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
    fp = _fp(TRACE.format(d="a", n=1, q="x1"))
    assert act.inject_context == NUDGE.format(n=k, fp=fp)
    assert act.replace_output is None and act.user_message is None
    assert len(pol.decisions) == 1  # decided once, at the first repeat
    recs = _lg_records(tmp_path)
    assert len(recs) == 1 and recs[0]["arm"] == arm and recs[0]["propensity"] == 0.5


def test_static_policy_is_patient(tmp_path: Path) -> None:
    pol = StaticPolicy({})
    _prompt(tmp_path, pol)
    acts = [_tool(tmp_path, pol, _fail(tmp_path, n=i)) for i in range(5)]
    assert [a.inject_context is not None for a in acts] == [False] * 4 + [True]


@pytest.mark.parametrize("arm", ["eager", "standard", "patient"])
def test_exit_only_failures_never_nudge(tmp_path: Path, arm: str) -> None:
    """Exploration with non-zero exits (grep misses, test -f, diff) is not a loop."""
    pol = Rec(arm)
    _prompt(tmp_path, pol)
    cmds = ["grep -rn foo .", "test -f a.txt", "diff a b", "git diff --exit-code", "grep bar x"]
    for cmd in cmds:
        ev = _event(tmp_path, "post_tool_failure", tool_input={"command": cmd}, text="Exit code 1")
        assert _tool(tmp_path, pol, ev).is_noop()
    for _ in range(5):
        ev = _event(tmp_path, "post_tool_failure", text="Exit code 1\n")
        assert _tool(tmp_path, pol, ev).is_noop()
    assert pol.decisions == [] and _lg_records(tmp_path) == []


def test_distinct_failures_never_nudge(tmp_path: Path) -> None:
    """Ten different failures in one turn: no repeat, no decision, no nudge (no burst)."""
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    assert all(_tool(tmp_path, pol, _other_fail(tmp_path, i)).is_noop() for i in range(10))
    assert pol.decisions == []


def test_red_phase_progress_never_nudges(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    for got in (3, 4, 6):
        text = f"Exit code 1\nE       assert {got} == 5\ntests/test_x.py:3: AssertionError\n"
        ev = _event(tmp_path, "post_tool_failure", text=text)
        assert _tool(tmp_path, pol, ev).is_noop()


def test_edit_failures_in_different_files_never_nudge(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    text = "<tool_use_error>String to replace not found in file.\nString: foo</tool_use_error>"
    for path in ("src/a.py", "src/b.py", "src/c.py"):
        inp = {"file_path": path, "old_string": "foo", "new_string": "bar"}
        ev = _event(tmp_path, "post_tool_failure", tool="Edit", tool_input=inp, text=text)
        assert _tool(tmp_path, pol, ev).is_noop()


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
    assert len(_lg_records(tmp_path)) == 2  # every turn with a repeat logs its decision


def test_turn_without_repeat_logs_no_decision(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    _prompt(tmp_path, pol)
    assert pol.decisions == [] and _lg_records(tmp_path) == [] and pol.rewards == []


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


# --- per agent ------------------------------------------------------------------------------


def test_agents_are_counted_separately(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    # two parallel subagents and the main agent each hit the same error once: no loop
    for agent in ("sub-1", "sub-2", None):
        assert _tool(tmp_path, pol, _fail(tmp_path, agent=agent)).is_noop()
    assert pol.decisions == []
    first = _tool(tmp_path, pol, _fail(tmp_path, agent="sub-1"))
    assert first.inject_context is not None and "repeated 2×" in first.inject_context
    # the other subagent gets its own (single) redirect; the main agent still has one occurrence
    second = _tool(tmp_path, pol, _fail(tmp_path, agent="sub-2"))
    assert second.inject_context is not None
    assert _tool(tmp_path, pol, _ok(tmp_path)).is_noop()
    assert len(pol.decisions) == 2
    state = _overlay(tmp_path)[loopguard.STATE_KEY]
    assert set(state["agents"]) == {"sub-1", "sub-2", ""}


def test_watch_counts_only_the_agents_own_calls(tmp_path: Path) -> None:
    pol = Rec("standard")
    _prompt(tmp_path, pol)
    for _ in range(3):
        _tool(tmp_path, pol, _fail(tmp_path, agent="sub-1"))
    did = pol.decisions[0].decision_id
    for _ in range(5):
        _tool(tmp_path, pol, _ok(tmp_path))  # the main agent's calls do not close the window
    assert pol.rewards == []
    for _ in range(5):
        _tool(tmp_path, pol, _ok(tmp_path, agent="sub-1"))
    assert pol.rewards == [(did, 1.0, None)]


# --- edit-revert oscillation ----------------------------------------------------------------


def _edit(tmp_path: Path, old: str, new: str, path: str = "src/a.py") -> Event:
    inp = {"file_path": path, "old_string": old, "new_string": new}
    return _event(tmp_path, "post_tool", tool="Edit", tool_input=inp, text="ok")


def _write(tmp_path: Path, content: str, path: str = "src/b.py") -> Event:
    inp = {"file_path": path, "content": content}
    return _event(tmp_path, "post_tool", tool="Write", tool_input=inp, text="ok")


@pytest.mark.parametrize("arm", ["eager", "standard", "patient"])
def test_debug_line_add_and_remove_is_not_oscillation(tmp_path: Path, arm: str) -> None:
    pol = Rec(arm)
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = f()", "x = f()\nprint(x)")).is_noop()
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = f()\nprint(x)", "x = f()")).is_noop()
    assert pol.decisions == []


@pytest.mark.parametrize("arm", ["eager", "standard"])
def test_edit_oscillation(tmp_path: Path, arm: str) -> None:
    pol = Rec(arm)
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2")).is_noop()
    assert _tool(tmp_path, pol, _edit(tmp_path, "y = 1", "y = 2")).is_noop()
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = 2", "x = 1")).is_noop()  # one reversal
    act = _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2"))  # A -> B -> A -> B
    assert act.inject_context is not None and act.replace_output is None
    fp = act.inject_context.split("(")[1].split(")")[0]
    assert act.inject_context == OSC.format(n=2, fp=fp)


def test_edit_oscillation_patient_waits(tmp_path: Path) -> None:
    pol = Rec("patient")
    _prompt(tmp_path, pol)
    pairs = [("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]
    assert all(_tool(tmp_path, pol, _edit(tmp_path, o, n)).is_noop() for o, n in pairs)
    act = _tool(tmp_path, pol, _edit(tmp_path, "a", "b"))  # 4 reversals
    assert act.inject_context == OSC.format(n=4, fp=act.inject_context.split("(")[1][:10])


def test_edit_of_another_file_is_not_oscillation(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2", "src/a.py"))
    _tool(tmp_path, pol, _edit(tmp_path, "x = 2", "x = 1", "src/c.py"))
    assert _tool(tmp_path, pol, _edit(tmp_path, "x = 1", "x = 2", "src/d.py")).is_noop()


def test_write_oscillation(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).is_noop()
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).is_noop()  # same content again
    assert _tool(tmp_path, pol, _write(tmp_path, "v2")).is_noop()
    assert _tool(tmp_path, pol, _write(tmp_path, "v1")).is_noop()  # back once: not yet
    assert _tool(tmp_path, pol, _write(tmp_path, "v2")).inject_context is not None


# --- reward ---------------------------------------------------------------------------------


def _nudged(tmp_path: Path, pol: Rec) -> str:
    """A standard-arm redirect at n = 3 (eligible for savings)."""
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    assert _tool(tmp_path, pol, _fail(tmp_path)).inject_context is not None
    return pol.decisions[0].decision_id


def test_reward_loop_broken_after_nudge_at_three(tmp_path: Path) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    for _ in range(5):
        _tool(tmp_path, pol, _ok(tmp_path))
    assert pol.rewards == [(did, 1.0, None)]
    _prompt(tmp_path, pol, "thanks, continue")
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


def test_eager_nudge_at_two_earns_no_savings(tmp_path: Path) -> None:
    pol = Rec("eager")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    assert _tool(tmp_path, pol, _fail(tmp_path)).inject_context is not None
    did = pol.decisions[0].decision_id
    for _ in range(5):
        _tool(tmp_path, pol, _ok(tmp_path))
    assert pol.rewards == [(did, 0.0, None)]


def test_decision_without_nudge_earns_no_savings(tmp_path: Path) -> None:
    pol = Rec("standard")
    _prompt(tmp_path, pol)
    _tool(tmp_path, pol, _fail(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))  # first repeat: decided (k = 3 never reached)
    did = pol.decisions[0].decision_id
    recs = _lg_records(tmp_path)
    assert len(recs) == 1 and recs[0]["decision_id"] == did and recs[0]["tokens_after"] == 0
    _prompt(tmp_path, pol, "stop")  # no redirect was shown: no regret either
    assert pol.rewards == [(did, 0.0, None), (did, None, 0.0)]


def test_reward_loop_not_broken(tmp_path: Path) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    _tool(tmp_path, pol, _ok(tmp_path))
    _tool(tmp_path, pol, _fail(tmp_path))
    assert pol.rewards == [(did, 0.0, None)]


def test_turn_end_settles_open_window_as_broken(tmp_path: Path) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    _tool(tmp_path, pol, _ok(tmp_path))
    _prompt(tmp_path, pol, "ok")
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


@pytest.mark.parametrize(
    "text",
    ["STOP please", "undo that", "Revert it", "that's wrong", "No, don't do that", "that’s wrong"],
)
def test_regret_from_user_stop_words(tmp_path: Path, text: str) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    _prompt(tmp_path, pol, text)
    assert (did, None, 1.0) in pol.rewards
    outs = [r for r in _decisions(tmp_path) if r.get("kind") == "outcome"]
    assert outs and outs[-1]["decision_id"] == did and outs[-1]["regret"] == 1.0


@pytest.mark.parametrize("text", ["stopwatch helper next", "nothing", "no problem, do it"])
def test_no_regret_without_stop_words(tmp_path: Path, text: str) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    _prompt(tmp_path, pol, text)
    assert (did, None, 0.0) in pol.rewards


def test_interrupt_is_regret(tmp_path: Path) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    ev = _event(tmp_path, "post_tool_failure", text="interrupted", extra={"is_interrupt": True})
    _tool(tmp_path, pol, ev)
    assert (did, None, 1.0) in pol.rewards
    _prompt(tmp_path, pol, "go on")
    assert [r for r in pol.rewards if r[2] is not None] == [(did, None, 1.0)]  # paid once


def test_session_end_settles(tmp_path: Path) -> None:
    pol = Rec("standard")
    did = _nudged(tmp_path, pol)
    ev = _event(tmp_path, "session_end")
    loopguard.on_session_end(ev, pol, _cfg(tmp_path), env=_env(tmp_path))
    assert pol.rewards == [(did, 1.0, None), (did, None, 0.0)]


def test_nudge_counts_as_injected_tokens(tmp_path: Path) -> None:
    pol = Rec("standard")
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
    cfg = _cfg(tmp_path)
    env = _env(tmp_path)
    _prompt(tmp_path, pol)
    for i in range(80):
        loopguard.on_tool(_other_fail(tmp_path, i), pol, cfg, env=env)
        loopguard.on_tool(_edit(tmp_path, f"a{i}", f"b{i}", f"f{i}.py"), pol, cfg, env=env)
    for i in range(30):
        loopguard.on_tool(_fail(tmp_path, agent=f"agent-{i}"), pol, cfg, env=env)
    state = _overlay(tmp_path)[loopguard.STATE_KEY]
    assert len(state["agents"]) <= loopguard.MAX_AGENTS
    main = state["agents"][""]
    assert len(main["fps"]) <= loopguard.MAX_FINGERPRINTS == 50
    assert len(main["files"]) <= loopguard.MAX_FINGERPRINTS


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


def test_failure_hook_counts_subagents_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook(tmp_path, "UserPromptSubmit", {"prompt": "investigate"}, monkeypatch)
    fail = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -c 'import x'"},
        "error": "ModuleNotFoundError: No module named 'x'",
    }
    for i, agent in enumerate(("sub-1", "sub-2")):
        payload = {**fail, "tool_use_id": f"t{i}", "agent_id": agent, "agent_type": "general"}
        assert _hook(tmp_path, "PostToolUseFailure", payload, monkeypatch) == {}


def test_post_tool_output_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hook(tmp_path, "UserPromptSubmit", {"prompt": "fix it"}, monkeypatch)

    def edit(old: str, new: str, tid: str) -> dict[str, Any]:
        return {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/a.py", "old_string": old, "new_string": new},
            "tool_use_id": tid,
            "tool_response": {"filePath": "src/a.py", "oldString": old, "newString": new},
        }

    assert _hook(tmp_path, "PostToolUse", edit("a", "b", "t1"), monkeypatch) == {}
    assert _hook(tmp_path, "PostToolUse", edit("b", "a", "t2"), monkeypatch) == {}
    out = _hook(tmp_path, "PostToolUse", edit("a", "b", "t3"), monkeypatch)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    assert spec["additionalContext"].startswith("[token-sparen: the same edit has been made")
    assert "updatedToolOutput" not in spec
