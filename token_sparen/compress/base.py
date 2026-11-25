"""Compression framework: types, levels, error signatures and the compressor registry."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Literal, Protocol, runtime_checkable

from token_sparen import config as _config
from token_sparen.config import Config

ContentType = Literal[
    "json",
    "diff",
    "stacktrace",
    "test_report",
    "compiler",
    "build_log",
    "search",
    "pplx",
    "log",
    "table",
    "text",
]

ENTRY_POINT_GROUP = "token_sparen.compressors"

# Error/failure signatures. Lines matching any of these are never elided.
# Simple alternations only (no nested quantifiers) so scanning stays linear.
ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:errors?|exceptions?|traceback|fail(?:s|ed|ure|ures|ing)?|fatal|panic(?:ked)?"
        r"|assert(?:ion)?)\b",
        # ASCII-only case folding keeps the byte-level anchor scan exact (no Unicode
        # look-alikes such as U+017F matching "s"); non-ASCII letters count as boundaries,
        # which can only pin more lines, never fewer.
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(r"(?:Error|Exception)\b"),  # CamelCase names: ValueError, IOException
    re.compile("✗"),
)
# Literal (lower-case, UTF-8) anchors: every ERROR_PATTERNS match contains one of them. Whole-text
# scanning uses C-level `bytes.find` on these, then verifies the candidate line with the regexes;
# this is several times faster than running the regexes over megabytes of output.
_ANCHORS: tuple[bytes, ...] = tuple(
    k.encode("utf-8")
    for k in ("error", "exception", "traceback", "fail", "fatal", "panic", "assert", "✗")
)


def is_error_line(line: str) -> bool:
    return any(p.search(line) for p in ERROR_PATTERNS)


def encode_lossless(text: str) -> bytes:
    """UTF-8 encode any str (lone surrogates included); newlines map 1:1 to b"\\n"."""
    return text.encode("utf-8", "surrogatepass")


def error_line_indices(
    text: str, lines: Sequence[str] | None = None, raw: bytes | None = None
) -> list[int]:
    """Sorted 0-based indices of lines (split on newline) matching ERROR_PATTERNS."""
    if lines is None:
        lines = text.split("\n")
    low = (raw if raw is not None else encode_lossless(text)).lower()  # ASCII-only, same length
    found: set[int] = set()
    rejected: set[int] = set()
    for anchor in _ANCHORS:
        pos = low.find(anchor)
        idx = 0
        last = 0
        while pos != -1:
            idx += low.count(b"\n", last, pos)
            last = pos
            if idx not in found and idx not in rejected:
                (found if is_error_line(lines[idx]) else rejected).add(idx)
            nxt = low.find(b"\n", pos)
            if nxt == -1:
                break
            pos = low.find(anchor, nxt)
    return sorted(found)


@dataclass(frozen=True)
class Level:
    head: int
    tail: int
    keep_ratio: float


def load_level(config: Config, arm: str) -> Level:
    """Read `[compression.levels.<arm>]`; raises ValueError for unknown/invalid arms."""
    raw = config.get(f"compression.levels.{arm}")
    if not isinstance(raw, dict):
        raise ValueError(f"no compression level for arm {arm!r}")
    try:
        return Level(
            head=int(raw["head"]), tail=int(raw["tail"]), keep_ratio=float(raw["keep_ratio"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid compression level {arm!r}: {exc}") from exc


@dataclass(frozen=True)
class CompressContext:
    prompt: str | None
    recent_paths: Sequence[str]


@dataclass(frozen=True)
class CompressResult:
    """`text` is the compressed body without the recovery marker."""

    text: str
    kept_lines: int
    total_lines: int
    lossless: bool


@runtime_checkable
class Compressor(Protocol):
    name: str
    handles: frozenset[ContentType]

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult: ...


def _default_entry_points() -> Iterable[Any]:
    return metadata.entry_points(group=ENTRY_POINT_GROUP)


class Registry:
    """Maps content types to compressors; plugin entry points load lazily on first lookup."""

    def __init__(self, entry_points_fn: Callable[[], Iterable[Any]] | None = None) -> None:
        self._by_type: dict[str, Compressor] = {}
        self._entry_points_fn = entry_points_fn or _default_entry_points
        self._loaded = False
        self._fallback: Compressor | None = None

    def register(self, c: Compressor) -> None:
        for t in c.handles:
            self._by_type[t] = c

    def for_type(self, t: ContentType) -> Compressor:
        if not self._loaded:
            self._loaded = True
            self._load_entry_points()
        found = self._by_type.get(t)
        if found is not None:
            return found
        if self._fallback is None:
            from token_sparen.compress.text import TextCompressor

            self._fallback = TextCompressor()
        return self._fallback

    def _load_entry_points(self) -> None:
        try:
            eps = list(self._entry_points_fn())
        except Exception as exc:  # noqa: BLE001 - plugin discovery must never break a hook
            _config.log_error(os.environ, "compress.registry", f"entry points: {exc}")
            return
        for ep in eps:
            name = getattr(ep, "name", "?")
            try:
                obj = ep.load()
                if isinstance(obj, type):
                    obj = obj()
                if not isinstance(obj, Compressor):
                    raise TypeError(f"{type(obj).__name__} is not a Compressor")
                self.register(obj)
            except Exception as exc:  # noqa: BLE001 - skip broken plugins
                _config.log_error(os.environ, "compress.registry", f"{name}: {exc}")


registry = Registry()
