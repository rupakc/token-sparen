"""Opt-in learned tool pruning with in-session recovery (spec section 6.10).

Every test runs in a temporary state root and a temporary project; nothing touches a real
``~/.claude`` or a real project.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from token_sparen.adapters.claude import entry, settings
from token_sparen.cli import main
from token_sparen.config import load_config, policy_for
from token_sparen.estimate import estimate_tokens
from token_sparen.learn import overlay
from token_sparen.learn.beta import Beta
from token_sparen.mechanisms import prune, tracking
from token_sparen.store import keycache, session

BUILTINS = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "WebFetch", "WebSearch"]


# --------------------------------------------------------------------------- helpers
def _git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git without the user's global config or excludes file."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    for k in list(os.environ):
        if k.startswith("GIT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))


def _git_checkout(root: Path, ignored: bool = True) -> Path:
    """A git work tree; with ``ignored`` its ``.gitignore`` covers the settings file (pruning
    writes a settings file only once git confirms it is untracked and ignored)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    if ignored:
        (root / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    return root


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in list(os.environ):
        if k.startswith(("TOKEN_SPAREN_", "CLAUDE_")):
            monkeypatch.delenv(k)
    _git_env(tmp_path, monkeypatch)
    return _git_checkout(tmp_path / "proj")


@pytest.fixture
def plain_env(tmp_path: Path, proj: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Temporary state root and Claude config dir (never the real ``~/.claude``)."""
    e = {
        "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(proj),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }
    for k in ("TOKEN_SPAREN_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(k, e[k])
    monkeypatch.chdir(proj)
    return e


def _user_config(env: dict[str, str], text: str) -> None:
    home = _home(env)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def env(plain_env: dict[str, str]) -> dict[str, str]:
    """As ``plain_env`` with built-in pruning switched on in the user's own config (most tests
    use built-ins listed by an SDK init record; the default scope is tested separately)."""
    _user_config(plain_env, "[prune]\nbuiltins = true\n")
    return plain_env


def _home(env: dict[str, str]) -> Path:
    return Path(env["TOKEN_SPAREN_HOME"])


def _key(env: dict[str, str], proj: Path) -> str:
    return keycache.cached_project_key(proj, env)


def _pdir(env: dict[str, str], proj: Path) -> Path:
    return _home(env) / "projects" / _key(env, proj)


def _transcript(
    path: Path,
    *,
    listed: list[str] | None,
    used: Iterable[str] = (),
    prompts: int = 100,
    unknown: Iterable[str] = (),
    deferred: bool = False,
) -> Path:
    recs: list[dict[str, Any]] = []
    if listed is not None and not deferred:
        recs.append({"type": "system", "subtype": "init", "tools": listed})
    if listed is not None and deferred:
        att = {"type": "deferred_tools_delta", "addedNames": listed, "removedNames": []}
        recs.append({"type": "attachment", "attachment": att})
    for i in range(prompts):
        recs.append({"type": "user", "message": {"role": "user", "content": f"step {i}"}})
    n = 0
    for name, err in [*((u, None) for u in used), *((u, "unknown") for u in unknown)]:
        n += 1
        use = {"type": "tool_use", "id": f"t{n}", "name": name, "input": {}}
        recs.append({"type": "assistant", "message": {"id": f"m{n}", "content": [use]}})
        text = "ok" if err is None else f"<tool_use_error>No such tool available: {name}"
        res = {"type": "tool_result", "tool_use_id": f"t{n}", "content": text}
        if err is not None:
            res["is_error"] = True
        recs.append({"type": "user", "message": {"content": [res]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return path


def _fold(
    env: dict[str, str],
    proj: Path,
    sid: str,
    *,
    listed: list[str] | None = BUILTINS,
    used: Iterable[str] = (),
    prompts: int = 100,
    unknown: Iterable[str] = (),
    deferred: bool = False,
) -> overlay.FoldReport:
    tr = _transcript(
        _home(env).parent / "transcripts" / f"{sid}.jsonl",
        listed=listed,
        used=used,
        prompts=prompts,
        unknown=unknown,
        deferred=deferred,
    )
    key = _key(env, proj)
    ov = session.session_path(_home(env), key, sid)
    ov.parent.mkdir(parents=True, exist_ok=True)
    doc = {"evidence": {}, "transcript": str(tr), "project_dir": str(proj)}
    ov.write_text(json.dumps(doc), encoding="utf-8")
    params = load_config(proj, env).data
    # what the SessionEnd-spawned fold runs: the only fold that writes new deny rules
    return overlay.fold(sid, key, _home(env), params=params, prune_apply=True)


def _enable(env: dict[str, str], proj: Path) -> None:
    assert main(["prune", "enable", "--project", str(proj)]) == 0


def _local(proj: Path) -> Path:
    return proj / ".claude" / "settings.local.json"


def _deny(proj: Path) -> list[str]:
    data = json.loads(_local(proj).read_text(encoding="utf-8"))
    deny = data.get("permissions", {}).get("deny", [])
    assert isinstance(deny, list)
    return deny


def _doc(env: dict[str, str], proj: Path) -> dict[str, Any]:
    """``prune.json`` with this project's per-checkout records from the user-level registry."""
    path = _pdir(env, proj) / prune.PRUNE_FILE
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    prune.attach(doc, _reg(env), _key(env, proj), None)
    return doc


def _reg(env: dict[str, str]) -> dict[str, Any]:
    path = prune.registry_path(_home(env))
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _owned(env: dict[str, str], proj: Path) -> list[str]:
    return prune.owned_in(_doc(env, proj), prune.file_key(_local(proj)))


def _three(env: dict[str, str], proj: Path, **kw: Any) -> None:
    for sid in ("s1", "s2", "s3"):
        _fold(env, proj, sid, **kw)


def _hook(env: dict[str, str], event: str, payload: dict[str, Any]) -> dict[str, Any]:
    out = io.StringIO()
    body = {"session_id": "live", **payload}
    assert entry.main(event, io.StringIO(json.dumps(body)), out, env) == 0
    doc = json.loads(out.getvalue())
    assert isinstance(doc, dict)
    return doc


def _make_idle(env: dict[str, str], proj: Path, age_s: float = 2 * 3600) -> None:
    path = tracking.last_active_path(_home(env), _key(env, proj))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": "old", "ts": time.time() - age_s}), "utf-8")


def _make_busy(env: dict[str, str], proj: Path) -> None:
    _make_idle(env, proj, age_s=60)


class _Spawned:
    calls: list[list[str]] = []

    def __init__(self, args: list[str], **kw: Any) -> None:
        _Spawned.calls.append(list(args))


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> type[_Spawned]:
    _Spawned.calls = []
    monkeypatch.setattr(overlay, "POPEN", _Spawned)
    return _Spawned


# --------------------------------------------------------------------------- the brief's seven
def test_disabled_by_default(env: dict[str, str], proj: Path) -> None:
    cfg = load_config(proj, env)
    assert cfg.get("prune.enabled") is False
    _three(env, proj)
    assert not _local(proj).exists()
    assert not (proj / ".claude").exists()
    assert not (_pdir(env, proj) / prune.PRUNE_FILE).exists()  # no evidence work either
    ctx = _hook(env, "SessionStart", {"source": "startup"})
    assert "Pruned" not in json.dumps(ctx)


def test_protected_never_pruned(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    listed = [
        *BUILTINS,
        "Agent",
        "Task",
        "TaskCreate",
        "TaskUpdate",
        "TodoWrite",
        "Skill",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
        "AskUserQuestion",
        "SendMessage",
        "NotebookEdit",
    ]
    _fold(env, proj, "s0", listed=listed, used=["NotebookEdit"], prompts=1)  # used once, ever
    _three(env, proj, listed=listed)
    deny = _deny(proj)
    assert deny == ["WebFetch", "WebSearch"]
    for name in listed:
        if name not in ("WebFetch", "WebSearch"):
            assert not prune.is_candidate(name, _doc(env, proj)), name
    assert "NotebookEdit" in _doc(env, proj)["used"]


def test_requires_evidence(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _fold(env, proj, "a1")
    _fold(env, proj, "a2")
    assert not _local(proj).exists()  # two sessions: too few, whatever the prompts
    doc = _doc(env, proj)
    assert doc["tools"]["WebFetch"]["s"] == 2 and doc["tools"]["WebFetch"]["p"] == 200
    # three sessions but few prompts: under min_prompts (15)
    for sid in ("b1", "b2", "b3"):
        _fold(env, proj, sid, listed=["Read", "NotebookEdit"], prompts=4)
    assert not _local(proj).exists()
    # 3 x 10 prompts clears min_prompts, but the high-tier ucb gate (tau 0.01) does not
    for sid in ("c1", "c2", "c3"):
        _fold(env, proj, sid, listed=["Read", "LSP"], prompts=10)
    assert not _local(proj).exists()
    lsp = Beta.from_list(_doc(env, proj)["tools"]["LSP"]["r"])
    assert lsp.ucb(0.95) > 0.01
    _fold(env, proj, "a3")  # WebFetch/WebSearch: 3 sessions, 300 prompts -> ucb < 0.01
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    assert Beta.from_list(_doc(env, proj)["tools"]["WebFetch"]["r"]).ucb(0.95) <= 0.01


def test_idle_gate_defers(env: dict[str, str], proj: Path, spawned: type[_Spawned]) -> None:
    _enable(env, proj)
    _make_busy(env, proj)  # another session of the project was active < 60 min ago
    _three(env, proj)
    assert not _local(proj).exists()  # deferred
    assert _doc(env, proj)["pending"] is True
    # a session start never writes rules and spawns no writer, idle gap or not
    _make_idle(env, proj)
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert not _local(proj).exists() and "Pruned" not in json.dumps(out)
    assert not [c for c in spawned.calls if "prune" in c]
    _hook(env, "SessionEnd", {"reason": "exit"})
    # the next session-end fold (no other session active) writes them: effective next session
    _fold(env, proj, "s4")
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    out = _hook(env, "SessionStart", {"source": "startup"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert (
        "Pruned (unused here): WebFetch, WebSearch — run "
        "`token-sparen prune restore <tool>` if needed." in ctx
    )
    assert _doc(env, proj)["pending"] is False


def test_idle_gate_defers_at_session_start_too(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _make_busy(env, proj)
    _three(env, proj)
    _make_busy(env, proj)  # another session of the project is live
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert not _local(proj).exists()
    assert "Pruned" not in json.dumps(out)
    _make_idle(env, proj)
    _fold(env, proj, "s4")  # the next fold applies once the project is idle
    assert _deny(proj) == ["WebFetch", "WebSearch"]


def test_writes_only_owned_rules_and_backup(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    original = {
        "permissions": {"allow": ["Read"], "deny": ["Bash(rm *)", "WebSearch"]},
        "env": {"X": "1"},
    }
    _local(proj).parent.mkdir()
    raw = json.dumps(original, indent=4) + "\n"
    _local(proj).write_text(raw, encoding="utf-8")
    _three(env, proj)
    data = json.loads(_local(proj).read_text(encoding="utf-8"))
    assert data["permissions"]["deny"] == ["Bash(rm *)", "WebSearch", "WebFetch"]
    assert data["permissions"]["allow"] == ["Read"] and data["env"] == {"X": "1"}
    assert _owned(env, proj) == ["WebFetch"]  # WebSearch was the user's rule
    backup = _local(proj).with_name("settings.local.json.token-sparen.bak")
    assert backup.read_text(encoding="utf-8") == raw
    # the user edits the file; a later prune never overwrites the pristine backup
    data["permissions"]["deny"].append("Bash(curl *)")
    _local(proj).write_text(json.dumps(data), encoding="utf-8")
    for sid in ("t1", "t2", "t3"):
        _fold(env, proj, sid, listed=["Read", "NotebookEdit"])
    assert _deny(proj)[-1] == "NotebookEdit"
    assert backup.read_text(encoding="utf-8") == raw
    # exact revert of our rules only: the user's own rules (also later ones) stay
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    after = json.loads(_local(proj).read_text(encoding="utf-8"))
    assert after["permissions"]["deny"] == ["Bash(rm *)", "WebSearch", "Bash(curl *)"]
    assert after["env"] == {"X": "1"}
    assert _owned(env, proj) == []


def test_restore_on_attempted_use(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    before = Beta.from_list(_doc(env, proj)["tools"]["WebFetch"]["r"])
    out = _hook(env, "PreToolUse", {"tool_name": "WebFetch", "tool_input": {"url": "x"}})
    assert _deny(proj) == ["WebSearch"]  # removed immediately
    assert "WebFetch" in out["systemMessage"] and "restored" in out["systemMessage"]
    assert "hookSpecificOutput" not in out or "permissionDecision" not in json.dumps(out)
    doc = _doc(env, proj)
    after = Beta.from_list(doc["tools"]["WebFetch"]["r"])
    assert after.a == pytest.approx(before.a + 1.0)  # regret 1.0
    assert "WebFetch" not in _owned(env, proj) and "WebFetch" in doc["kept"]
    assert not prune.is_candidate("WebFetch", doc)  # never pruned again
    _three(env, proj)
    assert "WebFetch" not in _deny(proj)


def test_restore_cli_exact_revert(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    assert not (proj / ".claude").exists()
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    assert main(["prune", "restore", "WebSearch", "--project", str(proj)]) == 0
    assert _deny(proj) == ["WebFetch"]
    assert "WebSearch" in _doc(env, proj)["kept"]
    assert main(["prune", "restore", "Nope", "--project", str(proj)]) == 1
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    # the file and the directory did not exist before: both are gone again
    assert not _local(proj).exists() and not (proj / ".claude").exists()
    assert _owned(env, proj) == []


# --------------------------------------------------------------------------- more behaviour
def test_project_config_cannot_enable(env: dict[str, str], proj: Path) -> None:
    (proj / "token-sparen.toml").write_text(
        "[prune]\nenabled = true\nmin_sessions = 1\ntau = 0.9\n", encoding="utf-8"
    )
    cfg = load_config(proj, env)
    assert cfg.get("prune.enabled") is False
    assert cfg.get("prune.min_sessions") == 3 and cfg.get("prune.tau") == 0.01
    assert policy_for(("prune", "enabled")) == "user_only"
    assert policy_for(("prune", "allow")) == "false_only"
    _three(env, proj)
    assert not _local(proj).exists()


def test_user_config_enables_and_project_may_disable(env: dict[str, str], proj: Path) -> None:
    _user_config(env, "[prune]\nenabled = true\nbuiltins = true\n")
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    # the project switches pruning off: the next fold reverts every rule we own
    (proj / "token-sparen.toml").write_text("[prune]\nallow = false\n", encoding="utf-8")
    _fold(env, proj, "s4")
    assert not _local(proj).exists()


def test_env_cannot_enable_or_widen(
    plain_env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a repository's committed Claude Code settings can set `env`: never enough to opt in
    for k, v in (("TOKEN_SPAREN_PRUNE__ENABLED", "1"), ("TOKEN_SPAREN_PRUNE__BUILTINS", "1")):
        monkeypatch.setenv(k, v)
        plain_env[k] = v
    _three(plain_env, proj, listed=[*BUILTINS, "mcp__alpha__get"])
    assert not _local(proj).exists()
    _enable(plain_env, proj)
    _three(plain_env, proj, listed=[*BUILTINS, "mcp__alpha__get"])
    assert _deny(proj) == ["mcp__alpha"]  # built-ins need `builtins = true` in config.toml


def test_default_scope_is_mcp_servers_only(plain_env: dict[str, str], proj: Path) -> None:
    _enable(plain_env, proj)
    listed = [*BUILTINS, "mcp__alpha__get", "mcp__alpha__put"]
    _three(plain_env, proj, listed=listed)
    assert _deny(proj) == ["mcp__alpha"]
    assert "WebFetch" not in _doc(plain_env, proj)["tools"]


def test_builtins_only_when_always_loaded(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    # listed only as deferred tools (interactive Claude Code): ~5 tokens each, never pruned
    _three(env, proj, listed=["Read", "WebFetch", "mcp__alpha__get"], deferred=True)
    assert _deny(proj) == ["mcp__alpha"]


def test_mcp_server_level_candidates(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    listed = ["Read", "mcp__alpha__get", "mcp__alpha__put", "mcp__beta__ask", "mcp__beta__tell"]
    _fold(env, proj, "s0", listed=listed, used=["mcp__beta__ask"], prompts=1)
    _three(env, proj, listed=listed)
    assert _deny(proj) == ["mcp__alpha"]  # a whole unused server, never single tools
    doc = _doc(env, proj)
    assert set(doc["tools"]) == {"mcp__alpha"}
    assert not prune.is_candidate("mcp__beta", doc)
    out = _hook(env, "PreToolUse", {"tool_name": "mcp__alpha__get", "tool_input": {}})
    assert "mcp__alpha" in out["systemMessage"]
    assert not _local(proj).exists()


def test_deferred_tools_listing_and_fallback(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1", listed=["Read", "WebFetch", "mcp__alpha__get"], deferred=True)
    # sessions without a tool list count for the tools other sessions listed
    _fold(env, proj, "s2", listed=None)
    _fold(env, proj, "s3", listed=None)
    assert _deny(proj) == ["mcp__alpha"]  # a deferred built-in is never a candidate


def test_mtime_race_retries_then_gives_up(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(env, proj)
    _local(proj).parent.mkdir()
    _local(proj).write_text('{"permissions": {"deny": []}}', encoding="utf-8")
    calls = {"n": 0}

    def once(path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the user saves the file between our read and our replace
            path.write_text('{"permissions": {"deny": ["Bash(sudo *)"]}}', encoding="utf-8")

    monkeypatch.setattr(settings, "_before_replace", once)
    _three(env, proj)
    assert _deny(proj) == ["Bash(sudo *)", "WebFetch", "WebSearch"]  # re-read, edit kept

    def always(path: Path) -> None:
        # a genuine edit every time (a distinct key): a byte-identical rewrite loses no user
        # edit, so the content-digest stamp rightly treats it as unchanged
        calls["n"] += 1
        data = json.loads(path.read_text(encoding="utf-8"))
        data[f"edit{calls['n']}"] = calls["n"]
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(settings, "_before_replace", always)
    for sid in ("t1", "t2", "t3"):
        _fold(env, proj, sid, listed=["Read", "NotebookEdit"])
    assert "NotebookEdit" not in _deny(proj)
    assert "NotebookEdit" not in _owned(env, proj)  # never owned what we did not write


def test_invalid_settings_file_is_never_touched(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _local(proj).parent.mkdir()
    _local(proj).write_text("{not json", encoding="utf-8")
    _three(env, proj)
    assert _local(proj).read_text(encoding="utf-8") == "{not json"
    assert _owned(env, proj) == []


@pytest.fixture
def plain_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git without the user's global config or excludes file."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))


def test_unignored_and_tracked_files_are_refused(env: dict[str, str], proj: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)

    (proj / ".gitignore").unlink()
    _enable(env, proj)
    _three(env, proj)
    assert not _local(proj).exists()  # not covered by .gitignore: never written
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert ".gitignore" in out["systemMessage"] and "did not write" in out["systemMessage"]
    assert not (proj / ".gitignore").exists()  # we tell, we never edit it
    # a tracked settings.local.json is never written (our rules would be committed)
    (proj / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    _local(proj).parent.mkdir(exist_ok=True)
    _local(proj).write_text("{}\n", encoding="utf-8")
    git("add", "-f", ".claude/settings.local.json")
    _make_idle(env, proj)
    for sid in ("t1", "t2", "t3"):
        _fold(env, proj, sid, listed=["Read", "NotebookEdit"])
    assert _local(proj).read_text(encoding="utf-8") == "{}\n"
    assert "tracked" in _hook(env, "SessionStart", {"source": "startup"})["systemMessage"]


@pytest.mark.usefixtures("plain_git")
def test_ignored_settings_file_gives_no_warning(env: dict[str, str], proj: Path) -> None:
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
    (proj / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    _enable(env, proj)
    _three(env, proj)
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert ".gitignore" not in out.get("systemMessage", "")


def test_restore_on_user_mention(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    out = _hook(env, "UserPromptSubmit", {"prompt": "please do a web search for the docs"})
    assert _deny(proj) == ["WebFetch"]
    assert "WebSearch" in out["systemMessage"]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "WebSearch" in ctx and "restored" in ctx
    out = _hook(env, "UserPromptSubmit", {"prompt": "unrelated question about the code"})
    assert _deny(proj) == ["WebFetch"] and "systemMessage" not in out


def test_restore_on_tool_search_and_failures(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    out = _hook(
        env,
        "PostToolUse",
        {
            "tool_name": "ToolSearch",
            "tool_input": {"query": "select:WebFetch"},
            "tool_response": "",
        },
    )
    assert _deny(proj) == ["WebSearch"]
    assert "WebFetch" in out["hookSpecificOutput"]["additionalContext"]
    _hook(env, "PostToolUseFailure", {"tool_name": "WebSearch", "tool_input": {}, "error": "x"})
    assert not _local(proj).exists()  # the last rule of ours: exact revert


def test_restore_on_permission_denied(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    out = _hook(env, "PermissionDenied", {"tool_name": "WebFetch", "tool_input": {}})
    assert _deny(proj) == ["WebSearch"] and "WebFetch" in out["systemMessage"]


def test_fold_restores_after_an_attempt_seen_in_the_transcript(
    env: dict[str, str], proj: Path
) -> None:
    _enable(env, proj)
    _three(env, proj)
    listed = [n for n in BUILTINS if n not in ("WebFetch", "WebSearch")]
    _fold(env, proj, "s4", listed=listed, unknown=["WebFetch"], prompts=3)
    assert _deny(proj) == ["WebSearch"]
    assert "WebFetch" in _doc(env, proj)["kept"]


def test_rule_removed_by_the_user_counts_as_a_restore(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    _local(proj).write_text('{"permissions": {"deny": ["WebSearch"]}}', encoding="utf-8")
    _fold(env, proj, "s4")
    doc = _doc(env, proj)
    assert _owned(env, proj) == ["WebSearch"] and "WebFetch" in doc["kept"]
    assert _deny(proj) == ["WebSearch"]


def test_disable_restores_everything_and_stops(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    assert main(["prune", "disable", "--project", str(proj)]) == 0
    assert not _local(proj).exists()
    _three(env, proj)
    assert not _local(proj).exists()
    assert _doc(env, proj)["enabled"] is False


def test_status_cli(env: dict[str, str], proj: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["prune", "status", "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "disabled" in out and "prune enable" in out
    _enable(env, proj)
    out = capsys.readouterr().out
    assert "settings.local.json" in out and "opt-in" in out
    _fold(env, proj, "s1")
    assert main(["prune", "status", "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "enabled" in out and "WebFetch" in out and "1 session" in out
    _fold(env, proj, "s2")
    _fold(env, proj, "s3")
    assert main(["prune", "status", "--project", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "pruned (2): WebFetch, WebSearch" in out
    assert "cannot measure" in out  # savings are not estimated from hooks


def test_session_start_notice_and_stats(
    env: dict[str, str], proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable(env, proj)
    _three(env, proj)
    out = _hook(env, "SessionStart", {"source": "startup"})
    line = (
        "Pruned (unused here): WebFetch, WebSearch — run "
        "`token-sparen prune restore <tool>` if needed."
    )
    assert out["hookSpecificOutput"]["additionalContext"].endswith(line)
    assert line in out["systemMessage"]
    assert main(["stats", "--project", str(proj)]) == 0
    assert "pruned tools: 2" in capsys.readouterr().out


def test_hooks_are_cheap_without_pruning(env: dict[str, str], proj: Path) -> None:
    out = _hook(env, "PreToolUse", {"tool_name": "WebFetch", "tool_input": {}})
    assert out == {} or "systemMessage" not in out
    assert not (_pdir(env, proj) / prune.PRUNE_FILE).exists()


def test_candidate_names_and_matching() -> None:
    names = {"Read", "WebFetch", "mcp__srv__a", "mcp__srv__b", "mcp__x_y_z__q", "mcp__bad"}
    assert prune.candidate_names(names) == {"Read", "WebFetch", "mcp__srv", "mcp__x_y_z"}
    assert prune.server_of("mcp__srv__a") == "mcp__srv"
    assert prune.server_of("WebFetch") is None
    assert prune.mentions("use webfetch please", ["WebFetch", "WebSearch"]) == ["WebFetch"]
    assert prune.mentions("a Web-Search now", ["WebSearch"]) == ["WebSearch"]
    gmail = ["mcp__claude_ai_Gmail"]
    assert prune.mentions("open my gmail", gmail) == []  # a single common word never restores
    assert prune.mentions("use claude ai gmail", gmail) == gmail
    assert prune.mentions("the mcp__claude_ai_Gmail server", gmail) == gmail
    assert prune.mentions("check github", ["mcp__github"]) == []
    assert prune.mentions("use mcp__github", ["mcp__github"]) == ["mcp__github"]
    assert prune.mentions("monitor the build", ["Monitor"]) == []
    assert prune.mentions("use the Monitor tool", ["Monitor"]) == ["Monitor"]
    assert prune.mentions("select:mcp__srv__a", ["mcp__srv"]) == ["mcp__srv"]
    assert prune.mentions("webfetcher", ["WebFetch"]) == []
    assert prune.mentions("the ai is fine", ["mcp__claude_ai"]) == []  # short words never match


# --------------------------------------------------------------------------- writer edges
def test_writer_refuses_bad_shapes_and_handles_missing_files(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    assert settings.remove_deny_rules(path, ["X"], {}, True).skipped == ["X"]  # no file
    assert settings.deny_rules(path) == set()
    path.parent.mkdir()
    for bad in ('{"permissions": []}', '{"permissions": {"deny": "WebFetch"}}', "[1]"):
        path.write_text(bad, encoding="utf-8")
        assert not settings.add_deny_rules(path, ["WebFetch"]).ok
        assert not settings.remove_deny_rules(path, ["WebFetch"], {}, False).ok
        assert settings.deny_rules(path) is None
        assert path.read_text(encoding="utf-8") == bad
    path.write_text('{"permissions": {"deny": ["A"]}}', encoding="utf-8")
    res = settings.add_deny_rules(path, ["A", "B", "B"])
    assert res.ok and res.changed == ["B"] and res.skipped == ["A"]
    assert res.created == {"dir": False, "file": False, "permissions": False, "deny": False}
    assert settings.add_deny_rules(path, ["A", "B"]).changed == []  # nothing new: no write
    backup = path.with_name(path.name + settings.BACKUP_SUFFIX)
    assert backup.exists()
    res = settings.remove_deny_rules(path, ["B", "Z"], {}, True)
    assert res.changed == ["B"] and res.skipped == ["Z"]
    assert not backup.exists()  # the file equals its backup again: the backup is redundant
    assert settings.remove_deny_rules(path, ["Z"], {}, False).changed == []


def test_writer_keeps_a_created_file_the_user_added_to(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.local.json"
    res = settings.add_deny_rules(path, ["WebFetch"])
    assert res.created == {"dir": True, "file": True, "permissions": True, "deny": True}
    data = json.loads(path.read_text(encoding="utf-8"))
    data["env"] = {"A": "1"}
    path.write_text(json.dumps(data), encoding="utf-8")
    settings.remove_deny_rules(path, ["WebFetch"], res.created, True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"env": {"A": "1"}}


def test_subagent_uses_protect_a_tool(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    for sid in ("s1", "s2", "s3"):
        main_tr = _home(env).parent / "transcripts" / f"{sid}.jsonl"
        _transcript(
            main_tr.with_suffix("") / "subagents" / "agent-1.jsonl",
            listed=None,
            used=["WebSearch"],
            prompts=0,
        )
        _fold(env, proj, sid)
    assert _deny(proj) == ["WebFetch"]


def test_tool_inventory_reads_listings(tmp_path: Path) -> None:
    from token_sparen.transcript import tool_inventory

    recs = [
        {"type": "system", "subtype": "init", "tools": ["Read", {"name": "WebFetch"}, 3, ""]},
        {
            "type": "attachment",
            "attachment": {
                "type": "deferred_tools_delta",
                "addedNames": ["mcp__a__b"],
                "removedNames": ["WebFetch"],
            },
        },
        {"type": "attachment", "attachment": {"type": "other"}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    inv = tool_inventory(path)
    assert inv.listed == {"Read", "mcp__a__b"} and inv.prompts == 0
    empty = tmp_path / "none.jsonl"
    empty.write_text("", encoding="utf-8")
    assert tool_inventory(empty).listed is None


def test_restore_cli_maps_a_tool_to_its_server(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj, listed=["Read", "mcp__alpha__get", "mcp__beta__ask"])
    assert _deny(proj) == ["mcp__alpha", "mcp__beta"]
    assert main(["prune", "restore", "mcp__alpha__get", "--project", str(proj)]) == 0
    assert _deny(proj) == ["mcp__beta"]


@pytest.mark.parametrize(
    "bad", ["{not json", '{"a": 1, "a": 2}', '{"permissions": {"deny": [], "deny": ["X"]}}']
)
def test_refused_settings_file_gives_a_notice(env: dict[str, str], proj: Path, bad: str) -> None:
    _enable(env, proj)
    _local(proj).parent.mkdir()
    _local(proj).write_text(bad, encoding="utf-8")
    _three(env, proj)
    assert _local(proj).read_text(encoding="utf-8") == bad
    msg = _hook(env, "SessionStart", {"source": "startup"})["systemMessage"]
    assert "could not write" in msg and "settings.local.json" in msg


def test_notice_names_the_plugin_launcher(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    root = str(proj.parent / "plugin root")
    out = _hook({**env, "CLAUDE_PLUGIN_ROOT": root}, "SessionStart", {"source": "startup"})
    cmd = f'python3 -I "{root}/hooks/run_cli.py" prune restore <tool>'
    assert f"run `{cmd}` if needed." in out["hookSpecificOutput"]["additionalContext"]
    assert cmd in out["systemMessage"]


def test_notice_is_truncated_not_dropped(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    servers = [f"mcp__plugin_some-vendor_server{i:02d}__tool" for i in range(28)]
    _three(env, proj, listed=["Read", *servers])
    assert len(_deny(proj)) == 28
    out = _hook(env, "SessionStart", {"source": "startup"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Pruned (unused here): mcp__plugin_some-vendor_server00, ")
    assert "more — run `token-sparen prune status`" in ctx
    assert estimate_tokens(ctx) <= prune.NOTICE_TOKENS


def test_enable_seeds_tools_used_in_earlier_sessions(
    plain_env: dict[str, str], proj: Path, tmp_path: Path
) -> None:
    hist = tmp_path / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(proj))
    _transcript(hist / "old.jsonl", listed=None, used=["mcp__alpha__get"], prompts=1)
    stale = _transcript(hist / "older.jsonl", listed=None, used=["mcp__beta__ask"], prompts=1)
    old = time.time() - 90 * 86400  # outside the 60-day window
    os.utime(stale, (old, old))
    _enable(plain_env, proj)
    used = _doc(plain_env, proj)["used"]
    assert "mcp__alpha__get" in used and "mcp__beta__ask" not in used
    _three(plain_env, proj, listed=["Read", "mcp__alpha__get", "mcp__beta__ask"])
    assert _deny(proj) == ["mcp__beta"]  # alpha was used before pruning was enabled


def test_first_fold_after_config_enable_seeds_history(env: dict[str, str], proj: Path) -> None:
    _user_config(env, "[prune]\nenabled = true\n")
    _transcript(
        _home(env).parent / "transcripts" / "earlier.jsonl",
        listed=None,
        used=["mcp__alpha__put"],
        prompts=1,
    )
    _three(env, proj, listed=["Read", "mcp__alpha__get", "mcp__beta__ask"])
    assert _deny(proj) == ["mcp__beta"]
    assert _doc(env, proj)["seeded"] is True


def test_tool_calls_count_as_activity(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _make_idle(env, proj)
    _hook(env, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "make"}})
    path = tracking.last_active_path(_home(env), _key(env, proj))
    first = json.loads(path.read_text(encoding="utf-8"))["ts"]
    assert time.time() - first < 5
    _hook(env, "PostToolUse", {"tool_name": "Bash", "tool_input": {}, "tool_response": "ok"})
    assert json.loads(path.read_text(encoding="utf-8"))["ts"] == first  # throttled (1/min)
    _three(env, proj)
    assert not _local(proj).exists()  # a long autonomous turn is activity: deferred
    assert _doc(env, proj)["pending"] is True


@pytest.mark.usefixtures("plain_git")
def test_sibling_checkouts_and_worktree_keep_their_own_rules(
    env: dict[str, str], tmp_path: Path
) -> None:
    def git(where: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True)

    a, b = tmp_path / "clone-a", tmp_path / "clone-b"
    for d in (a, b):
        d.mkdir()
        git(d, "init", "-q")
        git(d, "remote", "add", "origin", "https://example.invalid/team/repo.git")
        (d / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    git(a, "add", ".gitignore")
    git(a, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    wt = tmp_path / "wt"
    git(a, "worktree", "add", "-q", str(wt))
    assert _key(env, a) == _key(env, b) == _key(env, wt)  # one project key
    _enable(env, a)
    for d, tag in ((a, "a"), (b, "b"), (wt, "w")):
        for i in range(3):
            _fold(env, d, f"{tag}{i}")
        assert _deny(d) == ["WebFetch", "WebSearch"], d
    doc = _doc(env, a)
    assert len(doc["files"]) == 3
    # a restore in one checkout touches only that checkout's file
    out = _hook({**env, "CLAUDE_PROJECT_DIR": str(b)}, "PreToolUse", {"tool_name": "WebFetch"})
    assert "WebFetch" in out["systemMessage"]
    assert _deny(b) == ["WebSearch"] and _deny(a) == ["WebFetch", "WebSearch"]
    # restore --all and disable revert every recorded file of the project key
    assert main(["prune", "disable", "--project", str(a)]) == 0
    for d in (a, b, wt):
        assert not _local(d).exists(), d


# --------------------------------------------------------------------------- fix round 2
def test_no_history_found_keeps_unseeded_and_the_fold_seeds(
    plain_env: dict[str, str], proj: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # the Claude config folder name does not match (P4b): enable finds nothing to seed from
    _transcript(
        _home(plain_env).parent / "transcripts" / "earlier.jsonl",
        listed=None,
        used=["mcp__figma__get_design"],
        prompts=2,
    )
    _enable(plain_env, proj)
    out = capsys.readouterr().out
    assert "found 0 earlier sessions" in out
    assert "pruning waits until a session has been recorded" in out
    assert _doc(plain_env, proj).get("seeded") is not True
    # the first fold seeds from its own transcript's folder: figma was used, never pruned
    _three(plain_env, proj, listed=["Read", "mcp__figma__get_design", "mcp__beta__ask"])
    assert _deny(proj) == ["mcp__beta"]
    assert _doc(plain_env, proj)["seeded"] is True


def test_enable_reports_sessions_found(
    plain_env: dict[str, str], proj: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hist = tmp_path / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(proj))
    for i in range(2):
        _transcript(hist / f"h{i}.jsonl", listed=None, used=["Read"], prompts=1)
    _enable(plain_env, proj)
    assert "found 2 earlier sessions" in capsys.readouterr().out
    assert _doc(plain_env, proj)["seeded"] is True


def test_nothing_is_pruned_before_seeding_succeeded(env: dict[str, str]) -> None:
    s = prune.settings({}, {"prune": {"builtins": True}})
    tools = {"mcp__a": {"s": 9, "p": 900, "r": [1.0, 901.0, 900.0]}}
    assert prune.plan({"tools": tools}, s, "f") == []
    assert prune.plan({"tools": tools, "seeded": True}, s, "f") == ["mcp__a"]


@pytest.mark.usefixtures("plain_git")
def test_cli_resolves_the_project_from_a_subdirectory(
    env: dict[str, str],
    proj: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
    _enable(env, proj)
    _three(env, proj, listed=["Read", "mcp__alpha__get", "mcp__beta__ask"])
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    capsys.readouterr()
    assert main(["prune", "status"]) == 0
    assert "pruned (2): mcp__alpha, mcp__beta" in capsys.readouterr().out
    assert main(["prune", "restore", "mcp__alpha"]) == 0
    assert _deny(proj) == ["mcp__beta"]
    # CLAUDE_PROJECT_DIR wins over the cwd
    other = proj.parent / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    assert main(["prune", "disable"]) == 0
    assert not _local(proj).exists()


@pytest.mark.usefixtures("plain_git")
def test_restore_from_a_sibling_worktree_restores_where_pruned(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def git(where: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True)

    a = tmp_path / "main"
    a.mkdir()
    git(a, "init", "-q")
    git(a, "remote", "add", "origin", "https://example.invalid/team/repo.git")
    (a / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    git(a, "add", ".gitignore")
    git(a, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    wt = tmp_path / "wt"
    git(a, "worktree", "add", "-q", str(wt))
    _enable(env, a)
    for i in range(3):
        _fold(env, a, f"a{i}", listed=["Read", "mcp__alpha__get"])
    assert _deny(a) == ["mcp__alpha"] and not _local(wt).exists()
    capsys.readouterr()
    assert main(["prune", "restore", "mcp__alpha", "--project", str(wt)]) == 0
    out = capsys.readouterr().out
    assert "not pruned" not in out and str(_local(a).resolve()) in out
    assert not _local(a).exists()


@pytest.mark.usefixtures("plain_git")
def test_notices_are_shown_in_their_own_checkout(env: dict[str, str], tmp_path: Path) -> None:
    a, b = tmp_path / "ca", tmp_path / "cb"
    for d in (a, b):
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(d), "remote", "add", "origin", "https://example.invalid/r.git"],
            check=True,
        )
        (d / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    assert _key(env, a) == _key(env, b)
    _enable(env, a)
    for i in range(3):
        _fold(env, a, f"a{i}", listed=["Read", "mcp__alpha__get"])
    assert _deny(a) == ["mcp__alpha"]
    doc = _doc(env, a)
    assert not doc.get("notices")  # per checkout, never project-wide
    assert any("mcp__alpha" in n for n in doc["files"][prune.file_key(_local(a))]["notices"])
    out_b = _hook({**env, "CLAUDE_PROJECT_DIR": str(b)}, "SessionStart", {"source": "startup"})
    assert "pruned unused tools" not in out_b.get("systemMessage", "")
    out_a = _hook({**env, "CLAUDE_PROJECT_DIR": str(a)}, "SessionStart", {"source": "startup"})
    assert "pruned unused tools: mcp__alpha" in out_a["systemMessage"]


# --------------------------------------------------------------------------- fix wave A (M3 review)
class _Killed(BaseException):
    """The hook process dies (watchdog ``os._exit``, SIGKILL): nothing after it runs."""


def _kill_after_add(monkeypatch: pytest.MonkeyPatch) -> None:
    real = settings.add_deny_rules

    def add_then_die(path: Path, rules: Any, *a: Any, **kw: Any) -> Any:
        real(path, rules, *a, **kw)
        raise _Killed

    monkeypatch.setattr(settings, "add_deny_rules", add_then_die)


def _killed_mid_apply(env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    with monkeypatch.context() as m:
        _kill_after_add(m)
        with pytest.raises(_Killed):
            _fold(env, proj, "s3")
    assert _deny(proj) == ["WebFetch", "WebSearch"]  # the settings write happened ...
    entry_ = _doc(env, proj)["files"][prune.file_key(_local(proj))]
    assert entry_["owned"] == []  # ... the ownership record did not
    assert sorted(entry_["pending_add"]) == ["WebFetch", "WebSearch"]  # but the intent did


def test_b1_kill_after_settings_write_is_owned_next_run(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed_mid_apply(env, proj, monkeypatch)
    _fold(env, proj, "s4")
    doc = _doc(env, proj)
    entry_ = doc["files"][prune.file_key(_local(proj))]
    assert sorted(entry_["owned"]) == ["WebFetch", "WebSearch"]
    assert not entry_.get("foreign") and not entry_.get("pending_add")
    # in-session recovery works on it
    out = _hook(env, "PreToolUse", {"tool_name": "WebFetch", "tool_input": {}})
    assert "WebFetch" in out["systemMessage"] and _deny(proj) == ["WebSearch"]


def test_b1_kill_after_settings_write_hooks_and_restore_all(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed_mid_apply(env, proj, monkeypatch)
    # no fold in between: a hook already treats the journaled rule as ours
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert "WebFetch, WebSearch" in out["hookSpecificOutput"]["additionalContext"]
    out = _hook(env, "UserPromptSubmit", {"prompt": "do a web search"})
    assert "WebSearch" in out["systemMessage"] and _deny(proj) == ["WebFetch"]
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert not _local(proj).exists()  # created by that write: removed again (journaled)
    assert prune.all_owned(_doc(env, proj)) == []


def test_b1_kill_after_settings_write_then_disable(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed_mid_apply(env, proj, monkeypatch)
    assert main(["prune", "disable", "--project", str(proj)]) == 0
    assert not _local(proj).exists()
    assert prune.all_owned(_doc(env, proj)) == []


def test_b1_a_rule_already_present_is_never_journaled(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local(proj).parent.mkdir()
    _local(proj).write_text('{"permissions": {"deny": ["WebSearch"]}}', encoding="utf-8")
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    with monkeypatch.context() as m:
        _kill_after_add(m)
        with pytest.raises(_Killed):
            _fold(env, proj, "s3")
    entry_ = _doc(env, proj)["files"][prune.file_key(_local(proj))]
    assert entry_["pending_add"] == ["WebFetch"]  # WebSearch was the user's rule
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert _deny(proj) == ["WebSearch"]  # the user's rule stays


def test_session_start_never_writes_rules_nor_spawns_a_writer(
    env: dict[str, str], proj: Path, spawned: type[_Spawned]
) -> None:
    _enable(env, proj)
    _make_busy(env, proj)
    _three(env, proj)
    assert _doc(env, proj)["pending"] is True
    _make_idle(env, proj)
    for source in ("startup", "resume", "clear"):
        out = _hook(env, "SessionStart", {"source": source})
        assert not _local(proj).exists()
        assert "Pruned" not in json.dumps(out)
    assert not [c for c in spawned.calls if "prune" in c or "--prune-apply" in c]
    assert main(["prune", "apply", "--project", str(proj)]) == 2  # no such command


def test_only_the_session_end_fold_applies(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    tr = _transcript(_home(env).parent / "transcripts" / "s3.jsonl", listed=BUILTINS)
    key = _key(env, proj)
    ov = session.session_path(_home(env), key, "s3")
    ov.write_text(
        json.dumps({"evidence": {}, "transcript": str(tr), "project_dir": str(proj)}), "utf-8"
    )
    # a left-over fold (SessionStart's in-process or `fold --all`) never writes rules
    overlay.fold("s3", key, _home(env), params=load_config(proj, env).data)
    assert not _local(proj).exists()
    assert _doc(env, proj)["pending"] is True
    _fold(env, proj, "s4")  # the session-end fold
    assert _deny(proj) == ["WebFetch", "WebSearch"]


def test_session_end_spawns_the_applying_fold(
    env: dict[str, str], proj: Path, spawned: type[_Spawned]
) -> None:
    _hook(env, "SessionEnd", {"reason": "exit"})
    folds = [c[c.index("fold") :] for c in spawned.calls if "fold" in c]
    assert folds and folds[0][:3] == ["fold", "--session", "live"] and "--prune-apply" in folds[0]


def test_idle_excludes_the_session_being_folded(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    # s3 itself was active a moment ago and has ended: the project is idle for its fold
    _hook(env, "SessionStart", {"source": "startup", "session_id": "s3"})
    _hook(env, "SessionEnd", {"reason": "exit", "session_id": "s3"})
    _fold(env, proj, "s3")
    assert _deny(proj) == ["WebFetch", "WebSearch"]


def test_another_live_session_defers_until_its_end(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    _hook(env, "SessionStart", {"source": "startup", "session_id": "other"})  # still running
    _fold(env, proj, "s3")
    assert not _local(proj).exists() and _doc(env, proj)["pending"] is True
    _hook(env, "SessionEnd", {"reason": "exit", "session_id": "other"})
    _fold(env, proj, "s4")
    assert _deny(proj) == ["WebFetch", "WebSearch"]


def test_hook_restore_never_waits_on_git_during_a_fold(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(env, proj)
    _fold(env, proj, "s1")
    _fold(env, proj, "s2")
    real = settings._git
    free: list[bool] = []

    def slow_git(project_dir: Path, *args: str) -> Any:
        # a hook restore at this moment takes both locks within its 0.1 s budget
        for path in (_pdir(env, proj) / prune.PRUNE_FILE, prune.registry_path(_home(env))):
            with prune.jsonstore.locked(path, prune.HOOK_LOCK_S):
                pass
        free.append(True)
        return real(project_dir, *args)

    monkeypatch.setattr(settings, "_git", slow_git)
    _fold(env, proj, "s3")
    assert free and _deny(proj) == ["WebFetch", "WebSearch"]


# M6: pruned rules accrue no evidence; each prune is a lease
def test_m6_pruned_rules_accrue_no_evidence(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    before = _doc(env, proj)["tools"]["WebFetch"]
    _fold(env, proj, "s4")
    _fold(env, proj, "s5")
    assert _doc(env, proj)["tools"]["WebFetch"] == {**before, **{"r": before["r"]}}
    assert _doc(env, proj)["tools"]["WebFetch"]["s"] == before["s"]


def test_m6_lease_restores_then_needs_fresh_evidence(env: dict[str, str], proj: Path) -> None:
    _user_config(env, "[prune]\nbuiltins = true\nlease_sessions = 2\n")
    _enable(env, proj)
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    _fold(env, proj, "l1")
    assert _deny(proj) == ["WebFetch", "WebSearch"]  # lease: 1 of 2 sessions
    _fold(env, proj, "l2")
    assert not _local(proj).exists()  # lease over: restored for at least one session
    doc = _doc(env, proj)
    assert "WebFetch" not in doc["kept"]  # no regret: a lease end is not a restore by use
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert "lease" in out["systemMessage"]
    _make_idle(env, proj)  # that session start counted as activity
    # re-pruned only after fresh evidence from un-pruned sessions (min_sessions = 3)
    _fold(env, proj, "f1")
    _fold(env, proj, "f2")
    assert not _local(proj).exists()
    _fold(env, proj, "f3")
    assert _deny(proj) == ["WebFetch", "WebSearch"]


def test_m6_tool_used_after_the_lease_is_kept_for_good(env: dict[str, str], proj: Path) -> None:
    _user_config(env, "[prune]\nbuiltins = true\nlease_sessions = 1\n")
    _enable(env, proj)
    _three(env, proj)
    _fold(env, proj, "l1")
    assert not _local(proj).exists()
    _fold(env, proj, "u1", used=["WebFetch"])  # un-pruned session: fresh evidence 1 of 3
    _fold(env, proj, "f1")
    _fold(env, proj, "f2")
    assert _deny(proj) == ["WebSearch"]  # re-pruned on fresh evidence; WebFetch never again
    assert prune.is_protected("WebFetch", _doc(env, proj))


def test_m6_project_may_only_lower_the_lease(env: dict[str, str], proj: Path) -> None:
    assert policy_for(("prune", "lease_sessions")) == "min"
    (proj / "token-sparen.toml").write_text("[prune]\nlease_sessions = 4\n", encoding="utf-8")
    assert load_config(proj, env).get("prune.lease_sessions") == 4
    (proj / "token-sparen.toml").write_text("[prune]\nlease_sessions = 400\n", encoding="utf-8")
    assert load_config(proj, env).get("prune.lease_sessions") == 15
    (proj / "token-sparen.toml").write_text("[prune]\nlease_sessions = 0\n", encoding="utf-8")
    assert load_config(proj, env).get("prune.lease_sessions") == 15


# I2: a settings file is written only on positive git confirmation (untracked and ignored)
def test_git_unavailable_means_never_write(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "_git", lambda *a: None)
    assert settings.git_exposure(proj) == "unknown"
    _enable(env, proj)
    _three(env, proj)
    assert not _local(proj).exists()
    msg = _hook(env, "SessionStart", {"source": "startup"})["systemMessage"]
    assert "git could not confirm" in msg and "did not write" in msg


def test_git_timeout_means_unknown(proj: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired("git", settings.GIT_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", slow)
    assert settings.git_exposure(proj) == "unknown"


@pytest.mark.parametrize("rc", [1, 2, 128, 129, -9])
def test_any_unexpected_git_exit_means_unknown(
    proj: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(project_dir: Path, *args: str) -> tuple[int, bytes]:
        calls.append(args)
        return (rc, b"") if args[0] == "ls-files" else (0, b"")

    monkeypatch.setattr(settings, "_git", fake)
    assert settings.git_exposure(proj) == "unknown"  # e.g. dubious ownership (128)

    def fake_ignore(project_dir: Path, *args: str) -> tuple[int, bytes]:
        return (0, b"") if args[0] == "ls-files" else (rc, b"")

    monkeypatch.setattr(settings, "_git", fake_ignore)
    assert settings.git_exposure(proj) == ("unignored" if rc == 1 else "unknown")


def test_git_exposure_probes(proj: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.git_exposure(proj) is None  # untracked and ignored: the only "write"
    loose = tmp_path / "loose"
    loose.mkdir()
    assert settings.git_exposure(loose) == "unknown"  # not a git work tree
    bare = _git_checkout(tmp_path / "bare", ignored=False)
    assert settings.git_exposure(bare) == "unignored"
    tracked = _git_checkout(tmp_path / "tracked")
    _local(tracked).parent.mkdir()
    _local(tracked).write_text("{}\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tracked), "add", "-f", ".claude/settings.local.json"], check=True
    )
    assert settings.git_exposure(tracked) == "tracked"
    (tracked / ".git" / "index").write_bytes(b"corrupt")  # ls-files exits 128
    assert settings.git_exposure(tracked) == "unknown"
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    assert settings.git_exposure(proj) == "unknown"
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "nonexistent" / "x"))
    assert settings.git_exposure(proj) == "unknown"  # git would read an empty index
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert settings.git_exposure(proj) is None


def test_unignored_or_non_git_checkout_is_never_written(
    env: dict[str, str], tmp_path: Path
) -> None:
    for name, ignored in (("plain", False), ("nogit", None)):
        root = tmp_path / name
        if ignored is None:
            root.mkdir()
        else:
            _git_checkout(root, ignored=False)
        e = {**env, "CLAUDE_PROJECT_DIR": str(root)}
        assert main(["prune", "enable", "--project", str(root)]) == 0
        _three(e, root)
        assert not _local(root).exists(), name
        msg = _hook(e, "SessionStart", {"source": "startup"})["systemMessage"]
        assert "did not write" in msg, name


# minor 4: the backup is refreshed at the start of a cycle in which we own no rule there
def test_stale_backup_is_refreshed_when_we_own_nothing(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _local(proj).parent.mkdir()
    _local(proj).write_text('{"permissions": {"deny": []}}', encoding="utf-8")
    backup = _local(proj).with_name("settings.local.json.token-sparen.bak")
    _three(env, proj)
    data = json.loads(_local(proj).read_text(encoding="utf-8"))
    data["env"] = {"LATER": "1"}  # a user edit while our rules are in place
    _local(proj).write_text(json.dumps(data), encoding="utf-8")
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert backup.exists()  # differs from the file: kept (stale now)
    now = _local(proj).read_text(encoding="utf-8")
    for sid in ("t1", "t2", "t3"):
        _fold(env, proj, sid, listed=["Read", "NotebookEdit"])
    assert _deny(proj) == ["NotebookEdit"]
    assert backup.read_text(encoding="utf-8") == now  # the backup is the pre-cycle file


# minor 9: no claim about mid-session availability
def test_restore_message_makes_no_mid_session_claim(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    out = _hook(env, "PreToolUse", {"tool_name": "WebFetch", "tool_input": {}})
    msg = out["systemMessage"]
    assert "available again from the next request" not in msg
    assert f"`WebFetch` is no longer denied in {_local(proj)}" in msg
    assert "Claude Code may need a new session to show it again." in msg


# I3: ownership lives in a user-level registry keyed by the settings file's real path
def test_remote_change_never_orphans_a_rule(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    old_key = _key(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    reg = _reg(env)["files"][prune.file_key(_local(proj))]
    assert reg["owned"] == ["WebFetch", "WebSearch"] and reg["key"] == old_key
    assert "files" not in json.loads((_pdir(env, proj) / prune.PRUNE_FILE).read_text("utf-8"))
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin", "https://example.invalid/t/r.git"],
        check=True,
    )
    assert _key(env, proj) != old_key  # a new project key: its store has no record
    out = _hook(env, "SessionStart", {"source": "startup"})
    assert "WebFetch, WebSearch" in out["hookSpecificOutput"]["additionalContext"]
    out = _hook(env, "PreToolUse", {"tool_name": "WebFetch", "tool_input": {}})
    assert "WebFetch" in out["systemMessage"] and _deny(proj) == ["WebSearch"]
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert not _local(proj).exists()


def test_remote_change_then_disable(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin", "https://example.invalid/t/r.git"],
        check=True,
    )
    assert main(["prune", "disable", "--project", str(proj)]) == 0
    assert not _local(proj).exists()


def test_legacy_per_project_records_are_migrated(env: dict[str, str], proj: Path) -> None:
    _enable(env, proj)
    _three(env, proj)
    # rewrite the state as an older version kept it: the records inside prune.json
    path = _pdir(env, proj) / prune.PRUNE_FILE
    doc = json.loads(path.read_text(encoding="utf-8"))
    files = _reg(env)["files"]
    for e in files.values():
        e.pop("key", None)
    doc["files"] = files
    path.write_text(json.dumps(doc), encoding="utf-8")
    prune.registry_path(_home(env)).unlink()
    assert prune.pruned_count(_home(env), _key(env, proj), env) == 2  # read: migrated in memory
    out = _hook(env, "PreToolUse", {"tool_name": "WebSearch", "tool_input": {}})
    assert "WebSearch" in out["systemMessage"] and _deny(proj) == ["WebFetch"]
    assert "files" not in json.loads(path.read_text(encoding="utf-8"))  # a locked write moved it
    assert _reg(env)["files"][prune.file_key(_local(proj))]["owned"] == ["WebFetch"]


# concern 1: a journal-recovered restore also removes the containers we created
def test_recovered_restore_removes_created_containers(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed_mid_apply(env, proj, monkeypatch)  # the file did not exist before
    created = _doc(env, proj)["files"][prune.file_key(_local(proj))]["pending_created"]
    assert created == {"dir": True, "file": True, "permissions": True, "deny": True}
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert not _local(proj).exists() and not (proj / ".claude").exists()


def test_recovered_restore_keeps_the_users_file(
    env: dict[str, str], proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local(proj).parent.mkdir()
    _local(proj).write_text('{"env": {"A": "1"}}', encoding="utf-8")
    _killed_mid_apply(env, proj, monkeypatch)
    assert main(["prune", "restore", "--all", "--project", str(proj)]) == 0
    assert json.loads(_local(proj).read_text(encoding="utf-8")) == {"env": {"A": "1"}}


# Fix round 2 (R1-I1): opt-in is not carried across project keys; the fold restores instead
def _remote_change(proj: Path) -> None:
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin", "https://example.invalid/t/r.git"],
        check=True,
    )


def test_remote_change_restores_at_the_next_fold(env: dict[str, str], proj: Path) -> None:
    _user_config(env, "[prune]\nbuiltins = true\nlease_sessions = 2\n")
    _enable(env, proj)  # a per-project opt-in, under the old key
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    _remote_change(proj)
    _fold(env, proj, "r1")
    assert not _local(proj).exists()  # not enabled under the new key: restored (exact revert)
    assert prune.all_owned(_doc(env, proj)) == []
    msg = _hook(env, "SessionStart", {"source": "startup"})["systemMessage"]
    assert "WebFetch, WebSearch" in msg and "not enabled" in msg
    for sid in ("r2", "r3", "r4"):
        _fold(env, proj, sid)
    assert not _local(proj).exists()  # and never pruned again under the new key


def test_remote_change_with_user_wide_opt_in_keeps_the_lease(
    env: dict[str, str], proj: Path
) -> None:
    _user_config(env, "[prune]\nenabled = true\nbuiltins = true\nlease_sessions = 2\n")
    _three(env, proj)
    assert _deny(proj) == ["WebFetch", "WebSearch"]
    _remote_change(proj)
    _fold(env, proj, "r1")
    assert _deny(proj) == ["WebFetch", "WebSearch"]  # active under the new key too: adopted
    assert _reg(env)["files"][prune.file_key(_local(proj))]["key"] == _key(env, proj)
    _fold(env, proj, "r2")
    assert not _local(proj).exists()  # the lease ended after 2 sessions


def test_symlinked_settings_file_is_never_written(
    env: dict[str, str], proj: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "dotfiles" / "settings.local.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text("{}\n", encoding="utf-8")
    _local(proj).parent.mkdir()
    _local(proj).symlink_to(elsewhere)
    _enable(env, proj)
    _three(env, proj)
    assert elsewhere.read_text(encoding="utf-8") == "{}\n" and _local(proj).is_symlink()
    msg = _hook(env, "SessionStart", {"source": "startup"})["systemMessage"]
    assert "symbolic link" in msg and "nothing was changed" in msg


def test_symlinked_settings_folder_is_never_written(
    env: dict[str, str], proj: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "shared-claude"
    elsewhere.mkdir()
    (proj / ".claude").symlink_to(elsewhere, target_is_directory=True)
    _enable(env, proj)
    _three(env, proj)
    assert not (elsewhere / "settings.local.json").exists()
