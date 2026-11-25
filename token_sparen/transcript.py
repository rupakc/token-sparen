"""Streaming parser for Claude Code JSONL transcripts. Never raises on bad input."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen.estimate import Usage


@dataclass
class ToolResultRecord:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    turn_index: int = 0


def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _message(rec: dict[str, Any]) -> dict[str, Any]:
    msg = rec.get("message")
    return msg if isinstance(msg, dict) else {}


def iter_assistant_usage(
    path: Path, include_sidechains: bool = False
) -> Iterator[tuple[str, Usage, str | None]]:
    """Yield (message_id, usage, model) once per API response (last record wins)."""
    seen: dict[str, tuple[Usage, str | None]] = {}
    for rec in _records(path):
        if rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain") and not include_sidechains:
            continue
        msg = _message(rec)
        mid = msg.get("id")
        usage = msg.get("usage")
        model = msg.get("model")
        if not isinstance(mid, str) or not isinstance(usage, dict) or model == "<synthetic>":
            continue
        seen[mid] = (
            Usage(
                input=_int(usage.get("input_tokens")),
                cache_read=_int(usage.get("cache_read_input_tokens")),
                cache_write=_int(usage.get("cache_creation_input_tokens")),
                output=_int(usage.get("output_tokens")),
            ),
            model if isinstance(model, str) else None,
        )
    for mid, (u, model) in seen.items():
        yield mid, u, model


def session_burn(path: Path) -> int:
    """Sum of context tokens (input + cache_read + cache_write) over deduped calls."""
    return sum(u.input + u.cache_read + u.cache_write for _, u, _ in iter_assistant_usage(path))


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def iter_tool_results(path: Path) -> Iterator[ToolResultRecord]:
    """Yield tool results joined with their tool_use; turn_index counts user prompts so far."""
    uses: dict[str, tuple[str, dict[str, Any]]] = {}
    turn = 0
    for rec in _records(path):
        kind = rec.get("type")
        content = _message(rec).get("content")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    bid = block.get("id")
                    if isinstance(bid, str):
                        name = block.get("name")
                        inp = block.get("input")
                        uses[bid] = (
                            name if isinstance(name, str) else "",
                            inp if isinstance(inp, dict) else {},
                        )
        elif kind == "user":
            blocks = (
                [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            )
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if not results:
                if isinstance(content, str) or any(b.get("type") == "text" for b in blocks):
                    turn += 1
                continue
            for b in results:
                tid = b.get("tool_use_id")
                if not isinstance(tid, str):
                    continue
                name, inp = uses.get(tid, ("", {}))
                yield ToolResultRecord(tid, name, inp, _result_text(b.get("content")), turn)
