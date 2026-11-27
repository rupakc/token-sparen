"""Typed run statuses (ruling M5-B2) and load verification (M5 Task 5)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bench import arms as am
from bench import classify as cl
from bench import streamjson as sj

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_794_990_000.0


def _m(name: str) -> sj.RunMetrics:
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return sj.parse_stream(lines, observed_at=NOW)


CLEAN: dict[str, Any] = {
    "exit_code": 0,
    "killed_for_timeout": False,
    "backstop_killed": False,
    "disk_low": False,
    "oom_killed": False,
    "load_problems": [],
    "live": True,
    "leak": False,
}

# one fixture per row of the mapping table, in table order (first match wins)
ROWS: list[tuple[str, str | None, dict[str, Any], tuple[str, str]]] = [
    ("leak beats everything", "stream_success", {"leak": True, "exit_code": 125},
     ("infra_fail", "leak")),
    ("docker exit 125", None, {"exit_code": 125}, ("infra_fail", "docker")),
    ("docker exit 126", "stream_success", {"exit_code": 126}, ("infra_fail", "docker")),
    ("docker exit 127", None, {"exit_code": 127}, ("infra_fail", "docker")),
    ("backstop kill", "stream_success", {"backstop_killed": True}, ("infra_fail", "docker")),
    ("oom", "status_max_turns", {"oom_killed": True, "exit_code": 137},
     ("infra_fail", "docker")),
    # ruling I1: the in-container timeout marker wins (patch graded), even if not live
    ("in-container timeout", "status_max_turns", {"killed_for_timeout": True, "exit_code": 124},
     ("agent_fail", "timeout")),
    ("timeout cut inside infra-looking tool output", "status_timeout_truncated",
     {"killed_for_timeout": True, "exit_code": 124, "live": False}, ("agent_fail", "timeout")),
    ("api 401", "status_auth_401", {"exit_code": 1}, ("infra_fail", "auth")),
    ("auth beats not live", "status_auth_401", {"exit_code": 1, "live": False},
     ("infra_fail", "auth")),
    ("auth text", "status_auth_text", {"exit_code": 1}, ("infra_fail", "auth")),
    ("rate rejected event", "status_rate_rejected", {"exit_code": 1}, ("infra_fail", "rate")),
    ("api 429", "status_rate_429", {"exit_code": 1}, ("infra_fail", "rate")),
    ("hit your limit text", "status_rate_text", {"exit_code": 1}, ("infra_fail", "rate")),
    ("retries exhausted on 429", "status_retry_429", {"exit_code": 1}, ("infra_fail", "rate")),
    ("api 529", "status_api_529", {"exit_code": 1}, ("infra_fail", "api")),
    ("api retry exhausted", "status_api_retry", {"exit_code": 1}, ("infra_fail", "api")),
    ("wrapper proxy refused", "status_proxy", {"exit_code": 1, "wrapper": True},
     ("infra_fail", "proxy")),
    ("no init event", "status_no_init", {}, ("infra_fail", "load")),
    ("no stream at all", None, {"exit_code": 1}, ("infra_fail", "load")),
    ("load problems", "stream_success", {"load_problems": ["plugin x missing"]},
     ("infra_fail", "load")),
    ("not live", "stream_success", {"live": False}, ("infra_fail", "load")),
    ("disk below floor", "stream_success", {"disk_low": True}, ("infra_fail", "disk")),
    # ruling I2: the final result decides; earlier retries/rejections are counters only
    ("success", "stream_success", {}, ("ok", "success")),
    ("success after retries and a rejection", "status_success_after_retries", {},
     ("ok", "success")),
    # ruling I3: context overflow is the agent's (graded)
    ("prompt is too long", "status_context", {"exit_code": 1}, ("agent_fail", "context")),
    ("max turns", "status_max_turns", {"exit_code": 1}, ("agent_fail", "max_turns")),
    ("execution error", "status_execution", {"exit_code": 1}, ("agent_fail", "execution")),
    ("anything else", "status_unknown", {"exit_code": 1}, ("infra_fail", "unknown")),
    ("stream cut without a timeout marker", "status_timeout_truncated", {"exit_code": 1},
     ("infra_fail", "unknown")),
]  # fmt: skip


@pytest.mark.parametrize(("fixture", "kwargs", "want"), [r[1:] for r in ROWS],
                         ids=[r[0] for r in ROWS])  # fmt: skip
def test_status_mapping_table(
    fixture: str | None, kwargs: dict[str, Any], want: tuple[str, str]
) -> None:
    m = _m(fixture) if fixture else None
    assert cl.classify(m, **{**CLEAN, **kwargs}) == want


def test_infra_heuristics_never_read_tool_output() -> None:
    m = _m("status_timeout_truncated")
    assert m.error_text is None and m.api_error_status is None  # tool text is not error text
    assert m.malformed_lines == 1
    # an assistant message quoting an API error is not the final result
    quoted = sj.parse_stream(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
            json.dumps({"type": "assistant", "message": {"id": "m", "model": "x", "content": [
                {"type": "text", "text": "The log says API Error: 401 Unauthorized"}]}}),
            json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}),
        ],
        observed_at=NOW,
    )  # fmt: skip
    assert quoted.api_error_status is None
    assert cl.classify(quoted, **CLEAN) == ("agent_fail", "max_turns")
    # stray stderr text without a result never makes an infra status by itself
    stray = sj.parse_stream(["Invalid API key", "API Error: 500"], observed_at=NOW)
    assert stray.error_text is None and stray.api_error_status is None


def test_status_mapping_edges() -> None:
    clean = dict(CLEAN)
    # a refused connection outside a wrapper arm is not a proxy failure
    assert cl.classify(_m("status_proxy"), **clean) == ("infra_fail", "unknown")
    # execution error with an API status that no infra row claims is not an agent failure
    m = replace(_m("status_execution"), api_error_status=400)
    assert cl.classify(m, **clean) == ("infra_fail", "unknown")
    # subtype success flagged is_error (e.g. an API error reported as the final text)
    assert cl.classify(replace(_m("stream_success"), is_error=True), **clean) == (
        "infra_fail",
        "unknown",
    )
    # retries and rejections are still recorded as counters
    ok = _m("status_success_after_retries")
    assert len(ok.api_retries) == 2 and ok.api_retry_exhausted
    assert [r.status for r in ok.rate_limits] == ["rejected"]
    # infra statuses are never agent failures
    for row in ROWS:
        status, sub = row[3]
        assert (status == "infra_fail") == (sub in cl.INFRA_SUBSTATUSES)


def _arm(name: str) -> am.Arm:
    return am.load_arms()[name]


def _init(**over: Any) -> sj.RunMetrics:
    init: dict[str, Any] = {
        "type": "system",
        "subtype": "init",
        "session_id": "s",
        "plugins": [],
        "mcp_servers": [],
    }
    init.update(over)
    return sj.parse_stream([json.dumps(init)], observed_at=NOW)


def test_verify_load_flags_plugin_errors_and_missing_mcp() -> None:
    ts = _m("stream_success")
    assert cl.verify_load(ts, _arm("token-sparen"), "ab" * 32) == []
    # a frozen-plugin arm without a recorded hash, or loaded from elsewhere
    assert cl.verify_load(ts, _arm("token-sparen"), None)
    moved = replace(ts, plugins={"token-sparen": "/home/bench/.claude/plugins/token-sparen"})
    assert any("frozen" in p for p in cl.verify_load(moved, _arm("token-sparen"), "ab" * 32))
    # plugin errors reported at init
    errs = replace(ts, plugin_errors=["token-sparen: hooks.json invalid"])
    assert any("hooks.json" in p for p in cl.verify_load(errs, _arm("token-sparen"), "ab" * 32))
    # expected plugin missing
    bare = _init()
    problems = cl.verify_load(bare, _arm("token-sparen"), "ab" * 32)
    assert any("token-sparen" in p and "not loaded" in p for p in problems)
    # a plugin in an arm that should not have it (contamination)
    assert any("unexpected" in p for p in cl.verify_load(ts, _arm("baseline"), None))
    assert cl.verify_load(bare, _arm("baseline"), None) == []
    # context-mode: plugin by install name, MCP server must be connected
    cm_ok = _init(
        plugins=[{"name": "context-mode", "path": "/home/bench/.claude/plugins/cache/cm"}],
        mcp_servers=[{"name": "plugin:context-mode:context-mode", "status": "connected"}],
    )
    assert cl.verify_load(cm_ok, _arm("context-mode"), None) == []
    cm_failed = replace(cm_ok, mcp_servers={"plugin:context-mode:context-mode": "failed"})
    assert any("context-mode" in p and "failed" in p for p in
               cl.verify_load(cm_failed, _arm("context-mode"), None))  # fmt: skip
    cm_missing = replace(cm_ok, mcp_servers={})
    assert any("MCP" in p for p in cl.verify_load(cm_missing, _arm("context-mode"), None))
    # any failed MCP server is a load problem, whatever the arm
    other = replace(bare, mcp_servers={"stray": "failed"})
    assert any("stray" in p for p in cl.verify_load(other, _arm("baseline"), None))
    # plugin names may carry the marketplace suffix
    cave = _init(plugins=[{"name": "caveman@caveman", "path": "/x"}])
    assert cl.verify_load(cave, _arm("caveman"), None) == []
    # no init at all
    assert cl.verify_load(sj.parse_stream([], observed_at=NOW), _arm("baseline"), None)
    # headroom registers its MCP server at run time
    hr = _init(mcp_servers=[{"name": "headroom", "status": "connected"}])
    assert cl.verify_load(hr, _arm("headroom"), None) == []
    assert cl.verify_load(bare, _arm("headroom"), None)


def _hook(command: str, event: str = "PreToolUse") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "hook_response",
        "hook_event": event,
        "command": command,
        "exit_code": 0,
        "outcome": "success",
    }


def test_verify_load_flags_foreign_tools_hash_and_unpriced_models() -> None:
    ts = _m("stream_success")
    # stage hash compare (review M3)
    assert cl.verify_load(ts, _arm("token-sparen"), "ab" * 32, stage_hash="ab" * 32) == []
    mismatch = cl.verify_load(ts, _arm("token-sparen"), "ab" * 32, stage_hash="cd" * 32)
    assert any("hash" in p for p in mismatch)
    # a string-form plugin entry has no path: the frozen mount cannot be confirmed
    no_path = replace(ts, plugins={"token-sparen": ""})
    assert any("frozen" in p for p in cl.verify_load(no_path, _arm("token-sparen"), "ab" * 32))
    # another arm's hook (rtk installs a settings hook, not a plugin) or MCP server (review M2)
    base_init = {"type": "system", "subtype": "init", "session_id": "s", "plugins": []}
    rtk_in_baseline = sj.parse_stream(
        [json.dumps(base_init), json.dumps(_hook("rtk hook claude"))], observed_at=NOW
    )
    assert any("rtk" in p for p in cl.verify_load(rtk_in_baseline, _arm("baseline"), None))
    assert cl.verify_load(rtk_in_baseline, _arm("rtk"), None) == []
    cm_server = replace(rtk_in_baseline, hook_events=[], mcp_servers={"context-mode": "connected"})
    assert any("context-mode" in p for p in cl.verify_load(cm_server, _arm("baseline"), None))
    ts_hook = sj.parse_stream(
        [json.dumps(base_init), json.dumps(_hook('python3 -I "/opt/plugin/token-sparen/x"'))],
        observed_at=NOW,
    )
    assert any("token-sparen" in p for p in cl.verify_load(ts_hook, _arm("baseline"), None))
    # a model the price table does not know fails the load check (review I4)
    odd = replace(ts, model_usage={"claude-mystery-9": ts.model_usage["claude-sonnet-4-6"]})
    assert any("price" in p for p in cl.verify_load(odd, _arm("token-sparen"), "ab" * 32))
