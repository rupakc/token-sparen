"""E1 facts lane: repo-fact mining risk and targeted waste from transcripts only (M4 Task 3).

Synthetic transcripts only; nothing here reads the real home or real sessions. The project
directories named in the transcripts live outside the transcript directory: in transcript
evidence mode the lane must never look at them (nor run git).
"""

from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_replay import Session

from token_sparen.config import Config, packaged_defaults
from token_sparen.facts import miners
from token_sparen.facts.sanitize import Fact, fact_id
from token_sparen.replay import facts_lane, runner
from token_sparen.transcript import ToolResultRecord

CUTOFF = date(2026, 11, 1)
FACTS = ("facts", {})
DEV_TS = "2026-10-10T09:00:00.000Z"
TEST_TS = "2026-11-20T09:00:00.000Z"
CFG = Config(data=packaged_defaults())


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(home))
    return home


class Log(Session):
    """A ``Session`` with a cwd of choice and one-line tool calls."""

    def __init__(self, sid: str, ts: str, cwd: str) -> None:
        super().__init__(sid, ts)
        self.cwd = cwd
        self.k = 0

    def _base(self, kind: str, **extra: Any) -> dict[str, Any]:
        rec = super()._base(kind, **extra)
        rec["cwd"] = self.cwd
        return rec

    def tool(self, name: str, inp: dict[str, Any], out: str = "ok", err: bool = False) -> Log:
        self.k += 1
        tid = f"t{self.k}"
        self.call(f"m{self.k}", tool=(tid, name, inp))
        self.result(tid, out, is_error=err, raw={"stdout": out})
        return self

    def bash(self, cmd: str, out: str = "ok", err: bool = False) -> Log:
        return self.tool("Bash", {"command": cmd}, out, err)


def lane(res: runner.RunResult, split: str) -> dict[str, Any]:
    out: dict[str, Any] = res.by_split[split]["facts"]
    return out


def kind(res: runner.RunResult, split: str, name: str) -> dict[str, Any]:
    row: dict[str, Any] = lane(res, split)["kinds"][name]
    return row


def run(root: Path, split: str = "all") -> runner.RunResult:
    return runner.run_parallel([root], [FACTS], jobs=1, cutoff=CUTOFF, split=split, cfg=CFG)


# ------------------------------------------------------------------ no filesystem, no git


@contextmanager
def no_project_access(mp: pytest.MonkeyPatch, allowed: Path) -> Iterator[list[str]]:
    """Every stat / open / read / directory listing outside ``allowed`` (and every subprocess)
    is recorded and raises ``PermissionError`` (recorded first: callers fail open on OSError)."""
    roots = {os.path.abspath(allowed), os.path.realpath(allowed)}
    # The interpreter's own standard library only (a coverage tracer resolves a stdlib module
    # first run under the guard; realpath lstat's each of its parent directories exactly).
    stdlib = os.path.realpath(sys.base_prefix)
    parents = {stdlib, *(str(p) for p in Path(stdlib).parents)}
    seen: list[str] = []

    def tooling(a: str) -> bool:
        return a in parents or a.startswith(stdlib + os.sep)

    class Forbidden(PermissionError):
        pass

    def check(p: Any) -> None:
        if isinstance(p, int):
            return  # an open file descriptor
        raw = os.fsdecode(os.fspath(p))
        a = os.path.abspath(raw)
        if not any(a == r or a.startswith(r + os.sep) for r in roots) and not tooling(a):
            seen.append(a)
            raise Forbidden(a)

    def wrap(fn: Any) -> Any:
        def inner(p: Any, *args: Any, **kw: Any) -> Any:
            check(p)
            return fn(p, *args, **kw)

        return inner

    def no_proc(*args: Any, **kw: Any) -> Any:
        seen.append(f"process {args[:1]!r}")
        raise Forbidden("process")

    with mp.context() as m:
        for mod, name in (
            (os, "stat"),
            (os, "lstat"),
            (os, "scandir"),
            (os, "listdir"),
            (os, "open"),
            (os, "access"),
            (os, "readlink"),
            (builtins, "open"),
            (io, "open"),
        ):
            m.setattr(mod, name, wrap(getattr(mod, name)))
        for name in ("stat", "lstat", "open", "read_text", "read_bytes"):
            m.setattr(Path, name, wrap(getattr(Path, name)))
        m.setattr(subprocess, "run", no_proc)
        m.setattr(subprocess, "Popen", no_proc)
        for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execve", "fork"):
            if hasattr(os, name):
                m.setattr(os, name, no_proc)
        yield seen


