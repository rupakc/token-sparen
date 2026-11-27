"""Streaming parser for Claude Code JSONL transcripts. Never raises on bad input.

Every reader streams one record at a time (transcripts can be hundreds of MB). A read error
mid-file ends the iteration; a line that is not JSON, or nests too deeply to decode
(RecursionError), is skipped.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_sparen.estimate import Usage

MAX_OPEN_USES = 1024  # tool_use inputs awaiting their result (oldest dropped beyond this)
SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class ToolResultRecord:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    turn_index: int = 0
    is_error: bool = False
    raw: Any = None  # the record's ``toolUseResult`` (the host's raw response), when present
    sidechain: bool = False
    # Set by a replay that de-duplicates across files: the result is copied history (a resumed
    # session) already seen earlier in the run.
    duplicate: bool = False


def _bounded_lines(
    path: Path, max_bytes: int, on_limit: Callable[[], None] | None
) -> Iterator[str]:
    """Lines of the first ``max_bytes`` bytes (memory bounded: a longer line is cut and ends the
    read); ``on_limit`` is called once if the file has more."""
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        used = 0
        while True:
            try:
                room = max_bytes - used
                raw = fh.readline(room) if room > 0 else b""
                more = not raw and room <= 0 and fh.read(1) != b""
            except (OSError, ValueError):
                return
            if more or (raw and not raw.endswith(b"\n") and used + len(raw) >= max_bytes):
                if on_limit is not None:
                    on_limit()
                return
            if not raw:
                return
            used += len(raw)
            yield raw.decode("utf-8", errors="replace")


def _text_lines(path: Path) -> Iterator[str]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        while True:
            try:
                line = next(fh)
            except (StopIteration, OSError, ValueError):
                return
            yield line


def _records(
    path: Path, max_bytes: int | None = None, on_limit: Callable[[], None] | None = None
) -> Iterator[dict[str, Any]]:
    lines = _text_lines(path) if max_bytes is None else _bounded_lines(path, max_bytes, on_limit)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
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


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def is_prompt(rec: dict[str, Any]) -> bool:
    """A user prompt that starts a turn: a main-chain user record with text and no tool result.

    Sidechain (subagent) records, ``isMeta`` records (local-command caveats and the like) and
    compaction summaries are not prompts.
    """
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return False
    if rec.get("isCompactSummary"):
        return False
    content = _message(rec).get("content")
    if isinstance(content, str):
        return True
    blocks = _blocks(content)
    if any(b.get("type") == "tool_result" for b in blocks):
        return False
    return any(b.get("type") == "text" for b in blocks)


class _Uses:
    """tool_use id -> (name, input), bounded; an entry is dropped once its result is seen."""

    def __init__(self) -> None:
        self._uses: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

    def add(self, block: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        bid = block.get("id")
        if not isinstance(bid, str):
            return None
        name = block.get("name")
        inp = block.get("input")
        entry = (name if isinstance(name, str) else "", inp if isinstance(inp, dict) else {})
        self._uses[bid] = entry
        self._uses.move_to_end(bid)
        while len(self._uses) > MAX_OPEN_USES:
            self._uses.popitem(last=False)
        return bid, entry[0], entry[1]

    def take(self, tid: str) -> tuple[str, dict[str, Any]]:
        return self._uses.pop(tid, ("", {}))


def _results(
    rec: dict[str, Any], blocks: list[dict[str, Any]], uses: _Uses, turn: int
) -> Iterator[ToolResultRecord]:
    results = [b for b in blocks if b.get("type") == "tool_result"]
    # One raw response per record: it belongs to a result only when the record holds one.
    raw = rec.get("toolUseResult") if len(results) == 1 else None
    side = bool(rec.get("isSidechain"))
    for b in results:
        tid = b.get("tool_use_id")
        if not isinstance(tid, str):
            continue
        name, inp = uses.take(tid)
        text = _result_text(b.get("content"))
        yield ToolResultRecord(tid, name, inp, text, turn, b.get("is_error") is True, raw, side)


def iter_tool_results(
    path: Path, *, max_bytes: int | None = None, on_limit: Callable[[], None] | None = None
) -> Iterator[ToolResultRecord]:
    """Yield tool results joined with their tool_use; turn_index counts user prompts so far
    (``is_prompt``: sidechain, meta and compaction-summary records do not start a turn).
    ``max_bytes`` stops after that many bytes of the file (``on_limit`` is called if it had
    more), bounding memory too: a cut line is never decoded."""
    uses = _Uses()
    turn = 0
    for rec in _records(path, max_bytes, on_limit):
        kind = rec.get("type")
        content = _message(rec).get("content")
        if kind == "assistant":
            for block in _blocks(content):
                if block.get("type") == "tool_use":
                    uses.add(block)
        elif kind == "user":
            if is_prompt(rec):
                turn += 1
                continue
            yield from _results(rec, _blocks(content), uses, turn)


# --------------------------------------------------------------------------- session events


@dataclass(frozen=True)
class Prompt:
    """A user prompt of the session's chain (starts a turn)."""

    text: str
    turn: int


