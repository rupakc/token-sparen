"""Fake `claude` for the bench dry run: no model, no network, no credential.

Invoked like ``claude -p <prompt> --output-format stream-json …`` in the task repository. It
prints a synthetic stream-json session (``system/init``, one assistant turn, ``result``), writes
a transcript under ``$CLAUDE_CONFIG_DIR/projects/`` and, for the ``success`` scenario, fixes the
fake task's off-by-one bug in ``calc.py``. ``TSB_FAKE_SCENARIO`` picks the behaviour
(``success`` by default, ``fail`` leaves the bug; the execution backend adds more scenarios).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

BUGGY = "return sum(range(1, n))"
FIXED = "return sum(range(1, n + 1))"
VERSION = "0.0.0-fake"


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")


def _model(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1] if "--model" in argv[:-1] else "fake-model"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    scenario = os.environ.get("TSB_FAKE_SCENARIO", "success")
    session = str(uuid.uuid4())
    model = _model(args)
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
            "plugins": [],
        }
    )
    calc = Path("calc.py")
    if scenario == "success" and calc.is_file():
        calc.write_text(calc.read_text(encoding="utf-8").replace(BUGGY, FIXED), encoding="utf-8")
    usage = {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_creation_input_tokens": 400,
        "cache_read_input_tokens": 3000,
    }
    message = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": usage}
    _emit({"type": "assistant", "session_id": session, "message": message})
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        transcript = Path(config) / "projects" / "fake" / f"{session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "sessionId": session, "message": message}
        transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "num_turns": 1,
            "duration_ms": 10,
            "total_cost_usd": 0.01,
            "usage": usage,
            "modelUsage": {model: {"costUSD": 0.01}},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
