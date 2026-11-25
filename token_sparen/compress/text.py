"""Generic line-based compressor: head/tail, error pinning, folding and relevance fill."""

from __future__ import annotations

import math
import operator
import re
from bisect import bisect_right
from itertools import compress, islice

from token_sparen.compress import relevance
from token_sparen.compress.base import (
    CompressContext,
    CompressResult,
    ContentType,
    Level,
    encode_lossless,
    error_line_indices,
)

FOLD_MIN = 3
PIN_CONTEXT = 2
SCORE_CAP = 20_000
_DIGITS = b"0123456789"
# Digit runs normalise to 0xFF, a byte that never occurs in UTF-8, so a literal `#` in the
# output is never confused with a number.
_MARK = b"\xff"
_DIGIT_TABLE = bytes.maketrans(_DIGITS, _MARK * 10)
_DIGIT_RUN = re.compile(rb"\d+")


def elided_line(n: int) -> str:
    return f"… [{n} lines elided]"


def folded_line(line: str, n: int) -> str:
    return f"{line}  [× {n} similar lines]"


_FLIP = bytes.maketrans(b"\x00\x01", b"\x01\x00")


def _or(a: bytes | bytearray, b: bytes | bytearray) -> bytes:
    """Element-wise OR of two equal-length 0/1 masks (big-int arithmetic, C speed)."""
    return (int.from_bytes(a, "big") | int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _and_not(a: bytes | bytearray, b: bytes | bytearray) -> bytes:
    """Element-wise `a and not b` of two equal-length 0/1 masks."""
    return (int.from_bytes(a, "big") & ~int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _neighbour_equal(raw: bytes) -> bytearray:
    """eq[i] = 1 iff lines i and i+1 are equal once every digit run is replaced by one marker.

    Exact two-level C-speed test: equal-after-deleting-digits is necessary; equal-after-mapping
    each digit to a marker is sufficient; only pairs in between (digit runs of different
    lengths, e.g. `step 9` / `step 10`) get a per-pair regex check.
    """
    stripped = raw.translate(None, _DIGITS).split(b"\n")
    eq = bytearray(map(operator.eq, stripped, islice(stripped, 1, None)))
    if 1 not in eq:
        return eq
    marked = raw.translate(_DIGIT_TABLE).split(b"\n")
    sure = bytearray(map(operator.eq, marked, islice(marked, 1, None)))
    unsure = _and_not(eq, sure)
    pos = unsure.find(1)
    if pos == -1:
        return eq
    lines = raw.split(b"\n")
    while pos != -1:
        same = _DIGIT_RUN.sub(_MARK, lines[pos]) == _DIGIT_RUN.sub(_MARK, lines[pos + 1])
        eq[pos] = 1 if same else 0
        pos = unsure.find(1, pos + 1)
    return eq


def _fold_runs(raw: bytes, errors: list[int]) -> tuple[list[int], list[int]]:
    """Return (starts, ends) of runs of >= FOLD_MIN lines equal after digit normalisation.

    Error lines never take part in a run. Equality flags are packed into bytes so run
    boundaries are found with C-level `bytes.find` instead of a per-line Python loop.
    """
    eq = _neighbour_equal(raw)  # eq[i]: line i ~ line i+1
    for e in errors:
        if e < len(eq):
            eq[e] = 0
        if e > 0:
            eq[e - 1] = 0
    starts: list[int] = []
    ends: list[int] = []
    pos = eq.find(b"\x01" * (FOLD_MIN - 1))
    while pos != -1:
        stop = eq.find(b"\x00", pos)
        end = (stop if stop != -1 else len(eq)) + 1  # exclusive line index
        starts.append(pos)
        ends.append(end)
        pos = eq.find(b"\x01" * (FOLD_MIN - 1), end)
    return starts, ends


def _free_units(keep: bytearray, covered: bytearray) -> list[int]:
    """Indices that are neither kept nor folded into a run (C-level, no per-line Python)."""
    return list(compress(range(len(keep)), _or(keep, covered).translate(_FLIP)))


class TextCompressor:
    name = "text"
    handles: frozenset[ContentType] = frozenset({"text"})

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        body, trailer = (text[:-1], "\n") if text.endswith("\n") else (text, "")
        if not text:
            return CompressResult(text=text, kept_lines=0, total_lines=0, lossless=True)
        lines = body.split("\n")
        total = len(lines)
        if total <= level.head + level.tail:
            return CompressResult(text=text, kept_lines=total, total_lines=total, lossless=True)
        budget = max(level.head + level.tail, math.ceil(round(level.keep_ratio * total, 9)))

        raw = encode_lossless(body)
        errors = error_line_indices(body, lines, raw)
        starts, ends = _fold_runs(raw, errors)
        covered = bytearray(total)  # 1 = folded into a run whose first line represents it
        for s, e in zip(starts, ends, strict=True):
            covered[s + 1 : e] = b"\x01" * (e - s - 1)

        # keep[i] = 1 marks unit i (a single line or a run's first line) as kept. Covered
        # positions may also be set by range fills; they are masked out at the end.
        keep = bytearray(total)

        def pin(i: int) -> None:
            keep[i if not covered[i] else starts[bisect_right(starts, i) - 1]] = 1

        for i in range(min(level.head, total)):
            pin(i)
        for i in range(max(total - level.tail, 0), total):
            pin(i)
        for e in errors:
            for i in range(max(e - PIN_CONTEXT, 0), min(e + PIN_CONTEXT + 1, total)):
                pin(i)

        kept_units = _and_not(keep, covered)
        remaining = budget - kept_units.count(1)
        if remaining > 0:
            self._fill(lines, covered, keep, remaining, ctx)
            kept_units = _and_not(keep, covered)
        kept_count = kept_units.count(1)
        if kept_count == total:
            return CompressResult(text=text, kept_lines=total, total_lines=total, lossless=True)
        out = _assemble(lines, kept_units, starts, ends)
        return CompressResult(
            text="\n".join(out) + trailer,
            kept_lines=kept_count,
            total_lines=total,
            lossless=False,
        )

    @staticmethod
    def _fill(
        lines: list[str],
        covered: bytearray,
        keep: bytearray,
        remaining: int,
        ctx: CompressContext,
    ) -> None:
        """Spend `remaining` budget: relevant lines first, then extend head and tail inward."""
        free = _free_units(keep, covered)
        if relevance.query_terms(ctx):
            # Score only the first SCORE_CAP candidate units; the rest count as score 0.
            scored = free[:SCORE_CAP]
            scores = relevance.Corpus([lines[c] for c in scored], ctx).score_all()
            # best score first, ties broken by position
            ranked = sorted((-sc, c) for sc, c in zip(scores, scored, strict=True) if sc > 0.0)
            ranked = ranked[:remaining]
            if ranked:
                for _, c in ranked:
                    keep[c] = 1
                remaining -= len(ranked)
                free = _free_units(keep, covered)
        if remaining <= 0:
            return
        if remaining >= len(free):
            keep[:] = b"\x01" * len(keep)
            return
        front = (remaining + 1) // 2
        back = remaining - front
        # Every free unit up to free[front-1] is taken; other positions in that range are
        # already kept or covered, so a slice fill is exact once covered lines are masked.
        keep[: free[front - 1] + 1] = b"\x01" * (free[front - 1] + 1)
        if back:
            first = free[len(free) - back]
            keep[first:] = b"\x01" * (len(keep) - first)


def _assemble(lines: list[str], kept: bytes, starts: list[int], ends: list[int]) -> list[str]:
    """Kept units in order; folded runs annotated; each elided gap becomes one marker line."""
    total = len(lines)
    shown = bytearray(kept)  # 1 = line represented in the output (kept, or folded into a kept run)
    kept_runs: list[tuple[int, int]] = []
    for s, e in zip(starts, ends, strict=True):
        if kept[s]:
            shown[s + 1 : e] = b"\x01" * (e - s - 1)
            kept_runs.append((s, e))
    out: list[str] = []
    run_i = 0
    pos = 0
    while pos < total:
        gap = shown.find(0, pos)
        seg_end = total if gap == -1 else gap
        # emit shown segment [pos, seg_end), folding kept runs inside it
        while pos < seg_end:
            if run_i < len(kept_runs) and kept_runs[run_i][0] < seg_end:
                s, e = kept_runs[run_i]
                out.extend(lines[pos:s])
                out.append(folded_line(lines[s], e - s))
                pos = e
                run_i += 1
            else:
                out.extend(lines[pos:seg_end])
                pos = seg_end
        if gap == -1:
            break
        nxt = shown.find(1, gap)
        gap_end = total if nxt == -1 else nxt
        out.append(elided_line(gap_end - gap))
        pos = gap_end
    return out
