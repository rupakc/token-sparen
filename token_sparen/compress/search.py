"""Search-result, `pplx` and log compressors.

All three are linear-time and never raise: any failure is logged and the generic
`TextCompressor` handles the output. Lines matching `ERROR_PATTERNS` are never elided.
"""

from __future__ import annotations

import json
import re
from typing import Any

from token_sparen.compress import relevance
from token_sparen.compress._shared import _TEXT as _FALLBACK
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
    encode_lossless,
    error_line_indices,
)
from token_sparen.compress.text import SCORE_CAP, assemble, fold_runs

# ---------------------------------------------------------------- search

_MATCH = re.compile(r"([^\s:][^:]*):(\d+):")  # path:line: (the optional column stays in the text)
_CONTEXT = re.compile(r"(.+?)-(\d+)-")
_SEPARATOR = "--"


def _nearest(entries: list[tuple[int, str, int]], kept: set[int]) -> set[int]:
    """Context entries (line indices) whose nearest match, within the same block, is kept."""
    n = len(entries)
    prev: list[tuple[int, int] | None] = [None] * n  # (line number, line index) of match before
    nxt: list[tuple[int, int] | None] = [None] * n
    last: tuple[int, int] | None = None
    for p, (idx, kind, ln) in enumerate(entries):
        if kind == "s":
            last = None
        elif kind == "m":
            last = (ln, idx)
        prev[p] = last
    last = None
    for p in range(n - 1, -1, -1):
        _, kind, ln = entries[p]
        if kind == "s":
            last = None
        elif kind == "m":
            last = (ln, entries[p][0])
        nxt[p] = last
    out: set[int] = set()
    for p, (idx, kind, ln) in enumerate(entries):
        if kind != "c":
            continue
        before, after = prev[p], nxt[p]
        pick = after
        if before is not None and (after is None or ln - before[0] <= after[0] - ln):
            pick = before
        if pick is not None and pick[1] in kept:
            out.add(idx)
    return out


