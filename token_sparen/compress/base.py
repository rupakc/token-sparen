"""Compression framework: types, levels, error signatures and the compressor registry."""

from __future__ import annotations

import contextlib
import math
import os
import re
import sys
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
        r"|assert(?:ion)?|segmentation fault|segfault(?:s|ed)?|permission denied|eacces"
        r"|access (?:is )?denied|undefined reference|command not found)\b",
        # ASCII-only case folding keeps the byte-level anchor scan exact (no Unicode
        # look-alikes such as U+017F matching "s"); non-ASCII letters count as boundaries,
        # which can only pin more lines, never fewer.
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(r"(?:Error|Exception)\b", re.ASCII),  # CamelCase names: ValueError, IOException
    re.compile(r"\bERR!", re.ASCII),  # npm ERR! (the npm failure prefix)
    re.compile(r"\bKilled\b", re.ASCII),  # a shell reporting a SIGKILLed job (OOM killer)
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
    _anchored("segmentation fault"),
    _anchored("segfault", "(?:s|ed)?"),
    _anchored("permission denied"),
    _anchored("eacces"),
    _anchored("access denied"),
    _anchored("access is denied"),
    _anchored("undefined reference"),
    _anchored("command not found"),
)
_RAW_SCANNERS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"Error(?![A-Za-z0-9_])"),
    re.compile(rb"Exception(?![A-Za-z0-9_])"),
    re.compile(rb"ERR!(?<![A-Za-z0-9_]ERR!)"),
    re.compile(rb"Killed(?<![A-Za-z0-9_]Killed)(?![A-Za-z0-9_])"),
    re.compile("✗".encode()),
)


# Stack-frame heads: Python ``File "<path>", line N`` and JS / JVM ``at [fn (]<path>:N[:M][)]``.
# Group 2 is the file location; a match only inside it (``requests/exceptions.py``,
# ``http-errors/index.js``) does not make the frame an error line.
_FRAME_HEAD = re.compile(
    r'^(\s*File ")([^"\n]*)(", line \d+)'
    r"|^(\s*at\s+(?:[^()\n]*\()?)([^()\s]+:\d+(?::\d+)?)(\)?\s*)$",
    re.ASCII,  # ASCII whitespace, as the byte-level pre-check ``_FRAME_START``
)
_FRAME_START = re.compile(rb'^\s*(?:File "|at\s)')
# Possible first bytes of a frame-head line (ASCII whitespace, ``F``, ``a``): a cheap filter
# so the common error line ("ERROR: ...") skips the frame check entirely.
_FRAME_FIRST = frozenset(b" \t\r\x0b\x0cFa")


def _matches(line: str) -> bool:
    return any(p.search(line) for p in ERROR_PATTERNS)


def is_error_line(line: str) -> bool:
    """Does ``line`` match ``ERROR_PATTERNS``? A stack-frame head counts only when it matches
    outside its file location (its path, file name and line number)."""
    if not _matches(line):
        return False
    m = _FRAME_HEAD.match(line)
    if m is None:
        return True
    loc = 2 if m.group(2) is not None else 5
    return _matches(line[: m.start(loc)] + line[m.end(loc) :])


def _frame_only(data: bytes, start: int, pos: int) -> bool:
    """Is the line of ``data`` starting at ``start`` (holding ``pos``) a frame head that
    matches only in its location?"""
    end = data.find(b"\n", pos)
    line = data[start : len(data) if end == -1 else end]
    if _FRAME_START.match(line) is None:
        return False
    return not is_error_line(line.decode("utf-8", "surrogatepass"))


def encode_lossless(text: str) -> bytes:
    """UTF-8 encode any str (lone surrogates included); newlines map 1:1 to b"\\n"."""
    return text.encode("utf-8", "surrogatepass")