def _project(tmp_path: Path) -> Path:
    """A real project outside the transcript dir: manifest declaring a package, a git repo, a
    generated directory with marked files. Full evidence mode would read all of it."""
    proj = tmp_path / "project"
    (proj / "gen").mkdir(parents=True)
    (proj / "package.json").write_text(json.dumps({"devDependencies": {"eslint": "1"}}))
    (proj / "gen" / "a.go").write_text("// Code generated by tool. DO NOT EDIT.\n")
    (proj / ".gitignore").write_text("gen/\n")
    return proj


def _fs_session(sid: str, ts: str, proj: Path) -> Log:
    s = Log(sid, ts, str(proj)).prompt()
    s.bash("eslint src", "zsh: command not found: eslint", err=True)
    s.bash("npx eslint src", "clean")  # downloader: needs the manifest (skipped)
    s.tool("Read", {"file_path": f"{proj}/gen/a.go"}, "     1\t// Code generated. DO NOT EDIT.")
    s.tool("Edit", {"file_path": f"{proj}/gen/a.go", "old_string": "a", "new_string": "b"})
    s.tool("Edit", {"file_path": f"{proj}/gen/b.go", "old_string": "a", "new_string": "b"})
    s.bash("make build", "built")
    return s


def test_transcript_mode_never_touches_fs_or_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _project(tmp_path)
    src = tmp_path / "transcripts"
    _fs_session("a", DEV_TS, proj).write(src / "p" / "a.jsonl")
    _fs_session("b", "2026-10-12T09:00:00.000Z", proj).write(src / "p" / "b.jsonl")
    _fs_session("c", TEST_TS, proj).write(src / "p" / "c.jsonl")
    monkeypatch.setenv("TOKEN_SPAREN_HOME", str(src / "state"))
    with no_project_access(monkeypatch, src) as seen:
        res = run(src)
        assert seen == []  # nothing outside the transcripts was stat'ed, opened or run
        # sanity: the guard does catch full evidence mode reading the project
        calls = [
            ToolResultRecord("1", "Bash", {"command": "eslint src"}, "eslint: not found", 1, True),
            ToolResultRecord("2", "Bash", {"command": "npx eslint src"}, "clean", 1),
        ]
        miners.mine_calls(calls, "s", project_dir=str(proj), evidence="full")
        assert seen and all(str(proj) in s for s in seen)
    assert res.sessions == 3 and res.sessions_failed == 0 and not res.worker_errors
    alias = kind(res, "dev", "command_alias")
    assert alias["candidates"] == 0 and alias["skipped_needs_fs"] == 2
    gen = kind(res, "dev", "generated_dir")
    assert gen["candidates"] == 1  # evidence: the Read head showed the generated marker
    assert gen["render_tokens"] > 0
    # the later sessions edit the generated dir again and rebuild: the waste it targets
    assert gen["waste_events"] == 1
    assert gen["contradiction_observable"] is False  # needs git: 0 is not evidence
    test_gen = kind(res, "test", "generated_dir")
    assert test_gen["waste_events"] == 0 and test_gen["waste_events_cross_split"] == 1
    assert lane(res, "dev")["sessions"] == 2 and lane(res, "test")["sessions"] == 1


# ------------------------------------------------------------------ contradictions


def _alias_session(sid: str, ts: str, cwd: str) -> Log:
    s = Log(sid, ts, cwd).prompt()
    s.bash("pytest -q", "zsh: command not found: pytest", err=True)
    s.bash("python -m pytest -q", "3 passed")
    return s


