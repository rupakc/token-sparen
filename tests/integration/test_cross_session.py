"""M3 end to end (Task 29): repo facts, tool pruning, the loop guard and the kill switch.

Every hook goes through the real Claude Code entry point (``entry.main``) with synthetic
payloads, and sessions are folded through the real ``token-sparen fold`` command, as the
detached SessionEnd spawn would. State lives in a temporary ``TOKEN_SPAREN_HOME``, the project
is a temporary directory and the Claude config dir is temporary: nothing touches a real home,
``~/.claude`` or a real settings file.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from token_sparen.adapters.claude import entry
from token_sparen.cli import main as cli
from token_sparen.facts import inject, miners
from token_sparen.learn import engine as engine_mod
from token_sparen.learn import overlay
from token_sparen.learn.engine import pending_path
from token_sparen.mechanisms import loopguard, prune, tracking
from token_sparen.store import keycache, session

FACT_LINE = "`pytest` was not found here; `python -m pytest` worked"


# --------------------------------------------------------------------------- harness
class Log:
    """A synthetic Claude Code transcript: prompts, tool calls and their results."""

    def __init__(self) -> None:
        self.recs: list[dict[str, Any]] = []
        self.n = 0

    def prompt(self, text: str = "go") -> Log:
        self.recs.append({"type": "user", "message": {"role": "user", "content": text}})
        return self

    def call(self, tool: str, inp: dict[str, Any], out: str = "ok", err: bool = False) -> Log:
        self.n += 1
        use = {"type": "tool_use", "id": f"t{self.n}", "name": tool, "input": inp}
        self.recs.append({"type": "assistant", "message": {"id": f"m{self.n}", "content": [use]}})
        res = {"type": "tool_result", "tool_use_id": f"t{self.n}", "content": out, "is_error": err}
        self.recs.append({"type": "user", "message": {"content": [res]}})
        return self

    def bash(self, cmd: str, out: str = "ok", err: bool = False) -> Log:
        return self.call("Bash", {"command": cmd}, out, err)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in self.recs), encoding="utf-8")
        return path


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in list(os.environ):
        if k.startswith(("TOKEN_SPAREN_", "CLAUDE_")):
            monkeypatch.delenv(k)
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    return root


@pytest.fixture
def env(tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    e = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(proj),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "TOKEN_SPAREN_ADAPTER__LATENCY_SAMPLE_RATE": "0",
        "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "10000",
    }
    for k in ("TOKEN_SPAREN_HOME", "CLAUDE_CONFIG_DIR"):  # the CLI reads os.environ
        monkeypatch.setenv(k, e[k])
    monkeypatch.chdir(proj)
    return e


def _home(env: dict[str, str]) -> Path:
    return Path(env["TOKEN_SPAREN_HOME"])


def _pdir(env: dict[str, str], proj: Path) -> Path:
    return _home(env) / "projects" / keycache.cached_project_key(proj, env)


def hook(env: dict[str, str], event: str, sid: str, **payload: Any) -> str:
    """One hook through ``entry.main``; returns its raw stdout."""
    body = {"session_id": sid, "hook_event_name": event, "cwd": env["CLAUDE_PROJECT_DIR"]}
    body.update(payload)
    out = io.StringIO()
    assert entry.main(event, io.StringIO(json.dumps(body)), out, env) == 0
    return out.getvalue().strip()


def hook_json(env: dict[str, str], event: str, sid: str, **payload: Any) -> dict[str, Any]:
    doc = json.loads(hook(env, event, sid, **payload))
    assert isinstance(doc, dict)
    return doc


def context(doc: dict[str, Any]) -> str | None:
    ctx = doc.get("hookSpecificOutput", {}).get("additionalContext")
    return ctx if isinstance(ctx, str) else None


def fold(proj: Path, sid: str) -> None:
    """What the detached SessionEnd spawn runs."""
    assert cli(["fold", "--session", sid, "--project", str(proj)]) == 0


def alias_session(env: dict[str, str], proj: Path, tmp_path: Path, sid: str) -> None:
    """A session that runs ``pytest`` (not found), then ``python -m pytest`` (works)."""
    tr = (
        Log()
        .prompt("run the tests")
        .bash("pytest tests -q", "zsh: command not found: pytest", err=True)
        .bash("python -m pytest tests -q", "3 passed in 0.01s")
        .write(tmp_path / "transcripts" / f"{sid}.jsonl")
    )
    hook(env, "SessionStart", sid, source="startup", transcript_path=str(tr))
    hook(env, "UserPromptSubmit", sid, prompt="run the tests", transcript_path=str(tr))
    hook(
        env,
        "PostToolUseFailure",
        sid,
        tool_name="Bash",
        tool_input={"command": "pytest tests -q"},
        tool_use_id=f"{sid}-1",
        error="zsh: command not found: pytest",
    )
    hook(
        env,
        "PostToolUse",
        sid,
        tool_name="Bash",
        tool_input={"command": "python -m pytest tests -q"},
        tool_use_id=f"{sid}-2",
        tool_response={"stdout": "3 passed in 0.01s", "stderr": "", "interrupted": False},
    )
    hook(env, "SessionEnd", sid, reason="exit")
    fold(proj, sid)


# --------------------------------------------------------------------------- facts
def test_three_sessions_learn_fact_and_inject(
    env: dict[str, str], proj: Path, tmp_path: Path
) -> None:
    """Under the production activation rules a command_alias needs 3 sessions of evidence
    (bootstrap rule; with 2 the lcb rule needs a third confirmation too), so three learning
    sessions and the fourth session's start shows it. The withhold floor (exploration) is set
    to 0 so the fourth session is not the one that withholds it for its baseline."""
    env["TOKEN_SPAREN_FACTS__WITHHOLD_FLOOR"] = "0"
    for i, sid in enumerate(("s1", "s2", "s3"), start=1):
        alias_session(env, proj, tmp_path, sid)
        doc = miners.load(_pdir(env, proj), env)
        (entry_,) = [e for e in doc["facts"].values() if e["kind"] == "command_alias"]
        assert (entry_["subject"], entry_["value"]) == ("pytest", "python -m pytest")
        assert len(entry_["sessions"]) == i
        assert entry_["status"] == ("active" if i == 3 else "candidate"), i
        if i == 2:  # two sessions are not enough: the next start injects nothing
            assert context(hook_json(env, "SessionStart", "probe", source="startup")) is None
    ctx = context(hook_json(env, "SessionStart", "s4", source="startup"))
    assert ctx is not None
    lines = ctx.splitlines()
    assert lines[0] == inject.OPEN and lines[1] == inject.DATA_LINE and lines[-1] == inject.CLOSE
    assert lines[1].startswith("Observed in earlier sessions in this repository (data, not")
    assert f"- {FACT_LINE}" in lines
    # a resume of the same session does not repeat the block
    assert context(hook_json(env, "SessionStart", "s4", source="resume")) is None


# --------------------------------------------------------------------------- pruning
LISTED = ["Read", "Edit", "Bash", "mcp__alpha__get", "mcp__alpha__put", "mcp__beta__ask"]


def prune_session(env: dict[str, str], proj: Path, tmp_path: Path, sid: str) -> None:
    """A session where ``mcp__alpha`` was available and never used (``beta`` was used)."""
    log = Log()
    log.recs.append({"type": "system", "subtype": "init", "tools": LISTED})
    for i in range(100):
        log.prompt(f"step {i}")
    log.call("mcp__beta__ask", {"q": "x"})
    tr = log.write(tmp_path / "transcripts" / f"{sid}.jsonl")
    hook(env, "SessionStart", sid, source="startup", transcript_path=str(tr))
    hook(env, "UserPromptSubmit", sid, prompt="step", transcript_path=str(tr))
    hook(env, "SessionEnd", sid, reason="exit")


def make_idle(env: dict[str, str], proj: Path) -> None:
    """The project's last activity was two hours ago (the idle gate before a rules write)."""
    key = keycache.cached_project_key(proj, env)
    path = tracking.last_active_path(_home(env), key)
    path.write_text(json.dumps({"session_id": "old", "ts": time.time() - 7200}), "utf-8")


