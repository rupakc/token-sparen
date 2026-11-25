"""Compression framework: types, levels, error signatures and the compressor registry."""

from __future__ import annotations

import math
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
    re.compile(r"(?:Error|Exception)\b", re.ASCII),  # CamelCase names: ValueError, IOException
    re.compile("✗"),
)


def _anchored(literal: str, suffix: str = "") -> re.Pattern[bytes]:
    """Byte twin of one ERROR_PATTERNS[0] alternative, for lower-cased UTF-8 text.

    The pattern starts with a literal so the regex engine uses its fast literal-prefix
    search; the word boundary before the literal is checked by a look-behind placed after
    it. Non-word false positives (e.g. "terror") are rejected inside the engine, never in
    Python.
    """
    lit = re.escape(literal)
    return re.compile(rf"{lit}(?<![a-z0-9_]{lit}){suffix}(?![a-z0-9_])".encode())


# Byte-level scanners whose union matches exactly the lines ERROR_PATTERNS matches (ASCII
# word semantics on both sides; property-tested). First group runs on lower-cased bytes.
_LOWER_SCANNERS: tuple[re.Pattern[bytes], ...] = (
    _anchored("error", "s?"),
    _anchored("exception", "s?"),
    _anchored("traceback"),
    _anchored("fail", "(?:s|ed|ure|ures|ing)?"),
    _anchored("fatal"),
    _anchored("panic", "(?:ked)?"),
    _anchored("assert", "(?:ion)?"),
)
_RAW_SCANNERS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"Error(?![A-Za-z0-9_])"),
    re.compile(rb"Exception(?![A-Za-z0-9_])"),
    re.compile("✗".encode()),
)


def is_error_line(line: str) -> bool:
    return any(p.search(line) for p in ERROR_PATTERNS)


def encode_lossless(text: str) -> bytes:
    """UTF-8 encode any str (lone surrogates included); newlines map 1:1 to b"\\n"."""
    return text.encode("utf-8", "surrogatepass")


def error_line_indices(
    text: str, raw: bytes | None = None, stop_at: int | None = None
) -> list[int]:
    """Sorted 0-based indices of lines (split on newline) matching ERROR_PATTERNS.

    With `stop_at`, scanning stops as soon as that many error lines are known; the result is
    then incomplete (callers use it to detect error-dense output cheaply). `raw` may pass the
    already encoded text to avoid a second encode.
    """
    data = raw if raw is not None else encode_lossless(text)
    low = data.lower()  # ASCII-only lower-casing: same length, same newline offsets
    found: set[int] = set()
    for scanners, hay in ((_LOWER_SCANNERS, low), (_RAW_SCANNERS, data)):
        for pat in scanners:
            m = pat.search(hay)
            idx = 0
            last = 0
            while m is not None:
                pos = m.start()
                idx += hay.count(b"\n", last, pos)
                last = pos
                found.add(idx)
                if stop_at is not None and len(found) >= stop_at:
                    return sorted(found)
                nxt = hay.find(b"\n", pos)
                if nxt == -1:
                    break
                m = pat.search(hay, nxt)
    return sorted(found)


@dataclass(frozen=True)
class Level:
    head: int
    tail: int
    keep_ratio: float


def _parse_level(raw: Any) -> Level:
    """Strict validation: integer head, tail >= 0 and finite 0 <= keep_ratio <= 1."""
    if not isinstance(raw, dict):
        raise ValueError("not a table")
    head: Any = raw.get("head")
    tail: Any = raw.get("tail")
    ratio: Any = raw.get("keep_ratio")
    for key, val in (("head", head), ("tail", tail)):
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ValueError(f"{key} must be an integer >= 0, got {val!r}")
    if (
        not isinstance(ratio, int | float)
        or isinstance(ratio, bool)
        or not math.isfinite(ratio)
        or not 0.0 <= ratio <= 1.0
    ):
        raise ValueError(f"keep_ratio must be a finite number in [0, 1], got {ratio!r}")
    return Level(head=head, tail=tail, keep_ratio=float(ratio))


def load_level(config: Config, arm: str) -> Level:
    """Read `[compression.levels.<arm>]`.

    An invalid user value is logged and replaced by the packaged default for that arm (fail
    open). Only an arm with no valid packaged default (e.g. `off`) raises ValueError.
    """
    raw = config.get(f"compression.levels.{arm}")
    try:
        return _parse_level(raw)
    except ValueError as exc:
        if raw is not None:
            _config.log_error(os.environ, "compress.level", f"{arm}: {exc}")
    defaults = _config.packaged_defaults().get("compression", {}).get("levels", {})
    try:
        return _parse_level(defaults.get(arm))
    except ValueError as exc:
        raise ValueError(f"no compression level for arm {arm!r}") from exc


@dataclass(frozen=True)
class CompressContext:
    prompt: str | None
    recent_paths: Sequence[str]
    config: Config | None = None


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
