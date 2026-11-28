"""End to end: a PostToolUse payload piped through the real hook launcher subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sampleoutputs import big_pytest
from userconf import write_user_config

ROOT = Path(__file__).resolve().parents[3]


def _run(
    event: str,
    payload: dict[str, Any],
    tmp_path: Path,
    user: dict[str, Any] | None = None,
    **extra: str,
) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    env.update(
        {
            "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
            "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
            "TOKEN_SPAREN_TEST_SEED": "2",  # deterministic engine draws (non-default home only)
            **extra,
        }
    )
    # slow CI runners: a larger budget widens, so it goes in the user's file (env only narrows)
    write_user_config(tmp_path / "home", {"adapter": {"latency_budget_ms": 5000}, **(user or {})})
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "hooks" / "run.py"), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=True,
    )
    assert proc.stdout.count("\n") == 1
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


def _payload(event: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"session_id": "e2e-1", "cwd": "/", "hook_event_name": event}
    base.update(extra)
    return base


def test_e2e_post_tool(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    text = big_pytest()
    prompt = _payload("UserPromptSubmit", prompt="why does test_cache_eviction fail?")
    assert _run("UserPromptSubmit", prompt, tmp_path) == {}
    payload = _payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -v"},
        tool_use_id="tu-e2e",
        tool_response={"stdout": text, "stderr": "", "interrupted": False, "isImage": False},
    )
    out = _run("PostToolUse", payload, tmp_path)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    stdout = spec["updatedToolOutput"]["stdout"]
    assert "[token-sparen: kept " in stdout and "`token-sparen show " in stdout
    assert "FAILED tests/test_cache.py::test_cache_eviction" in stdout
    assert len(stdout) < len(text)
    saved = list((tmp_path / "proj" / ".token-sparen" / "outputs").glob("*.txt"))
    assert [p.read_text(encoding="utf-8") for p in saved] == [text]
    (logged,) = (tmp_path / "home" / "projects").glob("*/decisions.jsonl")
    rec = json.loads(logged.read_text(encoding="utf-8"))
    assert rec["tool_use_id"] == "tu-e2e"
    # the learning engine decided: a cold bucket's ladder allows only its first trimming arm
    assert rec["arm"] == "light" and rec["bucket"] != "static" and rec["propensity"] < 1


def test_e2e_failure_event_is_noop_on_claude(tmp_path: Path) -> None:
    """Claude Code cannot replace a PostToolUseFailure result: no work, no fake savings."""
    (tmp_path / "proj").mkdir()
    payload = _payload(
        "PostToolUseFailure",
        tool_name="Bash",
        tool_input={"command": "pytest -v"},
        tool_use_id="tu-f",
        error=big_pytest(),
    )
    assert _run("PostToolUseFailure", payload, tmp_path) == {}
    assert not (tmp_path / "proj" / ".token-sparen" / "outputs").exists()


def test_e2e_agent_result_compressed(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    text = big_pytest()
    half = len(text.splitlines()) // 2
    lines = text.splitlines()
    blocks = ["\n".join(lines[:half]), "\n".join(lines[half:])]
    response = {
        "status": "completed",
        "agentId": "agent-7",
        "content": [{"type": "text", "text": b} for b in blocks],
        "totalTokens": 999,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    payload = _payload(
        "PostToolUse",
        tool_name="Agent",
        tool_input={"prompt": "run the tests"},
        tool_use_id="tu-agent",
        tool_response=response,
    )
    new = _run("PostToolUse", payload, tmp_path)["hookSpecificOutput"]["updatedToolOutput"]
    assert {k: v for k, v in new.items() if k != "content"} == {
        k: v for k, v in response.items() if k != "content"
    }
    (block,) = new["content"]
    assert block["type"] == "text" and "[token-sparen: kept " in block["text"]
    assert len(block["text"]) <= len("\n".join(blocks))  # never more than the model saw
    assert "FAILED tests/test_cache.py::test_cache_eviction" in block["text"]


def test_e2e_agent_call_site_report_left_whole(tmp_path: Path) -> None:
    """v1 review M11 (probed): a subagent listing 240 call sites is not cut like grep output."""
    (tmp_path / "proj").mkdir()
    report = "\n".join(
        f"src/pkg/mod_{i % 12}.py:{i + 3}: result = legacy_client.fetch(user_id={i}, retries=3)"
        for i in range(240)
    )
    response = {
        "status": "completed",
        "agentId": "agent-8",
        "content": [{"type": "text", "text": report}],
        "totalTokens": 999,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    payload = _payload(
        "PostToolUse",
        tool_name="Agent",
        tool_input={"prompt": "list every call site of legacy_client.fetch"},
        tool_use_id="tu-agent-sites",
        tool_response=response,
    )
    static = {"learn": {"enabled": False}}  # the static policy always trims an allowed type
    assert "updatedToolOutput" not in json.dumps(_run("PostToolUse", payload, tmp_path, static))


def test_e2e_env_narrowing_reaches_the_hook(tmp_path: Path) -> None:
    """Through the real launcher: a narrowing TOKEN_SPAREN_* variable is honoured (the output is
    left whole), a widening one is ignored with one logged line."""
    (tmp_path / "proj").mkdir()
    payload = _payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -v"},
        tool_use_id="tu-narrow",
        tool_response={
            "stdout": big_pytest(),
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
    )
    out = _run(
        "PostToolUse",
        payload,
        tmp_path,
        TOKEN_SPAREN_COMPRESSION__MIN_TOKENS="999999999",  # narrows: never trim
        TOKEN_SPAREN_COMPRESSION__TRIM_FILE_CONTENTS="true",  # widens: ignored
    )
    assert out == {}
    errors = (tmp_path / "home" / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert errors.count("TOKEN_SPAREN_COMPRESSION__TRIM_FILE_CONTENTS") == 1
    assert "MIN_TOKENS" not in errors