def error_line_indices(
    text: str, raw: bytes | None = None, stop_at: int | None = None
) -> list[int]:
    """Sorted 0-based indices of lines (split on newline) that are error lines
    (``is_error_line``: ERROR_PATTERNS, frame heads judged without their file location).

    With `stop_at`, scanning stops as soon as that many error lines are known; the result is
    then incomplete (callers use it to detect error-dense output cheaply). `raw` may pass the
    already encoded text to avoid a second encode.
    """
    data = raw if raw is not None else encode_lossless(text)
    low = data.lower()  # ASCII-only lower-casing: same length, same newline offsets
    found: set[int] = set()
    frames: set[int] = set()  # frame heads that match only in their file location
    for scanners, hay in ((_LOWER_SCANNERS, low), (_RAW_SCANNERS, data)):
        for pat in scanners:
            m = pat.search(hay)
            idx = 0
            last = 0
            while m is not None:
                pos = m.start()
                idx += hay.count(b"\n", last, pos)
                last = pos
                if idx not in found and idx not in frames:
                    start = data.rfind(b"\n", 0, pos) + 1  # start <= pos < len(data)
                    if data[start] in _FRAME_FIRST and _frame_only(data, start, pos):
                        frames.add(idx)
                    else:
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


def is_compressor(obj: Any) -> bool:
    """Structural check stricter than the Protocol: str name, frozenset of str handles."""
    if not isinstance(obj, Compressor):
        return False
    handles = getattr(obj, "handles", None)
    return (
        isinstance(getattr(obj, "name", None), str)
        and isinstance(handles, frozenset)
        and all(isinstance(t, str) for t in handles)
    )