@dataclass(frozen=True)
class Compaction:
    """The context was compacted: everything before it is no longer re-sent."""


@dataclass(frozen=True)
class AssistantRecord:
    """One assistant record of the chain. ``new_call`` marks the first record of an API response
    (Claude Code writes one record per content block, all with the same message id)."""

    new_call: bool
    usage: Usage | None
    model: str | None
    texts: tuple[str, ...]
    tool_uses: tuple[tuple[str, str, dict[str, Any]], ...]


SessionEvent = Prompt | Compaction | AssistantRecord | ToolResultRecord


def is_compaction(rec: dict[str, Any]) -> bool:
    """Heuristic: a ``system`` record with subtype ``compact_boundary`` or a user record flagged
    ``isCompactSummary`` (Claude Code writes both around a compaction; consecutive markers merge
    because nothing is sent between them). ``type: summary`` records are session titles, not
    compactions, and are ignored."""
    if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
        return True
    return rec.get("type") == "user" and rec.get("isCompactSummary") is True


def _usage(msg: dict[str, Any]) -> Usage | None:
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return Usage(
        input=_int(usage.get("input_tokens")),
        cache_read=_int(usage.get("cache_read_input_tokens")),
        cache_write=_int(usage.get("cache_creation_input_tokens")),
        output=_int(usage.get("output_tokens")),
    )


def _prompt_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        b["text"]
        for b in _blocks(content)
        if b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def iter_events(
    path: Path, recent_ids: int = 4096, is_new_id: Callable[[str], bool] | None = None
) -> Iterator[SessionEvent]:
    """Stream one session's events in file order, for the file's own chain only.

    The chain is the ``isSidechain`` value of the first user/assistant record: a subagent
    transcript (all sidechain) replays as its own session, and inline sidechain records of a
    main transcript are skipped. Memory is bounded: open tool uses (``MAX_OPEN_USES``) and the
    last ``recent_ids`` message ids. ``is_new_id`` (records the id, True if unseen) replaces the
    per-file id memory, e.g. to count a resumed session's copied history only once per run.
    """
    return events_from_records(_records(path), recent_ids, is_new_id)


def events_from_records(
    records: Iterable[dict[str, Any]],
    recent_ids: int = 4096,
    is_new_id: Callable[[str], bool] | None = None,
) -> Iterator[SessionEvent]:
    """``iter_events`` over already parsed records (a caller that reads the file itself, e.g.
    with its own line limits and counters)."""
    chain: bool | None = None
    uses = _Uses()
    seen: OrderedDict[str, None] = OrderedDict()
    turn = 0
    for rec in records:
        kind = rec.get("type")
        if is_compaction(rec):
            yield Compaction()
            continue
        if kind not in ("user", "assistant"):
            continue
        side = bool(rec.get("isSidechain"))
        if chain is None:
            chain = side
        if side != chain:
            continue
        msg = _message(rec)
        content = msg.get("content")
        if kind == "assistant":
            mid = msg.get("id")
            model = msg.get("model")
            new = False
            if isinstance(mid, str) and model != SYNTHETIC_MODEL:
                if is_new_id is not None:
                    new = is_new_id(f"m:{mid}")
                elif mid not in seen:
                    seen[mid] = None
                    if len(seen) > recent_ids:
                        seen.popitem(last=False)
                    new = True
            blocks = _blocks(content)
            texts = tuple(
                b["text"]
                for b in blocks
                if b.get("type") == "text" and isinstance(b.get("text"), str)
            )
            added = [uses.add(b) for b in blocks if b.get("type") == "tool_use"]
            yield AssistantRecord(
                new,
                _usage(msg) if new else None,
                model if isinstance(model, str) else None,
                texts,
                tuple(u for u in added if u is not None),
            )
            continue
        if rec.get("isMeta") or rec.get("isCompactSummary"):
            continue
        blocks = _blocks(content)
        if any(b.get("type") == "tool_result" for b in blocks):
            yield from _results(rec, blocks, uses, turn)
        elif isinstance(content, str) or any(b.get("type") == "text" for b in blocks):
            turn += 1
            yield Prompt(_prompt_text(content), turn)


