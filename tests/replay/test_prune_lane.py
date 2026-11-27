"""E1 pruning lane: simulated opt-in MCP tool pruning over past transcripts, in memory only
(M4 Task 5).

Synthetic transcripts only; nothing here reads the real home or real sessions. The lane never
calls a settings writer, git or any file outside the transcripts.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_facts_lane import no_project_access
from test_replay import Session

from token_sparen.config import Config, packaged_defaults
from token_sparen.estimate import estimate_tokens
from token_sparen.mechanisms import prune
from token_sparen.replay import prune_lane, runner
from token_sparen.replay.replay import RunIds

CUTOFF = date(2026, 11, 1)
DEV_TS = "2026-10-10T09:00:00.000Z"
TEST_TS = "2026-11-20T09:00:00.000Z"
CFG = Config(data=packaged_defaults())
ALPHA = ["mcp__alpha__get_page", "mcp__alpha__put_page"]
BETA = ["mcp__beta__query"]
LISTED = ["Read", "Bash", "ToolSearch", *ALPHA, *BETA]
FAST = {
    "min_sessions": 1,
    "min_prompts": 1,
    "tau": 0.5,
    "ucb_q": 0.51,
    "lease_sessions": 2,
}


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    return home


class Log(Session):
    """A ``Session`` with unique record uuids, a deferred-tool listing and tool calls."""

    def __init__(self, sid: str, ts: str, side: bool = False) -> None:
        super().__init__(sid, ts)
        self.k = 0
        self.side = side

    def _base(self, kind: str, **extra: Any) -> dict[str, Any]:
        rec = super()._base(kind, **extra)
        rec["uuid"] = f"{self.sid}-{rec['uuid']}"
        return rec

    def listing(self, names: list[str] = LISTED) -> Log:
        att = {"type": "deferred_tools_delta", "addedNames": names, "removedNames": []}
        self.records.append(self._base("attachment", attachment=att))
        return self

    def prompts(self, n: int = 5, text: str = "next step please") -> Log:
        for _ in range(n):
            self.prompt(text)
        return self

    def tool(self, name: str, inp: dict[str, Any] | None = None, out: str = "ok") -> Log:
        self.k += 1
        tid = f"t{self.k}"
        self.call(f"m{self.k}", tool=(tid, name, inp or {}), sidechain=self.side)
        self.result(tid, out, sidechain=self.side)
        return self


def idle(sid: str, ts: str = DEV_TS) -> Log:
    """A session that lists alpha and beta and uses beta only."""
    return Log(sid, ts).listing().prompts().tool("mcp__beta__query")


def run(root: Path, split: str = "all", **params: Any) -> runner.RunResult:
    spec = ("prune", {"overlay": {"prune": {**FAST, **params}}})
    return runner.run_parallel([root], [spec], jobs=1, cutoff=CUTOFF, split=split, cfg=CFG)


def part(res: runner.RunResult, split: str) -> dict[str, Any]:
    out: dict[str, Any] = res.by_split[split]["prune"]
    return out


def listing_tokens() -> int:
    return estimate_tokens("\n".join(sorted(ALPHA)))


# ------------------------------------------------------------------ the required behaviours


def test_idle_server_would_be_pruned(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    for i, sid in enumerate("abc"):
        idle(sid, f"2026-10-1{i}T09:00:00.000Z").write(src / f"{sid}.jsonl")
    dev = part(run(tmp_path / "t", lease_sessions=10), "dev")
    assert dev["sessions"] == 3
    assert dev["servers_seen"] == 2  # alpha and beta; built-ins are not candidates
    assert dev["would_prune"] == 1  # alpha, after a; beta is used
    assert dev["sessions_pruned"] == 2  # b and c ran with alpha pruned
    assert dev["listing_tokens_saved"] == 2 * listing_tokens()
    assert dev["needed_while_pruned"] == 0
    assert dev["lease_restores"] == 0
    assert dev["unobservable_sessions"] == 0


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        (lambda s: s.tool("mcp__alpha__get_page", {"id": 1}), "tool_call"),
        (
            lambda s: s.prompt("can you look this up in the alpha wiki via mcp__alpha"),
            "user_mention",
        ),
        (lambda s: s.tool("ToolSearch", {"query": "mcp__alpha page tools"}), "tool_search"),
    ],
)
def test_use_while_pruned_counts_needed(tmp_path: Path, how: Any, reason: str) -> None:
    src = tmp_path / "t" / "p"
    idle("a", "2026-10-10T09:00:00.000Z").write(src / "a.jsonl")
    b = idle("b", "2026-10-11T09:00:00.000Z")
    how(b)
    b.write(src / "b.jsonl")
    idle("c", "2026-10-12T09:00:00.000Z").write(src / "c.jsonl")
    dev = part(run(tmp_path / "t", lease_sessions=10), "dev")
    assert dev["would_prune"] == 1  # needed once: kept for good, never pruned again
    assert dev["needed_while_pruned"] == 1
    assert dev["needed_by"] == {"tool_call": 0, "tool_search": 0, "user_mention": 0} | {reason: 1}
    assert dev["sessions_pruned"] == 1  # b only; the restore holds from c on
    assert dev["lease_restores"] == 0


def test_lease_restore_counted(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    for i, sid in enumerate("abcde"):
        idle(sid, f"2026-10-1{i}T09:00:00.000Z").write(src / f"{sid}.jsonl")
    dev = part(run(tmp_path / "t"), "dev")
    # a: pruned; b: lease 1; c: lease 2 -> restored (fresh evidence needed); d: fresh evidence,
    # pruned again; e: lease 1
    assert dev["would_prune"] == 2
    assert dev["lease_restores"] == 1
    assert dev["sessions_pruned"] == 3  # b, c, e
    assert dev["needed_while_pruned"] == 0


class _Raising:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"settings writer called: {name}")


def test_never_calls_settings_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "t" / "p"
    for i, sid in enumerate("abc"):
        idle(sid, f"2026-10-1{i}T09:00:00.000Z").write(src / f"{sid}.jsonl")
    monkeypatch.setattr(prune, "WRITER", _Raising())

    def boom(*args: Any, **kw: Any) -> Any:
        raise AssertionError("git or another process was run")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(prune, "_apply", boom)
    monkeypatch.setattr(prune, "_restore_file", boom)
    with no_project_access(monkeypatch, tmp_path / "t") as seen:
        res = run(tmp_path / "t")
    assert seen == []
    assert part(res, "dev")["would_prune"] == 1
    assert part(res, "dev")["sessions_failed"] == 0
    assert not (tmp_path / "state").exists()  # nothing in the state root either


# ------------------------------------------------------------------ splits and edge cases


def test_need_in_test_after_dev_prune_is_cross_split(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    idle("a", DEV_TS).write(src / "a.jsonl")
    idle("b", TEST_TS).tool("mcp__alpha__put_page").write(src / "b.jsonl")
    res = run(tmp_path / "t", lease_sessions=10)
    dev, test = part(res, "dev"), part(res, "test")
    assert dev["would_prune"] == 1 and dev["needed_while_pruned"] == 0
    assert test["needed_while_pruned"] == 0  # the prune was decided in dev
    assert test["needed_while_pruned_cross_split"] == 1
    assert test["sessions_pruned"] == 0 and test["sessions_pruned_cross_split"] == 1
    assert test["listing_tokens_saved"] == 0
    assert test["listing_tokens_saved_cross_split"] == listing_tokens()
    assert "needed_while_pruned_cross_split" not in dev
    only_test = part(run(tmp_path / "t", split="test", lease_sessions=10), "test")
    assert only_test == test  # the row does not depend on the other split being reported


def test_lease_end_in_test_of_dev_prune_is_cross_split(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    idle("a", DEV_TS).write(src / "a.jsonl")
    idle("b", "2026-11-20T09:00:00.000Z").write(src / "b.jsonl")
    idle("c", "2026-11-21T09:00:00.000Z").write(src / "c.jsonl")
    test = part(run(tmp_path / "t"), "test")
    assert test["lease_restores"] == 0 and test["lease_restores_cross_split"] == 1


def test_session_without_listing_is_unobservable(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    for i, sid in enumerate("abc"):
        s = Log(sid, f"2026-10-1{i}T09:00:00.000Z").prompts().tool("mcp__beta__query")
        s.write(src / f"{sid}.jsonl")
    dev = part(run(tmp_path / "t"), "dev")
    assert dev["unobservable_sessions"] == 3
    assert dev["servers_seen"] == 0 and dev["would_prune"] == 0  # never guessed


def test_unobservable_session_accrues_no_evidence_from_known_listing(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    idle("a", "2026-10-10T09:00:00.000Z").write(src / "a.jsonl")  # lists alpha: now known
    for i, sid in enumerate("bc"):
        s = Log(sid, f"2026-10-1{i + 1}T09:00:00.000Z").prompts().tool("mcp__beta__query")
        s.write(src / f"{sid}.jsonl")
    dev = part(run(tmp_path / "t", min_sessions=3), "dev")
    assert dev["unobservable_sessions"] == 2
    assert dev["would_prune"] == 0  # production would assume a's listing for b and c


def test_copied_history_is_not_needed_again(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    a = idle("a", "2026-10-10T09:00:00.000Z").prompt("use mcp__alpha now")  # not pruned yet
    a.write(src / "a.jsonl")
    resumed = Log("c", "2026-10-10T09:00:00.000Z")
    resumed.records = [dict(r) for r in a.records]  # copied history, original timestamps
    resumed.ts = "2026-10-12T09:00:00.000Z"
    resumed.listing().prompts(2)
    resumed.write(src / "c.jsonl")
    dev = part(run(tmp_path / "t", lease_sessions=10), "dev")
    assert dev["would_prune"] == 1 and dev["sessions_pruned"] == 1
    assert dev["needed_while_pruned"] == 0  # the copied mention predates the prune


def test_subagent_file_counts_need_but_no_lease_tick(tmp_path: Path) -> None:
    src = tmp_path / "t" / "p"
    idle("a", "2026-10-10T09:00:00.000Z").write(src / "a.jsonl")
    sub = Log("agent-1", "2026-10-10T10:00:00.000Z", side=True).listing()
    sub.tool("mcp__beta__query")
    sub.write(src / "a" / "subagents" / "agent-1.jsonl")
    idle("b", "2026-10-11T09:00:00.000Z").write(src / "b.jsonl")
    dev = part(run(tmp_path / "t"), "dev")
    assert dev["sessions"] == 2 and dev["subagent_files"] == 1
    assert dev["lease_restores"] == 0  # only b ticked the lease (1 of 2)
    assert dev["sessions_pruned"] == 1


def test_failed_session_is_counted_and_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "t" / "p"
    for i, sid in enumerate("abc"):
        idle(sid, f"2026-10-1{i}T09:00:00.000Z").write(src / f"{sid}.jsonl")
    real = prune_lane.add_inventory_record

    def flaky(inv: Any, rec: dict[str, Any]) -> None:
        if rec.get("sessionId") == "b":
            raise ValueError("bad record")
        real(inv, rec)

    monkeypatch.setattr(prune_lane, "add_inventory_record", flaky)
    res = run(tmp_path / "t", lease_sessions=10)
    dev = part(res, "dev")
    assert dev["sessions_failed"] == 1 and dev["sessions"] == 2
    assert dev["would_prune"] == 1 and dev["sessions_pruned"] == 1
    assert res.sessions_failed == 0  # the lane never raised into the runner


def test_abort_and_unseen_sessions() -> None:
    lane = prune_lane.PruneLane(CFG, {})
    info = runner.SessionInfo("g", 0, "dev", CUTOFF, None, RunIds())
    lane.end_session(info)  # no record: not a session
    lane.abort_session(info)
    assert lane.report()["dev"]["sessions"] == 0
    lane.feed_record({"type": "user", "message": {"content": "hi"}}, info)
    lane.feed(None, info)  # type: ignore[arg-type]
    lane.abort_session(info)
    assert lane.report()["dev"]["sessions_failed"] == 1


def test_report_is_aggregates_only() -> None:
    lane = prune_lane.PruneLane(CFG, {})
    rep = lane.report()
    assert set(rep) == {"dev", "test"}
    text = repr(rep)
    assert "mcp__" not in text
