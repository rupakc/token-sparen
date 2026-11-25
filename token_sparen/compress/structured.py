"""Structured compressors: JSON documents, unified diffs and stack traces.

All three are linear-time and fail soft: malformed or unrecognised input is handed to the
generic `TextCompressor`, and no input makes them raise.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from token_sparen.compress._shared import (
    arm,
    guarded,
    setting,
    split_lines,
    unchanged,
)
from token_sparen.compress.base import (
    CompressContext,
    CompressResult,
    ContentType,
    Level,
    error_line_indices,
    is_error_line,
)
from token_sparen.compress.text import TextCompressor

_TEXT = TextCompressor()

MAX_JSON_KEYS = 50
MAX_JSON_STRING = 500
_SECTION = {"items": "json", "context": "diff", "edge_frames": "stacktrace"}


def _knob(key: str, level: Level, ctx: CompressContext) -> int:
    """Per-type knob for the arm nearest to `level` (see `[compression.<type>]` defaults)."""
    return setting(f"{_SECTION[key]}.{key}.{arm(level)}", ctx)


# ------------------------------------------------------------------ JSON


def _survey(root: Any) -> tuple[set[int], dict[int, int]]:
    """One bottom-up pass: ids of containers holding an error string, and pretty line counts."""
    order: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            order.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            order.append(node)
            stack.extend(node)
    errs: set[int] = set()
    lines: dict[int, int] = {}
    for node in reversed(order):
        children = node.values() if isinstance(node, dict) else node
        bad = isinstance(node, dict) and any(is_error_line(k) for k in node)
        count = 0
        for ch in children:
            if isinstance(ch, dict | list):
                count += lines[id(ch)]
                bad = bad or id(ch) in errs
            else:
                count += 1
                bad = bad or (isinstance(ch, str) and is_error_line(ch))
        lines[id(node)] = 1 if not children else count + 2
        if bad:
            errs.add(id(node))
    return errs, lines


class _JsonRender:
    def __init__(self, k: int, errs: set[int], lines: dict[int, int]) -> None:
        self.k = k
        self.errs = errs
        self.lines = lines
        self.out: list[str] = []
        self.markers = 0
        self.changed = False

    def _bad(self, v: Any) -> bool:
        if isinstance(v, dict | list):
            return id(v) in self.errs
        return isinstance(v, str) and is_error_line(v)

    def _scalar(self, v: Any) -> str:
        if isinstance(v, str) and len(v) > MAX_JSON_STRING and not is_error_line(v):
            self.changed = True
            cut = json_dumps(v[:MAX_JSON_STRING])[:-1]
            return f'{cut}…(+{len(v) - MAX_JSON_STRING} chars)"'
        return json_dumps(v)

    def _marker(self, pad: str, text: str) -> None:
        self.changed = True
        self.markers += 1
        self.out.append(f"{pad}{text}")

    def emit(self, v: Any, depth: int, lead: str, trail: str) -> None:
        pad = "  " * depth
        if not isinstance(v, dict | list):
            self.out.append(f"{pad}{lead}{self._scalar(v)}{trail}")
            return
        if not v:
            self.out.append(f"{pad}{lead}{'{}' if isinstance(v, dict) else '[]'}{trail}")
            return
        opener, closer = ("{", "}") if isinstance(v, dict) else ("[", "]")
        self.out.append(f"{pad}{lead}{opener}")
        if isinstance(v, dict):
            self._members(v, depth + 1)
        else:
            self._items(v, depth + 1)
        self.out.append(f"{pad}{closer}{trail}")

    def _items(self, arr: list[Any], depth: int) -> None:
        first_keys = list(arr[0].keys()) if isinstance(arr[0], dict) else None
        plan: list[tuple[bool, Any]] = []  # (is_marker, item or marker text)
        gap: list[Any] = []

        def flush() -> None:
            if not gap:
                return
            same = first_keys is not None and all(
                isinstance(g, dict) and list(g.keys()) == first_keys for g in gap
            )
            plan.append((True, f"… {len(gap)} more items{' with same keys' if same else ''}"))
            gap.clear()

        for i, item in enumerate(arr):
            if i < self.k or self._bad(item):
                flush()
                plan.append((False, item))
            else:
                gap.append(item)
        flush()
        for j, (is_marker, payload) in enumerate(plan):
            if is_marker:
                self._marker("  " * depth, payload)
            else:
                self.emit(payload, depth, "", "," if j < len(plan) - 1 else "")

    def _members(self, obj: dict[str, Any], depth: int) -> None:
        plan: list[tuple[bool, Any]] = []
        gap = 0
        for i, (key, val) in enumerate(obj.items()):
            if i < MAX_JSON_KEYS or is_error_line(key) or self._bad(val):
                if gap:
                    plan.append((True, f"… {gap} more keys"))
                    gap = 0
                plan.append((False, (key, val)))
            else:
                gap += 1
        if gap:
            plan.append((True, f"… {gap} more keys"))
        for j, (is_marker, payload) in enumerate(plan):
            if is_marker:
                self._marker("  " * depth, payload)
            else:
                key, val = payload
                self.emit(val, depth, f"{json_dumps(key)}: ", "," if j < len(plan) - 1 else "")


def json_dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


class JsonCompressor:
    name = "json"
    handles: frozenset[ContentType] = frozenset({"json"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("json", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        cap = setting("json.max_bytes", ctx)
        if len(text) * 4 > cap and len(text.encode("utf-8", "surrogatepass")) > cap:
            return _TEXT.compress(text, level, ctx)
        try:
            doc = json.loads(text)
            if not isinstance(doc, dict | list):
                return _TEXT.compress(text, level, ctx)
            errs, lines = _survey(doc)
            render = _JsonRender(_knob("items", level, ctx), errs, lines)
            render.emit(doc, 0, "", "")
        except (ValueError, RecursionError, MemoryError):
            return _TEXT.compress(text, level, ctx)
        total = lines[id(doc)]
        if not render.changed:
            return unchanged(text, total)
        trailer = "\n" if text.endswith("\n") else ""
        return CompressResult(
            text="\n".join(render.out) + trailer,
            kept_lines=len(render.out) - render.markers,
            total_lines=total,
            lossless=False,
        )


# ------------------------------------------------------------------ diff

_HUNK = re.compile(r"@@ -\d{1,9}(?:,(\d{1,9}))? \+\d{1,9}(?:,(\d{1,9}))? @@")


def unchanged_line(n: int) -> str:
    return f" … [{n} unchanged lines]"


def _gap(out: list[str], lines: list[str], a: int, b: int) -> int:
    """Append lines[a:b], or one marker when at least two are unchanged; returns markers added."""
    if b - a >= 2:
        out.append(unchanged_line(b - a))
        return 1
    out.extend(lines[a:b])
    return 0


class DiffCompressor:
    name = "diff"
    handles: frozenset[ContentType] = frozenset({"diff"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("diff", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        hid = [0] * total  # hunk id of context-capable body lines, 0 elsewhere
        is_ctx = [False] * total
        hunks = 0
        rem_old = rem_new = 0
        for i, line in enumerate(lines):
            if rem_old > 0 or rem_new > 0:
                lead_ch = line[:1]
                if lead_ch == " " or line == "":
                    rem_old -= 1
                    rem_new -= 1
                    is_ctx[i] = True
                    hid[i] = hunks
                    continue
                if lead_ch == "-":
                    rem_old -= 1
                    hid[i] = hunks
                    continue
                if lead_ch == "+":
                    rem_new -= 1
                    hid[i] = hunks
                    continue
                if lead_ch == "\\":
                    hid[i] = hunks
                    continue
                rem_old = rem_new = 0  # malformed counts: stop trusting this hunk
            m = _HUNK.match(line)
            if m is not None:
                hunks += 1
                rem_old = int(m.group(1)) if m.group(1) is not None else 1
                rem_new = int(m.group(2)) if m.group(2) is not None else 1
        if hunks == 0 and not any(ln.startswith("diff --git") for ln in lines):
            return _TEXT.compress(text, level, ctx)

        c = _knob("context", level, ctx)
        errors = set(error_line_indices(text)) if any(is_ctx) else set()
        out: list[str] = []
        markers = 0
        i = 0
        while i < total:
            if not is_ctx[i]:
                out.append(lines[i])
                i += 1
                continue
            j = i
            while j < total and is_ctx[j] and hid[j] == hid[i]:
                j += 1
            lead = i == 0 or hid[i - 1] != hid[i]
            trail = j == total or hid[j] != hid[i]
            first = 0 if lead else c
            last = 0 if trail else c
            if j - i <= first + last:
                out.extend(lines[i:j])
            else:
                lo, hi = i + first, j - last
                out.extend(lines[i:lo])
                pos = lo
                for e in range(lo, hi):
                    if e in errors:
                        markers += _gap(out, lines, pos, e)
                        out.append(lines[e])
                        pos = e + 1
                markers += _gap(out, lines, pos, hi)
                out.extend(lines[hi:j])
            i = j
        if markers == 0:
            return unchanged(text, total)
        return CompressResult(
            text="\n".join(out) + trailer,
            kept_lines=len(out) - markers,
            total_lines=total,
            lossless=False,
        )


# ------------------------------------------------------------------ stack traces

_PY_HEADER = "Traceback (most recent call last):"
_PY_FRAME = re.compile(r'\s+File "[^"]*", line \d+')
_JVM_EXTRA = re.compile(r"\s*(?:Caused by:|Suppressed:|\.\.\. \d+ (?:more|common frames omitted))")
_LIBRARY = re.compile(
    r"site-packages|dist-packages|node_modules|<frozen"
    r"|^\s*at\s+(?:java\.|javax\.|jdk\.internal\.|sun\.)"
)
_AT_POSITION = re.compile(r".+:\d+(?::\d+)?")


def _is_at_frame(line: str) -> bool:
    if not line or line[0] not in " \t":
        return False
    s = line.lstrip()
    if not s.startswith("at ") or len(s) < 4:
        return False
    if s.endswith(")") and "(" in s:
        return True
    return _AT_POSITION.fullmatch(s.rsplit(None, 1)[-1]) is not None


def _basenames(paths: Any) -> list[str]:
    names = (os.path.basename(str(p).rstrip("/\\")) for p in paths)
    return [n for n in names if n]


class StackTraceCompressor:
    name = "stacktrace"
    handles: frozenset[ContentType] = frozenset({"stacktrace"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("stacktrace", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        # segments: ("T", text lines) | ("K", [line]) kept verbatim | ("F", frame units)
        segs: list[tuple[str, list[Any]]] = []
        n = total
        i = 0
        while i < n:
            line = lines[i]
            if line.startswith(_PY_HEADER):
                segs.append(("K", [line]))
                i += 1
                units: list[list[str]] = []
                while i < n:
                    if _PY_FRAME.match(lines[i]):
                        units.append([lines[i]])
                    elif units and lines[i][:1] in (" ", "\t"):
                        units[-1].append(lines[i])
                    else:
                        break
                    i += 1
                if units:
                    segs.append(("F", units))
                if i < n and lines[i] and lines[i][0] not in " \t":
                    segs.append(("K", [lines[i]]))
                    i += 1
            elif _is_at_frame(line):
                if segs and segs[-1][0] == "T":
                    head = segs[-1][1].pop()
                    if not segs[-1][1]:
                        segs.pop()
                    segs.append(("K", [head]))
                units = []
                while i < n and _is_at_frame(lines[i]):
                    units.append([lines[i]])
                    i += 1
                segs.append(("F", units))
            elif _JVM_EXTRA.match(line):
                segs.append(("K", [line]))
                i += 1
            else:
                if segs and segs[-1][0] == "T":
                    segs[-1][1].append(line)
                else:
                    segs.append(("T", [line]))
                i += 1
        if not any(kind == "F" for kind, _ in segs):
            return _TEXT.compress(text, level, ctx)

        edge = _knob("edge_frames", level, ctx)
        names = _basenames(ctx.recent_paths)
        out: list[str] = []
        kept = 0
        for kind, body in segs:
            if kind == "K":
                out.extend(body)
                kept += len(body)
            elif kind == "T":
                res = _TEXT.compress("\n".join(body), level, ctx)
                out.append(res.text)
                kept += res.kept_lines
            else:
                kept += self._frames(body, edge, names, out)
        result = "\n".join(out) + trailer
        if result == text:
            return unchanged(text, total)
        return CompressResult(text=result, kept_lines=kept, total_lines=total, lossless=False)

    @staticmethod
    def _frames(units: list[list[str]], edge: int, names: list[str], out: list[str]) -> int:
        m = len(units)
        kept = 0
        dropped = 0
        for idx, unit in enumerate(units):
            head = unit[0]
            keep = (
                idx < edge
                or idx >= m - edge
                or _LIBRARY.search(head) is None
                or any(nm in head for nm in names)
                or any(is_error_line(ln) for ln in unit)
            )
            if keep:
                if dropped:
                    out.append(f"  … [{dropped} library frames]")
                    dropped = 0
                out.extend(unit)
                kept += len(unit)
            else:
                dropped += 1
        if dropped:
            out.append(f"  … [{dropped} library frames]")
        return kept