def line_count(text: str) -> int:
    """Lines of ``text`` by the built-in rule: split on newline, one trailing newline ignored."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# Elision/summary marker lines the built-ins emit; they never count as kept input lines.
_MARKER_LINE = re.compile(r"^\s*(?:…|\[\d+ (?:passed tests|warnings) elided)", re.ASCII)


def content_lines(text: str) -> int:
    """Output lines that can stand for kept input lines (elision markers excluded)."""
    if not text:
        return 0
    body = text[:-1] if text.endswith("\n") else text
    return sum(1 for line in body.split("\n") if not _MARKER_LINE.match(line))


def missing_error_lines(source: str, output: str) -> list[str]:
    """Input lines matching ERROR_PATTERNS that are not whole lines of ``output``.

    Comparison ignores trailing whitespace only; a line merely contained in a longer output
    line does not count as present.
    """
    idx = error_line_indices(source)
    if not idx:
        return []
    lines = source.split("\n")
    present = {x.rstrip() for x in output.split("\n")}
    return [lines[i] for i in idx if lines[i].rstrip() not in present]


def result_problems(source: str, result: Any) -> list[str]:
    """Contract violations of one ``compress`` result: type, counts, dropped error lines.

    ``total_lines`` must equal ``line_count(source)``; ``kept_lines`` may not exceed the output's
    non-marker lines (``content_lines``) nor ``total_lines``.
    """
    if not isinstance(result, CompressResult):
        return [f"returned {type(result).__name__}, not a CompressResult"]
    if not isinstance(result.text, str):
        return [f"CompressResult.text is {type(result.text).__name__}, not str"]
    problems: list[str] = []
    kept, total = result.kept_lines, result.total_lines
    if (
        not isinstance(kept, int)
        or not isinstance(total, int)
        or isinstance(kept, bool)
        or isinstance(total, bool)
        or not 0 <= kept <= total
    ):
        problems.append(f"kept_lines/total_lines invalid: kept={kept!r} total={total!r}")
    else:
        expected = line_count(source)
        if total != expected:
            problems.append(f"total_lines={total} but the input has {expected} lines")
        shown = content_lines(result.text)
        if kept > shown:
            problems.append(f"kept_lines={kept} but the output shows only {shown} content lines")
    missing = missing_error_lines(source, result.text)
    if missing:
        problems.append(f"dropped {len(missing)} error line(s), e.g. {missing[0][:120]!r}")
    return problems


class CheckedCompressor:
    """Runs a third-party compressor under the runtime safety check.

    Any exception, malformed result or dropped error line disables the extension for the rest
    of the process (logged once) and the built-in ``fallback`` answers instead.
    """

    def __init__(self, inner: Compressor, fallback: Compressor, owner: Registry) -> None:
        self.inner = inner
        self.fallback = fallback
        self.name = inner.name
        self.handles = inner.handles
        self._owner = owner

    def compress(self, text: str, level: Level, ctx: CompressContext) -> CompressResult:
        if not self._owner.is_disabled(self.inner):
            try:
                with contextlib.redirect_stdout(sys.stderr):  # stdout is the host protocol
                    result: Any = self.inner.compress(text, level, ctx)
                problems = result_problems(text, result)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - third-party code
                problems = [f"{type(exc).__name__}: {exc}"]
            if not problems:
                return result  # type: ignore[no-any-return]
            self._owner.disable(self.inner, problems[0])
        return self.fallback.compress(text, level, ctx)


def unwrap(c: Compressor) -> Compressor:
    """The extension behind a CheckedCompressor (``c`` itself otherwise)."""
    return c.inner if isinstance(c, CheckedCompressor) else c


def _default_entry_points() -> Iterable[Any]:
    return metadata.entry_points(group=ENTRY_POINT_GROUP)


class Registry:
    """Maps content types to compressors.

    ``register`` is the in-process path (built-ins). Entry-point plugins load lazily, only when a
    lookup can use one: a content type without a built-in, or ``allow_override``. Built-ins win
    by default. Every extension compressor is returned wrapped in ``CheckedCompressor``.
    """

    def __init__(self, entry_points_fn: Callable[[], Iterable[Any]] | None = None) -> None:
        self._by_type: dict[str, Compressor] = {}
        self._builtins: list[Compressor] = []
        self._ext_by_type: dict[str, list[Compressor]] = {}
        self._entry_points_fn = entry_points_fn or _default_entry_points
        self._loaded = False
        self._fallback: Compressor | None = None
        self._disabled: dict[int, Compressor] = {}  # id -> object (kept alive: ids stay unique)

    def register(self, c: Compressor) -> None:
        if c not in self._builtins:
            self._builtins.append(c)
        for t in c.handles:
            self._by_type[t] = c

    def builtins(self) -> list[Compressor]:
        return list(self._builtins)

    def is_disabled(self, c: Compressor) -> bool:
        return id(c) in self._disabled

    def disable(self, c: Compressor, reason: str) -> None:
        if id(c) in self._disabled:
            return
        self._disabled[id(c)] = c
        name = getattr(c, "name", type(c).__name__)
        _config.log_error(os.environ, "extensions.compressor", f"{name}: disabled: {reason}")

    def for_type(
        self,
        t: str,
        *,
        allow_override: bool = False,
        extra: Callable[[str], Sequence[Compressor]] | None = None,
    ) -> Compressor:
        base = self._by_type.get(t)
        if base is None or allow_override:
            ext = self._extension_for(t, extra)
            if ext is not None:
                return CheckedCompressor(ext, base if base is not None else self._text(), self)
        return base if base is not None else self._text()

    def _text(self) -> Compressor:
        if self._fallback is None:
            from token_sparen.compress.text import TextCompressor

            self._fallback = TextCompressor()
        return self._fallback

    def _extension_for(
        self, t: str, extra: Callable[[str], Sequence[Compressor]] | None
    ) -> Compressor | None:
        if not self._loaded:
            self._loaded = True
            self._load_entry_points()
        candidates = list(self._ext_by_type.get(t, ()))
        if extra is not None:
            try:
                candidates.extend(extra(t))
            except Exception as exc:  # noqa: BLE001 - extension discovery never breaks a hook
                _config.log_error(os.environ, "extensions", f"drop-in lookup: {exc}")
        for c in candidates:
            if id(c) not in self._disabled:
                return c
        return None

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
                if not is_compressor(obj):
                    raise TypeError(f"{type(obj).__name__} is not a Compressor")
                for t in obj.handles:
                    self._ext_by_type.setdefault(t, []).append(obj)
            except Exception as exc:  # noqa: BLE001 - skip broken plugins
                _config.log_error(os.environ, "compress.registry", f"{name}: {exc}")


registry = Registry()
