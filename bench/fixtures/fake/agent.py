"""Fake `claude` for the bench dry run: no model, no network, no credential.

Invoked like ``claude -p <prompt> --output-format stream-json …`` in the task repository. It
prints a synthetic stream-json session (``system/init``, one assistant turn, ``result``), writes
a transcript under ``$CLAUDE_CONFIG_DIR/projects/`` and, for the ``success`` scenario, fixes the
fake task's off-by-one bug in ``calc.py``. With ``--plugin-dir`` it reports that plugin loaded,
emits a token-sparen hook event and writes a session file under ``$TOKEN_SPAREN_HOME`` (what the
liveness probe checks). ``TSB_FAKE_SCENARIO`` picks the behaviour:

``success`` (default) fixes the bug; ``fail`` leaves it; ``quota``, ``auth`` and ``api`` end with
a 429 / 401 / 529 error result; ``inert`` fixes the bug but the plugin never acts (no hook event,
no session file); ``timeout`` fixes the bug, writes its state and exits 124 without a result (the
in-container ``timeout`` fired); ``leak`` writes the OAuth token (or an ``sk-ant-oat`` look-alike)
into its transcript; ``crash`` dies after ``system/init`` (the backend then writes nothing more);
``symlink`` fixes the bug and replaces ``$TOKEN_SPAREN_HOME`` with a link to ``/``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

BUGGY = "return sum(range(1, n))"
FIXED = "return sum(range(1, n + 1))"
VERSION = "0.0.0-fake"
FIXES = ("success", "inert", "timeout", "leak", "symlink")
ERRORS = {
    "quota": (429, "API Error: 429 You've hit your limit"),
    "auth": (401, "API Error: 401 OAuth token has expired"),
    "api": (529, "API Error: 529 overloaded_error"),
}
TS_HOOK = 'python3 -I "{}/hooks/run.py" SessionStart'


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _model(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1] if "--model" in argv[:-1] else "fake-model"


def _plugin_dir(argv: list[str]) -> str | None:
    return argv[argv.index("--plugin-dir") + 1] if "--plugin-dir" in argv[:-1] else None


def _token_sparen_acts(session: str, plugin: str) -> None:
    _emit(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SessionStart",
            "hook_name": "SessionStart:startup",
            "command": TS_HOOK.format(plugin),
            "exit_code": 0,
            "outcome": "success",
            "session_id": session,
        }
    )
    home = os.environ.get("TOKEN_SPAREN_HOME")
    if home:
        path = Path(home) / "projects" / ("0" * 16) / "sessions" / f"{session}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")


def _transcript(session: str, message: Mapping[str, object], scenario: str) -> None:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config:
        return
    transcript = Path(config) / "projects" / "fake" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"type": "assistant", "sessionId": session, "message": message}) + "\n"
    if scenario == "leak":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "sk-ant-oat01-" + "x" * 24
        text += json.dumps({"type": "user", "text": token}) + "\n"
    transcript.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    scenario = os.environ.get("TSB_FAKE_SCENARIO", "success")
    session = str(uuid.uuid4())
    model = _model(args)
    plugin = _plugin_dir(args)
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "model": model,
            "claude_code_version": VERSION,
            "cwd": os.getcwd(),
            "tools": ["Bash", "Edit", "Read"],
            "mcp_servers": [],
            "plugins": [{"name": "token-sparen", "path": plugin}] if plugin else [],
        }
    )
    if scenario == "crash":
        return 137
    if plugin and scenario != "inert":
        _token_sparen_acts(session, plugin)
    calc = Path("calc.py")
    if scenario in FIXES and calc.is_file():
        calc.write_text(calc.read_text(encoding="utf-8").replace(BUGGY, FIXED), encoding="utf-8")
    usage = {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_creation_input_tokens": 400,
        "cache_read_input_tokens": 3000,
    }
    message = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": usage}
    _emit({"type": "assistant", "session_id": session, "message": message})
    _transcript(session, message, scenario)
    ts_home = os.environ.get("TOKEN_SPAREN_HOME")
    if scenario == "symlink" and ts_home:
        shutil.rmtree(ts_home, ignore_errors=True)
        os.symlink("/", ts_home)
    if scenario == "timeout":
        return 124
    status, error = ERRORS.get(scenario, (None, None))
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": error is not None,
            "session_id": session,
            "num_turns": 1,
            "duration_ms": 10,
            "total_cost_usd": 0.01,
            "usage": usage,
            "modelUsage": {model: {"costUSD": 0.01}},
            **({"result": error, "api_error_status": status} if error else {}),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
