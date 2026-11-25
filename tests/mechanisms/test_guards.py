"""Pre-tool guards (block unbounded scans) and the learned pre-trim decision point."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from token_sparen.config import Config, load_config
from token_sparen.events import Event, ToolCall
from token_sparen.learn.policy import PRETRIM, StaticPolicy
from token_sparen.mechanisms import guards

KEY = "0123456789abcdef"
BIG = 5_000_001


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"TOKEN_SPAREN_HOME": str(tmp_path / "home")}
    env.update({f"TOKEN_SPAREN_{k}": v for k, v in over.items()})
    return env


def _cfg(tmp_path: Path, **over: str) -> Config:
    return load_config(tmp_path / "proj", _env(tmp_path, **over))


def _event(tmp_path: Path, tool: str, tool_input: Mapping[str, Any]) -> Event:
    from token_sparen.adapters.claude.translate import tool_category

    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Event(
        host="claude",
        kind="pre_tool",
        session_id="s-1",
        project_dir=proj,
        project_key=KEY,
        transcript_path=None,
        tool=ToolCall(tool, tool_category(tool), dict(tool_input), "tu-1"),
    )


def _bash(tmp_path: Path, command: str, **extra: Any) -> Event:
    return _event(tmp_path, "Bash", {"command": command, **extra})


def _check(tmp_path: Path, command: str, **over: str) -> str | None:
    return guards.check(_bash(tmp_path, command), _cfg(tmp_path, **over)).block_reason


def _big_file(tmp_path: Path, name: str = "big.log", size: int = BIG) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    path = proj / name
    with path.open("wb") as fh:
        fh.truncate(size)  # sparse: cheap to create
    return path


# --- find -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "find / -name foo",
        "find ~ -type f",
        "find ~/ -name '*.py'",
        "find -L / -name x",
        "find / -path '*/x/*' -prune -o -print",
        "find / -name x 2>/dev/null | head -5",
        "cd src && find / -name y",
        "ls; find ~ -name z",
        "FOO=1 find / -name x",
        "sudo find / -name x",
        "find . -name a && find / -name b",
    ],
)
def test_blocks_find_root(tmp_path: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None
    assert "-maxdepth" in reason and reason.count(".") >= 1


@pytest.mark.parametrize(
    "command",
    [
        "find / -maxdepth 2 -name x",
        "find ~ -name x -maxdepth 3",
        "find . -name x",
        "find src -type f",
        "find /usr/lib -name x",
        "find ./ -name '/'",
        "echo find /",
        "grep -r 'find /' src",
        "find",
        "fd x /",
    ],
)
def test_allows_find_with_maxdepth(tmp_path: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- grep / rg --------------------------------------------------------------------------


@pytest.fixture()
def vendored(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("node_modules", ".git", "dist", "build", ".venv", "src"):
        (proj / name).mkdir(parents=True)
    (proj / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    return proj


@pytest.mark.parametrize(
    "command",
    [
        "grep -r foo node_modules",
        "grep -rn foo ./node_modules/",
        "grep -R foo .git",
        "grep --recursive foo dist",
        "grep -nir foo build",
        "rg foo .venv",
        "rg -n foo src node_modules",
        "grep -r -e foo node_modules",
        "cat x | grep -r foo node_modules",
    ],
)
def test_blocks_recursive_grep_over_vendored(tmp_path: Path, vendored: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None and "include" in reason


@pytest.mark.parametrize(
    "command",
    [
        "grep -r --include=*.js foo node_modules",
        "grep -r --include '*.js' foo node_modules",
        "rg -g '*.ts' foo node_modules",
        "rg --glob=*.ts foo node_modules",
        "rg -t js foo node_modules",
        "grep -r node_modules src",
        "rg node_modules src",
        "grep foo node_modules/x.js",
        "grep -r foo node_modules/x.js",
        "grep -r foo src",
        "grep -r foo .",
        "rg -e node_modules src",
        "grep -r foo missing/node_modules",
    ],
)
def test_allows_bounded_grep(tmp_path: Path, vendored: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- cat / Read -------------------------------------------------------------------------


def test_blocks_cat_huge_file(tmp_path: Path) -> None:
    big = _big_file(tmp_path)
    (tmp_path / "proj" / "small.txt").write_text("hi", encoding="utf-8")
    reason = _check(tmp_path, "cat big.log")
    assert reason is not None and ("head" in reason or "tail" in reason)
    assert _check(tmp_path, f"cat -n {big}") is not None
    assert _check(tmp_path, "cat small.txt big.log") is not None
    assert _check(tmp_path, "cat small.txt") is None
    assert _check(tmp_path, "cat missing.txt") is None
    # the output is bounded or does not reach the context
    assert _check(tmp_path, "cat big.log | head -n 50") is None
    assert _check(tmp_path, "cat big.log > copy.log") is None
    # below the threshold, and a raised threshold
    _big_file(tmp_path, "edge.log", 5_000_000)
    assert _check(tmp_path, "cat edge.log") is None
    assert _check(tmp_path, "cat big.log", GUARDS__MAX_READ_BYTES="9000000") is None


def test_blocks_read_tool_huge_file_without_range(tmp_path: Path) -> None:
    big = _big_file(tmp_path)
    cfg = _cfg(tmp_path)
    reason = guards.check(_event(tmp_path, "Read", {"file_path": str(big)}), cfg).block_reason
    assert reason is not None and "offset" in reason and "limit" in reason
    for rng in ({"limit": 200}, {"offset": 10}, {"offset": 1, "limit": 5}):
        ev = _event(tmp_path, "Read", {"file_path": str(big), **rng})
        assert guards.check(ev, cfg).is_noop()
    small = tmp_path / "proj" / "s.txt"
    small.write_text("x", encoding="utf-8")
    for path in (str(small), str(tmp_path / "nope"), 3):
        assert guards.check(_event(tmp_path, "Read", {"file_path": path}), cfg).is_noop()


# --- ls -R ------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["ls -R /", "ls -laR ~", "ls --recursive /", "ls -R / | wc"])
def test_blocks_ls_recursive_root(tmp_path: Path, command: str) -> None:
    reason = _check(tmp_path, command)
    assert reason is not None and "depth" in reason


@pytest.mark.parametrize("command", ["ls -R src", "ls /", "ls -la ~", "ls -R"])
def test_allows_ls(tmp_path: Path, command: str) -> None:
    assert _check(tmp_path, command) is None


# --- robustness -------------------------------------------------------------------------


def test_shlex_parse_error_never_blocks(tmp_path: Path) -> None:
    assert _check(tmp_path, 'find / -name "unterminated') is None
    assert _check(tmp_path, "find / -name 'x") is None


def test_guards_disabled_flag(tmp_path: Path) -> None:
    assert _check(tmp_path, "find / -name x", GUARDS__ENABLED="false") is None
    assert _check(tmp_path, "find / -name x", GUARDS__ENABLED="true") is not None


def test_guards_ignore_other_tools_and_bad_input(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert guards.check(_event(tmp_path, "Grep", {"pattern": "x", "path": "/"}), cfg).is_noop()
    assert guards.check(_event(tmp_path, "Bash", {"command": 3}), cfg).is_noop()
    assert guards.check(_event(tmp_path, "Bash", {}), cfg).is_noop()
    ev = _bash(tmp_path, "find / -name x")
    post = Event(**{**ev.__dict__, "kind": "post_tool"})
    assert guards.check(post, cfg).is_noop()
    no_tool = Event(**{**ev.__dict__, "tool": None})
    assert guards.check(no_tool, cfg).is_noop()


# --- pretrim ----------------------------------------------------------------------------


def _store(tmp_path: Path, candidates: Mapping[str, Any] | str) -> Path:
    store = tmp_path / "home" / "projects" / KEY
    store.mkdir(parents=True, exist_ok=True)
    body = candidates if isinstance(candidates, str) else json.dumps({"candidates": candidates})
    (store / "pretrim.json").write_text(body, encoding="utf-8")
    return store


def _pretrim(
    tmp_path: Path, ev: Event, store: Path, arms: dict[str, str] | None = None, **over: str
) -> Any:
    return guards.pretrim(
        ev, StaticPolicy(arms or {}), store, _cfg(tmp_path, **over), env=_env(tmp_path)
    )


def _decisions(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "home" / "projects" / KEY / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_pretrim_point_contract() -> None:
    assert guards.PRETRIM is PRETRIM
    assert PRETRIM.name == "pretrim" and PRETRIM.arms == ("off", "bounded")
    assert PRETRIM.safe_arm == "off" and PRETRIM.tier == "medium"
    assert guards._normalise("pytest -q") == "pytest -q"


def test_pretrim_rewrites_learned_command(tmp_path: Path) -> None:
    store = _store(tmp_path, {"pytest -q": {"lines": 80}})
    act = _pretrim(tmp_path, _bash(tmp_path, "pytest -q"), store)
    assert act.rewrite_input == {"command": "pytest -q 2>&1 | tail -n 80"}
    assert act.block_reason is None
    (rec,) = _decisions(tmp_path)
    assert rec["point"] == "pretrim" and rec["arm"] == "bounded" and rec["propensity"] == 1.0
    assert rec["command_sig"] == "pytest -q" and rec["tool_name"] == "Bash"


def test_pretrim_preserves_other_input_fields(tmp_path: Path) -> None:
    store = _store(tmp_path, {"make test": {}})
    ev = _bash(tmp_path, "make test", description="run tests", timeout=60000, extra={"a": [1]})
    act = _pretrim(tmp_path, ev, store)
    assert act.rewrite_input == {
        "command": "make test 2>&1 | tail -n 120",
        "description": "run tests",
        "timeout": 60000,
        "extra": {"a": [1]},
    }
    act = _pretrim(tmp_path, ev, store, GUARDS__PRETRIM_LINES="40")
    assert act.rewrite_input["command"] == "make test 2>&1 | tail -n 40"
    assert ev.tool is not None and ev.tool.input["command"] == "make test"  # not mutated


@pytest.mark.parametrize(
    "command",
    [
        "pytest | tee out",
        "pytest > out.txt",
        "pytest < in.txt",
        "make && make test",
        "make || true",
        "make; make test",
        "echo `date`",
        "echo $(date)",
        "cat <<EOF\nx\nEOF",
        "sleep 10 &",
        "pytest\nls",
        "make 2>err.txt",
    ],
)
def test_pretrim_never_rewrites_pipelines(tmp_path: Path, command: str) -> None:
    store = _store(tmp_path, {command: {"lines": 50}})
    assert _pretrim(tmp_path, _bash(tmp_path, command), store).is_noop()
    assert _decisions(tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        "vim x.py",
        "less log",
        "more log",
        "top",
        "watch ls",
        "ssh host",
        "python",
        "node",
        "python3",
    ],
)
def test_pretrim_never_rewrites_interactive(tmp_path: Path, command: str) -> None:
    store = _store(tmp_path, {command: {}})
    assert _pretrim(tmp_path, _bash(tmp_path, command), store).is_noop()


def test_pretrim_allows_scripts(tmp_path: Path) -> None:
    store = _store(tmp_path, {"python run.py": {}, "node build.js": {}})
    for command in ("python run.py", "node build.js"):
        act = _pretrim(tmp_path, _bash(tmp_path, command), store)
        assert act.rewrite_input == {"command": f"{command} 2>&1 | tail -n 120"}


def test_pretrim_noop_cases(tmp_path: Path) -> None:
    store = _store(tmp_path, {"pytest": {"lines": 10}})
    # not a candidate: nothing decided, nothing logged
    assert _pretrim(tmp_path, _bash(tmp_path, "pytest -x"), store).is_noop()
    assert _decisions(tmp_path) == []
    # arm off: logged, not rewritten
    assert _pretrim(tmp_path, _bash(tmp_path, "pytest"), store, {"pretrim": "off"}).is_noop()
    assert [r["arm"] for r in _decisions(tmp_path)] == ["off"]
    # background, other tools, shlex error, guards disabled
    bg = _bash(tmp_path, "pytest", run_in_background=True)
    assert _pretrim(tmp_path, bg, store).is_noop()
    assert _pretrim(tmp_path, _event(tmp_path, "Read", {"command": "pytest"}), store).is_noop()
    store = _store(tmp_path, {"echo 'x": {}})
    assert _pretrim(tmp_path, _bash(tmp_path, "echo 'x"), store).is_noop()
    store = _store(tmp_path, {"pytest": {}})
    off = _pretrim(tmp_path, _bash(tmp_path, "pytest"), store, GUARDS__ENABLED="false")
    assert off.is_noop()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"candidates": []}',
        '{"candidates": {"pytest": 3}}',
    ],
)
def test_pretrim_bad_store_is_noop_or_default(tmp_path: Path, body: str) -> None:
    store = _store(tmp_path, body)
    act = _pretrim(tmp_path, _bash(tmp_path, "pytest"), store)
    assert act.is_noop() or act.rewrite_input == {"command": "pytest 2>&1 | tail -n 120"}


def test_pretrim_bad_lines_falls_back(tmp_path: Path) -> None:
    for lines in (0, -5, "12", True, 10.5):
        store = _store(tmp_path, {"pytest": {"lines": lines}})
        act = _pretrim(tmp_path, _bash(tmp_path, "pytest"), store)
        assert act.rewrite_input == {"command": "pytest 2>&1 | tail -n 120"}


def test_pretrim_missing_store(tmp_path: Path) -> None:
    store = tmp_path / "home" / "projects" / KEY
    assert _pretrim(tmp_path, _bash(tmp_path, "pytest"), store).is_noop()


@pytest.mark.perf
def test_guard_check_fast(tmp_path: Path) -> None:
    from perfutil import best_of, perf_factor

    cfg = _cfg(tmp_path)
    ev = _bash(tmp_path, " && ".join(f"grep -rn 'x{i}' src/{i} | head -5" for i in range(50)))
    best, act = best_of(lambda: guards.check(ev, cfg), 5)
    assert act.is_noop()
    assert best < 0.005 * perf_factor()
