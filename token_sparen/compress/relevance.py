"""BM25-lite relevance of output lines to the current prompt and recently touched paths."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from itertools import chain, compress
from pathlib import PurePath

from token_sparen.compress.base import CompressContext

K1 = 1.2
B = 0.75
MAX_TERMS = 64

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    "a an and are as at be but by can do does for from how i if in is it me my of on or so "
    "that the this to was what when where which who why will with you".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1 and t not in _STOP]


def query_terms(ctx: CompressContext) -> frozenset[str]:
    """Prompt tokens plus tokens of the stems of recent paths' basenames."""
    terms: list[str] = []
    if ctx.prompt:
        terms.extend(_tokens(ctx.prompt))
    for path in ctx.recent_paths:
        terms.extend(_tokens(PurePath(path.replace("\\", "/")).stem))
    return frozenset(sorted(set(terms), key=lambda t: (-len(t), t))[:MAX_TERMS])


class Corpus:
    """BM25 statistics over a set of lines (each line is one document).

    Document length is measured in characters (BM25 only uses the dl/avgdl ratio), and hits
    are found with a single scan over the joined block rather than one regex call per line.
    """

    def __init__(self, lines: Sequence[str], ctx: CompressContext) -> None:
        self.terms = query_terms(ctx)
        self._lines = lines
        self._matcher: re.Pattern[str] | None = None
        self._n = max(len(lines), 1)
        self._avgdl = 1.0
        self._idf: dict[str, float] = {}
        self._doc_hits: dict[int, list[str]] = {}
        if not self.terms:
            return
        alts = "|".join(re.escape(t) for t in sorted(self.terms, key=lambda t: (-len(t), t)))
        self._matcher = re.compile(rf"(?<![a-z0-9_])(?:{alts})(?![a-z0-9_])")
        blob = "\n".join(lines)
        self._avgdl = max((len(blob) - len(lines) + 1) / self._n, 1.0)
        per_line = list(map(self._matcher.findall, blob.lower().split("\n")))
        hit_lists = list(compress(per_line, per_line))
        self._doc_hits = dict(zip(compress(range(len(per_line)), per_line), hit_lists, strict=True))
        df = Counter(chain.from_iterable(map(set, hit_lists)))
        self._idf = {term: self._idf_of(df.get(term, 0)) for term in self.terms}

    def _idf_of(self, df: int) -> float:
        return math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)

    def score_all(self) -> list[float]:
        """Scores of the corpus's own lines, in order (reuses the indexing pass)."""
        out = [0.0] * len(self._lines)
        lines, idf, avgdl = self._lines, self._idf, self._avgdl
        k1p = K1 + 1.0
        for idx, hits in self._doc_hits.items():
            if len(hits) == 1:  # fast path: the common single-hit line
                out[idx] = idf[hits[0]] * k1p / (1.0 + K1 * (1.0 - B + B * len(lines[idx]) / avgdl))
            else:
                out[idx] = self._bm25(hits, len(lines[idx]))
        return out

    def score(self, line: str) -> float:
        if self._matcher is None:
            return 0.0
        hits = self._matcher.findall(line.lower())
        return self._bm25(hits, len(line)) if hits else 0.0

    def _bm25(self, hits: list[str], dl: int) -> float:
        norm = K1 * (1.0 - B + B * dl / self._avgdl)
        if len(hits) == 1:
            return self._idf[hits[0]] * (K1 + 1.0) / (1.0 + norm)
        total = 0.0
        for term, tf in Counter(hits).items():
            total += self._idf[term] * tf * (K1 + 1.0) / (tf + norm)
        return total


def score(line: str, ctx: CompressContext, corpus: Corpus | None = None) -> float:
    """BM25-lite score of `line`; without a corpus the line is scored as a one-line corpus."""
    return (corpus or Corpus([line], ctx)).score(line)
