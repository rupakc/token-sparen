"""Per-arm liveness probes from hook events and copied-out state (M5 Task 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from bench import arms as am
from bench import liveness as lv
from bench import streamjson as sj

NOW = 1_794_990_000.0
SID = "sess-main"


def _hook(event: str, command: str, outcome: str = "success", code: int = 0) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "hook_response",
        "hook_event": event,
        "hook_name": event,
        "command": command,
        "stdout": "{}",
        "exit_code": code,
        "outcome": outcome,
        "session_id": SID,
    }


def _metrics(*events: dict[str, Any], mcp: list[dict[str, str]] | None = None) -> sj.RunMetrics:
    init = {"type": "system", "subtype": "init", "session_id": SID, "mcp_servers": mcp or []}
    return sj.parse_stream([json.dumps(e) for e in (init, *events)], observed_at=NOW)


TS_CMD = 'python3 -I "/opt/plugin/token-sparen/hooks/run.py" {}'
CM_CMD = "node /home/bench/.claude/plugins/cache/context-mode/hooks/{}.mjs"
TE_CMD = "python3 /home/bench/.claude/plugins/cache/tokeneconomizer/hooks/{}.py"
CAVE_CMD = "node /home/bench/.claude/plugins/cache/caveman/hooks/{}.js"


def _session_file(home: Path, sid: str = SID) -> Path:
    path = home / ".token-sparen" / "projects" / ("0" * 16) / "sessions" / f"{sid}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_liveness_from_hook_events_per_arm(tmp_path: Path) -> None:
    # every liveness kind used by arms.toml (and every declared kind) has a probe
    used = {arm.liveness for arm in am.load_arms().values()}
    assert used <= set(lv.LIVENESS)
    assert set(lv.LIVENESS) == set(am.LIVENESS_KINDS)

    home = tmp_path / "home"
    home.mkdir()
    nothing = _metrics()
    assert lv.LIVENESS["none"](nothing, home)
    for kind in set(lv.LIVENESS) - {"none"}:
        assert not lv.LIVENESS[kind](nothing, home), kind

    # token-sparen: hooks ran and printed {} but no session file → not live
    ts = _metrics(_hook("SessionStart", TS_CMD.format("SessionStart")))
    assert not lv.LIVENESS["token_sparen"](ts, home)
    _session_file(home)
    assert lv.LIVENESS["token_sparen"](ts, home)
    # the session file must belong to this session, and a hook must have succeeded
    other = tmp_path / "other"
    other.mkdir()
    _session_file(other, "someone-else")
    assert not lv.LIVENESS["token_sparen"](ts, other)
    failed = _metrics(_hook("SessionStart", TS_CMD.format("SessionStart"), "error", 1))
    assert not lv.LIVENESS["token_sparen"](failed, home)
    assert not lv.LIVENESS["token_sparen"](_metrics(_hook("SessionStart", "true")), home)

    # rtk: an ok PreToolUse event from `rtk hook`
    assert lv.LIVENESS["rtk"](_metrics(_hook("PreToolUse", "rtk hook claude")), home)
    assert not lv.LIVENESS["rtk"](_metrics(_hook("PostToolUse", "rtk hook claude")), home)
    assert not lv.LIVENESS["rtk"](_metrics(_hook("PreToolUse", "rtk hook", "error", 2)), home)
    assert not lv.LIVENESS["rtk"](_metrics(_hook("PreToolUse", "/usr/bin/artkhook")), home)

    # headroom: a proxied request in the copied-out log
    hr_home = tmp_path / "hr"
    logs = hr_home / ".headroom" / "logs"
    logs.mkdir(parents=True)
    (logs / "proxy.log").write_text("started\n", encoding="utf-8")
    assert not lv.LIVENESS["headroom"](nothing, hr_home)
    (logs / "requests.jsonl").write_text('{"path": "/v1/messages"}\n', encoding="utf-8")
    assert lv.LIVENESS["headroom"](nothing, hr_home)

    # context-mode: an ok hook from its plugin and its MCP server connected
    cm_hook = _hook("PreToolUse", CM_CMD.format("pretooluse"))
    connected = [{"name": "plugin:context-mode:context-mode", "status": "connected"}]
    assert lv.LIVENESS["context_mode"](_metrics(cm_hook, mcp=connected), home)
    assert not lv.LIVENESS["context_mode"](_metrics(cm_hook), home)
    failed_mcp = [{"name": "plugin:context-mode:context-mode", "status": "failed"}]
    assert not lv.LIVENESS["context_mode"](_metrics(cm_hook, mcp=failed_mcp), home)

    # tokeneconomizer: an ok hook from its plugin
    assert lv.LIVENESS["tokeneconomizer"](_metrics(_hook("Stop", TE_CMD.format("stop"))), home)
    te_err = _hook("Stop", TE_CMD.format("stop"), "error", 1)
    assert not lv.LIVENESS["tokeneconomizer"](_metrics(te_err), home)

    # caveman: an ok SessionStart event from its plugin
    cave_start = _hook("SessionStart", CAVE_CMD.format("start"))
    assert lv.LIVENESS["caveman"](_metrics(cave_start), home)
    cave_prompt = _hook("UserPromptSubmit", CAVE_CMD.format("prompt"))
    assert not lv.LIVENESS["caveman"](_metrics(cave_prompt), home)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_liveness_ignores_planted_links(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _session_file(real)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".token-sparen").symlink_to(real / ".token-sparen", target_is_directory=True)
    ts = _metrics(_hook("SessionStart", TS_CMD.format("SessionStart")))
    assert not lv.LIVENESS["token_sparen"](ts, home)
    logs = home / ".headroom" / "logs"
    logs.mkdir(parents=True)
    target = tmp_path / "elsewhere.log"
    target.write_text("/v1/messages\n", encoding="utf-8")
    (logs / "requests.log").symlink_to(target)
    assert not lv.LIVENESS["headroom"](_metrics(), home)


def test_is_live_and_copy_out_paths() -> None:
    arms = am.load_arms()
    assert lv.is_live(_metrics(), arms["baseline"], Path("/nonexistent"))
    # the backend must copy these out of the container whatever the arm's state_paths
    assert lv.LIVENESS_PATHS["token_sparen"] == (".token-sparen",)
    assert lv.LIVENESS_PATHS["headroom"] == (".headroom/logs",)