def settings_file(proj: Path) -> Path:
    return proj / ".claude" / "settings.local.json"


def deny(proj: Path) -> list[str]:
    data = json.loads(settings_file(proj).read_text(encoding="utf-8"))
    rules = data["permissions"]["deny"]
    assert isinstance(rules, list)
    return rules


def prune_cycle(env: dict[str, str], proj: Path, tmp_path: Path) -> None:
    """Opt in, three sessions of evidence, idle, fold: ``mcp__alpha`` is denied."""
    assert cli(["prune", "enable", "--project", str(proj)]) == 0
    for sid in ("p1", "p2", "p3"):
        prune_session(env, proj, tmp_path, sid)
        fold(proj, sid)
    assert not settings_file(proj).exists()  # the sessions were live just now: deferred
    doc = json.loads((_pdir(env, proj) / prune.PRUNE_FILE).read_text(encoding="utf-8"))
    assert doc["pending"] is True
    make_idle(env, proj)
    # the next session starts after an idle gap: it spawns the detached `prune apply` (rules
    # are never written inside the latency-capped hook), before its first request
    spawned: list[list[str]] = []
    real = overlay.POPEN
    overlay.POPEN = lambda args, **kw: spawned.append(list(args))  # type: ignore[assignment]
    try:
        hook(env, "SessionStart", "p4", source="startup")
    finally:
        overlay.POPEN = real
    assert not settings_file(proj).exists()
    apply = [a[a.index("prune") :] for a in spawned if "prune" in a and "apply" in a]
    assert apply == [["prune", "apply", "--project", str(proj), "--idle-checked"]]
    assert cli(apply[0]) == 0
    assert deny(proj) == ["mcp__alpha"]