class SearchCompressor:
    name = "search"
    handles: frozenset[ContentType] = frozenset({"search"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("search", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        quota = setting(f"search.per_file.{arm(level)}", ctx)

        parsed: list[tuple[str, str, int] | None] = [None] * total  # (kind, path, line number)
        match_paths: set[str] = set()
        for i, line in enumerate(lines):
            m = _MATCH.match(line)
            if m is not None:
                parsed[i] = ("m", m.group(1), int(m.group(2)))
                match_paths.add(m.group(1))
        if not match_paths:
            return unchanged(text, total)

        files: dict[str, list[tuple[int, str, int]]] = {}  # path -> (line idx, kind, number)
        order: list[tuple[str, str | int]] = []  # ("f", path) or ("o", line idx), first-seen
        current: str | None = None
        has_sep = False
        for i, line in enumerate(lines):
            entry = parsed[i]
            if entry is None:
                c = _CONTEXT.match(line)
                if c is not None and c.group(1) in match_paths:
                    entry = ("c", c.group(1), int(c.group(2)))
                elif line == _SEPARATOR and current is not None:
                    entry = ("s", current, 0)
                    has_sep = True
            if entry is None:
                order.append(("o", i))
                continue
            kind, path, number = entry
            current = path
            if path not in files:
                files[path] = []
                order.append(("f", path))
            files[path].append((i, kind, number))

        err = set(error_line_indices(text))
        scores = self._scores(lines, files, ctx)
        kept: set[int] = set()
        dropped: dict[str, int] = {}
        for path, entries in files.items():
            matches = [idx for idx, kind, _ in entries if kind == "m"]
            pinned = [idx for idx in matches if idx in err]
            rest = [idx for idx in matches if idx not in err]
            room = max(quota - len(pinned), 0)
            if scores is not None:
                rest.sort(key=lambda idx: (-scores.get(idx, 0.0), idx))
            keep_here = set(pinned) | set(rest[:room])
            kept |= keep_here
            dropped[path] = len(matches) - len(keep_here)
            kept |= _nearest(entries, keep_here)

        out: list[str] = []
        shown = 0
        for kind, ref in order:
            if kind == "o":
                out.append(lines[int(ref)])
                shown += 1
                continue
            path = str(ref)
            prev_no: int | None = None
            for idx, _kind, number in files[path]:
                if idx not in kept:
                    continue
                if has_sep and prev_no is not None and number - prev_no > 1:
                    out.append(_SEPARATOR)
                prev_no = number
                out.append(lines[idx])
                shown += 1
            if dropped[path]:
                out.append(f"  … {dropped[path]} more matches in {path}")
        result = "\n".join(out) + trailer
        if len(result) >= len(text):
            return unchanged(text, total)
        return CompressResult(text=result, kept_lines=shown, total_lines=total, lossless=False)

    @staticmethod
    def _scores(
        lines: list[str], files: dict[str, list[tuple[int, str, int]]], ctx: CompressContext
    ) -> dict[int, float] | None:
        """Relevance of match lines to the prompt/recent paths; None when there is no query."""
        if not relevance.query_terms(ctx):
            return None
        idxs = [idx for entries in files.values() for idx, kind, _ in entries if kind == "m"]
        idxs = idxs[:SCORE_CAP]
        scored = relevance.Corpus([lines[i] for i in idxs], ctx).score_all()
        return dict(zip(idxs, scored, strict=True))


# ------------------------------------------------------------------ pplx

_WORD = re.compile(r"\w+")
NEAR_DUPLICATE = 0.8
_RESULT_KEYS = ("hits", "results", "web_results")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = value if isinstance(value, int | float) else json.dumps(value, ensure_ascii=False)
    return " ".join(str(value).split())


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    if len(words) < 3:
        return {tuple(words)}
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _near_duplicate(a: str, b: str) -> bool:
    """True when `a` is a substring of `b` (or vice versa) or their 3-shingle Jaccard >= 0.8."""
    wa, wb = _WORD.findall(a.lower()), _WORD.findall(b.lower())
    if not wa or not wb:
        return False
    ja, jb = " ".join(wa), " ".join(wb)
    if ja in jb or jb in ja:
        return True
    sa, sb = _shingles(wa), _shingles(wb)
    return len(sa & sb) / len(sa | sb) >= NEAR_DUPLICATE


def _pplx_results(doc: Any) -> tuple[str, list[dict[str, Any]]] | None:
    """The result list and the key it came from ("" for a top-level list)."""
    key, found = "", doc
    if isinstance(doc, dict):
        key, found = next(
            ((k, doc[k]) for k in _RESULT_KEYS if isinstance(doc.get(k), list)), ("", None)
        )
    if not isinstance(found, list) or not found:
        return None
    if not all(isinstance(r, dict) for r in found):
        return None
    return key, found


def _scalar(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class PplxCompressor:
    name = "pplx"
    handles: frozenset[ContentType] = frozenset({"pplx"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("pplx", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        try:
            doc = json.loads(text)
        except ValueError:
            return _FALLBACK.compress(text, level, ctx)
        found = _pplx_results(doc)
        if found is None:
            return _FALLBACK.compress(text, level, ctx)
        key, results = found
        k = setting(f"pplx.results.{arm(level)}", ctx)
        # The first k results plus every per-result error entry are shown in full.
        show = [i for i, r in enumerate(results) if i < k or "error" in r]
        out: list[str] = []
        if key == "hits":
            total = doc.get("total")
            of = total if isinstance(total, int) and not isinstance(total, bool) else len(results)
            out.append(f"pplx: showing {len(show)} of {of} hits")
        if isinstance(doc, dict):
            out.extend(
                f"{name}: {_scalar(v)}"
                for name, v in doc.items()
                if name != "total" and (v is None or isinstance(v, str | int | float | bool))
            )
        for i in show:
            out.extend(self._render(i + 1, results[i], level, ctx))
        shown = set(show)
        refs = [
            _clean(r.get("url")) or _clean(r.get("title")) or f"#{i + 1}"
            for i, r in enumerate(results)
            if i not in shown
        ]
        if refs:
            out.append(f"… {len(refs)} more results: {', '.join(refs)}")
        body, trailer = split_lines(text)
        total_lines = len(body)
        result = "\n".join(out) + trailer
        if len(result) >= len(text):
            return unchanged(text, total_lines)
        return CompressResult(
            text=result, kept_lines=len(out), total_lines=total_lines, lossless=False
        )

    @staticmethod
    def _render(i: int, res: dict[str, Any], level: Level, ctx: CompressContext) -> list[str]:
        title, url = _clean(res.get("title")), _clean(res.get("url"))
        date, source = _clean(res.get("date")), _clean(res.get("source"))
        head = " — ".join(p for p in (title, url) if p) or "(untitled)"
        if date:
            head += f" ({date})"
        if res.get("tokens_count") is not None:
            head += f" ({_clean(res['tokens_count'])} tok)"
        if source:
            head += f" · {source}"
        trust = res.get("trust")
        trust_level = _clean(trust.get("level")) if isinstance(trust, dict) else ""
        if trust_level:
            head += f" · trust: {trust_level}"
        lines = [f"[{i}] {head}"]
        if res.get("error") is not None:
            lines[0] += f" — error: {_clean(res['error'])}"
        summary, snippet = _clean(res.get("summary")), _clean(res.get("snippet"))
        if summary:
            lines.append(f"    {summary}")
        if snippet and not (summary and _near_duplicate(snippet, summary)):
            lines.append(f"    {snippet}")
        page = res.get("text")
        if isinstance(page, str) and page:
            squeezed = _FALLBACK.compress(page, level, ctx).text
            lines.extend(f"    {ln}" for ln in squeezed.rstrip("\n").split("\n"))
        return lines


# ------------------------------------------------------------------- log

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO 8601
    rf"|(?:{_MONTHS}) +\d{{1,2}} \d{{2}}:\d{{2}}:\d{{2}}"  # syslog
    r"|\[\d{9,10}(?:\.\d+)?\]"  # epoch
    r"|\d{2}:\d{2}:\d{2}[.,]\d{3}"  # HH:MM:SS.mmm
)
_LEVEL = re.compile(
    r"\b(?:WARN|WARNING|ERROR|FATAL|CRITICAL)\b"
    r"|(?i:\b(?:level|lvl|severity)\b[ ]*[=:][ ]*[\"']?(?:warn|warning|error|fatal|critical)\b)"
)


def _level_lines(text: str) -> list[int]:
    """Sorted 0-based indices of lines holding a WARN/ERROR/FATAL/CRITICAL level token."""
    found: list[int] = []
    idx = last = 0
    pos = 0
    while True:
        m = _LEVEL.search(text, pos)
        if m is None:
            return found
        idx += text.count("\n", last, m.start())
        last = m.start()
        found.append(idx)
        nl = text.find("\n", last)
        if nl == -1:
            return found
        pos = nl


class LogCompressor:
    name = "log"
    handles: frozenset[ContentType] = frozenset({"log"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        return guarded("log", self._run, text, level, ctx)

    def _run(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        lines, trailer = split_lines(text)
        total = len(lines)
        tail = setting("log.tail", ctx)
        tail_from = total - tail
        if tail_from <= 0:
            return unchanged(text, total)
        body = text[: len(text) - len(trailer)]
        errors = error_line_indices(body)
        raw = encode_lossless(_TIMESTAMP.sub("<ts>", body))
        starts, ends = fold_runs(raw, errors)
        keep = bytearray(total)
        for i in errors:
            keep[i] = 1
        for i in _level_lines(body):
            keep[i] = 1
        keep[tail_from:] = b"\x01" * tail
        runs_s: list[int] = []
        runs_e: list[int] = []
        for s, e in zip(starts, ends, strict=True):
            e = min(e, tail_from)  # the tail is kept line by line
            if e - s < 3:
                continue
            runs_s.append(s)
            runs_e.append(e)
            pinned = keep.find(1, s, e) != -1
            keep[s:e] = b"\x00" * (e - s)
            keep[s] = 1 if pinned else 0
        shown = assemble(lines, bytes(keep), runs_s, runs_e)
        result = "\n".join(shown) + trailer
        if len(result) >= len(text):
            return unchanged(text, total)
        return CompressResult(
            text=result, kept_lines=keep.count(1), total_lines=total, lossless=False
        )


__all__ = ["LogCompressor", "PplxCompressor", "SearchCompressor"]
