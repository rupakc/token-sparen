"""End to end: a PostToolUse payload piped through the real hook launcher subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sampleoutputs import big_pytest

ROOT = Path(__file__).resolve().parents[3]


def _run(event: str, payload: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOKEN_SPAREN_", "CLAUDE_"))}
    env.update(
        {
            "TOKEN_SPAREN_HOME": str(tmp_path / "home"),
            "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
            "TOKEN_SPAREN_ADAPTER__LATENCY_BUDGET_MS": "5000",  # slow CI runners
        }
    )
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
    assert rec["tool_use_id"] == "tu-e2e" and rec["arm"] == "standard"


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
