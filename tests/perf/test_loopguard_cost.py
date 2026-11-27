"""Loop-guard bookkeeping per hook stays O(1): under 3 ms with a full (50-fingerprint) turn."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from perfutil import best_of, perf_factor

from token_sparen.config import load_config
from token_sparen.events import Event, ToolCall
from token_sparen.learn.policy import Decision
from token_sparen.mechanisms import loopguard, tracking

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
SID = "s-1"


class _Rec:
    def decide(self, point: Any, features: Any, session: Any) -> Decision:
        return Decision("d" * 32, point.name, "patient", 1.0, "*")

    def reward(self, decision_id: str, savings: Any, regret: Any) -> None:
        pass


def test_loopguard_hook_under_three_ms(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    proj.mkdir()
    env = {"TOKEN_SPAREN_HOME": str(home)}
    cfg = load_config(proj, env)
    pol = _Rec()

    def ev(kind: str, tool: str, inp: dict[str, Any], text: str) -> Event:
        call = ToolCall(tool, "shell" if tool == "Bash" else "edit", inp, "t")
        return Event("claude", kind, SID, proj, KEY, None, tool=call, output_text=text)  # type: ignore[arg-type]

    prompt = Event("claude", "prompt", SID, proj, KEY, None, prompt="go")
    tracking.handle_prompt(prompt, cfg, env=env)
    loopguard.on_prompt(prompt, pol, cfg, env=env)
    trace = "Traceback (most recent call last):\n" + "  frame line\n" * 5000
    for i in range(60):
        fail = ev("post_tool_failure", "Bash", {"command": "x"}, f"E{'Q' * i}Error: boom {i}\n")
        loopguard.on_tool(fail, pol, cfg, env=env)
        edit = {"file_path": f"f{i}.py", "old_string": "a", "new_string": "b"}
        loopguard.on_tool(ev("post_tool", "Edit", edit, "ok"), pol, cfg, env=env)
    big = ev("post_tool_failure", "Bash", {"command": "x"}, trace + "ValueError: bad 1\n" * 20000)
    best, _ = best_of(lambda: loopguard.on_tool(big, pol, cfg, env=env), 15)
    assert best < 0.003 * perf_factor()
    # worst case: 16 agents filling the turn-wide caps (fingerprints, files x edit pairs)
    for agent in range(loopguard.MAX_AGENTS):
        who = {"agent_id": f"a{agent}"}
        for i in range(4):
            text = f"E{'Q' * i}Error: b {agent}\n"
            fail = ev("post_tool_failure", "Bash", {"command": "x"}, text)
            loopguard.on_tool(dataclasses.replace(fail, extra=who), pol, cfg, env=env)
            for j in range(loopguard.MAX_PAIRS):
                edit = {"file_path": f"f{agent}-{i}.py", "old_string": f"a{j}", "new_string": "b"}
                e = dataclasses.replace(ev("post_tool", "Edit", edit, "ok"), extra=who)
                loopguard.on_tool(e, pol, cfg, env=env)
    full = dataclasses.replace(big, extra={"agent_id": "a3"})
    best, _ = best_of(lambda: loopguard.on_tool(full, pol, cfg, env=env), 15)
    assert best < 0.003 * perf_factor()
    ok = ev("post_tool", "Grep", {"pattern": "x"}, "hit")
    best, _ = best_of(lambda: loopguard.on_tool(ok, pol, cfg, env=env), 15)
    assert best < 0.003 * perf_factor()