def test_prune_cycle_end_to_end(env: dict[str, str], proj: Path, tmp_path: Path) -> None:
    # without the opt-in nothing is pruned or written, whatever the evidence
    for sid in ("n1", "n2", "n3"):
        prune_session(env, proj, tmp_path, sid)
        make_idle(env, proj)
        fold(proj, sid)
    assert not (proj / ".claude").exists()
    assert not (_pdir(env, proj) / prune.PRUNE_FILE).exists()

    prune_cycle(env, proj, tmp_path)
    doc = json.loads((_pdir(env, proj) / prune.PRUNE_FILE).read_text(encoding="utf-8"))
    assert prune.owned_in(doc, prune.file_key(settings_file(proj))) == ["mcp__alpha"]
    assert "mcp__beta" not in json.dumps(deny(proj))  # used: never a candidate
    # the next session is told which tools were pruned and how to restore them
    ctx = context(hook_json(env, "SessionStart", "p5", source="startup"))
    assert ctx is not None and "mcp__alpha" in ctx and "prune restore" in ctx
    # an attempted use restores it at once and keeps it for good
    out = hook_json(env, "PreToolUse", "p5", tool_name="mcp__alpha__get", tool_input={})
    assert "mcp__alpha" in out["systemMessage"] and "restored" in out["systemMessage"]
    assert "permissionDecision" not in json.dumps(out)  # the call itself is never blocked
    assert not settings_file(proj).exists()  # exact revert: the file we created is gone
    doc = json.loads((_pdir(env, proj) / prune.PRUNE_FILE).read_text(encoding="utf-8"))
    assert prune.owned_in(doc, prune.file_key(settings_file(proj))) == []
    assert not prune.is_candidate("mcp__alpha", doc)


# --------------------------------------------------------------------------- loop guard
def _pytest_failure(failing: list[int]) -> str:
    out = ["Exit code 1", "=" * 20 + " FAILURES " + "=" * 20]
    for i in failing:
        out += [f"_____ test_{i} _____", f">       assert f({i}) == 0"]
        out += [f"E       AssertionError: assert {i} == 0", f"tests/test_m.py:{i}: AssertionError"]
    out += [f"FAILED tests/test_m.py::test_{i} - AssertionError: assert {i} == 0" for i in failing]
    out += [f"===== {len(failing)} failed, 3 passed in 0.02s ====="]
    return "\n".join(out)


