"""E1 loop-guard lane: error loops, their natural break rate, the exposure a perfect nudge could
save (upper bound) and triggers on loops that were already converging (M4 Task 4).

Synthetic transcripts only; nothing here reads the real home or real sessions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_replay import Session

from token_sparen.config import Config, packaged_defaults
from token_sparen.replay import loopguard_lane, runner
from token_sparen.replay.facts_lane import call_tokens
from token_sparen.transcript import ToolResultRecord

CUTOFF = date(2026, 11, 1)
LOOP = ("loopguard", {})
DEV_TS = "2026-10-10T09:00:00.000Z"
TEST_TS = "2026-11-20T09:00:00.000Z"
CFG = Config(data=packaged_defaults())
ERR = "Exit code 1\nValueError: invalid literal for int() with base 10: 'x1'\n"
CMD = "pytest -q tests/test_a.py"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    return home


class Log(Session):
    """A ``Session`` with one-line tool calls (optionally on the sidechain)."""

    def __init__(self, sid: str, ts: str, side: bool = False) -> None:
        super().__init__(sid, ts)
        self.k = 0
        self.side = side

    def tool(self, name: str, inp: dict[str, Any], out: str = "ok", err: bool = False) -> Log:
        self.k += 1
        tid = f"t{self.k}"
        self.call(f"m{self.k}", tool=(tid, name, inp), sidechain=self.side)
        self.result(tid, out, is_error=err, raw={"stdout": out}, sidechain=self.side)
        return self

    def fail(self, cmd: str = CMD, out: str = ERR) -> Log:
        return self.tool("Bash", {"command": cmd}, out, err=True)

    def ok(self, cmd: str = "ls src") -> Log:
        return self.tool("Bash", {"command": cmd}, "fine")

    def grep(self) -> Log:
        return self.tool("Grep", {"pattern": "x"}, "hit")


def run(root: Path, split: str = "all") -> runner.RunResult:
    return runner.run_parallel([root], [LOOP], jobs=1, cutoff=CUTOFF, split=split, cfg=CFG)


def cat(res: runner.RunResult, split: str, name: str = "shell") -> dict[str, Any]:
    row: dict[str, Any] = res.by_split[split]["loopguard"]["categories"][name]
    return row


def tokens(cmd: str = CMD, out: str = ERR) -> int:
    return call_tokens(ToolResultRecord("x", "Bash", {"command": cmd}, out, is_error=True))


def test_natural_break_rate(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    a = Log("a", DEV_TS).prompt().fail().fail().fail()  # at 3, then 5 calls without it: broken
    for _ in range(5):
        a.grep()
    a.write(src / "a.jsonl")
    b = Log("b", DEV_TS).prompt().fail().fail().fail().grep().fail()  # recurs: not broken
    b.write(src / "b.jsonl")
    c = Log("c", TEST_TS).prompt().fail().fail().fail().prompt("next").grep()  # turn ends: broken
    c.write(src / "c.jsonl")
    e = Log("e", DEV_TS).prompt()  # exit-only failures have no fingerprint, as in production
    for _ in range(5):
        e.fail(out="Exit code 1\n")
    e.write(src / "e.jsonl")
    res = run(tmp_path / "t")
    dev, test = cat(res, "dev"), cat(res, "test")
    assert (dev["natural_observed"], dev["natural_broken"]) == (2, 1)
    assert dev["natural_break_rate"] == pytest.approx(0.5)
    assert test["natural_break_rate"] == pytest.approx(1.0)
    assert cat(res, "all")["natural_break_rate"] == pytest.approx(2 / 3)  # merged, not summed
    assert cat(res, "dev", "*")["natural_break_rate"] == pytest.approx(0.5)
    assert dev["loops_reaching"] == {"2": 2, "3": 2, "5": 0}
    assert res.by_split["dev"]["loopguard"]["sessions"] == 3


def test_exposure_after_kth_repeat(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    s = Log("a", DEV_TS).prompt()
    for _ in range(5):
        s.fail()
    for _ in range(5):
        s.grep()
    s.fail()  # six calls later: a new loop, not this one's exposure
    s.write(src / "a.jsonl")
    res = run(tmp_path / "t")
    row = cat(res, "dev")
    t = tokens()
    assert row["exposure_tokens"] == {"2": 3 * t, "3": 2 * t, "5": 0}
    assert row["loops_reaching"] == {"2": 1, "3": 1, "5": 1}
    assert res.by_split["dev"]["loopguard"]["exposure_upper_bound"] is True


def test_exposure_counts_calls_between_repeats(tmp_path: Path) -> None:
    s = Log("a", DEV_TS).prompt().fail().fail().grep().fail()
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    grep = call_tokens(ToolResultRecord("x", "Grep", {"pattern": "x"}, "hit"))
    assert cat(run(tmp_path / "t"), "dev")["exposure_tokens"]["2"] == grep + tokens()


def test_converging_trigger_counted(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    Log("a", DEV_TS).prompt().fail().fail().fail().ok(CMD).write(src / "a.jsonl")
    Log("b", DEV_TS).prompt().fail().fail().ok("ls -la").write(src / "b.jsonl")  # other head
    late = Log("c", DEV_TS).prompt().fail().fail()
    for _ in range(5):
        late.grep()
    late.ok("pytest -x")  # the same head (pytest), but six calls after the trigger
    late.write(src / "c.jsonl")
    res = run(tmp_path / "t")
    row = cat(res, "dev")
    assert row["loops_reaching"] == {"2": 3, "3": 1, "5": 0}
    assert row["converging_triggers"] == {"2": 1, "3": 1, "5": 0}


def test_subagent_chains_separate(tmp_path: Path) -> None:
    proj = tmp_path / "t" / "proj"
    main = Log("sid1", DEV_TS).prompt().fail().fail()
    main.side = True
    main.fail()  # an inline sidechain record of the main file: not the main agent's chain
    main.side = False
    main.write(proj / "sid1.jsonl")
    sub = Log("agent-a", DEV_TS, side=True)  # its own ids, as a real subagent file
    sub.records.append(sub._base("user", message={"content": "brief"}, isSidechain=True))
    sub.fail().fail()
    sub.write(proj / "sid1" / "subagents" / "agent-a.jsonl")
    res = run(tmp_path / "t")
    row = cat(res, "dev")
    assert row["loops_reaching"] == {"2": 2, "3": 0, "5": 0}
    assert row["natural_observed"] == 0


def test_oscillation_counted_per_file(tmp_path: Path) -> None:
    s = Log("a", DEV_TS).prompt()
    for old, new in [("a", "b"), ("b", "a"), ("a", "b")]:
        s.tool("Edit", {"file_path": "src/x.py", "old_string": old, "new_string": new})
    s.tool("Edit", {"file_path": "src/y.py", "old_string": "b", "new_string": "a"})
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    row = cat(run(tmp_path / "t"), "dev", "edit")
    assert row["oscillations_reaching"] == {"2": 1, "3": 1, "5": 0}


def test_split_rows_independent(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    Log("a", DEV_TS).prompt().fail().fail().write(src / "a.jsonl")
    Log("b", TEST_TS).prompt().fail().write(src / "b.jsonl")  # same fingerprint, other split
    both, dev_only = run(tmp_path / "t"), run(tmp_path / "t", split="dev")
    assert both.by_split["dev"] == dev_only.by_split["dev"]
    assert cat(both, "test")["loops_reaching"]["2"] == 0


def test_parallel_equals_serial(tmp_path: Path) -> None:
    for g in range(4):
        src = tmp_path / "t" / f"p{g}"
        Log("a", DEV_TS).prompt().fail().fail().fail().grep().write(src / "a.jsonl")
        Log("b", TEST_TS).prompt().fail().fail().fail().fail().write(src / "b.jsonl")
    one = runner.run_parallel([tmp_path / "t"], [LOOP], jobs=1, cutoff=CUTOFF, cfg=CFG)
    two = runner.run_parallel([tmp_path / "t"], [LOOP], jobs=2, cutoff=CUTOFF, cfg=CFG)
    assert one.by_split == two.by_split
    assert cat(one, "all")["natural_break_rate"] == pytest.approx(0.5)


def test_bad_session_counted_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Log("a", DEV_TS).prompt().fail().fail().write(tmp_path / "t" / "p" / "a.jsonl")

    def boom(*args: Any, **kw: Any) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(loopguard_lane, "call_tokens", boom)
    lane = run(tmp_path / "t").by_split["dev"]["loopguard"]
    assert lane["sessions_failed"] == 1 and lane["categories"] == {}


def test_lane_registered() -> None:
    assert runner.lane_factory("loopguard") is runner.LANES["loopguard"]
    assert isinstance(runner.LANES["loopguard"](CFG, {}), loopguard_lane.LoopguardLane)


def test_interrupts_writes_and_multiedits(tmp_path: Path) -> None:
    s = Log("a", DEV_TS).prompt().fail().fail().fail()
    for _ in range(5):  # interrupts are not calls of the window (production skips them)
        s.tool("Bash", {"command": "sleep 9"}, "[Request interrupted by user for tool use]", True)
    s.fail()  # still the loop's fourth repeat within the window
    for content in ("v1", "v2", "v1", "v2"):
        s.tool("Write", {"file_path": "src/w.py", "content": content})
    pairs = [{"old_string": "a", "new_string": "b"}, {"old_string": 1, "new_string": "c"}]
    s.tool("MultiEdit", {"file_path": "src/m.py", "edits": pairs})
    s.tool("Edit", {"old_string": "a", "new_string": "b"})  # no path: not tracked
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    res = run(tmp_path / "t")
    assert cat(res, "dev")["natural_broken"] == 0
    assert cat(res, "dev", "edit")["oscillations_reaching"] == {"2": 1, "3": 1, "5": 0}


def test_merge_recomputes_rate() -> None:
    row = {"natural_observed": 1, "natural_broken": 1, "natural_break_rate": 1.0}
    other = {"natural_observed": 3, "natural_broken": 0, "natural_break_rate": 0.0}
    out = loopguard_lane.merge({"categories": {"*": row}}, {"categories": {"*": other}})
    assert out["categories"]["*"]["natural_break_rate"] == pytest.approx(0.25)
    assert row["natural_break_rate"] == 1.0  # inputs untouched
