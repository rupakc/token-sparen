"""Conformance kit for compressors (spec section 9).

``check_compressor(c)`` returns a list of human-readable problems; an empty list means ``c``
honours the contract every compressor must keep:

* shape: ``name`` is a non-empty str, ``handles`` a non-empty frozenset of str;
* error pinning: a planted error line survives at the light, standard and aggressive levels
  (no line matching ``ERROR_PATTERNS`` is ever dropped);
* order: kept input lines appear in their original order;
* counts: ``compress`` returns a ``CompressResult`` with integer ``0 <= kept <= total``;
* marker compatibility: ``text`` never contains the recovery marker (the mechanism appends it);
* size bound: a non-lossless result is never longer than its input;
* robustness: never raises on odd input (empty, NUL bytes, 200 000-deep ``[``, 1 MB).

Run it in your extension's tests: ``assert check_compressor(MyCompressor()) == []``.
"""

from __future__ import annotations

from typing import Any

from token_sparen.compress.base import (
    CompressContext,
    Level,
    load_level,
    result_problems,
)
from token_sparen.config import Config, packaged_defaults

ARMS = ("light", "standard", "aggressive")
PLANTED_ERROR = "E   AssertionError: conformance planted failure 7f3a"
MARKER_PREFIX = "[token-sparen: kept"


def _levels() -> dict[str, Level]:
    cfg = Config(data=packaged_defaults())
    return {arm: load_level(cfg, arm) for arm in ARMS}


def _sample() -> str:
    lines = [
        f"{i:04d} INFO worker step {i} processed batch of {i * 7 % 97} items in {i % 13} ms"
        for i in range(600)
    ]
    lines.insert(300, PLANTED_ERROR)
    return "\n".join(lines) + "\n"


def _odd_inputs() -> list[tuple[str, str, bool]]:
    """(label, text, run at every level)."""
    block = "".join(
        f"{i} lorem ipsum dolor sit amet {'ERROR boom' if i % 997 == 0 else 'ok'}\n"
        for i in range(30_000)
    )
    one_mb = (block * (1_000_000 // len(block) + 1))[:1_000_000]
    return [
        ("empty input", "", True),
        ("NUL bytes", "\x00" * 1000 + "\nERROR \x00 here\n" + "\x00\n" * 50, True),
        ("200 000-deep '['", "[" * 200_000, False),
        ("1 MB input", one_mb, False),
    ]


def _shape(c: Any) -> list[str]:
    issues: list[str] = []
    name = getattr(c, "name", None)
    if not isinstance(name, str) or not name:
        issues.append("name must be a non-empty str")
    handles = getattr(c, "handles", None)
    if (
        not isinstance(handles, frozenset)
        or not handles
        or not all(isinstance(t, str) for t in handles)
    ):
        issues.append("handles must be a non-empty frozenset of content-type strings")
    if not callable(getattr(c, "compress", None)):
        issues.append("compress must be callable")
    return issues


def _run(c: Any, text: str, level: Level, ctx: CompressContext) -> tuple[Any, str | None]:
    try:
        return c.compress(text, level, ctx), None
    except Exception as exc:  # noqa: BLE001 - reporting is the point
        return None, f"{type(exc).__name__}: {exc}"


def _order_issue(source: str, out: str) -> str | None:
    index: dict[str, int] = {}
    for i, line in enumerate(source.split("\n")):
        index.setdefault(line, i)
    last = -1
    for line in out.split("\n"):
        pos = index.get(line)
        if pos is None or not line:
            continue
        if pos <= last:
            return f"order not preserved: {line[:80]!r} appears after a later input line"
        last = pos
    return None


def _result_issues(source: str, result: Any, check_order: bool) -> list[str]:
    issues = result_problems(source, result)
    if issues and not hasattr(result, "text"):
        return issues
    text = getattr(result, "text", None)
    if not isinstance(text, str):
        return issues
    if MARKER_PREFIX in text:
        issues.append("text contains the recovery marker (the mechanism appends it)")
    if getattr(result, "lossless", False) is not True and len(text) > len(source):
        issues.append(f"non-lossless output longer than input ({len(text)} > {len(source)})")
    if check_order:
        problem = _order_issue(source, text)
        if problem is not None:
            issues.append(problem)
    return issues


def check_compressor(c: Any) -> list[str]:
    """Problems found in compressor ``c``; ``[]`` means it passes."""
    issues = _shape(c)
    if not callable(getattr(c, "compress", None)):
        return issues
    ctx = CompressContext(prompt=None, recent_paths=())
    levels = _levels()
    sample = _sample()
    for arm, level in levels.items():
        result, err = _run(c, sample, level, ctx)
        if err is not None:
            issues.append(f"{arm}: raised {err} on the sample")
            continue
        found = _result_issues(sample, result, check_order=True)
        issues.extend(f"{arm}: {p}" for p in found)
        text = getattr(result, "text", "")
        if isinstance(text, str) and PLANTED_ERROR not in text.split("\n"):
            if not any("error line" in p for p in found):
                issues.append(f"{arm}: planted error line was dropped")
    for label, text, every_level in _odd_inputs():
        arms = ARMS if every_level else ("standard",)
        for arm in arms:
            result, err = _run(c, text, levels[arm], ctx)
            if err is not None:
                issues.append(f"{arm}: raised {err} on {label}")
                continue
            issues.extend(
                f"{arm} on {label}: {p}" for p in _result_issues(text, result, check_order=False)
            )
    return issues