def test_contradiction_counts_risk(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
    ok = Log("b", "2026-10-11T09:00:00.000Z", cwd).prompt().bash("python -m pytest -q", "1 passed")
    ok.write(src / "b.jsonl")
    bad = Log("c", "2026-10-12T09:00:00.000Z", cwd).prompt()
    bad.bash("python -m pytest -q", "No module named pytest", err=True)
    bad.write(src / "c.jsonl")
    again = Log("d", TEST_TS, cwd).prompt()
    again.bash("python -m pytest -q", "No module named pytest", err=True)
    again.write(src / "d.jsonl")
    res = run(tmp_path / "t")
    alias = kind(res, "dev", "command_alias")
    assert alias["candidates"] == 1 and alias["eligible"] == 0
    assert alias["contradicted_later"] == 1  # once per candidate, however often
    cross = kind(res, "test", "command_alias")
    assert cross["contradicted_later"] == 0 and cross["contradicted_cross_split"] == 1
    test = kind(res, "dev", "test_command")
    assert test["candidates"] == 1 and test["eligible"] == 1  # a and b: 2 sessions
    assert test["contradicted_later"] == 1  # its own runner was missing later
    assert kind(res, "all", "command_alias")["contradicted_later"] == 1


def test_dev_rows_do_not_depend_on_the_test_split(tmp_path: Path) -> None:
    """A dev candidate contradicted (and re-mined) only in test sessions changes no dev figure:
    the dev row of an ``all`` run equals the dev-only run's; the test row says it apart."""
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
    _alias_session("b", TEST_TS, cwd).write(src / "b.jsonl")  # would make it eligible
    bad = Log("c", "2026-11-21T09:00:00.000Z", cwd).prompt()
    bad.bash("python -m pytest -q", "No module named pytest", err=True)
    bad.write(src / "c.jsonl")
    every, dev_only = run(tmp_path / "t"), run(tmp_path / "t", split="dev")
    assert lane(every, "dev") == lane(dev_only, "dev")
    for name in ("command_alias", "test_command"):
        assert kind(every, "dev", name)["contradicted_later"] == 0
        assert kind(every, "dev", name)["eligible"] == 0
        row = kind(every, "test", name)
        assert row["contradicted_cross_split"] == 1 and row["contradicted_later"] == 0
        assert row["candidates"] == 0 and row["eligible"] == 0  # dev's, not new in test
    test_only = run(tmp_path / "t", split="test")
    assert lane(test_only, "test") == lane(every, "test")


def test_mining_session_does_not_contradict_itself(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    s = _alias_session("a", DEV_TS, cwd)
    s.bash("python -m pytest -q", "No module named pytest", err=True)  # same session: not later
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    res = run(tmp_path / "t")
    assert kind(res, "dev", "command_alias")["contradicted_later"] == 0


# ------------------------------------------------------------------ waste


def tokens(name: str, inp: dict[str, Any], out: str = "ok", err: bool = False) -> int:
    return facts_lane.call_tokens(ToolResultRecord("x", name, inp, out, 0, err))


NOT_FOUND = "zsh: command not found: pytest"
RED = "F" * 40 + "\n1 failed, 2 passed\n" + "assert 1 == 2\n" * 200


def _waste_run(tmp_path: Path, first: Log, *later: Log) -> dict[str, Any]:
    """Mine ``first`` (dev), then the ``later`` dev sessions; the dev row's kinds."""
    src = tmp_path / "t" / "p"
    first.write(src / "a.jsonl")
    for i, s in enumerate(later):
        s.write(src / f"b{i}.jsonl")
    out: dict[str, Any] = lane(run(tmp_path / "t"), "dev")["kinds"]
    return out


def _dev(sid: str, day: int) -> Log:
    return Log(sid, f"2026-10-{day:02d}T09:00:00.000Z", "/nonexistent/proj").prompt()


def test_waste_counted_per_kind(tmp_path: Path) -> None:
    env_err = "Traceback ...\nKeyError: 'API_HOST'\n" + "detail line\n" * 50
    first = _alias_session("a", DEV_TS, "/nonexistent/proj")
    first.bash("make run", "KeyError: 'API_HOST'", err=True)
    first.bash("API_HOST=local make run", "running")
    later = _dev("b", 11)
    later.bash("make run", env_err, err=True)
    later.bash("make run", env_err, err=True)
    later.bash("API_HOST=local make run", "running")
    later.bash("pytest -q", NOT_FOUND, err=True)
    later.bash("python -m pytest -q", "3 passed")
    kinds = _waste_run(tmp_path, first, later)
    for name in ("requires_env", "command_alias", "test_command"):
        assert kinds[name]["candidates"] == 1
        assert kinds[name]["render_tokens"] > 0
    env = kinds["requires_env"]
    assert env["waste_events"] == 1 and env["eligible"] == 1
    assert env["waste_tokens"] == 2 * tokens("Bash", {"command": "make run"}, env_err, True)
    missing = tokens("Bash", {"command": "pytest -q"}, NOT_FOUND, True)
    assert kinds["command_alias"]["waste_events"] == 1
    assert kinds["command_alias"]["waste_tokens"] == missing
    assert kinds["test_command"]["waste_events"] == 1
    assert kinds["test_command"]["waste_tokens"] == missing
    assert kinds["build_first"]["waste_events"] == 0


def test_red_runs_of_the_right_spelling_are_no_waste(tmp_path: Path) -> None:
    later = _dev("b", 11)
    later.bash("pytest -q", NOT_FOUND, err=True)
    for _ in range(5):
        later.bash("python -m pytest -q", RED, err=True)  # the fact's spelling, red phase
    later.bash("python -m pytest -q", "3 passed")
    red_only = _dev("c", 12)
    for _ in range(3):
        red_only.bash("python -m pytest -q", RED, err=True)
    red_only.bash("python -m pytest -q", "3 passed")
    kinds = _waste_run(tmp_path, _alias_session("a", DEV_TS, "/nonexistent/proj"), later, red_only)
    missing = tokens("Bash", {"command": "pytest -q"}, NOT_FOUND, True)
    for name in ("command_alias", "test_command"):
        assert kinds[name]["waste_events"] == 1, name
        assert kinds[name]["waste_tokens"] == missing, name


def test_alias_waste_needs_the_spelling_to_work_after(tmp_path: Path) -> None:
    later = _dev("b", 11).bash("pytest -q", NOT_FOUND, err=True)  # gave up: nothing proven
    kinds = _waste_run(tmp_path, _alias_session("a", DEV_TS, "/nonexistent/proj"), later)
    assert kinds["command_alias"]["waste_events"] == 0
    assert kinds["command_alias"]["waste_tokens"] == 0
    assert kinds["test_command"]["waste_events"] == 1  # a spelling that does not work here


def test_wrong_runner_failure_is_test_command_waste(tmp_path: Path) -> None:
    first = _dev("a", 10).bash("python -m pytest -q", "3 passed")
    later = _dev("b", 11)
    later.bash("npm test", RED, err=True)  # another runner, then the fact's runner passes
    later.bash("python -m pytest -q", RED, err=True)  # own runner red: not waste
    later.bash("python -m pytest -q", "3 passed")
    unproven = _dev("c", 12).bash("npm test", RED, err=True)  # never followed by the own runner
    kinds = _waste_run(tmp_path, first, later, unproven)
    row = kinds["test_command"]
    assert row["candidates"] == 1 and row["waste_events"] == 1
    assert row["waste_tokens"] == tokens("Bash", {"command": "npm test"}, RED, True)


def test_build_first_waste_is_the_failures_the_build_fixed(tmp_path: Path) -> None:
    fail = "FAILED: stale build artefacts"
    first = _dev("a", 10)
    first.bash("make test", fail, err=True).bash("make build", "built").bash("make test", "ok")
    later = _dev("b", 11)
    later.bash("make test", fail, err=True)
    later.bash("make test", fail, err=True)
    later.bash("make build", "built")
    later.bash("make test", "ok")
    fixed_alone = _dev("c", 12)
    fixed_alone.bash("make test", fail, err=True).bash("make test", "ok")  # no build: no waste
    kinds = _waste_run(tmp_path, first, later, fixed_alone)
    row = kinds["build_first"]
    assert row["candidates"] == 1 and row["waste_events"] == 1
    assert row["waste_tokens"] == 2 * tokens("Bash", {"command": "make test"}, fail, True)


def test_generated_dir_waste_is_regenerated_edits(tmp_path: Path) -> None:
    proj = "/nonexistent/proj"
    head = "     1\t// Code generated. DO NOT EDIT."
    edit = {"file_path": f"{proj}/gen/a.go", "old_string": "a", "new_string": "b"}
    first = _dev("a", 10).tool("Read", {"file_path": f"{proj}/gen/a.go"}, head)
    first.tool("Edit", edit).tool("Edit", {**edit, "file_path": f"{proj}/gen/b.go"})
    later = _dev("b", 11).tool("Edit", edit).bash("make build", "built")
    lint = _dev("c", 12).tool("Edit", edit).bash("ruff check .", "ok")  # no regeneration
    kinds = _waste_run(tmp_path, first, later, lint)
    row = kinds["generated_dir"]
    assert row["candidates"] == 1 and row["waste_events"] == 1
    assert row["waste_tokens"] == tokens("Edit", edit)
    assert row["waste_unmeasured"] == 1  # edited, never regenerated: not measurable
    assert row["contradiction_observable"] is False


def test_render_tokens_cost_a_fact_over_the_block_budget(tmp_path: Path) -> None:
    cfg = Config(data={**packaged_defaults(), "facts": {"max_tokens": 1}})
    lane_ = facts_lane.FactsLane(cfg, {})
    fact = Fact("test_command", "tests", "python -m pytest", frozenset())
    assert lane_._render(fact_id(fact), fact) > 0


def test_split_test_uses_dev_history_but_reports_test_only(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
    later = Log("b", TEST_TS, cwd).prompt().bash("pytest -q", NOT_FOUND, err=True)
    later.bash("python -m pytest -q", "3 passed")
    later.write(src / "b.jsonl")
    res = run(tmp_path / "t", split="test")
    assert "dev" not in res.by_split
    row = kind(res, "test", "command_alias")
    assert row["waste_events_cross_split"] == 1 and row["waste_events"] == 0
    assert row["waste_tokens_cross_split"] > 0 and row["candidates"] == 0


def test_parallel_equals_serial(tmp_path: Path) -> None:
    for g in range(4):
        cwd = f"/nonexistent/proj{g}"
        src = tmp_path / "t" / f"p{g}"
        _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
        later = Log("b", TEST_TS, cwd).prompt()
        later.bash("pytest -q", NOT_FOUND, err=True).bash("python -m pytest -q", "ok")
        later.write(src / "b.jsonl")
    one = runner.run_parallel([tmp_path / "t"], [FACTS], jobs=1, cutoff=CUTOFF, cfg=CFG)
    two = runner.run_parallel([tmp_path / "t"], [FACTS], jobs=2, cutoff=CUTOFF, cfg=CFG)
    assert one.by_split == two.by_split
    assert kind(one, "all", "command_alias")["candidates"] == 4


def test_duplicate_history_not_mined_twice(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    orig = _alias_session("a", DEV_TS, cwd)
    orig.write(src / "a.jsonl")
    resumed = Log("a", DEV_TS, cwd)
    resumed.records = list(orig.records)  # copied history, then nothing new
    resumed.write(src / "a2.jsonl")
    res = run(tmp_path / "t")
    alias = kind(res, "dev", "command_alias")
    assert alias["candidates"] == 1 and alias["eligible"] == 0


def test_bad_session_counted_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = "/nonexistent/proj"
    _alias_session("a", DEV_TS, cwd).write(tmp_path / "t" / "p" / "a.jsonl")

    def boom(self: Any, call: Any) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(miners.SessionMiner, "feed", boom)
    res = run(tmp_path / "t")
    assert lane(res, "dev")["sessions_failed"] == 1
    assert kind(res, "dev", "command_alias")["candidates"] == 0


def test_lane_registered() -> None:
    assert runner.lane_factory("facts") is runner.LANES["facts"]
    assert isinstance(runner.LANES["facts"](CFG, {}), facts_lane.FactsLane)
