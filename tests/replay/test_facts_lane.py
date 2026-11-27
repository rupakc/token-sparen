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

import token_sparen
from token_sparen.config import Config, packaged_defaults
from token_sparen.facts import miners
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


def call_tokens(cmd: str, out: str, err: bool = True) -> int:
    rec = ToolResultRecord("x", "Bash", {"command": cmd}, out, 0, err)
    return facts_lane.call_tokens(rec)


# ------------------------------------------------------------------ no filesystem, no git


@contextmanager
def no_project_access(mp: pytest.MonkeyPatch, allowed: Path) -> Iterator[list[str]]:
    """Every stat / open / read / directory listing outside ``allowed`` (and every subprocess)
    is recorded and raises ``PermissionError`` (recorded first: callers fail open on OSError)."""
    roots = {os.path.abspath(allowed), os.path.realpath(allowed)}
    # The interpreter's own files (and their parent directories: realpath walks them), e.g. a
    # coverage tracer resolving a stdlib module first run under the guard. Never a project.
    tools = {
        os.path.realpath(p)
        for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, Path(token_sparen.__file__).parent)
    }
    seen: list[str] = []

    def tooling(a: str) -> bool:
        return any(
            a == t or a.startswith(t + os.sep) or t.startswith(a.rstrip(os.sep) + os.sep)
            for t in tools
        )

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
        seen.append(f"subprocess {args[:1]!r}")
        raise Forbidden("subprocess")

    with mp.context() as m:
        for mod, name in (
            (os, "stat"),
            (os, "lstat"),
            (os, "scandir"),
            (os, "listdir"),
            (builtins, "open"),
            (io, "open"),
        ):
            m.setattr(mod, name, wrap(getattr(mod, name)))
        for name in ("stat", "lstat", "open", "read_text", "read_bytes"):
            m.setattr(Path, name, wrap(getattr(Path, name)))
        m.setattr(subprocess, "run", no_proc)
        m.setattr(subprocess, "Popen", no_proc)
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
    assert gen["waste_events"] == 1 and kind(res, "test", "generated_dir")["waste_events"] == 1
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
    assert kind(res, "test", "command_alias")["contradicted_later"] == 0  # by mining split
    test = kind(res, "dev", "test_command")
    assert test["candidates"] == 1 and test["eligible"] == 1  # a and b: 2 sessions
    assert test["contradicted_later"] == 1  # its own runner was missing later
    assert kind(res, "all", "command_alias")["contradicted_later"] == 1


def test_mining_session_does_not_contradict_itself(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    s = _alias_session("a", DEV_TS, cwd)
    s.bash("python -m pytest -q", "No module named pytest", err=True)  # same session: not later
    s.write(tmp_path / "t" / "p" / "a.jsonl")
    res = run(tmp_path / "t")
    assert kind(res, "dev", "command_alias")["contradicted_later"] == 0


# ------------------------------------------------------------------ waste


def test_waste_counted_per_kind(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    first = _alias_session("a", DEV_TS, cwd)
    first.bash("make run", "KeyError: 'API_HOST'", err=True)
    first.bash("API_HOST=local make run", "running")
    first.write(src / "a.jsonl")
    env_err = "Traceback ...\nKeyError: 'API_HOST'\n" + "detail line\n" * 50
    later = Log("b", TEST_TS, cwd).prompt()
    later.bash("make run", env_err, err=True)
    later.bash("make run", env_err, err=True)
    later.bash("API_HOST=local make run", "running")
    later.bash("pytest -q", "zsh: command not found: pytest", err=True)
    later.bash("python -m pytest -q", "3 passed")
    later.write(src / "b.jsonl")
    res = run(tmp_path / "t")
    dev, test = lane(res, "dev"), lane(res, "test")
    assert dev["waste_tokens_upper_bound"] is True
    for name in ("requires_env", "command_alias", "test_command"):
        assert dev["kinds"][name]["candidates"] == 1
        assert dev["kinds"][name]["waste_events"] == 0  # unknown before it was mined
        assert dev["kinds"][name]["render_tokens"] > 0
    env = test["kinds"]["requires_env"]
    assert env["waste_events"] == 1
    assert env["waste_tokens"] == 2 * call_tokens("make run", env_err)
    missing = call_tokens("pytest -q", "zsh: command not found: pytest")
    assert test["kinds"]["command_alias"]["waste_events"] == 1
    assert test["kinds"]["command_alias"]["waste_tokens"] == missing
    assert test["kinds"]["test_command"]["waste_events"] == 1
    assert test["kinds"]["test_command"]["waste_tokens"] == missing
    assert test["kinds"]["build_first"]["waste_events"] == 0
    assert test["kinds"]["requires_env"]["candidates"] == 0  # re-mined, not new
    assert dev["kinds"]["requires_env"]["eligible"] == 1  # the later session re-derived it


def test_split_test_uses_dev_history_but_reports_test_only(tmp_path: Path) -> None:
    cwd = "/nonexistent/proj"
    src = tmp_path / "t" / "p"
    _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
    later = Log("b", TEST_TS, cwd).prompt().bash("pytest -q", "command not found: pytest", err=True)
    later.write(src / "b.jsonl")
    res = run(tmp_path / "t", split="test")
    assert "dev" not in res.by_split
    assert kind(res, "test", "command_alias")["waste_events"] == 1
    assert kind(res, "test", "command_alias")["candidates"] == 0


def test_parallel_equals_serial(tmp_path: Path) -> None:
    for g in range(4):
        cwd = f"/nonexistent/proj{g}"
        src = tmp_path / "t" / f"p{g}"
        _alias_session("a", DEV_TS, cwd).write(src / "a.jsonl")
        later = Log("b", TEST_TS, cwd).prompt()
        later.bash("pytest -q", "command not found: pytest", err=True)
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