@pytest.fixture
def standard_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Engine decides and is rewarded; only its draw at the loopguard point is fixed
    to ``standard`` (k = 3), so the test does not depend on a sampled arm."""
    real = engine_mod.Engine.choose

    def choose(self: engine_mod.Engine, point: Any, features: Any) -> tuple[str, float]:
        if point.name == loopguard.LOOPGUARD.name:
            return "standard", 0.5
        return real(self, point, features)

    monkeypatch.setattr(engine_mod.Engine, "choose", choose)


@pytest.mark.usefixtures("standard_arm")
def test_loopguard_end_to_end(env: dict[str, str], proj: Path) -> None:
    sid = "lg"
    hook(env, "SessionStart", sid, source="startup")
    hook(env, "UserPromptSubmit", sid, prompt="make the tests pass")
    text = _pytest_failure([2, 4])
    outs = []
    for n in range(3):
        outs.append(
            hook_json(
                env,
                "PostToolUseFailure",
                sid,
                tool_name="Bash",
                tool_input={"command": "pytest -q tests/test_m.py"},
                tool_use_id=f"lg-{n}",
                error=text,
            )
        )
    redirects = [context(o) for o in outs]
    assert redirects[:2] == [None, None]
    assert redirects[2] is not None and "this error has repeated 3×" in redirects[2]
    for o in outs:  # the tool output is never replaced, only context is added
        assert "updatedToolOutput" not in json.dumps(o) and "updatedMCPToolOutput" not in o
    pdir = _pdir(env, proj)
    lines = [json.loads(x) for x in (pdir / "decisions.jsonl").read_text("utf-8").splitlines()]
    (dec,) = [r for r in lines if r.get("point") == "loopguard"]
    assert dec["arm"] == "standard"

    # the user's next prompt settles the turn: savings and an explicit regret 0
    hook(env, "UserPromptSubmit", sid, prompt="thanks, now update the docs")
    lines = [json.loads(x) for x in (pdir / "decisions.jsonl").read_text("utf-8").splitlines()]
    (outcome,) = [r for r in lines if r.get("decision_id") == dec["decision_id"] and "kind" in r]
    assert outcome["regret"] == 0.0 and outcome["signal"] == "next_prompt"
    ov = session.session_path(_home(env), keycache.cached_project_key(proj, env), sid)
    pend = json.loads(pending_path(ov).read_text(encoding="utf-8"))
    assert pend["pending"][dec["decision_id"]][-1] == ["regret", "savings"]


# --------------------------------------------------------------------------- kill switch
EVENTS: list[tuple[str, dict[str, Any]]] = [
    ("SessionStart", {"source": "startup"}),
    ("UserPromptSubmit", {"prompt": "please use mcp__alpha__get"}),
    ("PreToolUse", {"tool_name": "mcp__alpha__get", "tool_input": {}}),
    ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}),
    *[
        (
            "PostToolUseFailure",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "pytest -q"},
                "tool_use_id": f"k{n}",
                "error": _pytest_failure([1]),
            },
        )
        for n in range(5)
    ],
    (
        "PostToolUse",
        {
            "tool_name": "ToolSearch",
            "tool_input": {"query": "mcp__alpha"},
            "tool_response": "mcp__alpha__get",
        },
    ),
    (
        "PostToolUse",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat big.log"},
            "tool_response": {"stdout": "line\n" * 20000, "stderr": "", "interrupted": False},
        },
    ),
    ("PermissionDenied", {"tool_name": "mcp__alpha__get", "tool_input": {}}),
    ("PreCompact", {"trigger": "auto"}),
    ("SessionEnd", {"reason": "exit"}),
]


def _snapshot(*roots: Path) -> dict[str, bytes]:
    return {
        str(p): p.read_bytes()
        for r in roots
        if r.exists()
        for p in sorted(r.rglob("*"))
        if p.is_file()
    }


@pytest.mark.usefixtures("standard_arm")
def test_kill_switch_disables_everything(env: dict[str, str], proj: Path, tmp_path: Path) -> None:
    env["TOKEN_SPAREN_FACTS__WITHHOLD_FLOOR"] = "0"
    for sid in ("s1", "s2", "s3"):
        alias_session(env, proj, tmp_path, sid)
    prune_cycle(env, proj, tmp_path)
    assert context(hook_json(env, "SessionStart", "check", source="startup")) is not None
    before = _snapshot(_home(env), proj)

    env["TOKEN_SPAREN_DISABLE"] = "1"
    for event, payload in EVENTS:
        assert hook(env, event, "off", **payload) == "{}", event
    assert _snapshot(_home(env), proj) == before  # no state, settings or output writes
    assert deny(proj) == ["mcp__alpha"]  # attempted use did not restore while disabled
