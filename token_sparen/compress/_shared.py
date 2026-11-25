"""Helpers shared by the structured and report compressors (knobs, arms, never-raise guard)."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from token_sparen import config as _config
from token_sparen.compress.base import CompressContext, CompressResult, Level
from token_sparen.compress.text import TextCompressor

_TEXT = TextCompressor()


@lru_cache(maxsize=1)
def defaults() -> dict[str, Any]:
    return _config.packaged_defaults()


def arm(level: Level) -> str:
    if level.keep_ratio >= 0.5:
        return "light"
    return "standard" if level.keep_ratio >= 0.25 else "aggressive"


def _valid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _lookup(dotted: str, ctx: CompressContext) -> Any:
    return ctx.config.get(f"compression.{dotted}") if ctx.config is not None else None


def _packaged(dotted: str) -> Any:
    node: Any = defaults().get("compression", {})
    for part in dotted.split("."):
        node = node[part]
    return node


def setting(dotted: str, ctx: CompressContext) -> int:
    """Non-negative int at `compression.<dotted>`: `ctx.config` value, else the packaged one."""
    value = _lookup(dotted, ctx)
    if _valid(value):
        return int(value)
    node = _packaged(dotted)
    if not _valid(node):
        raise ValueError(f"no valid packaged default for compression.{dotted}")
    return int(node)


def choice(dotted: str, ctx: CompressContext, allowed: frozenset[str]) -> str:
    """String at `compression.<dotted>` limited to `allowed`, else the packaged default."""
    value = _lookup(dotted, ctx)
    if isinstance(value, str) and value in allowed:
        return value
    node = _packaged(dotted)
    if not isinstance(node, str) or node not in allowed:
        raise ValueError(f"no valid packaged default for compression.{dotted}")
    return node


def guarded(
    where: str,
    run: Callable[[str, Level, CompressContext], CompressResult],
    text: str,
    level: Level,
    ctx: CompressContext,
) -> CompressResult:
    """Never raise: any failure is logged and the text compressor handles the output."""
    try:
        return run(text, level, ctx)
    except Exception as exc:  # noqa: BLE001 - compression must never break a hook
        _config.log_error(os.environ, f"compress.{where}", f"{type(exc).__name__}: {exc}")
        return _TEXT.compress(text, level, ctx)


def split_lines(text: str) -> tuple[list[str], str]:
    body, trailer = (text[:-1], "\n") if text.endswith("\n") else (text, "")
    return body.split("\n"), trailer


def unchanged(text: str, total: int) -> CompressResult:
    return CompressResult(text=text, kept_lines=total, total_lines=total, lossless=True)
