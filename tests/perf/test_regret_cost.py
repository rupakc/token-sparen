"""Regret bookkeeping per hook stays under 3 ms with a full (200-decision) overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from perfutil import best_of, perf_factor

from token_sparen.config import load_config
from token_sparen.events import Event, SessionCtx, ToolCall
from token_sparen.learn import regret
from token_sparen.learn.regret import RegretDetector
from token_sparen.store import session

pytestmark = pytest.mark.perf
KEY = "0123456789abcdef"
SID = "s-1"


class _Rec:
    def reward(self, decision_id: str, savings: Any, regret: Any) -> None:
        pass


def test_regret_hook_under_three_ms(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"TOKEN_SPAREN_HOME": str(home)}
    cfg = load_config(tmp_path / "p", env)
    ov = session.session_path(home, KEY, SID)
    det = RegretDetector(cfg, env)
    ctx = SessionCtx(SID, KEY, 1, None, None, ov)
    full = "\n".join(
        f"src/pkg/module_{i}.py:{i} identifier_{i:05d}_abcdef value_{i:05d}_ghijkl"
        for i in range(3000)
    )
    for i in range(200):
        det.track(
            ctx,
            f"{i:032x}",
            point="compression",
            bucket="band=lt50|cat=shell|size=s|ctype=log|phase=build",
            arm="standard",
            recovery_id=f"{i:012x}",
            command_sig=f"pytest -q tests/test_{i}.py",
            full=full,
            kept="kept",
            ctype="log",
        )
    assert len(session.read_session(regret.table_path(ov))["regret"]) == 200
    proj = tmp_path / "proj"
    proj.mkdir()
    ev = Event(
        "claude",
        "post_tool",
        SID,
        proj,
        KEY,
        None,
        tool=ToolCall("Grep", "search", {"pattern": "needle", "path": "src"}, "t"),
        output_text="x" * 1000,
    )
    best, _ = best_of(lambda: regret.handle(ev, cfg, _Rec(), env=env), 15)
    assert best < 0.003 * perf_factor()
    edit = Event(
        "claude",
        "post_tool",
        SID,
        proj,
        KEY,
        None,
        tool=ToolCall("Edit", "edit", {"file_path": str(proj / "docs" / "x.md")}, "t"),
        output_text="ok",
    )
    best, _ = best_of(lambda: regret.handle(edit, cfg, _Rec(), env=env), 15)
    assert best < 0.003 * perf_factor()
    prompt = Event("claude", "prompt", SID, proj, KEY, None, prompt="please continue " * 50)
    best, _ = best_of(lambda: regret.handle(prompt, cfg, _Rec(), env=env), 15)
    assert best < 0.003 * perf_factor()