def first_field(path: Path, key: str, max_records: int = 50) -> Any:
    """The first non-empty value of ``key`` among the first ``max_records`` records (e.g. the
    session's ``timestamp`` or ``cwd``), else None."""
    for i, rec in enumerate(_records(path)):
        value = rec.get(key)
        if value not in (None, ""):
            return value
        if i + 1 >= max_records:
            break
    return None


# --------------------------------------------------------------------------- tool inventory
_NO_SUCH_TOOL = re.compile(r"No such tool available: ([A-Za-z0-9_.:-]+)")


@dataclass
class ToolInventory:
    """Which tools one transcript shows as available, used and attempted (tool pruning).

    ``listed`` is None when the transcript carries no tool list: Claude Code's interactive
    transcripts have no documented one, so it is read from the SDK's ``system``/``init`` record
    (``tools``) or from ``deferred_tools_delta`` attachments (``addedNames`` / ``removedNames``)
    when present. ``unknown`` holds tools the model called that the host did not have ("No such
    tool available: X"), i.e. attempted uses of a removed tool.
    """

    used: set[str] = field(default_factory=set)
    listed: set[str] | None = None
    always: set[str] | None = None  # listed by an SDK ``init`` record: loaded on every request
    prompts: int = 0
    unknown: set[str] = field(default_factory=set)

    def merge(self, other: ToolInventory) -> None:
        """Add a subagent transcript: its uses count, its prompts do not."""
        self.used |= other.used
        self.unknown |= other.unknown
        if other.listed is not None:
            self.listed = (self.listed or set()) | other.listed
        if other.always is not None:
            self.always = (self.always or set()) | other.always


def _names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = (
            item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
        )
        if isinstance(name, str) and name:
            out.append(name)
    return out


def add_inventory_record(inv: ToolInventory, rec: dict[str, Any]) -> None:
    """Fold one parsed transcript record into ``inv`` (what ``tool_inventory`` does per line;
    the E1 pruning lane feeds it the records the replay runner reads)."""
    kind = rec.get("type")
    if kind == "system" and rec.get("subtype") == "init" and "tools" in rec:
        names = set(_names(rec.get("tools")))
        inv.listed = (inv.listed or set()) | names
        inv.always = (inv.always or set()) | names
        return
    att = rec.get("attachment")
    if kind == "attachment" and isinstance(att, dict):
        if att.get("type") == "deferred_tools_delta":
            listed = (inv.listed or set()) | set(_names(att.get("addedNames")))
            inv.listed = listed - set(_names(att.get("removedNames")))
        return
    content = _message(rec).get("content")
    if kind == "assistant":
        for block in _blocks(content):
            name = block.get("name")
            if block.get("type") == "tool_use" and isinstance(name, str) and name:
                inv.used.add(name)
    elif kind == "user":
        if is_prompt(rec):
            inv.prompts += 1
            return
        for block in _blocks(content):
            if block.get("type") == "tool_result" and block.get("is_error") is True:
                inv.unknown.update(_NO_SUCH_TOOL.findall(_result_text(block.get("content"))))


def tool_inventory(
    path: Path, *, max_bytes: int | None = None, on_limit: Callable[[], None] | None = None
) -> ToolInventory:
    """Stream ``path`` once into a ``ToolInventory`` (never raises; bounded like ``_records``)."""
    inv = ToolInventory()
    for rec in _records(path, max_bytes, on_limit):
        add_inventory_record(inv, rec)
    return inv
